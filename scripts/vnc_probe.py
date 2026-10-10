"""
Prueba del cliente VNC propio contra un escritorio: conecta, captura, y opcionalmente hace clic y escribe.

Uso:
  python scripts/vnc_probe.py --url wss://<escritorio>/websockify --password <clave> --out captura.png
  python scripts/vnc_probe.py --url ... --password ... --click 640,400 --type "Hola áéñ" --out despues.png
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.rfb import RFBClient  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", default="captura.png")
    ap.add_argument("--click", help="x,y")
    ap.add_argument("--double", help="x,y (doble clic)")
    ap.add_argument("--type", dest="text", help="texto a escribir (selecciona todo antes)")
    ap.add_argument("--key", help="combinación, p.ej. ctrl+a, Return, Tab")
    ap.add_argument("--zoom", help="x0,y0,x1,y1 (guarda zoom.png)")
    ap.add_argument("--raw-only", action="store_true", help="usar solo encoding Raw")
    args = ap.parse_args()

    encs = [0, 1, -223] if args.raw_only else None
    rfb = RFBClient(args.url, args.password, encodings=encs)
    t0 = time.time()
    await rfb.connect()
    print(f"conectado en {time.time()-t0:.1f}s: {rfb.width}x{rfb.height} '{rfb.name}' frames={rfb.frames}")
    await rfb.wait_stable(min_wait=0.5)
    open(args.out, "wb").write(rfb.screenshot_png())
    print("captura inicial ->", args.out)

    if args.click:
        x, y = map(int, args.click.split(","))
        await rfb.click(x, y)
        await rfb.wait_stable()
        print("clic en", x, y)
    if args.double:
        x, y = map(int, args.double.split(","))
        await rfb.click(x, y, count=2)
        await rfb.wait_stable()
    if args.key:
        await rfb.key_combo(args.key)
        await rfb.wait_stable()
    if args.text:
        await rfb.select_all()
        await rfb.type_text(args.text)
        await rfb.wait_stable()
        print("escrito:", args.text)
    if args.zoom:
        x0, y0, x1, y1 = map(int, args.zoom.split(","))
        png, _ = rfb.zoom_png(x0, y0, x1, y1)
        open("zoom.png", "wb").write(png)
        print("zoom -> zoom.png")
    if args.click or args.double or args.key or args.text:
        base, ext = os.path.splitext(args.out)
        out2 = f"{base}_despues{ext or '.png'}"
        open(out2, "wb").write(rfb.screenshot_png())
        print("captura final ->", out2)
    print(f"frames recibidos: {rfb.frames}, error: {rfb.error}")
    await rfb.close()


if __name__ == "__main__":
    asyncio.run(main())
