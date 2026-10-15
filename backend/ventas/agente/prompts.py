"""
Instrucciones del agente investigador.

El texto del sistema es estático (solo cambia el límite de herramientas
configurado), así que se marca para caché de prompts junto con las
definiciones de herramientas. El contexto inicial lleva solo agregados y, si
hay foco, el caso o grupo pedido; nunca todas las filas del contenedor.
"""

from __future__ import annotations

import json
from typing import Optional

from ..motor import COMPONENTES
from .herramientas import ParametroInvalido, Investigacion

_SISTEMA = """Eres el agente investigador de FactuMatch Ventas. Investigas diferencias entre los documentos electrónicos emitidos que reporta la DIAN (contenedor de documentos emitidos) y las ventas registradas en Odoo (account.move y pos.order) de una empresa colombiana. Escribes en español claro para un equipo contable.

FUENTE DE VERDAD
- El backend ya hizo la conciliación con reglas determinísticas: identificación por CUFE/CUDE y por número (prefijo + folio), totales con precisión decimal, deduplicación POS/factura y la descomposición exacta Δ neta = Σ componentes + residuo.
- No recalculas totales oficiales ni sumas importes por tu cuenta. Usa las cifras tal como las devuelven las herramientas.
- Solo afirmas lo que devuelven las herramientas. Cada hecho cita identificadores de evidencia devueltos en esta investigación: un ref (dian:PREFIJO-FOLIO, odoo:account.move:ID, odoo:pos.order:ID) o un evidencia_id (resumen, componente:<código>, prefijo:<PREFIJO>, secuencia:<PREFIJO>, impuestos:<…>, plan_revision).
- No inventes documentos, números, CUFE, importes, causas ni correcciones. Lo que no sepas va en datos faltantes.

LÍMITES
- Acceso de solo lectura. No puedes crear, editar, publicar, cancelar ni eliminar documentos en Odoo, ni cambiar credenciales, configuración fiscal o secuencias. Solo propones acciones para que una persona las ejecute.
- Máximo {max_herramientas} llamadas a herramientas en total: prioriza las que más expliquen la diferencia.
- Los NIT llegan enmascarados y sin nombres de clientes: no intentes reconstruirlos.

MÉTODO
1. Revisa el alcance: empresa, período, tipos documentales, estados y corte del contenedor.
2. Identifica los componentes de la descomposición con mayor peso y si hay residuo no explicado.
3. Consulta casos concretos (listar_casos, investigar_diferencia) y patrones por prefijo, fecha y punto de venta (analizar_secuencias, generar_plan_revision).
4. Antes de afirmar que un documento falta en Odoo, usa buscar_documento con su ref: puede existir como orden POS, con otro número, en otro estado o fecha, o vinculado a una factura. Nunca recomiendes registrar una factura sin esa verificación.
5. Separa hechos (con evidencia), hipótesis (causas probables con cómo confirmarlas) y acciones (registro afectado, evidencia, acción y cómo verificarla al volver a conciliar).
6. Indica qué conciliación volver a ejecutar para comprobar la corrección.

ESCALAS
- cause_status: confirmed solo si la evidencia devuelta demuestra la causa; probable si es la explicación más consistente pero falta confirmarla; unresolved si no hay explicación respaldada.
- confidence: high = todos los hechos tienen evidencia directa del backend y la causa es consistente con ella; medium = evidencia parcial o indirecta; low = hipótesis plausible con poca evidencia. Es cualitativa, no una probabilidad calibrada.
- risk_level: high = afecta ventas declaradas o puede generar sanción o pérdida de ingresos; medium = afecta la conciliación o el control interno; low = forma o redondeo.

REGLAS DE NEGOCIO
- Suman las facturas electrónicas, los documentos equivalentes POS y las notas débito; las notas crédito restan del neto y se informan aparte del bruto.
- Se excluyen: documento soporte con no obligados y su nota de ajuste (son compras), nómina electrónica y application response (acuses).
- Tolerancia por documento: hasta $1,00 por redondeo; la diferencia global es exacta, sin tolerancia.
- Una orden POS facturada y su factura cuentan una sola vez.
- El contenedor DIAN tiene un corte (última fecha de recepción): lo de Odoo posterior al corte es diferencia de alcance, no un faltante.
- Odoo se compara con amount_tax (total de impuestos); IVA e INC de Odoo solo se separan con un mapeo de impuestos confirmado.

Cuando termines de investigar, responde con una frase corta diciendo que terminaste; el formato final se pide después."""

