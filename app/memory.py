"""Memoria de la app: mapa JSON por huella (nombre) de aplicación. El escritorio se resetea; el conocimiento, no.

v2 (MEMORY_V2): el mapa describe FUNCIONES, nunca posiciones. Cada entrada guarda la firma visual de la UI
en la que se validó, contadores de éxito/fallo y una confianza; si la apariencia actual difiere (tema nuevo,
modal, botones movidos) el mapa se presenta como desactualizado y el Operator se guía por la pantalla.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

import numpy as np

from . import config

log = logging.getLogger("aprendiz.memory")

# Coordenadas en texto libre: "(x≈104,y≈44)", "x=10, y=20", "(104, 44)", "en 104,44".
_COORD_RES = [
    re.compile(r"\s*\(?\s*\b[xy]\s*[≈=~:]\s*-?\d+(?:\s*[,;/]?\s*\b[xy]\s*[≈=~:]\s*-?\d+)?\s*\)?", re.IGNORECASE),
    re.compile(r"\s*\(\s*\d{1,4}\s*,\s*\d{1,4}\s*\)"),
    re.compile(r"\s*\b(?:en|at)\s+\d{1,4}\s*,\s*\d{1,4}\b", re.IGNORECASE),
]
DRIFT_THRESHOLD = 0.18  # distancia de firma visual a partir de la cual el mapa se considera de otra UI


def fingerprint(app_name: str) -> str:
    s = (app_name or "desconocida").lower()
    s = re.sub(r"[^a-z0-9áéíóúñ]+", "-", s).strip("-")
    return s[:60] or "desconocida"


def strip_coordinates(obj: Any) -> Any:
    """Quita coordenadas de cualquier texto del mapa: las posiciones cambian entre versiones de la UI."""
    if isinstance(obj, str):
        for rx in _COORD_RES:
            obj = rx.sub("", obj)
        return obj.strip()
    if isinstance(obj, list):
        return [strip_coordinates(x) for x in obj]
    if isinstance(obj, dict):
        return {k: strip_coordinates(v) for k, v in obj.items()}
    return obj


def ui_signature(rgb: np.ndarray) -> list[float]:
    """Firma barata de la apariencia: paleta (64 colores cuantizados) + luminancia en rejilla 16x10.

    Sensible a cambios de tema/maquetación; poco sensible a los datos (texto de filas) que cambian entre tareas.
    """
    h, w = rgb.shape[:2]
    small = rgb[:: max(1, h // 200), :: max(1, w // 320)].astype(np.int32)
    q = (small // 64)  # 4 niveles por canal
    idx = (q[..., 0] * 16 + q[..., 1] * 4 + q[..., 2]).ravel()
    hist = np.bincount(idx, minlength=64).astype(np.float64)
    hist /= max(1.0, hist.sum())
    gray = rgb.mean(axis=2)
    gh, gw = gray.shape
    grid = gray[: gh - gh % 10, : gw - gw % 16].reshape(10, gh // 10, 16, gw // 16).mean(axis=(1, 3)) / 255.0
    return [round(float(v), 4) for v in np.concatenate([hist, grid.ravel()])]


def ui_distance(a: Optional[list], b: Optional[list]) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None
    va, vb = np.asarray(a), np.asarray(b)
    palette = 0.5 * float(np.abs(va[:64] - vb[:64]).sum())  # 0..1
    layout = float(np.abs(va[64:] - vb[64:]).mean())        # 0..1
    return round(0.6 * palette + 0.4 * layout, 4)


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

    def context(self, app_name: str, sig: Optional[list]) -> tuple[dict | None, dict]:
        """Mapa saneado + metadatos de fiabilidad para el Operator."""
        m = self.maps.get(fingerprint(app_name))
        if not m or not m.get("mapa"):
            return None, {}
        ok, bad = int(m.get("exitos", 0)), int(m.get("fallos", 0))
        dist = ui_distance(m.get("ui_sig"), sig)
        meta = {
            "confianza": round((ok + 1) / (ok + bad + 2), 2),
            "exitos": ok, "fallos": bad,
            "distancia_ui": dist,
            "drift": dist is not None and dist >= DRIFT_THRESHOLD,
            "edad_h": round((time.time() - float(m.get("actualizado", time.time()))) / 3600, 1),
        }
        return strip_coordinates(m["mapa"]), meta

    async def put(self, app_name: str, mapa: dict, sig: Optional[list] = None):
        async with self._lock:
            key = fingerprint(app_name)
            prev = self.maps.get(key, {})
            entry = {"app": app_name, "actualizado": time.time(), "mapa": mapa}
            if config.MEMORY_V2:
                entry["mapa"] = strip_coordinates(mapa)
                for k in ("exitos", "fallos", "ui_sig"):
                    if k in prev:
                        entry[k] = prev[k]
                if sig and "ui_sig" not in entry:
                    entry["ui_sig"] = sig
            self.maps[key] = entry
            self._save()

    async def record_outcome(self, app_name: str, ok: Optional[bool], sig: Optional[list]):
        """ok=True: la tarea quedó verificada en esta UI (se fija su firma). ok=False: fallo de navegación.
        ok=None: resultado que no dice nada sobre la ruta (p.ej. una tarea que debía fallar)."""
        if ok is None:
            return
        async with self._lock:
            key = fingerprint(app_name)
            entry = self.maps.setdefault(key, {"app": app_name, "actualizado": time.time(), "mapa": {}})
            if ok:
                entry["exitos"] = int(entry.get("exitos", 0)) + 1
                if sig:
                    prev = entry.get("ui_sig")
                    if prev and (ui_distance(prev, sig) or 0) >= DRIFT_THRESHOLD:
                        # La UI cambió y aun así la tarea se verificó: los contadores viejos ya no aplican.
                        entry["exitos"], entry["fallos"] = 1, 0
                    entry["ui_sig"] = sig
            else:
                entry["fallos"] = int(entry.get("fallos", 0)) + 1
            self._save()

    def _save(self):
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
