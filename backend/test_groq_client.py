"""
Tests para el endpoint de diagnóstico Groq (backend/main.py: /api/ai/test-groq)

Ejecutar desde la carpeta backend/:
    python -m pytest test_groq_client -v

Cubre:
  - API key ausente            → stage=configuration
  - HTTP 401 / 403 / 429 / 5xx → stage=groq con status_code
  - Timeout y error de red     → stage=network
  - Respuesta 200 válida       → stage=success
  - Respuesta 200 inesperada   → stage=groq con error claro
  - La API key NUNCA se expone en la respuesta
"""

import os
import unittest
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

# Requisitos de import de main.py (antes de importar el módulo)
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GROQ_API_KEY", "")

from fastapi.testclient import TestClient
import main as main_module


FAKE_KEY = "gsk_testfake1234567890abcd"


def make_http_response(status_code: int, *, json_body=None, text=None):
    """Construye una httpx.Response real con request asociado."""
    request = httpx.Request("POST", main_module.GROQ_API_URL)
    return httpx.Response(status_code, json=json_body, text=text, request=request)


class TestGroqDiagnostic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main_module.app)

    def _get(self):
        return self.client.get("/api/ai/test-groq")

    # ── 1. API key ausente ──

    def test_api_key_ausente_configuration(self):
        with patch.object(main_module, "GROQ_API_KEY", ""), \
             patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False):
            res = self._get()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["stage"], "configuration")
        self.assertIn("GROQ_API_KEY", body["error"])
        self.assertNotIn("key_last4", body)

    # ── 2, 3, 4, 5. Errores HTTP de Groq ──

    def test_http_401(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 401, json_body={"error": {"message": "Invalid API key"}}
             )):
            res = self._get()
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["stage"], "groq")
        self.assertEqual(body["status_code"], 401)
        self.assertIn("401", body["error"])

    def test_http_403(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 403, json_body={"error": {"message": "Forbidden"}}
             )):
            res = self._get()
        body = res.json()
        self.assertEqual(body["stage"], "groq")
        self.assertEqual(body["status_code"], 403)

    def test_http_429(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 429, json_body={"error": {"message": "Rate limit exceeded"}}
             )):
            res = self._get()
        body = res.json()
        self.assertEqual(body["stage"], "groq")
        self.assertEqual(body["status_code"], 429)

    def test_http_500(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 500, text="Internal Server Error"
             )):
            res = self._get()
        body = res.json()
        self.assertEqual(body["stage"], "groq")
        self.assertEqual(body["status_code"], 500)

    # ── 6, 7. Errores de red ──

    def test_timeout(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post",
                          side_effect=httpx.TimeoutException("timed out")):
            res = self._get()
        body = res.json()
        self.assertEqual(body["stage"], "network")
        self.assertIn("Timeout", body["error"])

    def test_error_de_conexion(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post",
                          side_effect=httpx.ConnectError("DNS resolution failed")):
            res = self._get()
        body = res.json()
        self.assertEqual(body["stage"], "network")
        self.assertIn("conexión", body["error"])

    # ── 8, 9. Respuestas 200 ──

    def test_respuesta_200_valida(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 200, json_body={"choices": [{"message": {"content": "GROQ_OK"}}]}
             )):
            res = self._get()
        body = res.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["stage"], "success")
        self.assertEqual(body["status_code"], 200)
        self.assertEqual(body["message"], "GROQ_OK")
        self.assertEqual(body["model"], main_module.GROQ_MODEL)

    def test_respuesta_200_estructura_inesperada(self):
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 200, json_body={"unexpected": True}
             )):
            res = self._get()
        body = res.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["stage"], "groq")
        self.assertEqual(body["status_code"], 200)
        self.assertIn("estructura", body["error"])

    # ── 10. Seguridad: nunca exponer la key completa ──

    def test_key_completa_nunca_expuesta(self):
        """La key completa no aparece en ninguna respuesta (solo últimos 4)."""
        with patch.object(main_module, "GROQ_API_KEY", FAKE_KEY), \
             patch.object(main_module.httpx, "post", return_value=make_http_response(
                 401, json_body={"error": {"message": f"bad key {FAKE_KEY}"}}
             )):
            res = self._get()
        raw = res.text
        self.assertNotIn(FAKE_KEY, raw)
        self.assertNotIn("gsk_testfake1234567890ab", raw)
        # Identificación parcial permitida: solo últimos 4
        self.assertIn("...abcd", raw)


if __name__ == "__main__":
    unittest.main()
