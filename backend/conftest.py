"""
Utilidades de prueba compartidas para FactuMatch Ventas.

Todo es SINTÉTICO: NIT, nombres, CUFE y montos inventados. El contenedor real de
la DIAN tiene datos personales y no se usa en pruebas ni se sube al repositorio.

No define fixtures automáticas: las pruebas de compras no se ven afectadas.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import xmlrpc.client
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

# main.py exige ENCRYPTION_KEY al importarse; en pruebas se usa una clave efímera.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

NIT_EMPRESA = "900000001"
NIT_EMPRESA_DV = "900000001-7"
ENCABEZADOS_DIAN = [
    "Tipo de documento", "CUFE/CUDE", "Folio", "Prefijo", "Divisa", "Forma de Pago", "Medio de Pago",
    "Fecha Emisión", "Fecha Recepción", "NIT Emisor", "Nombre Emisor", "NIT Receptor", "Nombre Receptor",
    "IVA", "ICA", "IC", "INC", "Timbre", "INC Bolsas", "IN Carbono", "IN Combustibles", "IC Datos", "ICL",
    "INPP", "IBUA", "ICUI", "Rete IVA", "Rete Renta", "Rete ICA", "Total", "Estado", "Grupo",
]
ZONA_CO = timezone(timedelta(hours=-5))


def cufe(semilla: str) -> str:
    """CUFE sintético de 96 caracteres hexadecimales (como SHA-384)."""
    return hashlib.sha384(semilla.encode()).hexdigest()


def fila_dian(prefijo: str, folio, total, *, tipo: str = "Factura electrónica", iva=0, inc=0,
              fecha: str = "05-10-2026", recepcion: str | None = None, nit_receptor: str = "222222222222",
              nombre_receptor: str = "Consumidor final", cufe_valor: str | None = None,
              estado: str = "Aprobado", grupo: str = "Emitido", nit_emisor: str = NIT_EMPRESA, **otros) -> dict:
    d, m, a = fecha.split("-")
    return {
        "Tipo de documento": tipo,
        "CUFE/CUDE": cufe_valor if cufe_valor is not None else cufe(f"{prefijo}{folio}{tipo}"),
        "Folio": str(folio),
        "Prefijo": prefijo,
        "Divisa": "COP", "Forma de Pago": "1", "Medio de Pago": "10",
        "Fecha Emisión": fecha,
        "Fecha Recepción": recepcion or f"{a}-{m}-{d}T23:10:00",
        "NIT Emisor": nit_emisor, "Nombre Emisor": "EMPRESA DE PRUEBA SAS",
        "NIT Receptor": nit_receptor, "Nombre Receptor": nombre_receptor,
        "IVA": iva, "INC": inc, "Total": total, "Estado": estado, "Grupo": grupo,
        **otros,
    }


def crear_contenedor(filas: list[dict], *, encabezados: list[str] | None = None, filas_titulo: int = 0,
                     hoja: str = "Rp_Doc_20261015_1501") -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    for i in range(filas_titulo):
        ws.append([f"Reporte de documentos emitidos {i + 1}"])
    columnas = encabezados or ENCABEZADOS_DIAN
    ws.append(columnas)
    for f in filas:
        ws.append([f.get(c, 0 if c in ("ICA", "IC", "Timbre", "INC Bolsas", "IN Carbono", "IN Combustibles",
                                       "IC Datos", "ICL", "INPP", "IBUA", "ICUI", "Rete IVA", "Rete Renta", "Rete ICA") else None)
                   for c in columnas])
        for celda in ws[ws.max_row]:
            if celda.data_type == "f":      # la DIAN exporta texto: «=…» es un valor, no una fórmula
                celda.data_type = "s"
    salida = io.BytesIO()
    wb.save(salida)
    return salida.getvalue()


def utc(fecha_local: str, hora: str = "12:00:00") -> str:
    """Fecha y hora de Colombia → texto UTC como lo devuelve Odoo en date_order."""
    local = datetime.fromisoformat(f"{fecha_local}T{hora}").replace(tzinfo=ZONA_CO)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def movimiento(id_: int, nombre: str, total, *, fecha: str = "2026-10-05", impuestos=0, tipo: str = "out_invoice",
               estado: str = "posted", cufe_valor=False, compania: int = 1, partner=False, diario: str = "Facturas",
               **extra) -> dict:
    return {
        "id": id_, "name": nombre, "move_type": tipo, "state": estado, "invoice_date": fecha,
        "partner_id": partner, "company_id": [compania, f"Compañía {compania}"], "journal_id": [7, diario],
        "amount_total": float(total), "amount_untaxed": float(total) - float(impuestos), "amount_tax": float(impuestos),
        "l10n_co_edi_cufe_cude_ref": cufe_valor, **extra,
    }


def orden_pos(id_: int, numero: str, total, *, fecha: str = "2026-10-05", hora: str = "12:00:00", impuestos=0,
              estado: str = "done", cufe_valor=False, compania: int = 1, factura=False, reembolso: bool = False,
              punto: str = "POS Centro", **extra) -> dict:
    signo = -1 if reembolso else 1
    return {
        "id": id_, "name": f"{punto}/{id_:04d}", "date_order": utc(fecha, hora), "state": estado,
        "partner_id": False, "company_id": [compania, f"Compañía {compania}"],
        "amount_total": signo * float(total), "amount_tax": signo * float(impuestos),
        "account_move": factura, "is_invoiced": bool(factura), "is_refund": reembolso,
        "l10n_co_edi_pos_name": numero, "l10n_co_edi_pos_cufe_cude_ref": cufe_valor,
        "l10n_co_edi_pos_dian_state": "accepted" if cufe_valor else "error",
        "config_id": [3, punto], **extra,
    }


CAMPOS_MOVE_PRUEBA = ["name", "move_type", "state", "invoice_date", "partner_id", "company_id", "journal_id",
                      "amount_total", "amount_untaxed", "amount_tax", "l10n_co_edi_cufe_cude_ref",
                      "l10n_latam_document_number", "reversed_entry_id", "pos_order_ids"]
CAMPOS_POS_PRUEBA = ["name", "date_order", "state", "partner_id", "company_id", "amount_total", "amount_tax",
                     "account_move", "is_invoiced", "is_refund", "refunded_order_id", "l10n_co_edi_pos_name",
                     "l10n_co_edi_pos_cufe_cude_ref", "l10n_co_edi_pos_dian_state", "config_id", "pos_reference"]


def _valor_m2o(v):
    return v[0] if isinstance(v, (list, tuple)) and v and isinstance(v[0], int) else v


def _condicion(c, rec) -> bool:
    campo, op, valor = c
    v = _valor_m2o(rec.get(campo))
    if op == "=":
        return v == valor
    if op == "!=":
        return v != valor
    if op == "in":
        return v in valor
    if op == "not in":
        return v not in valor
    if op in (">=", "<=", "<", ">"):
        if v in (None, False):
            return False
        return {">=": v >= valor, "<=": v <= valor, "<": v < valor, ">": v > valor}[op]
    if op == "ilike":
        return isinstance(v, str) and str(valor).lower() in v.lower()
    if op == "=ilike":   # patrón SQL LIKE completo: % = cualquier texto, _ = un carácter
        patron = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in str(valor))
        return isinstance(v, str) and re.fullmatch(patron, v, flags=re.IGNORECASE | re.DOTALL) is not None
    raise AssertionError(f"Operador no soportado en la prueba: {op}")


def evaluar_dominio(dominio: list, rec: dict) -> bool:
    """Evalúa un dominio de Odoo (notación polaca con '|', '&', '!')."""
    tokens = list(dominio)

    def parsear(i):
        t = tokens[i]
        if t in ("|", "&"):
            a, i = parsear(i + 1)
            b, i = parsear(i)
            return (a or b) if t == "|" else (a and b), i
        if t == "!":
            a, i = parsear(i + 1)
            return (not a), i
        return _condicion(t, rec), i + 1

    i, resultado = 0, True
    while i < len(tokens):
        v, i = parsear(i)
        resultado = resultado and v
    return resultado


class OdooFalso:
    """Odoo de prueba: implementa los métodos de lectura que usa el módulo y registra cada llamada."""

    def __init__(self, movimientos=(), ordenes=(), companias=None, partners=None,
                 campos_move=None, campos_pos=None, pos_instalado: bool = True):
        self.datos = {
            "account.move": [dict(m) for m in movimientos],
            "pos.order": [dict(o) for o in ordenes],
            "res.company": companias if companias is not None else [{"id": 1, "name": "EMPRESA DE PRUEBA SAS", "vat": NIT_EMPRESA_DV}],
            "res.partner": list((partners or {}).values()) if isinstance(partners, dict) else list(partners or []),
        }
        self.campos = {
            "account.move": campos_move if campos_move is not None else CAMPOS_MOVE_PRUEBA,
            "pos.order": campos_pos if campos_pos is not None else CAMPOS_POS_PRUEBA,
            "res.company": ["name", "vat"],
            "res.partner": ["name", "vat", "is_company"],
        }
        self.pos_instalado = pos_instalado
        self.llamadas: list[tuple[str, str]] = []

    def __call__(self, modelo, metodo, args, kwargs):
        self.llamadas.append((modelo, metodo))
        if modelo == "pos.order" and not self.pos_instalado:
            raise xmlrpc.client.Fault(2, "Object pos.order doesn't exist")
        if metodo == "fields_get":
            return {c: {"type": "char", "string": c} for c in self.campos[modelo]}
        if metodo == "search_read":
            dominio = args[0] if args else []
            registros = [r for r in self.datos[modelo] if evaluar_dominio(dominio, r)]
            if kwargs.get("order") == "id desc":
                registros.sort(key=lambda r: r["id"], reverse=True)
            offset, limite = kwargs.get("offset", 0), kwargs.get("limit")
            registros = registros[offset:offset + limite] if limite else registros[offset:]
            return [self._recortar(r, kwargs.get("fields")) for r in registros]
        if metodo == "read":
            ids = args[0]
            return [self._recortar(r, kwargs.get("fields")) for r in self.datos[modelo] if r["id"] in ids]
        if metodo in ("search_count",):
            return len([r for r in self.datos[modelo] if evaluar_dominio(args[0], r)])
        raise AssertionError(f"Método no esperado en Odoo falso: {metodo}")

    @staticmethod
    def _recortar(registro: dict, campos):
        if not campos:
            return dict(registro)
        return {k: v for k, v in registro.items() if k in set(campos) | {"id"}}


def conciliar_sintetico(filas_dian: list[dict], odoo: OdooFalso, desde: date = date(2026, 10, 1),
                        hasta: date = date(2026, 10, 15), tolerancia: str = "1.00", nit: str = NIT_EMPRESA, **kw):
    """Corre lectura → extracción → conjunto canónico → conciliación con datos sintéticos."""
    from decimal import Decimal

    from ventas.canonico import construir_conjunto
    from ventas.dian_emitidos import leer_contenedor
    from ventas.motor import ParametrosConciliacion, conciliar
    from ventas.odoo_ventas import OdooSoloLectura, extraer_ventas

    lectura = leer_contenedor(crear_contenedor(filas_dian), "contenedor_prueba.xlsx")
    lector = OdooSoloLectura(odoo)
    extraccion = extraer_ventas(lector, nit, desde, hasta, **kw)
    conjunto = construir_conjunto(extraccion, desde, hasta, lectura.prefijos_conocidos)
    parametros = ParametrosConciliacion(desde=desde, hasta=hasta, tolerancia_documento=Decimal(tolerancia))
    return conciliar(lectura, extraccion, conjunto, parametros)


@pytest.fixture
def ventas():
    """Fábrica de datos sintéticos para las pruebas del módulo de ventas."""
    return SimpleNamespace(
        NIT_EMPRESA=NIT_EMPRESA, NIT_EMPRESA_DV=NIT_EMPRESA_DV, ENCABEZADOS=ENCABEZADOS_DIAN,
        cufe=cufe, fila=fila_dian, contenedor=crear_contenedor, utc=utc, movimiento=movimiento,
        orden=orden_pos, orden_pos=orden_pos, OdooFalso=OdooFalso, dominio=evaluar_dominio, conciliar=conciliar_sintetico,
    )
