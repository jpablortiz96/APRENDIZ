"""Regresión: los conteos paginados retienen evidencia de todas sus páginas."""
import unittest
from unittest.mock import AsyncMock, patch

from app.agent import TaskRecord, _SingleTask
from app.guardian import Secrets


class TestPagedEvidence(unittest.IsolatedAsyncioTestCase):
    def make_runner(self):
        rec = TaskRecord(task_id="ejemplo", tarea="¿Cuántos registros del cliente exacto?")
        rec.plan = {"tipo": "consultar"}
        st = _SingleTask(None, rec, "wss://example.test/websockify", "secret", Secrets())
        st.rfb = type("FakeRFB", (), {"screenshot_png": lambda _: b"pagina-1", "closed": False})()
        return st

    async def test_remember_anchors_current_screenshot(self):
        st = self.make_runner()
        result = await st._execute({"name": "remember", "input": {"note": "Página 1/3, cliente exacto 16, similar 4"}}, 5, 0)
        self.assertEqual(len(st.evidence_shots), 1)
        self.assertEqual(st.evidence_shots[0], (5, "Página 1/3, cliente exacto 16, similar 4", b"pagina-1"))
        self.assertIn("captura visual conservadas", result[0]["text"])

    async def test_verifier_gets_pinned_pages_and_latest_capture(self):
        st = self.make_runner()
        st.evidence_shots = [(1, "Página 1/3", b"pagina-1"),
                             (2, "Página 2/3", b"pagina-2"),
                             (3, "Página 3/3", b"pagina-3")]
        st.last_shots = [b"pagina-3", b"actual"]
        with patch("app.agent.llm.call_json", new_callable=AsyncMock, return_value={"verificado": True}) as fake:
            await st._verify("total según páginas", "42", "3 páginas")
        content = fake.call_args.args[2]
        shots = [block for block in content if block.get("type") == "image"]
        self.assertEqual(len(shots), 5)
        labels = " ".join(block["text"] for block in content if block.get("type") == "text")
        for n in ("Página 1/3", "Página 2/3", "Página 3/3"):
            self.assertIn(n, labels)
        self.assertNotIn("respuesta: 45", labels)

    async def test_verified_consultation_keeps_actual_answer(self):
        st = self.make_runner()
        st.rfb = type("FakeRFB", (), {
            "screenshot_png": lambda _: b"actual",
            "wait_stable": AsyncMock(return_value=None), "closed": False,
        })()
        st.last_shots = [b"previa"]
        st.rec.steps = 6
        st.evidence_shots = [(4, "Página 1/1", b"pagina-1")]
        with patch.object(st, "_verify", new_callable=AsyncMock,
                          return_value={"verificado": True, "riesgo_cambios_extra": "ninguno"}):
            await st._handle_done({"estado": "terminada", "resumen": "Recuento verificado",
                                   "respuesta": "17", "verificado_en_pantalla": True,
                                   "evidencia": "Captura de 17 filas"}, 0)
        self.assertEqual(st.rec.estado, "terminada")
        self.assertTrue(st.rec.resumen.startswith("respuesta: 17\n"), st.rec.resumen)

    async def test_pinned_evidence_is_bounded(self):
        st = self.make_runner()
        for step in range(9):
            await st._execute({"name": "remember", "input": {"note": str(step)}}, step, 0)
        self.assertEqual(len(st.evidence_shots), 6)
        self.assertEqual(st.evidence_shots[0][1], "3")


if __name__ == "__main__":
    unittest.main()
