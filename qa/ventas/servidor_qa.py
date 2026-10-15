"""
Servidor local de QA para FactuMatch Ventas.

Monta el router REAL de /api/ventas con:
  - Odoo simulado (OdooFalso con los datos sintéticos de datos_sinteticos.py),
  - Claude simulado (cliente guionado que usa herramientas reales y devuelve
    hallazgos con evidencia de los datos sintéticos).
No usa red ni datos reales. Solo para QA local; no se despliega.

    python qa/ventas/servidor_qa.py            # http://127.0.0.1:8765

Modos del agente (para probar la interfaz): POST /qa/modo-ia/{sin_clave|ok|falla}
Credenciales de Odoo aceptadas por el simulador: «token-qa».
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace as NS

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(os.path.dirname(AQUI))
sys.path.insert(0, os.path.join(RAIZ, "backend"))
sys.path.insert(0, AQUI)

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

import ventas.router as rutas  # noqa: E402
from datos_sinteticos import construir  # noqa: E402
from ventas.agente import proveedor  # noqa: E402
from ventas.odoo_ventas import OdooSoloLectura  # noqa: E402

DATOS = construir()
TOKEN_QA = "token-qa"
POS_SIN_DIAN = sorted(r for r, e in DATOS.esperado.items() if r.startswith("odoo:pos.order:") and e == "missing_in_dian")


def _lector(credenciales: str, segundos: float = 120.0) -> OdooSoloLectura:
    if credenciales != TOKEN_QA:
        raise HTTPException(status_code=400, detail="Credenciales de Odoo inválidas o vencidas: vuelve a conectar Odoo")
    return OdooSoloLectura(DATOS.odoo)


rutas._lector = _lector


def _uso():
    return NS(input_tokens=6200, output_tokens=380, cache_creation_input_tokens=0, cache_read_input_tokens=5100)


def _herramientas(*usos):
    return NS(content=[NS(type="thinking", thinking="", signature="qa")] +
              [NS(type="tool_use", id=f"q{i}", name=n, input=x) for i, (n, x) in enumerate(usos)],
              stop_reason="tool_use", usage=_uso())


FINAL = {
    "resumen_general": ("Odoo registra más ventas que la DIAN: ocho órdenes POS del punto Centro no se transmitieron, "
                        "FE-15 está aprobada en la DIAN y no existe en Odoo, y NC-3 quedó registrada como factura."),
    "hallazgos": [
        {"finding_type": "missing_in_dian", "summary": "Ocho órdenes POS (POS-240 a POS-247) no llegaron a la DIAN.",
         "refs": POS_SIN_DIAN[:3],
         "facts": [{"texto": "El bloque POS 240–247 falta en el contenedor y esas órdenes existen en Odoo sin CUFE, con estado electrónico error.",
                    "evidencia": ["secuencia:POS"] + POS_SIN_DIAN[:2]}],
         "probable_causes": [{"descripcion": "Falla de transmisión del punto de venta Centro.", "confianza": "medium",
                              "como_confirmar": "Revisar el registro de envío electrónico de esas órdenes en Odoo.", "evidencia": ["secuencia:POS"]}],
         "cause_status": "probable", "confidence": "medium", "risk_level": "high",
         "recommended_actions": [{"accion": "Reintentar el envío electrónico de las órdenes en error.", "registro_afectado": "secuencia:POS",
                                  "evidencia": ["secuencia:POS"], "verificacion": "Exportar un contenedor nuevo y volver a conciliar el período: el bloque debe desaparecer."}],
         "verification_steps": ["Volver a conciliar 01-10-2026 a 15-10-2026 con un contenedor actualizado"],
         "requires_human_review": True},
        {"finding_type": "missing_in_odoo", "summary": "FE-15 está aprobada en la DIAN y no existe en Odoo.",
         "refs": ["dian:FE-15"],
         "facts": [{"texto": "FE-15 no coincide por CUFE ni por número y la búsqueda en Odoo en vivo no encontró registros.",
                    "evidencia": ["dian:FE-15"]}],
         "probable_causes": [{"descripcion": "Factura emitida desde otro sistema de facturación.", "confianza": "low",
                              "como_confirmar": "Consultar al proveedor tecnológico por el CUFE.", "evidencia": []}],
         "cause_status": "confirmed", "confidence": "high", "risk_level": "high",
         "recommended_actions": [{"accion": "Registrar la factura en Odoo con su CUFE.", "registro_afectado": "dian:FE-15",
                                  "evidencia": ["dian:FE-15"], "verificacion": "Al volver a conciliar, FE-15 debe quedar como Coincide."}],
         "verification_steps": [], "requires_human_review": False},
        {"finding_type": "credit_note_treatment", "summary": "NC-3 quedó registrada como factura en Odoo.",
         "refs": ["dian:NC-3"],
         "facts": [{"texto": "El CUFE de NC-3 corresponde a un asiento de tipo factura en Odoo, por eso suma en vez de restar.",
                    "evidencia": ["dian:NC-3"]}],
         "probable_causes": [], "cause_status": "confirmed", "confidence": "high", "risk_level": "medium",
         "recommended_actions": [{"accion": "Revisar el tipo del asiento NC/2026/00003 con contabilidad.", "registro_afectado": "dian:NC-3",
                                  "evidencia": ["dian:NC-3"], "verificacion": "NC-3 debe quedar como Coincide al reconciliar."}],
         "verification_steps": [], "requires_human_review": True},
    ],
    "datos_faltantes": ["Registro de envío electrónico del punto de venta Centro"],
    "siguiente_conciliacion": "Mismo período (01-10-2026 a 15-10-2026) con un contenedor exportado después de corregir.",
}


class ClienteQA:
    def __init__(self):
        self.n = 0

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        if kwargs.get("tool_choice") == {"type": "none"}:
            return NS(content=[NS(type="thinking", thinking="", signature="qa"),
                               NS(type="text", text=json.dumps(FINAL, ensure_ascii=False))], stop_reason="end_turn", usage=_uso())
        self.n += 1
        if self.n == 1:
            return _herramientas(("generar_plan_revision", {}), ("analizar_secuencias", {"prefijo": "POS"}))
        if self.n == 2:
            return _herramientas(("buscar_documento", {"ref": "dian:FE-15"}), ("investigar_diferencia", {"ref": "dian:NC-3"}))
        return NS(content=[NS(type="text", text="Terminé de investigar.")], stop_reason="end_turn", usage=_uso())


class ClienteCaido:
    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        import anthropic
        import httpx2
        solicitud = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.RateLimitError("rate limit", response=httpx2.Response(429, request=solicitud), body=None)


MODO = {"ia": "ok"}


def _crear_cliente(config):
    return ClienteCaido() if MODO["ia"] == "falla" else ClienteQA()


proveedor._crear_cliente = _crear_cliente


def _aplicar_modo(modo: str) -> None:
    MODO["ia"] = modo
    if modo == "sin_clave":
        os.environ.pop("ANTHROPIC_API_KEY", None)
    else:
        os.environ["ANTHROPIC_API_KEY"] = "clave-ficticia-de-qa"


_aplicar_modo("ok")

app = FastAPI(title="QA FactuMatch Ventas")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(rutas.router)


@app.post("/qa/modo-ia/{modo}")
def modo_ia(modo: str):
    if modo not in ("sin_clave", "ok", "falla"):
        raise HTTPException(status_code=422, detail="modo inválido")
    _aplicar_modo(modo)
    return {"modo": modo}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("QA_PUERTO_API", "8765")), log_level="warning")
