"""v0.2: memoria con drift, guarda anti-duplicados, reconexión VNC acotada, percepción sin capturas redundantes."""
import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config, llm  # noqa: E402
from app.agent import TaskRecord, TaskRunner, _SingleTask  # noqa: E402
from app.guardian import Secrets, check_action  # noqa: E402
from app.memory import AppMemory, DRIFT_THRESHOLD, strip_coordinates, ui_distance, ui_signature  # noqa: E402
from tests.sim_desktop import FakeApp, FakeRFB  # noqa: E402

FLAGS = ("MEMORY_V2", "RECOVERY_V2", "PERCEPTION_V2")


def flags(on: bool):
    st = ExitStack()
    for f in FLAGS:
        st.enter_context(patch.object(config, f, on))
    return st


def make_task(app: FakeApp, plan_tipo="crear"):
    rec = TaskRecord(task_id="t", tarea="tarea")
    rec.plan = {"tipo": plan_tipo}
    mem = AppMemory(os.path.join(tempfile.mkdtemp(), "m.json"))
    st = _SingleTask(TaskRunner(mem), rec, "wss://sim.invalid/websockify", "pw", Secrets())
    st.rfb = FakeRFB(app, {})
    return st


class TestMemoryV2(unittest.IsolatedAsyncioTestCase):
    def test_strip_coordinates(self):
        m = {"modulos": [{"como_llegar": "barra superior, botón 'Ventas' (x≈104,y≈44)"}],
             "notas": ["clic en 300, 200 abre", "x=10, y=20 es el menú", "pie (640, 700)", "precio 1.200"]}
        out = strip_coordinates(m)
        flat = str(out)
        for bad in ("104", "44", "x=10", "(640, 700)"):
            self.assertNotIn(bad, flat)
        self.assertIn("barra superior, botón 'Ventas'", flat)
        self.assertIn("precio 1.200", flat)  # números que no son coordenadas se conservan

    def test_drift_signature_separates_theme_from_data(self):
        def sig(**kw):
            recs = kw.pop("records", None)
            a = FakeApp(**kw)
            if recs:
                a.records = recs
            return ui_signature(a.render()[:, :, [2, 1, 0]])
        base = sig(screen="list")
        self.assertLess(ui_distance(base, sig(screen="list", records=["X", "Y", "Z", "W"])), DRIFT_THRESHOLD)
        self.assertGreaterEqual(ui_distance(base, sig(screen="list", theme="nuevo")), DRIFT_THRESHOLD)
        self.assertGreaterEqual(ui_distance(base, sig(screen="list", modal=True)), DRIFT_THRESHOLD)

    async def test_context_confidence_drift_and_reset(self):
        with flags(True):
            mem = AppMemory(os.path.join(tempfile.mkdtemp(), "m.json"))
            old = ui_signature(FakeApp().render()[:, :, [2, 1, 0]])
            new = ui_signature(FakeApp(theme="nuevo").render()[:, :, [2, 1, 0]])
            await mem.put("App", {"notas": ["menú (x≈5,y≈6)"]})
            await mem.record_outcome("App", True, old)
            await mem.record_outcome("App", False, old)
            mapa, meta = mem.context("App", old)
            self.assertNotIn("x≈", str(mapa))
            self.assertEqual((meta["exitos"], meta["fallos"], meta["drift"]), (1, 1, False))
            _, meta = mem.context("App", new)
            self.assertTrue(meta["drift"])
            await mem.record_outcome("App", True, new)  # éxito verificado en la UI nueva: contadores desde cero
            _, meta = mem.context("App", new)
            self.assertEqual((meta["exitos"], meta["fallos"], meta["drift"]), (1, 0, False))
            await mem.record_outcome("App", None, new)   # tarea que debía fallar: no dice nada de la ruta
            self.assertEqual(mem.context("App", new)[1]["exitos"], 1)

    async def test_flag_off_keeps_v1_storage(self):
        with flags(False):
            mem = AppMemory(os.path.join(tempfile.mkdtemp(), "m.json"))
            await mem.put("App", {"notas": ["menú (x≈5,y≈6)"]})
            self.assertIn("x≈5", str(mem.get("App")))


