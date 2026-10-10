"""Herramientas (schemas) que el Operator puede llamar. Son nuestras: el modelo decide, nosotros ejecutamos por RFB."""

_WHY = {"type": "string", "description": "Intención de la acción en pocas palabras (qué esperas que pase)."}
_RISK = {
    "type": "string",
    "enum": ["lectura", "cambio_esperado", "destructivo"],
    "description": "lectura = navegar/buscar/leer; cambio_esperado = el cambio que pide la tarea; destructivo = borrar/anular/pagar/enviar o cambios no pedidos (se bloquea).",
}
_X = {"type": "integer", "description": "Coordenada x (0-1279) en la captura."}
_Y = {"type": "integer", "description": "Coordenada y (0-799) en la captura."}

TOOLS = [
    {
        "name": "click",
        "description": "Clic izquierdo en una coordenada de la pantalla.",
        "input_schema": {"type": "object", "properties": {"x": _X, "y": _Y, "why": _WHY, "risk": _RISK},
                          "required": ["x", "y", "why", "risk"]},
    },
    {
        "name": "double_click",
        "description": "Doble clic izquierdo (abrir una fila, seleccionar una palabra).",
        "input_schema": {"type": "object", "properties": {"x": _X, "y": _Y, "why": _WHY, "risk": _RISK},
                          "required": ["x", "y", "why", "risk"]},
    },
    {
        "name": "right_click",
        "description": "Clic derecho (menú contextual).",
        "input_schema": {"type": "object", "properties": {"x": _X, "y": _Y, "why": _WHY, "risk": _RISK},
                          "required": ["x", "y", "why", "risk"]},
    },
    {
        "name": "type_text",
        "description": "Escribe texto en un campo. Si pasas x,y hace clic ahí primero (ahorra un paso). Por defecto selecciona todo el contenido previo (Ctrl+A) para reemplazarlo, porque los campos concatenan.",
        "input_schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Texto exacto a escribir (respeta tildes y mayúsculas)."},
            "x": {"type": "integer", "description": "Opcional: x del campo donde hacer clic antes de escribir."},
            "y": {"type": "integer", "description": "Opcional: y del campo donde hacer clic antes de escribir."},
            "replace": {"type": "boolean", "description": "true (defecto): Ctrl+A antes de escribir. false: solo escribir."},
            "enter": {"type": "boolean", "description": "true: pulsa Enter al terminar (p.ej. buscar)."},
            "tab": {"type": "boolean", "description": "true: pulsa Tab al terminar (pasar al siguiente campo)."},
            "why": _WHY, "risk": _RISK},
            "required": ["text", "why", "risk"]},
    },
    {
        "name": "press_key",
        "description": "Pulsa una tecla o combinación: Return, Tab, Escape, BackSpace, Delete, Home, End, Up, Down, PageDown, ctrl+a, shift+Tab, F5...",
        "input_schema": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Tecla o combinación, p.ej. 'Return', 'Tab', 'ctrl+a'."},
            "repeat": {"type": "integer", "description": "Veces (1-20)."},
            "why": _WHY, "risk": _RISK},
            "required": ["key", "why", "risk"]},
    },
    {
        "name": "scroll",
        "description": "Desplaza con la rueda del ratón sobre una coordenada.",
        "input_schema": {"type": "object", "properties": {
            "x": _X, "y": _Y,
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "integer", "description": "Clics de rueda (1-10)."},
            "why": _WHY, "risk": _RISK},
            "required": ["x", "y", "direction", "why", "risk"]},
    },
    {
        "name": "drag",
        "description": "Arrastra con el botón izquierdo desde un punto a otro (seleccionar texto, mover barra).",
        "input_schema": {"type": "object", "properties": {
            "x0": _X, "y0": _Y, "x1": _X, "y1": _Y, "why": _WHY, "risk": _RISK},
            "required": ["x0", "y0", "x1", "y1", "why", "risk"]},
    },
    {
        "name": "zoom",
        "description": "Devuelve una ampliación de una región de la pantalla para leer texto pequeño. No cambia nada. Las coordenadas para clics siguen siendo las de la pantalla completa.",
        "input_schema": {"type": "object", "properties": {
            "x0": _X, "y0": _Y, "x1": _X, "y1": _Y, "why": _WHY},
            "required": ["x0", "y0", "x1", "y1", "why"]},
    },
    {
        "name": "wait",
        "description": "Espera unos segundos (carga lenta) y devuelve una captura nueva.",
        "input_schema": {"type": "object", "properties": {
            "seconds": {"type": "number", "description": "1-10"}, "why": _WHY},
            "required": ["seconds", "why"]},
    },
    {
        "name": "remember",
        "description": "Guarda una nota sobre cómo funciona esta app (dónde está un módulo, cómo se busca, qué popup aparece) para tareas futuras. No cambia nada en la app.",
        "input_schema": {"type": "object", "properties": {
            "note": {"type": "string", "description": "Nota corta y concreta."}},
            "required": ["note"]},
    },
    {
        "name": "done",
        "description": "Termina la tarea y reporta el resultado. Úsala solo cuando hayas verificado en pantalla (terminada) o cuando la tarea no se pueda/deba hacer (fallida).",
        "input_schema": {"type": "object", "properties": {
            "estado": {"type": "string", "enum": ["terminada", "fallida"]},
            "resumen": {"type": "string", "description": "Qué hiciste y cómo lo verificaste, o por qué falló (con datos concretos). Sin secretos."},
            "respuesta": {"type": "string", "description": "Solo en preguntas terminadas: el valor pedido. Número puro si es numérico; dos datos separados por '; ' en el orden preguntado."},
            "claves": {"type": "array", "items": {"type": "string"}, "description": "Solo en fallida: identificadores/datos que explican el fallo (p.ej. cédulas de los homónimos)."},
            "verificado_en_pantalla": {"type": "boolean", "description": "true si viste la evidencia final en pantalla (ficha/lista/mensaje)."},
            "evidencia": {"type": "string", "description": "Qué evidencia exacta viste (texto del mensaje, valores en la ficha, contador)."}},
            "required": ["estado", "resumen", "verificado_en_pantalla", "evidencia"]},
    },
]

ACTION_TOOLS = {"click", "double_click", "right_click", "type_text", "press_key", "scroll", "drag"}
