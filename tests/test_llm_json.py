"""Regresión E3: salida JSON del Verifier robusta ante truncado, JSON inválido, esquema y reintentos."""
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx2

from app import llm
from app.agent import VERIFIER_SCHEMA, TaskRecord, _SingleTask
from app.guardian import Secrets


def resp(text: str, stop: str = "end_turn", out_tokens: int = 50):
    blocks = [SimpleNamespace(type="thinking", thinking="")]
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=SimpleNamespace(output_tokens=out_tokens),
                           _request_id="req_test")


OK = {"verificado": True, "motivo": "p1 y p2 contrastadas", "riesgo_cambios_extra": "ninguno",
      "respuesta_corregida": None}


def bad_request():
    req = httpx2.Request("POST", "https://api.example.test/v1/messages")
    return anthropic.BadRequestError("output_config no soportado", response=httpx2.Response(400, request=req), body=None)


class TestParseAndValidate(unittest.TestCase):
    def test_parse_plain_fenced_and_prose(self):
        self.assertEqual(llm.parse_json(json.dumps(OK)), OK)
        self.assertEqual(llm.parse_json("```json\n" + json.dumps(OK) + "\n```"), OK)
        self.assertEqual(llm.parse_json("Veo {dos} páginas. " + json.dumps(OK) + " fin {x}"), OK)

    def test_truncated_raises_typed_error(self):
        with self.assertRaises(llm.LLMJSONError) as cm:
            llm.parse_json('{"verificado": true, "motivo": "p1: 2')
        self.assertEqual(cm.exception.reason, "invalid_json")

    def test_schema_rejects_wrong_types_missing_and_extra(self):
        llm.validate_schema(OK, VERIFIER_SCHEMA)
        llm.validate_schema({**OK, "respuesta_corregida": "17"}, VERIFIER_SCHEMA)
        for bad in ({**OK, "verificado": "true"}, {k: v for k, v in OK.items() if k != "motivo"},
                    {**OK, "extra": 1}, {**OK, "respuesta_corregida": 17}):
            with self.assertRaises(llm.LLMJSONError):
                llm.validate_schema(bad, VERIFIER_SCHEMA)


