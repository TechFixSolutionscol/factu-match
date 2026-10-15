"""
Herramientas de SOLO LECTURA para el agente investigador.

Reglas comunes:
- Parámetros tipados y validados; no existe una herramienta de consulta libre.
- Resultados acotados (máximo 25 casos o 10 coincidencias por fuente) y
  recortados a ~14 000 caracteres.
- Datos minimizados: NIT enmascarados (***123), sin nombres de clientes ni
  CUFE completos.
- Las consultas en vivo a Odoo pasan por OdooSoloLectura (lista blanca de
  métodos de lectura y modelos), con dominios armados por el backend y
  restringidos a la compañía conciliada.
- Cada herramienta devuelve identificadores de evidencia (ref y evidencia_id).
  Solo esos identificadores se aceptan después como evidencia de un hallazgo.
"""

from __future__ import annotations

import json
import re
import time
import xmlrpc.client
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any, Callable, Optional

from ..canonico import DocumentoOdoo
from ..dian_emitidos import DocumentoDian
from ..motor import COMPONENTES, ETIQUETAS_ESTADO, FilaConciliacion, ResultadoConciliacion
from ..normalizacion import (
    CERO,
    a_decimal_seguro,
    cufe_corto,
    dinero,
    enmascarar_nit,
    es_consumidor_final,
    es_probable_persona_juridica,
    fecha_iso,
    nits_equivalentes,
    normalizar_cufe,
    normalizar_folio,
    normalizar_nit,
    normalizar_prefijo,
    parsear_fecha,
    parsear_numero,
    texto_limpio,
    utc_odoo_a_local,
)
from ..odoo_ventas import AccesoNoPermitido, OdooSoloLectura
from .esquema import recolectar_importes

LIMITE_CASOS = 25
LIMITE_BUSQUEDA = 10
LIMITE_CARACTERES = 14000
CODIGOS_COMPONENTE = [c[0] for c in COMPONENTES]
ESTADOS = list(ETIQUETAS_ESTADO)
_SEVERIDAD = {"identity_mismatch": 1, "possible_duplicate": 2, "missing_in_odoo": 3, "missing_in_dian": 4,
              "amount_mismatch": 5, "ambiguous": 6, "tax_mismatch": 7, "scope_difference": 8, "needs_review": 9}
_RE_EVIDENCIA = re.compile(
    r"^(?:dian:\S+|odoo:(?:account\.move|pos\.order):\d+|componente:\w+|secuencia:\S+|prefijo:\S+|"
    r"impuestos:\S+|resumen|plan_revision)$")
_RE_REF_ODOO = re.compile(r"^odoo:(account\.move|pos\.order):(\d+)$")
_TIPOS_VENTA = ["out_invoice", "out_refund"]
# Campos de una lectura en vivo que pueden llegar a Claude (lista blanca). Sin «ref» ni nombres
# de contactos; los many2one a account.move se envían solo como ref (su nombre visible puede
# incluir la referencia libre del asiento).
_CAMPOS_VIVO = {
    "account.move": ("name", "move_type", "state", "invoice_date", "invoice_date_due", "amount_total",
                     "amount_untaxed", "amount_tax", "amount_total_signed", "l10n_co_edi_cufe_cude_ref",
                     "l10n_latam_document_number", "edi_state", "l10n_co_dian_state",
                     "l10n_co_dian_commercial_state", "l10n_co_edi_type", "journal_id", "reversed_entry_id",
                     "pos_order_ids"),
    "pos.order": ("name", "date_order", "state", "amount_total", "amount_tax", "account_move", "is_invoiced",
                  "is_refund", "refunded_order_id", "l10n_co_edi_pos_name", "l10n_co_edi_pos_cufe_cude_ref",
                  "l10n_co_edi_pos_dian_state", "l10n_co_edi_pos_dian_date", "session_id", "config_id",
                  "pos_reference"),
}
_REFS_M2O = {"account_move": "account.move", "reversed_entry_id": "account.move", "refunded_order_id": "pos.order"}
_CAMPOS_NUMERO_VIVO = {"account.move": ("name", "l10n_latam_document_number"), "pos.order": ("l10n_co_edi_pos_name",)}
_CAMPO_CUFE_VIVO = {"account.move": "l10n_co_edi_cufe_cude_ref", "pos.order": "l10n_co_edi_pos_cufe_cude_ref"}
LIMITE_VIVO = 50


class ParametroInvalido(ValueError):
    """Parámetro fuera de lo permitido por la herramienta."""


class TiempoAgotado(RuntimeError):
    """Se venció el plazo de la investigación antes de una consulta a Odoo."""


# ──────────────────────────────────────────────
# DEFINICIONES (lo que ve Claude)
# ──────────────────────────────────────────────

_FECHA = {"type": "string", "description": "AAAA-MM-DD"}

