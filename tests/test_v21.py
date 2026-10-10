"""v0.2.1: concurrencia por escritorio, anti-duplicados fuerte, redacción/observabilidad, contrato real del SDK."""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from app import agent as agent_mod, config, llm  # noqa: E402
from app.agent import DesktopSlots, TaskRecord, TaskRunner, _SingleTask, desktop_key  # noqa: E402
from app.commit_guard import CommitGuard, commit_key, is_commit  # noqa: E402
from app.guardian import RedactingFilter, Secrets, redact_obj, register_secret  # noqa: E402
from app.memory import AppMemory  # noqa: E402
from tests.sim_desktop import FakeApp, FakeRFB  # noqa: E402

SAVE = {"x": 635, "y": 620, "why": "guardar el registro", "risk": "cambio_esperado"}


def flags(on: bool):
    st = ExitStack()
    for f in ("MEMORY_V2", "RECOVERY_V2", "PERCEPTION_V2"):
        st.enter_context(patch.object(config, f, on))
    return st


def make_task(app: FakeApp, tipo="crear"):
    rec = TaskRecord(task_id="t", tarea="tarea")
    rec.plan = {"tipo": tipo}
    st = _SingleTask(TaskRunner(AppMemory(os.path.join(tempfile.mkdtemp(), "m.json"))), rec,
                     "wss://sim.invalid/websockify", "pw", Secrets())
    st.rfb = FakeRFB(app, {})
    st._make_rfb = lambda timeout: FakeRFB(app, {})
    return st


async def act(st, name, inp, step):
    return await st._execute({"name": name, "input": inp}, step, 0)


def txt(out):
    return " ".join(b.get("text", "") for b in out if isinstance(b, dict))


# --------------------------------------------------------------------------- TAREA 1: concurrencia
class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mem = AppMemory(os.path.join(tempfile.mkdtemp(), "m.json"))

    async def _runner_with(self, fake_run):
        runner = TaskRunner(self.mem)
        p = patch.object(_SingleTask, "run", fake_run)
        p.start()
        self.addCleanup(p.stop)
        return runner

    async def test_same_desktop_serialized_different_desktops_parallel(self):
        active: dict[str, int] = {}
        peak: dict[str, int] = {}

        async def fake_run(self_):
            k = desktop_key(self_.url)
            active[k] = active.get(k, 0) + 1
            peak[k] = max(peak.get(k, 0), active[k])
            await asyncio.sleep(0.3)
            active[k] -= 1
            self_.finalize("terminada", "ok", None, None)

        runner = await self._runner_with(fake_run)
        t0 = time.perf_counter()
        recs = [runner.submit("a", "wss://d1.test/websockify", "p"), runner.submit("b", "wss://D1.test/websockify/", "p"),
                runner.submit("c", "wss://d2.test/websockify", "p")]
        self.assertLess(time.perf_counter() - t0, 0.05)  # POST /tasks no espera al escritorio
        await asyncio.gather(*(r.worker for r in recs))
        self.assertEqual(peak["d1.test/websockify"], 1)   # nunca dos a la vez en el mismo escritorio
        self.assertEqual(peak["d2.test/websockify"], 1)
        self.assertTrue(all(r.estado == "terminada" for r in recs))
        self.assertEqual(runner.slots.busy, {})           # todo liberado

    async def test_queued_task_that_runs_out_of_budget_never_touches_desktop(self):
        started = []

        async def fake_run(self_):
            started.append(self_.rec.task_id)
            await asyncio.sleep(0.6)
            self_.finalize("terminada", "ok", None, None)

        runner = await self._runner_with(fake_run)
        with patch.object(config, "OPERATOR_DEADLINE", agent_mod.MIN_USEFUL_S + 0.3):
            first = runner.submit("a", "wss://d1.test/websockify", "p")
            second = runner.submit("b", "wss://d1.test/websockify", "p")
            await asyncio.gather(first.worker, second.worker)
        self.assertEqual(started, [first.task_id])
        self.assertEqual(second.estado, "fallida")
        self.assertIn("ocupado", second.resumen)
        self.assertEqual(runner.slots.busy, {})

    async def test_slot_released_on_exception_and_on_cancellation(self):
        async def boom(self_):
            raise RuntimeError("fallo interno con wss://d1.test/websockify")

        runner = await self._runner_with(boom)
        rec = runner.submit("a", "wss://d1.test/websockify", "p")
        await rec.worker
        self.assertEqual(rec.estado, "fallida")
        self.assertEqual(runner.slots.busy, {})

        async def hang(self_):
            await asyncio.sleep(30)

        with patch.object(_SingleTask, "run", hang):
            rec = runner.submit("a", "wss://d1.test/websockify", "p")
            await asyncio.sleep(0.05)
            self.assertIn("d1.test/websockify", runner.slots.busy)
            rec.worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await rec.worker
        self.assertEqual(runner.slots.busy, {})

    async def test_hard_limit_counts_queue_time(self):
        async def hang(self_):
            await asyncio.sleep(30)

        runner = await self._runner_with(hang)
        with patch.object(config, "HARD_LIMIT", 0.4):
            rec = runner.submit("a", "wss://d1.test/websockify", "p")
            t0 = time.perf_counter()
            await rec.worker
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(rec.estado, "fallida")
        self.assertEqual(runner.slots.busy, {})

    async def test_slots_owner_only_release(self):
        s = DesktopSlots()
        self.assertTrue(await s.acquire("k", "a", time.monotonic() + 1))
        s.release("k", "b")                      # un tercero no puede liberar
        self.assertEqual(s.busy, {"k": "a"})
        self.assertFalse(await s.acquire("k", "b", time.monotonic() + 0.1))

    async def test_no_late_action_after_operator_deadline(self):
        app = FakeApp(screen="list")
        with flags(True):
            st = make_task(app)
            st.t0 -= config.OPERATOR_DEADLINE + 1
            out = await act(st, "click", {"x": 1175, "y": 160, "why": "x", "risk": "lectura"}, 1)
        self.assertEqual(out, [])
        self.assertEqual(st.rec.estado, "fallida")
        self.assertEqual(app.destructive, 0)  # el clic tardío nunca llegó a la app


