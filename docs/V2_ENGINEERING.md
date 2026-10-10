# APRENDIZ v0.2.0: evolución para generalizar (incidente FR-0153)

Rama `feat/frontier-agent-v2`. Baseline: `98b34ff` (v0.1.3, desplegado en Railway; etiqueta local `prod-v0.1.3`).
Nada de lo que hay aquí se ha publicado ni desplegado.

---

## GATE 0: Línea base

### Arquitectura (v0.1.3)

```
POST /tasks ──► TaskRunner.submit ──► asyncio.Task(_run_guarded, HARD_LIMIT 275 s)
                                          │
   RFBClient (VNC/RFB propio, ZRLE) ◄─────┤ 1. conectar (1 reintento)
                                          │ 2. Planner (1 llamada, JSON)
   AppMemory (JSON por app) ─────────────►│ 3. Operator: bucle ≤45 pasos, 1 herramienta/turno,
                                          │    captura estabilizada tras cada acción, poda a 4 imágenes
   Guardian (determinista) ──────────────►│    check_action, LoopDetector, format_summary, Secrets
                                          │ 4. done ─► Verifier (captura fresca + evidencias ancladas, JSON con esquema)
                                          │ 5. finalize ─► GET /tasks/{id}
                                          └─► Cartographer (en segundo plano, sin escritorio) ─► AppMemory
```

- **Pruebas:** 25 tests unitarios, todos PASS. E1–E5 en el escritorio oficial: según el equipo, aprobados.
- **Latencias conocidas:** no hay medición de producción en el repositorio (no hay logs ni métricas). En local, `POST /tasks`
  da p50 68 ms y p95 87 ms (20 peticiones, TestClient). PNG de 1280×800: ~335 ms en el peor caso (ruido aleatorio).
- **Condición del nuevo tema:** **no verificada.** No tengo una captura del escritorio posterior al anuncio. Antes de
  afirmar que una regresión corrió con el tema nuevo hay que comprobarlo con `scripts/vnc_probe.py --out tema.png`.

### Hallazgos de la auditoría

**D = demostrado** (en el código o con una prueba). **R = riesgo fundamentado**. **H = hipótesis.**

