# APRENDIZ v0.1.2 — corrección de evidencia para consultas paginadas

## Incidente observado en E3

El Operator navegó las páginas de facturas y obtuvo un recuento, pero el Verifier recibió solo las capturas recientes y no podía revisar las páginas anteriores. La tarea terminó `fallida` aunque `POST /__check` indicaba que no hubo alteraciones de datos. No se considera PASS E3 hasta que se repita el ciclo en el escritorio de evaluación.

## Cambios acotados

- `app/agent.py`: `remember` guarda una captura de pantalla anclada a una nota en tareas de consulta. Como máximo seis capturas previas por tarea. El verificador las recibe etiquetadas, además de la captura actual. No confía únicamente en notas textuales ni permite respuestas sin verificar.
- `app/tools.py`: explica que `remember` conserva evidencia visual en consultas.
- `app/prompts.py`: ordena conservar evidencia de cada página *antes* de pasar a la siguiente y verificar clientes de nombre exacto.
- `tests/test_evidence.py`: pruebas de regresión sin conexiones reales.
- `scripts/probar.py`: corrige un SyntaxWarning del texto de ayuda; no cambia la evaluación.

No hay valores programados de resultados (45, cliente o NIT), lógica específica de una aplicación, ni cambios en credenciales/autenticación/endpoints.

## Despliegue desde CMD de Windows

1. Entra a tu repositorio con `cd /d "C:\Users\jpabl\Downloads\APRENDIZ_v0.1.1\aprendiz-repo"`.
2. Descarga `APRENDIZ_E3_PATCH_v0.1.2.zip` en tu carpeta Descargas.
3. Ejecuta `tar -xf "%USERPROFILE%\Downloads\APRENDIZ_E3_PATCH_v0.1.2.zip" -C .`.
4. Ejecuta `python -m unittest discover -s tests -v`; requiere que ya instalaste `requirements.txt`.
5. Ejecuta `git add app/agent.py app/prompts.py app/tools.py scripts/probar.py tests/test_evidence.py PATCH_E3_v0.1.2.md`.
6. Ejecuta `git commit -m "Preserve paginated visual evidence for verifier"`.
7. Ejecuta `git push origin main`.
8. Comprueba en Railway que el nuevo deployment está ACTIVE y `GET /health` responde listo:true.
9. Desde el mismo CMD con AGENT_URL, TEAM_TOKEN, DESKTOP_URL, DESKTOP_PASSWORD, DESKTOP_TOKEN, repite `python scripts\probar.py material\pruebas\E3-facturas-vencidas.json`.

**Precaución**: no hagas varias tareas a la vez en el mismo escritorio de prueba; `__reset` de una tarea podría interferir con otra. Nunca subas archivos `.env`, contraseñas o tokens al repo.
