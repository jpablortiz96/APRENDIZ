# APRENDIZ · Predeploy v0.1.1 (parche mínimo sobre Fable v0.1)

## Cambios focalizados

- `GET /health` da 200 solo si están configurados `TEAM_TOKEN` y `ANTHROPIC_API_KEY`, de lo contrario 503.
- `POST /tasks` y GET de tareas rechazan sin token si `TEAM_TOKEN` no está configurado (antes quedaba abierto).
- Ante una excepción en Verifier o sin tiempo para verificación, ya **no** reporta `terminada` solo por la declaración del Operator.
- Se bloquea el éxito si Verifier detecta posibles cambios extra. Para consultas se utiliza `respuesta_corregida` si el Verifier indica éxito y sí aporta una corrección.
- `DEBUG_API` desactivado por defecto en producción. Para depuración temporal: `DEBUG_API=1`.
- `scripts/contract_smoke.py` comprueba endpoints y autenticación sin hacer cambios reales.
- `tests/test_guardian.py` añade verificaciones sin API externa.
- `scripts/probar.py` compara el **primer número** de una respuesta numérica (evita que «145» apruebe cuando se espera «45»), exige resultado válido de `__check` y resume PASS/FAIL con código de salida.

**Esta versión no se ha probado con VNC real ni con una API key de Anthropic.** La compilación y los tests estáticos no equivalen a una evaluación E1–E5. Es fundamental ejecutar las pruebas oficiales desde tu máquina.

## Checklist Railway

1. Descomprime este ZIP: abre GitHub y sube el **contenido** de la carpeta `aprendiz` a la raíz del repositorio público (debe verse `Dockerfile`, `app/` y `requirements.txt`).
2. Railway: New Project → Deploy from GitHub repo → selecciona tu repositorio → build Dockerfile.
3. Variables: `TEAM_TOKEN` = token COMPLETO DEL EQUIPO, `ANTHROPIC_API_KEY` = clave válida. Opcional: `DEBUG_API=0`, `SAVE_SCREENSHOTS=0`.
4. Settings/Networking → Generate Domain, copia la URL `https://...up.railway.app`.
5. Terminal PowerShell con Python instalado: `python -m pip install httpx` y `python scripts/contract_smoke.py --url https://URL-DE-TU-AGENTE` (usa `TEAM_TOKEN` de PowerShell).
6. Valida **solo lectura de VNC**: `python scripts/vnc_probe.py --url wss://URL-DE-TU-ESCRITORIO/websockify --password TU_PASSWORD --out captura.png`.
7. Configura `DESKTOP_URL`, `DESKTOP_PASSWORD`, `DESKTOP_TOKEN`, `AGENT_URL`, `TEAM_TOKEN` como variables locales (NUNCA en GitHub) y ejecuta `python scripts/probar.py 'material/pruebas/E1-crear-cliente.json' --debug`.
8. Repite E1–E5 con reset individual. Guarda estado, resúmenes, salida de `/__check` y duración, no secretos.

**El token del escritorio SOLO se utiliza para `/__reset` y `/__check`. El token del equipo SOLO autentica el agente.**

No incluyas `.env`, API keys, tokens, ni capturas sensibles en el repositorio público.