DEFINICIONES: list[dict] = [
    {
        "name": "obtener_resumen",
        "description": (
            "Resumen calculado por el backend: alcance comparado (empresa, período, tipos, estados, corte del "
            "contenedor), totales DIAN vs Odoo, descomposición exacta de la diferencia neta por componente "
            "(Δ = Σ componentes + residuo), tabla por prefijo y alertas. Evidencias: «resumen», "
            "«componente:<código>», «prefijo:<PREFIJO>»."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "listar_casos",
        "description": (
            "Lista documentos conciliados filtrando por componente de la diferencia, estado, prefijo o fechas. "
            "Devuelve el total del filtro, la suma de su aporte a la diferencia, agrupaciones por prefijo y por "
            "fecha y hasta 25 casos con su ref (dian:… u odoo:…). NIT enmascarados y sin nombres."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "componente": {"type": "string", "enum": CODIGOS_COMPONENTE},
                "estado": {"type": "string", "enum": ESTADOS},
                "prefijo": {"type": "string", "description": "Prefijo DIAN, p. ej. POS"},
                "fecha_desde": _FECHA,
                "fecha_hasta": _FECHA,
                "orden": {"type": "string", "enum": ["impacto", "fecha", "folio"]},
                "limite": {"type": "integer", "description": "1 a 25 (por defecto 15)"},
                "desplazamiento": {"type": "integer", "description": "Para paginar"},
            },
        },
    },
    {
        "name": "investigar_diferencia",
        "description": (
            "Evidencia completa de un caso: datos DIAN y Odoo comparables, validaciones campo a campo (NIT, "
            "tipo, fecha, total, impuestos, estado electrónico), candidatos, método de identificación, "
            "componente y aporte a la diferencia."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ref"],
            "properties": {"ref": {"type": "string", "description": "dian:PREFIJO-FOLIO u odoo:modelo:id"}},
        },
    },
    {
        "name": "buscar_documento",
        "description": (
            "Búsqueda alternativa antes de concluir que un documento falta: por ref de un caso (usa su CUFE y "
            "número internamente), por número (acepta POS6730, POS-6730, FE/2026/00300), por folio en "
            "cualquier prefijo, o documentos del mismo receptor de un caso. Busca en el contenedor DIAN completo "
            "(también excluidos y fuera del período), en todo lo leído de Odoo (cualquier estado) y, si hay "
            "conexión, en Odoo en vivo sin restricción de fecha ni estado. Máximo 10 resultados por fuente."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string"},
                "numero": {"type": "string"},
                "folio": {"type": "string", "description": "Solo dígitos"},
                "mismo_receptor_que": {"type": "string", "description": "ref de un caso"},
            },
        },
    },
    {
        "name": "consultar_documento_odoo",
        "description": (
            "Lee en vivo (solo lectura) los campos autorizados de un registro de Odoo de la compañía conciliada "
            "y sus relaciones: factura de una orden POS, órdenes POS de una factura, factura revertida u orden "
            "reembolsada. Sin conexión devuelve lo leído durante la conciliación."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ref"],
            "properties": {"ref": {"type": "string", "description": "odoo:account.move:ID u odoo:pos.order:ID"}},
        },
    },
    {
        "name": "analizar_impuestos",
        "description": (
            "Compara impuestos DIAN (IVA, INC y otras columnas) con amount_tax de Odoo para un documento, un "
            "prefijo o el total. IVA e INC de Odoo solo se separan con un mapeo de impuestos confirmado; hoy se "
            "compara el total de impuestos."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ref": {"type": "string"}, "prefijo": {"type": "string"}},
        },
    },
    {
        "name": "analizar_secuencias",
        "description": (
            "Saltos de numeración de un prefijo en el contenedor DIAN (bloques de folios ausentes con el "
            "documento anterior y el siguiente), folios repetidos, y si los folios ausentes existen en Odoo "
            "(en alcance o fuera), con su estado, estado electrónico y si tienen CUFE."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prefijo"],
            "properties": {"prefijo": {"type": "string"}},
        },
    },
    {
        "name": "generar_plan_revision",
        "description": (
            "Plan priorizado determinístico: casos pendientes agrupados (bloques de folios consecutivos con el "
            "mismo estado y prefijo) y ordenados por severidad e impacto."),
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limite": {"type": "integer", "description": "1 a 15 (por defecto 10)"}},
        },
    },
]
NOMBRES_HERRAMIENTAS = [d["name"] for d in DEFINICIONES]
_PARAMETROS = {d["name"]: set(d["input_schema"]["properties"]) for d in DEFINICIONES}


# ──────────────────────────────────────────────
# MINIMIZACIÓN DE DATOS
# ──────────────────────────────────────────────

def _receptor(nit: str) -> dict:
    n = normalizar_nit(nit)
    if not n:
        return {"tipo": "sin_nit"}
    if es_consumidor_final(n):
        return {"tipo": "consumidor_final"}
    return {"tipo": "probable_persona_juridica" if es_probable_persona_juridica(n) else "otro", "nit": enmascarar_nit(n)}


def _dian(d: DocumentoDian) -> dict:
    return {
        "ref": d.ref,
        "numero": d.numero_legible,
        "tipo_documento": d.tipo_documento,
        "categoria": d.categoria,
        "incluido": d.incluido,
        "motivo": d.motivo,
        "cufe": cufe_corto(d.cufe) or None,
        "fecha_emision": fecha_iso(d.fecha_emision),
        "fecha_recepcion": d.fecha_recepcion.isoformat(sep=" ") if d.fecha_recepcion else None,
        "receptor": _receptor(d.nit_receptor),
        "total": dinero(d.total),
        "iva": dinero(d.iva),
        "inc": dinero(d.inc),
        "otros_impuestos": {k: dinero(v) for k, v in d.otros_impuestos.items() if v},
        "impuestos": dinero(d.impuestos),
        "estado_dian": d.estado_dian,
    }


def _odoo(o: DocumentoOdoo) -> dict:
    return {
        "ref": o.ref,
        "modelo": o.modelo,
        "nombre": o.nombre,
        "numeros": [n.legible for n in o.numeros],
        "cufe": cufe_corto(o.cufe) or None,
        "tipo": o.tipo,
        "fecha": fecha_iso(o.fecha),
        "hora_local": o.fecha_hora.strftime("%H:%M") if o.fecha_hora else None,
        "estado": o.estado,
        "estado_electronico": o.estado_electronico or None,
        "diario_o_punto_de_venta": o.diario or None,
        "total": dinero(o.total),
        "impuestos": dinero(o.impuestos) if o.impuestos is not None else None,
        "en_alcance": o.en_alcance,
        "motivo_alcance": o.motivo_alcance or None,
        "origenes": o.origenes,
        "relaciones": o.relaciones,
        "notas": o.notas,
        "receptor": _receptor(o.partner_vat),
    }


def _caso(f: FilaConciliacion) -> dict:
    d, o = f.dian, f.odoo
    return {
        "ref": f.id,
        "estado": f.estado,
        "componente": f.componente,
        "metodo": f.metodo,
        "numero": d.numero_legible if d else (o.numero_legible if o else ""),
        "prefijo": f.prefijo,
        "tipo": d.categoria if d else (o.tipo if o else ""),
        "fecha": fecha_iso(f.fecha),
        "total_dian": dinero(d.total) if d else None,
        "total_odoo": dinero(o.total) if o else None,
        "aporte": dinero(f.contribucion),
        "odoo": o.ref if o else None,
        "candidatos": [c.ref for c in f.candidatos][:3],
        "nota": f.evidencia[0] if f.evidencia else None,
    }


def _validaciones_minimizadas(validaciones: list[dict]) -> list[dict]:
    salida = []
    for v in validaciones:
        v = dict(v)
        if "NIT" in v.get("campo", ""):
            for lado in ("dian", "odoo"):
                if v.get(lado) and v[lado] != "—":
                    v[lado] = enmascarar_nit(v[lado]) or v[lado]
        salida.append(v)
    return salida


def _o(subdominios: list[list]) -> list:
    """OR de subdominios en notación polaca: [[A], ['&', B, C]] → ['|', A, '&', B, C]."""
    if not subdominios:
        return []
    salida: list = ["|"] * (len(subdominios) - 1)
    for sub in subdominios:
        salida.extend(sub)
    return salida


