"""
Proveedor Anthropic (Claude) del agente investigador.

Flujo:
  1. Contexto inicial con agregados del backend (y el caso o grupo en foco).
  2. Bucle de herramientas de solo lectura con límite de llamadas y de tiempo.
  3. Llamada final sin herramientas y con salida estructurada (JSON Schema).
  4. Validación en el backend: esquema + reglas anti-invención.

Cualquier falla (clave, cuota, timeout, respuesta inválida…) devuelve
{"disponible": False, "mensaje": "Análisis IA no disponible: …"} y deja intacta
la conciliación determinística. La clave nunca se registra ni se devuelve.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..config import ConfigVentas, cargar_config
from ..motor import ResultadoConciliacion
from ..odoo_ventas import OdooSoloLectura
from .esquema import ESQUEMA_SALIDA, ContextoValidacion, aplicar_reglas, validar_estructura
from .herramientas import DEFINICIONES, Investigacion, ParametroInvalido
from .prompts import INSTRUCCION_FINAL, contexto_inicial, instrucciones_sistema, validar_foco

logger = logging.getLogger("factumatch.ventas.ia")

MENSAJES = {
    "no_configurada": "Análisis IA no disponible: falta configurar ANTHROPIC_API_KEY en el servidor.",
    "sin_sdk": "Análisis IA no disponible: falta instalar el paquete anthropic en el servidor.",
    "autenticacion": "Análisis IA no disponible: Anthropic rechazó la clave o no tiene permiso para el modelo configurado.",
    "cuota": "Análisis IA no disponible: se alcanzó el límite de uso, de crédito o de velocidad de Anthropic. Intenta más tarde.",
    "timeout": "Análisis IA no disponible: la investigación superó el tiempo máximo.",
    "conexion": "Análisis IA no disponible: no hubo conexión con Anthropic.",
    "no_disponible": "Análisis IA no disponible: el servicio de Anthropic está saturado o no responde. Intenta más tarde.",
    "modelo": "Análisis IA no disponible: el modelo de ANTHROPIC_MODEL no existe o no está habilitado para la clave.",
    "solicitud": "Análisis IA no disponible: Anthropic rechazó la solicitud.",
    "contexto": "Análisis IA no disponible: el contexto de la investigación fue demasiado grande.",
    "respuesta_invalida": "Análisis IA no disponible: la respuesta de Claude no cumplió el formato esperado.",
    "respuesta_incompleta": "Análisis IA no disponible: la respuesta de Claude quedó incompleta.",
    "rechazo": "Análisis IA no disponible: Claude no completó la solicitud.",
    "foco_invalido": "Análisis IA no disponible: el foco pedido no existe en esta conciliación.",
    "error": "Análisis IA no disponible por un error inesperado.",
}
MENSAJE_OK = ("Interpretación de Claude con evidencia del backend. Las hipótesis no son hechos contables "
              "confirmados y ninguna acción se ejecuta en Odoo.")


class FalloProveedor(Exception):
    def __init__(self, codigo: str, detalle: Optional[list[str]] = None):
        super().__init__(codigo)
        self.codigo = codigo
        self.detalle = detalle or []


def clasificar_error(exc: BaseException) -> str:
    """Traduce errores del SDK a códigos estables (sin exponer detalles sensibles)."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return "error"
    if isinstance(exc, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "conexion"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "autenticacion"
    if isinstance(exc, anthropic.RateLimitError):
        return "cuota"
    if isinstance(exc, anthropic.NotFoundError):
        return "modelo"
    if isinstance(exc, anthropic.RequestTooLargeError):
        return "contexto"
    if isinstance(exc, (anthropic.InternalServerError, anthropic.OverloadedError,
                        anthropic.ServiceUnavailableError, anthropic.DeadlineExceededError)):
        return "no_disponible"
    if isinstance(exc, anthropic.BadRequestError):
        texto = str(getattr(exc, "message", "") or exc).lower()
        if any(p in texto for p in ("credit", "billing", "quota", "usage limit")):
            return "cuota"
        if "model" in texto:
            return "modelo"
        if any(p in texto for p in ("too long", "too many tokens", "context window", "prompt is too long")):
            return "contexto"
        return "solicitud"
    if isinstance(exc, anthropic.APIStatusError):
        return "no_disponible" if (getattr(exc, "status_code", 0) or 0) >= 500 else "solicitud"
    return "error"


def _crear_cliente(config: ConfigVentas):
    try:
        import anthropic
    except ImportError:
        raise FalloProveedor("sin_sdk") from None
    # Sin reintentos automáticos: cada reintento usaría el mismo tiempo por llamada y podría
    # superar VENTAS_IA_TIMEOUT_TOTAL. Una falla transitoria se informa como «no disponible».
    return anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=float(config.ia_timeout_llamada_segundos),
                               max_retries=0)


