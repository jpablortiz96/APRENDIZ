"""Acceso al modelo (Claude) con reintentos cortos y utilidades para JSON e imágenes."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import contextvars
import re
import time
from typing import Any

import anthropic

from . import config

log = logging.getLogger("aprendiz.llm")

_client: anthropic.AsyncAnthropic | None = None

# Métricas por tarea: el agente fija un dict nuevo al empezar; las subtareas asyncio heredan la referencia.
METRICS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("aprendiz_llm_metrics", default=None)
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def new_metrics() -> dict:
    return {"llamadas": {}, "ms": {}, "reintentos_api": 0, "reintentos_json": 0,
            "tokens": {f: 0 for f in _USAGE_FIELDS}}


def _account(role: str, resp: Any, ms: float) -> None:
    m = METRICS.get()
    if m is None:
        return
    m["llamadas"][role] = m["llamadas"].get(role, 0) + 1
    m["ms"][role] = round(m["ms"].get(role, 0) + ms)
    usage = getattr(resp, "usage", None)
    for f in _USAGE_FIELDS:
        v = getattr(usage, f, None)
        if isinstance(v, int):
            m["tokens"][f] += v


def _bump(counter: str) -> None:
    m = METRICS.get()
    if m is not None:
        m[counter] += 1


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY or None, max_retries=0, timeout=90.0)
    return _client


def image_block(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(png).decode("ascii")}}


class LLMJSONError(ValueError):
    """El modelo no devolvió un JSON utilizable. `reason`: max_tokens | refusal | empty | invalid_json | schema."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def parse_json(text: str) -> dict:
    """Extrae el primer objeto JSON del texto (tolera ```json ... ``` y prosa con llaves alrededor)."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if m:
        t = m.group(1)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMJSONError("invalid_json", f"sin objeto JSON completo ({len(t)} caracteres)")


_TYPES = {"object": dict, "string": str, "boolean": bool, "null": type(None), "array": list}


def validate_schema(obj: Any, schema: dict, path: str = "$") -> None:
    """Validación mínima (subconjunto usado por nuestros esquemas): type, anyOf, required, additionalProperties, enum."""
    if "anyOf" in schema:
        for sub in schema["anyOf"]:
            try:
                validate_schema(obj, sub, path)
                break
            except LLMJSONError:
                continue
        else:
            raise LLMJSONError("schema", f"{path} no cumple ninguna alternativa")
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(isinstance(obj, _TYPES[t]) and not (t != "boolean" and isinstance(obj, bool)) for t in types):
            raise LLMJSONError("schema", f"{path} debe ser {'|'.join(types)}")
    if "enum" in schema and obj not in schema["enum"]:
        raise LLMJSONError("schema", f"{path} fuera de enum")
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                raise LLMJSONError("schema", f"falta {path}.{k}")
        if schema.get("additionalProperties") is False:
            extra = set(obj) - set(props)
            if extra:
                raise LLMJSONError("schema", f"{path} con campos no permitidos: {sorted(extra)}")
        for k, sub in props.items():
            if k in obj:
                validate_schema(obj[k], sub, f"{path}.{k}")


async def create(model: str, system: str, messages: list, tools: list | None = None, max_tokens: int = 1200,
                 attempts: int = 3, cache_messages: bool = False, role: str = "operator", **kw) -> Any:
    last = None
    for i in range(attempts):
        if i:
            _bump("reintentos_api")
        try:
            params = dict(model=model, max_tokens=max_tokens,
                          system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                          messages=messages, **kw)
            if tools:
                params["tools"] = tools
            if cache_messages:
                # Caché automática del último bloque: cada paso del Operator reutiliza el historial previo
                # hasta la primera captura podada, en lugar de pagarlo entero otra vez.
                params["cache_control"] = {"type": "ephemeral"}
            t0 = time.monotonic()
            resp = await client().messages.create(**params)
            _account(role, resp, (time.monotonic() - t0) * 1000)
            return resp
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


def _extract_json(resp: Any, schema: dict | None) -> dict:
    """Convierte la respuesta en dict validado o lanza LLMJSONError con el motivo concreto."""
    stop = getattr(resp, "stop_reason", None)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if stop == "refusal":
        raise LLMJSONError("refusal")
    if stop == "max_tokens":
        # El thinking adaptativo consume max_tokens: la salida puede venir vacía o cortada.
        raise LLMJSONError("max_tokens", f"{len(text)} caracteres de texto")
    if not text.strip():
        raise LLMJSONError("empty", f"stop_reason={stop}")
    obj = parse_json(text)
    if schema:
        validate_schema(obj, schema)
    return obj


def _diag(role: str, resp: Any, outcome: str) -> None:
    """Diagnóstico sin contenido: nunca registra el texto (puede llevar datos de la app)."""
    usage = getattr(resp, "usage", None)
    log.info("llm[%s] stop=%s out_tokens=%s req=%s -> %s", role, getattr(resp, "stop_reason", None),
             getattr(usage, "output_tokens", None), getattr(resp, "_request_id", None), outcome)


async def call_json(model: str, system: str, content: list | str, max_tokens: int = 4096,
                    schema: dict | None = None, retry_max_tokens: int | None = None,
                    deadline: float | None = None, role: str = "json", min_retry_s: float = 15.0) -> dict:
    """Pide un objeto JSON. Con `schema` usa structured outputs (output_config.format) y valida en cliente.

    Un único reintento acotado, siempre como petición nueva (sin reenviar texto truncado):
    - stop_reason=max_tokens -> mismo pedido con `retry_max_tokens` (por defecto el doble).
    - JSON inválido / esquema / vacío -> mismo pedido con recordatorio de formato.
    El reintento solo se lanza si queda `min_retry_s` antes de `deadline` (time.monotonic()).
    """
    base = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    kw: dict = {}
    if schema:
        kw["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    budgets = [max_tokens, retry_max_tokens or max_tokens * 2]
    last: Exception | None = None
    for attempt, budget in enumerate(budgets):
        if attempt:
            if deadline is not None and deadline - time.monotonic() < min_retry_s:
                log.warning("llm[%s] sin tiempo para reintentar (%s)", role, last)
                break
        user = base if attempt == 0 or (isinstance(last, LLMJSONError) and last.reason == "max_tokens") else base + [
            {"type": "text", "text": "Tu respuesta anterior no fue un JSON válido con el esquema pedido. "
                                     "Responde únicamente con el objeto JSON, sin texto adicional."}]
        try:
            if attempt:
                _bump("reintentos_json")
            resp = await create(model, system, [{"role": "user", "content": user}], max_tokens=budget, role=role, **kw)
        except anthropic.BadRequestError as e:
            if not kw:
                raise
            # Modelo configurado sin soporte de structured outputs: seguir con JSON por prompt + validación local.
            log.warning("llm[%s] output_config rechazado (%s); sigo sin structured outputs", role, type(e).__name__)
            kw = {}
            last = e
            continue
        try:
            obj = _extract_json(resp, schema)
        except LLMJSONError as e:
            _diag(role, resp, f"fallo {e.reason} (intento {attempt + 1})")
            last = e
            continue
        _diag(role, resp, f"ok (intento {attempt + 1})")
        return obj
    raise last if last else LLMJSONError("empty")
