"""
Extracción de ventas desde Odoo en SOLO LECTURA.

- Usa XML-RPC, el mismo mecanismo que el comparador de compras, y reutiliza
  OdooConnector para autenticar (odoo_match.py no se modifica).
- XML-RPC está deprecado desde Odoo 19 y se retira en Odoo 20. Todo el acceso
  pasa por OdooSoloLectura, así que cambiar el transporte (p. ej. a JSON-2)
  solo exige otra función `ejecutar`, sin tocar el motor.
- Antes de leer, verifica con fields_get qué campos existen en la instancia.
  Los campos que no existen se informan y no se usan: nunca se dan por hechos.
- Solo permite métodos de lectura sobre una lista blanca de modelos.
"""

from __future__ import annotations

import xmlrpc.client
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Optional

from .normalizacion import nit_compania_coincide, normalizar_nit, rango_utc_odoo

METODOS_PERMITIDOS = frozenset({"fields_get", "search_read", "read", "search_count", "check_access_rights"})
MODELOS_PERMITIDOS = frozenset({
    "account.move", "pos.order", "res.partner", "res.company", "account.journal", "pos.config",
})

CAMPOS_MOVE = (
    "name", "move_type", "state", "invoice_date", "invoice_date_due", "partner_id",
    "invoice_partner_display_name", "company_id", "journal_id", "amount_total",
    "amount_untaxed", "amount_tax", "amount_total_signed", "l10n_co_edi_cufe_cude_ref",
    "l10n_latam_document_number", "edi_state", "l10n_co_dian_state",
    "l10n_co_dian_commercial_state", "l10n_co_edi_type", "reversed_entry_id", "pos_order_ids",
)
CAMPOS_POS = (
    "name", "date_order", "state", "partner_id", "company_id", "amount_total", "amount_tax",
    "account_move", "is_invoiced", "is_refund", "refunded_order_id", "l10n_co_edi_pos_name",
    "l10n_co_edi_pos_cufe_cude_ref", "l10n_co_edi_pos_dian_state", "l10n_co_edi_pos_dian_date",
    "session_id", "config_id", "pos_reference",
)
CAMPOS_PARTNER = ("name", "vat", "is_company")
CAMPOS_COMPANIA = ("name", "vat")

# Campos sin los cuales una fuente no se puede conciliar.
CAMPOS_MINIMOS_MOVE = ("name", "move_type", "state", "invoice_date", "amount_total")
CAMPOS_MINIMOS_POS = ("date_order", "state", "amount_total")


class AccesoNoPermitido(PermissionError):
    """Se intentó un método o modelo fuera de la lista blanca de solo lectura."""


class LecturaTruncada(ValueError):
    """La consulta superó el máximo de registros: el resultado estaría incompleto."""


class PermisoInsuficiente(ValueError):
    """El usuario de Odoo no puede leer un modelo necesario para la conciliación."""


