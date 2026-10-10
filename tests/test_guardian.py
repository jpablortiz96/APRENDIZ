import unittest

from app.guardian import Secrets, format_summary, check_action

class GuardianTests(unittest.TestCase):
    def test_numeric_answer_first(self):
        r = format_summary("terminada", {"tipo": "consultar"}, "Consulté facturas.", "Tiene 45", None, Secrets())
        self.assertTrue(r.startswith("respuesta: 45\n"), r)

    def test_two_values_order(self):
        r = format_summary("terminada", {"tipo": "consultar"}, "", "Bogotá; 27", None, Secrets())
        self.assertEqual(r, "respuesta: Bogotá; 27")

    def test_failed_task_has_no_answer_and_keys(self):
        r = format_summary("fallida", {"tipo": "consultar"}, "Dos personas homónimas.", "45", ["52345678", "1019876543"], Secrets())
        self.assertNotIn("respuesta:", r)
        self.assertIn("52345678", r)
        self.assertIn("1019876543", r)

    def test_secrets_redacted(self):
        r = format_summary("fallida", {}, "clave abcdefgh en wss://test.example/websockify", None, None,
                           Secrets(password="abcdefgh", url="wss://test.example/websockify", token="ftr_abcdefghijkl"))
        self.assertNotIn("abcdefgh", r)
        self.assertNotIn("test.example", r)

    def test_destructive_intent_rejected(self):
        reason = check_action("click", {"why": "eliminar cliente", "risk": "lectura"}, {"tipo":"modificar", "objetivo":"editar teléfono"})
        self.assertIsNotNone(reason)

if __name__ == "__main__":
    unittest.main()
