"""
Normalización determinística para el módulo de ventas.

Reglas documentadas (ver docs/VENTAS.md, sección «Identificación»):

Números de documento
  - El prefijo se conserva tal cual, en mayúsculas y solo con letras y dígitos.
    Nunca se mezclan prefijos: POS-100 y FE-100 son documentos distintos.
  - El folio numérico pierde los ceros a la izquierda ("00706" → "706").
  - Un folio con letras se conserva en mayúsculas, sin quitar ceros.
  - Se ignoran los separadores "/", "-", "_", "." y espacios entre prefijo y folio.
  - Formato Odoo con año (PREFIJO/AAAA/NUMERO): se descarta el segmento del año.
  - Texto sin separador ("FE1706"): se parte con los prefijos conocidos del
    contenedor DIAN, el más largo primero. Si no hay prefijo conocido, se toma
    letras + dígitos. Si el texto es ambiguo, no se adivina: se devuelve None.
  - La clave de comparación es "PREFIJO|FOLIO" para que dos prefijos distintos
    nunca colisionen ("FE1|706" ≠ "FE|1706").

Dinero
  - Todo importe se maneja con Decimal y 2 decimales (ROUND_HALF_UP).
  - Los float de Excel/Odoo se convierten vía repr() para no arrastrar error binario.

Fechas
  - Colombia no tiene horario de verano: UTC-5 fijo.
  - pos.order.date_order llega en UTC desde Odoo y se convierte a hora de Colombia.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Iterable, Optional

ZONA_COLOMBIA = timezone(timedelta(hours=-5), name="America/Bogota")
CENTAVO = Decimal("0.01")
CERO = Decimal("0.00")
NIT_CONSUMIDOR_FINAL = "222222222222"

_VACIOS = {"", "nan", "none", "null", "false", "-", "/"}


# ──────────────────────────────────────────────
# TEXTO
# ──────────────────────────────────────────────

def texto_clave(valor) -> str:
    """Minúsculas, sin tildes y con espacios simples. Para comparar etiquetas."""
    if valor is None or valor is False:
        return ""
    s = unicodedata.normalize("NFKD", str(valor))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def texto_limpio(valor) -> str:
    """Texto sin espacios sobrantes; '' para vacíos de Excel/Odoo."""
    if valor is None or valor is False:
        return ""
    s = re.sub(r"\s+", " ", str(valor)).strip()
    return "" if s.lower() in _VACIOS else s


# ──────────────────────────────────────────────
# DINERO
# ──────────────────────────────────────────────

def a_decimal(valor) -> Decimal:
    """
    Convierte un importe a Decimal con 2 decimales.

    Acepta int, float, Decimal y texto ('1234.56', '1.234,56', '$ 1.234.567,00').
    Vacíos → 0.00. Lanza ValueError si el texto no es un número.
    """
    if valor is None or valor is False:
        return CERO
    if isinstance(valor, bool):
        return CERO
    if isinstance(valor, Decimal):
        d = valor
    elif isinstance(valor, int):
        d = Decimal(valor)
    elif isinstance(valor, float):
        if valor != valor:  # NaN
            return CERO
        d = Decimal(repr(valor))
    else:
        texto = str(valor).strip()
        if texto.lower() in _VACIOS:
            return CERO
        # Signo antes del primer dígito («-5.000», «$ -1.234,56») o entre paréntesis («(1.000)»).
        negativo = bool(re.match(r"^[^\d]*-", texto)) or (texto.startswith("(") and texto.endswith(")"))
        texto = re.sub(r"[^0-9,.]", "", texto)
        if not texto or not re.search(r"\d", texto):
            raise ValueError(f"Importe no numérico: {valor!r}")
        if "," in texto and "." in texto:
            if texto.rfind(",") > texto.rfind("."):
                texto = texto.replace(".", "").replace(",", ".")
            else:
                texto = texto.replace(",", "")
        elif "," in texto:
            entero, dec = texto.rsplit(",", 1)
            texto = f"{entero.replace(',', '')}.{dec}" if len(dec) <= 2 else texto.replace(",", "")
        elif texto.count(".") > 1:
            entero, dec = texto.rsplit(".", 1)
            texto = f"{entero.replace('.', '')}.{dec}" if len(dec) <= 2 else texto.replace(".", "")
        elif "." in texto and len(texto.rsplit(".", 1)[1]) == 3 and texto.rsplit(".", 1)[0] not in ("", "0"):
            # Un solo punto seguido de 3 dígitos: separador de miles colombiano («15.000» = quince mil).
            texto = texto.replace(".", "")
        try:
            d = Decimal(texto)
        except InvalidOperation as exc:
            raise ValueError(f"Importe no numérico: {valor!r}") from exc
        if negativo:
            d = -d
    return d.quantize(CENTAVO, rounding=ROUND_HALF_UP)


_RE_AMBIGUO = re.compile(r"^\s*\(?\s*-?\s*\$?\s*-?\s*[1-9]\d{0,2}\.\d{3}\s*\)?\s*$")


def importe_ambiguo(valor) -> bool:
    """Texto con un solo punto y 3 dígitos («592.593»): puede ser miles o decimales."""
    return isinstance(valor, str) and bool(_RE_AMBIGUO.match(valor))


def a_decimal_seguro(valor) -> tuple[Decimal, Optional[str]]:
    """Como a_decimal, pero devuelve (0.00, mensaje) en vez de lanzar error."""
    try:
        return a_decimal(valor), None
    except ValueError as exc:
        return CERO, str(exc)


def dinero(valor: Decimal) -> str:
    """Representación estable para JSON: '1234.50'."""
    return f"{valor.quantize(CENTAVO, rounding=ROUND_HALF_UP):.2f}"


# ──────────────────────────────────────────────
# NIT Y CUFE
# ──────────────────────────────────────────────

def normalizar_nit(valor) -> str:
    """Solo dígitos. 900.123.456-7 → 9001234567 ; 900123456.0 (float de Excel) → 900123456."""
    if valor is None or valor is False:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return re.sub(r"[^0-9]", "", str(valor))


def nits_equivalentes(a: str, b: str) -> bool:
    """
    Igualdad de NIT tolerando el dígito de verificación: la DIAN suele exportar
    el NIT sin DV y Odoo lo guarda con DV (900123456 vs 9001234567).
    """
    a, b = normalizar_nit(a), normalizar_nit(b)
    if not a or not b:
        return False
    if a == b:
        return True
    corto, largo = (a, b) if len(a) < len(b) else (b, a)
    return len(corto) >= 6 and len(largo) - len(corto) == 1 and largo.startswith(corto)


def nit_compania_coincide(vat_odoo, nit_dian) -> bool:
    """
    Como nits_equivalentes, pero sin confundir entidades cuando Odoo guarda el NIT con guion:
    «900123456-7» coincide con 900123456; «90012345-6» (otro NIT de 8 dígitos con su DV) no.
    """
    n = normalizar_nit(nit_dian)
    texto = texto_limpio(vat_odoo)
    if "-" in texto:
        base = normalizar_nit(texto.rsplit("-", 1)[0])
        # Solo se acepta el DV del lado DIAN si el NIT DIAN tiene 10 dígitos (9 + DV).
        return bool(n) and (base == n or (len(n) == 10 and len(base) == 9 and n.startswith(base)))
    return nits_equivalentes(texto, n)


def es_consumidor_final(nit: str) -> bool:
    return normalizar_nit(nit) == NIT_CONSUMIDOR_FINAL


def es_probable_persona_juridica(nit: str) -> bool:
    """Heurística: los NIT de empresas colombianas tienen 9 dígitos y empiezan por 8 o 9."""
    n = normalizar_nit(nit)
    if len(n) == 10 and n[0] in "89":
        n = n[:9]  # con DV
    return len(n) == 9 and n[0] in "89"


def enmascarar_nit(nit: str) -> str:
    """Para enviar a la IA: conserva solo los 3 últimos dígitos."""
    n = normalizar_nit(nit)
    if not n:
        return ""
    if es_consumidor_final(n):
        return n
    return "***" + n[-3:]


def normalizar_cufe(valor) -> str:
    """CUFE/CUDE en minúsculas y sin espacios; '' si no hay valor."""
    if valor is None or valor is False:
        return ""
    s = re.sub(r"\s+", "", str(valor)).lower()
    return "" if s in _VACIOS else s


def cufe_corto(cufe: str) -> str:
    return f"{cufe[:10]}…{cufe[-6:]}" if len(cufe) > 20 else cufe


# ──────────────────────────────────────────────
# NÚMEROS DE DOCUMENTO
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class NumeroDocumento:
    prefijo: str
    folio: str
    original: str = ""

    @property
    def clave(self) -> str:
        return f"{self.prefijo}|{self.folio}"

    @property
    def legible(self) -> str:
        return f"{self.prefijo}-{self.folio}" if self.prefijo else self.folio

    @property
    def folio_entero(self) -> Optional[int]:
        return int(self.folio) if self.folio.isdigit() else None


def normalizar_prefijo(valor) -> str:
    if isinstance(valor, float) and valor.is_integer():   # prefijo numérico leído de un .xls (20.0 → «20»)
        valor = int(valor)
    return re.sub(r"[^A-Za-z0-9]", "", texto_limpio(valor)).upper()


def normalizar_folio(valor) -> str:
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    s = re.sub(r"[\s/\-_.]", "", texto_limpio(valor)).upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
    if s.isdigit():
        return str(int(s))
    return s


def numero_desde_dian(prefijo, folio, prefijos_conocidos: Iterable[str] = ()) -> Optional[NumeroDocumento]:
    """Construye el número a partir de las columnas Prefijo y Folio del contenedor."""
    p = normalizar_prefijo(prefijo)
    f_texto = texto_limpio(folio)
    if not f_texto:
        return None
    if not p:
        # El folio puede traer el prefijo incluido (p. ej. 'FE14394').
        return parsear_numero(f_texto, prefijos_conocidos)
    f = normalizar_folio(folio)
    if not f:
        return None
    return NumeroDocumento(p, f, f"{texto_limpio(prefijo)}-{f_texto}")


def parsear_numero(texto, prefijos_conocidos: Iterable[str] = ()) -> Optional[NumeroDocumento]:
    """
    Interpreta un número de Odoo (name, l10n_latam_document_number,
    l10n_co_edi_pos_name). Devuelve None cuando el formato es ambiguo.
    """
    original = texto_limpio(texto)
    if not original:
        return None
    segmentos = [s for s in re.split(r"[\s/\-_.]+", original.upper()) if s]
    if not segmentos:
        return None

    if len(segmentos) == 3 and re.fullmatch(r"(19|20)\d{2}", segmentos[1]) and segmentos[2].isdigit():
        return NumeroDocumento(normalizar_prefijo(segmentos[0]), normalizar_folio(segmentos[2]), original)

    if len(segmentos) == 2:
        pref, fol = segmentos
        if re.fullmatch(r"[A-Z0-9]+", pref) and re.search(r"[A-Z]", pref) and re.search(r"\d", fol):
            return NumeroDocumento(normalizar_prefijo(pref), normalizar_folio(fol), original)
        return None

    if len(segmentos) == 1:
        s = re.sub(r"[^A-Z0-9]", "", segmentos[0])
        conocidos = sorted({normalizar_prefijo(p) for p in prefijos_conocidos if normalizar_prefijo(p)},
                           key=len, reverse=True)
        for p in conocidos:
            if s.startswith(p) and len(s) > len(p) and s[len(p):].isdigit():
                return NumeroDocumento(p, normalizar_folio(s[len(p):]), original)
        m = re.fullmatch(r"([A-Z]+)(\d+)", s)
        if m:
            return NumeroDocumento(m.group(1), normalizar_folio(m.group(2)), original)
        if s.isdigit():
            return NumeroDocumento("", normalizar_folio(s), original)
        return None

    return None


# ──────────────────────────────────────────────
# FECHAS
# ──────────────────────────────────────────────

_FORMATOS_FECHA = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d")
_FORMATOS_FECHA_HORA = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                        "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M")


def parsear_fecha(valor) -> Optional[date]:
    if valor is None or valor is False or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = texto_limpio(valor)
    if not texto:
        return None
    for fmt in _FORMATOS_FECHA:
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            continue
    dt = parsear_fecha_hora(texto)
    return dt.date() if dt else None


def parsear_fecha_hora(valor) -> Optional[datetime]:
    """Fecha y hora sin zona (tal como la exporta la DIAN)."""
    if valor is None or valor is False or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None)
    if isinstance(valor, date):
        return datetime.combine(valor, time(0, 0))
    texto = texto_limpio(valor)
    if not texto:
        return None
    for fmt in _FORMATOS_FECHA_HORA:
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(ZONA_COLOMBIA).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def utc_odoo_a_local(valor) -> Optional[datetime]:
    """'2026-10-15 01:49:40' (UTC, como lo entrega Odoo) → datetime en hora de Colombia (sin tzinfo)."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        dt = valor
    else:
        texto = texto_limpio(valor)
        try:
            dt = datetime.strptime(texto[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZONA_COLOMBIA).replace(tzinfo=None)


def rango_utc_odoo(desde: date, hasta: date) -> tuple[str, str]:
    """Días locales [desde, hasta] → límites UTC para filtrar date_order: [inicio, fin)."""
    inicio = datetime.combine(desde, time(0, 0), tzinfo=ZONA_COLOMBIA).astimezone(timezone.utc)
    fin = datetime.combine(hasta + timedelta(days=1), time(0, 0), tzinfo=ZONA_COLOMBIA).astimezone(timezone.utc)
    return inicio.strftime("%Y-%m-%d %H:%M:%S"), fin.strftime("%Y-%m-%d %H:%M:%S")


def fecha_iso(valor: Optional[date]) -> Optional[str]:
    return valor.isoformat() if valor else None
