"""
Endpoints de FactuMatch Ventas: /api/ventas/…

No modifica ni comparte rutas con compras. El trabajo pesado (lectura del
Excel, Odoo, conciliación, reporte, agente) corre en hilos para no bloquear el
servidor. Todo acceso a Odoo es de solo lectura (OdooSoloLectura).

  POST /api/ventas/analizar-contenedor  valida el contenedor DIAN (sin Odoo)
  POST /api/ventas/conciliar            conciliación individual y global
  GET  /api/ventas/documento            evidencia completa de un documento
  GET  /api/ventas/reporte              Excel de la conciliación (+ hallazgos IA si hay)
  POST /api/ventas/investigar           agente Claude (opcional; si falla, la conciliación sigue)
  GET  /api/ventas/estado-ia            si Claude está configurado (nunca devuelve la clave)
"""

from __future__ import annotations

import logging
import threading
import xmlrpc.client
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from odoo_match import CredentialManager, OdooAuthError, OdooConnectionError, OdooConnector

from . import almacen
from .agente import investigar as investigar_con_claude
from .canonico import construir_conjunto
from .config import ConfigVentas, cargar_config
from .dian_emitidos import COLUMNAS_REQUERIDAS, TODAS_LAS_COLUMNAS, LecturaContenedor, leer_contenedor
from .motor import ParametrosConciliacion, ResultadoConciliacion, conciliar
from .normalizacion import parsear_fecha
from .odoo_ventas import (
    AccesoNoPermitido,
    AlcanceInvalido,
    LecturaTruncada,
    OdooSoloLectura,
    PermisoInsuficiente,
    crear_desde_conector,
    extraer_ventas,
)
from .reporte import generar_reporte

logger = logging.getLogger("factumatch.ventas")

router = APIRouter(prefix="/api/ventas", tags=["ventas"])

# Un contenedor real de ~3.000 documentos pesa ~0,7 MB; al procesarlo ocupa ~20 veces su tamaño en memoria.
MAX_ARCHIVO = 10 * 1024 * 1024
MAX_DIAS_PERIODO = 93
_USO_COLUMNAS = {
    "Tipo de documento": "Regla de inclusión y signo",
    "CUFE/CUDE": "Identificación (ruta 1)",
    "Folio": "Identificación (ruta 2) y secuencias",
    "Prefijo": "Identificación (ruta 2) y secuencias",
    "Fecha Emisión": "Período",
    "Fecha Recepción": "Corte del contenedor",
    "NIT Emisor": "Empresa",
    "NIT Receptor": "Validación del cliente",
    "Nombre Receptor": "Solo en pantalla y reporte (no se envía a la IA)",
    "IVA": "Impuestos",
    "INC": "Impuestos",
    "Total": "Importe comparado con amount_total",
    "Estado": "Solo aprobados",
    "Grupo": "Solo Emitido",
    "Rete IVA": "Informativo (no altera el total)",
    "Rete Renta": "Informativo (no altera el total)",
    "Rete ICA": "Informativo (no altera el total)",
}

_investigaciones_en_curso: set[str] = set()
_cupo_investigaciones = threading.BoundedSemaphore(2)
_lock = threading.Lock()


# ──────────────────────────────────────────────
# AUXILIARES
# ──────────────────────────────────────────────

async def _leer_archivo(archivo: UploadFile) -> tuple[bytes, str]:
    nombre = archivo.filename or "contenedor.xlsx"
    if not nombre.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="El contenedor DIAN debe ser un archivo .xlsx o .xls")
    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="El archivo está vacío")
    if len(contenido) > MAX_ARCHIVO:
        raise HTTPException(status_code=413, detail="El archivo supera el máximo de 10 MB: exporta un período más corto")
    return contenido, nombre


def _leer_contenedor(contenido: bytes, nombre: str) -> LecturaContenedor:
    try:
        return leer_contenedor(contenido, nombre)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # archivo dañado, zip inválido, etc.
        logger.warning("ventas: contenedor ilegible (%s)", type(exc).__name__)
        raise HTTPException(status_code=422, detail="No se pudo leer el contenedor DIAN: el archivo parece dañado") from None


def _mapeo_columnas(lectura: LecturaContenedor) -> list[dict]:
    faltan = set(lectura.columnas_opcionales_faltantes)
    return [{
        "columna": c,
        "requerida": c in COLUMNAS_REQUERIDAS,
        "reconocida": c not in faltan,
        "uso": _USO_COLUMNAS.get(c, "Otros impuestos (suman a impuestos)" if c not in ("Divisa", "Forma de Pago", "Medio de Pago", "Nombre Emisor") else "Contexto"),
    } for c in TODAS_LAS_COLUMNAS]