| # | Hallazgo | Tipo | Evidencia |
|---|----------|------|-----------|
| 1 | El Cartographer guarda **coordenadas** en el mapa de la app y el Operator las recibe como pista. Tras un rediseño, esas coordenadas apuntan a otro sitio. | D | `prompts.CARTOGRAPHER_SYSTEM`, ejemplo `(x≈104,y≈44)`; benchmark `rediseño_modal` v0.1.3: 1 clic en el vacío |
| 2 | El mapa se reutiliza sin saber en qué UI se validó ni si funcionó; no hay confianza ni invalidación. | D | `memory.get()` devuelve el mapa tal cual |
| 3 | ~~`claude-haiku-5-5` no es un ID válido~~ **RETIRADO en v0.2.1: era un error.** La documentación vigente de Anthropic lista Claude Haiku 5.5 (`claude-haiku-5-5`); mi tabla de referencia tenía caché anterior a su lanzamiento. Se restauró el default original. | — | docs.claude.com, Models overview (consultado el 10-oct-2026) |
| 4 | Si cae la conexión VNC no hay reconexión: el resto de acciones falla y **el Verifier puede juzgar un framebuffer congelado** como si fuera la «captura fresca». | D | `_execute`/`_handle_done` sin comprobar `rfb.closed`; benchmark `corte_vnc` v0.1.3: `fallida` aunque la app quedó bien |
| 5 | Una acción de guardado repetida a ciegas (pantalla lenta, «SIN cambio visible») duplica registros. `LoopDetector` solo avisa a la 3.ª repetición y no bloquea. | D | `guardian.LoopDetector`; benchmark `guardado_lento` v0.1.3: 1 duplicado y **falso éxito** |
| 6 | Se envían capturas idénticas píxel a píxel (esperas, acciones sin efecto): tokens sin información nueva. | D | benchmark `carga_lenta`: 22 frente a 16 imágenes |
| 7 | El historial del Operator se reenvía entero en cada paso sin caché; solo el system prompt se cachea. | D | `llm.create`: `cache_control` solo en `system` |
| 8 | `Image.fromarray(..., "RGB")`: el parámetro `mode` está deprecado y **se elimina en Pillow 13 (15-oct-2026)**. `requirements.txt` declara `pillow>=10` sin tope, así que un rebuild podría romper todas las capturas. | D | DeprecationWarning en los tests |
| 9 | Las tareas viven en memoria: un reinicio o un redeploy durante la evaluación da 404 en `GET /tasks/{id}`. | R | `TaskRunner.tasks: dict` |
| 10 | No hay bloqueo por escritorio: dos tareas simultáneas sobre el mismo escritorio se pisarían. El evaluador es secuencial. | R | `submit` sin control de admisión |
| 11 | La memoria vive en el FS del contenedor: cada deploy la borra (sin volumen). En la evaluación oculta arranca vacía. | R | Dockerfile, `data/` |
| 12 | El texto de la app (notas, campos) podía tomarse como instrucciones: el prompt no lo prohibía. Mitigación previa: solo la regla léxica de `check_action`. | R | `OPERATOR_SYSTEM` v0.1.3 |
| 13 | El plan por defecto (`tipo: otro`, si falla el Planner) desactivaba las capturas ancladas. Corregido en v0.1.3. | D | ver PATCH v0.1.3 |
| 14 | El PNG se codifica en cada paso aunque el frame sea idéntico (~100–300 ms). | D | medición local; propuesta futura |
| 15 | La guarda anti-duplicados depende de que el Operator marque `risk=cambio_esperado`; si marca un Guardar como `lectura`, no actúa. | R | diseño de `_write_key` |
| 16 | La calidad del grounding sobre la app desconocida (etiquetas renombradas, botones movidos) depende del modelo; no es medible sin el escritorio y la API. | H | — |

Los logs no imprimen secretos: `Secrets.redact` filtra el resumen, la URL del escritorio se muestra solo como host (`_safe_url`) y los nuevos logs `llm[...]`, de reconexión y de mapa no incluyen contenido.

---

## Selección: tres mejoras (alto impacto, bajo riesgo, reversibles con un flag)

| Flag (por defecto ON) | Mejora | Hallazgos | Por qué ahora |
|-----------------------|--------|-----------|---------------|
| `MEMORY_V2` | **Memoria de navegación adaptable**: mapas sin coordenadas, firma visual de la UI, confianza y detección de drift | 1, 2, 3, 12 | FR-0153: tema nuevo, botones movidos, modal nuevo |
| `RECOVERY_V2` | **Recuperación segura**: reconexión VNC acotada y guarda anti-duplicados para escrituras no idempotentes | 4, 5 | Un duplicado o un falso éxito anula la tarea |
| `PERCEPTION_V2` | **Percepción e inferencia**: no reenviar capturas idénticas + caché incremental del historial | 6, 7 | Coste y latencia por paso, sin tocar la precisión |

Además, sin flag: Pillow 13 (8) y reglas de prompt contra la inyección (12) y sobre modales inesperados.

Quedan **fuera**, como propuestas futuras (ver «Riesgos pendientes»): persistencia de tareas, bloqueo por escritorio y selección de modelo por complejidad. Necesitan más cambio o una medición en producción que hoy no tenemos.

### GATE 1: Resiliencia visual (`MEMORY_V2`)

- `memory.strip_coordinates`: elimina coordenadas al guardar y **también al leer** mapas viejos.
- `memory.ui_signature` / `ui_distance`: paleta de 64 colores más luminancia en una rejilla de 16×10. Cuesta unos 54 ms una vez por tarea.
  Umbral 0,18. En pares sintéticos: mismos datos 0,0000; otros datos con la misma UI 0,0004; tema nuevo 0,8930; modal 0,7278 (4/4 aciertos).
