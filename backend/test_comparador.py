"""
Tests para backend/comparador.py

Ejecutar desde la carpeta backend/:
    python -m unittest test_comparador -v

Estos tests validan principalmente:
  - El fix del bug #5: una factura con la misma "clave" pero DIFERENTE NIT
    NO debe contar como encontrada (era un falso positivo silencioso).
  - La normalización de claves entre formatos DIAN y Siesa.
  - Casos extremos (datasets vacíos, NITs sin DV, etc.).
"""

import unittest
import pandas as pd

from comparador import (
    _ejecutar_comparacion,
    normalizar_clave,
    normalizar_nit,
)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def make_dian_row(nit, prefijo, folio, nombre="Proveedor X", fecha="2025-01-15"):
    """Construye una fila DIAN ya normalizada lista para _ejecutar_comparacion."""
    clave = normalizar_clave(prefijo, folio)
    folio_original = f"{prefijo}-{folio}" if prefijo else str(folio)
    # _ejecutar_comparacion solo lee: nit, nombre, clave, folio_original, fecha
    return {
        "nit": normalizar_nit(nit),
        "nombre": nombre,
        "fecha": fecha,
        "folio_original": folio_original,
        "clave": clave,
    }


def make_siesa_row(nit, docto, nombre="Proveedor X"):
    """Construye una fila Siesa ya normalizada."""
    clave = normalizar_clave("", str(docto))
    return {
        "nit": normalizar_nit(nit),
        "docto": docto,
        "docto_original": str(docto),
        "nombre": nombre,
        "clave": clave,
    }


def df_dian(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["nit", "nombre", "fecha", "folio_original", "clave"]
    )


def df_siesa(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["nit", "docto", "docto_original", "nombre", "clave"]
    )


# ──────────────────────────────────────────────
# TESTS DEL MOTOR DE COMPARACIÓN
# ──────────────────────────────────────────────

class TestComparacionBasica(unittest.TestCase):
    """Casos base: la comparación funciona como se espera."""

    def test_true_positive_mismo_nit_misma_clave(self):
        """Si DIAN y Siesa tienen la misma factura para el mismo NIT → encontrada."""
        dian = df_dian([make_dian_row("900123456", "FEMQ", "00004465")])
        siesa = df_siesa([make_siesa_row("900123456", "FEMQ-00004465")])

        resultado = _ejecutar_comparacion(dian, siesa)

        self.assertEqual(resultado["resumen_general"]["total_dian"], 1)
        self.assertEqual(resultado["resumen_general"]["total_en_siesa"], 1)
        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 0)
        self.assertEqual(resultado["proveedores"][0]["total_faltantes"], 0)

    def test_true_negative_clave_no_existe(self):
        """Si la clave no está en Siesa → faltante."""
        dian = df_dian([make_dian_row("900123456", "FEMQ", "00004465")])
        siesa = df_siesa([make_siesa_row("900123456", "OTRO-99999")])

        resultado = _ejecutar_comparacion(dian, siesa)

        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 1)
        self.assertEqual(resultado["proveedores"][0]["total_faltantes"], 1)

    def test_misma_clave_distinto_nit_es_faltante(self):
        """
        REGRESIÓN BUG #5:
        Una factura con prefijo+folio igual pero registrada bajo un NIT distinto
        en Siesa NO debe contarse como encontrada. Esto era el falso positivo.
        Adicionalmente verifica que el folio_original aparezca en `faltantes`,
        no en `encontradas`.
        """
        dian = df_dian([make_dian_row("900123456", "FEMQ", "00004465", nombre="Proveedor A")])
        # Siesa tiene la MISMA clave pero bajo otro NIT (proveedor B)
        siesa = df_siesa([make_siesa_row("800999000", "FEMQ-00004465", nombre="Proveedor B")])

        resultado = _ejecutar_comparacion(dian, siesa)

        # ANTES DEL FIX esto pasaba 0 faltantes (FALSO POSITIVO)
        # DESPUÉS DEL FIX debe ser 1 faltante.
        self.assertEqual(
            resultado["resumen_general"]["total_faltantes"], 1,
            "BUG #5: factura con misma clave bajo NIT distinto fue marcada como encontrada"
        )
        prov = resultado["proveedores"][0]
        self.assertEqual(prov["total_faltantes"], 1)
        self.assertEqual(prov["encontradas"], [])
        self.assertEqual(
            [f["factura"] for f in prov["faltantes"]],
            ["FEMQ-00004465"],
            "El folio_original debe aparecer en faltantes con su formato original"
        )

    def test_dian_vacio(self):
        """Sin facturas DIAN no debería romper, y porcentaje_completitud = 0."""
        dian = df_dian([])
        siesa = df_siesa([make_siesa_row("900123456", "FEMQ-00004465")])

        resultado = _ejecutar_comparacion(dian, siesa)

        self.assertEqual(resultado["resumen_general"]["total_dian"], 0)
        self.assertEqual(resultado["resumen_general"]["total_en_siesa"], 0)
        self.assertEqual(resultado["resumen_general"]["porcentaje_completitud"], 0)
        self.assertEqual(resultado["proveedores"], [])

    def test_siesa_vacio_todas_faltantes(self):
        """Si Siesa está vacío, todas las DIAN son faltantes."""
        dian = df_dian([
            make_dian_row("900123456", "FEMQ", "1"),
            make_dian_row("900123456", "FEMQ", "2"),
        ])
        siesa = df_siesa([])

        resultado = _ejecutar_comparacion(dian, siesa)

        self.assertEqual(resultado["resumen_general"]["total_dian"], 2)
        self.assertEqual(resultado["resumen_general"]["total_en_siesa"], 0)
        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 2)
        self.assertEqual(resultado["resumen_general"]["porcentaje_completitud"], 0)


