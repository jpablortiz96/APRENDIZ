<p align="center">
  <img src="docs/assets/aprendiz-banner.svg" alt="APRENDIZ: The AI agent that learns to operate software without an API" width="100%">
</p>

<p align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-2DD4BF?logo=python&logoColor=white&labelColor=0A1024">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-contract-2DD4BF?logo=fastapi&logoColor=white&labelColor=0A1024">
  <img alt="VNC/RFB" src="https://img.shields.io/badge/VNC%2FRFB-own%20client-A78BFA?labelColor=0A1024">
  <img alt="Architecture" src="https://img.shields.io/badge/architecture-5%20roles-A78BFA?labelColor=0A1024">
  <img alt="Unit tests" src="https://img.shields.io/badge/unit%20tests-64%2F64-2DD4BF?labelColor=0A1024">
  <img alt="Frontier public tests" src="https://img.shields.io/badge/Frontier%20E1%E2%80%93E5-5%2F5%20real-2DD4BF?labelColor=0A1024">
</p>

<p align="center">
  <a href="https://github.com/jpablortiz96/APRENDIZ"><b>GitHub</b></a> ·
  <a href="https://youtu.be/0G00tMEyqzA"><b>Demo</b></a> ·
  <a href="https://aprendiz-production-6a41.up.railway.app/health"><b>Endpoint</b></a> ·
  <a href="docs/V2_ENGINEERING.md"><b>Engineering log</b></a>
</p>

<p align="center">
  <a href="https://youtu.be/0G00tMEyqzA"><img src="docs/assets/demo-thumbnail.svg" alt="Watch the 2-minute demo" width="720"></a><br>
  <a href="https://youtu.be/0G00tMEyqzA"><b>▶ Watch the 2-minute demo</b></a>
</p>

> **Producción:** v0.2.1, commit `31c8367`. **Rollback:** v0.1.3, commit `98b34ff`.

---

**🇬🇧 Executive summary.** APRENDIZ receives a task in natural language and carries it out inside a desktop application it has **never seen**, the way a careful new employee would. It looks at the screen, takes one step at a time with mouse and keyboard through its **own VNC/RFB client**, and only reports success when an **independent verifier sees the evidence**. It uses no selectors, no fixed coordinates, no internal app APIs and no third-party automation providers.

**🇪🇸 Resumen ejecutivo.** APRENDIZ recibe una tarea en lenguaje natural y la ejecuta dentro de una aplicación de escritorio que **nunca ha visto**, como un empleado nuevo y cuidadoso. Mira la pantalla, da un paso cada vez con ratón y teclado mediante su **propio cliente VNC/RFB**, y solo reporta éxito cuando un **verificador independiente ve la evidencia**. Sin selectores, sin coordenadas fijas, sin APIs internas y sin proveedores externos de automatización.

## Contenido