class OdooSoloLectura:
    """Envuelve execute_kw y bloquea cualquier escritura o modelo no autorizado."""

    def __init__(self, ejecutar: Callable[[str, str, list, dict], Any]):
        self._ejecutar = ejecutar
        self._campos: dict[str, dict] = {}
        self.llamadas = 0
        # Gancho opcional que se ejecuta antes de cada llamada (p. ej. el plazo de una investigación).
        self.antes_de_llamar: Optional[Callable[[], None]] = None

    def llamar(self, modelo: str, metodo: str, args: Optional[list] = None, kwargs: Optional[dict] = None):
        if metodo not in METODOS_PERMITIDOS:
            raise AccesoNoPermitido(f"Método no permitido en modo solo lectura: {metodo}")
        if modelo not in MODELOS_PERMITIDOS:
            raise AccesoNoPermitido(f"Modelo no permitido: {modelo}")
        if self.antes_de_llamar is not None:
            self.antes_de_llamar()
        self.llamadas += 1
        return self._ejecutar(modelo, metodo, list(args or []), dict(kwargs or {}))

    def campos(self, modelo: str) -> dict:
        if modelo not in self._campos:
            self._campos[modelo] = self.llamar(modelo, "fields_get", [], {"attributes": ["type", "string", "relation", "selection"]}) or {}
        return self._campos[modelo]

    def separar_campos(self, modelo: str, deseados: Iterable[str]) -> tuple[list[str], list[str]]:
        existentes = self.campos(modelo)
        disponibles = [c for c in deseados if c in existentes]
        faltantes = [c for c in deseados if c not in existentes]
        return disponibles, faltantes

    def search_read_todo(self, modelo: str, dominio: list, campos: list[str], lote: int = 500,
                         maximo: int = 50000, orden: str = "id") -> list[dict]:
        """Lee por lotes. Si hay más de `maximo` registros falla en vez de devolver un resultado parcial."""
        registros: list[dict] = []
        offset = 0
        while True:
            parte = self.llamar(modelo, "search_read", [dominio],
                                {"fields": campos, "limit": lote, "offset": offset, "order": orden}) or []
            registros.extend(parte)
            if len(parte) < lote:
                return registros
            offset += lote
            if offset >= maximo:
                raise LecturaTruncada(
                    f"{modelo} tiene más de {maximo} registros en la ventana leída: el resultado quedaría "
                    "incompleto. Usa un período más corto.")

    def leer(self, modelo: str, ids: list[int], campos: list[str], lote: int = 500) -> list[dict]:
        registros: list[dict] = []
        ids = [i for i in dict.fromkeys(ids) if isinstance(i, int)]
        for i in range(0, len(ids), lote):
            registros.extend(self.llamar(modelo, "read", [ids[i:i + lote]], {"fields": campos}) or [])
        return registros


class _TransporteConTiempo(xmlrpc.client.Transport):
    """XML-RPC sobre HTTP con tiempo máximo por llamada (ServerProxy no trae timeout)."""

    def __init__(self, segundos: float):
        super().__init__()
        self._segundos = segundos

    def make_connection(self, host):
        conexion = super().make_connection(host)
        conexion.timeout = self._segundos
        return conexion


class _TransporteSeguroConTiempo(xmlrpc.client.SafeTransport):
    def __init__(self, segundos: float):
        super().__init__()
        self._segundos = segundos

    def make_connection(self, host):
        conexion = super().make_connection(host)
        conexion.timeout = self._segundos
        return conexion


def _transporte(url: str, segundos: float):
    return (_TransporteSeguroConTiempo if url.startswith("https") else _TransporteConTiempo)(segundos)


def _autenticar(conector, segundos: float) -> int:
    """Como OdooConnector.authenticate, pero con tiempo máximo (no modifica odoo_match.py)."""
    from odoo_match import OdooAuthError, OdooConnectionError

    try:
        comun = xmlrpc.client.ServerProxy(f"{conector.url}/xmlrpc/2/common", allow_none=True,
                                          transport=_transporte(conector.url, segundos))
        uid = comun.authenticate(conector.database, conector.username, conector.api_key, {})
    except xmlrpc.client.ProtocolError as exc:
        if exc.errcode == 404:
            raise OdooConnectionError(f"URL de Odoo inválida ({conector.url}): la instancia no existe o no está en línea") from None
        raise OdooConnectionError(f"No se pudo conectar a Odoo (HTTP {exc.errcode})") from None
    except xmlrpc.client.Fault as exc:
        raise OdooConnectionError(f"Odoo rechazó la autenticación: {str(exc.faultString)[:160]}") from None
    except OSError as exc:
        raise OdooConnectionError(f"No se pudo conectar a Odoo ({type(exc).__name__})") from None
    except Exception as exc:  # p. ej. una página HTML de login o de un proxy en vez de XML-RPC
        raise OdooConnectionError(f"Odoo no respondió como se esperaba ({type(exc).__name__}): revisa la URL") from None
    if not uid:
        raise OdooAuthError("Credenciales inválidas. Verifique usuario y API key.")
    return uid


def crear_desde_conector(conector, segundos_por_llamada: float = 120.0) -> OdooSoloLectura:
    """Construye el lector con un OdooConnector ya configurado. Toda llamada tiene tiempo máximo."""
    uid = conector._uid or _autenticar(conector, segundos_por_llamada)
    modelos = xmlrpc.client.ServerProxy(f"{conector.url}/xmlrpc/2/object", allow_none=True,
                                        transport=_transporte(conector.url, segundos_por_llamada))

    def ejecutar(modelo: str, metodo: str, args: list, kwargs: dict):
        return modelos.execute_kw(conector.database, uid, conector.api_key, modelo, metodo, args, kwargs)

    return OdooSoloLectura(ejecutar)