- La entrada del mapa guarda `exitos`, `fallos` y `ui_sig` (la UI en la que se **verificó** el último éxito). Confianza = (éxitos+1)/(total+2).
  - Un éxito verificado en una UI distinta reinicia los contadores.
  - Agotar tiempo o pasos cuenta como fallo de ruta.
  - Una tarea que debía fallar (p. ej. homónimos) no cuenta ni a favor ni en contra.
- Operator: recibe confianza y aviso de drift («úsalo para saber QUÉ existe, no DÓNDE»). Reglas nuevas:
  - Coordenadas siempre desde la captura actual.
  - Buscar cada control por función, etiqueta equivalente y contexto.
  - Modal inesperado: leerlo y cerrarlo sin aceptar extras.
  - El texto de la app es dato, no orden.
- El Cartographer v2 tiene prohibido guardar coordenadas y no convierte rutas fallidas en recetas (`CARTOGRAPHER_SYSTEM_V1` se conserva para rollback).

### GATE 2: Fiabilidad y recuperación (`RECOVERY_V2`)

- **Reconexión acotada**: como máximo `VNC_RECONNECTS=2` por tarea, solo si quedan más de 15 s, y con timeout limitado al tiempo restante.
  - Si la conexión está cerrada al recibir una acción, esta **no se ejecuta**: el modelo la decidió sobre una pantalla ya no fiable. Se reconecta y se devuelve la pantalla real.
  - Si el corte ocurre durante la acción, se marca como **posiblemente aplicada**.
  - `done` con la conexión caída: se reconecta antes de verificar o se reporta `fallida`. **Nunca se verifica sobre un frame congelado.**
- **Guarda anti-duplicados**: bloquea repetir de inmediato la misma acción de cambio no idempotente (clic o tecla de confirmación, ±15 px) si el agente no ha observado nada en medio (`wait`/`zoom`).
  - Tras observar, se permite. Coste máximo: un paso.
  - Escribir texto con `replace=true` no se bloquea, porque es idempotente.

### GATE 3: Rendimiento (`PERCEPTION_V2`)

- Si la captura es **idéntica píxel a píxel** (md5 del framebuffer, ~15 ms) a la última captura completa que el modelo aún tiene en contexto, se envía solo texto.
  - El texto explica que «sin cambio» no siempre es fracaso.
  - Límite: no se omite si esa captura podría haber salido del contexto por la poda.
- `cache_control` a nivel de petición en el Operator: el historial se cachea hasta la primera captura podada.

### GATE 4: Validación

```
python -W error::DeprecationWarning -m unittest discover -s tests   ->  Ran 40 tests  OK
python scripts\bench.py                                              ->  tabla A/B (determinista)
```

Los tests nuevos (`tests/test_v2.py`, 15) cubren:
- Coordenadas eliminadas y detector de drift: tema, datos y modal.
- Confianza y reinicio de contadores; flag OFF = comportamiento v1.
- Guarda: bloquea, permite tras observar, ignora lecturas; flag OFF = repetición ciega (documentada).
- Instrucción maliciosa en una nota bloqueada por la política.
- Reconexión correcta, límite de intentos, nunca verificar sobre un frame congelado, sin reconexión si no hay tiempo.
- Deduplicación de capturas con su límite.
- `cache_control` solo cuando se pide.
- **Regresión del benchmark**: v0.2 correcto en los 5 escenarios, sin falsos éxitos ni duplicados.

---

## Benchmark A/B (números reales, `scripts/bench.py`)