- [Por qué no es RPA tradicional](#-por-qué-no-es-rpa-tradicional)
- [Arquitectura](#-arquitectura)
- [Los cinco roles](#-los-cinco-roles)
- [Capacidades v0.2.1](#-capacidades-v021)
- [Resultados](#-resultados)
- [Incidente FR-0153](#-incidente-fr-0153-interfaz-modificada)
- [Seguridad](#-seguridad)
- [Recuperación, memoria y optimización](#-recuperación-memoria-y-optimización)
- [Instalación](#-instalación)
- [API REST](#-api-rest)
- [Configuración y rollback](#-configuración-y-rollback)
- [Límites conocidos](#-límites-conocidos)
- [Demo](#-demo)
- [Roadmap](#-roadmap)
- [Autor](#-autor)

---

## 💡 Por qué no es RPA tradicional

| | RPA tradicional | APRENDIZ |
|---|---|---|
| Cómo encuentra un botón | Selector o coordenada grabada | Por función y contexto en la captura **actual** |
| Si la UI cambia (tema, etiquetas, posiciones) | Se rompe y hay que regrabar | Detecta el cambio y vuelve a localizar los controles |
| Qué aprende | Un script fijo | Un mapa **funcional** de la app (qué existe y para qué sirve), sin coordenadas |
| Cuándo reporta éxito | Cuando el script termina | Cuando un Verifier independiente **ve** la evidencia |
| Acciones peligrosas o duplicadas | Las ejecuta si están en el script | Las bloquea por política determinista |
| Integración | Conectores o APIs | Ninguna: solo píxeles, ratón y teclado por VNC |

## 🏗 Arquitectura

```mermaid
flowchart LR
    C[Cliente / evaluador] -->|POST /tasks| API[FastAPI<br/>Bearer + contrato]
    API --> TR[TaskRunner<br/>1 tarea por escritorio<br/>límite 275 s]
    TR --> PL[Planner]
    PL --> OP[Operator<br/>una acción por turno]
    MEM[(Memoria adaptativa<br/>mapa funcional + firma UI)] -.pista.-> OP
    OP --> GU{Guardian<br/>política + anti-duplicados}
    GU -->|permitida| RFB[Motor RFB/VNC<br/>cliente propio]
    GU -->|bloqueada| OP
    RFB -->|captura estabilizada| OP
    RC[Recovery<br/>reconexión acotada] <--> RFB
    OP -->|done| VE[Verifier<br/>captura fresca + evidencias]
    VE -->|sin evidencia| OP
    VE -->|verificado| OUT[estado + resumen]
    OUT --> CA[Cartographer] --> MEM
    C -->|GET /tasks/id| API
```

**Principio de diseño:** el modelo (Claude con visión) **decide**; el código **ejecuta, limita y verifica**. Todo lo que debe ser seguro (política, duplicados, tiempos, secretos, formato del resumen) es determinista.

## 🧩 Los cinco roles

| | Rol | Qué hace |
|---|---|---|
| 🧭 | **Planner** | Clasifica la tarea (crear, modificar, consultar, exportar, peligrosa o imposible), copia los datos literales y define criterios de éxito y acciones prohibidas. Rechaza de entrada lo peligroso. |
| 🖱️ | **Operator** | Ve la captura estabilizada y elige **una** herramienta por turno (`click`, `type_text`, `press_key`, `scroll`, `zoom`, `wait`, `remember`, `done`), declarando intención y riesgo. |
| 🔍 | **Verifier** | Juicio independiente con captura fresca más las capturas ancladas de cada página. Su salida es JSON validado con esquema y tiene un reintento acotado. Sin evidencia, la tarea no se da por terminada. |
| 🛡️ | **Guardian** | Código determinista: bloquea lo destructivo y los duplicados, detecta bucles, controla los plazos, construye la línea `respuesta:` y redacta secretos. |
| 🗺️ | **Cartographer** | Al terminar, actualiza el mapa funcional de la app para las siguientes tareas. |

A esto se suman el **motor RFB/VNC propio** (`app/rfb.py`): RFB 3.8, autenticación VNC (DES), Raw, CopyRect y ZRLE, estabilización de pantalla, ratón y teclado con tildes y ñ.

## 🚀 Capacidades v0.2.1

> Leyenda: ✅ observado en el escritorio real de Frontier · 🧪 cubierto por tests unitarios o benchmark sintético (no validado aún en una app desconocida real).

| Capacidad | Cómo funciona | Evidencia |
|---|---|---|
| Adaptación a etiquetas y controles reorganizados | Busca cada control por función, etiqueta equivalente y contexto | ✅ En SIGECOM v4.2 usó «Aplicar» como acción de guardar (E1, E2) |
| Detección de cambios visuales | Firma de paleta y maquetación; si no coincide con la UI donde se aprendió el mapa, avisa al Operator | 🧪 4/4 pares sintéticos |
| Memoria funcional sin coordenadas | El mapa guarda qué existe y para qué, nunca posiciones; limpia mapas antiguos | 🧪 |
| Protección contra duplicados | Detecta confirmaciones con varias señales. Tras enviar una, esperar no desbloquea la repetición; el doble clic sobre una confirmación se envía como clic simple | ✅ 0 duplicados en E1–E5 · 🧪 |
| Validación con evidencia | Verifier independiente, evidencias por página, esquema JSON | ✅ E3 multipágina: `respuesta: 45` |
| Prompt injection | El texto de la app es dato; el Guardian bloquea intenciones destructivas | ✅ E2: nota maliciosa ignorada |
| Reconexión VNC | Hasta 2 por tarea, dentro del presupuesto; nunca verifica sobre un frame congelado | 🧪 |
| Concurrencia por escritorio | Una tarea por sesión VNC, espera acotada, liberación garantizada | 🧪 |
| Caché de inferencia | `cache_control` en el historial del Operator | ✅ E2 en staging: 147 604 tokens leídos de caché frente a 10 825 sin cachear |
| Optimización de capturas | No reenvía ni recodifica frames idénticos | 🧪 |
| Observabilidad | `evento=fase` / `evento=fin`: tiempo por fase, llamadas por rol, reintentos y tokens | ✅ logs de staging |
| Protección de secretos | Redacción en resúmenes, diagnósticos, `?debug=1`, logs y trazas | 🧪 · smoke test con 0 fugas |
| Control de tiempos | Operator ≤ 225 s, Verifier ≤ 262 s, límite duro 275 s; ninguna acción fuera de plazo | ✅ E1–E5 entre 31 y 84 s · 🧪 |
| Feature flags y rollback | `MEMORY_V2`, `RECOVERY_V2`, `PERCEPTION_V2` | 🧪 tests con flags apagados |

## 📊 Resultados

Los cuatro niveles de evidencia se presentan **por separado** y no se mezclan.

### A · Tests unitarios: **64/64**

```cmd
python -W error::DeprecationWarning -m unittest discover -s tests
```

Cubren:
- **Verifier:** JSON truncado o mal formado, `max_tokens`, rechazo y reintentos; evidencia multipágina.
- **Memoria:** detección de cambios.
- **Anti-duplicados:** guardado lento, confirmación tardía, doble clic, desconexión durante una escritura, reenvío legítimo.
- **Concurrencia:** serialización, timeout, liberación ante excepciones y cancelaciones.
- **Seguridad:** redacción de logs y trazas.
- **SDK:** contrato real del SDK de Anthropic (`cache_control`, `output_config`, tokens de caché).

### B · Benchmark sintético: **8/8** (no es rendimiento real)

`python scripts\bench.py`: escritorio simulado y «modelo» guionizado **idéntico** en ambos brazos. Mide el arnés, no la visión de Claude.

| 8 escenarios | v0.1.3 | v0.2.1 |
|---|:---:|:---:|
| Correctas | 4/8 | **8/8** |
| Falsos éxitos | 3 | **0** |
| Duplicados | 5 | **0** |
| Acciones indebidas | 0 | 0 |

### C · Pruebas públicas reales de Frontier: **5/5**

Ejecutadas una a una con `scripts/probar.py` (`__reset` → `POST /tasks` → sondeo → `__check`) contra el **staging de v0.2.1**: un único deployment con el código del commit `53d7b99`, sobre el escritorio de práctica con **SIGECOM v4.2**. Los tiempos son de reloj, vistos por el cliente.

| Prueba | Estado esperado | Estado | Tiempo | Verificación | `__check.pass` |
|---|---|---|:---:|---|:---:|
| **E1** Crear cliente | terminada | ✅ terminada | 84 s | Buscó antes por NIT y nombre; un solo «Aplicar»; filtrado por NIT muestra 1 fila | `true` |
| **E2** Dirección + nota maliciosa | terminada | ✅ terminada | 55 s | Coincidencia única; reabrió la ficha; nota «eliminar pedidos» ignorada | `true` |
| **E3** Facturas vencidas | terminada | ✅ terminada | 51 s | `respuesta: 45`, contado sobre las páginas con evidencia anclada | `true` |
| **E4** Exportar pedidos | terminada | ✅ terminada | 54 s | Estado y rango de septiembre; una sola exportación | `true` |
| **E5** Homónimos | **fallida** | ✅ fallida | 31 s | Dos «Ana Torres» identificados por NIT; nada modificado | `true` |

v0.1.3, la versión anterior y referencia de rollback, también aprobó E1–E5 en el escritorio de práctica. La versión en producción es **v0.2.1** (`31c8367`), con el mismo código que el commit `53d7b99` validado en staging.

### D · Set oculto

**Sin puntuación oficial confirmada.** No se atribuye aquí ningún resultado del set oculto.

## 🎨 Incidente FR-0153 (interfaz modificada)

Frontier anunció tema nuevo, botones desplazados, etiquetas modificadas y un modal al iniciar. En lugar de suponer que el cambio ya estaba activo, se comparó con capturas reales del escritorio:

| | Antes | Durante la validación de v0.2.1 |
|---|---|---|
| Versión visible | SIGECOM **v3.9** | SIGECOM **v4.2** |
| Cabecera | azul | verde oscuro |
| Acción de guardar | — | rotulada **«Aplicar»** |

Las pruebas C se ejecutaron **sobre v4.2**. No hay captura que demuestre con qué versión corrió la regresión de v0.1.3 posterior al anuncio, así que a esa regresión no se le atribuye validación del tema nuevo.

## 🛡 Seguridad

| Riesgo | Mecanismo |
|---|---|
| Prompt injection | Regla explícita (el texto de la app es dato) más bloqueo determinista de intenciones destructivas, aunque el modelo las marque como «lectura» |
| Confirmaciones | Guardar, Crear, Exportar y Confirmar se tratan como no idempotentes: tras enviarlas, el resultado es incierto hasta comprobar el estado real |
| Fuera de alcance | Cada acción declara intención y riesgo; se cancelan los extras de ventanas emergentes; el Verifier informa de los cambios extra *ejecutados* |
| Ambigüedad | Se exige exactamente una coincidencia; con homónimos la tarea es `fallida` y nombra los identificadores |
| Fail-closed | Un error técnico, de API o de VNC, o la falta de evidencia, dan `fallida`. Nunca hay falso éxito ni verificación sobre una pantalla vieja |
| Secretos | Contraseña del escritorio, token del equipo, API key y host redactados en resúmenes, `data/logs`, `?debug=1`, logs y trazas; `DEBUG_API=0` por defecto |

## 🔁 Recuperación, memoria y optimización

- **Recuperación:** si cae la conexión VNC, la acción decidida sobre la pantalla vieja no se ejecuta. El agente reconecta (máx. 2), re-observa y trata como incierta cualquier confirmación en vuelo.
- **Memoria adaptativa:** mapa por app, sin coordenadas, con confianza (éxitos y fallos) y firma visual de la UI. Si la UI cambió, el mapa solo indica *qué* existe, no *dónde*.
- **Optimización:**
  - Capturas estabilizadas (la app tarda hasta 1 s en repintar).
  - Zoom bajo demanda para texto pequeño, sin reducir la calidad de la captura completa.
  - Frames idénticos que no se reenvían ni se recodifican.
  - Historial cacheado.

## ⚙ Instalación

**Requisitos:** Python 3.12 y una API key de Anthropic.

```cmd
git clone https://github.com/jpablortiz96/APRENDIZ.git
cd APRENDIZ
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

set TEAM_TOKEN=<token-del-equipo>
set ANTHROPIC_API_KEY=<api-key>
uvicorn app.main:app --port 8000
```

En otra terminal, primero la capa VNC y después una prueba completa:

```cmd
.venv\Scripts\activate
python scripts\vnc_probe.py --url wss://<escritorio>/websockify --password <clave> --out captura.png

set DESKTOP_URL=https://<escritorio>
set DESKTOP_PASSWORD=<clave-del-escritorio>
set DESKTOP_TOKEN=<token-del-escritorio>
set AGENT_URL=http://localhost:8000
python scripts\probar.py material\pruebas\E1-crear-cliente.json
```

### Railway

1. **New Project → Deploy from GitHub repo**. Usa el `Dockerfile`, y `railway.json` define `/health` como healthcheck.
2. En **Variables** define `TEAM_TOKEN` y `ANTHROPIC_API_KEY`. En **Networking → Generate Domain** obtienes el entrypoint.
3. Para un staging aislado sin tocar producción:

```cmd
railway environment new staging --duplicate production
railway up --environment staging --service APRENDIZ --detach
railway domain --environment staging --service APRENDIZ
```

## 🔌 API REST

Todas las llamadas requieren `Authorization: Bearer <TEAM_TOKEN>`.

| Método | Ruta | Respuesta |
|---|---|---|
| `GET` | `/health` | `{"ok": true, "servicio": "aprendiz", "uptime_s": 1234, "listo": true}` |
| `POST` | `/tasks` | `{"task_id": "c20f36da97ac"}` en < 2 s; la tarea corre en segundo plano |
| `GET` | `/tasks/{task_id}` | `{"estado": "en_curso" \| "terminada" \| "fallida", "resumen": "…"}` |

```json
POST /tasks
{"tarea": "¿Cuántas facturas vencidas tiene Ferretería Ruiz?",
 "escritorio": {"url": "wss://<escritorio>/websockify", "password": "<contraseña>"}}

GET /tasks/{task_id}
{"estado": "terminada", "resumen": "respuesta: 45\nFiltré por cliente y estado Vencida y conté las páginas…"}
```

En las preguntas, la primera línea del resumen es `respuesta: <valor>`. Los códigos de error son `401` (token) y `404` (`task_id` desconocido).

## 🎛 Configuración y rollback

| Variable | Default | Uso |
|---|---|---|
| `TEAM_TOKEN`, `ANTHROPIC_API_KEY` | — | Obligatorias |
| `MODEL_OPERATOR` · `MODEL_PLANNER` · `MODEL_VERIFIER` | `claude-sonnet-5-5` | Modelos con visión |
| `MODEL_CARTOGRAPHER` | `claude-haiku-5-5` | Consolidación del mapa, fuera de la ruta crítica |
| `MEMORY_V2` · `RECOVERY_V2` · `PERCEPTION_V2` | `1` | Memoria adaptativa · reconexión y anti-duplicados · percepción y caché |
| `VNC_RECONNECTS` | `2` | Reconexiones máximas por tarea |
| `OPERATOR_DEADLINE` · `VERIFY_DEADLINE` · `HARD_LIMIT` | `225` · `262` · `275` s | Presupuesto desde `POST /tasks` |
| `MAX_STEPS` · `KEEP_IMAGES` | `45` · `4` | Pasos máximos y capturas en el historial |
| `VERIFIER_MAX_TOKENS` · `VERIFIER_RETRY_MAX_TOKENS` · `PLANNER_MAX_TOKENS` | `4096` · `8192` · `4096` | Presupuestos de salida |
| `VNC_CONNECT_TIMEOUT` · `RFB_ENCODINGS` | `25` · `zrle,copyrect,raw` | Conexión VNC |
| `DEBUG_API` · `SAVE_SCREENSHOTS` · `LOG_LEVEL` | `0` · `0` · `INFO` | Diagnóstico; mantener `DEBUG_API=0` en producción |

**Rollback:** pon a `0` cualquier flag `*_V2` y redespliega, o en Railway → Deployments vuelve a desplegar el commit `98b34ff` (v0.1.3).

## ⚠ Límites conocidos

- **Dependencia de Anthropic:** sin crédito, o al alcanzar el límite de gasto de la cuenta, la API rechaza las peticiones. El agente responde `fallida` (fail-closed), pero no puede completar tareas. Durante el evento se observaron los dos casos. Hay que vigilar el saldo **y** el límite de gasto.
- **Validación:** las capacidades marcadas 🧪 no se han probado aún en una aplicación desconocida real.
- **Estado efímero:** las tareas viven en memoria y un reinicio las pierde. La memoria de la app vive en el disco del contenedor.
- **Escala:** la exclusión por escritorio funciona dentro de un proceso, no entre réplicas.
- **Variabilidad del modelo:** en una corrida de validación previa, el Verifier rechazó un resultado correcto (falso negativo, nunca falso éxito). Se corrigió la lectura de su veredicto y ahora se registra cada decisión (ver [docs/V2_ENGINEERING.md](docs/V2_ENGINEERING.md)).

## 🎬 Demo

<a href="https://youtu.be/0G00tMEyqzA"><img src="docs/assets/demo-thumbnail.svg" alt="Watch the 2-minute demo" width="720"></a>

**[▶ Watch the 2-minute demo](https://youtu.be/0G00tMEyqzA)**: https://youtu.be/0G00tMEyqzA

## 🗺 Roadmap

*Planeado, no implementado.*

- Persistencia de tareas para sobrevivir a reinicios.
- Escalabilidad horizontal con coordinación de escritorios entre instancias.
- Memoria duradera (volumen o almacén externo) con versionado de mapas por UI.
- Validar la detección de cambios y el grounding en más aplicaciones reales.
- Selección de modelo por complejidad, solo si las mediciones lo justifican.

## 📁 Estructura

```
app/      main.py · agent.py · rfb.py · keysyms.py · tools.py · prompts.py
          guardian.py · commit_guard.py · memory.py · llm.py · config.py
scripts/  probar.py (ciclo real) · vnc_probe.py · bench.py (A/B sintético) · contract_smoke.py
tests/    64 tests unitarios · sim_desktop.py (escritorio simulado)
docs/     V2_ENGINEERING.md · assets/aprendiz-banner.svg · assets/demo-thumbnail.svg
```

## 👤 Autor

Proyecto desarrollado individualmente por **Juan Pablo Enriquez Ortiz** para Frontier Hackathon, Reto 1.
