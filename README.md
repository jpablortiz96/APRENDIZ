# APRENDIZ · el agente que opera lo que no tiene API

> Frontier · Reto 1. Un agente que aprende a operar sistemas sin API **por pantalla** y ejecuta tareas en lenguaje natural en apps que nunca ha visto. Trabaja como un empleado nuevo: mira, aprende, ejecuta con cuidado, **verifica antes de reportar**.

## Qué hace

1. Recibe una tarea (`POST /tasks`) y un escritorio VNC (URL de websockify + contraseña).
2. Se conecta al escritorio con un **cliente RFB propio** (protocolo VNC sobre WebSocket, sin librerías de automatización ni proveedores externos).
3. Un modelo con visión (Claude) opera la app en un ciclo **ver → pensar → una acción → verificar**, con cinco roles:
   - **Planner**: clasifica la tarea (crear / modificar / consultar / exportar / peligrosa), extrae los datos literales y define criterios de éxito y acciones prohibidas.
   - **Operator**: recibe la captura estabilizada y elige UNA acción (`click`, `type_text`, `press_key`, `scroll`, `zoom`, `remember`, `done`…), declarando intención y nivel de riesgo.
   - **Verifier**: con una captura fresca y el registro de acciones decide si hay evidencia visible de que la tarea quedó bien; sin evidencia → `fallida` (nunca un falso éxito).
   - **Guardian** (determinista): bloquea acciones destructivas, detecta bucles, corta a tiempo, formatea el resumen (`respuesta: …`, claves de razón) y filtra secretos.
   - **Cartographer**: después de cada tarea consolida un **mapa de la app** (módulos, formularios, filtros, recetas) para que las siguientes tareas en la misma app sean más rápidas. El escritorio se resetea; el conocimiento, no.
4. Reporta `terminada` o `fallida` con un resumen, cierra la conexión y no vuelve a tocar el escritorio.

Nada está cableado a la app de pruebas: no hay selectores, coordenadas fijas ni nombres de botones en el código. Todo se decide mirando la pantalla.

## Contrato (entrypoint)

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/health` | 200 cuando el servicio está listo |
| `POST` | `/tasks` | Cuerpo `{"tarea": "...", "escritorio": {"url": "wss://.../websockify", "password": "..."}}` → `{"task_id": "..."}` en < 2 s |
| `GET` | `/tasks/{task_id}` | `{"estado": "en_curso" \| "terminada" \| "fallida", "resumen": "..."}` (`?debug=1` agrega plan, acciones y verificación) |

Todas las llamadas llevan `Authorization: Bearer <token del equipo>`.

## Estructura

```
app/
  main.py       FastAPI: contrato, auth Bearer, tareas en background
  agent.py      Orquestador: conexión, Planner → Operator loop → Verifier → reporte; Cartographer al final
  rfb.py        Cliente RFB/VNC sobre WebSocket: handshake, auth DES, Raw/CopyRect/ZRLE, capturas, zoom, ratón y teclado
  keysyms.py    Teclas → keysyms X11 (tildes y ñ incluidas)
  tools.py      Herramientas que el Operator puede llamar (schemas)
  prompts.py    Prompts de los cinco roles
  guardian.py   Política de seguridad, detector de bucles, formato del resumen, filtro de secretos
  memory.py     Mapa de la app por huella (JSON persistente)
  config.py     Variables de entorno y presupuestos de tiempo
scripts/
  vnc_probe.py  Prueba el cliente VNC: captura, clic, escritura, zoom
  probar.py     Ciclo completo: __reset → POST /tasks → sondeo → __check
```

## Ejecutar en local (Windows / PowerShell)

```powershell
git clone <este repo> aprendiz
cd aprendiz
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:TEAM_TOKEN = "<token del equipo>"
$env:ANTHROPIC_API_KEY = "<tu api key>"
uvicorn app.main:app --port 8000
```

En otra terminal, primero prueba la capa VNC contra tu escritorio:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts\vnc_probe.py --url wss://<tu-escritorio>.up.railway.app/websockify --password <clave> --out captura.png
```

Y luego el ciclo completo con una prueba del material:

```powershell
$env:DESKTOP_URL = "https://<tu-escritorio>.up.railway.app"
$env:DESKTOP_PASSWORD = "<clave del escritorio>"
$env:DESKTOP_TOKEN = "<token del escritorio>"
$env:AGENT_URL = "http://localhost:8000"
python scripts\probar.py material\pruebas\E1-crear-cliente.json --debug
python scripts\probar.py material\pruebas\*.json
```

## Desplegar en Railway

1. Sube el repo a GitHub (público).
2. Railway → **New Project → Deploy from GitHub repo** → elige el repo. Railway detecta el `Dockerfile`.
3. En **Variables** agrega `TEAM_TOKEN` y `ANTHROPIC_API_KEY` (y opcionalmente las de `.env.example`).
4. **Settings → Networking → Generate Domain**. La URL pública es el entrypoint: `https://<servicio>.up.railway.app`.
5. Verifica: `GET https://<servicio>.up.railway.app/health` → 200, y corre `scripts\probar.py` con `AGENT_URL` apuntando a esa URL.

## Reglas de evaluación y cómo las cubre el agente

| Regla | Mecanismo |
| --- | --- |
| Cambios esperados en la app | Plan con datos literales + verificación visual del registro/lista antes de reportar |
| Sin acciones fuera de la tarea | Cada acción declara intención y riesgo; Guardian bloquea lo destructivo; popups con extras no pedidos se cancelan |
| Sin registros duplicados | Buscar antes de crear; tras Guardar, confirmar en la lista antes de reinsertar; exportar una sola vez |
| Estado final correcto / sin falso éxito | Verifier con captura fresca; sin evidencia → `fallida` |
| `respuesta: <valor>` (número primero, `;` entre datos) | Guardian construye la primera línea del resumen a partir del campo `respuesta` |
| Fallos nombran los datos (homónimos) | `done(fallida, claves=[…])`; si faltan en el texto, se agregan |
| Sin secretos en el resumen | Filtro de contraseña, URL y token |
| 5 minutos y nada después | Cronómetro: operador hasta 3:45, verificación hasta 4:22, límite duro 4:35; al reportar se cierra la conexión VNC |
| "Hoy" = fecha de la app | El Planner la lee de la captura inicial y el Operator la usa para periodos relativos |

## Variables de entorno

Ver `.env.example`. Las importantes: `TEAM_TOKEN`, `ANTHROPIC_API_KEY`, `MODEL_OPERATOR` (por defecto `claude-sonnet-5-5`), `MAX_STEPS`, `OPERATOR_DEADLINE`.
