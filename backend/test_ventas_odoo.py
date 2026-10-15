"""Extracción de Odoo en solo lectura y conjunto canónico (account.move + pos.order)."""

from datetime import date
from decimal import Decimal

import pytest

from ventas.canonico import construir_conjunto
import xmlrpc.client

from ventas.odoo_ventas import (
    AccesoNoPermitido,
    AlcanceInvalido,
    LecturaTruncada,
    OdooSoloLectura,
    PermisoInsuficiente,
    _TransporteConTiempo,
    extraer_ventas,
    ubicar_compania,
)

DESDE, HASTA = date(2026, 10, 1), date(2026, 10, 15)


# ── Solo lectura ──

@pytest.mark.parametrize("metodo", ["create", "write", "unlink", "action_post", "button_cancel", "copy", "execute"])
def test_metodos_de_escritura_bloqueados(ventas, metodo):
    odoo = ventas.OdooFalso()
    lector = OdooSoloLectura(odoo)
    with pytest.raises(AccesoNoPermitido):
        lector.llamar("account.move", metodo, [[1]], {})
    assert odoo.llamadas == []          # ni siquiera llega a Odoo


@pytest.mark.parametrize("modelo", ["res.users", "ir.config_parameter", "account.tax", "ir.sequence"])
def test_modelos_fuera_de_lista_blanca_bloqueados(ventas, modelo):
    with pytest.raises(AccesoNoPermitido):
        OdooSoloLectura(ventas.OdooFalso()).llamar(modelo, "search_read", [[]], {})


def test_la_extraccion_usa_solo_estos_metodos(ventas):
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(1, "FE/2026/00001", 100, partner=[70, "Cliente"])],
                            ordenes=[ventas.orden_pos(5, "POS10", 50)],
                            partners=[{"id": 70, "name": "Cliente", "vat": "800000002"}])
    extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA)
    assert {m for _, m in odoo.llamadas} == {"fields_get", "search_read", "read"}


def test_transporte_con_tiempo_maximo():
    conexion = _TransporteConTiempo(7.5).make_connection("odoo.example.com")
    assert conexion.timeout == 7.5


# ── Compañía ──

def test_ubica_la_compania_por_nit_con_dv(ventas):
    odoo = ventas.OdooFalso(companias=[{"id": 1, "name": "Otra", "vat": "800000002-1"},
                                       {"id": 2, "name": "La nuestra", "vat": ventas.NIT_EMPRESA_DV}])
    companias, total = ubicar_compania(OdooSoloLectura(odoo), ventas.NIT_EMPRESA)
    assert [c["id"] for c in companias] == [2] and total == 2


def test_varias_companias_con_el_mismo_nit_se_leen_juntas(ventas):
    odoo = ventas.OdooFalso(
        companias=[{"id": 1, "name": "Sede norte", "vat": ventas.NIT_EMPRESA_DV}, {"id": 3, "name": "Sede sur", "vat": ventas.NIT_EMPRESA},
                   {"id": 2, "name": "Otra", "vat": "800000002"}],
        movimientos=[ventas.movimiento(1, "FE/2026/00001", 100), ventas.movimiento(2, "FE/2026/00002", 100, compania=3),
                     ventas.movimiento(3, "FE/2026/00003", 100, compania=2)])
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA, incluir_pos=False)
    assert sorted(m["id"] for m in ext.movimientos) == [1, 2]
    assert ext.companias_ids == [1, 3] and ext.a_dict()["companias_con_el_nit"] == 2


def test_nit_emisor_sin_compania_en_odoo(ventas):
    odoo = ventas.OdooFalso(companias=[{"id": 1, "name": "Otra", "vat": "800000002-1"}])
    with pytest.raises(AlcanceInvalido):
        ubicar_compania(OdooSoloLectura(odoo), ventas.NIT_EMPRESA)


def test_registros_de_otra_compania_no_se_leen(ventas):
    odoo = ventas.OdooFalso(
        companias=[{"id": 1, "name": "La nuestra", "vat": ventas.NIT_EMPRESA_DV}, {"id": 2, "name": "Otra", "vat": "800000002"}],
        movimientos=[ventas.movimiento(1, "FE/2026/00001", 100), ventas.movimiento(2, "FE/2026/00002", 100, compania=2)],
        ordenes=[ventas.orden_pos(5, "POS10", 50), ventas.orden_pos(6, "POS11", 50, compania=2)])
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA)
    assert [m["id"] for m in ext.movimientos] == [1]
    assert [o["id"] for o in ext.ordenes_pos] == [5]


# ── Campos y fuentes ──