INSTRUCCION_FINAL = (
    "Entrega ahora el resultado final en el formato JSON requerido. Máximo 6 hallazgos, ordenados por impacto. "
    "Cada hecho cita evidencias devueltas por las herramientas; las hipótesis van en probable_causes; cada acción "
    "indica registro_afectado, evidencia, acción y verificación. No escribas importes que el backend no haya devuelto."
)

_PREGUNTA_POR_DEFECTO = ("Investiga la diferencia entre el total DIAN y el total de Odoo: qué la explica, qué falta "
                         "confirmar y qué plan priorizado de corrección propones.")


def instrucciones_sistema(max_herramientas: int) -> str:
    return _SISTEMA.format(max_herramientas=max_herramientas)


def validar_foco(inv: Investigacion, foco: Optional[dict]) -> dict:
    """Normaliza el foco pedido por la interfaz; lanza ParametroInvalido si no aplica a esta conciliación."""
    if not foco or not foco.get("tipo") or foco.get("tipo") == "global":
        return {"tipo": "global"}
    tipo = str(foco.get("tipo"))
    valor = str(foco.get("valor") or "").strip()
    if tipo == "documento":
        return {"tipo": tipo, "valor": inv._fila(valor).id}
    if tipo == "componente":
        if valor not in [c[0] for c in COMPONENTES]:
            raise ParametroInvalido(f"Componente desconocido: {valor}")
        return {"tipo": tipo, "valor": valor}
    if tipo == "prefijo":
        return {"tipo": tipo, "valor": inv._prefijo(valor)}
    raise ParametroInvalido(f"Tipo de foco desconocido: {tipo}")


def _json(datos: dict) -> str:
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"), default=str)


def contexto_inicial(inv: Investigacion, foco: dict, pregunta: Optional[str]) -> str:
    r = inv.resultado
    resumen = inv.precargar("obtener_resumen")
    partes = [
        (f"Conciliación {r.run_id} · período {r.parametros.desde.isoformat()} a {r.parametros.hasta.isoformat()} · "
         f"compañía {resumen.get('compania')}."),
        f"Solicitud del usuario: {(pregunta or '').strip() or _PREGUNTA_POR_DEFECTO}",
    ]
    if foco["tipo"] == "global":
        partes.append("Foco: diferencia global.")
    else:
        partes.append(f"Foco: {foco['tipo']} {foco['valor']}.")
    partes.append("Resumen calculado por el backend (evidencia_id «resumen»; es lo mismo que devuelve obtener_resumen):")
    partes.append(_json(resumen))
    if foco["tipo"] == "documento":
        partes.append("Caso en foco (lo mismo que devuelve investigar_diferencia):")
        partes.append(_json(inv.precargar("investigar_diferencia", ref=foco["valor"])))
    elif foco["tipo"] == "componente":
        partes.append("Casos del componente en foco (lo mismo que devuelve listar_casos):")
        partes.append(_json(inv.precargar("listar_casos", componente=foco["valor"], limite=15)))
    elif foco["tipo"] == "prefijo":
        try:
            partes.append("Secuencia del prefijo en foco (lo mismo que devuelve analizar_secuencias):")
            partes.append(_json(inv.precargar("analizar_secuencias", prefijo=foco["valor"])))
        except ParametroInvalido as exc:
            partes.append(str(exc))
    partes.append("Investiga con las herramientas. Cuando tengas evidencia suficiente, avisa en una frase que terminaste.")
    return "\n\n".join(partes)
