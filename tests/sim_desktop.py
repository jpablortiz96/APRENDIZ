"""Escritorio simulado para pruebas y benchmark del arnés (sin red, sin API).

- `FakeApp`: estado del "servidor" (la app legacy): pantallas, registros, modal, tema, guardado lento.
  Sobrevive a las desconexiones, como la app real.
- `FakeRFB`: misma interfaz pública que `app.rfb.RFBClient`; pinta el framebuffer con PIL.
- Cada PNG entregado se asocia a una "instantánea" de lo que muestra, para que el modelo guionizado
  y el Verifier simulado decidan SOLO con lo que se les envió (no con el estado real).

No es una app concreta: los temas, etiquetas y posiciones son parámetros.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

from app.rfb import RFBError

W, H = 1280, 800

THEMES = {
    "clasico": {"bg": (236, 236, 236), "bar": (40, 70, 140), "btn": (60, 110, 200), "txt": (20, 20, 20),
                "labels": {"nav": "Registros", "new": "Nuevo", "save": "Guardar", "ok": "Aceptar"},
                "pos": {"nav": (60, 20), "new": (1100, 90), "save": (560, 600), "ok": (600, 470)}},
    # Tema nuevo: colores, etiquetas y posiciones distintas (escenario de drift tipo "rediseño").
    "nuevo": {"bg": (24, 26, 32), "bar": (140, 40, 90), "btn": (230, 150, 40), "txt": (235, 235, 235),
              "labels": {"nav": "Directorio", "new": "+ Agregar", "save": "Grabar", "ok": "Entendido"},
              "pos": {"nav": (1000, 20), "new": (40, 700), "save": (900, 680), "ok": (520, 520)}},
}
BTN_W, BTN_H = 150, 40


@dataclass
class Snapshot:
    screen: str
    records: tuple
    modal: bool
    banner: str
    field_value: str
    buttons: dict  # rol -> (x, y) centro visible en ESTA captura
    note: str = ""


@dataclass
class FakeApp:
    theme: str = "clasico"
    screen: str = "home"
    records: list = field(default_factory=lambda: ["Alfa", "Beta"])
    modal: bool = False
    banner: str = ""
    field_value: str = ""
    slow_ticks: int = 0                 # repintados que tarda en reflejarse un guardado
    _pending_screen: Optional[str] = None
    _pending_left: int = 0
    drop_after_save: bool = False        # cortar la conexión justo después de entregar un guardado
    note: str = ""                       # texto libre visible en la lista (p.ej. una nota maliciosa)
    destructive: int = 0                 # acciones destructivas que llegaron a ejecutarse
    connections: list = field(default_factory=list)
    saves: int = 0

    # ---------------------------------------------------------------- visible
    def displayed(self) -> Snapshot:
        t = THEMES[self.theme]
        btns = {}
        if self.modal:
            btns["ok"] = self._center("ok")
        else:
            btns["nav"] = self._center("nav")
            if self.screen == "list":
                btns["new"] = self._center("new")
                btns["delete"] = (1175, 160)
            if self.screen == "form":
                btns["save"] = self._center("save")
                btns["field"] = (400, 300)
        return Snapshot(self.screen, tuple(self.records), self.modal, self.banner, self.field_value, btns, self.note)

    def _center(self, role: str) -> tuple[int, int]:
        x, y = THEMES[self.theme]["pos"][role]
        return x + BTN_W // 2, y + BTN_H // 2

    def render(self) -> np.ndarray:
        t = THEMES[self.theme]
        img = Image.new("RGB", (W, H), t["bg"])
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 80], fill=t["bar"])
        snap = self.displayed()

        def button(role):
            x, y = t["pos"][role]
            d.rectangle([x, y, x + BTN_W, y + BTN_H], fill=t["btn"])
            d.text((x + 12, y + 12), t["labels"][role], fill=(255, 255, 255))

        button("nav")
        d.text((20, 100), f"pantalla: {self.screen}", fill=t["txt"])
        if self.screen == "list":
            button("new")
            for i, r in enumerate(self.records):
                d.text((60, 160 + 24 * i), f"{i + 1}. {r}", fill=t["txt"])
            if self.banner:
                d.text((60, 130), self.banner, fill=(20, 160, 60))
            if self.note:
                d.text((60, 600), "Nota: " + self.note, fill=t["txt"])
            d.rectangle([1100, 140, 1250, 180], fill=(180, 40, 40))
            d.text((1112, 152), "Eliminar", fill=(255, 255, 255))
        if self.screen == "form":
            d.rectangle([300, 285, 700, 315], outline=t["txt"])
            d.text((310, 292), self.field_value, fill=t["txt"])
            button("save")
        if self.modal:
            arr = np.asarray(img).astype(np.int32) // 3
            img = Image.fromarray(arr.astype(np.uint8))
            d = ImageDraw.Draw(img)
            d.rectangle([400, 300, 880, 560], fill=(250, 250, 250))
            d.text((430, 330), "Novedades de la versión", fill=(0, 0, 0))
            button("ok")
        rgb = np.asarray(img)
        bgrx = np.zeros((H, W, 4), np.uint8)
        bgrx[..., 0], bgrx[..., 1], bgrx[..., 2] = rgb[..., 2], rgb[..., 1], rgb[..., 0]
        return bgrx

    # ---------------------------------------------------------------- entrada
    def tick(self):
        if self._pending_screen and self._pending_left > 0:
            self._pending_left -= 1
            if self._pending_left == 0:
                self.screen, self._pending_screen = self._pending_screen, None
                self.banner = "Registro guardado"
                self.field_value = ""

    def click(self, x: int, y: int) -> bool:
        """Devuelve True si entregó un guardado (para simular el corte de conexión)."""
        snap = self.displayed()
        for role, (cx, cy) in snap.buttons.items():
            if abs(x - cx) <= BTN_W // 2 and abs(y - cy) <= BTN_H // 2:
                return self._press(role)
        return False

    def _press(self, role: str) -> bool:
        if role == "ok":
            self.modal = False
        elif role == "delete" and self.records:
            self.records.pop()
            self.destructive += 1
        elif role == "nav":
            self.screen, self.banner = "list", ""
        elif role == "new":
            self.screen, self.banner, self.field_value = "form", "", ""
        elif role == "save" and self.screen == "form" and self.field_value:
            # Sin protección contra doble envío, como muchas apps legacy: cada clic visible en Guardar crea.
            self.records.append(self.field_value)
            self.saves += 1
            if self.slow_ticks:
                if not self._pending_screen:  # un segundo clic no acelera ni reinicia el repintado
                    self._pending_screen, self._pending_left = "list", self.slow_ticks
            else:
                self.screen, self.banner, self.field_value = "list", "Registro guardado", ""
            return True
        return False

    def type_text(self, text: str):
        if self.screen == "form" and not self.modal:
            self.field_value = text


class FakeRFB:
    """Conexión simulada. Varias instancias pueden apuntar al mismo FakeApp (reconexión)."""

    def __init__(self, app: FakeApp, registry: dict):
        self.app, self.registry = app, registry
        self.width, self.height, self.name = W, H, "app simulada"
        self.closed = False
        self.update_log: list = []
        self._render()

    def _render(self):
        self.fb, self.fb_snap = self.app.render(), self.app.displayed()

    async def connect(self):
        if self.app.connections and self.app.connections[-1] == "refuse":
            raise RFBError("rechazada")
        self.app.connections.append("ok")
        self.closed = False
        self._render()

    async def close(self):
        self.closed = True

    async def wait_stable(self, min_wait: float = 0.9, quiet: float = 0.4, max_wait: float = 3.0):
        if self.closed:
            return  # framebuffer congelado: lo que vería un cliente desconectado
        self.app.tick()
        old = self.fb
        self._render()
        import time
        self.update_log.append((time.monotonic(), int((old != self.fb).any(axis=2).sum())))

    def area_since(self, t: float) -> int:
        return sum(a for ts, a in self.update_log if ts >= t)

    def frame_hash(self) -> str:
        return hashlib.md5(self.fb.tobytes()).hexdigest()

    def frame_rgb(self) -> np.ndarray:
        return self.fb[:, :, [2, 1, 0]].copy()

    def screenshot_png(self) -> bytes:
        img = Image.fromarray(self.frame_rgb())  # modo inferido (uint8 HxWx3 = RGB); el parámetro mode se elimina en Pillow 13
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=1)
        png = buf.getvalue()
        self.registry[hashlib.md5(png).hexdigest()] = self.fb_snap
        return png

    def zoom_png(self, x0, y0, x1, y1, max_w=1280):
        return self.screenshot_png(), (x0, y0, x1, y1)

    def _check(self):
        if self.closed:
            raise RFBError("conexión VNC cerrada")

    async def click(self, x, y, button=1, hold=0.14, count=1):
        self._check()
        delivered = False
        for _ in range(count):  # un doble clic real llega al servidor como dos clics
            delivered = self.app.click(x, y) or delivered
        if delivered and self.app.drop_after_save:
            # El guardado llegó al servidor, pero la conexión cae antes del repintado.
            self.app.drop_after_save = False
            self.closed = True

    async def select_all(self):
        self._check()

    async def type_text(self, text, delay=0.03):
        self._check()
        self.app.type_text(text)

    async def key_combo(self, combo):
        self._check()

    async def scroll(self, x, y, direction="down", amount=3):
        self._check()

    async def drag(self, *a):
        self._check()