def _patrones_numero(prefijo: str, folio: str) -> list[str]:
    """
    Patrones =ilike anclados al final para un número: el folio con o sin ceros a la izquierda
    (hasta 8 dígitos), pegado al prefijo o tras «/», «-» o espacio. «FE/2026/00005», «FE-5» y
    «FE0005» coinciden con FE|5; «FE/2026/00015» no.
    """
    variantes = [folio.zfill(w) for w in range(len(folio), max(len(folio), 8) + 1)] if folio.isdigit() else [folio]
    patrones: list[str] = []
    for v in dict.fromkeys(variantes):
        patrones.append(f"{prefijo}{v}")
        patrones.extend(f"{prefijo}%{s}{v}" for s in ("/", "-", " "))
    return patrones


def _m2o(valor) -> tuple[Optional[int], str]:
    if isinstance(valor, (list, tuple)) and valor:
        return (valor[0] if isinstance(valor[0], int) else None), (texto_limpio(valor[1]) if len(valor) > 1 else "")
    return None, ""


def _mensaje_seguro(exc: Exception) -> str:
    if isinstance(exc, xmlrpc.client.Fault):
        primera = (exc.faultString or "").strip().splitlines()[-1:] or [""]
        return f"Odoo respondió con error: {primera[0][:160]}"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def _recolectar_evidencias(valor: Any, destino: set) -> None:
    if isinstance(valor, str):
        if _RE_EVIDENCIA.match(valor):
            destino.add(valor)
    elif isinstance(valor, dict):
        for v in valor.values():
            _recolectar_evidencias(v, destino)
    elif isinstance(valor, (list, tuple)):
        for v in valor:
            _recolectar_evidencias(v, destino)


