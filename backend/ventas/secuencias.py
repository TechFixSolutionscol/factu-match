"""
Análisis determinístico de numeración por prefijo.

Trabaja solo con los folios numéricos del contenedor. Las notas crédito de los
POS comparten numeración con las facturas, así que se cuentan como folios
presentes (no como huecos).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from .dian_emitidos import DocumentoDian


def analizar_secuencias(documentos: Iterable[DocumentoDian], prefijo: Optional[str] = None,
                        max_bloques: int = 50, max_folios_listados: int = 500) -> list[dict]:
    """
    Por prefijo: rango, documentos presentes por tipo, folios ausentes y bloques
    de ausencias con el contexto de los folios vecinos (fecha y hora de recepción).

    Recorre solo los folios presentes (ordenados): el costo no depende del tamaño
    de los saltos, así un folio atípico (p. ej. 990000001) no bloquea el análisis.
    """
    por_prefijo: dict[str, dict[int, DocumentoDian]] = defaultdict(dict)
    for d in documentos:
        if not d.numero or not d.numero.prefijo or d.numero.folio_entero is None:
            continue
        if d.categoria not in ("factura", "nota_credito", "nota_debito"):
            continue
        if prefijo and d.numero.prefijo != prefijo.upper():
            continue
        por_prefijo[d.numero.prefijo][d.numero.folio_entero] = d

    resultado = []
    for pref, folios in sorted(por_prefijo.items()):
        presentes = sorted(folios)
        por_tipo: dict[str, int] = defaultdict(int)
        for d in folios.values():
            por_tipo[d.categoria] += 1

        bloques: list[dict] = []
        bloque_mayor: Optional[dict] = None
        total_bloques = 0
        ausentes = 0
        folios_ausentes: list[int] = []
        ausentes_por_fecha: dict[str, int] = defaultdict(int)
        for anterior, siguiente in zip(presentes, presentes[1:]):
            if siguiente - anterior <= 1:
                continue
            desde, hasta = anterior + 1, siguiente - 1
            cantidad = hasta - desde + 1
            ausentes += cantidad
            total_bloques += 1
            doc_anterior = folios[anterior]
            clave = doc_anterior.fecha_emision.isoformat() if doc_anterior.fecha_emision else "sin fecha"
            ausentes_por_fecha[clave] += cantidad
            bloque = {"desde": desde, "hasta": hasta, "cantidad": cantidad,
                      "anterior": _contexto(doc_anterior), "siguiente": _contexto(folios[siguiente])}
            if len(bloques) < max_bloques:
                bloques.append(bloque)
            if bloque_mayor is None or cantidad > bloque_mayor["cantidad"]:
                bloque_mayor = bloque
            cupo = max_folios_listados - len(folios_ausentes)
            if cupo > 0:
                folios_ausentes.extend(range(desde, min(hasta, desde + cupo - 1) + 1))

        resultado.append({
            "prefijo": pref,
            "folio_minimo": presentes[0],
            "folio_maximo": presentes[-1],
            "documentos": len(presentes),
            "por_tipo": dict(por_tipo),
            "notas_credito_en_rango": sorted(n for n, d in folios.items() if d.categoria == "nota_credito"),
            "ausentes": ausentes,
            "folios_ausentes": folios_ausentes,
            "bloques": bloques,
            "bloques_totales": total_bloques,
            "bloque_mayor": bloque_mayor,
            "ausentes_por_fecha": dict(sorted(ausentes_por_fecha.items())),
        })
    return resultado


def _contexto(doc: Optional[DocumentoDian]) -> Optional[dict]:
    if not doc:
        return None
    return {
        "numero": doc.numero_legible,
        "tipo": doc.categoria,
        "fecha_emision": doc.fecha_emision.isoformat() if doc.fecha_emision else None,
        "fecha_recepcion": doc.fecha_recepcion.isoformat(sep=" ") if doc.fecha_recepcion else None,
    }
