"""Memoria de la app: mapa JSON por huella (nombre) de aplicación. El escritorio se resetea; el conocimiento, no."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from . import config

log = logging.getLogger("aprendiz.memory")


def fingerprint(app_name: str) -> str:
    s = (app_name or "desconocida").lower()
    s = re.sub(r"[^a-z0-9áéíóúñ]+", "-", s).strip("-")
    return s[:60] or "desconocida"


class AppMemory:
    def __init__(self, path: str = config.MEMORY_PATH):
        self.path = path
        self._lock = asyncio.Lock()
        self.maps: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    self.maps = json.load(f)
                log.info("memoria cargada: %d apps", len(self.maps))
        except Exception as e:
            log.warning("no se pudo cargar memoria: %s", e)
            self.maps = {}

    def get(self, app_name: str) -> dict | None:
        m = self.maps.get(fingerprint(app_name))
        return m.get("mapa") if m else None

    async def put(self, app_name: str, mapa: dict):
        async with self._lock:
            key = fingerprint(app_name)
            self.maps[key] = {"app": app_name, "actualizado": time.time(), "mapa": mapa}
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.maps, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except Exception as e:
                log.warning("no se pudo guardar memoria: %s", e)

    def all_names(self) -> list[str]:
        return [v.get("app", k) for k, v in self.maps.items()]
