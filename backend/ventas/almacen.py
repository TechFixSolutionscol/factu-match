"""
Almacén en memoria de las conciliaciones recientes (para el reporte y el agente).

Primera versión sin base de datos: se guardan como máximo VENTAS_MAX_RESULTADOS
conciliaciones (5 por defecto) durante VENTAS_TTL_RESULTADOS segundos (1 hora
por defecto) y se pierden si Render reinicia el servicio. Si eso pasa, la
interfaz pide volver a ejecutar la conciliación.
"""

from __future__ import annotations

import threading
from typing import Optional

from cachetools import TTLCache

from .config import cargar_config
from .motor import ResultadoConciliacion

_lock = threading.Lock()
_cache: Optional[TTLCache] = None


def _almacen() -> TTLCache:
    global _cache
    if _cache is None:
        config = cargar_config()
        _cache = TTLCache(maxsize=config.max_resultados, ttl=config.ttl_resultados_segundos)
    return _cache


def guardar(resultado: ResultadoConciliacion) -> None:
    with _lock:
        _almacen()[resultado.run_id] = resultado


def obtener(run_id: str) -> Optional[ResultadoConciliacion]:
    with _lock:
        return _almacen().get((run_id or "").strip())


def guardar_hallazgos(run_id: str, hallazgos: dict) -> None:
    with _lock:
        resultado = _almacen().get(run_id)
        if resultado is not None:
            resultado.hallazgos_ia = hallazgos


def limpiar() -> None:
    """Vacía el almacén y vuelve a leer la configuración en el próximo uso (pruebas)."""
    global _cache
    with _lock:
        _cache = None