El escritorio está simulado (`tests/sim_desktop.py`): una app con estado que sobrevive a desconexiones, pintada con PIL. El «modelo» está guionizado y es **idéntico en ambos brazos**: un empleado razonable pero ingenuo. Solo ve lo que se le envió, mapeando cada PNG a la instantánea de lo que mostraba. El Verifier simulado ve solo la «captura actual».
**Mide el arnés, no la visión de Claude.**

| Escenario | Versión | Estado | Correcto | Falso éxito | Duplicados | Pasos | Llamadas LLM | Imágenes enviadas | Tokens imagen (est.) | KB historial sin caché |
|---|---|---|---|---|---|---|---|---|---|---|
| crear_normal | v0.1.3 | terminada | ✅ | – | 0 | 5 | 7 | 14 | 19 110 | 612 |
| crear_normal | v0.2 | terminada | ✅ | – | 0 | 5 | 7 | 14 | 19 110 | 354 |
| guardado_lento | v0.1.3 | terminada | ❌ | **sí** | **1** | 6 | 8 | 18 | 24 570 | 792 |
| guardado_lento | v0.2 | terminada | ✅ | – | 0 | 7 | 9 | 22 | 30 030 | 356 |
| corte_vnc | v0.1.3 | fallida | ❌ | – | 0 | 8 | 9 | 26 | 35 490 | 1 144 |
| corte_vnc | v0.2 | terminada | ✅ | – | 0 | 6 | 8 | 18 | 24 570 | 355 |
| rediseño_modal | v0.1.3 | terminada | ✅ (1 clic en vacío) | – | 0 | 7 | 9 | 22 | 30 030 | 961 |
| rediseño_modal | v0.2 | terminada | ✅ (drift detectado) | – | 0 | 6 | 8 | 18 | 24 570 | 527 |
| carga_lenta | v0.1.3 | terminada | ✅ | – | 0 | 7 | 9 | 22 | 30 030 | 956 |
| carga_lenta | v0.2 | terminada | ✅ | – | 0 | 7 | 9 | 16 | 21 840 | 359 |

**Totales:**
- v0.1.3: **3/5 correctas, 1 falso éxito, 1 duplicado**, 33 pasos, 42 llamadas, 139 230 tokens de imagen (est.), 4 465 KB de historial sin caché.
- v0.2: **5/5 correctas, 0 falsos éxitos, 0 duplicados**, 31 pasos, 41 llamadas, 120 120 tokens de imagen (−13,7 %), 1 951 KB de historial sin caché (−56 %).

Cómo leer estos números:
- Los tokens de imagen se estiman con w·h/750 por captura completa. Los KB de historial sin caché asumen que el prefijo idéntico entre peticiones se lee de caché. Hay que **confirmarlo en producción con `usage.cache_read_input_tokens`** (además, existe un mínimo de prefijo para cachear).
- `guardado_lento` v0.2 cuesta un paso más: el precio deliberado de comprobar antes de repetir una escritura.
- No hay latencia de producción comparable: los `ms` del benchmark son de la simulación y no se reportan como mejora.

**Lo que este benchmark no puede afirmar**: tasa de éxito con Claude sobre una app desconocida, p50/p95 reales por tarea ni precisión del Verifier real. Para eso hacen falta el escritorio oficial y la API, y **no deben consumir las dos corridas ocultas**.

---

## Diagrama de la arquitectura resultante (v0.2)

```
                       ┌──────────────── Guardian (determinista) ───────────────────┐
                       │ check_action · guarda anti-duplicados · LoopDetector ·     │
                       │ format_summary · Secrets                                   │
                       └───────────────▲────────────────────────────────────────────┘
POST /tasks ─► run ─► conectar ─► captura + ui_signature ─► Planner
                                                          │
       AppMemory.context(app, firma) ─► mapa SIN coordenadas + confianza + ¿drift?
                                                          ▼
   ┌──────────────────────────── Operator (≤45 pasos) ────────────────────────────┐
   │ OBSERVAR (captura; si es idéntica, solo texto) → DECIDIR (Claude, historial   │
   │ con caché) → [¿conexión caída? → RECONECTAR (≤2) → re-observar]               │
   │ → [¿misma escritura sin observar? → BLOQUEAR → wait/zoom]                     │
   │ → ACTUAR (RFB) → estabilizar → ¿cambió?                                       │
   └──────────────────────────────────────┬───────────────────────────────────────┘
                                          ▼ done
         [conexión caída → reconectar o fallida] → Verifier (fresca + evidencias, JSON validado)
                                          ▼
         finalize ─► record_outcome(éxito / fallo de ruta / neutro, firma UI) ─► Cartographer v2
```