def test_campos_inexistentes_se_informan_y_no_se_usan(ventas):
    campos = ["name", "move_type", "state", "invoice_date", "company_id", "amount_total"]
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(1, "FE/2026/00001", 100)], campos_move=campos)
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA, incluir_pos=False)
    assert "l10n_co_edi_cufe_cude_ref" in ext.campos_no_disponibles["account.move"]
    assert any("solo se identifican por número" in a for a in ext.advertencias)
    assert set(ext.movimientos[0]) <= set(campos) | {"id"}


def test_lectura_que_supera_el_maximo_falla_en_vez_de_truncar(ventas):
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(i, f"FE/2026/{i:05d}", 10) for i in range(1, 8)])
    lector = OdooSoloLectura(odoo)
    with pytest.raises(LecturaTruncada):
        lector.search_read_todo("account.move", [], ["name"], lote=3, maximo=6)
    assert len(lector.search_read_todo("account.move", [], ["name"], lote=3, maximo=9)) == 7


def test_sin_permiso_de_lectura_sobre_pos(ventas):
    class SinPermiso(ventas.OdooFalso):
        def __call__(self, modelo, metodo, args, kwargs):
            if modelo == "pos.order" and metodo == "search_read":
                raise xmlrpc.client.Fault(4, "odoo.exceptions.AccessError: You are not allowed to access 'Point of Sale Orders'")
            return super().__call__(modelo, metodo, args, kwargs)

    with pytest.raises(PermisoInsuficiente, match="pos.order"):
        extraer_ventas(OdooSoloLectura(SinPermiso()), ventas.NIT_EMPRESA, DESDE, HASTA)


def test_pos_no_instalado_no_rompe_la_extraccion(ventas):
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(1, "FE/2026/00001", 100)], pos_instalado=False)
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA)
    assert ext.fuentes == {"account.move": True, "pos.order": False}
    assert any("pos.order" in a for a in ext.advertencias)


def test_ventana_ampliada_y_liberar_registros(ventas):
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(1, "FE/2026/00001", 100, fecha="2026-09-28"),
                                         ventas.movimiento(2, "FE/2026/00002", 100, fecha="2026-09-20")])
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA, margen_dias=5)
    assert [m["id"] for m in ext.movimientos] == [1]          # 28-09 entra por el margen; 20-09 no
    ext.liberar_registros()
    assert ext.movimientos == [] and ext.a_dict()["registros_leidos"]["account.move"] == 1


# ── Conjunto canónico ──

def _conjunto(ventas, movimientos=(), ordenes=(), prefijos=("FE", "POS")):
    odoo = ventas.OdooFalso(movimientos=movimientos, ordenes=ordenes)
    ext = extraer_ventas(OdooSoloLectura(odoo), ventas.NIT_EMPRESA, DESDE, HASTA)
    return construir_conjunto(ext, DESDE, HASTA, set(prefijos))


def test_estados_en_alcance(ventas):
    c = _conjunto(ventas, movimientos=[
        ventas.movimiento(1, "FE/2026/00001", 100),
        ventas.movimiento(2, "FE/2026/00002", 100, estado="draft"),
        ventas.movimiento(3, "FE/2026/00003", 100, estado="cancel"),
    ], ordenes=[
        ventas.orden_pos(10, "POS10", 50, estado="paid"),
        ventas.orden_pos(11, "POS11", 50, estado="invoiced", factura=False),
        ventas.orden_pos(12, "POS12", 50, estado="cancel"),
        ventas.orden_pos(13, "POS13", 50, estado="draft"),
    ])
    en = {d.ref for d in c.documentos}
    fuera = {d.ref: d.motivo_alcance for d in c.fuera_de_alcance}
    assert en == {"odoo:account.move:1", "odoo:pos.order:10", "odoo:pos.order:11"}
    assert "draft" in fuera["odoo:account.move:2"] and "cancel" in fuera["odoo:account.move:3"]
    assert "cancel" in fuera["odoo:pos.order:12"] and "draft" in fuera["odoo:pos.order:13"]


def test_pos_facturado_cuenta_una_sola_vez(ventas):
    c_factura = ventas.cufe("FE-1")
    c = _conjunto(ventas,
                  movimientos=[ventas.movimiento(1, "FE/2026/00001", 100, cufe_valor=c_factura)],
                  ordenes=[ventas.orden_pos(10, "POS10", 100, estado="invoiced", factura=[1, "FE/2026/00001"])])
    assert [d.ref for d in c.documentos] == ["odoo:account.move:1"]
    assert c.pos_agrupadas == 1
    assert c.documentos[0].origenes == ["account.move:1", "pos.order:10"]
    assert c.trazabilidad[0]["accion"] == "agrupado"


def test_pos_facturado_con_cufe_distinto_se_conserva_con_alerta(ventas):
    c = _conjunto(ventas,
                  movimientos=[ventas.movimiento(1, "FE/2026/00001", 100, cufe_valor=ventas.cufe("A"))],
                  ordenes=[ventas.orden_pos(10, "POS10", 100, factura=[1, "FE/2026/00001"], cufe_valor=ventas.cufe("B"))])
    assert {d.ref for d in c.documentos} == {"odoo:account.move:1", "odoo:pos.order:10"}
    assert any(a["codigo"] == "pos_factura_cufe_distinto" for a in c.alertas)