def _periodo(desde: str, hasta: str) -> tuple[date, date]:
    d, h = parsear_fecha(desde), parsear_fecha(hasta)
    if d is None or h is None:
        raise HTTPException(status_code=422, detail="Las fechas deben tener formato AAAA-MM-DD")
    if d > h:
        raise HTTPException(status_code=422, detail="La fecha inicial es posterior a la final")
    if (h - d).days + 1 > MAX_DIAS_PERIODO:
        raise HTTPException(status_code=422, detail=f"El período no puede superar {MAX_DIAS_PERIODO} días")
    return d, h


def _tolerancia(valor: Optional[str], config: ConfigVentas) -> Decimal:
    if valor is None or str(valor).strip() == "":
        return config.tolerancia_documento
    try:
        t = Decimal(str(valor).replace(",", ".").strip())
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="Tolerancia inválida") from None
    if not t.is_finite():
        raise HTTPException(status_code=422, detail="Tolerancia inválida")
    if t < 0 or t > 1000:
        raise HTTPException(status_code=422, detail="La tolerancia por documento debe estar entre 0 y 1.000")
    return t


def _si_no(valor: Optional[str], defecto: bool = True) -> bool:
    if valor is None or str(valor).strip() == "":
        return defecto
    return str(valor).strip().lower() in ("1", "true", "si", "sí", "on", "yes")