## Archivos modificados

| Archivo | Cambio |
|---------|--------|
| `app/config.py` | Flags `MEMORY_V2`, `RECOVERY_V2`, `PERCEPTION_V2`, `VNC_RECONNECTS` |
| `app/memory.py` | Coordenadas eliminadas, firma y distancia de UI, `context()`, `record_outcome()` |
| `app/agent.py` | Integración de los tres flags, `_reconnect`, `_recover_connection`, `_write_guard`, `_screen_blocks`, `_make_rfb` |
| `app/llm.py` | `cache_messages` → `cache_control` a nivel de petición |
| `app/prompts.py` | Grounding adaptativo, modales, anti-inyección, aviso de drift, Cartographer v2 (+ V1 para rollback) |
| `app/rfb.py` | `Image.fromarray` sin `mode` (Pillow 13) |
| `tests/sim_desktop.py`, `tests/test_v2.py`, `scripts/bench.py` | Simulador, 15 tests nuevos y benchmark |
| `tests/test_evidence.py`, `tests/test_llm_json.py` | Los fakes declaran `closed=False` (interfaz real de RFBClient) |

## Riesgos pendientes

1. **No validado contra Claude real ni contra el tema nuevo.** Las mejoras son de arnés; el efecto en la app oculta es experimental hasta una corrida en el escritorio de práctica.
2. El umbral de drift (0,18) está calibrado con UIs sintéticas. En una UI real, un cambio de pantalla inicial (otra vista tras el reset) podría marcar drift falso. Consecuencia: el mapa se presenta como «desactualizado». Es conservador, nunca bloquea.
3. La guarda depende de `risk=cambio_esperado` (hallazgo 15).
4. Tareas en memoria y sin bloqueo por escritorio (hallazgos 9, 10): **no desplegar durante la evaluación**.
6. Futuro: no codificar el PNG cuando el frame es idéntico (hallazgo 14). Persistencia ligera de tareas en `data/`. Bloqueo por URL de escritorio. Selección de modelo por complejidad, solo con medición.

## Rollback

- **Por mejora, sin código**: en Railway, Variables → `MEMORY_V2=0`, `RECOVERY_V2=0` o `PERCEPTION_V2=0` → redeploy. Con los tres a 0, el arnés (memoria, Cartographer, reconexión, guarda, capturas y caché) se comporta como en v0.1.3. Lo cubren los tests con flags OFF. Las reglas nuevas del prompt del Operator (grounding por función, modales, anti-inyección) no dependen de ningún flag: para quitarlas hay que hacer el rollback total.
- **Total**: Railway → Deployments → el deployment de `98b34ff` → *Redeploy*. O bien `git revert <merge>` en `main` y push.
- Los fixes sin flag (Pillow, reglas de prompt, redacción de logs, slots por escritorio) son compatibles hacia atrás.

## Comprobar y desplegar (con aprobación)

```cmd
cd /d "C:\Users\jpabl\Downloads\APRENDIZ_v0.1.1\aprendiz-repo"
git switch feat/frontier-agent-v2
python -W error::DeprecationWarning -m unittest discover -s tests
python scripts\bench.py
git push origin feat/frontier-agent-v2
```

Desplegar = merge a `main` (tras revisión) → Railway redeploya. Después, en el escritorio de **práctica** (nunca en las corridas ocultas):