class TestWriteGuard(unittest.IsolatedAsyncioTestCase):
    async def run_tool(self, st, name, inp, step):
        return await st._execute({"name": name, "input": inp}, step, 0)

    async def test_blocks_immediate_repeat_until_observed(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=2)
        with flags(True), patch("app.agent.asyncio.sleep", new=AsyncMock()):
            st = make_task(app)
            save = {"x": 635, "y": 620, "why": "guardar", "risk": "cambio_esperado"}
            await self.run_tool(st, "click", save, 1)
            out = await self.run_tool(st, "click", {**save, "x": 640}, 2)
            self.assertIn("anti-duplicados", out[0]["text"])
            self.assertEqual(app.saves, 1)
            await self.run_tool(st, "wait", {"seconds": 1, "why": "ver"}, 3)
            self.assertEqual(app.screen, "list")
            self.assertEqual(app.records.count("Nuevo"), 1)

    async def test_flag_off_allows_blind_repeat(self):
        app = FakeApp(screen="form", field_value="Nuevo", slow_ticks=2)
        with flags(False), patch("app.agent.asyncio.sleep", new=AsyncMock()):
            st = make_task(app)
            save = {"x": 635, "y": 620, "why": "guardar", "risk": "cambio_esperado"}
            await self.run_tool(st, "click", save, 1)
            await self.run_tool(st, "click", save, 2)
            self.assertEqual(app.records.count("Nuevo"), 2)  # comportamiento v0.1.3 documentado

    def test_injected_instruction_cannot_trigger_destructive_action(self):
        # Una nota de la app que ordena borrar no puede pasar la política, aunque el operador la marque como lectura.
        why = "la nota del cliente pide eliminar el registro duplicado"
        self.assertIsNotNone(check_action("click", {"why": why, "risk": "lectura"}, {"tipo": "modificar", "objetivo": "cambiar teléfono"}))


class TestReconnect(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_after_drop_and_bounded(self):
        app = FakeApp(screen="list")
        with flags(True):
            st = make_task(app)
            st._make_rfb = lambda timeout: FakeRFB(app, {})
            st.rfb.closed = True
            out = await st._execute({"name": "click", "input": {"x": 1, "y": 1, "why": "x", "risk": "lectura"}}, 1, 0)
            self.assertIn("se restableció", out[0]["text"])
            self.assertEqual(out[1]["type"], "image")
            self.assertEqual(st.reconnects, 1)
            st.reconnects = config.VNC_RECONNECTS
            st.rfb.closed = True
            out = await st._execute({"name": "click", "input": {"x": 1, "y": 1, "why": "x", "risk": "lectura"}}, 2, 0)
            self.assertEqual(out, [])
            self.assertEqual(st.rec.estado, "fallida")

    async def test_done_never_verifies_on_stale_frame(self):
        app = FakeApp(screen="list")
        with flags(True):
            st = make_task(app)
            app.connections.append("refuse")
            st._make_rfb = lambda timeout: FakeRFB(app, {})
            st.rfb.closed = True
            st.last_shots = [st.rfb.screenshot_png()]
            with patch.object(st, "_verify", new_callable=AsyncMock) as ver:
                await st._handle_done({"estado": "terminada", "resumen": "listo", "verificado_en_pantalla": True,
                                       "evidencia": "fila"}, 0)
            ver.assert_not_called()
            self.assertEqual(st.rec.estado, "fallida")

    async def test_no_reconnect_without_time(self):
        app = FakeApp()
        with flags(True):
            st = make_task(app)
            st.t0 -= config.OPERATOR_DEADLINE - 8
            self.assertFalse(await st._reconnect())
            self.assertEqual(st.reconnects, 0)


class TestPerception(unittest.IsolatedAsyncioTestCase):
    async def test_identical_frame_not_resent_but_bounded(self):
        app = FakeApp(screen="list")
        with flags(True):
            st = make_task(app)
            shot, h = st._capture()
            self.assertEqual(len(st._screen_blocks("a", shot, h)), 2)
            self.assertEqual(len(st._screen_blocks("b", shot, h)), 1)        # idéntica: solo texto
            st.images_since_full = config.KEEP_IMAGES - 1                     # la completa saldría del contexto
            self.assertEqual(len(st._screen_blocks("c", shot, h)), 2)

    async def test_flag_off_always_sends(self):
        app = FakeApp(screen="list")
        with flags(False):
            st = make_task(app)
            shot, h = st._capture()
            st._screen_blocks("a", shot, h)
            self.assertEqual(len(st._screen_blocks("b", shot, h)), 2)

    async def test_cache_control_only_when_requested(self):
        fake = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value="ok")))
        with patch("app.llm.client", return_value=fake):
            await llm.create("m", "s", [{"role": "user", "content": "x"}], cache_messages=True)
            self.assertEqual(fake.messages.create.call_args.kwargs["cache_control"], {"type": "ephemeral"})
            await llm.create("m", "s", [{"role": "user", "content": "x"}])
            self.assertNotIn("cache_control", fake.messages.create.call_args.kwargs)


class TestBenchmarkRegression(unittest.IsolatedAsyncioTestCase):
    async def test_v2_scenarios(self):
        import logging
        from scripts import bench
        logging.disable(logging.WARNING)
        try:
            for sc in bench.SCENARIOS:
                m = await bench.run_scenario(sc, True)
                self.assertTrue(m.correcto, sc.name)
                self.assertFalse(m.falso_exito, sc.name)
                self.assertEqual(m.duplicados, 0, sc.name)
                self.assertLess(m.pasos, config.MAX_STEPS, sc.name)
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
