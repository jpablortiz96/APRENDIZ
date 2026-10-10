"""Guarda anti-duplicados para operaciones no idempotentes (Guardar, Crear, Exportar, Confirmar, Enviar...).

No confía solo en el `risk` que declara el modelo: clasifica con varias señales (herramienta, tecla, intención
`why`, tipo de tarea, acción previa). Tras una confirmación, su efecto queda INCIERTO; repetir la MISMA
confirmación solo se permite cuando hubo progreso real desde entonces (nuevo dato escrito o navegación que
cambió la pantalla, p.ej. revisar la lista y reabrir el formulario). Esperar o mirar no es prueba de que la
operación no ocurrió.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

TOLERANCE_PX = 15

# Verbos de confirmación/escritura (es/en). "agregar/añadir" se excluyen: suelen abrir formularios.
_COMMIT = re.compile(
    r"\b(guard\w*|grab\w*|crear|crea|cree|creo|registrar|registra|export\w*|confirm\w*|envi\w*|aplic\w*|"
    r"emit\w*|procesa\w*|finaliz\w*|actualizar|actualiza|publica\w*|generar\s+(el\s+)?(reporte|informe|archivo)|"
    r"descarg\w*|submit\w*|save\w*|send\w*|apply|post)\b", re.IGNORECASE)
# Intenciones de navegación/lectura que pueden mencionar un verbo de escritura sin ejecutarlo
# ("abrir el formulario para crear cliente", "cerrar el aviso de guardado").
_NAV_FIRST = re.compile(
    r"^\s*(abrir|abre|ir|ve|ver|navegar|volver|buscar|filtrar|seleccionar|elegir|cerrar|cancelar|"
    r"descartar|revisar|comprobar|verificar|leer|desplegar|enfocar|escribir|llenar|completar|open|go|view|close|cancel)\b",
    re.IGNORECASE)
_SUBMIT_KEYS = {"return", "enter", "kp_enter", "ctrl+s", "ctrl+return", "ctrl+enter"}
WRITE_TASKS = {"crear", "modificar", "exportar", "otro"}


def is_commit(name: str, args: dict, plan_tipo: str = "", prev_tool: str = "") -> bool:
    why = str(args.get("why", ""))
    risk = str(args.get("risk", ""))
    commit_words = bool(_COMMIT.search(why)) and not _NAV_FIRST.search(why)
    if name in ("click", "double_click"):
        return risk == "cambio_esperado" and not _NAV_FIRST.search(why) or commit_words
    if name == "press_key":
        key = str(args.get("key", "")).strip().lower()
        if key not in _SUBMIT_KEYS:
            return False
        # Enter justo después de escribir en una tarea de escritura suele enviar el formulario.
        return risk == "cambio_esperado" or commit_words or (prev_tool == "type_text" and plan_tipo in WRITE_TASKS
                                                             and not _NAV_FIRST.search(why))
    if name == "type_text" and args.get("enter"):
        return risk == "cambio_esperado" and commit_words or (commit_words and plan_tipo in WRITE_TASKS)
    return False


def commit_key(name: str, args: dict) -> Optional[tuple]:
    try:
        if name in ("click", "double_click"):
            return ("point", int(args["x"]), int(args["y"]))  # clic y doble clic sobre el mismo control = misma operación
        if name == "press_key":
            return ("key", str(args.get("key", "")).strip().lower(), 0)
        if name == "type_text":
            x, y = args.get("x"), args.get("y")
            return ("type_enter", int(x) if x is not None else -1, int(y) if y is not None else -1)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def same_op(a: tuple, b: tuple) -> bool:
    if a[0] != b[0]:
        return False
    if a[0] == "key":
        return a[1] == b[1]
    return abs(a[1] - b[1]) <= TOLERANCE_PX and abs(a[2] - b[2]) <= TOLERANCE_PX


@dataclass
class PendingCommit:
    key: tuple
    step: int
    uncertain: bool          # la conexión cayó durante la operación
    pre_hash: Optional[str]
    post_hash: Optional[str]


class CommitGuard:
    def __init__(self):
        self.pending: Optional[PendingCommit] = None
        self.progressed = True
        self.blocked = 0

    def check(self, key: Optional[tuple]) -> Optional[str]:
        """Motivo de bloqueo si `key` repite la última confirmación sin progreso real; None si se permite."""
        p = self.pending
        if key is None or p is None or self.progressed or not same_op(p.key, key):
            return None
        self.blocked += 1
        if p.uncertain:
            why = "la conexión cayó mientras se enviaba"
        elif p.pre_hash is not None and p.pre_hash == p.post_hash:
            why = "la pantalla no mostró cambio, pero una app lenta puede haberla aplicado igualmente"
        else:
            why = "la pantalla cambió tras el primer intento: probablemente ya se aplicó"
        return (f"⛔ Guarda anti-duplicados: esta confirmación ya se envió en el paso {p.step} y su resultado es incierto "
                f"({why}). Esperar o mirar la misma pantalla NO prueba que no ocurrió. Verifica el estado real "
                "(abre la lista/ficha, busca el registro o el mensaje de exportación). Si existe, no repitas: llama a done. "
                "Si confirmas que NO existe, vuelve al formulario, corrige o reingresa los datos y entonces podrás confirmar.")

    def record_commit(self, key: tuple, step: int, pre_hash: Optional[str], post_hash: Optional[str], uncertain: bool):
        self.pending = PendingCommit(key, step, uncertain, pre_hash, post_hash)
        self.progressed = False

    def record_action(self, name: str, changed: Optional[bool]):
        """Acción NO confirmatoria ejecutada. Progreso = nuevo dato escrito o navegación con cambio visible."""
        if self.pending is None:
            return
        if name == "type_text" or (changed and name in ("click", "double_click", "press_key", "scroll", "drag",
                                                         "right_click")):
            self.progressed = True