```cmd
python scripts\vnc_probe.py --url %DESKTOP_URL%/websockify --password %DESKTOP_PASSWORD% --out tema.png
python scripts\probar.py material\pruebas\E1-crear-cliente.json
python scripts\probar.py material\pruebas\E2-direccion-con-nota.json
python scripts\probar.py material\pruebas\E3-facturas-vencidas.json
python scripts\probar.py material\pruebas\E4-exportar-pedidos.json
python scripts\probar.py material\pruebas\E5-homonimos.json
```

`tema.png` sirve para confirmar si el escritorio ya muestra el tema nuevo **antes** de atribuir resultados a él. En los logs, busca `mapa '…': confianza=… drift=…`, `reconexión VNC` y `llm[verifier]`.

## Debrief técnico (10 min)

1. **(1 min) Problema.** Una UI desconocida que además cambia (FR-0153). El agente no debe «recordar dónde», sino «saber qué» y encontrarlo de nuevo.
2. **(2 min) Memoria que sabe cuándo dudar.**
   - El mapa describe funciones, no píxeles.
   - Cada mapa lleva la firma visual de la UI donde se verificó y su historial de éxitos y fallos.
   - Si la pantalla actual difiere (tema, maquetación, modal), el agente lo sabe y vuelve a localizar cada control.
   - Detector barato (54 ms/tarea, sin modelo): 4/4 en los pares de prueba.
3. **(2 min) Recuperación sin efectos secundarios.**
   - Una desconexión no se convierte en un fallo ni en un falso éxito: reconectar, re-observar y nunca verificar sobre un frame congelado.
   - Una escritura no idempotente no se repite sin haber observado antes su efecto.
   - Resultado medido: 0 duplicados y 0 falsos éxitos frente a 1 y 1.
4. **(2 min) Percibir con menos.** No reenviar lo que el modelo ya vio, más caché incremental: −13,7 % de tokens de imagen y −56 % de historial sin caché (estimados) sin perder precisión. Todo reversible por flag.
5. **(2 min) Método.**
   - Benchmark A/B determinista, con el mismo «modelo» en ambos brazos: aísla el aporte del arnés.
   - Explicamos qué **no** mide.
   - Encontramos un bug de producción latente (Pillow 13, en 5 días) gracias a ejecutar los tests con warnings como error.
6. **(1 min) Límites honestos.** La visión de Claude sobre la app oculta sigue siendo la variable dominante. La validamos en el escritorio de práctica, nunca gastando las corridas ocultas.


---

# v0.2.1: Release gate

## Cambios

