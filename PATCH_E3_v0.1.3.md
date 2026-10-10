# APRENDIZ v0.1.3 — salida JSON robusta del Verifier (E3)

## Síntoma

E3: el Operator llega a `done` con un recuento, pero el Verifier termina en `JSONDecodeError`; la tarea queda `fallida` sin línea `respuesta:`.

## Causa raíz (código + documentación de la API; pendiente de confirmar con logs de Railway)

- El Verifier pedía `max_tokens=800`. Con `claude-sonnet-5-5`, omitir `thinking` activa thinking adaptativo y **esos tokens cuentan contra `max_tokens`**.
- En E3 el Verifier recibe hasta 8 capturas y debe contar filas página por página: es justo el caso que más razona. Al agotar los 800 tokens la respuesta llega con `stop_reason=max_tokens` y texto vacío o cortado.
- `call_json` no miraba `stop_reason`: `parse_json("")` → `JSONDecodeError`. El reintento repetía el mismo presupuesto de 800 (y reenviaba el texto cortado), así que volvía a fallar. `_handle_done` convierte esa excepción en «no verificado» (correcto: nunca es un éxito falso).

## Cambios

- `app/llm.py`: `call_json` usa structured outputs (`output_config.format` con JSON Schema) cuando se le da un esquema, comprueba `stop_reason` (`max_tokens`/`refusal`/vacío), valida el esquema en el cliente y hace **un** reintento acotado como petición nueva: el doble de presupuesto si hubo truncado, o un recordatorio de formato si el JSON no era válido. No reintenta si no quedan ≥15 s antes del plazo de verificación. Si el modelo configurado rechaza `output_config`, sigue con JSON por prompt + validación local. Los logs `llm[rol] stop=… out_tokens=… req=…` no incluyen contenido.
- `app/agent.py`: `VERIFIER_SCHEMA` (`verificado` bool, `motivo` str, `riesgo_cambios_extra` str, `respuesta_corregida` str|null); Verifier con 4096/8192 tokens y plazo `VERIFY_DEADLINE`; Planner con 4096 (si falla, el plan por defecto `otro` hace que se pierdan las capturas ancladas). `remember` también ancla capturas con el plan `otro`. Si `respuesta_corregida` viene vacía, ya no borra la respuesta.
- `app/prompts.py`: el Verifier devuelve un JSON compacto y razona el conteo fuera del JSON.
- `app/config.py`: `VERIFIER_MAX_TOKENS`, `VERIFIER_RETRY_MAX_TOKENS` y `PLANNER_MAX_TOKENS` se pueden ajustar por entorno.
- `tests/test_llm_json.py`: 16 pruebas con la API simulada: JSON válido, truncado, mal formado, `max_tokens`, `refusal`, error de API en el reintento, plazo agotado, `output_config` rechazado y el flujo `done → verify → finalize` con evidencia de varias páginas.

No se fijan valores de la app de práctica. Tampoco cambian los contratos `/health` y `/tasks`, ni las reglas de seguridad, cancelación y anti-duplicados.
