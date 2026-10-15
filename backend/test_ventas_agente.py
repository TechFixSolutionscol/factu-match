"""
Agente investigador (Claude) con un cliente falso: nunca llama a la API real.

Cubre: salida estructurada válida e inválida, timeout, cuota, autenticación,
caída del proveedor sin afectar la conciliación, anti-invención, límites de
herramientas y de tiempo, causas probables vs confirmadas, solo lectura y
minimización de datos personales.
"""

import copy
import dataclasses
import json
import logging
from decimal import Decimal
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from ventas.agente import investigar
from ventas.agente.esquema import ESQUEMA_SALIDA, importes_en_texto, validar_estructura
from ventas.agente.herramientas import DEFINICIONES, Investigacion
from ventas.agente.prompts import INSTRUCCION_FINAL
from ventas.config import cargar_config
from ventas.odoo_ventas import METODOS_PERMITIDOS, OdooSoloLectura

CLAVE = "sk-ant-prueba-secreta-123"
NOMBRE_PERSONA = "Juan Pérez Prueba"
NIT_PERSONA = "1020304050"


# ── datos sintéticos ──

@pytest.fixture
def escenario(ventas):
    filas = [ventas.fila("FE", i, 1000 * i) for i in range(1, 5)]
    filas.append(ventas.fila("FE", 5, 5000))                                       # falta en Odoo
    filas.append(ventas.fila("FE", 6, 600, nit_receptor=NIT_PERSONA, nombre_receptor=NOMBRE_PERSONA))
    filas.append(ventas.fila("FE", 7, 700))                                        # también falta en Odoo
    movs = [ventas.movimiento(i, f"FE/2026/{i:05d}", 1000 * i, cufe_valor=filas[i - 1]["CUFE/CUDE"]) for i in range(1, 5)]
    movs[2]["amount_total"] = 3500.0                                                # diferencia de valor
    movs.append(ventas.movimiento(6, "FE/2026/00006", 600, cufe_valor=filas[5]["CUFE/CUDE"], partner=[70, NOMBRE_PERSONA],
                                  ref=f"Pedido de {NOMBRE_PERSONA}"))              # texto libre con datos personales
    movs.append(ventas.movimiento(9, "FE/2026/00009", 50, compania=2))              # otra compañía
    movs.append(ventas.movimiento(20, "NOM/2026/00001", 1000, tipo="entry",          # asiento de nómina: no es venta
                                  ref=f"Nómina octubre - {NOMBRE_PERSONA} CC {NIT_PERSONA}"))
    ordenes = [ventas.orden_pos(40 + i, f"FV{i}", 100) for i in (7, 8)]             # faltan en DIAN
    from conftest import CAMPOS_MOVE_PRUEBA
    odoo = ventas.OdooFalso(
        movimientos=movs, ordenes=ordenes, campos_move=CAMPOS_MOVE_PRUEBA + ["ref"],
        companias=[{"id": 1, "name": "EMPRESA DE PRUEBA SAS", "vat": ventas.NIT_EMPRESA_DV}, {"id": 2, "name": "Otra", "vat": "800000002"}],
        partners=[{"id": 70, "name": NOMBRE_PERSONA, "vat": NIT_PERSONA, "is_company": False}])
    resultado = ventas.conciliar(filas, odoo)
    return NS(resultado=resultado, odoo=odoo)


def config(**cambios):
    base = dataclasses.replace(cargar_config(), anthropic_api_key=CLAVE, anthropic_model="claude-haiku-5-5",
                               ia_max_herramientas=12, ia_timeout_total_segundos=120, ia_timeout_llamada_segundos=60)
    return dataclasses.replace(base, **cambios)


def _uso():
    return NS(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=80)


def texto(t, stop="end_turn"):
    return NS(content=[NS(type="text", text=t)], stop_reason=stop, usage=_uso())


def herramientas(*usos):
    return NS(content=[NS(type="tool_use", id=f"t{i}", name=n, input=x) for i, (n, x) in enumerate(usos)],
              stop_reason="tool_use", usage=_uso())


def razonamiento(firma="firma-1"):
    """Bloque de razonamiento adaptativo (display «omitted»): texto vacío y firma."""
    return NS(type="thinking", thinking="", signature=firma)


