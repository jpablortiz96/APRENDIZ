"""
Cliente RFB (VNC) propio sobre WebSocket (websockify), sin librerías de automatización.

- Handshake RFB 3.3/3.7/3.8, autenticación VNC (DES), ClientInit/ServerInit.
- Encodings: Raw, CopyRect, ZRLE (+ DesktopSize pseudo-encoding).
- Framebuffer en memoria (numpy, BGRX) → capturas PNG y zoom.
- Eventos de puntero y teclado con los tiempos que el escritorio del reto necesita.
- Espera de "pantalla estable" después de cada acción.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import ssl
import struct
import time
import zlib
from typing import Optional

import numpy as np
import websockets
from Crypto.Cipher import DES
from PIL import Image

from . import keysyms as KS

log = logging.getLogger("aprendiz.rfb")

ENC_RAW = 0
ENC_COPYRECT = 1
ENC_ZRLE = 16
ENC_DESKTOP_SIZE = -223

BTN_LEFT = 1
BTN_MIDDLE = 2
BTN_RIGHT = 4
BTN_SCROLL_UP = 8
BTN_SCROLL_DOWN = 16

# Por debajo de esta área (px²) un repintado se considera "ruido" (p.ej. parpadeo del cursor de texto)
SIGNIFICANT_AREA = 500


class RFBError(Exception):
    pass


def _vnc_encrypt(password: str, challenge: bytes) -> bytes:
    """Cifra el reto de 16 bytes con DES usando la contraseña (bits invertidos por byte), como en VNC."""
    key = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    # VNC invierte el orden de los bits de cada byte de la clave
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
    cipher = DES.new(key, DES.MODE_ECB)
    return cipher.encrypt(challenge)


class RFBClient:
    def __init__(self, url: str, password: str, encodings: Optional[list[int]] = None,
                 connect_timeout: float = 20.0):
        self.url = url
        self.password = password
        self.encodings = encodings or [ENC_ZRLE, ENC_COPYRECT, ENC_RAW, ENC_DESKTOP_SIZE]
        self.connect_timeout = connect_timeout

        self.ws = None
        self.reader: Optional[asyncio.StreamReader] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._msg_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()

        self.width = 0
        self.height = 0
        self.name = ""
        self.fb: Optional[np.ndarray] = None  # (H, W, 4) uint8, orden B,G,R,X
        self._zlib = zlib.decompressobj()

        self.frames = 0  # número de FramebufferUpdate recibidos
        self.update_log: list[tuple[float, int]] = []  # (monotonic, área repintada)
        self.last_update = 0.0  # monotonic de la última actualización (cualquiera)
        self.last_significant_update = 0.0  # ... de la última actualización "grande"
        self._first_frame = asyncio.Event()
        self.closed = False
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ conexión
    async def connect(self):
        ssl_ctx = None
        if self.url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
        log.info("Conectando a %s", self._safe_url())
        self.ws = await asyncio.wait_for(
            websockets.connect(
                self.url,
                subprotocols=["binary"],
                ssl=ssl_ctx,
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
                open_timeout=self.connect_timeout,
                compression=None,
            ),
            timeout=self.connect_timeout + 5,
        )
        self.reader = asyncio.StreamReader(limit=1 << 26)
        self._pump_task = asyncio.create_task(self._pump())
        await asyncio.wait_for(self._handshake(), timeout=self.connect_timeout)
        await self._set_pixel_format()
        await self._set_encodings()
        self._msg_task = asyncio.create_task(self._message_loop())
        await self.request_update(incremental=False)
        await asyncio.wait_for(self._first_frame.wait(), timeout=self.connect_timeout)
        log.info("Conectado: %dx%d '%s'", self.width, self.height, self.name)

    def _safe_url(self) -> str:
        try:
            host = self.url.split("://", 1)[1].split("/", 1)[0]
            return f"{self.url.split('://',1)[0]}://{host}/…"
        except Exception:
            return "<url>"

    async def _pump(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.reader.feed_data(bytes(msg))
                else:
                    self.reader.feed_data(msg.encode("latin-1", "replace"))
        except Exception as e:  # cierre o error del websocket
            self.error = f"websocket: {type(e).__name__}"
            log.warning("pump terminado: %s", self.error)
        finally:
            self.closed = True
            try:
                self.reader.feed_eof()
            except Exception:
                pass

    async def _read(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError:
            raise RFBError("conexión VNC cerrada")

    async def _send(self, data: bytes):
        if self.closed or self.ws is None:
            raise RFBError("conexión VNC cerrada")
        async with self._send_lock:
            await self.ws.send(data)

    async def _handshake(self):
        ver = await self._read(12)
        if not ver.startswith(b"RFB "):
            raise RFBError(f"no es un servidor RFB: {ver!r}")
        major, minor = int(ver[4:7]), int(ver[8:11])
        log.info("Servidor RFB %d.%d", major, minor)
        if major != 3:
            raise RFBError(f"versión RFB no soportada: {ver!r}")
        if minor >= 8:
            minor = 8
        elif minor >= 7:
            minor = 7
        else:
            minor = 3
        await self._send(f"RFB 003.00{minor}\n".encode())

        if minor == 3:
            (sectype,) = struct.unpack(">I", await self._read(4))
            if sectype == 0:
                raise RFBError("el servidor rechazó la conexión: " + await self._read_reason())
        else:
            (n,) = struct.unpack(">B", await self._read(1))
            if n == 0:
                raise RFBError("el servidor rechazó la conexión: " + await self._read_reason())
            types = list(await self._read(n))
            if 2 in types and self.password:
                sectype = 2
            elif 1 in types:
                sectype = 1
            elif 2 in types:
                sectype = 2
            else:
                raise RFBError(f"tipos de seguridad no soportados: {types}")
            await self._send(bytes([sectype]))

        if sectype == 2:
            challenge = await self._read(16)
            await self._send(_vnc_encrypt(self.password or "", challenge))
        # SecurityResult: en 3.8 siempre; en 3.7 y 3.3 solo con VNC auth
        if minor == 8 or sectype == 2:
            (result,) = struct.unpack(">I", await self._read(4))
            if result != 0:
                reason = ""
                if minor == 8:
                    try:
                        reason = await self._read_reason()
                    except Exception:
                        pass
                raise RFBError("autenticación VNC fallida " + reason)

        await self._send(b"\x01")  # ClientInit: shared
        head = await self._read(24)
        self.width, self.height = struct.unpack(">HH", head[:4])
        (name_len,) = struct.unpack(">I", head[20:24])
        self.name = (await self._read(name_len)).decode("utf-8", "replace")
        self.fb = np.zeros((self.height, self.width, 4), dtype=np.uint8)

    async def _read_reason(self) -> str:
        (n,) = struct.unpack(">I", await self._read(4))
        return (await self._read(n)).decode("utf-8", "replace")

    async def _set_pixel_format(self):
        # 32 bpp, profundidad 24, little-endian, true colour, R<<16 G<<8 B
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await self._send(struct.pack(">Bxxx", 0) + pf)

    async def _set_encodings(self):
        encs = self.encodings
        msg = struct.pack(">BxH", 2, len(encs)) + b"".join(struct.pack(">i", e) for e in encs)
        await self._send(msg)

    async def request_update(self, incremental: bool = True):
        await self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, self.width, self.height))

    # ------------------------------------------------------------ mensajes servidor
    async def _message_loop(self):
        try:
            while not self.closed:
                (mtype,) = struct.unpack(">B", await self._read(1))
                if mtype == 0:
                    await self._framebuffer_update()
                elif mtype == 1:  # SetColourMapEntries
                    head = await self._read(5)
                    (_, n) = struct.unpack(">xHH", head)
                    await self._read(n * 6)
                elif mtype == 2:  # Bell
                    pass
                elif mtype == 3:  # ServerCutText
                    head = await self._read(7)
                    (n,) = struct.unpack(">xxxI", head)
                    if n & 0x80000000:  # extended clipboard (negativo): ignorar
                        n = (-n) & 0xFFFFFFFF
                    await self._read(n)
                else:
                    raise RFBError(f"mensaje de servidor desconocido: {mtype}")
        except RFBError as e:
            if not self.closed:
                self.error = str(e)
                log.warning("message loop: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("message loop")
        finally:
            self.closed = True
            self._first_frame.set()

    async def _framebuffer_update(self):
        head = await self._read(3)
        (nrects,) = struct.unpack(">xH", head)
        area = 0
        for _ in range(nrects):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self._read(12))
            if enc == ENC_RAW:
                data = await self._read(w * h * 4)
                if w and h:
                    arr = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
                    self.fb[y:y + h, x:x + w] = arr
                area += w * h
            elif enc == ENC_COPYRECT:
                sx, sy = struct.unpack(">HH", await self._read(4))
                if w and h:
                    self.fb[y:y + h, x:x + w] = self.fb[sy:sy + h, sx:sx + w].copy()
                area += w * h
            elif enc == ENC_ZRLE:
                (n,) = struct.unpack(">I", await self._read(4))
                data = await self._read(n)
                if w and h:
                    self._decode_zrle(x, y, w, h, self._zlib.decompress(data))
                area += w * h
            elif enc == ENC_DESKTOP_SIZE:
                self.width, self.height = w, h
                newfb = np.zeros((h, w, 4), dtype=np.uint8)
                if self.fb is not None:
                    hh, ww = min(h, self.fb.shape[0]), min(w, self.fb.shape[1])
                    newfb[:hh, :ww] = self.fb[:hh, :ww]
                self.fb = newfb
                area += w * h
            else:
                raise RFBError(f"encoding no soportado recibido: {enc}")
        now = time.monotonic()
        self.frames += 1
        self.last_update = now
        if area >= SIGNIFICANT_AREA:
            self.last_significant_update = now
        self.update_log.append((now, area))
        if len(self.update_log) > 400:
            del self.update_log[:200]
        self._first_frame.set()
        # Mantener siempre una petición incremental pendiente (como un visor normal)
        try:
            await self.request_update(incremental=True)
        except RFBError:
            pass

    # ------------------------------------------------------------------- ZRLE
    def _decode_zrle(self, rx: int, ry: int, rw: int, rh: int, data: bytes):
        pos = 0
        mv = memoryview(data)
        for ty in range(ry, ry + rh, 64):
            th = min(64, ry + rh - ty)
            for tx in range(rx, rx + rw, 64):
                tw = min(64, rx + rw - tx)
                sub = mv[pos]
                pos += 1
                npx = tw * th
                if sub == 0:  # raw
                    px = np.frombuffer(mv[pos:pos + npx * 3], dtype=np.uint8).reshape(th, tw, 3)
                    pos += npx * 3
                    self.fb[ty:ty + th, tx:tx + tw, :3] = px
                elif sub == 1:  # color sólido
                    self.fb[ty:ty + th, tx:tx + tw, :3] = np.frombuffer(mv[pos:pos + 3], dtype=np.uint8)
                    pos += 3
                elif 2 <= sub <= 16:  # paleta empaquetada
                    psize = sub
                    pal = np.frombuffer(mv[pos:pos + psize * 3], dtype=np.uint8).reshape(psize, 3)
                    pos += psize * 3
                    bits = 1 if psize == 2 else (2 if psize <= 4 else 4)
                    row_bytes = (tw * bits + 7) // 8
                    raw = np.frombuffer(mv[pos:pos + row_bytes * th], dtype=np.uint8).reshape(th, row_bytes)
                    pos += row_bytes * th
                    ub = np.unpackbits(raw, axis=1)  # (th, row_bytes*8)
                    if bits == 1:
                        idx = ub[:, :tw]
                    else:
                        ub = ub[:, : (tw * bits)].reshape(th, tw, bits)
                        weights = (1 << np.arange(bits - 1, -1, -1)).astype(np.uint8)
                        idx = (ub * weights).sum(axis=2)
                    idx = np.minimum(idx, psize - 1)
                    self.fb[ty:ty + th, tx:tx + tw, :3] = pal[idx]
                elif sub == 128:  # RLE simple
                    out = np.empty((npx, 3), dtype=np.uint8)
                    i = 0
                    while i < npx:
                        color = mv[pos:pos + 3]
                        pos += 3
                        run = 1
                        while True:
                            b = mv[pos]
                            pos += 1
                            run += b
                            if b != 255:
                                break
                        end = min(i + run, npx)
                        out[i:end] = np.frombuffer(color, dtype=np.uint8)
                        i = end
                    self.fb[ty:ty + th, tx:tx + tw, :3] = out.reshape(th, tw, 3)
                elif sub >= 130:  # RLE con paleta
                    psize = sub - 128
                    pal = np.frombuffer(mv[pos:pos + psize * 3], dtype=np.uint8).reshape(psize, 3)
                    pos += psize * 3
                    idxs = np.empty(npx, dtype=np.uint8)
                    i = 0
                    while i < npx:
                        b = mv[pos]
                        pos += 1
                        if b & 128:
                            index = b & 127
                            run = 1
                            while True:
                                c = mv[pos]
                                pos += 1
                                run += c
                                if c != 255:
                                    break
                        else:
                            index = b
                            run = 1
                        end = min(i + run, npx)
                        idxs[i:end] = min(index, psize - 1)
                        i = end
                    self.fb[ty:ty + th, tx:tx + tw, :3] = pal[idxs].reshape(th, tw, 3)
                else:
                    raise RFBError(f"subencoding ZRLE inválido: {sub}")

    # --------------------------------------------------------------- capturas
    def frame_rgb(self) -> np.ndarray:
        return self.fb[:, :, [2, 1, 0]].copy()

    def frame_hash(self) -> str:
        return hashlib.md5(self.fb.tobytes()).hexdigest()

    def screenshot_png(self) -> bytes:
        img = Image.fromarray(self.frame_rgb(), "RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False, compress_level=6)
        return buf.getvalue()

    def zoom_png(self, x0: int, y0: int, x1: int, y1: int, max_w: int = 1280) -> tuple[bytes, tuple[int, int, int, int]]:
        x0, x1 = sorted((max(0, min(self.width, x0)), max(0, min(self.width, x1))))
        y0, y1 = sorted((max(0, min(self.height, y0)), max(0, min(self.height, y1))))
        if x1 - x0 < 20:
            x1 = min(self.width, x0 + 20)
        if y1 - y0 < 20:
            y1 = min(self.height, y0 + 20)
        img = Image.fromarray(self.frame_rgb()[y0:y1, x0:x1], "RGB")
        scale = min(2.0, max_w / max(1, (x1 - x0)))
        if scale > 1.0:
            img = img.resize((int((x1 - x0) * scale), int((y1 - y0) * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), (x0, y0, x1, y1)

    async def wait_stable(self, min_wait: float = 0.9, quiet: float = 0.4, max_wait: float = 3.0):
        """Espera a que la pantalla deje de repintarse (ignorando parpadeos pequeños)."""
        t0 = time.monotonic()
        await asyncio.sleep(min_wait)
        while time.monotonic() - t0 < max_wait:
            if time.monotonic() - self.last_significant_update >= quiet:
                return
            await asyncio.sleep(0.1)

    def area_since(self, t: float) -> int:
        """Área total (px²) repintada desde el instante t (monotonic)."""
        return sum(a for ts, a in self.update_log if ts >= t)

    async def refresh(self):
        """Pide un repintado completo (por si hubo desincronización)."""
        await self.request_update(incremental=False)
        await asyncio.sleep(0.8)

    # ---------------------------------------------------------------- entrada
    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        return max(0, min(self.width - 1, int(x))), max(0, min(self.height - 1, int(y)))

    async def pointer(self, x: int, y: int, mask: int = 0):
        x, y = self._clamp(x, y)
        await self._send(struct.pack(">BBHH", 5, mask, x, y))

    async def move(self, x: int, y: int):
        await self.pointer(x, y, 0)

    async def click(self, x: int, y: int, button: int = BTN_LEFT, hold: float = 0.14, count: int = 1):
        """Clic con ≥100 ms entre bajar y soltar (el escritorio ignora clics más cortos)."""
        await self.move(x, y)
        await asyncio.sleep(0.08)
        for i in range(count):
            await self.pointer(x, y, button)
            await asyncio.sleep(hold)
            await self.pointer(x, y, 0)
            if i < count - 1:
                await asyncio.sleep(0.09)

    async def drag(self, x0: int, y0: int, x1: int, y1: int, steps: int = 12):
        await self.move(x0, y0)
        await asyncio.sleep(0.08)
        await self.pointer(x0, y0, BTN_LEFT)
        await asyncio.sleep(0.15)
        for i in range(1, steps + 1):
            xi = x0 + (x1 - x0) * i / steps
            yi = y0 + (y1 - y0) * i / steps
            await self.pointer(int(xi), int(yi), BTN_LEFT)
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.12)
        await self.pointer(x1, y1, 0)

    async def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3):
        await self.move(x, y)
        await asyncio.sleep(0.05)
        btn = BTN_SCROLL_DOWN if direction == "down" else BTN_SCROLL_UP
        for _ in range(max(1, min(amount, 15))):
            await self.pointer(x, y, btn)
            await asyncio.sleep(0.05)
            await self.pointer(x, y, 0)
            await asyncio.sleep(0.06)

    async def key(self, keysym: int, down: bool):
        await self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym))

    async def press(self, keysym: int, hold: float = 0.06):
        await self.key(keysym, True)
        await asyncio.sleep(hold)
        await self.key(keysym, False)

    async def key_combo(self, combo: str):
        """Pulsa una combinación como 'ctrl+a', 'Return', 'Tab', 'shift+Tab'."""
        syms = KS.parse_combo(combo)
        mods, main = syms[:-1], syms[-1]
        for m in mods:
            await self.key(m, True)
            await asyncio.sleep(0.03)
        await self.press(main)
        for m in reversed(mods):
            await asyncio.sleep(0.02)
            await self.key(m, False)
        await asyncio.sleep(0.05)

    async def type_text(self, text: str, delay: float = 0.03):
        """Escribe texto carácter a carácter (tildes y ñ como keysyms Latin-1)."""
        for ch in text:
            if ch == "\n":
                await self.press(KS.KEYSYMS["return"])
            elif ch == "\t":
                await self.press(KS.KEYSYMS["tab"])
            else:
                sym = KS.char_to_keysym(ch)
                need_shift = ch.isupper() or ch in KS.SHIFTED_ASCII
                if need_shift:
                    await self.key(KS.KEYSYMS["shift"], True)
                    await asyncio.sleep(0.02)
                await self.press(sym)
                if need_shift:
                    await asyncio.sleep(0.02)
                    await self.key(KS.KEYSYMS["shift"], False)
            await asyncio.sleep(delay)

    async def select_all(self):
        await self.key_combo("ctrl+a")
        await asyncio.sleep(0.08)

    async def cut_text(self, text: str):
        """Envía texto al portapapeles del servidor (ClientCutText, Latin-1)."""
        data = text.encode("latin-1", "replace")
        await self._send(struct.pack(">BxxxI", 6, len(data)) + data)

    # ----------------------------------------------------------------- cierre
    async def close(self):
        self.closed = True
        for t in (self._msg_task, self._pump_task):
            if t and not t.done():
                t.cancel()
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.ws.close(), timeout=3)
            except Exception:
                pass
        self.ws = None