@dataclass
class ExtraccionOdoo:
    compania: dict
    companias_leidas: int
    movimientos: list[dict]
    ordenes_pos: list[dict]
    partners: dict[int, dict]
    campos_no_disponibles: dict[str, list[str]]
    advertencias: list[str] = field(default_factory=list)
    fuentes: dict[str, bool] = field(default_factory=dict)
    ventana: dict[str, str] = field(default_factory=dict)
    llamadas: int = 0
    conteo_liberado: dict[str, int] = field(default_factory=dict)
    companias_ids: list[int] = field(default_factory=list)

    def registros_leidos(self) -> dict[str, int]:
        if self.conteo_liberado:
            return dict(self.conteo_liberado)
        return {"account.move": len(self.movimientos), "pos.order": len(self.ordenes_pos)}

    def liberar_registros(self) -> None:
        """Tras construir el conjunto canónico los registros crudos ya no se usan: se liberan
        para que las conciliaciones guardadas en memoria ocupen menos (Render tiene RAM limitada)."""
        self.conteo_liberado = self.registros_leidos()
        self.movimientos = []
        self.ordenes_pos = []
        self.partners = {}

    def a_dict(self) -> dict:
        return {
            "compania": {"id": self.compania.get("id"), "nombre": self.compania.get("name"),
                         "nit": normalizar_nit(self.compania.get("vat"))},
            "companias_leidas": self.companias_leidas,
            "companias_con_el_nit": len(self.companias_ids) or 1,
            "registros_leidos": self.registros_leidos(),
            "campos_no_disponibles": self.campos_no_disponibles,
            "advertencias": self.advertencias,
            "fuentes": self.fuentes,
            "ventana": self.ventana,
            "llamadas": self.llamadas,
        }


class AlcanceInvalido(ValueError):
    """La compañía de Odoo no corresponde al NIT emisor del contenedor."""


def _id(valor) -> Optional[int]:
    if isinstance(valor, (list, tuple)) and valor:
        return valor[0] if isinstance(valor[0], int) else None
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def ubicar_compania(lector: OdooSoloLectura, nit_emisor: str) -> tuple[list[dict], int]:
    """
    Compañías de Odoo con el NIT emisor del contenedor. Si varias comparten el NIT (por ejemplo
    sucursales creadas como compañías), todas son la misma persona jurídica ante la DIAN y se leen juntas.
    """
    disponibles, _ = lector.separar_campos("res.company", CAMPOS_COMPANIA)
    companias = lector.search_read_todo("res.company", [], ["id"] + disponibles, lote=200, maximo=1000)
    coinciden = [c for c in companias if nit_compania_coincide(c.get("vat") or "", nit_emisor)]
    if coinciden:
        return coinciden, len(companias)
    nits = ", ".join(f"{c.get('name')} ({normalizar_nit(c.get('vat')) or 'sin NIT'})" for c in companias) or "ninguna"
    raise AlcanceInvalido(
        f"Ninguna compañía de Odoo tiene el NIT emisor del contenedor ({nit_emisor}). "
        f"Compañías visibles para el usuario: {nits}."
    )


def _leer_con_permiso(lector: OdooSoloLectura, modelo: str, dominio: list, campos: list[str]) -> list[dict]:
    try:
        return lector.search_read_todo(modelo, dominio, campos)
    except xmlrpc.client.Fault as exc:
        texto = str(exc.faultString or "")
        if "AccessError" in texto or "access" in texto.lower() or "permis" in texto.lower():
            raise PermisoInsuficiente(
                f"El usuario de Odoo no tiene permiso de lectura sobre {modelo}: concédelo o desmarca esa fuente "
                "(sus documentos aparecerían como faltantes en Odoo).") from None
        raise