class TestNormalizacionDeFormatos(unittest.TestCase):
    """Validar que la normalización equipara los distintos formatos de factura."""

    def test_dian_y_siesa_formatos_distintos_misma_factura(self):
        """DIAN viene con prefijo+folio separados; Siesa los manda concatenados."""
        dian = df_dian([make_dian_row("900111", "FEMQ", "00004465")])
        siesa = df_siesa([make_siesa_row("900111", "FEMQ-00004465")])

        resultado = _ejecutar_comparacion(dian, siesa)
        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 0)

    def test_ceros_a_la_izquierda(self):
        """'00004465' y '4465' deben tratarse como la misma factura."""
        dian = df_dian([make_dian_row("900111", "FEMQ", "00004465")])
        siesa = df_siesa([make_siesa_row("900111", "FEMQ-4465")])

        resultado = _ejecutar_comparacion(dian, siesa)
        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 0)

    def test_case_insensitive_en_prefijo(self):
        """'femq' y 'FEMQ' deben considerarse iguales."""
        clave_minus = normalizar_clave("femq", "001")
        clave_mayus = normalizar_clave("FEMQ", "001")
        self.assertEqual(clave_minus, clave_mayus)

    def test_nit_con_puntos_y_dv_se_normaliza(self):
        """'900.123.456-7' debe limpiarse a '9001234567'."""
        self.assertEqual(normalizar_nit("900.123.456-7"), "9001234567")

    def test_nit_vacio_o_nan(self):
        self.assertEqual(normalizar_nit(None), "")
        self.assertEqual(normalizar_nit(""), "")
        self.assertEqual(normalizar_nit(float("nan")), "")


class TestNormalizarClave(unittest.TestCase):
    """Tests directos sobre la función normalizar_clave."""

    def test_prefijo_y_folio_separados(self):
        self.assertEqual(normalizar_clave("BOG", "16856"), "BOG16856")

    def test_folio_combinado_con_guion(self):
        self.assertEqual(normalizar_clave("", "FEMQ-00004465"), "FEMQ4465")

    def test_folio_con_ceros_a_la_izquierda(self):
        self.assertEqual(normalizar_clave("FEMQ", "00004465"), "FEMQ4465")

    def test_caracteres_no_alfanumericos_en_prefijo(self):
        self.assertEqual(normalizar_clave("FE.M-Q", "00001"), "FEMQ1")


class TestCasosCombinados(unittest.TestCase):
    """Casos de mayor complejidad que combinan múltiples reglas."""

    def test_multiple_facturas_mismo_proveedor(self):
        """5 DIAN, 3 en Siesa → 3 encontradas + 2 faltantes."""
        nit = "900111"
        dian = df_dian([
            make_dian_row(nit, "FEMQ", str(i)) for i in range(1, 6)  # 1..5
        ])
        siesa = df_siesa([
            make_siesa_row(nit, f"FEMQ-{i:05d}") for i in [1, 2, 3]
        ])

        resultado = _ejecutar_comparacion(dian, siesa)

        self.assertEqual(resultado["resumen_general"]["total_dian"], 5)
        self.assertEqual(resultado["resumen_general"]["total_en_siesa"], 3)
        self.assertEqual(resultado["resumen_general"]["total_faltantes"], 2)

    def test_porcentaje_completitud_correcto(self):
        """Validar el cálculo del % completitud."""
        nit = "900111"
        dian = df_dian([
            make_dian_row(nit, "FAC", str(i)) for i in range(1, 5)  # 4 facturas
        ])
        siesa = df_siesa([make_siesa_row(nit, f"FAC-{i}") for i in [1, 2, 3]])

        resultado = _ejecutar_comparacion(dian, siesa)
        # 3 de 4 = 75%
        self.assertEqual(resultado["resumen_general"]["porcentaje_completitud"], 75.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