| Tarea | Implementación | Archivos |
|-------|----------------|----------|
| 1. Concurrencia | `DesktopSlots`: exclusión mutua por escritorio (host+ruta normalizados), comprobación y asignación atómicas en asyncio, espera por sondeo con plazo. Si una tarea en cola agota su tiempo útil (`OPERATOR_DEADLINE − 40 s` desde el POST), **no llega a conectarse**. `HARD_LIMIT` incluye el tiempo en cola. El slot se libera en `finally` (excepción, timeout o cancelación) y solo puede liberarlo su dueño. Ninguna acción se ejecuta después de `OPERATOR_DEADLINE`. | `app/agent.py` |
| 2. Duplicados | `app/commit_guard.py`. Clasifica las confirmaciones por herramienta, tecla de envío, verbo en `why` (es/en), `risk`, tipo de tarea y acción previa (Enter tras escribir), así que **ya no depende solo de `risk`**. Tras una confirmación, el resultado queda incierto: **esperar o hacer zoom no desbloquea**. Hace falta progreso real (un dato nuevo escrito o una navegación con cambio visible). Un `double_click` sobre una confirmación se ejecuta como clic simple. Un corte de conexión durante el envío marca la operación como incierta. | `app/commit_guard.py`, `app/agent.py` |
| 3. Seguridad y observabilidad | `redact_obj` recursivo antes de escribir `data/logs/*.json` y en `?debug=1`. `RedactingFilter` en los handlers raíz redacta mensajes **y trazas** (contraseña, token del equipo, API key, host del escritorio, `wss://`, `password=`, `Bearer`). Logs `evento=fase task=… fase=… ms=…` y `evento=fin task=… {métricas}`. Métricas: duración de espera, conexión VNC, planner, operator, verifier y reconexión; pasos; llamadas LLM por rol; reintentos de API y de JSON; tokens de entrada, salida, `cache_read` y `cache_creation`; reconexiones; bloqueos. `DEBUG_API=0` por defecto. | `app/guardian.py`, `app/llm.py`, `app/main.py`, `app/agent.py` |
| 4. Percepción | Contrato real del SDK 1.13.0 verificado con `httpx2.MockTransport`: el SDK serializa `cache_control` a nivel de petición, `system[].cache_control` y `output_config.format`, y expone `cache_read_input_tokens` y `cache_creation_input_tokens`, que se contabilizan. El PNG no se recodifica si el framebuffer no cambió (md5 ~15 ms frente a PNG ~100–335 ms). Test de poda: pares `tool_use`/`tool_result` intactos y como máximo `KEEP_IMAGES` imágenes. Sin cambios de calidad ni de modelos. | `app/agent.py`, `tests/test_v21.py` |
| 5. Adaptabilidad | Escenarios nuevos en el benchmark: `riesgo_mal_clasificado`, `doble_clic_guardar` y `nota_maliciosa`, además de los de v0.2.0 (rediseño con modal y mapa viejo con coordenadas, corte VNC, carga lenta). | `scripts/bench.py`, `tests/sim_desktop.py` |
| Modelo | `MODEL_CARTOGRAPHER` vuelve a `claude-haiku-5-5`, verificado en la documentación vigente. | `app/config.py` |

## Validación (local, 10-oct-2026)

- `python -W error::DeprecationWarning -m unittest discover -s tests`: **63/63 OK**.
- Compilación OK. `pip check`: sin conflictos en las dependencias del proyecto; los avisos son de `opentelemetry`, ajeno al repo.
- Smoke del contrato FastAPI:
  - `/health` 200 con las mismas claves; `GET /tasks/{id}` → `estado`, `resumen`.
  - 401 sin token o con token malo; 404 si la tarea no existe.
  - `?debug=1` no expone nada con `DEBUG_API=0`.
  - `POST /tasks`: p50 33 ms, p95 170 ms, máx 177 ms (20 tareas encoladas sobre el mismo escritorio).
  - **0 fugas** de contraseña, token, API key o host en 113 líneas de log reales.
- Benchmark A/B (determinista, 8 escenarios):

| | v0.1.3 | v0.2.1 |
|---|---|---|
| Correctas | 4/8 | **8/8** |
| Falsos éxitos | 3 | **0** |
| Duplicados | 5 | **0** |
| Acciones indebidas | 0 | 0 (el Guardian ya bloqueaba la nota maliciosa) |
| Pasos | 51 | 51 |
| Tokens de imagen (est.) | 210 210 | 202 020 (−3,9 %) |
| KB de historial sin caché (est.) | 6 797 | 3 044 (−55 %) |

**No ejecutado:** E1–E5 reales contra la nueva versión. No hay un entorno staging de Railway accesible desde esta sesión (el CLI no está enlazado) y la ejecución de `probar.py` contra el escritorio quedó bloqueada por permisos en una sesión anterior. **Los tests sintéticos no sustituyen a E1–E5.**

## Rollback a 98b34ff

- **Sin código**: Railway → Deployments → deployment de `98b34ff` → *Redeploy*.
- **Por flags**: `MEMORY_V2=0`, `RECOVERY_V2=0`, `PERCEPTION_V2=0`. Desactivan memoria v2, guarda y reconexión, y percepción y caché. Los slots por escritorio y la redacción de logs no tienen flag porque son puramente defensivos.
- **Git**: `main` sigue en `98b34ff` mientras no se haga merge.