class ClienteFalso:
    """Devuelve respuestas guionadas; un elemento Exception se lanza en esa llamada."""

    def __init__(self, guion, reloj=None, avance=0.0):
        self.guion = list(guion)
        self.llamadas = []
        self.reloj = reloj
        self.avance = avance

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.llamadas.append(copy.deepcopy(kwargs))
        if self.reloj is not None:
            self.reloj.t += self.avance
        paso = self.guion.pop(0) if len(self.guion) > 1 else self.guion[0]
        if callable(paso) and not isinstance(paso, NS):
            paso = paso(kwargs)
        if isinstance(paso, BaseException):
            raise paso
        return paso


class Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def final_valido(**cambios):
    datos = {
        "resumen_general": "La diferencia neta se explica por FE-5 sin registro en Odoo, la diferencia de valor de FE-3 y dos órdenes POS sin reporte DIAN.",
        "hallazgos": [{
            "finding_type": "missing_in_odoo",
            "summary": "FE-5 está aprobada en la DIAN y no existe en Odoo.",
            "refs": ["dian:FE-5"],
            "facts": [{"texto": "FE-5 por $5.000,00 no coincide por CUFE ni por número.", "evidencia": ["dian:FE-5"]}],
            "probable_causes": [{"descripcion": "Factura emitida desde otro sistema.", "confianza": "low",
                                 "como_confirmar": "Revisar el proveedor tecnológico.", "evidencia": []}],
            "cause_status": "confirmed", "confidence": "high", "risk_level": "high",
            "recommended_actions": [{"accion": "Registrar la factura en Odoo con su CUFE.", "registro_afectado": "dian:FE-5",
                                     "evidencia": ["dian:FE-5"], "verificacion": "Al reconciliar, FE-5 queda como Coincide."}],
            "verification_steps": ["Volver a conciliar el mismo período"],
            "requires_human_review": False,
        }],
        "datos_faltantes": [],
        "siguiente_conciliacion": "Mismo período después de registrar FE-5.",
    }
    datos.update(cambios)
    return json.dumps(datos, ensure_ascii=False)


def _foto(resultado):
    return json.dumps(resultado.a_dict(), sort_keys=True, ensure_ascii=False)


# ──────────────────────────────────────────────
# FLUJO NORMAL
# ──────────────────────────────────────────────

def test_hallazgos_estructurados_validos(escenario):
    cliente = ClienteFalso([
        herramientas(("investigar_diferencia", {"ref": "dian:FE-5"}), ("buscar_documento", {"ref": "dian:FE-5"})),
        texto("Terminé."),
        texto(final_valido()),
    ])
    antes = _foto(escenario.resultado)
    h = investigar(escenario.resultado, config=config(), cliente=cliente, lector=OdooSoloLectura(escenario.odoo))

    assert h["disponible"] is True, h
    hz = h["resultado"]["hallazgos"][0]
    assert hz["cause_status"] == "confirmed"                       # hubo búsqueda alternativa completa en vivo
    assert hz["advertencias"] == []
    assert hz["financial_impact"]["documentos_citados"] == {"monto": "5000.00", "documentos": 1}
    assert hz["financial_impact"]["componente"]["monto"] == "5700.00"      # FE-5 + FE-7
    assert [t["herramienta"] for t in h["traza"]] == ["investigar_diferencia", "buscar_documento"]
    assert h["uso"]["llamadas_modelo"] == 3 and h["uso"]["herramientas"] == 2
    assert _foto(escenario.resultado) == antes                     # la IA no toca el resultado determinístico

    bucle, final = cliente.llamadas[0], cliente.llamadas[-1]
    assert bucle["tool_choice"] == {"type": "auto"} and "output_config" not in bucle
    assert final["tool_choice"] == {"type": "none"}
    assert final["output_config"] == {"format": {"type": "json_schema", "schema": ESQUEMA_SALIDA}}
    assert final["tools"] == DEFINICIONES and final["model"] == "claude-haiku-5-5"
    assert final["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert final["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert final["messages"][-1]["content"][-1]["text"] == INSTRUCCION_FINAL


def test_resultados_de_herramientas_se_devuelven_a_claude(escenario):
    cliente = ClienteFalso([herramientas(("listar_casos", {"componente": "missing_in_odoo"})), texto("Listo."), texto(final_valido())])
    investigar(escenario.resultado, config=config(), cliente=cliente)
    segunda = cliente.llamadas[1]["messages"]
    resultado_herramienta = segunda[-1]["content"][0]
    assert resultado_herramienta["type"] == "tool_result" and resultado_herramienta["tool_use_id"] == "t0"
    assert json.loads(resultado_herramienta["content"])["total_casos"] == 2


# ──────────────────────────────────────────────
# RESPUESTAS INVÁLIDAS Y FALLAS DEL PROVEEDOR
# ──────────────────────────────────────────────

def test_respuesta_que_no_es_json(escenario):
    h = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([texto("Listo."), texto("esto no es JSON")]))
    assert h["disponible"] is False and h["codigo"] == "respuesta_invalida"
    assert h["mensaje"].startswith("Análisis IA no disponible")


