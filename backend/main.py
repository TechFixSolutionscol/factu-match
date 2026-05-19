from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os
import io
import json
import httpx
from comparador import comparar_facturas, comparar_facturas_odoo, generar_excel_reporte
from odoo_match import OdooConnector, CredentialManager

app = FastAPI(title="Comparador Facturas DIAN vs Siesa")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"


@app.get("/")
def root():
    return {"status": "ok", "mensaje": "Comparador DIAN vs Siesa activo"}


@app.post("/comparar")
async def comparar(
    dian: UploadFile = File(...),
    siesa: UploadFile = File(...),
):
    try:
        dian_bytes = await dian.read()
        siesa_bytes = await siesa.read()

        resultado = comparar_facturas(dian_bytes, siesa_bytes)

        if not resultado["proveedores"]:
            raise HTTPException(status_code=400, detail="No se encontraron datos para comparar.")

        narrativa = await generar_narrativa_groq(resultado)
        resultado["narrativa"] = narrativa

        return resultado

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando archivos: {str(e)}")


@app.post("/descargar-reporte")
async def descargar_reporte(
    dian: UploadFile = File(...),
    siesa: UploadFile = File(...),
):
    try:
        dian_bytes = await dian.read()
        siesa_bytes = await siesa.read()

        resultado = comparar_facturas(dian_bytes, siesa_bytes)
        ruta_excel = generar_excel_reporte(resultado)

        return FileResponse(
            path=ruta_excel,
            filename="reporte_comparacion_facturas.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando reporte: {str(e)}")


# ──────────────────────────────────────────────
# ENDPOINTS ODOO
# ──────────────────────────────────────────────

@app.post("/odoo/test-connection")
async def odoo_test_connection(credentials: str = Form(...)):
    """Verifica credenciales Odoo sin guardarlas."""
    try:
        creds = json.loads(credentials)
        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        result = connector.test_connection()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/odoo/save-connection")
async def odoo_save_connection(
    credentials: str = Form(...),
    user_id: str = Form("default")
):
    """Valida y encripta las credenciales Odoo para almacenamiento seguro."""
    try:
        creds = json.loads(credentials)
        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        test = connector.test_connection()
        if not test["success"]:
            raise HTTPException(status_code=400, detail=test.get("error", "Error de conexión con Odoo"))

        manager = CredentialManager()
        encrypted = manager.encrypt(creds)
        return {
            "success": True,
            "encrypted_credentials": encrypted,
            "odoo_version": test.get("odoo_version", "?")
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error guardando configuración: {str(e)}")


@app.post("/odoo/comparar")
async def odoo_comparar(
    dian: UploadFile = File(...),
    credentials: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...)
):
    """Compara archivo DIAN contra facturas extraídas de Odoo vía API."""
    try:
        dian_bytes = await dian.read()

        manager = CredentialManager()
        creds = manager.decrypt(credentials)

        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        facturas_odoo = connector.fetch_invoices(date_from=date_from, date_to=date_to)
        resultado = comparar_facturas_odoo(dian_bytes, facturas_odoo)

        resultado["resumen_general"]["total_odoo"] = len(facturas_odoo)
        resultado["resumen_general"]["fuente"] = "odoo"

        narrativa = await generar_narrativa_groq(resultado)
        resultado["narrativa"] = narrativa
        return resultado

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error comparando con Odoo: {str(e)}")


@app.post("/odoo/descargar-reporte")
async def odoo_descargar_reporte(
    dian: UploadFile = File(...),
    credentials: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...)
):
    """Genera y descarga reporte Excel de comparación DIAN vs Odoo."""
    try:
        dian_bytes = await dian.read()

        manager = CredentialManager()
        creds = manager.decrypt(credentials)

        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        facturas_odoo = connector.fetch_invoices(date_from=date_from, date_to=date_to)
        resultado = comparar_facturas_odoo(dian_bytes, facturas_odoo)
        ruta_excel = generar_excel_reporte(resultado)

        return FileResponse(
            path=ruta_excel,
            filename="reporte_comparacion_dian_vs_odoo.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando reporte Odoo: {str(e)}")


async def generar_narrativa_groq(resultado: dict) -> str:
    if not GROQ_API_KEY:
        return generar_narrativa_local(resultado)

    resumen = construir_resumen_para_ia(resultado)

    prompt = f"""Eres un asistente contable. Analiza este resumen de comparación de facturas entre la DIAN y el sistema Siesa, y genera un informe claro y profesional en español.

El informe debe:
1. Empezar con un resumen general (total proveedores, total facturas DIAN, total encontradas, total faltantes)
2. Por cada proveedor con facturas faltantes, indicar claramente cuáles son
3. Usar un tono profesional pero directo
4. Si todo está completo, felicitar por la buena gestión

Datos:
{resumen}

Genera el informe ahora:"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.3
                }
            )
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except Exception:
        return generar_narrativa_local(resultado)


def construir_resumen_para_ia(resultado: dict) -> str:
    lineas = []
    r = resultado["resumen_general"]
    lineas.append(f"RESUMEN GENERAL:")
    lineas.append(f"- Total proveedores analizados: {r['total_proveedores']}")
    lineas.append(f"- Total facturas en DIAN: {r['total_dian']}")
    lineas.append(f"- Total encontradas en Siesa: {r['total_en_siesa']}")
    lineas.append(f"- Total faltantes en Siesa: {r['total_faltantes']}")
    lineas.append("")
    lineas.append("DETALLE POR PROVEEDOR:")

    for p in resultado["proveedores"]:
        lineas.append(f"\nProveedor: {p['nombre']} (NIT: {p['nit']})")
        lineas.append(f"  - Facturas en DIAN: {p['total_dian']}")
        lineas.append(f"  - Encontradas en Siesa: {p['total_en_siesa']}")
        lineas.append(f"  - Faltantes: {p['total_faltantes']}")
        if p["faltantes"]:
            lineas.append(f"  - Facturas faltantes: {', '.join(f['factura'] for f in p['faltantes'])}")

    return "\n".join(lineas)


def generar_narrativa_local(resultado: dict) -> str:
    r = resultado["resumen_general"]
    lineas = []
    lineas.append("=== INFORME DE COMPARACIÓN DIAN vs SIESA ===\n")
    lineas.append(f"Se analizaron {r['total_proveedores']} proveedores.")
    lineas.append(f"La DIAN reporta {r['total_dian']} facturas en total.")
    lineas.append(f"En Siesa se encontraron {r['total_en_siesa']} facturas.")

    if r["total_faltantes"] == 0:
        lineas.append("✅ Todas las facturas están registradas en Siesa. ¡Excelente gestión!")
    else:
        lineas.append(f"⚠️ Faltan {r['total_faltantes']} facturas por registrar en Siesa.\n")
        for p in resultado["proveedores"]:
            if p["faltantes"]:
                lineas.append(f"📌 {p['nombre']} (NIT: {p['nit']})")
                lineas.append(f"   DIAN: {p['total_dian']} facturas | Siesa: {p['total_en_siesa']} | Faltan: {p['total_faltantes']}")
                lineas.append(f"   Facturas faltantes: {', '.join(f['factura'] for f in p['faltantes'])}")

    return "\n".join(lineas)
