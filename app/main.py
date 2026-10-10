"""
Entrypoint de APRENDIZ (contrato del reto):
  GET  /health            -> 200 cuando el servicio está listo
  POST /tasks             -> {"task_id": "..."} en < 2 s; la tarea corre en segundo plano
  GET  /tasks/{task_id}   -> {"estado": "en_curso|terminada|fallida", "resumen": "..."}
Todas las llamadas llevan Authorization: Bearer <token del equipo>.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .agent import TaskRunner
from .memory import AppMemory

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aprendiz.main")

app = FastAPI(title="APRENDIZ · el agente que opera lo que no tiene API", version="1.0")
memory = AppMemory()
runner = TaskRunner(memory)
STARTED = time.time()


class Escritorio(BaseModel):
    url: str
    password: str = ""


class TaskIn(BaseModel):
    tarea: str = Field(..., min_length=1)
    escritorio: Escritorio


def require_token(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not config.TEAM_TOKEN:
        log.error("TEAM_TOKEN sin configurar: peticiones rechazadas")
        raise HTTPException(status_code=503, detail="servicio sin configurar")
    if not token or not hmac.compare_digest(token, config.TEAM_TOKEN):
        raise HTTPException(status_code=401, detail="token inválido")


@app.get("/health")
async def health():
    ready = bool(config.ANTHROPIC_API_KEY and config.TEAM_TOKEN)
    if not ready:
        return JSONResponse(status_code=503, content={"ok": False, "listo": False})
    return {"ok": True, "servicio": "aprendiz", "uptime_s": round(time.time() - STARTED),
            "listo": True}


@app.get("/")
async def root():
    return {"servicio": "APRENDIZ", "contrato": ["GET /health", "POST /tasks", "GET /tasks/{task_id}"]}


@app.post("/tasks", dependencies=[Depends(require_token)])
async def create_task(body: TaskIn) -> dict[str, Any]:
    url = body.escritorio.url.strip()
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]
    if not url.startswith(("ws://", "wss://")):
        raise HTTPException(status_code=400, detail="escritorio.url debe ser ws:// o wss://")
    if not url.rstrip("/").endswith("/websockify") and "/websockify" not in url:
        url = url.rstrip("/") + "/websockify"
    rec = runner.submit(body.tarea.strip(), url, body.escritorio.password)
    log.info("tarea %s recibida: %s", rec.task_id, rec.tarea[:120])
    return {"task_id": rec.task_id}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_token)])
async def get_task(task_id: str, debug: int = 0):
    rec = runner.tasks.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="task_id desconocido")
    if debug and config.DEBUG_API:
        return rec.debug()
    return rec.public()


@app.get("/tasks", dependencies=[Depends(require_token)])
async def list_tasks():
    return [{"task_id": r.task_id, "estado": r.estado, "tarea": r.tarea[:80]} for r in runner.tasks.values()]


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("error no controlado en %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "error interno"})