def _conector(credenciales_cifradas: str) -> OdooConnector:
    try:
        creds = CredentialManager().decrypt(credenciales_cifradas)
        return OdooConnector(url=creds["url"], database=creds["database"],
                             username=creds["username"], api_key=creds["api_key"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Credenciales de Odoo inválidas o vencidas: vuelve a conectar Odoo") from None


def _lector(credenciales_cifradas: str, segundos: float = 120.0) -> OdooSoloLectura:
    conector = _conector(credenciales_cifradas)
    try:
        return crear_desde_conector(conector, segundos_por_llamada=segundos)
    except OdooAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OdooConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


def _resultado(run_id: str) -> ResultadoConciliacion:
    resultado = almacen.obtener((run_id or "").strip())
    if resultado is None:
        raise HTTPException(status_code=404, detail="La conciliación ya no está en memoria (expiró o el servidor se reinició): vuelve a ejecutarla")
    return resultado


def _conciliar(contenido: bytes, nombre: str, credenciales: str, parametros: ParametrosConciliacion,
               config: ConfigVentas) -> ResultadoConciliacion:
    lectura = _leer_contenedor(contenido, nombre)
    errores = [v for v in lectura.validaciones if v["nivel"] == "error"]
    if errores:
        raise HTTPException(status_code=422, detail="; ".join(
            v["mensaje"] + (f" ({v['detalle']})" if v.get("detalle") else "") for v in errores))
    nits = lectura.nits_emisores()
    if len(nits) != 1:
        raise HTTPException(status_code=422, detail="El contenedor debe tener un único NIT emisor")
    if not lectura.incluidos:
        raise HTTPException(status_code=422, detail="El contenedor no tiene documentos de venta para conciliar")

    lector = _lector(credenciales)
    try:
        extraccion = extraer_ventas(lector, next(iter(nits)), parametros.desde, parametros.hasta,
                                    margen_dias=config.margen_dias_busqueda,
                                    incluir_move=parametros.incluir_move, incluir_pos=parametros.incluir_pos)
    except (AlcanceInvalido, LecturaTruncada, PermisoInsuficiente) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except AccesoNoPermitido as exc:  # no debería ocurrir: el extractor solo lee
        raise HTTPException(status_code=500, detail=str(exc)) from None
    except xmlrpc.client.Fault as exc:
        ultima = (exc.faultString or "").strip().splitlines()[-1:] or [""]
        raise HTTPException(status_code=502, detail=f"Odoo respondió con error: {ultima[0][:200]}") from None
    except (OSError, xmlrpc.client.ProtocolError) as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo leer Odoo ({type(exc).__name__}): intenta de nuevo") from None

    conjunto = construir_conjunto(extraccion, parametros.desde, parametros.hasta, lectura.prefijos_conocidos)
    resultado = conciliar(lectura, extraccion, conjunto, parametros)
    extraccion.liberar_registros()
    return resultado


# ──────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────

@router.post("/analizar-contenedor")
async def analizar_contenedor(dian: UploadFile = File(...)):
    """Valida estructura, columnas y tipos del contenedor DIAN. No consulta Odoo."""
    contenido, nombre = await _leer_archivo(dian)
    lectura = await run_in_threadpool(_leer_contenedor, contenido, nombre)
    datos = lectura.a_dict()
    desde, hasta = lectura.rango_fechas()
    return {
        **datos,
        "mapeo_columnas": _mapeo_columnas(lectura),
        "periodo_sugerido": {"desde": desde.isoformat() if desde else None, "hasta": hasta.isoformat() if hasta else None},
        "bloqueante": any(v["nivel"] == "error" for v in lectura.validaciones),
    }


@router.post("/conciliar")
async def conciliar_ventas(
    dian: UploadFile = File(...),
    credentials: str = Form(...),
    desde: str = Form(...),
    hasta: str = Form(...),
    incluir_move: Optional[str] = Form(None),
    incluir_pos: Optional[str] = Form(None),
    tolerancia: Optional[str] = Form(None),
):
    """Concilia el contenedor DIAN contra account.move y pos.order (solo lectura)."""
    config = cargar_config()
    contenido, nombre = await _leer_archivo(dian)
    d, h = _periodo(desde, hasta)
    parametros = ParametrosConciliacion(desde=d, hasta=h, tolerancia_documento=_tolerancia(tolerancia, config),
                                        incluir_move=_si_no(incluir_move), incluir_pos=_si_no(incluir_pos))
    if not (parametros.incluir_move or parametros.incluir_pos):
        raise HTTPException(status_code=422, detail="Elige al menos una fuente de Odoo (account.move o pos.order)")
    resultado = await run_in_threadpool(_conciliar, contenido, nombre, credentials, parametros, config)
    almacen.guardar(resultado)
    return resultado.a_dict()


@router.get("/documento")
async def documento(run_id: str = Query(...), ref: str = Query(...)):
    """Evidencia completa de un documento de la conciliación."""
    resultado = _resultado(run_id)
    fila = resultado.fila(ref)
    if fila is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado en esta conciliación")
    datos = fila.a_dict()
    datos["candidatos_detalle"] = [c.a_dict() for c in fila.candidatos[:10]]
    return datos


@router.get("/reporte")
async def reporte(run_id: str = Query(...)):
    """Excel de la conciliación. Se genera aunque el análisis de Claude no exista o haya fallado."""
    resultado = _resultado(run_id)
    contenido = await run_in_threadpool(generar_reporte, resultado)
    nombre = f"conciliacion_ventas_{resultado.parametros.desde.isoformat()}_{resultado.parametros.hasta.isoformat()}.xlsx"
    return StreamingResponse(
        iter([contenido]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.post("/investigar")
async def investigar(
    run_id: str = Form(...),
    foco_tipo: Optional[str] = Form(None),
    foco_valor: Optional[str] = Form(None),
    pregunta: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
):
    """
    Investigación de Claude sobre una conciliación guardada. Siempre responde 200 con
    {"disponible": false, "mensaje": …} si Claude no está configurado o falla.
    """
    run_id = (run_id or "").strip()
    resultado = _resultado(run_id)
    if pregunta and len(pregunta) > 500:
        raise HTTPException(status_code=422, detail="La pregunta admite máximo 500 caracteres")
    with _lock:
        if run_id in _investigaciones_en_curso:
            raise HTTPException(status_code=409, detail="Ya hay una investigación en curso para esta conciliación")
        if not _cupo_investigaciones.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Hay demasiadas investigaciones en curso: intenta en un momento")
        _investigaciones_en_curso.add(run_id)
    try:
        lector = None
        aviso = None
        if credentials and cargar_config().ia_configurada:
            try:
                # Durante la investigación cada consulta a Odoo tiene un tope corto.
                lector = await run_in_threadpool(_lector, credentials, 20.0)
            except HTTPException as exc:
                aviso = f"Sin consulta en vivo a Odoo ({exc.detail}): el agente usa lo leído en la conciliación"
            except Exception as exc:  # la investigación nunca falla por la conexión a Odoo
                logger.warning("ventas: sin Odoo en vivo para la investigación (%s)", type(exc).__name__)
                aviso = "Sin consulta en vivo a Odoo: el agente usa lo leído en la conciliación"
        foco = {"tipo": foco_tipo, "valor": foco_valor} if foco_tipo else None
        hallazgos = await run_in_threadpool(investigar_con_claude, resultado, foco, pregunta, lector)
        if aviso:
            hallazgos["aviso_odoo"] = aviso
        almacen.guardar_hallazgos(run_id, hallazgos)
        return hallazgos
    finally:
        with _lock:
            _investigaciones_en_curso.discard(run_id)
            _cupo_investigaciones.release()


@router.get("/estado-ia")
async def estado_ia():
    config = cargar_config()
    return {
        "proveedor": "Anthropic (Claude)",
        "configurada": config.ia_configurada,
        "modelo": config.anthropic_model,
        "limites": {
            "herramientas": config.ia_max_herramientas,
            "segundos_totales": config.ia_timeout_total_segundos,
            "segundos_por_llamada": config.ia_timeout_llamada_segundos,
            "max_tokens": config.ia_max_tokens,
        },
        "acceso_odoo": "solo lectura",
    }