# --------------------------------------------------------------------------- TAREA 2: duplicados
class TestCommitClassification(unittest.TestCase):
    def test_signals_beyond_declared_risk(self):
        self.assertTrue(is_commit("click", {"why": "Guardar el cliente", "risk": "lectura"}))       # risk mal clasificado
        self.assertTrue(is_commit("click", {"why": "pulsar Exportar", "risk": "lectura"}))
        self.assertTrue(is_commit("click", {"why": "confirm the export", "risk": ""}))
        self.assertTrue(is_commit("press_key", {"key": "Return", "why": "enviar", "risk": "lectura"}, "crear", "type_text"))
        self.assertTrue(is_commit("press_key", {"key": "Return", "why": "", "risk": ""}, "crear", "type_text"))
        self.assertTrue(is_commit("click", {"why": "pulsar el botón", "risk": "cambio_esperado"}))

    def test_navigation_and_reads_are_not_commits(self):
        self.assertFalse(is_commit("click", {"why": "abrir el formulario para crear cliente", "risk": "cambio_esperado"}))
        self.assertFalse(is_commit("click", {"why": "cerrar el aviso de guardado", "risk": "lectura"}))
        self.assertFalse(is_commit("press_key", {"key": "Return", "why": "buscar el cliente", "risk": "lectura"}, "consultar", "type_text"))
        self.assertFalse(is_commit("press_key", {"key": "Tab", "why": "guardar", "risk": "cambio_esperado"}))
        self.assertFalse(is_commit("type_text", {"text": "x", "why": "llenar nombre", "risk": "cambio_esperado"}))
        self.assertFalse(is_commit("scroll", {"why": "guardar", "risk": "cambio_esperado"}))

    def test_click_and_double_click_are_same_operation(self):
        g = CommitGuard()
        g.record_commit(commit_key("click", {"x": 100, "y": 100}), 1, "a", "a", False)
        self.assertIsNotNone(g.check(commit_key("double_click", {"x": 108, "y": 95})))
        self.assertIsNone(g.check(commit_key("click", {"x": 400, "y": 100})))  # otro control: permitido


