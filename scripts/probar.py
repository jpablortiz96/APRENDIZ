"""
Ciclo de prueba completo contra tu escritorio del reto:
  POST /__reset  ->  POST /tasks (tu agente)  ->  GET /tasks/{id} cada 5 s  ->  POST /__check con el JSON de la prueba

Uso (PowerShell):
  $env:DESKTOP_URL="https://frontier-r1-xxxx.up.railway.app"
  $env:DESKTOP_PASSWORD="xxxxxxxx"
  $env:DESKTOP_TOKEN="<token del escritorio>"
  $env:AGENT_URL="http://localhost:8000"      # o la URL pública en Railway
  $env:TEAM_TOKEN="<token del equipo>"
  python scripts\probar.py material\pruebas\E1-crear-cliente.json
  python scripts\probar.py material\pruebas\*.json      # varias seguidas

Opciones: --no-reset (no resetea antes), --no-check (no llama a /__check), --timeout 300
"""
import argparse
import glob
import json
import os
import sys
import time

import httpx


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    if not v:
        print(f"Falta la variable de entorno {name}")
        sys.exit(2)
    return v


def run_one(path: str, args) -> dict:
    prueba = json.load(open(path, encoding="utf-8"))
    desktop = env("DESKTOP_URL").rstrip("/")
    password = env("DESKTOP_PASSWORD")
    dtoken = env("DESKTOP_TOKEN")
    agent = env("AGENT_URL").rstrip("/")
    team = env("TEAM_TOKEN")
    ws = desktop.replace("https://", "wss://").replace("http://", "ws://") + "/websockify"

    print(f"\n=== {prueba.get('id', path)} · {prueba.get('nivel', '')} ===\n{prueba['instruccion']}")
    with httpx.Client(timeout=60) as c:
        if not args.no_reset:
            r = c.post(f"{desktop}/__reset", headers={"Authorization": f"Bearer {dtoken}"})
            print(f"reset: HTTP {r.status_code}")
            r.raise_for_status()
            time.sleep(3)
        t0 = time.time()
        r = c.post(f"{agent}/tasks", headers={"Authorization": f"Bearer {team}"},
                   json={"tarea": prueba["instruccion"], "escritorio": {"url": ws, "password": password}})
        dt = time.time() - t0
        print(f"POST /tasks: {r.status_code} en {dt:.2f}s -> {r.text[:200]}")
        r.raise_for_status()
        task_id = r.json()["task_id"]
        estado, resumen = "en_curso", ""
        while time.time() - t0 < args.timeout:
            time.sleep(5)
            g = c.get(f"{agent}/tasks/{task_id}", headers={"Authorization": f"Bearer {team}"})
            data = g.json()
            estado, resumen = data.get("estado"), data.get("resumen", "")
            print(f"  [{time.time()-t0:5.0f}s] {estado}")
            if estado in ("terminada", "fallida"):
                break
        print(f"\nESTADO: {estado} (esperado: {prueba.get('estado_esperado')})\nRESUMEN:\n{resumen}\n")
        ok_estado = estado == prueba.get("estado_esperado")
        result = {"id": prueba.get("id"), "estado": estado, "estado_ok": ok_estado, "resumen": resumen}
        if "respuesta_esperada" in prueba:
            import re
            line = next((ln.strip() for ln in resumen.splitlines() if ln.lower().startswith("respuesta:")), "")
            answer = line.partition(":")[2].strip()
            expected = str(prueba["respuesta_esperada"]).strip()
            if re.fullmatch(r"-?\d+(?:[.,]\d+)*", expected):
                first = re.search(r"-?\d+(?:[.,]\d+)*", answer)
                result["respuesta_ok"] = first is not None and first.group() == expected
            else:
                result["respuesta_ok"] = answer == expected
            print(f"respuesta esperada: {expected!r} · línea: {line!r} · {'OK' if result['respuesta_ok'] else 'MAL'}")
        if prueba.get("claves_razon"):
            faltan = [k for k in prueba["claves_razon"] if k not in resumen]
            result["claves_ok"] = not faltan
            print(f"claves de razón: {'OK' if not faltan else 'faltan ' + str(faltan)}")
        if not args.no_check:
            r = c.post(f"{desktop}/__check", headers={"Authorization": f"Bearer {dtoken}"}, json=prueba)
            print(f"__check: {r.status_code} {r.text}")
            try:
                result["check"] = r.json() if r.status_code == 200 else {"pass": False, "error": f"HTTP {r.status_code}"}
            except Exception:
                result["check"] = {"pass": False, "error": f"respuesta no JSON: HTTP {r.status_code}"}
        if args.debug:
            g = c.get(f"{agent}/tasks/{task_id}?debug=1", headers={"Authorization": f"Bearer {team}"})
            print(json.dumps(g.json(), ensure_ascii=False, indent=1)[:4000])
        return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pruebas", nargs="+")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    ap.add_argument("--debug", action="store_true", help="imprime el detalle de la tarea al final")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    paths = []
    for p in args.pruebas:
        paths += sorted(glob.glob(p)) or [p]
    results = [run_one(p, args) for p in paths]
    print("\n=== RESUMEN ===")
    passed = 0
    for r in results:
        chk = r.get("check", {})
        check_ok = isinstance(chk, dict) and chk.get("pass") is True if not args.no_check else True
        ok = r["estado_ok"] and r.get("respuesta_ok", True) and r.get("claves_ok", True) and check_ok
        passed += bool(ok)
        print(f"{r['id']}: {'PASS' if ok else 'FAIL'} estado={r['estado']} check={chk.get('pass') if isinstance(chk, dict) else '-'}")
    print(f"RESULTADO: {passed}/{len(results)}")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
