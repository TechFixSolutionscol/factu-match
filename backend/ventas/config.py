"""
Configuración del módulo de ventas leída desde variables de entorno.

Ninguna clave se guarda en el código. Los valores por defecto reflejan las
decisiones tomadas con el negocio (ver docs/VENTAS.md):

- Tolerancia por documento: $1,00 (redondeos de INC/IVA en POS).
- La diferencia global se informa exacta, sin tolerancia.
- Modelo por defecto del agente: claude-haiku-5-5.
- max_tokens incluye el razonamiento adaptativo (thinking), activo por defecto en ese modelo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def _decimal_env(nombre: str, defecto: str) -> Decimal:
    valor = os.getenv(nombre, defecto)
    try:
        return Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal(defecto)


def _int_env(nombre: str, defecto: int, minimo: int, maximo: int) -> int:
    try:
        valor = int(os.getenv(nombre, str(defecto)))
    except ValueError:
        valor = defecto
    return max(minimo, min(maximo, valor))


@dataclass(frozen=True)
class ConfigVentas:
    tolerancia_documento: Decimal
    margen_dias_busqueda: int
    ttl_resultados_segundos: int
    max_resultados: int
    anthropic_api_key: str = field(repr=False)   # nunca en logs ni en repr
    anthropic_model: str
    ia_max_herramientas: int
    ia_timeout_total_segundos: int
    ia_timeout_llamada_segundos: int
    ia_max_tokens: int

    @property
    def ia_configurada(self) -> bool:
        return bool(self.anthropic_api_key)


def cargar_config() -> ConfigVentas:
    """Lee la configuración en cada llamada para respetar cambios de entorno en pruebas."""
    return ConfigVentas(
        tolerancia_documento=_decimal_env("VENTAS_TOLERANCIA_DOCUMENTO", "1.00"),
        margen_dias_busqueda=_int_env("VENTAS_MARGEN_DIAS", 5, 0, 31),
        ttl_resultados_segundos=_int_env("VENTAS_TTL_RESULTADOS", 3600, 60, 86400),
        max_resultados=_int_env("VENTAS_MAX_RESULTADOS", 5, 1, 50),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5").strip() or "claude-haiku-5-5",
        ia_max_herramientas=_int_env("VENTAS_IA_MAX_HERRAMIENTAS", 12, 1, 30),
        ia_timeout_total_segundos=_int_env("VENTAS_IA_TIMEOUT_TOTAL", 120, 10, 600),
        ia_timeout_llamada_segundos=_int_env("VENTAS_IA_TIMEOUT_LLAMADA", 60, 5, 300),
        ia_max_tokens=_int_env("VENTAS_IA_MAX_TOKENS", 16000, 2048, 64000),  # incluye el razonamiento (thinking)
    )