def test_respuesta_incompleta_segun_esquema(escenario):
    incompleta = json.loads(final_valido())
    del incompleta["hallazgos"]
    h = investigar(escenario.resultado, config=config(),
                   cliente=ClienteFalso([texto("Listo."), texto(json.dumps(incompleta))]))
    assert h["codigo"] == "respuesta_invalida"
    assert any("hallazgos" in d for d in h["detalle"])


def test_respuesta_cortada_por_max_tokens(escenario):
    h = investigar(escenario.resultado, config=config(),
                   cliente=ClienteFalso([texto("Listo."), texto('{"resumen_general": "corta', stop="max_tokens")]))
    assert h["codigo"] == "respuesta_incompleta"


_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize("error, codigo", [
    (anthropic.APITimeoutError(request=_REQ), "timeout"),
    (anthropic.APIConnectionError(request=_REQ), "conexion"),
    (anthropic.RateLimitError("rate limit", response=httpx2.Response(429, request=_REQ), body=None), "cuota"),
    (anthropic.BadRequestError("Your credit balance is too low", response=httpx2.Response(400, request=_REQ), body=None), "cuota"),
    (anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=_REQ), body=None), "autenticacion"),
    (anthropic.PermissionDeniedError("forbidden", response=httpx2.Response(403, request=_REQ), body=None), "autenticacion"),
    (anthropic.NotFoundError("model not found", response=httpx2.Response(404, request=_REQ), body=None), "modelo"),
    (anthropic.InternalServerError("boom", response=httpx2.Response(500, request=_REQ), body=None), "no_disponible"),
    (anthropic.OverloadedError("overloaded", response=httpx2.Response(529, request=_REQ), body=None), "no_disponible"),
    (RuntimeError("fallo inesperado"), "error"),
])
def test_fallas_del_proveedor_no_afectan_la_conciliacion(escenario, error, codigo):
    antes = _foto(escenario.resultado)
    h = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([error]))
    assert h["disponible"] is False and h["codigo"] == codigo
    assert h["mensaje"].startswith("Análisis IA no disponible")
    assert _foto(escenario.resultado) == antes
    from ventas.reporte import generar_reporte
    escenario.resultado.hallazgos_ia = h
    assert generar_reporte(escenario.resultado)[:2] == b"PK"        # el reporte se sigue generando


def test_falla_a_mitad_de_la_investigacion(escenario):
    cliente = ClienteFalso([herramientas(("obtener_resumen", {})), anthropic.APITimeoutError(request=_REQ)])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)
    assert h["codigo"] == "timeout" and h["uso"]["herramientas"] == 1 and len(h["traza"]) == 1


def test_sin_clave_no_se_llama_a_anthropic(escenario):
    h = investigar(escenario.resultado, config=config(anthropic_api_key=""))
    assert h["disponible"] is False and h["codigo"] == "no_configurada"
    assert h["uso"]["llamadas_modelo"] == 0


def test_la_clave_no_aparece_en_respuestas_ni_logs(escenario, caplog):
    caplog.set_level(logging.DEBUG)
    error = anthropic.AuthenticationError(f"invalid x-api-key {CLAVE}", response=httpx2.Response(401, request=_REQ), body=None)
    h = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([error]))
    assert CLAVE not in json.dumps(h, ensure_ascii=False)
    assert CLAVE not in caplog.text
    ok = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([texto("Listo."), texto(final_valido())]))
    assert CLAVE not in json.dumps(ok, ensure_ascii=False) and CLAVE not in caplog.text


# ──────────────────────────────────────────────
# ANTI-INVENCIÓN Y CAUSAS
# ──────────────────────────────────────────────

