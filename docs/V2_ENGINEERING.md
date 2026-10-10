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
| 3 | `MODEL_CARTOGRAPHER` por defecto `claude-haiku-5-5` no es un ID válido, así que el mapa nunca se guardaba (salvo que la variable de entorno lo sobrescriba). | D | `config.py`; lista de modelos vigente |
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

Además, sin flag: Pillow 13 (8), ID del Cartographer (3) y reglas de prompt contra la inyección (12) y sobre modales inesperados.

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
| `app/config.py` | Flags `MEMORY_V2`, `RECOVERY_V2`, `PERCEPTION_V2`, `VNC_RECONNECTS`; ID válido del Cartographer |
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
5. Confirmar en Railway que `MODEL_CARTOGRAPHER` no está fijado a un ID inválido.
6. Futuro: no codificar el PNG cuando el frame es idéntico (hallazgo 14). Persistencia ligera de tareas en `data/`. Bloqueo por URL de escritorio. Selección de modelo por complejidad, solo con medición.

## Rollback

- **Por mejora, sin código**: en Railway, Variables → `MEMORY_V2=0`, `RECOVERY_V2=0` o `PERCEPTION_V2=0` → redeploy. Con los tres a 0, el arnés (memoria, Cartographer, reconexión, guarda, capturas y caché) se comporta como en v0.1.3. Lo cubren los tests con flags OFF. Las reglas nuevas del prompt del Operator (grounding por función, modales, anti-inyección) no dependen de ningún flag: para quitarlas hay que hacer el rollback total.
- **Total**: Railway → Deployments → el deployment de `98b34ff` → *Redeploy*. O bien `git revert <merge>` en `main` y push.
- Los fixes sin flag (Pillow, ID del Cartographer, reglas de prompt) son compatibles hacia atrás.

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