class TestDuplicatePrevention(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._p = patch("app.agent.asyncio.sleep", new=AsyncMock())
        self._p.start()
        self._f = flags(True)
        self._f.__enter__()

    async def asyncTearDown(self):
        self._f.__exit__(None, None, None)
        self._p.stop()

    async def test_slow_save_wait_is_not_proof(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=5)
        st = make_task(app)
        await act(st, "click", SAVE, 1)
        self.assertIn("anti-duplicados", txt(await act(st, "click", SAVE, 2)))
        await act(st, "wait", {"seconds": 1, "why": "ver"}, 3)
        await act(st, "zoom", {"x0": 0, "y0": 0, "x1": 100, "y1": 100, "why": "ver"}, 4)
        self.assertIn("NO prueba", txt(await act(st, "click", SAVE, 5)))    # esperar/mirar no desbloquea
        self.assertEqual(app.records.count("Nuevo"), 1)

    async def test_late_confirmation_blocks_repeat(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=2)
        st = make_task(app)
        await act(st, "click", SAVE, 1)
        await act(st, "wait", {"seconds": 2, "why": "ver"}, 2)              # llega la confirmación tardía
        self.assertEqual(app.screen, "list")
        await act(st, "click", {**SAVE, "risk": "lectura"}, 3)              # mismo punto, riesgo mal declarado
        self.assertEqual(app.records.count("Nuevo"), 1)
        self.assertEqual(st.guard.blocked, 1)

    async def test_double_click_on_save_sends_single_click(self):
        app = FakeApp(screen="form", field_value="Nuevo")
        st = make_task(app)
        out = await act(st, "double_click", SAVE, 1)
        self.assertEqual(app.records.count("Nuevo"), 1)
        self.assertIn("clic simple", txt(out))

    async def test_double_click_on_non_commit_keeps_two_clicks(self):
        app = FakeApp(screen="list")
        st = make_task(app)
        st.rfb.click = AsyncMock()
        await act(st, "double_click", {"x": 60, "y": 160, "why": "abrir la fila", "risk": "lectura"}, 1)
        self.assertEqual(st.rfb.click.call_args.kwargs.get("count"), 2)

    async def test_disconnect_during_write_then_recovery(self):
        app = FakeApp(screen="form", field_value="Nuevo", drop_after_save=True)
        st = make_task(app)
        await act(st, "click", SAVE, 1)                                     # llega al servidor y cae la conexión
        out = await act(st, "click", SAVE, 2)                               # decisión sobre pantalla vieja
        self.assertIn("se restableció", txt(out))
        self.assertEqual(st.reconnects, 1)
        self.assertEqual(app.screen, "list")                                # la pantalla real muestra el registro
        out = await act(st, "click", SAVE, 3)
        self.assertEqual(app.records.count("Nuevo"), 1)

    async def test_uncertain_write_during_drop_requires_progress(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=3)
        st = make_task(app)
        st.rfb.closed = False
        orig = st.rfb.click

        async def click_then_drop(x, y, button=1, hold=0.14, count=1):
            await orig(x, y, count=count)
            st.rfb.closed = True
            from app.rfb import RFBError
            raise RFBError("conexión VNC cerrada")
        st.rfb.click = click_then_drop
        out = await act(st, "click", SAVE, 1)
        self.assertIn("pudo haberse aplicado", txt(out))
        self.assertIn("anti-duplicados", txt(await act(st, "click", SAVE, 2)))
        self.assertEqual(app.records.count("Nuevo"), 1)

    async def test_legit_resubmit_after_fixing_data(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=50)    # la app nunca confirma
        st = make_task(app)
        await act(st, "click", SAVE, 1)
        self.assertIn("anti-duplicados", txt(await act(st, "click", SAVE, 2)))
        await act(st, "type_text", {"text": "Nuevo corregido", "x": 400, "y": 300, "why": "corregir el nombre",
                                    "risk": "cambio_esperado"}, 3)          # progreso real: nuevo dato
        out = await act(st, "click", SAVE, 4)
        self.assertNotIn("anti-duplicados", txt(out))
        self.assertEqual(app.saves, 2)

    async def test_two_different_dialog_buttons_not_blocked(self):
        app = FakeApp(screen="list")
        st = make_task(app)
        await act(st, "click", {"x": 600, "y": 500, "why": "Guardar sin nota", "risk": "cambio_esperado"}, 1)
        out = await act(st, "click", {"x": 600, "y": 380, "why": "Confirmar", "risk": "cambio_esperado"}, 2)
        self.assertNotIn("anti-duplicados", txt(out))

    async def test_flag_off_disables_guard(self):
        self._f.__exit__(None, None, None)
        self._f = flags(False)
        self._f.__enter__()
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=5)
        st = make_task(app)
        await act(st, "click", SAVE, 1)
        await act(st, "click", SAVE, 2)
        self.assertEqual(app.records.count("Nuevo"), 2)  # comportamiento v0.1.3 documentado