def test_no_inventa_documentos_ni_importes(escenario):
    datos = json.loads(final_valido())
    datos["resumen_general"] = "La diferencia total es de $9.999.999,99."
    datos["hallazgos"][0]["facts"].append({"texto": "FE-5 suma $123.456,00 en impuestos.", "evidencia": ["dian:FE-5"]})
    datos["hallazgos"][0]["recommended_actions"].append(
        {"accion": "Anular", "registro_afectado": "odoo:account.move:424242", "evidencia": [], "verificacion": "x"})
    datos["hallazgos"].append({**datos["hallazgos"][0], "summary": "Documento inventado", "refs": ["dian:FE-999"],
                               "facts": [{"texto": "FE-999 falta", "evidencia": ["dian:FE-999"]}]})
    cliente = ClienteFalso([herramientas(("buscar_documento", {"ref": "dian:FE-5"})), texto("Listo."), texto(json.dumps(datos))])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)

    r = h["resultado"]
    assert len(r["hallazgos"]) == 1                                   # el inventado se descarta completo
    assert h["validacion"]["descartados"][0]["resumen"] == "Documento inventado"
    assert "dian:FE-999" in h["validacion"]["evidencias_desconocidas"]
    hz = r["hallazgos"][0]
    assert all(a["registro_afectado"] != "odoo:account.move:424242" for a in hz["recommended_actions"])
    assert any("123456.00" in a for a in hz["advertencias"])          # importe no devuelto por el backend
    assert hz["requires_human_review"] is True
    assert any("9999999.99" in a for a in h["validacion"]["advertencias"])
    assert hz["financial_impact"]["documentos_citados"]["monto"] == "5000.00"   # lo calcula el backend


def test_sin_evidencia_devuelta_no_hay_hechos(escenario):
    """Sin consultar el documento, Claude no puede citarlo: el hallazgo se descarta."""
    h = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([texto("Listo."), texto(final_valido())]))
    assert h["disponible"] is True and h["resultado"]["hallazgos"] == []
    assert h["validacion"]["descartados"][0]["motivo"].startswith("Ningún hecho")


def test_sin_odoo_en_vivo_la_busqueda_no_es_concluyente(escenario):
    cliente = ClienteFalso([herramientas(("buscar_documento", {"ref": "dian:FE-5"})), texto("Listo."), texto(final_valido())])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)          # sin lector
    hz = h["resultado"]["hallazgos"][0]
    assert hz["cause_status"] == "probable" and hz["requires_human_review"] is True
    resultado_busqueda = json.loads(cliente.llamadas[1]["messages"][-1]["content"][0]["content"])
    assert resultado_busqueda["busqueda_en_vivo_completa"] is False


def test_hay_que_buscar_todos_los_faltantes_citados(escenario):
    datos = json.loads(final_valido())
    datos["hallazgos"][0]["refs"] = ["dian:FE-5", "dian:FE-7"]
    datos["hallazgos"][0]["facts"].append({"texto": "FE-7 tampoco está en Odoo.", "evidencia": ["dian:FE-7"]})
    cliente = ClienteFalso([herramientas(("buscar_documento", {"ref": "dian:FE-5"}), ("investigar_diferencia", {"ref": "dian:FE-7"})),
                            texto("Listo."), texto(json.dumps(datos))])
    h = investigar(escenario.resultado, config=config(), cliente=cliente, lector=OdooSoloLectura(escenario.odoo))
    hz = h["resultado"]["hallazgos"][0]
    assert hz["cause_status"] == "probable"
    assert any("dian:FE-7" in a for a in hz["advertencias"])


def test_faltantes_sin_documentos_citados_no_se_confirman(escenario):
    datos = json.loads(final_valido())
    datos["hallazgos"][0]["refs"] = []
    datos["hallazgos"][0]["facts"] = [{"texto": "Hay facturas DIAN sin registro en Odoo.", "evidencia": ["componente:missing_in_odoo"]}]
    datos["hallazgos"][0]["recommended_actions"] = []
    h = investigar(escenario.resultado, config=config(), cliente=ClienteFalso([texto("Listo."), texto(json.dumps(datos))]))
    hz = h["resultado"]["hallazgos"][0]
    assert hz["cause_status"] == "probable" and any("uno por uno" in a for a in hz["advertencias"])