def extraer_ventas(lector: OdooSoloLectura, nit_emisor: str, desde: date, hasta: date,
                   margen_dias: int = 5, incluir_move: bool = True, incluir_pos: bool = True) -> ExtraccionOdoo:
    """
    Lee facturas/notas crédito de cliente y órdenes POS de la compañía cuyo NIT
    coincide con el emisor del contenedor. La ventana se amplía `margen_dias`
    a cada lado y se leen todos los estados: lo que quede fuera del período o
    en un estado no elegible sirve como evidencia de diferencias de alcance.
    """
    coinciden, total_companias = ubicar_compania(lector, nit_emisor)
    compania = coinciden[0]
    ids = [c["id"] for c in coinciden]
    desde_amp = desde - timedelta(days=margen_dias)
    hasta_amp = hasta + timedelta(days=margen_dias)
    advertencias: list[str] = []
    no_disponibles: dict[str, list[str]] = {}
    movimientos: list[dict] = []
    ordenes: list[dict] = []
    fuentes = {"account.move": False, "pos.order": False}

    if incluir_move:
        campos, faltan = lector.separar_campos("account.move", CAMPOS_MOVE)
        no_disponibles["account.move"] = faltan
        minimos = [c for c in CAMPOS_MINIMOS_MOVE if c not in campos]
        if minimos:
            advertencias.append("account.move no expone campos mínimos: " + ", ".join(minimos))
        else:
            dominio = [
                ("move_type", "in", ["out_invoice", "out_refund"]),
                ("company_id", "in", ids),
                ("invoice_date", ">=", desde_amp.isoformat()),
                ("invoice_date", "<=", hasta_amp.isoformat()),
            ]
            movimientos = _leer_con_permiso(lector, "account.move", dominio, ["id"] + campos)
            fuentes["account.move"] = True
        if "l10n_co_edi_cufe_cude_ref" in faltan:
            advertencias.append("account.move no tiene l10n_co_edi_cufe_cude_ref: las facturas solo se identifican por número")

    if incluir_pos:
        try:
            campos, faltan = lector.separar_campos("pos.order", CAMPOS_POS)
        except xmlrpc.client.Fault as exc:
            campos, faltan = [], list(CAMPOS_POS)
            advertencias.append(f"No se pudo leer pos.order (¿módulo POS sin instalar o sin permiso?): {exc.faultString[:160]}")
        no_disponibles["pos.order"] = faltan
        minimos = [c for c in CAMPOS_MINIMOS_POS if c not in campos]
        if campos and minimos:
            advertencias.append("pos.order no expone campos mínimos: " + ", ".join(minimos))
        elif campos:
            inicio_utc, fin_utc = rango_utc_odoo(desde_amp, hasta_amp)
            dominio = [("date_order", ">=", inicio_utc), ("date_order", "<", fin_utc)]
            if "company_id" in campos:
                dominio.append(("company_id", "in", ids))
            ordenes = _leer_con_permiso(lector, "pos.order", dominio, ["id"] + campos)
            fuentes["pos.order"] = True
            if "l10n_co_edi_pos_cufe_cude_ref" in faltan:
                advertencias.append("pos.order no tiene l10n_co_edi_pos_cufe_cude_ref: las órdenes POS solo se identifican por número")
            if "account_move" in faltan:
                advertencias.append("pos.order no tiene account_move: no se puede agrupar una orden con su factura")

    partner_ids = {_id(r.get("partner_id")) for r in movimientos + ordenes}
    partner_ids.discard(None)
    partners: dict[int, dict] = {}
    if partner_ids:
        campos_p, faltan_p = lector.separar_campos("res.partner", CAMPOS_PARTNER)
        no_disponibles["res.partner"] = faltan_p
        for p in lector.leer("res.partner", sorted(partner_ids), ["id"] + campos_p):
            partners[p["id"]] = p

    return ExtraccionOdoo(
        compania=compania,
        companias_leidas=total_companias,
        movimientos=movimientos,
        ordenes_pos=ordenes,
        partners=partners,
        campos_no_disponibles={k: v for k, v in no_disponibles.items() if v},
        advertencias=advertencias,
        fuentes=fuentes,
        ventana={"desde": desde_amp.isoformat(), "hasta": hasta_amp.isoformat(), "margen_dias": str(margen_dias)},
        llamadas=lector.llamadas,
        companias_ids=ids,
    )
