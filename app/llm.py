"""Acceso al modelo (Claude) con reintentos cortos y utilidades para JSON e imágenes."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import anthropic

from . import config

log = logging.getLogger("aprendiz.llm")

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY or None, max_retries=0, timeout=90.0)
    return _client


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode("ascii")}}


def parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON del texto (tolera ```json ... ```)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    return json.loads(t)


async def create(model: str, system: str, messages: list, tools: list | None = None, max_tokens: int = 1200,
                 attempts: int = 3, **kw) -> Any:
    last = None
    for i in range(attempts):
        try:
            params = dict(model=model, max_tokens=max_tokens,
                          system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                          messages=messages, **kw)
            if tools:
                params["tools"] = tools
            return await client().messages.create(**params)
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                anthropic.APITimeoutError) as e:
            last = e
            wait = 1.5 * (i + 1)
            log.warning("API %s; reintento en %.1fs", type(e).__name__, wait)
            await asyncio.sleep(wait)
        except anthropic.BadRequestError as e:
            # p.ej. imagen demasiado grande o historial inválido: no reintentar a ciegas
            raise
    raise last  # type: ignore[misc]


async def call_json(model: str, system: str, content: list | str, max_tokens: int = 1500) -> dict:
    msgs = [{"role": "user", "content": content}]
    resp = await create(model, system, msgs, max_tokens=max_tokens)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        return parse_json(text)
    except Exception:
        log.warning("JSON inválido del modelo: %s", text[:300])
        # Segundo intento pidiendo solo JSON
        msgs.append({"role": "assistant", "content": text})
        msgs.append({"role": "user", "content": "Responde únicamente con el JSON válido, sin texto adicional."})
        resp = await create(model, system, msgs, max_tokens=max_tokens)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return parse_json(text)