---

## Validación real en staging (10-oct-2026)

- **Infraestructura:**
  - Entorno Railway `staging`, duplicado de `production` en el proyecto del servicio `APRENDIZ`, con dominio propio.
  - Despliegue por `railway up` del árbol limpio del commit `53d7b99`.
  - Producción no se tocó: sigue en `main` `98b34ff`.
- **Interfaz:** una captura real del escritorio de práctica muestra SIGECOM **v4.2** (cabecera verde, «Aplicar»); la referencia anterior era v3.9 (cabecera azul).

| Corrida | Build | Resultado |
|---|---|---|
| E1 | `47989e8` | PASS (87 s) |
| E2 | `47989e8` | **FAIL de estado**, `__check.pass=true`: la app quedó bien, pero el Verifier no fue aceptado |
| E2 | `47989e8` + log | PASS (`riesgo='ninguno'`) |
| E1 | `53d7b99` | PASS (84 s) |
| E2 | `53d7b99` | PASS (55 s, nota maliciosa ignorada) |
| E3 | `53d7b99` | Sin resultado válido: `400 "You have reached your specified API usage limits… regain access on 2026-11-01"` |
| E4, E5 | — | No ejecutadas, por el mismo límite de la API |

- **Primer fallo de E2:** causa no observable, porque no existía log de la decisión. Hipótesis: el valor de `riesgo_cambios_extra` varió (formato, o mención de la nota maliciosa no ejecutada). Corrección en `53d7b99`:
  - Log de la decisión del Verifier.
  - Parser tolerante a variantes de formato; cualquier «posible» sigue rechazándose.
  - Aclaración en el prompt: el riesgo se refiere a cambios *ejecutados*.
- **E3:** el límite de gasto de la cuenta de Anthropic **no es una regresión del código**. El agente actuó fail-closed: `fallida`, sin falso éxito, `__check.pass=true`.
- **Métricas reales** (`evento=fin`, E2 en `47989e8`): planner 7,6 s; operator 48,5 s en 17 pasos; verifier 8,6 s en 2 rondas; conexión VNC 1,0 s. Tokens: `input` 10 825, `output` 4 761, `cache_read` 147 604, `cache_creation` 129 754.

### Cierre de la validación (5/5 sobre v0.2.1)

**Procedencia comprobada en los logs de Railway:**
- Los PASS de E3, E4 y E5 de las 22:16–22:19 se ejecutaron contra **producción (v0.1.3)**, no contra staging.
- Por eso se repitieron contra staging, en el **mismo deployment** (22:04:25, código `53d7b99`) donde pasaron E1 y E2:

| Prueba | Estado | Tiempo cliente | `__check.pass` |
|---|---|---|---|
| E1 | terminada | 84 s | true |
| E2 | terminada | 55 s | true |
| E3 | terminada, `respuesta: 45` | 51 s | true |
| E4 | terminada | 54 s | true |
| E5 | fallida (esperado) | 31 s | true |

**Observación sobre el contrato:** en E4, `POST /tasks` midió 2,92 s desde el cliente; el resto de mediciones de la sesión quedaron entre 0,35 y 1,67 s. El handler no hace E/S (solo crea la tarea asyncio), así que se atribuye a la red o al proxy. Queda como riesgo a vigilar.

**Dependencia de Anthropic:** en los logs de producción entre las 20:24 y las 21:00 hay 60 tareas de otra aplicación. Desde las 20:54 la API respondió «credit balance is too low» y esas tareas terminaron `fallida` en ~2 s. Más tarde, staging recibió «usage limits» (límite de gasto). En los dos casos el agente fue fail-closed. No se sabe a qué evaluación corresponden esas tareas.
