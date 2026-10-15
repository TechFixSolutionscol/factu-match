"""
Esquema de la respuesta estructurada de Claude y su validación en el backend.

La respuesta de Claude nunca alimenta cálculos: el impacto financiero de cada
hallazgo lo calcula el backend a partir de los documentos citados. Antes de
mostrar un hallazgo se aplican reglas anti-invención:

  1. La estructura cumple el esquema (tipos, enums, campos requeridos).
  2. Cada hecho cita evidencias que alguna herramienta devolvió en esta
     investigación; los hechos sin evidencia válida se descartan.
  3. Un hallazgo sin hechos válidos se descarta completo.
  4. Las acciones deben apuntar a un registro devuelto por las herramientas.
  5. Los importes escritos en el texto deben existir en lo que devolvió el
     backend; si no, el hallazgo queda marcado para revisión humana.
  6. «confirmed» exige hechos con evidencia y, si el hallazgo dice que falta un
     documento en Odoo, haber hecho la búsqueda alternativa (POS, otro número,
     asiento vinculado). Si no, baja a «probable».
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from ..motor import ETIQUETAS_ESTADO, ResultadoConciliacion
from ..normalizacion import CERO, dinero

TIPOS_HALLAZGO = (
    "missing_in_odoo", "missing_in_dian", "amount_mismatch", "tax_mismatch",
    "credit_note_treatment", "identity_mismatch", "possible_duplicate", "ambiguous",
    "scope_difference", "rounding_within_tolerance", "sequence_gap",
    "unexplained_residual", "other",
)
ETIQUETAS_TIPO_HALLAZGO = {
    **{k: v for k, v in ETIQUETAS_ESTADO.items() if k in TIPOS_HALLAZGO},
    "credit_note_treatment": "Tratamiento de notas crédito",
    "rounding_within_tolerance": "Redondeo dentro de la tolerancia",
    "sequence_gap": "Salto de numeración",
    "unexplained_residual": "Residuo no explicado",
    "other": "Otro",
}
ESTADOS_CAUSA = ("confirmed", "probable", "unresolved")
ETIQUETAS_ESTADO_CAUSA = {"confirmed": "Confirmada", "probable": "Probable", "unresolved": "Sin resolver"}
NIVELES = ("high", "medium", "low")
ETIQUETAS_NIVEL = {"high": "Alta", "medium": "Media", "low": "Baja"}
ETIQUETAS_RIESGO = {"high": "Alto", "medium": "Medio", "low": "Bajo"}

SIGNIFICADO_CONFIANZA = {
    "high": "Todos los hechos del hallazgo tienen evidencia directa del backend y la causa es consistente con ella.",
    "medium": "Evidencia parcial o indirecta: la causa es la explicación más consistente, pero falta confirmarla.",
    "low": "Hipótesis plausible con poca evidencia.",
    "nota": "Es una escala cualitativa, no una probabilidad estadística calibrada.",
}

# Límites de la respuesta (se recorta lo que sobre y se informa).
MAX_HALLAZGOS = 6
MAX_HECHOS = 8
MAX_CAUSAS = 5
MAX_ACCIONES = 6
MAX_PASOS = 6
MAX_TEXTO = 700

_LISTA_TEXTO = {"type": "array", "items": {"type": "string"}}
_LISTA_EVIDENCIA = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Identificadores devueltos por las herramientas (ref o evidencia_id), p. ej. dian:FE-300, odoo:pos.order:15, componente:missing_in_dian, secuencia:POS",
}

ESQUEMA_HALLAZGO = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "finding_type", "summary", "refs", "facts", "probable_causes", "cause_status",
        "confidence", "risk_level", "recommended_actions", "verification_steps",
        "requires_human_review",
    ],
    "properties": {
        "finding_type": {"type": "string", "enum": list(TIPOS_HALLAZGO)},
        "summary": {"type": "string", "description": "Una o dos frases con el hallazgo."},
        "refs": {**_LISTA_EVIDENCIA, "description": "Documentos afectados (dian:… u odoo:…) devueltos por las herramientas."},
        "facts": {
            "type": "array",
            "description": "Hechos comprobados con la evidencia citada. Sin evidencia no es un hecho.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["texto", "evidencia"],
                "properties": {"texto": {"type": "string"}, "evidencia": _LISTA_EVIDENCIA},
            },
        },
        "probable_causes": {
            "type": "array",
            "description": "Hipótesis aún no confirmadas y cómo confirmarlas.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["descripcion", "confianza", "como_confirmar", "evidencia"],
                "properties": {
                    "descripcion": {"type": "string"},
                    "confianza": {"type": "string", "enum": list(NIVELES)},
                    "como_confirmar": {"type": "string"},
                    "evidencia": _LISTA_EVIDENCIA,
                },
            },
        },
        "cause_status": {"type": "string", "enum": list(ESTADOS_CAUSA)},
        "confidence": {"type": "string", "enum": list(NIVELES)},
        "risk_level": {"type": "string", "enum": list(NIVELES)},
        "recommended_actions": {
            "type": "array",
            "description": "Acciones para una persona. Claude no ejecuta nada en Odoo.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["accion", "registro_afectado", "evidencia", "verificacion"],
                "properties": {
                    "accion": {"type": "string"},
                    "registro_afectado": {"type": "string", "description": "Un ref o evidencia_id devuelto por las herramientas."},
                    "evidencia": _LISTA_EVIDENCIA,
                    "verificacion": {"type": "string", "description": "Cómo comprobar que la corrección funcionó al volver a conciliar."},
                },
            },
        },
        "verification_steps": _LISTA_TEXTO,
        "requires_human_review": {"type": "boolean"},
    },
}

ESQUEMA_SALIDA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resumen_general", "hallazgos", "datos_faltantes", "siguiente_conciliacion"],
    "properties": {
        "resumen_general": {"type": "string", "description": "Conclusión en 2 a 4 frases, sin importes que el backend no haya devuelto."},
        "hallazgos": {"type": "array", "items": ESQUEMA_HALLAZGO},
        "datos_faltantes": {**_LISTA_TEXTO, "description": "Información que faltó para confirmar causas."},
        "siguiente_conciliacion": {"type": "string", "description": "Qué conciliación volver a ejecutar después de corregir."},
    },
}


# ──────────────────────────────────────────────
# VALIDACIÓN ESTRUCTURAL (subconjunto de JSON Schema usado arriba)
# ──────────────────────────────────────────────

_TIPOS_JSON = {"object": dict, "array": list, "string": str, "boolean": bool}


def validar_estructura(valor: Any, esquema: dict = ESQUEMA_SALIDA, ruta: str = "$") -> list[str]:
    errores: list[str] = []
    tipo = esquema.get("type")
    if tipo and not isinstance(valor, _TIPOS_JSON[tipo]):
        return [f"{ruta}: se esperaba {tipo}"]
    if "enum" in esquema and valor not in esquema["enum"]:
        errores.append(f"{ruta}: valor no permitido {valor!r}")
    if tipo == "object":
        props = esquema.get("properties", {})
        for requerido in esquema.get("required", []):
            if requerido not in valor:
                errores.append(f"{ruta}.{requerido}: falta")
        if esquema.get("additionalProperties") is False:
            for extra in set(valor) - set(props):
                errores.append(f"{ruta}.{extra}: campo no permitido")
        for nombre, sub in props.items():
            if nombre in valor:
                errores.extend(validar_estructura(valor[nombre], sub, f"{ruta}.{nombre}"))
    elif tipo == "array":
        for i, item in enumerate(valor):
            errores.extend(validar_estructura(item, esquema.get("items", {}), f"{ruta}[{i}]"))
    return errores


# ──────────────────────────────────────────────
# IMPORTES EN TEXTO
# ──────────────────────────────────────────────

_RE_IMPORTE = re.compile(
    r"(?<![\w.,/-])(?:\$\s?)?-?\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?![\w])"   # 1.234.567,89 · 5.000
    r"|\$\s?-?\d+(?:[.,]\d{1,2})?(?![\w])"                                      # $5000 · $200,50
    r"|(?<![\w.,/-])-?\d+[.,]\d{2}(?![\d])"                                       # 200.50 · 200,50
)


def _a_decimal_texto(token: str) -> Optional[Decimal]:
    s = token.replace("$", "").replace(" ", "").strip()
    negativo = s.startswith("-")
    s = s.lstrip("-")
    if "." in s and "," in s:
        decimal_sep = "." if s.rfind(".") > s.rfind(",") else ","
        miles = "," if decimal_sep == "." else "."
        s = s.replace(miles, "").replace(decimal_sep, ".")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        partes = s.split(sep)
        if len(partes) == 2 and len(partes[1]) in (1, 2):
            s = partes[0] + "." + partes[1]
        else:
            s = s.replace(sep, "")
    try:
        valor = Decimal(s)
    except InvalidOperation:
        return None
    return -valor if negativo else valor


def importes_en_texto(texto: str) -> list[Decimal]:
    salida = []
    for m in _RE_IMPORTE.finditer(texto or ""):
        v = _a_decimal_texto(m.group(0))
        if v is not None:
            salida.append(v)
    return salida


def recolectar_importes(valor: Any, destino: set) -> None:
    """Todos los números que devolvió el backend (importes, cantidades, folios)."""
    if isinstance(valor, bool):
        return
    if isinstance(valor, (int, Decimal)):
        destino.add(abs(Decimal(valor)))
    elif isinstance(valor, float):
        destino.add(abs(Decimal(repr(valor))))
    elif isinstance(valor, str):
        if re.fullmatch(r"-?\d+(?:\.\d+)?", valor.strip()):
            destino.add(abs(Decimal(valor.strip())))
        else:  # importes dentro de textos de evidencia («Diferencia de 0.50 …»)
            destino.update(abs(v) for v in importes_en_texto(valor))
    elif isinstance(valor, dict):
        for v in valor.values():
            recolectar_importes(v, destino)
    elif isinstance(valor, (list, tuple)):
        for v in valor:
            recolectar_importes(v, destino)


# ──────────────────────────────────────────────
# REGLAS ANTI-INVENCIÓN
# ──────────────────────────────────────────────

# Verbos que proponen registrar un documento (no sustantivos como «registro» o «factura»).
_RE_REGISTRAR = re.compile(
    r"\b(registrar|registre|registren|crear|cree|creen|contabilizar|contabilice|emitir|emita|"
    r"ingresar|ingrese|facturar|facture|digitar|digite|cargar|cargue)\b", re.IGNORECASE)


@dataclass
class ContextoValidacion:
    resultado: ResultadoConciliacion
    evidencias: set[str]
    importes: set[Decimal]
    busquedas_alternativas: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._mapa = {e.lower(): e for e in self.evidencias}

    def canonica(self, evidencia: str) -> Optional[str]:
        e = (evidencia or "").strip()
        if e in self.evidencias:
            return e
        return self._mapa.get(e.lower())


def _texto(valor: Any) -> str:
    s = str(valor or "").strip()
    return s if len(s) <= MAX_TEXTO else s[: MAX_TEXTO - 1] + "…"


def _evidencias_validas(lista: list, ctx: ContextoValidacion, descartadas: list[str]) -> list[str]:
    salida: list[str] = []
    for e in lista or []:
        c = ctx.canonica(e)
        if c:
            if c not in salida:
                salida.append(c)
        else:
            descartadas.append(str(e)[:80])
    return salida


def _impacto(refs: list[str], tipo: str, ctx: ContextoValidacion) -> dict:
    """Impacto financiero calculado por el backend, nunca tomado del texto de Claude."""
    filas = {}
    for r in refs:
        f = ctx.resultado.fila(r)
        if f is not None:
            filas[f.id] = f
    citados = sum((f.contribucion for f in filas.values()), CERO)
    componente = None
    for c in ctx.resultado.descomposicion.get("componentes", []):
        if c["codigo"] == tipo:
            componente = {"codigo": c["codigo"], "etiqueta": c["etiqueta"], "monto": c["monto"], "documentos": c["documentos"]}
    if tipo == "unexplained_residual":
        componente = {"codigo": "residuo", "etiqueta": "Residuo no explicado",
                      "monto": ctx.resultado.descomposicion.get("residuo"), "documentos": 0}
    return {
        "documentos_citados": {"monto": dinero(citados), "documentos": len(filas)},
        "componente": componente,
        "calculo": "Σ aporte (DIAN − Odoo) de los documentos citados, calculado por el backend",
    }


def aplicar_reglas(datos: dict, ctx: ContextoValidacion) -> tuple[dict, dict]:
    """Devuelve (resultado depurado, informe de validación)."""
    informe = {"hallazgos_recibidos": len(datos.get("hallazgos", [])), "descartados": [], "advertencias": [],
               "evidencias_desconocidas": []}
    hallazgos_ok = []
    recibidos = datos.get("hallazgos", [])
    if len(recibidos) > MAX_HALLAZGOS:
        informe["advertencias"].append(f"Se recibieron {len(recibidos)} hallazgos; se muestran los {MAX_HALLAZGOS} primeros")
    for n, h in enumerate(recibidos[:MAX_HALLAZGOS], start=1):
        advertencias: list[str] = []
        desconocidas: list[str] = []

        refs = [r for r in _evidencias_validas(h.get("refs", []), ctx, desconocidas) if r.startswith(("dian:", "odoo:"))]

        hechos = []
        for hecho in h.get("facts", [])[:MAX_HECHOS]:
            ev = _evidencias_validas(hecho.get("evidencia", []), ctx, desconocidas)
            if ev:
                hechos.append({"texto": _texto(hecho.get("texto")), "evidencia": ev})
            else:
                advertencias.append(f"Hecho descartado por no citar evidencia devuelta por el backend: «{_texto(hecho.get('texto'))[:120]}»")

        causas = []
        for c in h.get("probable_causes", [])[:MAX_CAUSAS]:
            causas.append({
                "descripcion": _texto(c.get("descripcion")),
                "confianza": c.get("confianza") if c.get("confianza") in NIVELES else "low",
                "como_confirmar": _texto(c.get("como_confirmar")),
                "evidencia": _evidencias_validas(c.get("evidencia", []), ctx, desconocidas),
            })

        acciones = []
        for a in h.get("recommended_actions", [])[:MAX_ACCIONES]:
            registro = ctx.canonica(a.get("registro_afectado", ""))
            if not registro:
                advertencias.append(f"Acción descartada: el registro «{str(a.get('registro_afectado'))[:60]}» no fue devuelto por las herramientas")
                continue
            accion = {
                "accion": _texto(a.get("accion")),
                "registro_afectado": registro,
                "evidencia": _evidencias_validas(a.get("evidencia", []), ctx, desconocidas),
                "verificacion": _texto(a.get("verificacion")),
            }
            fila = ctx.resultado.fila(registro)
            if (fila is not None and fila.estado == "missing_in_odoo" and _RE_REGISTRAR.search(accion["accion"])
                    and registro not in ctx.busquedas_alternativas):
                advertencias.append(
                    f"Antes de registrar {registro} hay que verificar en Odoo si ya existe como orden POS, con otro "
                    "número, en otro estado o fecha, o vinculado a un asiento: no hubo una búsqueda alternativa completa")
            acciones.append(accion)

        if not hechos:
            informe["descartados"].append({"hallazgo": n, "resumen": _texto(h.get("summary"))[:160],
                                           "motivo": "Ningún hecho cita evidencia devuelta por el backend"})
            informe["evidencias_desconocidas"].extend(desconocidas)
            continue

        textos = [h.get("summary", "")] + [x["texto"] for x in hechos] + [x["descripcion"] for x in causas] + [x["accion"] for x in acciones]
        sin_respaldo = sorted({dinero(v) for t in textos for v in importes_en_texto(t) if abs(v) not in ctx.importes})
        if sin_respaldo:
            advertencias.append("El texto menciona importes que el backend no devolvió: " + ", ".join(sin_respaldo))

        estado_causa = h.get("cause_status")
        tipo = h.get("finding_type")
        faltantes = [r for r in refs if (f := ctx.resultado.fila(r)) is not None and f.estado == "missing_in_odoo"]
        sin_busqueda = [r for r in faltantes if r not in ctx.busquedas_alternativas]
        if estado_causa == "confirmed":
            if tipo == "missing_in_odoo" and not faltantes:
                estado_causa = "probable"
                advertencias.append("La causa baja a «probable»: afirma documentos faltantes en Odoo sin citarlos uno por uno")
            elif sin_busqueda:
                estado_causa = "probable"
                advertencias.append("La causa baja a «probable»: no se descartaron búsquedas alternativas completas "
                                    "en Odoo para " + ", ".join(sin_busqueda[:5]))
        if desconocidas:
            advertencias.append("Se ignoraron evidencias no devueltas por el backend: " + ", ".join(sorted(set(desconocidas))[:8]))
            informe["evidencias_desconocidas"].extend(desconocidas)

        revision = bool(h.get("requires_human_review")) or bool(advertencias) or estado_causa != "confirmed"
        hallazgos_ok.append({
            "finding_type": tipo,
            "etiqueta_tipo": ETIQUETAS_TIPO_HALLAZGO.get(tipo, tipo),
            "summary": _texto(h.get("summary")),
            "refs": refs,
            "facts": hechos,
            "probable_causes": causas,
            "cause_status": estado_causa,
            "etiqueta_estado_causa": ETIQUETAS_ESTADO_CAUSA.get(estado_causa, estado_causa),
            "confidence": h.get("confidence"),
            "risk_level": h.get("risk_level"),
            "financial_impact": _impacto(refs, tipo, ctx),
            "recommended_actions": acciones,
            "verification_steps": [_texto(p) for p in h.get("verification_steps", [])[:MAX_PASOS]],
            "requires_human_review": revision,
            "advertencias": advertencias,
        })

    resumen = _texto(datos.get("resumen_general"))
    sin_respaldo = sorted({dinero(v) for v in importes_en_texto(resumen) if abs(v) not in ctx.importes})
    if sin_respaldo:
        informe["advertencias"].append("El resumen menciona importes que el backend no devolvió: " + ", ".join(sin_respaldo))
    informe["hallazgos_validos"] = len(hallazgos_ok)
    informe["evidencias_desconocidas"] = sorted(set(informe["evidencias_desconocidas"]))[:20]
    return {
        "resumen_general": resumen,
        "hallazgos": hallazgos_ok,
        "datos_faltantes": [_texto(x) for x in datos.get("datos_faltantes", [])[:10]],
        "siguiente_conciliacion": _texto(datos.get("siguiente_conciliacion")),
        "significado_confianza": SIGNIFICADO_CONFIANZA,
    }, informe
