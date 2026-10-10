"""Guardian: política de seguridad, tiempo, formato del resumen y filtro de secretos. Todo determinista."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

DESTRUCTIVE_WORDS = re.compile(
    r"\b(elimin\w*|borr\w*|anul\w*|pagar|transfer\w*|"
    r"enviar (correo|email|mensaje|sms|whatsapp)|masiv\w*|vaciar|purgar|dar de baja|desactivar)\b",
    re.IGNORECASE,
)


@dataclass
class Secrets:
    password: str = ""
    url: str = ""
    token: str = ""

    def redact(self, text: str) -> str:
        if not text:
            return text
        for s in (self.password, self.token):
            if s and len(s) >= 4:
                text = text.replace(s, "[oculto]")
        if self.url:
            host = self.url.split("://", 1)[-1].split("/", 1)[0]
            if host:
                text = text.replace(host, "[escritorio]")
            text = text.replace(self.url, "[escritorio]")
        # patrones genéricos
        text = re.sub(r"wss?://\S+", "[escritorio]", text)
        text = re.sub(r"(contraseña|password|clave|token)\s*[:=]\s*\S+", r"\1: [oculto]", text, flags=re.IGNORECASE)
        return text


@dataclass
class ActionRecord:
    step: int
    tool: str
    args: dict
    why: str = ""
    risk: str = ""
    changed: Optional[bool] = None
    blocked: bool = False

    def short(self) -> str:
        a = {k: v for k, v in self.args.items() if k not in ("why", "risk")}
        s = f"{self.step}. {self.tool}({', '.join(f'{k}={v!r}' for k, v in a.items())})"
        if self.why:
            s += f" — {self.why}"
        if self.blocked:
            s += " [BLOQUEADA]"
        elif self.changed is False:
            s += " [sin cambio visible]"
        return s


def check_action(tool: str, args: dict, plan: dict) -> Optional[str]:
    """Devuelve un motivo de bloqueo o None si la acción se permite."""
    risk = (args.get("risk") or "").lower()
    why = (args.get("why") or "")
    if risk == "destructivo":
        return ("Acción bloqueada por política: es destructiva o está fuera de la tarea. "
                "Si la tarea exige esto, repórtala como fallida explicando por qué.")
    if tool in ("click", "double_click", "press_key", "type_text") and DESTRUCTIVE_WORDS.search(why):
        if plan.get("tipo") not in ("peligrosa",) and not _task_requests_destruction(plan):
            return ("Acción bloqueada por política: la intención describe borrar/anular/pagar/enviar, que no forma parte de la tarea. "
                    "Si de verdad es necesaria, reporta fallida explicando el motivo.")
    return None


def _task_requests_destruction(plan: dict) -> bool:
    obj = (plan.get("objetivo") or "") + " " + " ".join(plan.get("pasos_sugeridos") or [])
    return bool(DESTRUCTIVE_WORDS.search(obj))


_LEADING_WORDS = re.compile(r"^(tiene|hay|son|es|existen|total(?:\s+de)?|en total|un total de)\s*:?\s*(?=-?\d)", re.IGNORECASE)


def _tidy_part(value: str) -> str:
    """Quita muletillas antes de un número ("Tiene 45" -> "45"); no reordena textos (direcciones, códigos)."""
    v = value.strip().rstrip(".")
    v = _LEADING_WORDS.sub("", v)
    return v.strip()


def _clean_answer(value: str) -> str:
    v = (value or "").strip()
    v = re.sub(r"^(respuesta\s*:)\s*", "", v, flags=re.IGNORECASE)
    if ";" in v:
        return "; ".join(_tidy_part(p) for p in v.split(";"))
    return _tidy_part(v)


def format_summary(estado: str, plan: dict, resumen: str, respuesta: Optional[str], claves: list[str] | None,
                   secrets: Secrets, extra_note: str = "") -> str:
    resumen = (resumen or "").strip()
    # Quitar cualquier línea respuesta: del cuerpo; la reconstruimos nosotros
    body_lines = [ln for ln in resumen.splitlines() if not re.match(r"^\s*respuesta\s*:", ln, re.IGNORECASE)]
    body = "\n".join(ln for ln in body_lines).strip()
    lines: list[str] = []
    if estado == "terminada" and plan.get("tipo") == "consultar":
        ans = _clean_answer(respuesta or "")
        if ans:
            lines.append(f"respuesta: {ans}")
    if estado == "terminada" and plan.get("tipo") != "consultar" and respuesta and plan.get("tipo") == "otro":
        ans = _clean_answer(respuesta)
        if ans:
            lines.append(f"respuesta: {ans}")
    if extra_note:
        lines.append(extra_note.strip())
    if body:
        lines.append(body)
    if estado == "fallida":
        claves = [c for c in (claves or []) if c]
        missing = [c for c in claves if c.lower() not in "\n".join(lines).lower()]
        if missing:
            lines.append("Datos relevantes: " + "; ".join(missing))
        if not lines:
            lines.append("La tarea no se pudo completar.")
    text = "\n".join(lines).strip()
    text = secrets.redact(text)
    if len(text) > 1800:
        text = text[:1800].rstrip() + "…"
    return text


@dataclass
class LoopDetector:
    """Detecta repeticiones de la misma acción sin efecto."""
    history: list = field(default_factory=list)

    def push(self, tool: str, args: dict, changed: Optional[bool]) -> Optional[str]:
        key = (tool, tuple(sorted((k, str(v)) for k, v in args.items() if k not in ("why", "risk"))))
        self.history.append((key, changed))
        last = self.history[-3:]
        if len(last) == 3 and all(k == key for k, _ in last) and all(c is False for _, c in last):
            return "⚠ Has repetido la misma acción 3 veces sin cambio visible. Cambia de estrategia (otro elemento, Tab/Enter, cerrar popup, scroll, zoom para verificar)."
        if len(last) == 3 and all(k == key for k, _ in last):
            return "⚠ Tres veces la misma acción seguida. Confirma en la captura si realmente hace falta repetirla."
        return None


# ----------------------------------------------------------------- redacción en logs y diagnósticos
_LOG_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Registra un secreto (contraseña, token, host del escritorio) para redactarlo en TODOS los logs del proceso."""
    if value and len(value) >= 4:
        _LOG_SECRETS.add(value)


def redact_text(text: str) -> str:
    for s in sorted(_LOG_SECRETS, key=len, reverse=True):
        text = text.replace(s, "[oculto]")
    text = re.sub(r"wss?://\S+", "[escritorio]", text)
    text = re.sub(r"(contraseña|password|clave|token|authorization|bearer)(\s*[:=]\s*|\s+)(?!\[oculto\])[^\s,;\"']+",
                  r"\1\2[oculto]", text, flags=re.IGNORECASE)
    return text


def redact_obj(obj: Any, secrets: Secrets) -> Any:
    """Redacción recursiva (no sobre el JSON serializado, que se podría corromper)."""
    if isinstance(obj, str):
        return redact_text(secrets.redact(obj))
    if isinstance(obj, list):
        return [redact_obj(x, secrets) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v, secrets) for k, v in obj.items()}
    return obj


class RedactingFilter(logging.Filter):
    """Filtro de handler: redacta el mensaje final y la traza de cualquier excepción antes de emitirlos."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg, record.args = redact_text(msg), None
        if record.exc_info:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