# --------------------------------------------------------------------------- TAREA 3: seguridad y observabilidad
class TestRedactionAndMetrics(unittest.IsolatedAsyncioTestCase):
    async def test_debug_safe_and_written_log_have_no_secrets(self):
        sec = Secrets(password="Pw-SuperSecreta", url="wss://desk-77.example.app/websockify", token="ftr_TEAMTOKEN123")
        rec = TaskRecord(task_id="r1", tarea="usa la clave Pw-SuperSecreta en desk-77.example.app")
        rec.secrets = sec
        rec.notes = ["token: ftr_TEAMTOKEN123"]
        rec.error = "ConnectionError wss://desk-77.example.app/websockify"
        rec.plan = {"datos": {"x": "Pw-SuperSecreta"}}
        dump = json.dumps(rec.debug_safe(), ensure_ascii=False)
        for s in ("Pw-SuperSecreta", "ftr_TEAMTOKEN123", "desk-77.example.app"):
            self.assertNotIn(s, dump)
        with tempfile.TemporaryDirectory() as d, patch.object(config, "LOG_DIR", d):
            TaskRunner(AppMemory(os.path.join(d, "m.json")))._write_log(rec)
            raw = open(os.path.join(d, "r1.json"), encoding="utf-8").read()
        json.loads(raw)
        self.assertNotIn("Pw-SuperSecreta", raw)

    def test_log_filter_redacts_messages_and_tracebacks(self):
        register_secret("Pw-OtroSecreto")
        logger = logging.getLogger("aprendiz.test.redact")
        logger.propagate = False
        records = []

        class H(logging.Handler):
            def emit(self, r):
                records.append(self.format(r))
        h = H()
        h.addFilter(RedactingFilter())
        logger.addHandler(h)
        try:
            logger.warning("conectando con %s", "Pw-OtroSecreto")
            try:
                raise RuntimeError("fallo en wss://host.example/websockify password=Pw-OtroSecreto")
            except RuntimeError:
                logger.exception("error")
        finally:
            logger.removeHandler(h)
        joined = "\n".join(records)
        self.assertNotIn("Pw-OtroSecreto", joined)
        self.assertNotIn("host.example", joined)
        self.assertIn("RuntimeError", joined)

    async def test_task_metrics_collected(self):
        app = FakeApp(screen="list", records=["Meta"])
        runner = TaskRunner(AppMemory(os.path.join(tempfile.mkdtemp(), "m.json")))

        async def fake_call_json(model, system, content, role="json", **kw):
            llm._account(role, type("R", (), {"usage": type("U", (), {"input_tokens": 100, "output_tokens": 10,
                                                                        "cache_read_input_tokens": 0,
                                                                        "cache_creation_input_tokens": 0})()})(), 5)
            if role == "planner":
                return {"app_nombre": "Sim", "tipo": "consultar", "objetivo": "x"}
            return {"verificado": True, "motivo": "ok", "riesgo_cambios_extra": "ninguno", "respuesta_corregida": None}

        calls = iter([("remember", {"note": "pagina 1"}),
                      ("done", {"estado": "terminada", "resumen": "r", "respuesta": "1",
                                "verificado_en_pantalla": True, "evidencia": "fila"})])

        async def fake_create(model, system, messages, role="operator", **kw):
            n, i = next(calls)
            r = type("R", (), {"content": [type("B", (), {"type": "tool_use", "id": n, "name": n, "input": i})()],
                               "usage": type("U", (), {"input_tokens": 1000, "output_tokens": 50,
                                                       "cache_read_input_tokens": 800,
                                                       "cache_creation_input_tokens": 200})()})()
            llm._account(role, r, 7)
            return r

        with flags(True), patch.object(llm, "call_json", fake_call_json), patch.object(llm, "create", fake_create), \
                patch.object(_SingleTask, "_make_rfb", lambda self, t: FakeRFB(app, {})), \
                patch.object(config, "LOG_DIR", tempfile.mkdtemp()):
            rec = runner.submit("¿cuántos?", "wss://sim.invalid/websockify", "pw")
            await rec.worker
        m = rec.metrics
        self.assertEqual(rec.estado, "terminada")
        for fase in ("vnc_conexion", "planner", "operator", "verifier"):
            self.assertIn(fase, m["fases_ms"])
        self.assertEqual(m["llm"]["llamadas"], {"planner": 1, "operator": 2, "verifier": 1})
        self.assertEqual(m["llm"]["tokens"]["cache_read_input_tokens"], 1600)
        self.assertEqual(m["pasos"], 2)