def test_pos_facturado_fuera_de_la_ventana_queda_fuera_de_alcance(ventas):
    c = _conjunto(ventas, ordenes=[ventas.orden_pos(10, "POS10", 100, factura=[99, "FE/2026/00099"])])
    assert c.documentos == []
    assert "FE/2026/00099" in c.fuera_de_alcance[0].motivo_alcance


def test_devolucion_pos_y_nota_credito(ventas):
    c = _conjunto(ventas,
                  movimientos=[ventas.movimiento(1, "NC/2026/00001", 30, tipo="out_refund")],
                  ordenes=[ventas.orden_pos(10, "POS10", 20, reembolso=True)])
    tipos = {d.ref: (d.tipo, d.signo, d.total) for d in c.documentos}
    assert tipos["odoo:account.move:1"] == ("nota_credito", -1, Decimal("30.00"))
    assert tipos["odoo:pos.order:10"] == ("nota_credito", -1, Decimal("20.00"))


def test_fecha_pos_en_hora_de_colombia(ventas):
    # 22:00 del 15-10 en Colombia = 03:00 UTC del 16-10: pertenece al 15-10
    c = _conjunto(ventas, ordenes=[ventas.orden_pos(10, "POS10", 100, fecha="2026-10-15", hora="22:00:00"),
                                   ventas.orden_pos(11, "POS11", 100, fecha="2026-10-16", hora="00:30:00")])
    assert [d.ref for d in c.documentos] == ["odoo:pos.order:10"]
    assert c.documentos[0].fecha == date(2026, 10, 15)


def test_alertas_de_cufe_y_numero_repetidos(ventas):
    repetido = ventas.cufe("X")
    c = _conjunto(ventas, movimientos=[ventas.movimiento(1, "FE/2026/00001", 100, cufe_valor=repetido),
                                       ventas.movimiento(2, "FE/2026/00002", 100, cufe_valor=repetido),
                                       ventas.movimiento(3, "FE/2026/00003", 100),
                                       ventas.movimiento(4, "FE-3", 100)])
    codigos = [a["codigo"] for a in c.alertas]
    assert "cufe_repetido_odoo" in codigos and "numero_repetido_odoo" in codigos


def test_nombre_de_asiento_sin_referencia_libre(ventas):
    c = _conjunto(ventas,
                  movimientos=[ventas.movimiento(1, "FE/2026/00001", 100),
                               ventas.movimiento(2, "NC/2026/00001", 100, tipo="out_refund",
                                                 reversed_entry_id=[1, "FE/2026/00001 (Pedido de Juan Pérez)"])],
                  ordenes=[ventas.orden_pos(10, "POS10", 100, factura=[99, "FE/2026/00099 (Juan Pérez)"])])
    nc = next(d for d in c.documentos if d.ref == "odoo:account.move:2")
    assert nc.relaciones["factura_original"] == "FE/2026/00001"
    assert "Juan" not in c.fuera_de_alcance[0].motivo_alcance


def test_autenticacion_con_tiempo_maximo_y_errores_claros():
    from types import SimpleNamespace

    from odoo_match import OdooConnectionError
    from ventas.odoo_ventas import _autenticar

    conector = SimpleNamespace(url="http://127.0.0.1:9", database="db", username="u", api_key="k")
    with pytest.raises(OdooConnectionError):
        _autenticar(conector, 2)


def test_nit_con_guion_no_confunde_otra_entidad(ventas):
    odoo = ventas.OdooFalso(companias=[{"id": 1, "name": "La nuestra", "vat": "900123456-7"},
                                       {"id": 2, "name": "Otra entidad", "vat": "90012345-6"},
                                       {"id": 3, "name": "Sin guion", "vat": "9001234567"}])
    companias, _ = ubicar_compania(OdooSoloLectura(odoo), "900123456")
    assert [c["id"] for c in companias] == [1, 3]


def test_autenticacion_ante_una_pagina_html(monkeypatch):
    import xmlrpc.client
    from types import SimpleNamespace

    from odoo_match import OdooConnectionError
    from ventas import odoo_ventas

    class ProxyHtml:
        def __init__(self, *a, **k):
            pass

        def authenticate(self, *a):
            raise xmlrpc.client.ResponseError("respuesta HTML en vez de XML-RPC")

    monkeypatch.setattr(odoo_ventas.xmlrpc.client, "ServerProxy", ProxyHtml)
    with pytest.raises(OdooConnectionError, match="revisa la URL"):
        odoo_ventas._autenticar(SimpleNamespace(url="https://odoo.example.com", database="d", username="u", api_key="k"), 5)
