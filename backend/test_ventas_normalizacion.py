"""Normalización de importes, NIT, números de documento y fechas (FactuMatch Ventas)."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from ventas.normalizacion import (
    a_decimal,
    a_decimal_seguro,
    dinero,
    enmascarar_nit,
    es_consumidor_final,
    es_probable_persona_juridica,
    nits_equivalentes,
    normalizar_cufe,
    numero_desde_dian,
    parsear_fecha,
    parsear_fecha_hora,
    parsear_numero,
    rango_utc_odoo,
    utc_odoo_a_local,
)


class TestImportes:
    @pytest.mark.parametrize("valor, esperado", [
        (1234, "1234.00"),
        (0.1 + 0.2, "0.30"),             # el float 0.30000000000000004 no arrastra error
        (1037.035, "1037.04"),           # redondeo half-up a centavos
        ("1234.56", "1234.56"),
        ("1.234.567,89", "1234567.89"),
        ("1,234,567.89", "1234567.89"),
        ("$ 1.234.567,00", "1234567.00"),
        ("200,50", "200.50"),
        ("(1.000,00)", "-1000.00"),
        ("-5000", "-5000.00"),
        ("", "0.00"),
        (None, "0.00"),
    ])
    def test_a_decimal(self, valor, esperado):
        assert a_decimal(valor) == Decimal(esperado)

    def test_texto_no_numerico_lanza_error_y_la_version_segura_avisa(self):
        with pytest.raises(ValueError):
            a_decimal("abc")
        valor, error = a_decimal_seguro("abc")
        assert valor == Decimal("0.00") and "no numérico" in error

    def test_suma_decimal_exacta(self):
        total = sum((a_decimal("0.10") for _ in range(3)), Decimal("0"))
        assert total == Decimal("0.30")
        assert dinero(total) == "0.30"

    def test_dinero_formato_estable(self):
        assert dinero(Decimal("5")) == "5.00"
        assert dinero(Decimal("-0.005")) == "-0.01"


class TestNit:
    def test_equivalencia_con_y_sin_digito_de_verificacion(self):
        assert nits_equivalentes("900000001", "900000001-7")
        assert nits_equivalentes("900.000.001-7", "900000001")
        assert not nits_equivalentes("900000001", "900000002")
        assert not nits_equivalentes("", "900000001")

    def test_consumidor_final_y_tipo(self):
        assert es_consumidor_final("222222222222")
        assert es_probable_persona_juridica("900000001")
        assert es_probable_persona_juridica("9000000017")
        assert not es_probable_persona_juridica("1020304050")

    def test_enmascarado_para_la_ia(self):
        assert enmascarar_nit("1020304050") == "***050"
        assert enmascarar_nit("222222222222") == "222222222222"
        assert enmascarar_nit("") == ""


class TestNumeroDocumento:
    @pytest.mark.parametrize("texto", ["FV/2026/00300", "FV-300", "FV 300", "FV-0300", "fv300", "FV_00300"])
    def test_mismo_numero_con_barras_guiones_espacios_y_ceros(self, texto):
        n = parsear_numero(texto, {"FV"})
        assert n is not None and n.clave == "FV|300"

    def test_desde_columnas_dian(self):
        assert numero_desde_dian("POS", "06730", {"POS"}).clave == "POS|6730"
        # Folio con el prefijo incluido y la columna Prefijo vacía
        assert numero_desde_dian("", "FE14394", {"FE"}).clave == "FE|14394"

    def test_prefijos_distintos_no_colisionan(self):
        a = parsear_numero("FE1706", {"FE1", "FE"})
        b = parsear_numero("FE-1706", {"FE1", "FE"})
        assert a.clave == "FE1|706"           # prefijo conocido más largo primero
        assert b.clave == "FE|1706"
        assert a.clave != b.clave

    def test_solo_digitos_queda_sin_prefijo_y_ambiguo_devuelve_none(self):
        assert parsear_numero("00012").clave == "|12"
        assert parsear_numero("FACTURA DE VENTA 12 A") is None
        assert parsear_numero("") is None

    def test_cufe_normalizado(self):
        assert normalizar_cufe("  ABC def ") == "abcdef"
        assert normalizar_cufe(False) == ""


class TestFechas:
    def test_formatos_del_contenedor(self):
        assert parsear_fecha("15-10-2026") == date(2026, 10, 15)
        assert parsear_fecha("2026-10-15") == date(2026, 10, 15)
        assert parsear_fecha_hora("2026-10-15T07:47:58") == datetime(2026, 10, 15, 7, 47, 58)
        assert parsear_fecha("no es fecha") is None

    def test_utc_de_odoo_a_hora_colombia(self):
        # 03:30 UTC del 14 de octubre = 22:30 del 13 de octubre en Colombia
        assert utc_odoo_a_local("2026-10-14 03:30:00") == datetime(2026, 10, 13, 22, 30)

    def test_rango_de_dias_locales_en_utc(self):
        assert rango_utc_odoo(date(2026, 10, 1), date(2026, 10, 15)) == ("2026-10-01 05:00:00", "2026-10-16 05:00:00")


class TestRevision:
    @pytest.mark.parametrize("valor, esperado", [
        ("15.000", "15000.00"),            # punto de miles colombiano
        ("$ -1.234,56", "-1234.56"),       # signo después del símbolo
        ("-5.000", "-5000.00"),
        ("0.125", "0.13"),
        ("1.5", "1.50"),
    ])
    def test_importes_de_texto(self, valor, esperado):
        assert a_decimal(valor) == Decimal(esperado)

    def test_prefijo_numerico_de_xls(self):
        from ventas.normalizacion import normalizar_prefijo
        assert normalizar_prefijo(20.0) == "20"