def test_busqueda_fallida_no_cuenta_como_busqueda(escenario):
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(escenario.odoo))
    _, es_error = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5", "numero": "###"})
    assert es_error and "dian:FE-5" not in inv.busquedas_alternativas
    _, es_error = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    assert not es_error and "dian:FE-5" in inv.busquedas_alternativas


def test_causa_confirmada_sin_busqueda_alternativa_baja_a_probable(escenario):
    cliente = ClienteFalso([herramientas(("investigar_diferencia", {"ref": "dian:FE-5"})), texto("Listo."), texto(final_valido())])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)
    hz = h["resultado"]["hallazgos"][0]
    assert hz["cause_status"] == "probable"
    assert any("búsquedas alternativas" in a for a in hz["advertencias"])
    assert any("Antes de registrar dian:FE-5" in a for a in hz["advertencias"])
    assert hz["requires_human_review"] is True
    assert hz["probable_causes"][0]["descripcion"] == "Factura emitida desde otro sistema."   # hipótesis separada del hecho


# ──────────────────────────────────────────────
# LÍMITES
# ──────────────────────────────────────────────

def test_limite_de_llamadas_a_herramientas(escenario):
    def responder(kwargs):
        if kwargs["tool_choice"] == {"type": "none"}:
            return texto(final_valido())
        return herramientas(("obtener_resumen", {}), ("generar_plan_revision", {}))

    cliente = ClienteFalso([responder])
    h = investigar(escenario.resultado, config=config(ia_max_herramientas=3), cliente=cliente)
    assert h["disponible"] is True
    assert h["uso"]["herramientas"] == 3 and h["uso"]["limite_alcanzado"] is True
    assert len(cliente.llamadas) == 3                                 # 2 del bucle + la final
    ultimo = cliente.llamadas[-1]["messages"][-1]["content"]
    errores = [b for b in ultimo if b.get("type") == "tool_result" and b.get("is_error")]
    assert errores and "límite" in errores[0]["content"]


def test_limite_de_tiempo_total(escenario):
    reloj = Reloj()
    cliente = ClienteFalso([herramientas(("obtener_resumen", {})), herramientas(("obtener_resumen", {})), texto(final_valido())],
                           reloj=reloj, avance=50)
    h = investigar(escenario.resultado, config=config(ia_timeout_total_segundos=120), cliente=cliente, reloj=reloj)
    assert h["disponible"] is True
    assert len(cliente.llamadas) == 3                                 # el bucle se corta a los 72 s (60 %)
    assert cliente.llamadas[-1]["timeout"] == pytest.approx(20)       # la final usa lo que queda


def test_tiempo_agotado_antes_de_la_respuesta_final(escenario):
    reloj = Reloj()
    cliente = ClienteFalso([herramientas(("obtener_resumen", {}))], reloj=reloj, avance=130)
    h = investigar(escenario.resultado, config=config(ia_timeout_total_segundos=120), cliente=cliente, reloj=reloj)
    assert h["disponible"] is False and h["codigo"] == "timeout"


# ──────────────────────────────────────────────
# HERRAMIENTAS: VALIDACIÓN, SOLO LECTURA Y DATOS PERSONALES
# ──────────────────────────────────────────────

def test_parametros_invalidos_y_herramientas_desconocidas(escenario):
    inv = Investigacion(escenario.resultado, max_llamadas=10)
    for nombre, parametros in [("borrar_factura", {}), ("listar_casos", {"estado": "nope"}),
                               ("listar_casos", {"sql": "select *"}), ("investigar_diferencia", {"ref": "dian:NO-1"}),
                               ("consultar_documento_odoo", {"ref": "odoo:res.users:1"}), ("listar_casos", {"limite": "x"})]:
        texto_salida, es_error = inv.ejecutar(nombre, parametros)
        assert es_error, (nombre, parametros, texto_salida)
        assert "error" in json.loads(texto_salida)
    assert inv.llamadas == 6                                           # los intentos fallidos también cuentan


