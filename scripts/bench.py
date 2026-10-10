r"""
Benchmark reproducible del ARNÉS de APRENDIZ (sin red, sin API, determinista).

Compara el mismo código con flags v0.2 apagados (= comportamiento v0.1.3) y encendidos, sobre un escritorio
simulado (tests/sim_desktop.py) y un "modelo" guionizado IDÉNTICO en ambos brazos. Así cualquier diferencia
viene del arnés (guardas, reconexión, memoria, percepción), no del modelo.

Lo que NO mide: la calidad visual de Claude (grounding real, lectura de etiquetas). Eso exige el escritorio
oficial y la API; aquí el "modelo" ve una instantánea estructurada de cada captura que realmente se le envió.

Uso:  python scripts\bench.py            (tabla)
      python scripts\bench.py --json     (JSON para comparar corridas)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import types
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TEAM_TOKEN", "bench")

from app import agent as agent_mod, config, llm  # noqa: E402
from app.agent import TaskRecord, TaskRunner, _SingleTask  # noqa: E402
from app.guardian import Secrets  # noqa: E402
from app.memory import AppMemory, DRIFT_THRESHOLD, ui_distance, ui_signature  # noqa: E402
from tests.sim_desktop import FakeApp, FakeRFB, THEMES  # noqa: E402

IMG_TOKENS = round(1280 * 800 / 750)  # estimación de tokens por captura completa (fórmula pública w*h/750)
FLAGS = ("MEMORY_V2", "RECOVERY_V2", "PERCEPTION_V2")


# --------------------------------------------------------------------------- modelo guionizado
@dataclass
class Policy:
    """Empleado razonable pero ingenuo: si tras Guardar no ve cambio, vuelve a pulsar Guardar."""
    target: str
    trust_map_coords: bool = True       # usa coordenadas del mapa si las encuentra (como haría un modelo)
    errors: int = 0
    tried_map: set = field(default_factory=set)
    last: str = ""
    prev_seen: object = None

    def decide(self, seen, last_text: str, first_text: str):
        if "Guarda anti-duplicados" in last_text:
            return self._t("wait", {"seconds": 2, "why": "comprobar si el guardado se aplicó"})
        if "no se pudo ejecutar" in last_text:
            self.errors += 1
            if self.errors >= 3:
                return self._t("done", {"estado": "fallida", "resumen": "Las acciones dejaron de responder.",
                                        "verificado_en_pantalla": False, "evidencia": "errores repetidos"})
        if seen.modal:
            return self._click(seen, "ok", "lectura", "cerrar aviso informativo")
        unchanged, self.prev_seen = seen == self.prev_seen, seen
        if seen.screen == "home":
            if self.last in ("nav", "wait") and unchanged:
                self.last = "wait"
                return self._t("wait", {"seconds": 2, "why": "la lista está cargando"})
            mc = self._map_coord(first_text, "nav")
            if mc and "nav" not in self.tried_map:
                self.tried_map.add("nav")
                self.last = "nav-map"
                return self._t("click", {"x": mc[0], "y": mc[1], "why": "ir al módulo según el mapa", "risk": "lectura"})
            return self._click(seen, "nav", "lectura", "abrir el módulo de registros")
        if seen.screen == "list":
            if self.target in seen.records:
                return self._t("done", {"estado": "terminada", "resumen": f"Registro {self.target} creado y visible en la lista.",
                                        "verificado_en_pantalla": True, "evidencia": "fila en la lista"})
            return self._click(seen, "new", "lectura", "abrir formulario nuevo")
        if seen.screen == "form":
            if seen.field_value != self.target:
                x, y = seen.buttons["field"]
                self.last = "type"
                return self._t("type_text", {"text": self.target, "x": x, "y": y, "why": "llenar nombre",
                                             "risk": "cambio_esperado"})
            return self._click(seen, "save", "cambio_esperado", "guardar el registro")
        return self._t("wait", {"seconds": 1, "why": "pantalla desconocida"})

    def _map_coord(self, first_text: str, role: str):
        if not self.trust_map_coords:
            return None
        label = THEMES["clasico"]["labels"][role]
        m = re.search(re.escape(label) + r"[^(]{0,40}\(x≈(\d+),\s*y≈(\d+)\)", first_text)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _click(self, seen, role, risk, why):
        x, y = seen.buttons[role]
        self.last = role
        return self._t("click", {"x": x, "y": y, "why": why, "risk": risk})

    @staticmethod
    def _t(name, inp):
        return name, inp


def _images(content):
    for b in content if isinstance(content, list) else []:
        if b.get("type") == "image":
            yield b
        elif b.get("type") == "tool_result":
            yield from _images(b.get("content"))


def _texts(content):
    out = []
    for b in content if isinstance(content, list) else []:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "tool_result":
            out.extend(_texts(b.get("content")))
    return out


def _snap_of(block, registry):
    png = base64.b64decode(block["source"]["data"])
    return registry.get(hashlib.md5(png).hexdigest())


# --------------------------------------------------------------------------- escenarios
@dataclass
class Scenario:
    name: str
    what: str
    app_kw: dict
    expected: str = "terminada"
    memory_map: dict | None = None
    memory_theme: str = "clasico"


TARGET = "Registro Prueba 77"
OLD_MAP = {"app": "App Sim", "modulos": [{"nombre": "Registros", "como_llegar": "barra superior, botón 'Registros' (x≈135,y≈40)",
                                          "contiene": "lista y botón Nuevo"}],
           "recetas": {"crear": ["Registros", "Nuevo", "escribir nombre", "Guardar"]}}

SCENARIOS = [
    Scenario("crear_normal", "control: app rápida, sin incidentes", {}),
    Scenario("guardado_lento", "Guardar tarda en repintar; operación ya realizada", {"slow_ticks": 2}),
    Scenario("corte_vnc", "la conexión cae justo después de entregar Guardar", {"drop_after_save": True}),
    Scenario("rediseño_modal", "tema nuevo, botones movidos, etiquetas renombradas, modal al iniciar; mapa viejo con coordenadas",
             {"theme": "nuevo", "modal": True}, memory_map=OLD_MAP),
    Scenario("carga_lenta", "el módulo tarda en cargar; el operador espera", {"slow_nav": 3}),
]


@dataclass
class Metrics:
    estado: str = ""
    correcto: bool = False
    falso_exito: bool = False
    duplicados: int = 0
    pasos: int = 0
    llamadas_llm: int = 0
    imagenes_enviadas: int = 0      # suma de imágenes en todas las peticiones del Operator
    tokens_imagen_est: int = 0
    kb_historial_sin_cache: float = 0.0
    reconexiones: int = 0
    bloqueos_guarda: int = 0
    clics_en_vacio: int = 0
    drift_detectado: bool | None = None
    ms: float = 0.0


async def run_scenario(sc: Scenario, v2: bool) -> Metrics:
    m = Metrics()
    registry: dict = {}
    kw = dict(sc.app_kw)
    slow_nav = kw.pop("slow_nav", 0)
    app = FakeApp(**kw)
    if slow_nav:
        orig = app._press

        def press(role, _o=orig):
            if role == "nav" and not app._pending_screen and app.screen != "list":
                app._pending_screen, app._pending_left = "list", slow_nav
                return False
            return _o(role)
        app._press = press

    tmp = tempfile.mkdtemp(prefix="aprendiz-bench-")
    memory = AppMemory(os.path.join(tmp, "maps.json"))
    if sc.memory_map:
        old = FakeApp(theme=sc.memory_theme)
        memory.maps["app-sim"] = {"app": "App Sim", "actualizado": time.time() - 3600, "mapa": sc.memory_map,
                                  "exitos": 3, "fallos": 0,
                                  "ui_sig": ui_signature(old.render()[:, :, [2, 1, 0]])}
    policy = Policy(TARGET)
    prev_msgs: list[str] = []
    fast = types.ModuleType("asyncio_fast")
    fast.__dict__.update(asyncio.__dict__)

    async def _no_sleep(_t=0, *a, **k):
        await asyncio.sleep(0)
    fast.sleep = _no_sleep

    async def fake_create(model, system, messages, tools=None, max_tokens=0, attempts=3, cache_messages=False, **kw2):
        m.llamadas_llm += 1
        ser = [json.dumps(x, sort_keys=True, ensure_ascii=False) for x in messages]
        total = sum(len(s) for s in ser)
        common = 0
        for a, b in zip(prev_msgs, ser):
            if a != b:
                break
            common += len(a)
        prev_msgs[:] = ser
        m.kb_historial_sin_cache += (total - (common if cache_messages else 0)) / 1024
        imgs = [b for msg in messages if msg["role"] == "user" for b in _images(msg["content"])]
        m.imagenes_enviadas += len(imgs)
        last_img = imgs[-1] if imgs else None
        seen = _snap_of(last_img, registry) if last_img else None
        last_text = " ".join(_texts(messages[-1]["content"]))
        first_text = " ".join(_texts(messages[0]["content"]))
        name, inp = policy.decide(seen, last_text, first_text)
        if name == "click":
            hit = any(abs(inp["x"] - cx) <= 75 and abs(inp["y"] - cy) <= 20 for cx, cy in seen.buttons.values())
            m.clics_en_vacio += 0 if hit else 1
        return SimpleNamespace(stop_reason="tool_use", content=[
            SimpleNamespace(type="text", text="razonamiento breve"),
            SimpleNamespace(type="tool_use", id=f"tu{m.llamadas_llm}", name=name, input=inp)])

    async def fake_call_json(model, system, content, max_tokens=0, role="json", **kw2):
        m.llamadas_llm += 1
        if role == "planner":
            return {"app_nombre": "App Sim", "tipo": "crear", "objetivo": f"crear {TARGET}", "datos": {"nombre": TARGET},
                    "criterios_exito": ["registro visible en la lista"], "acciones_prohibidas": ["otros cambios"]}
        if role == "verifier":
            imgs = list(_images(content))
            seen = _snap_of(imgs[-1], registry)  # CAPTURA ACTUAL: lo único que el Verifier puede ver
            ok = seen is not None and seen.screen == "list" and TARGET in seen.records
            return {"verificado": ok, "motivo": "fila visible" if ok else "no se ve el registro",
                    "riesgo_cambios_extra": "ninguno", "respuesta_corregida": None}
        return {}

    def make_rfb(self, timeout):
        return FakeRFB(app, registry)

    with ExitStack() as st:
        for f in FLAGS:
            st.enter_context(patch.object(config, f, v2))
        st.enter_context(patch.object(agent_mod, "asyncio", fast))
        st.enter_context(patch.object(llm, "create", fake_create))
        st.enter_context(patch.object(llm, "call_json", fake_call_json))
        st.enter_context(patch.object(_SingleTask, "_make_rfb", make_rfb))
        rec = TaskRecord(task_id=f"{sc.name}-{'v2' if v2 else 'v1'}", tarea=f"Registra {TARGET}.")
        task = _SingleTask(TaskRunner(memory), rec, "wss://sim.invalid/websockify", "pw", Secrets(password="pw"))
        t0 = time.perf_counter()
        await asyncio.wait_for(task.run(), timeout=60)
        m.ms = round((time.perf_counter() - t0) * 1000, 1)
        if v2 and task.map_meta:
            m.drift_detectado = bool(task.map_meta.get("drift"))
    m.estado = rec.estado
    m.pasos = rec.steps
    m.duplicados = max(0, app.records.count(TARGET) - 1)
    created_ok = app.records.count(TARGET) == 1
    m.correcto = rec.estado == sc.expected and created_ok
    m.falso_exito = rec.estado == "terminada" and not created_ok
    m.reconexiones = task.reconnects
    m.bloqueos_guarda = sum(1 for a in rec.actions if a.blocked)
    m.tokens_imagen_est = m.imagenes_enviadas * IMG_TOKENS
    m.kb_historial_sin_cache = round(m.kb_historial_sin_cache, 1)
    return m


def drift_detector_eval() -> list[dict]:
    """Precisión del detector de drift sobre pares sintéticos con verdad conocida."""
    def sig(**kw):
        a = FakeApp(**{k: v for k, v in kw.items() if k != "records"})
        if "records" in kw:
            a.records = kw["records"]
        return ui_signature(a.render()[:, :, [2, 1, 0]])
    base = sig(screen="list")
    cases = [
        ("mismos datos, misma UI", sig(screen="list"), False),
        ("otros datos, misma UI", sig(screen="list", records=["Gamma", "Delta", "Épsilon", "Zeta", "Eta"]), False),
        ("tema nuevo + botones movidos", sig(screen="list", theme="nuevo"), True),
        ("modal nuevo encima", sig(screen="list", modal=True), True),
    ]
    out = []
    for name, s, expect in cases:
        d = ui_distance(base, s)
        out.append({"caso": name, "distancia": d, "drift": d >= DRIFT_THRESHOLD, "esperado": expect,
                    "ok": (d >= DRIFT_THRESHOLD) == expect})
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    import logging
    logging.disable(logging.WARNING)
    rows = []
    for sc in SCENARIOS:
        for v2 in (False, True):
            m = await run_scenario(sc, v2)
            rows.append({"escenario": sc.name, "version": "v0.2" if v2 else "v0.1.3", **m.__dict__})
    drift = drift_detector_eval()
    if args.json:
        print(json.dumps({"escenarios": rows, "drift": drift, "umbral": DRIFT_THRESHOLD}, ensure_ascii=False, indent=1))
        return
    cols = ["escenario", "version", "estado", "correcto", "falso_exito", "duplicados", "pasos", "llamadas_llm",
            "imagenes_enviadas", "tokens_imagen_est", "kb_historial_sin_cache", "reconexiones", "bloqueos_guarda",
            "clics_en_vacio"]
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))
    print("\nDetector de drift (umbral %.2f):" % DRIFT_THRESHOLD)
    for d in drift:
        print(f"  {d['caso']:<32} dist={d['distancia']:.4f} drift={d['drift']} esperado={d['esperado']} {'OK' if d['ok'] else 'FALLO'}")
    for v in ("v0.1.3", "v0.2"):
        sel = [r for r in rows if r["version"] == v]
        print(f"\n{v}: correctas {sum(r['correcto'] for r in sel)}/{len(sel)} · falsos éxitos {sum(r['falso_exito'] for r in sel)} · "
              f"duplicados {sum(r['duplicados'] for r in sel)} · pasos {sum(r['pasos'] for r in sel)} · "
              f"llamadas LLM {sum(r['llamadas_llm'] for r in sel)} · tokens imagen est. {sum(r['tokens_imagen_est'] for r in sel)} · "
              f"KB historial sin caché {round(sum(r['kb_historial_sin_cache'] for r in sel), 1)}")


if __name__ == "__main__":
    asyncio.run(main())