MARGEN_BUCLE_SEGUNDOS = 5


def _tipo(bloque: Any) -> Optional[str]:
    return bloque.get("type") if isinstance(bloque, dict) else getattr(bloque, "type", None)


def _es_timeout(exc: BaseException) -> bool:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, anthropic.APITimeoutError)


def _bloques(contenido: Any) -> list[dict]:
    """Lee el texto y las llamadas a herramientas de una respuesta (objetos del SDK o dicts)."""
    salida = []
    for b in contenido or []:
        tipo = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        g = (lambda k, b=b: b.get(k) if isinstance(b, dict) else getattr(b, k, None))
        if tipo == "text":
            if g("text"):
                salida.append({"type": "text", "text": g("text")})
        elif tipo == "tool_use":
            salida.append({"type": "tool_use", "id": g("id"), "name": g("name"), "input": g("input") or {}})
    return salida


def _con_cache(mensajes: list[dict]) -> list[dict]:
    """Copia los mensajes y marca el último bloque para caché incremental de la conversación."""
    if not mensajes:
        return mensajes
    copia = list(mensajes)
    ultimo = copy.deepcopy(copia[-1])
    contenido = ultimo.get("content")
    if isinstance(contenido, str):
        contenido = [{"type": "text", "text": contenido}]
    if contenido:
        contenido[-1] = {**contenido[-1], "cache_control": {"type": "ephemeral"}}
    ultimo["content"] = contenido
    copia[-1] = ultimo
    return copia


def _sumar_uso(uso: dict, respuesta: Any) -> None:
    u = getattr(respuesta, "usage", None)
    uso["llamadas_modelo"] += 1
    for campo in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        valor = getattr(u, campo, None) if u is not None else None
        if isinstance(valor, int):
            uso[campo] += valor


def _texto_respuesta(respuesta: Any) -> str:
    return "".join(b["text"] for b in _bloques(getattr(respuesta, "content", None)) if b["type"] == "text")


