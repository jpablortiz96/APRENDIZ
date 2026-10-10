"""Mapa de nombres de teclas a keysyms X11 (los que entiende el protocolo RFB)."""

KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "intro": 0xFF0D,
    "tab": 0xFF09,
    "backspace": 0xFF08, "retroceso": 0xFF08,
    "escape": 0xFF1B, "esc": 0xFF1B,
    "delete": 0xFFFF, "del": 0xFFFF, "suprimir": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50, "inicio": 0xFF50,
    "end": 0xFF57, "fin": 0xFF57,
    "left": 0xFF51, "izquierda": 0xFF51,
    "up": 0xFF52, "arriba": 0xFF52,
    "right": 0xFF53, "derecha": 0xFF53,
    "down": 0xFF54, "abajo": 0xFF54,
    "pageup": 0xFF55, "page_up": 0xFF55, "pgup": 0xFF55,
    "pagedown": 0xFF56, "page_down": 0xFF56, "pgdn": 0xFF56,
    "space": 0x0020, "espacio": 0x0020,
    "ctrl": 0xFFE3, "control": 0xFFE3,
    "shift": 0xFFE1, "mayus": 0xFFE1,
    "alt": 0xFFE9,
    "super": 0xFFEB, "win": 0xFFEB, "cmd": 0xFFEB, "meta": 0xFFE7,
    "capslock": 0xFFE5,
}
for _i in range(1, 13):
    KEYSYMS[f"f{_i}"] = 0xFFBE + _i - 1

MODIFIERS = {"ctrl", "control", "shift", "mayus", "alt", "super", "win", "cmd", "meta"}

# Caracteres ASCII que requieren Shift en un teclado US (por si el servidor lo necesita)
SHIFTED_ASCII = set('~!@#$%^&*()_+{}|:"<>?')


def char_to_keysym(ch: str) -> int:
    """Keysym para un carácter: Latin-1 directo; el resto como keysym Unicode."""
    code = ord(ch)
    if code < 0x100:
        return code
    return 0x01000000 | code


def parse_combo(combo: str) -> list[int]:
    """'ctrl+a' -> [Control_L, 'a']. Devuelve keysyms en orden (modificadores primero)."""
    parts = [p.strip() for p in combo.replace(" ", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("combinación vacía")
    syms = []
    for p in parts:
        low = p.lower()
        if low in KEYSYMS:
            syms.append(KEYSYMS[low])
        elif len(p) == 1:
            syms.append(char_to_keysym(p))
        else:
            raise ValueError(f"tecla desconocida: {p}")
    return syms


def is_modifier(name: str) -> bool:
    return name.lower() in MODIFIERS
