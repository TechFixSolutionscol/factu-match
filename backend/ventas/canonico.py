"""
Conjunto canónico de documentos fiscales de Odoo.

account.move y pos.order no se suman como dos conjuntos independientes:
una orden POS facturada (pos.order.account_move) y su factura representan la
misma operación fiscal y cuentan una sola vez, con trazabilidad de la unión.

Reglas (docs/VENTAS.md, «Deduplicación»):
  1. Factura o nota crédito de cliente (out_invoice/out_refund) → un documento.
  2. Orden POS con account_move leído → se agrupa en ese documento (no suma aparte).
  3. Orden POS con account_move que no está en la ventana leída → fuera de alcance.
  4. Orden POS sin account_move → documento propio (factura o nota crédito según
     is_refund o el signo del total).
  5. Si la orden y su factura tienen CUFE distintos, se conservan las dos y se
     marca alerta de posible duplicidad POS/factura: no se descarta en silencio.
  6. Dos documentos con el mismo CUFE o el mismo número normalizado → alerta de
     posible duplicado. Nunca se elimina un registro por tener igual total o fecha.
  7. En alcance: account.move publicado (state = posted) y orden POS pagada,
     cerrada o facturada (paid/done/invoiced), con fecha local dentro del período.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional

from .normalizacion import (
    CERO,
    NumeroDocumento,
    a_decimal,
    cufe_corto,
    dinero,
    fecha_iso,
    normalizar_cufe,
    normalizar_nit,
    parsear_fecha,
    parsear_numero,
    texto_limpio,
    utc_odoo_a_local,
)
from .odoo_ventas import ExtraccionOdoo

ESTADOS_POS_EN_ALCANCE = {"paid", "done", "invoiced"}
ESTADOS_MOVE_EN_ALCANCE = {"posted"}


def _m2o_id(valor) -> Optional[int]:
    if isinstance(valor, (list, tuple)) and valor and isinstance(valor[0], int):
        return valor[0]
    return None


def _m2o_nombre(valor) -> str:
    if isinstance(valor, (list, tuple)) and len(valor) > 1:
        return texto_limpio(valor[1])
    return ""


def _nombre_asiento(valor) -> str:
    """Nombre visible de un account.move sin la referencia libre que Odoo agrega entre paréntesis."""
    return re.sub(r"\s*\(.*\)\s*$", "", _m2o_nombre(valor))


@dataclass
class DocumentoOdoo:
    modelo: str
    registro_id: int
    nombre: str
    numeros: list[NumeroDocumento]
    cufe: str
    tipo: str
    signo: int
    fecha: Optional[date]
    fecha_hora: Optional[datetime]
    partner_id: Optional[int]
    partner_nombre: str
    partner_vat: str
    total: Decimal
    base: Optional[Decimal]
    impuestos: Optional[Decimal]
    estado: str
    estado_electronico: str
    diario: str
    en_alcance: bool = True
    motivo_alcance: str = ""
    origenes: list[str] = field(default_factory=list)
    relaciones: dict = field(default_factory=dict)
    notas: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"odoo:{self.modelo}:{self.registro_id}"

    @property
    def claves(self) -> list[str]:
        return [n.clave for n in self.numeros]

    @property
    def numero_legible(self) -> str:
        return self.numeros[0].legible if self.numeros else self.nombre

    @property
    def prefijo(self) -> str:
        return self.numeros[0].prefijo if self.numeros else ""

    @property
    def total_firmado(self) -> Decimal:
        return self.total * self.signo

    def a_dict(self) -> dict:
        return {
            "ref": self.ref,
            "modelo": self.modelo,
            "id": self.registro_id,
            "nombre": self.nombre,
            "numeros": [n.legible for n in self.numeros],
            "cufe_corto": cufe_corto(self.cufe),
            "tipo": self.tipo,
            "fecha": fecha_iso(self.fecha),
            "partner": self.partner_nombre,
            "partner_nit": self.partner_vat,
            "total": dinero(self.total),
            "base": dinero(self.base) if self.base is not None else None,
            "impuestos": dinero(self.impuestos) if self.impuestos is not None else None,
            "estado": self.estado,
            "estado_electronico": self.estado_electronico,
            "diario": self.diario,
            "en_alcance": self.en_alcance,
            "motivo_alcance": self.motivo_alcance,
            "origenes": self.origenes,
            "relaciones": self.relaciones,
            "notas": self.notas,
        }


@dataclass
class ConjuntoCanonico:
    documentos: list[DocumentoOdoo]
    fuera_de_alcance: list[DocumentoOdoo]
    pos_agrupadas: int
    trazabilidad: list[dict]
    alertas: list[dict]

    def a_dict(self) -> dict:
        return {
            "documentos_en_alcance": len(self.documentos),
            "fuera_de_alcance": len(self.fuera_de_alcance),
            "pos_agrupadas_con_factura": self.pos_agrupadas,
            "alertas": self.alertas,
        }


def _numeros(textos: Iterable, prefijos_conocidos: set[str]) -> list[NumeroDocumento]:
    vistos: dict[str, NumeroDocumento] = {}
    for t in textos:
        n = parsear_numero(t, prefijos_conocidos)
        if n and n.clave not in vistos:
            vistos[n.clave] = n
    return list(vistos.values())


def _partner(extraccion: ExtraccionOdoo, partner_id: Optional[int]) -> tuple[str, str]:
    p = extraccion.partners.get(partner_id or -1, {})
    return texto_limpio(p.get("name")), normalizar_nit(p.get("vat"))


def construir_conjunto(extraccion: ExtraccionOdoo, desde: date, hasta: date,
                       prefijos_conocidos: Optional[set[str]] = None) -> ConjuntoCanonico:
    prefijos = set(prefijos_conocidos or ())
    trazabilidad: list[dict] = []
    alertas: list[dict] = []
    documentos: list[DocumentoOdoo] = []
    fuera: list[DocumentoOdoo] = []
    por_move: dict[int, DocumentoOdoo] = {}

    for m in extraccion.movimientos:
        tipo = "nota_credito" if m.get("move_type") == "out_refund" else "factura"
        fecha = parsear_fecha(m.get("invoice_date"))
        partner_id = _m2o_id(m.get("partner_id"))
        nombre_p, vat_p = _partner(extraccion, partner_id)
        estado = texto_limpio(m.get("state"))
        doc = DocumentoOdoo(
            modelo="account.move",
            registro_id=m["id"],
            nombre=texto_limpio(m.get("name")),
            numeros=_numeros([m.get("name"), m.get("l10n_latam_document_number")], prefijos),
            cufe=normalizar_cufe(m.get("l10n_co_edi_cufe_cude_ref")),
            tipo=tipo,
            signo=-1 if tipo == "nota_credito" else 1,
            fecha=fecha,
            fecha_hora=None,
            partner_id=partner_id,
            partner_nombre=nombre_p or texto_limpio(m.get("invoice_partner_display_name")) or _m2o_nombre(m.get("partner_id")),
            partner_vat=vat_p,
            total=abs(a_decimal(m.get("amount_total"))),
            base=abs(a_decimal(m.get("amount_untaxed"))) if "amount_untaxed" in m else None,
            impuestos=abs(a_decimal(m.get("amount_tax"))) if "amount_tax" in m else None,
            estado=estado,
            estado_electronico=texto_limpio(m.get("l10n_co_dian_state") or m.get("edi_state") or ""),
            diario=_m2o_nombre(m.get("journal_id")),
            origenes=[f"account.move:{m['id']}"],
            relaciones={k: v for k, v in {
                "factura_original": _nombre_asiento(m.get("reversed_entry_id")) or None,
                "factura_original_id": _m2o_id(m.get("reversed_entry_id")),
            }.items() if v},
        )
        if estado not in ESTADOS_MOVE_EN_ALCANCE:
            doc.en_alcance, doc.motivo_alcance = False, f"Estado contable «{estado or 'vacío'}» (solo cuentan publicados)"
        elif fecha is None:
            doc.en_alcance, doc.motivo_alcance = False, "Sin fecha de factura"
        elif not (desde <= fecha <= hasta):
            doc.en_alcance, doc.motivo_alcance = False, f"Fecha {fecha.isoformat()} fuera del período"
        por_move[m["id"]] = doc

    pos_agrupadas = 0
    for o in extraccion.ordenes_pos:
        fecha_hora = utc_odoo_a_local(o.get("date_order"))
        fecha = fecha_hora.date() if fecha_hora else None
        total_firmado = a_decimal(o.get("amount_total"))
        es_reembolso = bool(o.get("is_refund")) or total_firmado < 0
        tipo = "nota_credito" if es_reembolso else "factura"
        cufe_pos = normalizar_cufe(o.get("l10n_co_edi_pos_cufe_cude_ref"))
        move_id = _m2o_id(o.get("account_move"))
        partner_id = _m2o_id(o.get("partner_id"))
        nombre_p, vat_p = _partner(extraccion, partner_id)
        estado = texto_limpio(o.get("state"))
        ref_pos = f"pos.order:{o['id']}"

        if move_id and move_id in por_move:
            factura = por_move[move_id]
            if cufe_pos and factura.cufe and cufe_pos != factura.cufe:
                alertas.append({
                    "codigo": "pos_factura_cufe_distinto",
                    "mensaje": f"La orden POS {texto_limpio(o.get('name'))} y su factura {factura.nombre} tienen CUFE distintos: se conservan ambas",
                    "refs": [f"odoo:{ref_pos}", factura.ref],
                })
            else:
                factura.origenes.append(ref_pos)
                factura.relaciones.setdefault("ordenes_pos", []).append(texto_limpio(o.get("name")))
                if not factura.cufe and cufe_pos:
                    factura.cufe = cufe_pos
                    factura.notas.append("CUFE tomado de la orden POS vinculada")
                pos_agrupadas += 1
                trazabilidad.append({"accion": "agrupado", "pos": ref_pos, "con": factura.ref,
                                     "motivo": "La orden POS está facturada en este account.move: cuenta una sola vez"})
                continue

        doc = DocumentoOdoo(
            modelo="pos.order",
            registro_id=o["id"],
            nombre=texto_limpio(o.get("l10n_co_edi_pos_name")) or texto_limpio(o.get("name")),
            numeros=_numeros([o.get("l10n_co_edi_pos_name")], prefijos),
            cufe=cufe_pos,
            tipo=tipo,
            signo=-1 if es_reembolso else 1,
            fecha=fecha,
            fecha_hora=fecha_hora,
            partner_id=partner_id,
            partner_nombre=nombre_p or _m2o_nombre(o.get("partner_id")),
            partner_vat=vat_p,
            total=abs(total_firmado),
            base=None,
            impuestos=abs(a_decimal(o.get("amount_tax"))) if "amount_tax" in o else None,
            estado=estado,
            estado_electronico=texto_limpio(o.get("l10n_co_edi_pos_dian_state") or ""),
            diario=_m2o_nombre(o.get("config_id")) or _m2o_nombre(o.get("session_id")),
            origenes=[ref_pos],
            relaciones={k: v for k, v in {
                "orden_original": _m2o_nombre(o.get("refunded_order_id")) or None,
                "factura": _nombre_asiento(o.get("account_move")) or None,
                "referencia": texto_limpio(o.get("pos_reference")) or None,
            }.items() if v},
        )
        if doc.base is None and doc.impuestos is not None:
            doc.base = doc.total - doc.impuestos
        if move_id and move_id not in por_move:
            doc.en_alcance = False
            doc.motivo_alcance = (f"Facturada en {_nombre_asiento(o.get('account_move')) or move_id}, "
                                  "fuera de la ventana leída: la venta fiscal es esa factura")
        elif estado not in ESTADOS_POS_EN_ALCANCE:
            doc.en_alcance, doc.motivo_alcance = False, f"Estado POS «{estado or 'vacío'}» (cuentan pagadas, cerradas o facturadas)"
        elif fecha is None:
            doc.en_alcance, doc.motivo_alcance = False, "Sin fecha de orden"
        elif not (desde <= fecha <= hasta):
            doc.en_alcance, doc.motivo_alcance = False, f"Fecha {fecha.isoformat()} (hora Colombia) fuera del período"
        (documentos if doc.en_alcance else fuera).append(doc)

    for doc in por_move.values():
        (documentos if doc.en_alcance else fuera).append(doc)

    alertas.extend(_alertas_duplicados(documentos))
    return ConjuntoCanonico(documentos=documentos, fuera_de_alcance=fuera, pos_agrupadas=pos_agrupadas,
                            trazabilidad=trazabilidad, alertas=alertas)


def _alertas_duplicados(documentos: list[DocumentoOdoo]) -> list[dict]:
    alertas = []
    por_cufe: dict[str, list[DocumentoOdoo]] = defaultdict(list)
    por_clave: dict[str, list[DocumentoOdoo]] = defaultdict(list)
    for d in documentos:
        if d.cufe:
            por_cufe[d.cufe].append(d)
        for c in d.claves:
            por_clave[c].append(d)
    for cufe, docs in por_cufe.items():
        if len(docs) > 1:
            alertas.append({"codigo": "cufe_repetido_odoo",
                            "mensaje": f"{len(docs)} registros de Odoo comparten el CUFE {cufe_corto(cufe)}",
                            "refs": [d.ref for d in docs]})
    for clave, docs in por_clave.items():
        if len({d.ref for d in docs}) > 1:
            alertas.append({"codigo": "numero_repetido_odoo",
                            "mensaje": f"{len(docs)} registros de Odoo comparten el número {clave.replace('|', '-')}",
                            "refs": [d.ref for d in docs]})
    return alertas


def total_firmado(docs: Iterable[DocumentoOdoo]) -> Decimal:
    return sum((d.total_firmado for d in docs), CERO)