def investigar(resultado: ResultadoConciliacion, foco: Optional[dict] = None, pregunta: Optional[str] = None,
               lector: Optional[OdooSoloLectura] = None, config: Optional[ConfigVentas] = None,
               cliente: Any = None, reloj: Callable[[], float] = time.monotonic) -> dict:
    config = config or cargar_config()
    inicio = reloj()
    inv = Investigacion(resultado, lector=lector, max_llamadas=config.ia_max_herramientas, reloj=reloj,
                        fin=inicio + config.ia_timeout_total_segundos * 0.6)
    uso = {"llamadas_modelo": 0, "input_tokens": 0, "output_tokens": 0,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    base = {
        "modelo": config.anthropic_model,
        "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "foco": {"tipo": "global"},
        "pregunta": (pregunta or "").strip()[:500] or None,
        "odoo_en_vivo": lector is not None,
    }
    codigo = "ok"
    detalle: list[str] = []
    salida: dict = {}
    try:
        try:
            base["foco"] = validar_foco(inv, foco)
        except ParametroInvalido as exc:
            raise FalloProveedor("foco_invalido", [str(exc)]) from None
        if cliente is None:
            if not config.ia_configurada:
                raise FalloProveedor("no_configurada")
            cliente = _crear_cliente(config)

        sistema = [{"type": "text", "text": instrucciones_sistema(config.ia_max_herramientas),
                    "cache_control": {"type": "ephemeral"}}]
        mensajes: list[dict] = [{"role": "user", "content": [
            {"type": "text", "text": contexto_inicial(inv, base["foco"], base["pregunta"])}]}]
        fin = inicio + config.ia_timeout_total_segundos
        fin_bucle = inicio + config.ia_timeout_total_segundos * 0.6

        def llamar(final: bool) -> Any:
            restante = (fin if final else fin_bucle) - reloj()
            if restante <= 1:
                raise FalloProveedor("timeout")
            parametros = {
                "model": config.anthropic_model,
                "max_tokens": config.ia_max_tokens,
                "system": sistema,
                "tools": DEFINICIONES,
                "messages": _con_cache(mensajes),
                "tool_choice": {"type": "none"} if final else {"type": "auto"},
                "timeout": min(float(config.ia_timeout_llamada_segundos), restante),
            }
            if final:
                parametros["output_config"] = {"format": {"type": "json_schema", "schema": ESQUEMA_SALIDA}}
            respuesta = cliente.messages.create(**parametros)
            _sumar_uso(uso, respuesta)
            if getattr(respuesta, "stop_reason", None) == "refusal":
                raise FalloProveedor("rechazo")
            return respuesta

        # Bucle de investigación con herramientas. Si se agota su parte del tiempo, se pasa a la
        # respuesta final con la evidencia reunida (no se descarta la investigación).
        while not inv.limite_alcanzado and fin_bucle - reloj() > MARGEN_BUCLE_SEGUNDOS:
            try:
                respuesta = llamar(final=False)
            except FalloProveedor as exc:
                if exc.codigo == "timeout":
                    break
                raise
            except Exception as exc:
                if _es_timeout(exc):
                    break
                raise
            originales = list(getattr(respuesta, "content", None) or [])
            usos = [b for b in _bloques(originales) if b["type"] == "tool_use"]
            if getattr(respuesta, "stop_reason", None) != "tool_use" or not usos:
                # Turno sin herramientas (o cortado): se conserva tal cual, incluidos los bloques de
                # razonamiento, pero sin llamadas a herramientas que quedarían sin respuesta.
                conservados = [b for b in originales if _tipo(b) != "tool_use"]
                if not any(_tipo(b) == "text" for b in conservados):
                    conservados.append({"type": "text", "text": "Terminé de investigar."})
                mensajes.append({"role": "assistant", "content": conservados})
                break
            # Los bloques de razonamiento (thinking) deben volver a la API completos y sin modificar.
            mensajes.append({"role": "assistant", "content": originales})
            resultados = []
            for b in usos:
                texto, es_error = inv.ejecutar(b["name"], b["input"])
                bloque = {"type": "tool_result", "tool_use_id": b["id"], "content": texto}
                if es_error:
                    bloque["is_error"] = True
                resultados.append(bloque)
            mensajes.append({"role": "user", "content": resultados})

        # Respuesta final estructurada
        if mensajes[-1]["role"] == "user":
            mensajes[-1] = {"role": "user", "content": list(mensajes[-1]["content"]) + [{"type": "text", "text": INSTRUCCION_FINAL}]}
        else:
            mensajes.append({"role": "user", "content": [{"type": "text", "text": INSTRUCCION_FINAL}]})
        respuesta = llamar(final=True)
        if getattr(respuesta, "stop_reason", None) == "max_tokens":
            raise FalloProveedor("respuesta_incompleta")
        try:
            datos = json.loads(_texto_respuesta(respuesta))
        except (json.JSONDecodeError, TypeError):
            raise FalloProveedor("respuesta_invalida", ["La respuesta no es JSON válido"]) from None
        errores = validar_estructura(datos)
        if errores:
            raise FalloProveedor("respuesta_invalida", errores[:6])
        depurado, informe = aplicar_reglas(
            datos, ContextoValidacion(resultado, inv.evidencias, inv.importes, inv.busquedas_alternativas))
        salida = {**base, "disponible": True, "mensaje": MENSAJE_OK, "resultado": depurado, "validacion": informe}
    except FalloProveedor as exc:
        codigo, detalle = exc.codigo, exc.detalle
    except Exception as exc:  # errores del SDK u otros: nunca rompen la conciliación
        codigo = clasificar_error(exc)
        logger.warning("ventas.ia error=%s tipo=%s", codigo, type(exc).__name__)

    if codigo != "ok":
        salida = {**base, "disponible": False, "codigo": codigo, "mensaje": MENSAJES.get(codigo, MENSAJES["error"])}
        if detalle:
            salida["detalle"] = detalle
    segundos = round(reloj() - inicio, 1)
    salida["traza"] = inv.traza
    salida["uso"] = {**uso, "herramientas": inv.llamadas, "limite_herramientas": config.ia_max_herramientas,
                     "limite_alcanzado": inv.limite_alcanzado, "segundos": segundos}
    logger.info(
        "ventas.ia run=%s modelo=%s resultado=%s llamadas_modelo=%d herramientas=%d tokens_entrada=%d "
        "tokens_salida=%d cache_lectura=%d cache_escritura=%d segundos=%.1f",
        resultado.run_id, config.anthropic_model, codigo, uso["llamadas_modelo"], inv.llamadas,
        uso["input_tokens"], uso["output_tokens"], uso["cache_read_input_tokens"],
        uso["cache_creation_input_tokens"], segundos)
    return salida