def test_consultas_en_vivo_solo_lectura_y_de_la_compania(escenario):
    lector = OdooSoloLectura(escenario.odoo)
    escenario.odoo.llamadas.clear()
    inv = Investigacion(escenario.resultado, lector=lector)
    propio, err = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:6"})
    assert not err and json.loads(propio)["registro"]["receptor"]["nit"] == "***050"
    ajeno, err = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:9"})
    assert err and "compañía conciliada" in json.loads(ajeno)["error"]
    busqueda, err = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    assert not err and json.loads(busqueda)["odoo_en_vivo"]["disponible"] is True
    assert {m for _, m in escenario.odoo.llamadas} <= METODOS_PERMITIDOS
    assert all(m != "read" or mod == "res.partner" for mod, m in escenario.odoo.llamadas)   # ventas: search_read con dominio
    assert "dian:FE-5" in inv.busquedas_alternativas


def test_odoo_caido_durante_la_investigacion(escenario):
    def caido(*_):
        raise ConnectionRefusedError("sin conexión")
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(caido))
    salida, err = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:6"})
    assert err and "La consulta falló" in json.loads(salida)["error"]
    salida, err = inv.ejecutar("buscar_documento", {"numero": "FE-5"})
    assert not err and json.loads(salida)["odoo_en_vivo"]["errores"]


def test_minimizacion_de_datos_personales(escenario):
    cliente = ClienteFalso([
        herramientas(("investigar_diferencia", {"ref": "dian:FE-6"}), ("buscar_documento", {"mismo_receptor_que": "dian:FE-6"}),
                     ("listar_casos", {"estado": "matched", "limite": 25}), ("analizar_impuestos", {"ref": "dian:FE-6"})),
        herramientas(("consultar_documento_odoo", {"ref": "odoo:account.move:6"}), ("generar_plan_revision", {})),
        texto("Listo."), texto(final_valido())])
    investigar(escenario.resultado, config=config(), cliente=cliente, lector=OdooSoloLectura(escenario.odoo))
    enviado = json.dumps([c["messages"] for c in cliente.llamadas], ensure_ascii=False, default=str)
    assert NOMBRE_PERSONA not in enviado
    assert NIT_PERSONA not in enviado
    assert "***050" in enviado                                          # solo los 3 últimos dígitos


def test_foco_en_un_documento_y_foco_invalido(escenario):
    cliente = ClienteFalso([texto("Listo."), texto(final_valido())])
    h = investigar(escenario.resultado, foco={"tipo": "documento", "valor": "dian:FE-3"}, config=config(), cliente=cliente)
    assert h["foco"] == {"tipo": "documento", "valor": "dian:FE-3"}
    assert "Caso en foco" in cliente.llamadas[0]["messages"][0]["content"][0]["text"]
    malo = investigar(escenario.resultado, foco={"tipo": "documento", "valor": "dian:NO-1"}, config=config(), cliente=cliente)
    assert malo["codigo"] == "foco_invalido"


# ──────────────────────────────────────────────
# ESQUEMA
# ──────────────────────────────────────────────

def test_validador_de_esquema():
    assert validar_estructura(json.loads(final_valido())) == []
    malo = json.loads(final_valido())
    malo["hallazgos"][0]["cause_status"] = "seguro"
    malo["hallazgos"][0]["extra"] = 1
    errores = validar_estructura(malo)
    assert any("cause_status" in e for e in errores) and any("extra" in e for e in errores)


def test_esquema_compatible_con_salida_estructurada():
    """Todos los objetos cierran additionalProperties y declaran todos sus campos como requeridos."""
    def revisar(esquema):
        if esquema.get("type") == "object":
            assert esquema["additionalProperties"] is False
            assert set(esquema["required"]) == set(esquema["properties"])
            for sub in esquema["properties"].values():
                revisar(sub)
        if esquema.get("type") == "array":
            revisar(esquema["items"])
        for prohibido in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
            assert prohibido not in esquema
    revisar(ESQUEMA_SALIDA)


@pytest.mark.parametrize("texto_entrada, esperado", [
    ("suman $224.000,00 en total", [Decimal("224000.00")]),
    ("diferencia de 200,50 y de 1.234.567,89", [Decimal("200.50"), Decimal("1234567.89")]),
    ("folios 6729–6745 del 2026-10-13, FE-310", []),
    ("$ 5000", [Decimal("5000")]),
])
def test_importes_en_texto(texto_entrada, esperado):
    assert importes_en_texto(texto_entrada) == esperado



# ──────────────────────────────────────────────
# REGRESIONES DE LA REVISIÓN INDEPENDIENTE
# ──────────────────────────────────────────────

