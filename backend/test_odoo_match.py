"""
Tests para el conector Odoo (backend/odoo_match.py)

Ejecutar desde la carpeta backend/:
    python -m pytest test_odoo_match -v

Cubre principalmente:
  - Error 404 de XML-RPC (URL placeholder como py-test.odoo.com)
  - Mensaje de error amigable para el usuario
  - Excepciones de autenticación
"""

import unittest
from unittest.mock import patch, MagicMock
import xmlrpc.client

from odoo_match import OdooConnector, OdooAuthError, OdooConnectionError, _normalizar_clave_odoo


class TestOdoo404Error(unittest.TestCase):
    """Reproduce el error reportado en producción:
    `<ProtocolError for py-test.odoo.com/xmlrpc/2/common: 404 Not Found>`."""

    @patch("xmlrpc.client.ServerProxy")
    def test_url_invalida_404_lanza_error_claro(self, mock_proxy):
        proxy = MagicMock()
        proxy.authenticate.side_effect = xmlrpc.client.ProtocolError(
            "https://fresqueria-myn-sas.odoo.com/xmlrpc/2/common", 404, "Not Found", {}
        )
        mock_proxy.return_value = proxy

        connector = OdooConnector(
            url="https://fresqueria-myn-sas.odoo.com",
            database="",
            username="contabilidad@fresqueria.com",
            api_key="01cf4e6f42c390ef53f90bb345f9ef06c0761807",
        )

        with self.assertRaises(OdooConnectionError) as ctx:
            connector.authenticate()

        msg = str(ctx.exception)
        self.assertIn("URL de Odoo inválida", msg)
        self.assertIn("fresqueria-myn-sas.odoo.com", msg)
        self.assertNotIn("ProtocolError", msg, "El error técnico no debe llegar al usuario")

    @patch("xmlrpc.client.ServerProxy")
    def test_url_con_404_en_otro_endpoint(self, mock_proxy):
        """Un 404 en cualquier llamado (ej. /xmlrpc/2/object) también da mensaje claro."""
        proxy = MagicMock()
        proxy.authenticate.side_effect = xmlrpc.client.ProtocolError(
            "https://fresqueria-myn-sas.odoo.com/xmlrpc/2/object", 404, "Not Found", {}
        )
        mock_proxy.return_value = proxy

        connector = OdooConnector(
            url="https://fresqueria-myn-sas.odoo.com",
            database="db",
            username="u",
            api_key="k",
        )

        with self.assertRaises(OdooConnectionError) as ctx:
            connector.authenticate()

        self.assertIn("URL de Odoo inválida", str(ctx.exception))


class TestOdooAuthErrors(unittest.TestCase):
    @patch("xmlrpc.client.ServerProxy")
    def test_credenciales_invalidas(self, mock_proxy):
        proxy = MagicMock()
        proxy.authenticate.return_value = False
        mock_proxy.return_value = proxy

        connector = OdooConnector("https://fresqueria-myn-sas.odoo.com", "db", "u", "bad-key")

        with self.assertRaises(OdooAuthError):
            connector.authenticate()

    @patch("xmlrpc.client.ServerProxy")
    def test_error_de_red_generico(self, mock_proxy):
        proxy = MagicMock()
        proxy.authenticate.side_effect = OSError("Network unreachable")
        mock_proxy.return_value = proxy

        connector = OdooConnector("https://fresqueria-myn-sas.odoo.com", "db", "u", "k")

        with self.assertRaises(OdooConnectionError) as ctx:
            connector.authenticate()

        self.assertIn("No se pudo conectar a Odoo", str(ctx.exception))


class TestReferenciaProveedor(unittest.TestCase):
    def test_referencia_iglu_sin_guion_conserva_el_prefijo_numerico(self):
        """IM36-66920 de DIAN y IM3666920 de account.move.ref son la misma factura."""
        self.assertEqual(
            _normalizar_clave_odoo("IM3666920"),
            _normalizar_clave_odoo("IM36-66920"),
        )

    def test_referencia_sin_guion_no_elimina_cero_del_prefijo(self):
        self.assertEqual(_normalizar_clave_odoo("IM0553299"), "IM0553299")


if __name__ == "__main__":
    unittest.main()