def _recortar(datos: dict, limite: int = LIMITE_CARACTERES) -> tuple[dict, str]:
    """Reduce las listas más largas hasta que el JSON quepa en el límite."""
    texto = json.dumps(datos, ensure_ascii=False, default=str)
    intentos = 0
    while len(texto) > limite and intentos < 12:
        intentos += 1
        candidatos: list[tuple[int, dict, str]] = []
        for k, v in datos.items():
            if isinstance(v, list) and len(v) > 1:
                candidatos.append((len(json.dumps(v, ensure_ascii=False, default=str)), datos, k))
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, list) and len(v2) > 1:
                        candidatos.append((len(json.dumps(v2, ensure_ascii=False, default=str)), v, k2))
        if not candidatos:
            break
        _, contenedor, clave = max(candidatos, key=lambda c: c[0])
        contenedor[clave] = contenedor[clave][: max(1, len(contenedor[clave]) // 2)]
        datos["recortado"] = "Resultado recortado por tamaño: usa filtros o paginación para ver más"
        texto = json.dumps(datos, ensure_ascii=False, default=str)
    if len(texto) > limite:
        datos = {"error": "El resultado es demasiado grande; usa filtros más específicos"}
        texto = json.dumps(datos, ensure_ascii=False)
    return datos, texto


def _entero(valor, nombre: str, minimo: int, maximo: int, defecto: int) -> int:
    if valor is None:
        return defecto
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        raise ParametroInvalido(f"{nombre} debe ser un entero")
    try:
        n = int(valor)
    except ValueError:
        raise ParametroInvalido(f"{nombre} debe ser un entero") from None
    return max(minimo, min(maximo, n))


def _texto_param(valor, nombre: str, patron: str = r"[\w\-/. ]{1,40}") -> str:
    if valor is None:
        return ""
    if not isinstance(valor, str) or not re.fullmatch(patron, valor.strip()):
        raise ParametroInvalido(f"{nombre} no tiene un formato válido")
    return valor.strip()


# ──────────────────────────────────────────────
# INVESTIGACIÓN
# ──────────────────────────────────────────────

class Investigacion:
    """Estado de una investigación: herramientas, límites y evidencia vista."""

    def __init__(self, resultado: ResultadoConciliacion, lector: Optional[OdooSoloLectura] = None,
                 max_llamadas: int = 12, reloj: Callable[[], float] = time.monotonic,
                 fin: Optional[float] = None):
        self.resultado = resultado
        self.lector = lector
        self.max_llamadas = max_llamadas
        self.llamadas = 0
        self.evidencias: set[str] = set()
        self.importes: set[Decimal] = set()
        self.busquedas_alternativas: set[str] = set()
        self.traza: list[dict] = []
        self._reloj = reloj
        self.fin = fin                      # plazo absoluto (según reloj) para ejecutar herramientas
        if lector is not None:
            lector.antes_de_llamar = self._verificar_plazo
        ids = getattr(resultado.extraccion, "companias_ids", None) or [resultado.extraccion.compania.get("id")]
        self._companias = {i for i in ids if isinstance(i, int)}
        self._tolerancia = resultado.parametros.tolerancia_documento
        self._filas: dict[str, FilaConciliacion] = {}
        # Primero las filas que entran en Δ: un número repetido en una fila excluida no las tapa.
        ordenadas = sorted(resultado.filas, key=lambda x: x.componente is None)
        for f in ordenadas:
            self._filas.setdefault(f.id, f)
        for f in ordenadas:
            if f.dian is not None:
                self._filas.setdefault(f.dian.ref, f)
            if f.odoo is not None:
                self._filas.setdefault(f.odoo.ref, f)
        self._filas_minus = {k.lower(): v for k, v in self._filas.items()}
        self._odoo = {d.ref: d for d in resultado.conjunto.documentos + resultado.conjunto.fuera_de_alcance}
        self._prefijos = resultado.lectura.prefijos_conocidos
        self._funciones: dict[str, Callable[..., dict]] = {
            "obtener_resumen": self.obtener_resumen,
            "listar_casos": self.listar_casos,
            "investigar_diferencia": self.investigar_diferencia,
            "buscar_documento": self.buscar_documento,
            "consultar_documento_odoo": self.consultar_documento_odoo,
            "analizar_impuestos": self.analizar_impuestos,
            "analizar_secuencias": self.analizar_secuencias,
            "generar_plan_revision": self.generar_plan_revision,
        }

    # ── control ──

    @property
    def limite_alcanzado(self) -> bool:
        return self.llamadas >= self.max_llamadas

    def _verificar_plazo(self) -> None:
        """Se ejecuta antes de cada consulta a Odoo: una herramienta no sigue consultando fuera de plazo."""
        if self.fin is not None and self._reloj() >= self.fin:
            raise TiempoAgotado("Se agotó el tiempo de la investigación")

    def registrar_contexto(self, datos: Any) -> None:
        """Marca como vista la evidencia entregada a Claude (contexto inicial o herramienta)."""
        _recolectar_evidencias(datos, self.evidencias)
        recolectar_importes(datos, self.importes)

    def precargar(self, nombre: str, **parametros) -> dict:
        """Ejecuta una herramienta para el contexto inicial; no cuenta en el límite de llamadas."""
        datos = self._funciones[nombre](**parametros)
        datos.pop("_resumen", None)
        datos, _ = _recortar(datos)
        self.registrar_contexto(datos)
        return datos

    def ejecutar(self, nombre: str, parametros: Any) -> tuple[str, bool]:
        """Ejecuta una herramienta pedida por Claude. Devuelve (JSON, es_error)."""
        inicio = self._reloj()
        if self.limite_alcanzado:
            datos = {"error": f"Se alcanzó el límite de {self.max_llamadas} llamadas a herramientas. "
                              "Entrega tus conclusiones con la evidencia que ya tienes."}
            self.traza.append({"herramienta": nombre, "parametros": {}, "ok": False, "ms": 0, "resumen": "límite alcanzado"})
            return json.dumps(datos, ensure_ascii=False), True
        if self.fin is not None and self._reloj() >= self.fin:
            datos = {"error": "Se agotó el tiempo de la investigación. Entrega tus conclusiones con la evidencia que ya tienes."}
            self.traza.append({"herramienta": nombre, "parametros": {}, "ok": False, "ms": 0, "resumen": "sin tiempo"})
            return json.dumps(datos, ensure_ascii=False), True
        self.llamadas += 1
        es_error = True
        try:
            funcion = self._funciones.get(nombre)
            if funcion is None:
                raise ParametroInvalido(f"Herramienta desconocida: {nombre}")
            if not isinstance(parametros, dict):
                raise ParametroInvalido("Los parámetros deben ser un objeto")
            extras = set(parametros) - _PARAMETROS[nombre]
            if extras:
                raise ParametroInvalido("Parámetros no permitidos: " + ", ".join(sorted(extras)))
            datos = funcion(**parametros)
            es_error = False
        except ParametroInvalido as exc:
            datos = {"error": str(exc)}
        except AccesoNoPermitido as exc:
            datos = {"error": f"Acceso no permitido: {exc}"}
        except TiempoAgotado:
            datos = {"error": "Se agotó el tiempo de la investigación. Entrega tus conclusiones con la evidencia que ya tienes."}
        except Exception as exc:  # Odoo caído, permisos, etc.: la investigación sigue
            datos = {"error": "La consulta falló: " + _mensaje_seguro(exc)}
        resumen = datos.pop("_resumen", "")
        datos, texto = _recortar(datos)
        if not es_error:
            self.registrar_contexto(datos)
        self.traza.append({
            "herramienta": nombre,
            "parametros": {k: (v if isinstance(v, (int, float, bool)) else str(v)[:60])
                           for k, v in (parametros or {}).items()} if isinstance(parametros, dict) else {},
            "ok": not es_error,
            "ms": int((self._reloj() - inicio) * 1000),
            "resumen": datos.get("error") if es_error else resumen,
        })
        return texto, es_error

    def _fila(self, ref: Any) -> FilaConciliacion:
        r = _texto_param(ref, "ref", r"[\w\-:/. ]{3,80}")
        f = self._filas.get(r) or self._filas_minus.get(r.lower())
        if f is None:
            raise ParametroInvalido(f"No existe el caso {r} en esta conciliación (usa listar_casos o buscar_documento)")
        return f

    def _prefijo(self, valor: Any) -> str:
        p = normalizar_prefijo(_texto_param(valor, "prefijo", r"[A-Za-z0-9]{1,12}"))
        if not p:
            raise ParametroInvalido("prefijo vacío")
        return p

    # ── herramientas ──

    def obtener_resumen(self) -> dict:
        r = self.resultado
        datos = r.a_dict(incluir_filas=False)
        alcance = []
        for a in datos["alcance"]:
            a = dict(a)
            if a["codigo"] == "empresa":
                a["detalle"] = ("El NIT emisor del contenedor coincide con la compañía de Odoo" if a["ok"]
                                else "El NIT emisor del contenedor NO coincide con la compañía de Odoo")
            alcance.append(a)
        desc = datos["descomposicion"]
        alertas = datos["alertas"]
        return {
            "_resumen": f"Δ {desc['diferencia_neta']} · residuo {desc['residuo']}",
            "evidencia_id": "resumen",
            "run_id": r.run_id,
            "parametros": datos["parametros"],
            "compania": datos["odoo"]["compania"]["nombre"],
            "contenedor": {
                "documentos_incluidos": datos["contenedor"]["documentos_incluidos"],
                "documentos_excluidos": datos["contenedor"]["documentos_excluidos"],
                "corte_recepcion": datos["contenedor"]["corte_recepcion"],
                "tipos": [{k: t[k] for k in ("tipo_documento", "filas", "incluido", "tratamiento")}
                          for t in datos["contenedor"]["tipos"]],
            },
            "odoo": {k: datos["odoo"].get(k) for k in ("registros_leidos", "documentos_en_alcance", "fuera_de_alcance",
                                                         "pos_agrupadas_con_factura", "campos_no_disponibles", "advertencias")},
            "alcance": alcance,
            "resumen": datos["resumen"],
            "metricas": datos["metricas"],
            "descomposicion": {**desc, "componentes": [{**c, "evidencia_id": f"componente:{c['codigo']}"}
                                                        for c in desc["componentes"]]},
            "prefijos": [{**{k: v for k, v in p.items() if k != "estados"},
                          "casos_no_coincidentes": sum(n for e, n in p["estados"].items() if e != "matched"),
                          "evidencia_id": f"prefijo:{p['prefijo']}"} for p in datos["prefijos"][:15]],
            "alertas": {
                "total": len(alertas),
                "por_codigo": dict(Counter(a["codigo"] for a in alertas)),
                "primeras": [{"codigo": a["codigo"], "mensaje": a["mensaje"], "refs": a.get("refs", [])[:4]}
                             for a in alertas[:5]],
            },
        }

    def listar_casos(self, componente=None, estado=None, prefijo=None, fecha_desde=None, fecha_hasta=None,
                     orden=None, limite=None, desplazamiento=None) -> dict:
        if componente is not None and componente not in CODIGOS_COMPONENTE:
            raise ParametroInvalido(f"componente debe ser uno de {CODIGOS_COMPONENTE}")
        if estado is not None and estado not in ESTADOS:
            raise ParametroInvalido(f"estado debe ser uno de {ESTADOS}")
        orden = orden or "impacto"
        if orden not in ("impacto", "fecha", "folio"):
            raise ParametroInvalido("orden debe ser impacto, fecha o folio")
        pref = self._prefijo(prefijo) if prefijo else ""
        desde = parsear_fecha(fecha_desde) if fecha_desde else None
        hasta = parsear_fecha(fecha_hasta) if fecha_hasta else None
        if (fecha_desde and desde is None) or (fecha_hasta and hasta is None):
            raise ParametroInvalido("Las fechas deben tener formato AAAA-MM-DD")
        limite = _entero(limite, "limite", 1, LIMITE_CASOS, 15)
        desplazamiento = _entero(desplazamiento, "desplazamiento", 0, 100000, 0)

        filas = [f for f in self.resultado.filas
                 if (estado == "excluded" or f.estado != "excluded")
                 and (componente is None or f.componente == componente)
                 and (estado is None or f.estado == estado)
                 and (not pref or f.prefijo == pref)
                 and (desde is None or (f.fecha is not None and f.fecha >= desde))
                 and (hasta is None or (f.fecha is not None and f.fecha <= hasta))]
        if orden == "impacto":
            filas.sort(key=lambda f: (-abs(f.contribucion), f.id))
        elif orden == "fecha":
            filas.sort(key=lambda f: (fecha_iso(f.fecha) or "", f.prefijo, f.id))
        else:
            filas.sort(key=lambda f: (f.prefijo, _clave_folio(f), f.id))

        por_prefijo: dict[str, dict] = defaultdict(lambda: {"casos": 0, "aporte": CERO})
        por_fecha: dict[str, dict] = defaultdict(lambda: {"casos": 0, "aporte": CERO})
        for f in filas:
            for grupo, clave in ((por_prefijo, f.prefijo or "SIN NÚMERO"), (por_fecha, fecha_iso(f.fecha) or "sin fecha")):
                grupo[clave]["casos"] += 1
                grupo[clave]["aporte"] += f.contribucion
        pagina = filas[desplazamiento: desplazamiento + limite]
        return {
            "_resumen": f"{len(filas)} casos, {len(pagina)} devueltos",
            "filtro": {k: v for k, v in {"componente": componente, "estado": estado, "prefijo": pref or None,
                                          "fecha_desde": fecha_iso(desde), "fecha_hasta": fecha_iso(hasta)}.items() if v},
            "evidencia_id": f"componente:{componente}" if componente else None,
            "total_casos": len(filas),
            "suma_aporte": dinero(sum((f.contribucion for f in filas), CERO)),
            "por_prefijo": {k: {"casos": v["casos"], "aporte": dinero(v["aporte"])}
                            for k, v in sorted(por_prefijo.items(), key=lambda kv: -abs(kv[1]["aporte"]))[:12]},
            "por_fecha": {k: {"casos": v["casos"], "aporte": dinero(v["aporte"])} for k, v in sorted(por_fecha.items())[:40]},
            "desplazamiento": desplazamiento,
            "casos": [_caso(f) for f in pagina],
            "hay_mas": desplazamiento + limite < len(filas),
        }

    def investigar_diferencia(self, ref=None) -> dict:
        f = self._fila(ref)
        return {
            "_resumen": f"{f.id}: {f.estado}",
            "ref": f.id,
            "estado": f.estado,
            "etiqueta_estado": ETIQUETAS_ESTADO.get(f.estado, f.estado),
            "componente": f.componente,
            "metodo": f.metodo,
            "aporte": dinero(f.contribucion),
            "diferencia_total": dinero(f.diferencia_total) if f.diferencia_total is not None else None,
            "diferencia_impuestos": dinero(f.diferencia_impuestos) if f.diferencia_impuestos is not None else None,
            "tolerancia_documento": dinero(self._tolerancia),
            "dian": _dian(f.dian) if f.dian else None,
            "odoo": _odoo(f.odoo) if f.odoo else None,
            "candidatos": [_odoo(c) for c in f.candidatos[:5]],
            "validaciones": _validaciones_minimizadas(f.validaciones),
            "evidencia": f.evidencia,
        }

    def buscar_documento(self, ref=None, numero=None, folio=None, mismo_receptor_que=None) -> dict:
        if not any((ref, numero, folio, mismo_receptor_que)):
            raise ParametroInvalido("Indica ref, numero, folio o mismo_receptor_que")
        cufe = ""
        claves: set[str] = set()              # PREFIJO|FOLIO exactos
        folios_libres: set[str] = set()       # folio en cualquier prefijo
        nit = ""
        criterio = []
        caso: Optional[FilaConciliacion] = None
        if ref:
            caso = self._fila(ref)
            if caso.dian is not None:
                cufe = caso.dian.cufe
                if caso.dian.numero:
                    (claves.add(caso.dian.numero.clave) if caso.dian.numero.prefijo else folios_libres.add(caso.dian.numero.folio))
            elif caso.odoo is not None:
                cufe = caso.odoo.cufe
                for n in caso.odoo.numeros:
                    (claves.add(n.clave) if n.prefijo else folios_libres.add(n.folio))
            criterio.append(f"CUFE y número de {caso.id}")
        if numero:
            n = parsear_numero(_texto_param(numero, "numero"), self._prefijos)
            if n is None:
                raise ParametroInvalido("No se pudo interpretar el número")
            (claves.add(n.clave) if n.prefijo else folios_libres.add(n.folio))
            criterio.append(f"número {n.legible}")
        if folio:
            fo = normalizar_folio(_texto_param(folio, "folio", r"\d{1,12}"))
            folios_libres.add(fo)
            criterio.append(f"folio {fo} en cualquier prefijo")
        if mismo_receptor_que:
            f = self._fila(mismo_receptor_que)
            nit = normalizar_nit(f.dian.nit_receptor if f.dian else (f.odoo.partner_vat if f.odoo else ""))
            if not nit or es_consumidor_final(nit):
                raise ParametroInvalido("El caso no tiene un receptor identificable (consumidor final o sin NIT)")
            criterio.append(f"mismo receptor que {f.id}")

        def coincide(numero_doc) -> bool:
            return bool(numero_doc) and (numero_doc.clave in claves or numero_doc.folio in folios_libres)

        dian = []
        for d in self.resultado.lectura.documentos:
            if (cufe and d.cufe == cufe) or coincide(d.numero) or (nit and nits_equivalentes(d.nit_receptor, nit)):
                fila = next((f for f in self.resultado.filas if f.dian is d), None)
                dian.append({**_dian(d), "fila": fila.id if fila else None,
                             "estado_conciliacion": fila.estado if fila else None})
                if len(dian) >= LIMITE_BUSQUEDA:
                    break

        odoo = []
        for o in self.resultado.conjunto.documentos + self.resultado.conjunto.fuera_de_alcance:
            if ((cufe and o.cufe == cufe) or any(coincide(n) for n in o.numeros)
                    or (nit and o.partner_vat and nits_equivalentes(o.partner_vat, nit))):
                fila = self._filas.get(o.ref)
                odoo.append({**_odoo(o), "estado_conciliacion": fila.estado if fila else None})
                if len(odoo) >= LIMITE_BUSQUEDA:
                    break

        en_vivo: Any
        if self.lector is None:
            en_vivo = {"disponible": False, "motivo": "Sin credenciales de Odoo en esta investigación: "
                                                      "solo se revisó lo leído en la conciliación (± días de margen)"}
        elif not (cufe or claves or folios_libres):
            en_vivo = {"disponible": False, "motivo": "La búsqueda por receptor solo usa lo leído en la conciliación"}
        else:
            en_vivo = self._buscar_en_vivo(cufe, claves, folios_libres)
        completa = bool(en_vivo.get("disponible")) and not en_vivo.get("errores") and not en_vivo.get("truncado")
        if caso is not None and completa:
            # Solo una búsqueda en vivo completa (sin errores ni recortes) cuenta como búsqueda alternativa.
            self.busquedas_alternativas.add(caso.id)
            if caso.dian is not None:
                self.busquedas_alternativas.add(caso.dian.ref)
        return {
            "_resumen": f"DIAN {len(dian)} · Odoo leído {len(odoo)} · en vivo {'completa' if completa else 'no concluyente'}",
            "criterio": "; ".join(criterio),
            "dian": dian,
            "odoo_leido_en_conciliacion": odoo,
            "odoo_en_vivo": en_vivo,
            "busqueda_en_vivo_completa": completa,
            "nota": "Incluye documentos excluidos, fuera del período y en cualquier estado. Solo una búsqueda en "
                    "vivo completa permite descartar que el documento exista en Odoo con otra fecha o estado.",
        }

    def _dominio_compania(self, disponibles: dict) -> list:
        if "company_id" in disponibles and self._companias:
            return [("company_id", "in", sorted(self._companias))]
        return []

    def _buscar_en_vivo(self, cufe: str, claves: set[str], folios_libres: set[str]) -> dict:
        """Consultas armadas por el backend: CUFE exacto o número con patrones anclados (=ilike)."""
        resultados: list[dict] = []
        errores: list[str] = []
        no_instalados: list[str] = []
        truncado = False
        for modelo, base in (("account.move", [("move_type", "in", _TIPOS_VENTA)]), ("pos.order", [])):
            try:
                try:
                    disponibles = self.lector.campos(modelo)
                except xmlrpc.client.Fault as exc:
                    texto = str(exc.faultString or "").lower()
                    if modelo == "pos.order" and ("doesn't exist" in texto or "does not exist" in texto):
                        no_instalados.append(modelo)       # sin POS no puede existir como orden POS
                        continue
                    raise
                campos_numero = [c for c in _CAMPOS_NUMERO_VIVO[modelo] if c in disponibles]
                campo_cufe = _CAMPO_CUFE_VIVO[modelo] if _CAMPO_CUFE_VIVO[modelo] in disponibles else None
                campos = [c for c in _CAMPOS_VIVO[modelo] if c in disponibles]
                consultas: list[tuple[list, int]] = []
                exactas: list[list] = []
                if cufe and campo_cufe:
                    exactas.append([(campo_cufe, "=", cufe)])
                for clave in sorted(claves)[:3]:
                    pref, fol = clave.split("|", 1)
                    for c in campos_numero:
                        exactas.extend([(c, "=ilike", patron)] for patron in _patrones_numero(pref, fol))
                if exactas:
                    consultas.append((exactas, LIMITE_VIVO))
                for fol in sorted(folios_libres)[:2]:
                    amplias = [[(c, "ilike", fol)] for c in campos_numero]
                    if amplias:
                        consultas.append((amplias, 200))
                if not consultas:
                    # El modelo existe pero no tiene campos para buscar este documento: la búsqueda no es completa.
                    errores.append(f"{modelo}: sin campos de número ni CUFE para buscar")
                    continue
                vistos: set[int] = set()
                for subdominios, limite in consultas:
                    dominio = self._dominio_compania(disponibles) + list(base) + _o(subdominios)
                    registros = self.lector.llamar(modelo, "search_read", [dominio],
                                                   {"fields": ["id"] + campos, "limit": limite, "order": "id desc"}) or []
                    truncado = truncado or len(registros) >= limite
                    for rec in registros:
                        if rec["id"] in vistos:
                            continue
                        vistos.add(rec["id"])
                        nums = [parsear_numero(rec.get(c), self._prefijos) for c in campos_numero if rec.get(c)]
                        por_cufe = bool(cufe and campo_cufe and normalizar_cufe(rec.get(campo_cufe)) == cufe)
                        por_numero = any(n and (n.clave in claves or n.folio in folios_libres) for n in nums)
                        if por_cufe or por_numero:
                            resultados.append(self._registro_vivo(modelo, rec, "cufe" if por_cufe else "numero"))
            except AccesoNoPermitido:
                raise
            except Exception as exc:  # p. ej. módulo POS no instalado o sin permiso
                errores.append(f"{modelo}: {_mensaje_seguro(exc)}")
        salida: dict = {"disponible": True, "resultados": resultados[:LIMITE_BUSQUEDA]}
        if no_instalados:
            salida["modelos_no_instalados"] = no_instalados
        if len(resultados) > LIMITE_BUSQUEDA or truncado:
            salida["truncado"] = True
            salida["aviso"] = "Hay más coincidencias de las que se devolvieron: la búsqueda no es concluyente"
        if errores:
            salida["errores"] = errores
        return salida

    def _registro_vivo(self, modelo: str, rec: dict, coincide_por: str = "") -> dict:
        ref = f"odoo:{modelo}:{rec['id']}"
        fila = self._filas.get(ref)
        en_conciliacion = self._odoo.get(ref)
        salida: dict = {
            "ref": ref,
            "modelo": modelo,
            "coincide_por": coincide_por or None,
            "en_esta_conciliacion": ("en alcance" if en_conciliacion and en_conciliacion.en_alcance
                                     else "fuera de alcance" if en_conciliacion else "no leído (fuera de la ventana)"),
            "estado_conciliacion": fila.estado if fila else None,
        }
        for campo in _CAMPOS_VIVO[modelo]:
            if campo not in rec:
                continue
            valor = rec[campo]
            if campo in _REFS_M2O:
                rid, _ = _m2o(valor)
                salida[campo] = f"odoo:{_REFS_M2O[campo]}:{rid}" if rid else None
            elif "cufe" in campo:
                salida[campo] = cufe_corto(normalizar_cufe(valor)) or None
            elif campo.startswith("amount_"):
                salida[campo] = dinero(a_decimal_seguro(valor)[0])
            elif campo == "pos_order_ids":
                ids = [i for i in (valor or []) if isinstance(i, int)]
                salida["pos_order_refs"] = [f"odoo:pos.order:{i}" for i in ids[:10]]
                salida["pos_order_cantidad"] = len(ids)
            elif campo == "date_order":
                local = utc_odoo_a_local(valor)
                salida["fecha_hora_local"] = local.isoformat(sep=" ") if local else None
            elif isinstance(valor, (list, tuple)):
                _, nombre = _m2o(valor)              # diario, sesión o punto de venta
                salida[campo] = nombre or None
            elif valor is False:
                salida[campo] = None
            else:
                salida[campo] = valor
        pid, _ = _m2o(rec.get("partner_id"))
        salida["tiene_cliente"] = bool(pid)
        return salida

    def _leer_venta(self, modelo: str, rid: int) -> Optional[dict]:
        """Lee un registro solo si es una venta (factura o nota crédito de cliente, u orden POS) de la compañía."""
        disponibles = self.lector.campos(modelo)
        dominio = [("id", "=", rid)] + self._dominio_compania(disponibles)
        if modelo == "account.move":
            dominio.append(("move_type", "in", _TIPOS_VENTA))
        campos = [c for c in _CAMPOS_VIVO[modelo] if c in disponibles]
        extra = ["partner_id"] if "partner_id" in disponibles else []
        registros = self.lector.llamar(modelo, "search_read", [dominio],
                                       {"fields": ["id"] + campos + extra, "limit": 1}) or []
        return registros[0] if registros else None

    def consultar_documento_odoo(self, ref=None) -> dict:
        r = _texto_param(ref, "ref", r"odoo:(?:account\.move|pos\.order):\d{1,12}")
        modelo, rid = _RE_REF_ODOO.match(r).groups()
        rid = int(rid)
        if self.lector is None:
            doc = self._odoo.get(r)
            if doc is None:
                raise ParametroInvalido(f"{r} no está en lo leído para esta conciliación y no hay conexión a Odoo")
            fila = self._filas.get(r)
            return {"_resumen": "sin conexión en vivo",
                    "fuente": "Lectura hecha durante la conciliación (sin conexión en vivo)",
                    "registro": {**_odoo(doc), "estado_conciliacion": fila.estado if fila else None}}
        rec = self._leer_venta(modelo, rid)
        if rec is None:
            raise ParametroInvalido(f"{r} no es una factura, nota crédito u orden POS de la compañía conciliada "
                                    "(o no existe, o el usuario no tiene permiso de lectura)")
        principal = self._registro_vivo(modelo, rec)
        pendientes: list[tuple[str, int]] = []
        if modelo == "pos.order":
            for campo, mod_rel in (("account_move", "account.move"), ("refunded_order_id", "pos.order")):
                rel, _ = _m2o(rec.get(campo))
                if rel:
                    pendientes.append((mod_rel, rel))
        else:
            rel, _ = _m2o(rec.get("reversed_entry_id"))
            if rel:
                pendientes.append(("account.move", rel))
            pendientes.extend(("pos.order", i) for i in (rec.get("pos_order_ids") or [])[:5] if isinstance(i, int))
        relacionados = []
        for mod_rel, id_rel in pendientes:
            rr = self._leer_venta(mod_rel, id_rel)
            if rr is not None:
                relacionados.append(self._registro_vivo(mod_rel, rr))
        pid, _ = _m2o(rec.get("partner_id"))
        receptor = {"tipo": "sin_cliente"}
        if pid:
            socio = self.lector.leer("res.partner", [pid], ["vat"])
            receptor = _receptor(socio[0].get("vat") if socio else "")
        return {"_resumen": f"{r} + {len(relacionados)} relacionados", "fuente": "Odoo en vivo (solo lectura)",
                "registro": {**principal, "receptor": receptor}, "relacionados": relacionados}

    def analizar_impuestos(self, ref=None, prefijo=None) -> dict:
        nota = ("El contenedor DIAN trae IVA, INC y otras columnas por separado; en Odoo se compara amount_tax "
                "(total de impuestos). Separar IVA e INC en Odoo requiere un mapeo de impuestos confirmado.")
        if ref:
            f = self._fila(ref)
            d, o = f.dian, f.odoo
            dif = f.diferencia_impuestos
            return {
                "_resumen": f"impuestos de {f.id}",
                "evidencia_id": f"impuestos:{f.id}",
                "ref": f.id,
                "dian": None if d is None else {"iva": dinero(d.iva), "inc": dinero(d.inc),
                                                "otros": {k: dinero(v) for k, v in d.otros_impuestos.items() if v},
                                                "impuestos": dinero(d.impuestos), "base_derivada": dinero(d.base_derivada),
                                                "total": dinero(d.total)},
                "odoo": None if o is None else {"amount_tax": dinero(o.impuestos) if o.impuestos is not None else None,
                                                "base": dinero(o.base) if o.base is not None else None,
                                                "total": dinero(o.total)},
                "diferencia_impuestos": dinero(dif) if dif is not None else None,
                "dentro_de_tolerancia": None if dif is None else abs(dif) <= self._tolerancia,
                "nota": nota,
            }
        pref = self._prefijo(prefijo) if prefijo else ""
        pares = [f for f in self.resultado.filas
                 if f.dian is not None and f.odoo is not None and f.estado != "excluded" and (not pref or f.prefijo == pref)]
        con_impuestos = [f for f in pares if f.odoo.impuestos is not None]
        dian_iva = sum((f.dian.iva * f.dian.signo for f in pares), CERO)
        dian_inc = sum((f.dian.inc * f.dian.signo for f in pares), CERO)
        dian_otros = sum((f.dian.total_otros_impuestos * f.dian.signo for f in pares), CERO)
        dian_imp = sum((f.dian.impuestos * f.dian.signo for f in con_impuestos), CERO)
        odoo_imp = sum((f.odoo.impuestos * f.odoo.signo for f in con_impuestos), CERO)
        fuera_tol = [f for f in con_impuestos if f.diferencia_impuestos is not None and abs(f.diferencia_impuestos) > self._tolerancia]
        fuera_tol.sort(key=lambda f: -abs(f.diferencia_impuestos))
        metricas = [m for m in self.resultado.metricas if m["codigo"] in ("impuestos", "iva", "inc", "otros_impuestos")]
        return {
            "_resumen": f"impuestos {pref or 'global'}",
            "evidencia_id": f"impuestos:{pref or 'global'}",
            "alcance": f"Documentos emparejados{' del prefijo ' + pref if pref else ''} (neto, notas crédito restan)",
            "documentos_emparejados": len(pares),
            "dian": {"iva": dinero(dian_iva), "inc": dinero(dian_inc), "otros": dinero(dian_otros),
                     "impuestos_comparables": dinero(dian_imp)},
            "odoo": {"amount_tax": dinero(odoo_imp), "sin_impuestos_leidos": len(pares) - len(con_impuestos)},
            "diferencia_impuestos": dinero(dian_imp - odoo_imp),
            "documentos_fuera_de_tolerancia": len(fuera_tol),
            "mayores_diferencias": [{"ref": f.id, "numero": f.dian.numero_legible, "dian": dinero(f.dian.impuestos),
                                     "odoo": dinero(f.odoo.impuestos), "diferencia": dinero(f.diferencia_impuestos)}
                                    for f in fuera_tol[:10]],
            "metricas_globales": metricas if not pref else None,
            "nota": nota,
        }

    def analizar_secuencias(self, prefijo=None) -> dict:
        pref = self._prefijo(prefijo)
        s = next((x for x in self.resultado.secuencias if x["prefijo"] == pref), None)
        if s is None:
            disponibles = [x["prefijo"] for x in self.resultado.secuencias]
            raise ParametroInvalido(f"Sin secuencia numérica para {pref}. Prefijos con secuencia: {', '.join(disponibles)}")
        por_folio: dict[int, list[DocumentoOdoo]] = defaultdict(list)
        for o in self.resultado.conjunto.documentos + self.resultado.conjunto.fuera_de_alcance:
            for n in o.numeros:
                if n.prefijo == pref and n.folio_entero is not None:
                    por_folio[n.folio_entero].append(o)
        folios_odoo = sorted(por_folio)
        bloques = []
        for b in s["bloques"][:20]:
            i, j = bisect_left(folios_odoo, b["desde"]), bisect_right(folios_odoo, b["hasta"])
            encontrados = list({o.ref: o for f in folios_odoo[i:j] for o in por_folio[f]}.values())
            aporte = sum((self._filas[o.ref].contribucion for o in encontrados if o.ref in self._filas), CERO)
            bloques.append({
                **b,
                "en_odoo": {
                    "encontrados": len(encontrados),
                    "en_alcance": sum(1 for o in encontrados if o.en_alcance),
                    "sin_cufe": sum(1 for o in encontrados if not o.cufe),
                    "modelos": dict(Counter(o.modelo for o in encontrados)),
                    "estados": dict(Counter(o.estado for o in encontrados)),
                    "estados_electronicos": dict(Counter(o.estado_electronico or "vacío" for o in encontrados)),
                    "puntos_de_venta": dict(Counter(o.diario or "—" for o in encontrados)),
                    "aporte_a_la_diferencia": dinero(aporte),
                    "refs": [o.ref for o in encontrados[:6]],
                },
                "no_encontrados_en_odoo": b["cantidad"] - (j - i),
            })
        repetidos = Counter(d.numero.clave for d in self.resultado.lectura.incluidos if d.numero and d.numero.prefijo == pref)
        return {
            "_resumen": f"{pref}: {s['ausentes']} ausentes en {s.get('bloques_totales', len(s['bloques']))} bloques",
            "evidencia_id": f"secuencia:{pref}",
            "prefijo": pref,
            "folio_minimo": s["folio_minimo"],
            "folio_maximo": s["folio_maximo"],
            "documentos": s["documentos"],
            "por_tipo": s["por_tipo"],
            "notas_credito_en_rango": s["notas_credito_en_rango"][:30],
            "ausentes": s["ausentes"],
            "ausentes_por_fecha": s["ausentes_por_fecha"],
            "bloques": bloques,
            "bloques_totales": s.get("bloques_totales", len(s["bloques"])),
            "folios_repetidos_dian": [c.replace("|", "-") for c, n in repetidos.items() if n > 1][:10],
            "nota": "Un folio ausente puede ser un documento no transmitido, anulado o emitido con otro prefijo: "
                    "confirmar en Odoo y en el portal DIAN antes de concluir.",
        }

    def generar_plan_revision(self, limite=None) -> dict:
        limite = _entero(limite, "limite", 1, 15, 10)
        pendientes = [f for f in self.resultado.filas if f.estado in _SEVERIDAD]
        pendientes.sort(key=lambda f: (_SEVERIDAD[f.estado], f.prefijo, _clave_folio(f), f.id))
        grupos: list[dict] = []
        for f in pendientes:
            folio = _folio_entero(f)
            g = grupos[-1] if grupos else None
            if (g is not None and g["estado"] == f.estado and g["prefijo"] == f.prefijo and folio is not None
                    and g["hasta"] is not None and folio == g["hasta"] + 1):
                g["hasta"] = folio
                g["filas"].append(f)
            else:
                grupos.append({"estado": f.estado, "prefijo": f.prefijo, "desde": folio, "hasta": folio, "filas": [f]})
        salida = []
        for g in grupos:
            filas = g.pop("filas")
            aporte = sum((f.contribucion for f in filas), CERO)
            fechas = sorted(fecha_iso(f.fecha) for f in filas if f.fecha)
            salida.append({
                **g,
                "etiqueta_estado": ETIQUETAS_ESTADO.get(g["estado"], g["estado"]),
                "casos": len(filas),
                "aporte": dinero(aporte),
                "fechas": [fechas[0], fechas[-1]] if fechas else [],
                "refs": [f.id for f in filas[:5]],
                "_orden": (_SEVERIDAD[g["estado"]], -abs(aporte)),
            })
        salida.sort(key=lambda g: g["_orden"])
        for g in salida:
            g.pop("_orden")
        return {
            "_resumen": f"{len(salida)} grupos pendientes",
            "evidencia_id": "plan_revision",
            "criterio": "Severidad (identidad > duplicado > falta en Odoo > falta en DIAN > valor > ambiguo > impuestos > alcance) y luego impacto",
            "grupos_totales": len(salida),
            "casos_pendientes": len(pendientes),
            "grupos": salida[:limite],
        }


def _folio_entero(f: FilaConciliacion) -> Optional[int]:
    if f.dian is not None and f.dian.numero is not None:
        return f.dian.numero.folio_entero
    if f.odoo is not None and f.odoo.numeros:
        return f.odoo.numeros[0].folio_entero
    return None


def _clave_folio(f: FilaConciliacion) -> int:
    n = _folio_entero(f)
    return -1 if n is None else n