def test_bloques_de_razonamiento_vuelven_sin_modificar(escenario):
    pensamiento = razonamiento("firma-abc")
    primera = NS(content=[pensamiento, NS(type="tool_use", id="t0", name="obtener_resumen", input={})],
                 stop_reason="tool_use", usage=_uso())
    segunda = NS(content=[razonamiento("firma-def"), NS(type="text", text="Terminé.")], stop_reason="end_turn", usage=_uso())
    cliente = ClienteFalso([primera, segunda, texto(final_valido())])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)
    assert h["disponible"] is True
    turno = cliente.llamadas[1]["messages"][1]
    assert turno["role"] == "assistant"
    assert [getattr(b, "type", None) for b in turno["content"]] == ["thinking", "tool_use"]
    assert turno["content"][0].signature == "firma-abc"
    final = cliente.llamadas[2]["messages"]
    assert [getattr(b, "type", None) for b in final[3]["content"]] == ["thinking", "text"]


def test_respuesta_final_con_razonamiento_y_json(escenario):
    final = NS(content=[razonamiento(), NS(type="text", text=final_valido())], stop_reason="end_turn", usage=_uso())
    h = investigar(escenario.resultado, config=config(),
                   cliente=ClienteFalso([herramientas(("investigar_diferencia", {"ref": "dian:FE-5"})), texto("Listo."), final]))
    assert h["disponible"] is True and len(h["resultado"]["hallazgos"]) == 1


def test_busqueda_en_vivo_encuentra_el_numero_aunque_haya_muchos_parecidos(ventas, escenario):
    from conftest import CAMPOS_MOVE_PRUEBA
    parecidos = [ventas.movimiento(1000 + i, f"FE/2026/{n:05d}", 10) for i, n in enumerate(list(range(10, 60)) + [15, 105, 1005, 5005, 50005])]
    viejo = ventas.movimiento(300, "FE/2026/00005", 5000, fecha="2026-03-02")      # existe, en marzo
    odoo = ventas.OdooFalso(movimientos=[viejo] + parecidos, campos_move=CAMPOS_MOVE_PRUEBA)
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(odoo))
    salida, es_error = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    vivo = json.loads(salida)["odoo_en_vivo"]
    assert not es_error and [r["ref"] for r in vivo["resultados"]] == ["odoo:account.move:300"]
    assert vivo["resultados"][0]["invoice_date"] == "2026-03-02"
    assert "dian:FE-5" in inv.busquedas_alternativas


def test_busqueda_en_vivo_recortada_no_es_concluyente(ventas, escenario):
    repetidos = [ventas.movimiento(500 + i, "FE/2026/00005", 10) for i in range(55)]
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(ventas.OdooFalso(movimientos=repetidos)))
    salida, _ = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    datos = json.loads(salida)
    assert datos["odoo_en_vivo"]["truncado"] is True and datos["busqueda_en_vivo_completa"] is False
    assert "dian:FE-5" not in inv.busquedas_alternativas


def test_no_lee_asientos_que_no_son_ventas_ni_texto_libre(escenario):
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(escenario.odoo))
    nomina, es_error = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:20"})
    assert es_error and NOMBRE_PERSONA not in nomina
    venta, es_error = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:6"})
    assert not es_error
    assert NOMBRE_PERSONA not in venta and "Pedido de" not in venta and NIT_PERSONA not in venta


def test_corte_del_bucle_por_tiempo_pasa_a_la_respuesta_final(escenario):
    reloj = Reloj()
    cliente = ClienteFalso([herramientas(("obtener_resumen", {})), texto(final_valido())], reloj=reloj, avance=68)
    h = investigar(escenario.resultado, config=config(ia_timeout_total_segundos=120), cliente=cliente, reloj=reloj)
    assert h["disponible"] is True and len(cliente.llamadas) == 2
    assert cliente.llamadas[-1]["timeout"] == pytest.approx(52)        # 120 − 68: no se pierde la investigación


def test_timeout_de_una_llamada_del_bucle_no_descarta_la_investigacion(escenario):
    cliente = ClienteFalso([anthropic.APITimeoutError(request=_REQ), texto(final_valido())])
    h = investigar(escenario.resultado, config=config(), cliente=cliente)
    assert h["disponible"] is True and len(cliente.llamadas) == 2
    assert cliente.llamadas[-1]["tool_choice"] == {"type": "none"}


