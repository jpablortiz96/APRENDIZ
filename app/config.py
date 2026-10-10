"""Configuración por variables de entorno."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


TEAM_TOKEN = os.environ.get("TEAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Modelos: el operador necesita visión y buena precisión de coordenadas; el verificador también ve capturas.
MODEL_OPERATOR = os.environ.get("MODEL_OPERATOR", "claude-sonnet-5-5")
MODEL_PLANNER = os.environ.get("MODEL_PLANNER", MODEL_OPERATOR)
MODEL_VERIFIER = os.environ.get("MODEL_VERIFIER", MODEL_OPERATOR)
MODEL_CARTOGRAPHER = os.environ.get("MODEL_CARTOGRAPHER", "claude-haiku-5-5")  # ID vigente (docs, oct-2026)


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# Feature flags v0.2 (rollback individual: poner a 0 en Railway y redeploy, sin tocar código).
MEMORY_V2 = _flag("MEMORY_V2", True)          # mapas sin coordenadas + confianza + detección de drift visual
RECOVERY_V2 = _flag("RECOVERY_V2", True)      # reconexión VNC acotada + guarda anti-repetición de escrituras
PERCEPTION_V2 = _flag("PERCEPTION_V2", True)  # no reenviar capturas idénticas + caché de prompt incremental
VNC_RECONNECTS = _int("VNC_RECONNECTS", 2)    # reconexiones máximas por tarea

# Presupuesto de tiempo por tarea (segundos desde POST /tasks). El evaluador corta a los 300 s.
OPERATOR_DEADLINE = _float("OPERATOR_DEADLINE", 225.0)   # el operador deja de actuar aquí
VERIFY_DEADLINE = _float("VERIFY_DEADLINE", 262.0)       # último momento para verificar
HARD_LIMIT = _float("HARD_LIMIT", 275.0)                 # límite absoluto: se reporta sí o sí
MAX_STEPS = _int("MAX_STEPS", 45)
KEEP_IMAGES = _int("KEEP_IMAGES", 4)                     # capturas que se conservan en el historial
# Salida JSON de roles auxiliares. El thinking adaptativo de los modelos actuales consume max_tokens:
# con 800 el Verifier podía cortarse (stop_reason=max_tokens) antes de escribir el JSON.
VERIFIER_MAX_TOKENS = _int("VERIFIER_MAX_TOKENS", 4096)
VERIFIER_RETRY_MAX_TOKENS = _int("VERIFIER_RETRY_MAX_TOKENS", 8192)
PLANNER_MAX_TOKENS = _int("PLANNER_MAX_TOKENS", 4096)

VNC_CONNECT_TIMEOUT = _float("VNC_CONNECT_TIMEOUT", 25.0)
RFB_ENCODINGS = os.environ.get("RFB_ENCODINGS", "zrle,copyrect,raw")  # orden de preferencia

MEMORY_PATH = os.environ.get("MEMORY_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "app_maps.json"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "logs"))
SAVE_SCREENSHOTS = os.environ.get("SAVE_SCREENSHOTS", "0") == "1"
DEBUG_API = os.environ.get("DEBUG_API", "0") == "1"      # permite ?debug=1 en GET /tasks/{id}


def encodings_list() -> list[int]:
    names = {"zrle": 16, "copyrect": 1, "raw": 0}
    out = []
    for n in RFB_ENCODINGS.split(","):
        n = n.strip().lower()
        if n in names:
            out.append(names[n])
    if 0 not in out:
        out.append(0)
    out.append(-223)  # DesktopSize
    return out
