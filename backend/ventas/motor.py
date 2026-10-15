"""
Motor de conciliación de ventas: individual (documento a documento) y global.

Estados estables (códigos en inglés, etiquetas en español):
  matched, missing_in_odoo, missing_in_dian, amount_mismatch, tax_mismatch,
  identity_mismatch, possible_duplicate, ambiguous, scope_difference,
  needs_review, excluded.

Identificación (docs/VENTAS.md, «Identificación»):
  - Ruta 1: CUFE/CUDE igual en ambas fuentes.
  - Ruta 2: número normalizado (prefijo + folio) igual en ambas fuentes.
  - CUFE coincide y número no → identity_mismatch (no se oculta el conflicto).
  - Número coincide y CUFE distinto → identity_mismatch.
  - Varios candidatos por número → ambiguous; varios por CUFE → possible_duplicate.
  - Un registro de Odoo ya emparejado no se vuelve a usar: el segundo documento
    DIAN queda como possible_duplicate.

Conciliación global: cada documento DIAN elegible y cada documento canónico de
Odoo en alcance cae en exactamente un componente, así que
    Δ neta = Σ componentes + residuo
y el residuo solo puede ser distinto de cero si algún documento quedó sin
componente (se reporta, nunca se fuerza).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .canonico import ConjuntoCanonico, DocumentoOdoo
from .dian_emitidos import DocumentoDian, LecturaContenedor
from .normalizacion import CERO, cufe_corto, dinero, fecha_iso, nits_equivalentes
from .odoo_ventas import ExtraccionOdoo
from .secuencias import analizar_secuencias

ETIQUETAS_ESTADO = {
    "matched": "Coincide",
    "missing_in_odoo": "Falta en Odoo",
    "missing_in_dian": "Falta en DIAN",
    "amount_mismatch": "Diferencia de valor",
    "tax_mismatch": "Diferencia de impuestos",
    "identity_mismatch": "Identidad contradictoria",
    "possible_duplicate": "Posible duplicado",
    "ambiguous": "Ambiguo",
    "scope_difference": "Diferencia de alcance",
    "needs_review": "Revisar",
    "excluded": "Excluido",
}

COMPONENTES = (
    ("missing_in_odoo", "+", "Documentos DIAN sin coincidencia en Odoo"),
    ("missing_in_dian", "−", "Documentos de Odoo sin reporte DIAN"),
    ("amount_mismatch", "±", "Diferencias de valor en documentos emparejados"),
    ("rounding_within_tolerance", "±", "Redondeos dentro de la tolerancia por documento"),
    ("credit_note_treatment", "±", "Notas crédito con tipo o signo distinto"),
    ("identity_mismatch", "±", "Documentos con identidad contradictoria"),
    ("possible_duplicate", "−", "Duplicidades (POS/factura o registros repetidos)"),
    ("ambiguous", "±", "Documentos ambiguos (varios candidatos)"),
    ("scope_difference", "±", "Diferencias de alcance (fecha, estado, corte del contenedor)"),
)

_PRIORIDAD = ["identity_mismatch", "possible_duplicate", "ambiguous", "amount_mismatch",
              "tax_mismatch", "needs_review", "matched"]


@dataclass
class ParametrosConciliacion:
    desde: date
    hasta: date
    tolerancia_documento: Decimal = Decimal("1.00")
    incluir_move: bool = True
    incluir_pos: bool = True
    criterio_fecha: str = "Fecha Emisión (DIAN) ↔ invoice_date (account.move) / date_order en hora de Colombia (pos.order)"

    def a_dict(self) -> dict:
        return {
            "desde": self.desde.isoformat(),
            "hasta": self.hasta.isoformat(),
            "tolerancia_documento": dinero(self.tolerancia_documento),
            "diferencia_global": "exacta, sin tolerancia",
            "fuentes": {"account.move": self.incluir_move, "pos.order": self.incluir_pos},
            "criterio_fecha": self.criterio_fecha,
        }


@dataclass
class FilaConciliacion:
    id: str
    estado: str
    metodo: str
    componente: Optional[str]
    dian: Optional[DocumentoDian] = None
    odoo: Optional[DocumentoOdoo] = None
    candidatos: list[DocumentoOdoo] = field(default_factory=list)
    diferencia_total: Optional[Decimal] = None
    diferencia_impuestos: Optional[Decimal] = None
    validaciones: list[dict] = field(default_factory=list)
    evidencia: list[str] = field(default_factory=list)

    @property
    def contribucion(self) -> Decimal:
        """Aporte de la fila a Δ neta = DIAN firmado − Odoo firmado (0 si la fila no entra en Δ)."""
        aporte = CERO
        if self.componente is None:
            return aporte
        if self.dian is not None and self.dian.incluido:
            aporte += self.dian.total_firmado
        if self.odoo is not None and self.odoo.en_alcance:
            aporte -= self.odoo.total_firmado
        return aporte

    @property
    def prefijo(self) -> str:
        if self.dian is not None and self.dian.numero:
            return self.dian.numero.prefijo
        if self.odoo is not None:
            return self.odoo.prefijo
        return ""

    @property
    def fecha(self) -> Optional[date]:
        if self.dian is not None and self.dian.fecha_emision:
            return self.dian.fecha_emision
        return self.odoo.fecha if self.odoo is not None else None

    def a_dict_compacta(self) -> dict:
        """Fila liviana para la tabla de detalle (la evidencia completa se pide por documento)."""
        d = self.dian
        o = self.odoo
        return {
            "id": self.id,
            "estado": self.estado,
            "metodo": self.metodo,
            "componente": self.componente,
            "numero": d.numero_legible if d else (o.numero_legible if o else ""),
            "prefijo": self.prefijo,
            "tipo": (d.categoria if d else o.tipo if o else ""),
            "fecha": fecha_iso(self.fecha),
            "receptor_nit": d.nit_receptor if d else (o.partner_vat if o else ""),
            "receptor_nombre": d.nombre_receptor if d else (o.partner_nombre if o else ""),
            "total_dian": dinero(d.total) if d else None,
            "iva_dian": dinero(d.iva) if d else None,
            "inc_dian": dinero(d.inc) if d else None,
            "odoo_ref": o.ref if o else None,
            "odoo_nombre": o.nombre if o else (self.candidatos[0].nombre if self.candidatos else None),
            "total_odoo": dinero(o.total) if o else None,
            "diferencia_total": dinero(self.diferencia_total) if self.diferencia_total is not None else None,
            "diferencia_impuestos": dinero(self.diferencia_impuestos) if self.diferencia_impuestos is not None else None,
            "nota": self.evidencia[0] if self.evidencia else "",
        }

    def a_dict(self) -> dict:
        d = self.dian
        o = self.odoo
        return {
            "id": self.id,
            "estado": self.estado,
            "etiqueta_estado": ETIQUETAS_ESTADO.get(self.estado, self.estado),
            "metodo": self.metodo,
            "componente": self.componente,
            "numero": d.numero_legible if d else (o.numero_legible if o else ""),
            "prefijo": self.prefijo,
            "tipo": (d.categoria if d else o.tipo if o else ""),
            "tipo_documento": d.tipo_documento if d else ("Nota crédito (Odoo)" if o and o.tipo == "nota_credito" else "Factura (Odoo)"),
            "fecha": fecha_iso(self.fecha),
            "receptor_nit": d.nit_receptor if d else (o.partner_vat if o else ""),
            "receptor_nombre": d.nombre_receptor if d else (o.partner_nombre if o else ""),
            "dian": None if d is None else {
                "ref": d.ref, "cufe_corto": cufe_corto(d.cufe),
                "total": dinero(d.total), "iva": dinero(d.iva), "inc": dinero(d.inc),
                "impuestos": dinero(d.impuestos), "base_derivada": dinero(d.base_derivada),
                "fecha_recepcion": d.fecha_recepcion.isoformat(sep=" ") if d.fecha_recepcion else None,
                "motivo": d.motivo,
            },
            "odoo": None if o is None else {
                "ref": o.ref, "modelo": o.modelo, "id": o.registro_id, "nombre": o.nombre,
                "numeros": [n.legible for n in o.numeros], "cufe_corto": cufe_corto(o.cufe),
                "total": dinero(o.total), "impuestos": dinero(o.impuestos) if o.impuestos is not None else None,
                "fecha": fecha_iso(o.fecha), "estado": o.estado, "estado_electronico": o.estado_electronico,
                "diario": o.diario, "origenes": o.origenes, "en_alcance": o.en_alcance,
                "motivo_alcance": o.motivo_alcance,
            },
            "candidatos": [c.ref for c in self.candidatos],
            "diferencia_total": dinero(self.diferencia_total) if self.diferencia_total is not None else None,
            "diferencia_impuestos": dinero(self.diferencia_impuestos) if self.diferencia_impuestos is not None else None,
            "validaciones": self.validaciones,
            "evidencia": self.evidencia,
        }


@dataclass
class ResultadoConciliacion:
    run_id: str
    generado_en: str
    parametros: ParametrosConciliacion
    lectura: LecturaContenedor
    extraccion: ExtraccionOdoo
    conjunto: ConjuntoCanonico
    filas: list[FilaConciliacion]
    alcance: list[dict]
    totales: dict
    metricas: list[dict]
    descomposicion: dict
    prefijos: list[dict]
    secuencias: list[dict]
    alertas: list[dict]
    fecha_corte: Optional[date]
    hallazgos_ia: Optional[dict] = None
    _indice: Optional[tuple] = field(default=None, repr=False, compare=False)

    def resumen(self) -> dict:
        por_estado = Counter(f.estado for f in self.filas)
        por_metodo = Counter(f.metodo for f in self.filas if f.dian is not None and f.dian.incluido)
        elegibles = [f for f in self.filas if f.dian is not None and f.dian.incluido and f.estado != "excluded"]
        coincidentes = sum(1 for f in elegibles if f.odoo is not None and f.estado != "identity_mismatch")
        return {
            "documentos_dian_elegibles": len(elegibles),
            "documentos_odoo_canonicos": len(self.conjunto.documentos),
            "emparejados": coincidentes,
            "por_estado": {k: por_estado.get(k, 0) for k in ETIQUETAS_ESTADO},
            "por_metodo": dict(por_metodo),
            "diferencia_neta": self.descomposicion["diferencia_neta"],
            "residuo": self.descomposicion["residuo"],
        }

    def a_dict(self, incluir_filas: bool = True, filas_compactas: bool = True) -> dict:
        datos = {
            "run_id": self.run_id,
            "generado_en": self.generado_en,
            "parametros": self.parametros.a_dict(),
            "contenedor": self.lectura.a_dict(),
            "odoo": {**self.extraccion.a_dict(), **self.conjunto.a_dict()},
            "alcance": self.alcance,
            "resumen": self.resumen(),
            "totales": _json_seguro(self.totales),
            "metricas": self.metricas,
            "descomposicion": self.descomposicion,
            "prefijos": self.prefijos,
            "secuencias": self.secuencias,
            "alertas": self.alertas,
            "fecha_corte": fecha_iso(self.fecha_corte),
            "etiquetas_estado": ETIQUETAS_ESTADO,
        }
        if incluir_filas:
            datos["documentos"] = [f.a_dict_compacta() if filas_compactas else f.a_dict() for f in self.filas]
        return datos

    def fila(self, ref: str) -> Optional[FilaConciliacion]:
        """Por id exacto; si no, por ref DIAN u Odoo, prefiriendo la fila que entra en Δ."""
        ref = (ref or "").strip()
        if self._indice is None or self._indice[0] != len(self.filas):
            por_id: dict[str, FilaConciliacion] = {}
            alias: dict[str, FilaConciliacion] = {}
            for f in sorted(self.filas, key=lambda x: x.componente is None):
                por_id.setdefault(f.id, f)
                for r in (f.dian.ref if f.dian else None, f.odoo.ref if f.odoo else None):
                    if r:
                        alias.setdefault(r, f)
            self._indice = (len(self.filas), por_id, alias)
        _, por_id, alias = self._indice
        return por_id.get(ref) or alias.get(ref)


def _json_seguro(valor):
    """Decimal → '0.00', date → ISO; recorre dicts y listas."""
    if isinstance(valor, Decimal):
        return dinero(valor)
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    if isinstance(valor, dict):
        return {k: _json_seguro(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_json_seguro(v) for v in valor]
    return valor


# ──────────────────────────────────────────────
# CONCILIACIÓN
# ──────────────────────────────────────────────

def _indexar(docs: list[DocumentoOdoo]) -> tuple[dict[str, list[DocumentoOdoo]], dict[str, list[DocumentoOdoo]]]:
    por_cufe: dict[str, list[DocumentoOdoo]] = defaultdict(list)
    por_clave: dict[str, list[DocumentoOdoo]] = defaultdict(list)
    for d in docs:
        if d.cufe:
            por_cufe[d.cufe].append(d)
        for c in dict.fromkeys(d.claves):
            por_clave[c].append(d)
    return por_cufe, por_clave


def _validaciones_par(dian: DocumentoDian, odoo: DocumentoOdoo, nit_compania: str, tolerancia: Decimal) -> tuple[list[dict], list[str], bool]:
    """Devuelve validaciones, notas de evidencia y si hay motivo de revisión humana."""
    v: list[dict] = []
    evidencia: list[str] = []
    revisar = False

    v.append({"campo": "NIT emisor", "dian": dian.nit_emisor, "odoo": nit_compania,
              "resultado": "ok" if nits_equivalentes(dian.nit_emisor, nit_compania) else "diferente"})

    if dian.consumidor_final:
        v.append({"campo": "NIT receptor", "dian": dian.nit_receptor, "odoo": odoo.partner_vat or "—",
                  "resultado": "no_aplica", "nota": "Consumidor final: no se exige NIT del receptor"})
    elif not odoo.partner_vat:
        v.append({"campo": "NIT receptor", "dian": dian.nit_receptor, "odoo": "—",
                  "resultado": "no_disponible", "nota": "El contacto de Odoo no tiene NIT"})
    else:
        igual = nits_equivalentes(dian.nit_receptor, odoo.partner_vat)
        v.append({"campo": "NIT receptor", "dian": dian.nit_receptor, "odoo": odoo.partner_vat,
                  "resultado": "ok" if igual else "diferente"})
        if not igual:
            revisar = True
            evidencia.append("El NIT del receptor en DIAN no coincide con el del contacto en Odoo")

    tipo_igual = (dian.categoria == "nota_credito") == (odoo.tipo == "nota_credito")
    v.append({"campo": "Tipo", "dian": dian.categoria, "odoo": odoo.tipo, "resultado": "ok" if tipo_igual else "diferente"})

    if dian.fecha_emision and odoo.fecha:
        dias = (odoo.fecha - dian.fecha_emision).days
        v.append({"campo": "Fecha", "dian": fecha_iso(dian.fecha_emision), "odoo": fecha_iso(odoo.fecha),
                  "resultado": "ok" if dias == 0 else "diferente",
                  **({"nota": f"{abs(dias)} día(s) de diferencia"} if dias else {})})
        if abs(dias) > 3:
            revisar = True
            evidencia.append(f"La fecha difiere {abs(dias)} días entre DIAN y Odoo")

    dif_total = dian.total - odoo.total
    v.append({"campo": "Total", "dian": dinero(dian.total), "odoo": dinero(odoo.total),
              "resultado": "ok" if dif_total == 0 else ("redondeo" if abs(dif_total) <= tolerancia else "diferente"),
              "diferencia": dinero(dif_total)})

    if odoo.impuestos is not None:
        dif_imp = dian.impuestos - odoo.impuestos
        v.append({"campo": "Impuestos (IVA + INC + otros)", "dian": dinero(dian.impuestos), "odoo": dinero(odoo.impuestos),
                  "resultado": "ok" if dif_imp == 0 else ("redondeo" if abs(dif_imp) <= tolerancia else "diferente"),
                  "diferencia": dinero(dif_imp),
                  "nota": "IVA e INC se comparan por separado solo con un mapeo de impuestos confirmado"})
    else:
        v.append({"campo": "Impuestos", "dian": dinero(dian.impuestos), "odoo": "—", "resultado": "no_disponible"})

    if odoo.estado_electronico:
        v.append({"campo": "Estado electrónico", "dian": dian.estado_dian, "odoo": odoo.estado_electronico, "resultado": "informativo"})
    return v, evidencia, revisar


def _peor(estados: list[str]) -> str:
    for e in _PRIORIDAD:
        if e in estados:
            return e
    return "matched"


def conciliar(lectura: LecturaContenedor, extraccion: ExtraccionOdoo, conjunto: ConjuntoCanonico,
              parametros: ParametrosConciliacion) -> ResultadoConciliacion:
    tolerancia = parametros.tolerancia_documento
    nit_compania = extraccion.a_dict()["compania"]["nit"]
    fecha_corte_dt = lectura.max_fecha_recepcion()
    fecha_corte = fecha_corte_dt.date() if fecha_corte_dt else None

    elegibles: list[DocumentoDian] = []
    filas: list[FilaConciliacion] = []
    fuera_periodo_dian: list[DocumentoDian] = []
    ids_usados: set[str] = set()

    def id_unico(d: DocumentoDian, principal: bool) -> str:
        """Las filas que entran en Δ usan dian:PREFIJO-FOLIO; las demás (o un número repetido), dian:filaN."""
        candidato = d.ref if principal else f"dian:fila{d.fila}"
        if candidato in ids_usados:
            candidato = f"dian:fila{d.fila}"
        ids_usados.add(candidato)
        return candidato

    for d in lectura.documentos:
        if not d.incluido:
            filas.append(FilaConciliacion(id=id_unico(d, False), estado="excluded",
                                          metodo="unmatched", componente=None, dian=d, evidencia=[d.motivo]))
        elif d.fecha_emision is None or not (parametros.desde <= d.fecha_emision <= parametros.hasta):
            fuera_periodo_dian.append(d)
        else:
            elegibles.append(d)

    en_alcance = conjunto.documentos
    por_cufe, por_clave = _indexar(en_alcance)
    fuera_cufe, fuera_clave = _indexar(conjunto.fuera_de_alcance)
    emparejado: dict[str, str] = {}          # ref Odoo → ref DIAN
    pareja_dian: dict[str, DocumentoDian] = {}
    candidatos_ambiguos: set[str] = set()
    duplicados_odoo: set[str] = set()

    elegibles.sort(key=lambda d: (d.fecha_emision or date.min, d.prefijo,
                                  d.numero.folio_entero if d.numero and d.numero.folio_entero is not None else 0))

    def emparejar(fila: FilaConciliacion, d: DocumentoDian, x: DocumentoOdoo, estados: list[str]) -> None:
        emparejado[x.ref] = d.ref
        pareja_dian[x.ref] = d
        fila.odoo = x
        validaciones, notas, revisar = _validaciones_par(d, x, nit_compania, tolerancia)
        fila.validaciones = validaciones
        fila.evidencia.extend(notas)
        fila.diferencia_total = d.total - x.total
        if x.impuestos is not None:
            fila.diferencia_impuestos = d.impuestos - x.impuestos
        tipo_distinto = (d.categoria == "nota_credito") != (x.tipo == "nota_credito")
        if tipo_distinto:
            estados.append("identity_mismatch")
            fila.evidencia.append(f"Tipo distinto: DIAN {d.categoria}, Odoo {x.tipo}")
        if not nits_equivalentes(d.nit_emisor, nit_compania):
            estados.append("identity_mismatch")
            fila.evidencia.append("El NIT emisor no corresponde a la compañía de Odoo")
        if abs(fila.diferencia_total) > tolerancia:
            estados.append("amount_mismatch")
        elif fila.diferencia_impuestos is not None and abs(fila.diferencia_impuestos) > tolerancia:
            estados.append("tax_mismatch")
        if revisar:
            estados.append("needs_review")
        fila.estado = _peor(estados)
        if fila.estado == "identity_mismatch":
            fila.componente = "credit_note_treatment" if tipo_distinto else "identity_mismatch"
        elif abs(fila.diferencia_total) > tolerancia:
            fila.componente = "amount_mismatch"
        else:
            fila.componente = "rounding_within_tolerance"
            if fila.diferencia_total:
                fila.evidencia.append(
                    f"Diferencia de {dinero(fila.diferencia_total)} dentro de la tolerancia de {dinero(tolerancia)}")

    pares: list[tuple[DocumentoDian, FilaConciliacion]] = []

    # Pasada 1: CUFE/CUDE, la identificación más fuerte.
    for d in elegibles:
        fila = FilaConciliacion(id=id_unico(d, True), estado="missing_in_odoo", metodo="unmatched",
                                componente="missing_in_odoo", dian=d)
        pares.append((d, fila))
        c_cufe = por_cufe.get(d.cufe, []) if d.cufe else []
        if len(c_cufe) > 1:
            fila.estado, fila.metodo, fila.componente = "possible_duplicate", "cufe", "possible_duplicate"
            fila.candidatos = list(c_cufe)
            fila.evidencia.append(f"{len(c_cufe)} registros de Odoo comparten el CUFE {cufe_corto(d.cufe)}")
            duplicados_odoo.update(c.ref for c in c_cufe)
        elif len(c_cufe) == 1:
            x = c_cufe[0]
            if x.ref in emparejado:
                fila.estado, fila.metodo, fila.componente = "possible_duplicate", "cufe", "possible_duplicate"
                fila.candidatos = [x]
                fila.evidencia.append(f"El registro {x.nombre} ya está emparejado con {emparejado[x.ref]}")
                continue
            estados: list[str] = []
            numero_coincide = bool(d.numero and d.numero.clave in x.claves)
            fila.metodo = "both" if numero_coincide else "cufe"
            if d.numero and x.numeros and not numero_coincide:
                estados.append("identity_mismatch")
                fila.evidencia.append(
                    f"El CUFE coincide con {x.nombre}, pero su número "
                    f"({', '.join(n.legible for n in x.numeros)}) no es {d.numero.legible}")
            otros = [c for c in (por_clave.get(d.numero.clave, []) if d.numero else []) if c.ref != x.ref]
            if otros:
                fila.evidencia.append("Otro registro de Odoo usa el mismo número: " + ", ".join(o.nombre for o in otros))
            emparejar(fila, d, x, estados)

    # Pasada 2: número normalizado, solo con registros de Odoo todavía libres.
    for d, fila in pares:
        if fila.odoo is not None or fila.estado != "missing_in_odoo":
            continue
        c_num = por_clave.get(d.numero.clave, []) if d.numero else []
        libres = [c for c in c_num if c.ref not in emparejado and c.ref not in duplicados_odoo]
        if len(libres) == 1:
            x = libres[0]
            estados = []
            fila.metodo = "invoice_number"
            if x.cufe and d.cufe and x.cufe != d.cufe:
                estados.append("identity_mismatch")
                fila.evidencia.append(
                    f"El número coincide con {x.nombre}, pero el CUFE es distinto "
                    f"({cufe_corto(x.cufe)} en Odoo, {cufe_corto(d.cufe)} en DIAN)")
            elif not x.cufe:
                fila.evidencia.append("Identificado solo por número: el registro de Odoo no tiene CUFE")
            emparejar(fila, d, x, estados)
        elif len(libres) > 1:
            fila.estado, fila.metodo, fila.componente = "ambiguous", "invoice_number", "ambiguous"
            fila.candidatos = list(libres)
            fila.evidencia.append(
                f"{len(libres)} registros de Odoo tienen el número {d.numero.legible}: no se elige uno al azar")
            candidatos_ambiguos.update(c.ref for c in libres)
        elif c_num:
            fila.candidatos = list(c_num)
            fila.metodo = "invoice_number"
            mismo_numero = [c for c in c_num if c.ref in pareja_dian and pareja_dian[c.ref].numero
                            and pareja_dian[c.ref].numero.clave == d.numero.clave]
            if mismo_numero:
                fila.estado, fila.componente = "possible_duplicate", "possible_duplicate"
                fila.evidencia.append(
                    f"El número {d.numero.legible} ya está emparejado con {mismo_numero[0].nombre} "
                    f"({pareja_dian[mismo_numero[0].ref].ref}): el contenedor DIAN repite el número")
            elif all(c.ref in duplicados_odoo for c in c_num):
                fila.estado, fila.componente = "possible_duplicate", "possible_duplicate"
                fila.evidencia.append(
                    f"El número {d.numero.legible} existe en Odoo ({', '.join(c.nombre for c in c_num)}), "
                    "en registros que comparten CUFE con otros: posible duplicado")
            else:
                fila.estado, fila.componente = "identity_mismatch", "identity_mismatch"
                fila.evidencia.append(
                    f"El número {d.numero.legible} existe en Odoo ({', '.join(c.nombre for c in c_num)}), "
                    "pero ese registro ya corresponde a otro documento DIAN: "
                    + ", ".join(sorted({emparejado[c.ref] for c in c_num if c.ref in emparejado})))
        else:
            fuera = (fuera_cufe.get(d.cufe, []) if d.cufe else []) or (fuera_clave.get(d.numero.clave, []) if d.numero else [])
            if fuera:
                fila.estado, fila.componente = "scope_difference", "scope_difference"
                fila.candidatos = list(fuera)
                fila.metodo = "cufe" if d.cufe and fuera[0].cufe == d.cufe else "invoice_number"
                fila.evidencia.extend(f"En Odoo existe {o.nombre}: {o.motivo_alcance}" for o in fuera[:3])
            else:
                fila.evidencia.append("No hay ningún registro de Odoo con este CUFE ni con este número en la ventana leída")

    filas.extend(f for _, f in pares)

    # Documentos de Odoo en alcance sin pareja
    dian_fuera_cufe = {d.cufe: d for d in fuera_periodo_dian if d.cufe}
    dian_fuera_clave = {d.numero.clave: d for d in fuera_periodo_dian if d.numero}
    excluidos_cufe = {d.cufe: d for d in lectura.excluidos if d.cufe}
    pos_con_factura = {r for a in conjunto.alertas if a.get("codigo") == "pos_factura_cufe_distinto"
                       for r in a.get("refs", []) if r.startswith("odoo:pos.order:")}
    for o in en_alcance:
        if o.ref in emparejado:
            continue
        fila = FilaConciliacion(id=o.ref, estado="missing_in_dian", metodo="unmatched", componente="missing_in_dian", odoo=o)
        par_fuera = dian_fuera_cufe.get(o.cufe) if o.cufe else None
        if par_fuera is None:
            par_fuera = next((dian_fuera_clave[c] for c in o.claves if c in dian_fuera_clave), None)
        if o.ref in duplicados_odoo:
            fila.estado, fila.componente = "possible_duplicate", "possible_duplicate"
            fila.evidencia.append("Comparte CUFE con otro registro de Odoo")
        elif o.ref in candidatos_ambiguos:
            fila.estado, fila.componente = "ambiguous", "ambiguous"
            fila.evidencia.append("Es uno de varios candidatos para un mismo documento DIAN")
        elif o.ref in pos_con_factura:
            fila.estado, fila.componente = "possible_duplicate", "possible_duplicate"
            fila.evidencia.append(f"Orden POS facturada en {o.relaciones.get('factura') or 'otra factura'} con un CUFE distinto: "
                                  "posible doble registro de la misma venta")
        elif par_fuera is not None:
            fila.estado, fila.componente = "scope_difference", "scope_difference"
            fila.evidencia.append(f"En la DIAN aparece como {par_fuera.numero_legible} con fecha {fecha_iso(par_fuera.fecha_emision)}, fuera del período")
        elif o.cufe and o.cufe in excluidos_cufe:
            ex = excluidos_cufe[o.cufe]
            fila.estado, fila.componente = "scope_difference", "scope_difference"
            fila.evidencia.append(f"En la DIAN el CUFE corresponde a un documento excluido: {ex.motivo}")
        elif fecha_corte and o.fecha and o.fecha > fecha_corte:
            fila.estado, fila.componente = "scope_difference", "scope_difference"
            fila.evidencia.append(f"Posterior al corte del contenedor ({fecha_corte_dt.isoformat(sep=' ')})")
        else:
            if fecha_corte and o.fecha == fecha_corte:
                fila.evidencia.append("Día del corte del contenedor: la DIAN puede no haberlo recibido aún")
            if o.estado_electronico:
                fila.evidencia.append(f"Estado electrónico en Odoo: {o.estado_electronico}")
            if not o.cufe:
                fila.evidencia.append("El registro de Odoo no tiene CUFE")
        filas.append(fila)

    # DIAN elegible por tipo pero fuera del período elegido
    for d in fuera_periodo_dian:
        filas.append(FilaConciliacion(id=id_unico(d, False), estado="excluded", metodo="unmatched", componente=None, dian=d,
                                      evidencia=[f"Fecha de emisión {fecha_iso(d.fecha_emision)} fuera del período elegido"]))

    totales = _totales(elegibles, en_alcance)
    descomposicion = _descomponer(filas, totales)
    secuencias = analizar_secuencias(elegibles)
    alcance = _alcance(lectura, extraccion, conjunto, parametros, nit_compania, fecha_corte_dt, filas)
    alertas = list(conjunto.alertas) + [
        {"codigo": "odoo_advertencia", "mensaje": a, "refs": []} for a in extraccion.advertencias
    ]
    resultado = ResultadoConciliacion(
        run_id=uuid.uuid4().hex[:12],
        generado_en=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        parametros=parametros,
        lectura=lectura,
        extraccion=extraccion,
        conjunto=conjunto,
        filas=filas,
        alcance=alcance,
        totales=totales,
        metricas=_metricas(totales),
        descomposicion=descomposicion,
        prefijos=_por_prefijo(filas, secuencias),
        secuencias=secuencias,
        alertas=alertas,
        fecha_corte=fecha_corte,
    )
    return resultado


# ──────────────────────────────────────────────
# TOTALES Y DESCOMPOSICIÓN
# ──────────────────────────────────────────────

def _suma(valores) -> Decimal:
    return sum(valores, CERO)


def _totales(dian: list[DocumentoDian], odoo: list[DocumentoOdoo]) -> dict:
    def bloque_dian(docs: list[DocumentoDian]) -> dict:
        return {
            "cantidad": len(docs),
            "total": _suma(d.total for d in docs),
            "base_derivada": _suma(d.base_derivada for d in docs),
            "iva": _suma(d.iva for d in docs),
            "inc": _suma(d.inc for d in docs),
            "otros_impuestos": _suma(d.total_otros_impuestos for d in docs),
            "impuestos": _suma(d.impuestos for d in docs),
        }

    def bloque_odoo(docs: list[DocumentoOdoo]) -> dict:
        return {
            "cantidad": len(docs),
            "total": _suma(d.total for d in docs),
            "base": _suma(d.base for d in docs if d.base is not None),
            "impuestos": _suma(d.impuestos for d in docs if d.impuestos is not None),
            "sin_impuestos": sum(1 for d in docs if d.impuestos is None),
        }

    df = bloque_dian([d for d in dian if d.categoria == "factura"])
    dd = bloque_dian([d for d in dian if d.categoria == "nota_debito"])
    dc = bloque_dian([d for d in dian if d.categoria == "nota_credito"])
    of = bloque_odoo([d for d in odoo if d.tipo == "factura"])
    oc = bloque_odoo([d for d in odoo if d.tipo == "nota_credito"])

    def neto(f: dict, deb: Optional[dict], c: dict, campos: tuple) -> dict:
        r = {}
        for k in campos:
            r[k] = f[k] + (deb[k] if deb else CERO) - c[k]
        return r

    dian_neto = neto(df, dd, dc, ("total", "base_derivada", "iva", "inc", "otros_impuestos", "impuestos"))
    odoo_neto = neto(of, None, oc, ("total", "base", "impuestos"))
    return {
        "dian": {"facturas": df, "notas_debito": dd, "notas_credito": dc, "neto": dian_neto},
        "odoo": {"facturas": of, "notas_credito": oc, "neto": odoo_neto},
    }


def _porcentaje(diferencia: Decimal, base: Decimal) -> Optional[str]:
    if not base:
        return None
    valor = (diferencia / base * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{abs(valor) if valor == 0 else valor}"  # sin «-0.00»


def _metricas(t: dict) -> list[dict]:
    d, o = t["dian"], t["odoo"]
    filas = [
        ("cantidad_facturas", "Facturas (cantidad)", Decimal(d["facturas"]["cantidad"] + d["notas_debito"]["cantidad"]), Decimal(o["facturas"]["cantidad"]), True, ""),
        ("cantidad_notas_credito", "Notas crédito (cantidad)", Decimal(d["notas_credito"]["cantidad"]), Decimal(o["notas_credito"]["cantidad"]), True, ""),
        ("total_bruto", "Total bruto facturas", d["facturas"]["total"] + d["notas_debito"]["total"], o["facturas"]["total"], False, "Incluye notas débito del lado DIAN" if d["notas_debito"]["cantidad"] else ""),
        ("total_notas_credito", "Notas crédito y devoluciones", d["notas_credito"]["total"], o["notas_credito"]["total"], False, ""),
        ("total_neto", "Total neto (facturas − notas crédito)", d["neto"]["total"], o["neto"]["total"], False, ""),
        ("base", "Base (DIAN derivada) vs amount_untaxed", d["neto"]["base_derivada"], o["neto"]["base"], False, "La base DIAN se deriva: Total − impuestos"),
        ("impuestos", "Impuestos totales (IVA + INC + otros)", d["neto"]["impuestos"], o["neto"]["impuestos"], False, "Odoo: amount_tax"),
        ("iva", "IVA neto", d["neto"]["iva"], None, False, "Odoo: requiere mapeo de impuestos confirmado"),
        ("inc", "INC neto", d["neto"]["inc"], None, False, "Odoo: requiere mapeo de impuestos confirmado"),
        ("otros_impuestos", "Otros impuestos (ICA, IBUA, ICUI…)", d["neto"]["otros_impuestos"], None, False, "Odoo: requiere mapeo de impuestos confirmado"),
    ]
    salida = []
    for codigo, etiqueta, vd, vo, es_cantidad, nota in filas:
        diferencia = (vd - vo) if vo is not None else None
        salida.append({
            "codigo": codigo,
            "etiqueta": etiqueta,
            "dian": str(int(vd)) if es_cantidad else dinero(vd),
            "odoo": None if vo is None else (str(int(vo)) if es_cantidad else dinero(vo)),
            "diferencia": None if diferencia is None else (str(int(diferencia)) if es_cantidad else dinero(diferencia)),
            "diferencia_porcentual": None if diferencia is None else _porcentaje(diferencia, vd),
            "nota": nota,
        })
    return salida


def _descomponer(filas: list[FilaConciliacion], totales: dict) -> dict:
    delta = totales["dian"]["neto"]["total"] - totales["odoo"]["neto"]["total"]
    por_componente: dict[str, list[FilaConciliacion]] = defaultdict(list)
    for f in filas:
        if f.componente:
            por_componente[f.componente].append(f)
    componentes = []
    explicado = CERO
    montos = {}
    for codigo, _, _ in COMPONENTES:
        montos[codigo] = _suma(f.contribucion for f in por_componente.get(codigo, []))
    movimiento_bruto = _suma(abs(m) for m in montos.values())
    for codigo, signo, etiqueta in COMPONENTES:
        fs = por_componente.get(codigo, [])
        monto = montos[codigo]
        explicado += monto
        componentes.append({
            "codigo": codigo,
            "signo": signo,
            "etiqueta": etiqueta,
            "documentos": len(fs),
            "con_diferencia": sum(1 for f in fs if f.contribucion != 0),
            "monto": dinero(monto),
            # Peso sobre el movimiento bruto (|componentes|): los componentes pueden
            # compensarse entre sí, así que no se expresan como % de Δ.
            "participacion": _porcentaje(abs(monto), movimiento_bruto) if movimiento_bruto else None,
        })
    residuo = delta - explicado
    return {
        "diferencia_neta": dinero(delta),
        "explicado": dinero(explicado),
        "residuo": dinero(residuo),
        "cuadra": residuo == 0,
        "formula": "Δ neta (DIAN − Odoo) = Σ componentes + residuo",
        "componentes": componentes,
    }


def _por_prefijo(filas: list[FilaConciliacion], secuencias: list[dict]) -> list[dict]:
    grupos: dict[str, dict] = {}
    seq = {s["prefijo"]: s for s in secuencias}
    for f in filas:
        if f.estado == "excluded":
            continue
        pref = f.prefijo or "SIN NÚMERO"
        g = grupos.setdefault(pref, {"prefijo": pref, "facturas_dian": 0, "notas_credito_dian": 0,
                                     "neto_dian": CERO, "neto_odoo": CERO, "estados": Counter(), "diarios": Counter()})
        if f.dian is not None and f.dian.incluido:
            g["neto_dian"] += f.dian.total_firmado
            if f.dian.categoria == "nota_credito":
                g["notas_credito_dian"] += 1
            else:
                g["facturas_dian"] += 1
        if f.odoo is not None and f.odoo.en_alcance:
            g["neto_odoo"] += f.odoo.total_firmado
            if f.odoo.diario:
                g["diarios"][f.odoo.diario] += 1
        g["estados"][f.estado] += 1
    salida = []
    for pref, g in sorted(grupos.items(), key=lambda kv: -abs(kv[1]["neto_dian"])):
        s = seq.get(pref)
        salida.append({
            "prefijo": pref,
            "origen": g["diarios"].most_common(1)[0][0] if g["diarios"] else None,
            "facturas_dian": g["facturas_dian"],
            "notas_credito_dian": g["notas_credito_dian"],
            "neto_dian": dinero(g["neto_dian"]),
            "neto_odoo": dinero(g["neto_odoo"]),
            "diferencia": dinero(g["neto_dian"] - g["neto_odoo"]),
            "estados": dict(g["estados"]),
            "folio_minimo": s["folio_minimo"] if s else None,
            "folio_maximo": s["folio_maximo"] if s else None,
            "ausentes_en_secuencia": s["ausentes"] if s else 0,
        })
    return salida


def _alcance(lectura: LecturaContenedor, extraccion: ExtraccionOdoo, conjunto: ConjuntoCanonico,
             p: ParametrosConciliacion, nit_compania: str, corte: Optional[datetime], filas: list[FilaConciliacion]) -> list[dict]:
    nits = lectura.nits_emisores()
    empresa_ok = len(nits) == 1 and nits_equivalentes(next(iter(nits)), nit_compania)
    fuera_periodo = sum(1 for f in filas if f.estado == "excluded" and f.dian is not None and f.dian.incluido)
    campos_faltantes = extraccion.campos_no_disponibles
    claves_identidad = [c for c in ("l10n_co_edi_cufe_cude_ref", "l10n_co_edi_pos_cufe_cude_ref", "l10n_co_edi_pos_name")
                        if c in campos_faltantes.get("account.move", []) + campos_faltantes.get("pos.order", [])]
    posteriores_corte = sum(1 for f in filas if f.estado == "scope_difference" and any("corte" in e for e in f.evidencia))
    desde_c, hasta_c = lectura.rango_fechas()
    cubre = bool(desde_c and hasta_c and desde_c <= p.desde and hasta_c >= p.hasta)
    pedidas = {"account.move": p.incluir_move, "pos.order": p.incluir_pos}
    leidas = [m for m, pedida in pedidas.items() if pedida and extraccion.fuentes.get(m)]
    no_leidas = [m for m, pedida in pedidas.items() if pedida and not extraccion.fuentes.get(m)]
    no_pedidas = [m for m, pedida in pedidas.items() if not pedida]
    detalle_fuentes = ("Leídas: " + (", ".join(leidas) or "ninguna")
                       + (f" · NO se pudo leer {', '.join(no_leidas)}: sus documentos DIAN aparecerán como faltantes en Odoo"
                          if no_leidas else "")
                       + (f" · excluida por elección: {', '.join(no_pedidas)}" if no_pedidas else ""))
    return [
        {"codigo": "fuentes", "ok": not no_leidas, "etiqueta": "Fuentes de Odoo", "detalle": detalle_fuentes},
        {"codigo": "empresa", "ok": empresa_ok, "etiqueta": "Empresa",
         "detalle": f"NIT emisor {', '.join(sorted(nits))} · compañía Odoo {nit_compania or 'sin NIT'}"},
        {"codigo": "periodo", "ok": cubre, "etiqueta": "Período y fecha",
         "detalle": f"{p.desde.isoformat()} → {p.hasta.isoformat()} en ambas fuentes, hora de Colombia"
                    + (f" · {fuera_periodo} documentos DIAN fuera del período" if fuera_periodo else "")
                    + ("" if cubre else f" · el contenedor solo trae documentos del {fecha_iso(desde_c)} al {fecha_iso(hasta_c)}: "
                                        "en los días que no cubre, lo de Odoo aparecerá como faltante en DIAN")},
        {"codigo": "tipos", "ok": True, "etiqueta": "Tipos documentales",
         "detalle": "Facturas, notas débito y notas crédito; documento soporte, nómina y acuses excluidos por regla"},
        {"codigo": "estados", "ok": True, "etiqueta": "Estados",
         "detalle": "DIAN aprobados · Odoo publicados y POS pagados, cerrados o facturados"},
        {"codigo": "notas_credito", "ok": True, "etiqueta": "Notas crédito",
         "detalle": "Restan del neto en ambas fuentes, separadas del bruto"},
        {"codigo": "deduplicacion", "ok": True, "etiqueta": "Deduplicación",
         "detalle": f"{conjunto.pos_agrupadas} órdenes POS agrupadas con su factura (cuentan una vez)"},
        {"codigo": "identificacion", "ok": not claves_identidad, "etiqueta": "Campos de identificación",
         "detalle": "CUFE y número disponibles en Odoo" if not claves_identidad
                    else "Faltan en Odoo: " + ", ".join(claves_identidad) + " (se identifica solo con lo disponible)"},
        {"codigo": "corte", "ok": posteriores_corte == 0, "etiqueta": "Corte del contenedor",
         "detalle": (f"Última recepción DIAN {corte.isoformat(sep=' ')}" if corte else "Sin fecha de recepción")
                    + (f" · {posteriores_corte} documentos de Odoo posteriores al corte" if posteriores_corte else "")},
    ]