def test_herramientas_respetan_el_plazo(escenario):
    reloj = Reloj()
    inv = Investigacion(escenario.resultado, reloj=reloj, fin=10)
    reloj.t = 11
    salida, es_error = inv.ejecutar("obtener_resumen", {})
    assert es_error and "tiempo" in json.loads(salida)["error"] and inv.llamadas == 0


def test_herramienta_de_secuencias_con_folio_atipico(ventas):
    import time
    filas = [ventas.fila("FE", 1, 10), ventas.fila("FE", 2, 10), ventas.fila("FE", 990000001, 10)]
    r = ventas.conciliar(filas, ventas.OdooFalso(movimientos=[ventas.movimiento(1, "FE/2026/00003", 10)]))
    inv = Investigacion(r)
    inicio = time.monotonic()
    salida, es_error = inv.ejecutar("analizar_secuencias", {"prefijo": "FE"})
    assert not es_error and time.monotonic() - inicio < 1
    bloque = json.loads(salida)["bloques"][0]
    assert bloque["en_odoo"]["encontrados"] == 1 and bloque["no_encontrados_en_odoo"] == 989999997


def test_la_configuracion_no_muestra_la_clave():
    assert CLAVE not in repr(config())


def test_herramientas_prefieren_la_fila_incluida(ventas):
    filas = [ventas.fila("FE", 5, 5000, estado="Rechazado", cufe_valor=ventas.cufe("rechazada")), ventas.fila("FE", 5, 5000)]
    r = ventas.conciliar(filas, ventas.OdooFalso())
    salida, es_error = Investigacion(r).ejecutar("investigar_diferencia", {"ref": "dian:FE-5"})
    assert not es_error and json.loads(salida)["estado"] == "missing_in_odoo"


def test_folio_largo_tambien_se_busca(ventas):
    filas = [ventas.fila("FE", 990000005, 10)]
    r = ventas.conciliar(filas, ventas.OdooFalso())
    odoo = ventas.OdooFalso(movimientos=[ventas.movimiento(77, "FE/2026/990000005", 10, fecha="2026-01-10")])
    inv = Investigacion(r, lector=OdooSoloLectura(odoo))
    salida, _ = inv.ejecutar("buscar_documento", {"ref": "dian:FE-990000005"})
    assert [x["ref"] for x in json.loads(salida)["odoo_en_vivo"]["resultados"]] == ["odoo:account.move:77"]


def test_modelo_sin_campos_de_busqueda_no_es_busqueda_completa(ventas, escenario):
    odoo = ventas.OdooFalso(campos_pos=["name", "date_order", "state", "company_id", "amount_total"])
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(odoo))
    salida, _ = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    datos = json.loads(salida)
    assert datos["busqueda_en_vivo_completa"] is False and "pos.order" in datos["odoo_en_vivo"]["errores"][0]


def test_sin_modulo_pos_la_busqueda_puede_ser_completa(ventas, escenario):
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(ventas.OdooFalso(pos_instalado=False)))
    salida, _ = inv.ejecutar("buscar_documento", {"ref": "dian:FE-5"})
    datos = json.loads(salida)
    assert datos["busqueda_en_vivo_completa"] is True
    assert datos["odoo_en_vivo"]["modelos_no_instalados"] == ["pos.order"]


def test_consultas_a_odoo_se_detienen_al_vencer_el_plazo(ventas, escenario):
    reloj = Reloj()

    class Lento(ventas.OdooFalso):
        def __call__(self, modelo, metodo, args, kwargs):
            reloj.t += 15                      # cada consulta tarda 15 s
            return super().__call__(modelo, metodo, args, kwargs)

    odoo = Lento(movimientos=[ventas.movimiento(1, "FE/2026/00001", 600),
                              ventas.movimiento(6, "NC/2026/00006", 600, tipo="out_refund", reversed_entry_id=[1, "FE/2026/00001"])])
    inv = Investigacion(escenario.resultado, lector=OdooSoloLectura(odoo), reloj=reloj, fin=20)
    salida, es_error = inv.ejecutar("consultar_documento_odoo", {"ref": "odoo:account.move:6"})
    assert es_error and "tiempo" in json.loads(salida)["error"]
    assert len(odoo.llamadas) == 2 and reloj.t == 30                 # la 3.ª consulta (factura original) no se hace