# --------------------------------------------------------------------------- TAREA 4: SDK real y percepción
class TestRealSDKContract(unittest.IsolatedAsyncioTestCase):
    """El SDK instalado (no un mock nuestro) serializa cache_control/output_config y expone los tokens de caché."""

    async def test_cache_control_output_config_and_cache_usage(self):
        bodies = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json.loads(request.content))
            body = bodies[-1]
            text = json.dumps({"verificado": True, "motivo": "ok", "riesgo_cambios_extra": "ninguno",
                               "respuesta_corregida": None}) if "output_config" in body else "hola"
            return httpx2.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 3, "cache_read_input_tokens": 3456,
                          "cache_creation_input_tokens": 78}})

        client = anthropic.AsyncAnthropic(api_key="test-key", base_url="http://sdk.test", max_retries=0,
                                          http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
        m = llm.new_metrics()
        token = llm.METRICS.set(m)
        try:
            with patch.object(llm, "_client", client):
                await llm.create(config.MODEL_OPERATOR, "sys", [{"role": "user", "content": "x"}], cache_messages=True)
                out = await llm.call_json(config.MODEL_VERIFIER, "sys", "verifica", schema=agent_mod.VERIFIER_SCHEMA,
                                          role="verifier")
        finally:
            llm.METRICS.reset(token)
        self.assertEqual(bodies[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(bodies[0]["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(bodies[1]["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("cache_control", bodies[1])
        self.assertTrue(out["verificado"])
        self.assertEqual(m["tokens"]["cache_read_input_tokens"], 2 * 3456)
        self.assertEqual(m["tokens"]["cache_creation_input_tokens"], 2 * 78)
        self.assertEqual(m["llamadas"], {"operator": 1, "verifier": 1})


class TestPerceptionConsistency(unittest.IsolatedAsyncioTestCase):
    async def test_png_not_reencoded_for_identical_frame(self):
        app = FakeApp(screen="list")
        st = make_task(app)
        calls = []
        orig = st.rfb.screenshot_png
        st.rfb.screenshot_png = lambda: calls.append(1) or orig()
        a, h1 = st._capture()
        b, h2 = st._capture()
        self.assertEqual((a, h1), (b, h2))
        self.assertEqual(len(calls), 1)
        app.screen = "form"
        await st.rfb.wait_stable()
        c, h3 = st._capture()
        self.assertNotEqual(h3, h1)
        self.assertEqual(len(calls), 2)

    def test_pruning_keeps_tool_pairs_and_bounds_images(self):
        app = FakeApp()
        st = make_task(app)
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}
        st.messages = [{"role": "user", "content": [{"type": "text", "text": "inicio"}, img]}]
        for i in range(8):
            st.messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "click", "input": {}}]})
            st.messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                             "content": [{"type": "text", "text": "ok"}, dict(img)]}]})
        st._prune_images()
        n_img = json.dumps(st.messages).count('"type": "image"')
        self.assertEqual(n_img, config.KEEP_IMAGES)
        uses = [b["id"] for m in st.messages if m["role"] == "assistant" for b in m["content"]]
        results = [b["tool_use_id"] for m in st.messages if m["role"] == "user" for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(uses, results)
        for m in st.messages[1:]:
            if m["role"] == "user":
                self.assertTrue(m["content"][0]["content"])  # ningún tool_result queda vacío


if __name__ == "__main__":
    unittest.main()
