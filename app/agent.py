"""
Orquestador de una tarea: conecta al escritorio, planifica, opera, verifica y reporta.
Un modelo, cinco roles: Planner, Operator, Verifier, Guardian (determinista) y Cartographer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import config, llm, prompts
from .guardian import ActionRecord, LoopDetector, Secrets, check_action, format_summary
from .memory import AppMemory
from .rfb import RFBClient, RFBError
from .tools import ACTION_TOOLS, TOOLS

log = logging.getLogger("aprendiz.agent")

CHANGE_AREA = 2500  # px² repintados para considerar que la pantalla cambió (ignora el cursor del ratón)


@dataclass
class TaskRecord:
    task_id: str
    tarea: str
    estado: str = "en_curso"
    resumen: str = ""
    created: float = field(default_factory=time.time)
    started_mono: float = field(default_factory=time.monotonic)
    finished: Optional[float] = None
    plan: dict = field(default_factory=dict)
    actions: list[ActionRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    steps: int = 0
    verifier: dict = field(default_factory=dict)
    error: str = ""
    app_name: str = ""
    worker: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {"estado": self.estado, "resumen": self.resumen}

    def debug(self) -> dict:
        return {
            **self.public(),
            "task_id": self.task_id,
            "tarea": self.tarea,
            "app": self.app_name,
            "pasos": self.steps,
            "duracion_s": round((self.finished or time.time()) - self.created, 1),
            "plan": self.plan,
            "acciones": [a.short() for a in self.actions],
            "notas": self.notes,
            "verifier": self.verifier,
            "error": self.error,
        }


class TaskRunner:
    def __init__(self, memory: AppMemory):
        self.memory = memory
        self.tasks: dict[str, TaskRecord] = {}

    def submit(self, tarea: str, url: str, password: str) -> TaskRecord:
        rec = TaskRecord(task_id=uuid.uuid4().hex[:12], tarea=tarea)
        self.tasks[rec.task_id] = rec
        rec.worker = asyncio.create_task(self._run_guarded(rec, url, password))
        return rec

    # ------------------------------------------------------------------ control
    async def _run_guarded(self, rec: TaskRecord, url: str, password: str):
        secrets = Secrets(password=password, url=url, token=config.TEAM_TOKEN)
        runner = _SingleTask(self, rec, url, password, secrets)
        try:
            await asyncio.wait_for(runner.run(), timeout=config.HARD_LIMIT)
        except asyncio.TimeoutError:
            runner.finalize("fallida", "Se agotó el tiempo antes de verificar el resultado.", None, None,
                            extra="Acciones realizadas: " + "; ".join(a.short() for a in rec.actions[-6:]))
        except Exception as e:  # pragma: no cover - red de seguridad
            log.exception("tarea %s: error no controlado", rec.task_id)
            rec.error = f"{type(e).__name__}: {e}"
            runner.finalize("fallida", "Error técnico del agente al operar el escritorio; no se completó la tarea.", None, None)
        finally:
            await runner.close()
            rec.finished = rec.finished or time.time()
            self._write_log(rec)
            # Consolidar el mapa de la app (no toca el escritorio)
            if rec.app_name and rec.actions:
                asyncio.create_task(runner.cartographer())

    def _write_log(self, rec: TaskRecord):
        try:
            os.makedirs(config.LOG_DIR, exist_ok=True)
            with open(os.path.join(config.LOG_DIR, f"{rec.task_id}.json"), "w", encoding="utf-8") as f:
                json.dump(rec.debug(), f, ensure_ascii=False, indent=1)
        except Exception as e:
            log.warning("no se pudo escribir log: %s", e)


class _SingleTask:
    def __init__(self, runner: TaskRunner, rec: TaskRecord, url: str, password: str, secrets: Secrets):
        self.runner = runner
        self.rec = rec
        self.url = url
        self.password = password
        self.secrets = secrets
        self.rfb: Optional[RFBClient] = None
        self.t0 = rec.started_mono
        self.loop = LoopDetector()
        self.messages: list = []
        self.last_shots: list[bytes] = []
        # Evidencia anclada: capturas de páginas leídas que no se pierden al podar el historial.
        self.evidence_shots: list[tuple[int, str, bytes]] = []
        self.done = False
        self._pending_feedback: tuple[str, bytes] | None = None

    # ----------------------------------------------------------------- tiempo
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def remaining_operator(self) -> float:
        return config.OPERATOR_DEADLINE - self.elapsed()

    def fmt_remaining(self) -> str:
        r = max(0, int(self.remaining_operator()))
        return f"{r // 60}:{r % 60:02d}"

    # ---------------------------------------------------------------- reporte
    def finalize(self, estado: str, resumen: str, respuesta: Optional[str], claves: Optional[list], extra: str = ""):
        if self.done:
            return
        self.done = True
        self.rec.estado = estado
        self.rec.resumen = format_summary(estado, self.rec.plan, resumen, respuesta, claves, self.secrets, extra)
        self.rec.finished = time.time()
        log.info("tarea %s -> %s (%.0fs, %d pasos): %s", self.rec.task_id, estado, self.elapsed(), self.rec.steps,
                 self.rec.resumen[:200].replace("\n", " | "))

    async def close(self):
        if self.rfb is not None:
            try:
                await self.rfb.close()
            except Exception:
                pass
            self.rfb = None

    # ------------------------------------------------------------------- run
    async def run(self):
        rec = self.rec
        # 1) Conectar al escritorio (un reintento)
        for attempt in (1, 2):
            try:
                self.rfb = RFBClient(self.url, self.password, encodings=config.encodings_list(),
                                     connect_timeout=config.VNC_CONNECT_TIMEOUT)
                await self.rfb.connect()
                break
            except Exception as e:
                log.warning("conexión VNC fallida (intento %d): %s", attempt, e)
                await self.close()
                if attempt == 2:
                    rec.error = f"vnc: {type(e).__name__}"
                    self.finalize("fallida", "No se pudo conectar al escritorio para ejecutar la tarea.", None, None)
                    return
                await asyncio.sleep(2)
        rfb = self.rfb
        assert rfb is not None
        await rfb.wait_stable(min_wait=0.6)
        shot = rfb.screenshot_png()
        self._save_shot(shot, 0)

        # 2) Planner
        try:
            plan = await llm.call_json(
                config.MODEL_PLANNER, prompts.PLANNER_SYSTEM,
                [{"type": "text", "text": f"TAREA: {rec.tarea}\n\nCaptura inicial del escritorio:"}, llm.image_block(shot)],
                max_tokens=1200,
            )
        except Exception as e:
            log.warning("planner falló: %s", e)
            plan = {"app_nombre": "desconocida", "tipo": "otro", "objetivo": rec.tarea, "datos": {},
                    "criterios_exito": ["evidencia visible en pantalla de que la tarea quedó hecha"],
                    "acciones_prohibidas": ["cualquier cambio no pedido"], "pasos_sugeridos": []}
        if not isinstance(plan, dict):
            plan = {"tipo": "otro", "objetivo": rec.tarea}
        plan.setdefault("tipo", "otro")
        rec.plan = plan
        rec.app_name = str(plan.get("app_nombre") or rfb.name or "desconocida")

        dec = plan.get("decision_inmediata")
        if isinstance(dec, dict) and dec.get("estado") == "fallida" and plan.get("tipo") in ("peligrosa", "imposible"):
            self.finalize("fallida", dec.get("motivo") or "La tarea no se puede ejecutar de forma segura.", None,
                          dec.get("claves") or [])
            return

        app_map = self.runner.memory.get(rec.app_name)

        # 3) Operator loop
        first = prompts.operator_first_message(rec.tarea, plan, app_map, self.remaining_operator(), 1, config.MAX_STEPS)
        self.messages = [{"role": "user", "content": [{"type": "text", "text": first}, llm.image_block(shot)]}]
        self.last_shots = [shot]
        verify_rounds = 0
        no_tool_strikes = 0

        for step in range(1, config.MAX_STEPS + 1):
            if self.done:
                return
            if self.remaining_operator() < 6:
                self._timeout_finalize()
                return
            rec.steps = step
            resp = await llm.create(
                config.MODEL_OPERATOR, prompts.OPERATOR_SYSTEM, self.messages, tools=TOOLS, max_tokens=1024,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
            blocks = []
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            if not blocks:
                blocks = [{"type": "text", "text": "(sin contenido)"}]
            self.messages.append({"role": "assistant", "content": blocks})

            tool_uses = [b for b in blocks if b["type"] == "tool_use"]
            if not tool_uses:
                no_tool_strikes += 1
                self.messages.append({"role": "user", "content": [{"type": "text", "text":
                    "Debes llamar a UNA herramienta en cada turno (click, type_text, zoom, done, ...)."}]})
                if no_tool_strikes >= 3:
                    self.finalize("fallida", "El agente no pudo decidir acciones sobre la app.", None, None)
                    return
                continue
            no_tool_strikes = 0

            results = []
            for i, tu in enumerate(tool_uses):
                if i > 0:
                    results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                    "content": [{"type": "text", "text": "Ignorada: una sola acción por turno."}]})
                    continue
                content = await self._execute(tu, step, verify_rounds)
                if self.done:
                    return
                if content is None:  # done aceptado pero verificación pidió otra ronda
                    verify_rounds += 1
                    content = self._verify_feedback_content()
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": content})
            self.messages.append({"role": "user", "content": results})
            self._prune_images()

        # Se acabaron los pasos
        self._timeout_finalize("Se agotó el número máximo de pasos sin verificar el resultado.")

    def _timeout_finalize(self, motivo: str = "Se agotó el tiempo disponible sin verificar el resultado."):
        hechas = "; ".join(a.short() for a in self.rec.actions[-5:])
        self.finalize("fallida", motivo, None, None, extra=("Últimas acciones: " + hechas) if hechas else "")

    # ------------------------------------------------------------- ejecución
    async def _execute(self, tu: dict, step: int, verify_rounds: int):
        name, args = tu["name"], dict(tu["input"] or {})
        rec, rfb = self.rec, self.rfb
        assert rfb is not None
        why, risk = str(args.get("why", "")), str(args.get("risk", ""))

        if name == "done":
            return await self._handle_done(args, verify_rounds)

        if name == "remember":
            note = str(args.get("note", "")).strip()[:300]
            if note:
                rec.notes.append(note)
                # La nota debe ir acompañada de la pantalla que efectivamente ve el operador.
                # Es una captura en memoria, no una acción de la app, y no modifica datos.
                if rec.plan.get("tipo") == "consultar":
                    shot = rfb.screenshot_png()
                    self.evidence_shots.append((step, note, shot))
                    # Presupuesto acotado para el Verifier: máximo seis pantallas ancladas.
                    if len(self.evidence_shots) > 6:
                        self.evidence_shots.pop(0)
            rec.actions.append(ActionRecord(step, name, {"note": note}, "conservar evidencia", "lectura"))
            detail = ("Nota y captura visual conservadas para el Verifier." if note and rec.plan.get("tipo") == "consultar"
                      else "Nota guardada.")
            return [{"type": "text", "text": f"{detail} Tiempo restante: {self.fmt_remaining()}."}]

        if name == "zoom":
            try:
                png, box = rfb.zoom_png(int(args["x0"]), int(args["y0"]), int(args["x1"]), int(args["y1"]))
            except Exception as e:
                return [{"type": "text", "text": f"Zoom inválido: {e}"}]
            rec.actions.append(ActionRecord(step, name, {k: args.get(k) for k in ("x0", "y0", "x1", "y1")}, why, "lectura"))
            return [{"type": "text", "text": f"Zoom de la región {box} (ampliada). Las coordenadas para clics siguen siendo las de la pantalla completa. Tiempo restante: {self.fmt_remaining()}."},
                    llm.image_block(png)]

        if name == "wait":
            secs = max(0.5, min(float(args.get("seconds", 2)), 10.0))
            await asyncio.sleep(secs)
            await rfb.wait_stable(min_wait=0.2)
            shot = rfb.screenshot_png()
            self._remember_shot(shot, step)
            rec.actions.append(ActionRecord(step, name, {"seconds": secs}, why, "lectura"))
            return [{"type": "text", "text": f"Esperé {secs:.0f}s. Captura actual. Tiempo restante: {self.fmt_remaining()}."},
                    llm.image_block(shot)]

        if name in ACTION_TOOLS:
            block = check_action(name, args, rec.plan)
            if block:
                rec.actions.append(ActionRecord(step, name, args, why, risk, blocked=True))
                return [{"type": "text", "text": block + f" Tiempo restante: {self.fmt_remaining()}."}]
            t_act = time.monotonic()
            try:
                await self._do_action(name, args)
            except (RFBError, KeyError, ValueError) as e:
                rec.actions.append(ActionRecord(step, name, args, why, risk))
                return [{"type": "text", "text": f"La acción no se pudo ejecutar ({e}). Revisa los parámetros."}]
            await rfb.wait_stable()
            changed = rfb.area_since(t_act) >= CHANGE_AREA
            shot = rfb.screenshot_png()
            self._remember_shot(shot, step)
            rec.actions.append(ActionRecord(step, name, args, why, risk, changed=changed))
            warn = self.loop.push(name, args, changed)
            desc = {k: v for k, v in args.items() if k not in ("why", "risk")}
            txt = (f"Paso {step} ejecutado: {name}({', '.join(f'{k}={v!r}' for k, v in desc.items())}). "
                   f"Pantalla: {'cambió' if changed else 'SIN cambio visible'}. "
                   f"Tiempo restante: {self.fmt_remaining()} · próximo paso {step + 1}/{config.MAX_STEPS}.")
            if warn:
                txt += "\n" + warn
            if self.remaining_operator() < 45:
                txt += "\n⏰ Quedan menos de 45 s: llama a done AHORA con lo que hayas verificado (o fallida si no verificaste)."
            if rfb.closed:
                txt += "\n⚠ La conexión con el escritorio se cerró."
            return [{"type": "text", "text": txt}, llm.image_block(shot)]

        return [{"type": "text", "text": f"Herramienta desconocida: {name}"}]

    async def _do_action(self, name: str, a: dict):
        rfb = self.rfb
        assert rfb is not None
        if name == "click":
            await rfb.click(int(a["x"]), int(a["y"]))
        elif name == "double_click":
            await rfb.click(int(a["x"]), int(a["y"]), count=2)
        elif name == "right_click":
            await rfb.click(int(a["x"]), int(a["y"]), button=4)
        elif name == "type_text":
            if a.get("x") is not None and a.get("y") is not None:
                await rfb.click(int(a["x"]), int(a["y"]))
                await asyncio.sleep(0.35)
            if a.get("replace", True):
                await rfb.select_all()
            await rfb.type_text(str(a.get("text", "")))
            if a.get("enter"):
                await asyncio.sleep(0.1)
                await rfb.key_combo("Return")
            elif a.get("tab"):
                await asyncio.sleep(0.1)
                await rfb.key_combo("Tab")
        elif name == "press_key":
            n = max(1, min(int(a.get("repeat") or 1), 20))
            for _ in range(n):
                await rfb.key_combo(str(a["key"]))
                await asyncio.sleep(0.05)
        elif name == "scroll":
            await rfb.scroll(int(a["x"]), int(a["y"]), str(a.get("direction", "down")), int(a.get("amount") or 3))
        elif name == "drag":
            await rfb.drag(int(a["x0"]), int(a["y0"]), int(a["x1"]), int(a["y1"]))

    # ---------------------------------------------------------------- done
    async def _handle_done(self, args: dict, verify_rounds: int):
        rec, rfb = self.rec, self.rfb
        assert rfb is not None
        estado = str(args.get("estado", "fallida"))
        resumen = str(args.get("resumen", ""))
        respuesta = args.get("respuesta")
        claves = args.get("claves") or []
        visto = bool(args.get("verificado_en_pantalla"))
        evidencia = str(args.get("evidencia", ""))
        rec.actions.append(ActionRecord(rec.steps, "done", {"estado": estado, "verificado": visto, "evidencia": evidencia[:120]}))

        if estado != "terminada":
            self.finalize("fallida", resumen, None, [str(c) for c in claves])
            return []

        # Verificación independiente con captura fresca
        can_verify = self.elapsed() < config.VERIFY_DEADLINE - 12
        if can_verify:
            await rfb.wait_stable(min_wait=0.3)
            fresh = rfb.screenshot_png()
            self._remember_shot(fresh, rec.steps)
            try:
                ver = await asyncio.wait_for(self._verify(resumen, respuesta, evidencia), timeout=max(5, config.VERIFY_DEADLINE - self.elapsed()))
            except Exception as e:
                log.warning("verifier falló: %s", e)
                # Nunca convertir un fallo técnico del verificador en éxito.
                ver = {"verificado": False, "motivo": f"verificador no disponible ({type(e).__name__})"}
            rec.verifier = ver
            riesgo = str(ver.get("riesgo_cambios_extra") or "").strip().lower()
            verificado = ver.get("verificado") is True and (not riesgo or riesgo == "ninguno")
            if verificado:
                correccion = ver.get("respuesta_corregida")
                if self.rec.plan.get("tipo") == "consultar" and correccion is not None:
                    # La línea respuesta es evaluada por separado; priorizar el dato
                    # que el verificador dice haber observado en la captura fresca.
                    respuesta = str(correccion)
                if self.rec.plan.get("tipo") == "consultar" and not str(respuesta or "").strip():
                    verificado = False
                    ver["motivo"] = "consulta sin dato de respuesta comprobable"
            if verificado:
                self.finalize("terminada", resumen, respuesta, None)
                return []
            # No verificado: ¿hay tiempo para una ronda más del operador?
            if verify_rounds < 1 and self.remaining_operator() > 35:
                self._pending_feedback = (ver.get("motivo") or "no se ve evidencia suficiente", fresh)
                return None
            self.finalize("fallida", f"No se pudo confirmar el resultado en pantalla: {ver.get('motivo', '')}. " + resumen,
                          None, [str(c) for c in claves])
            return []
        # Sin tiempo para una verificación independiente, nunca declarar éxito.
        self.finalize("fallida", "No hubo tiempo para verificar independientemente el resultado. " + resumen, None, None)
        return []

    def _verify_feedback_content(self) -> list:
        motivo, fresh = self._pending_feedback or ("sin evidencia", self.last_shots[-1])
        self._pending_feedback = None
        return [{"type": "text", "text":
                 f"VERIFICADOR: todavía NO está verificado: {motivo}. Muestra la evidencia en pantalla (abre la ficha o la lista, aplica el filtro, lee el mensaje) y vuelve a llamar a done. "
                 f"Si no puedes demostrarlo, reporta fallida. Tiempo restante: {self.fmt_remaining()}."},
                llm.image_block(fresh)]

    async def _verify(self, resumen: str, respuesta, evidencia: str) -> dict:
        rec = self.rec
        text = (
            f"TAREA: {rec.tarea}\n\nPLAN: {json.dumps(rec.plan, ensure_ascii=False)}\n\n"
            f"RESUMEN DEL OPERATOR: {resumen}\nRESPUESTA DEL OPERATOR: {respuesta!r}\nEVIDENCIA QUE DICE HABER VISTO: {evidencia}\n\n"
            "ACCIONES EJECUTADAS:\n" + "\n".join(a.short() for a in rec.actions) +
            "\n\nCAPTURAS ANCLADAS EN PASOS ANTERIORES Y CAPTURA ACTUAL. "
            "Las notas del Operator NO son pruebas por sí solas: lee los datos de cada imagen."
        )
        content = [{"type": "text", "text": text}]
        # Fotografías de páginas previas: sobreviven a _prune_images y no dependen de la pantalla final.
        for step, note, shot in self.evidence_shots:
            content.extend([
                {"type": "text", "text": f"CAPTURA ANCLADA en paso {step}. Etiqueta del operador (verificar contra imagen): {note}"},
                llm.image_block(shot),
            ])
        # Capturas actuales: para confirmar el filtro visible y el estado final.
        for i, shot in enumerate(self.last_shots[-2:]):
            content.extend([
                {"type": "text", "text": "CAPTURA ACTUAL (fresca)" if i == len(self.last_shots[-2:]) - 1 else "CAPTURA RECIENTE"},
                llm.image_block(shot),
            ])
        return await llm.call_json(config.MODEL_VERIFIER, prompts.VERIFIER_SYSTEM, content, max_tokens=800)

    # ----------------------------------------------------------- cartographer
    async def cartographer(self):
        rec = self.rec
        try:
            old = self.runner.memory.get(rec.app_name) or {}
            text = (
                f"MAPA ANTERIOR: {json.dumps(old, ensure_ascii=False)}\n\nAPP: {rec.app_name}\nTAREA: {rec.tarea}\n"
                f"PLAN: {json.dumps(rec.plan, ensure_ascii=False)}\nRESULTADO: {rec.estado}\n"
                "ACCIONES:\n" + "\n".join(a.short() for a in rec.actions) +
                "\nNOTAS DEL OPERATOR:\n" + "\n".join(f"- {n}" for n in rec.notes)
            )
            mapa = await asyncio.wait_for(llm.call_json(config.MODEL_CARTOGRAPHER, prompts.CARTOGRAPHER_SYSTEM, text, max_tokens=1500), timeout=60)
            if isinstance(mapa, dict) and mapa:
                s = json.dumps(mapa, ensure_ascii=False)
                if len(s) > 3500:
                    mapa["notas"] = (mapa.get("notas") or [])[:8]
                    mapa["recetas"] = dict(list((mapa.get("recetas") or {}).items())[:6])
                await self.runner.memory.put(rec.app_name, mapa)
                log.info("mapa de '%s' actualizado", rec.app_name)
        except Exception as e:
            log.warning("cartographer falló: %s", e)

    # ------------------------------------------------------------ utilidades
    def _remember_shot(self, shot: bytes, step: int):
        self.last_shots.append(shot)
        if len(self.last_shots) > 4:
            del self.last_shots[0]
        self._save_shot(shot, step)

    def _save_shot(self, shot: bytes, step: int):
        if config.SAVE_SCREENSHOTS:
            try:
                d = os.path.join(config.LOG_DIR, self.rec.task_id)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, f"{step:02d}.png"), "wb") as f:
                    f.write(shot)
            except Exception:
                pass

    def _prune_images(self):
        """Conserva solo las últimas KEEP_IMAGES capturas en el historial para controlar tokens y latencia."""
        keep = config.KEEP_IMAGES
        seen = 0
        for msg in reversed(self.messages):
            if msg["role"] != "user" or not isinstance(msg["content"], list):
                continue
            content = msg["content"]
            for i in range(len(content) - 1, -1, -1):
                part = content[i]
                if part.get("type") == "image":
                    seen += 1
                    if seen > keep:
                        content[i] = {"type": "text", "text": "[captura anterior omitida]"}
                elif part.get("type") == "tool_result" and isinstance(part.get("content"), list):
                    items = part["content"]
                    for j in range(len(items) - 1, -1, -1):
                        it = items[j]
                        if isinstance(it, dict) and it.get("type") == "image":
                            seen += 1
                            if seen > keep:
                                items[j] = {"type": "text", "text": "[captura anterior omitida]"}