class TestCallJson(unittest.IsolatedAsyncioTestCase):
    async def call(self, *responses, **kw):
        fake = AsyncMock(side_effect=list(responses))
        with patch("app.llm.create", fake):
            out = await llm.call_json("m", "sys", [{"type": "text", "text": "x"}], max_tokens=4096,
                                      schema=VERIFIER_SCHEMA, role="verifier", **kw)
        return out, fake

    async def test_valid_json_uses_structured_outputs(self):
        out, fake = await self.call(resp(json.dumps(OK)))
        self.assertEqual(out, OK)
        self.assertEqual(fake.await_count, 1)
        fmt = fake.call_args.kwargs["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertEqual(fmt["schema"], VERIFIER_SCHEMA)

    async def test_max_tokens_retries_once_with_bigger_budget_and_fresh_request(self):
        out, fake = await self.call(resp("", stop="max_tokens", out_tokens=4096), resp(json.dumps(OK)))
        self.assertEqual(out, OK)
        budgets = [c.kwargs["max_tokens"] for c in fake.call_args_list]
        self.assertEqual(budgets, [4096, 8192])
        msgs = fake.call_args_list[1].args[2]
        self.assertEqual([m["role"] for m in msgs], ["user"])  # nunca reenvía el texto truncado

    async def test_truncated_text_with_max_tokens_is_not_parsed(self):
        # Aunque haya un prefijo con llaves, stop_reason=max_tokens nunca se acepta como resultado.
        with self.assertRaises(llm.LLMJSONError) as cm:
            await self.call(resp('{"verificado": true', stop="max_tokens"), resp('{"verificado": true', stop="max_tokens"))
        self.assertEqual(cm.exception.reason, "max_tokens")

    async def test_malformed_then_valid(self):
        out, fake = await self.call(resp("no es json"), resp(json.dumps(OK)))
        self.assertEqual(out, OK)
        reminder = fake.call_args_list[1].args[2][0]["content"][-1]["text"]
        self.assertIn("JSON", reminder)

    async def test_retry_is_bounded_to_one(self):
        with self.assertRaises(llm.LLMJSONError):
            _, fake = await self.call(resp("x"), resp("y"), resp(json.dumps(OK)))

    async def test_schema_violation_retries(self):
        out, _ = await self.call(resp(json.dumps({**OK, "verificado": "si"})), resp(json.dumps(OK)))
        self.assertEqual(out, OK)

    async def test_refusal_is_error(self):
        with self.assertRaises(llm.LLMJSONError) as cm:
            await self.call(resp("", stop="refusal"), resp("", stop="refusal"))
        self.assertEqual(cm.exception.reason, "refusal")

    async def test_no_retry_without_time(self):
        with self.assertRaises(llm.LLMJSONError):
            _, fake = await self.call(resp("", stop="max_tokens"), resp(json.dumps(OK)),
                                      deadline=time.monotonic() + 3)

    async def test_api_error_on_retry_propagates(self):
        with self.assertRaises(anthropic.APIConnectionError):
            await self.call(resp("", stop="max_tokens"),
                            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x.test")))

    async def test_output_config_rejected_falls_back_to_prompt_json(self):
        out, fake = await self.call(bad_request(), resp(json.dumps(OK)))
        self.assertEqual(out, OK)
        self.assertNotIn("output_config", fake.call_args_list[1].kwargs)


class TestVerifierEndToEnd(unittest.IsolatedAsyncioTestCase):
    """done -> _verify -> call_json -> finalize, con API simulada y evidencia multipágina."""

    def make(self):
        rec = TaskRecord(task_id="e2e", tarea="¿Cuántos registros cumplen la condición?")
        rec.plan = {"tipo": "consultar"}
        st = _SingleTask(None, rec, "wss://example.test/websockify", "secret", Secrets())
        st.rfb = SimpleNamespace(screenshot_png=lambda: b"final", wait_stable=AsyncMock(return_value=None))
        st.evidence_shots = [(3, "Página 1/2", b"p1"), (6, "Página 2/2", b"p2")]
        st.last_shots = [b"p2"]
        rec.steps = 8
        return st

    DONE = {"estado": "terminada", "resumen": "Sumé las páginas documentadas.", "respuesta": "Hay 31",
            "verificado_en_pantalla": True, "evidencia": "contador por página"}

    async def test_truncated_first_answer_recovers_and_reports_answer_line(self):
        st = self.make()
        fake = AsyncMock(side_effect=[resp("", stop="max_tokens"), resp(json.dumps(OK))])
        with patch("app.llm.create", fake):
            await st._handle_done(dict(self.DONE), 0)
        self.assertEqual(st.rec.estado, "terminada")
        self.assertTrue(st.rec.resumen.startswith("respuesta: 31\n"), st.rec.resumen)
        content = fake.call_args_list[0].args[2][0]["content"]
        images = [b for b in content if b.get("type") == "image"]
        labels = " ".join(b["text"] for b in content if b.get("type") == "text")
        self.assertEqual(len(images), 4)  # 2 páginas ancladas + 2 capturas recientes
        self.assertIn("Página 1/2", labels)
        self.assertIn("Página 2/2", labels)

    async def test_verifier_failure_never_becomes_success(self):
        st = self.make()
        st.t0 = time.monotonic() - 220  # sin tiempo para otra ronda del operador
        fake = AsyncMock(side_effect=[resp("{roto"), resp("{roto")])
        with patch("app.llm.create", fake):
            await st._handle_done(dict(self.DONE), 0)
        self.assertEqual(st.rec.estado, "fallida")
        self.assertNotIn("respuesta:", st.rec.resumen)

    async def test_verifier_correction_overrides_but_empty_correction_does_not(self):
        st = self.make()
        with patch("app.llm.create", AsyncMock(return_value=resp(json.dumps({**OK, "respuesta_corregida": "30"})))):
            await st._handle_done(dict(self.DONE), 0)
        self.assertTrue(st.rec.resumen.startswith("respuesta: 30\n"), st.rec.resumen)
        st = self.make()
        with patch("app.llm.create", AsyncMock(return_value=resp(json.dumps({**OK, "respuesta_corregida": ""})))):
            await st._handle_done(dict(self.DONE), 0)
        self.assertTrue(st.rec.resumen.startswith("respuesta: 31\n"), st.rec.resumen)


if __name__ == "__main__":
    unittest.main()
