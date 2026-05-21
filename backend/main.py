from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import os
import io
import json
import httpx
import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel
from typing import List, Optional
from comparador import comparar_facturas, comparar_facturas_odoo, generar_excel_reporte, generar_excel_reporte_bytes
from odoo_match import OdooConnector, CredentialManager, _normalizar_clave_odoo, _normalizar_nit_odoo
from email_parser import process_emails, connect_db
from ai_auditor import build_odoo_context, run_ai_audit
from datetime import datetime, timedelta
import psycopg2.extras
from dotenv import load_dotenv
from cachetools import TTLCache
load_dotenv()
app = FastAPI(title="Comparador Facturas DIAN vs Siesa")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── Validación de ENCRYPTION_KEY (requerida) ──
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY es obligatoria. "
        "Ejecuta: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        " y configúrala como variable de entorno en Render."
    )

# ── Configuración de rendimiento ──
executor = ThreadPoolExecutor(max_workers=2)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# ── Caché para respuestas de Groq (5 min TTL) ──
groq_cache = TTLCache(maxsize=128, ttl=300)

# ── Auth middleware opcional (API_AUTH_TOKEN en entorno) ──
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN", "")

@app.middleware("http")
async def auth_middleware(request, call_next):
    if API_AUTH_TOKEN:
        if request.method != "OPTIONS" and request.url.path != "/":
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") != API_AUTH_TOKEN:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=401, content={"detail": "No autorizado. Token inválido o faltante."})
    return await call_next(request)


@app.get("/")
def root():
    return {"status": "ok", "mensaje": "Comparador DIAN vs Siesa activo"}


@app.post("/comparar")
async def comparar(
    dian: UploadFile = File(...),
    siesa: UploadFile = File(...),
    limit: int = Form(0),
    offset: int = Form(0),
):
    try:
        for name, f in [("DIAN", dian), ("Siesa", siesa)]:
            if f.size and f.size > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail=f"Archivo {name} demasiado grande ({f.size / 1024 / 1024:.1f} MB). Máximo: {MAX_FILE_SIZE / 1024 / 1024:.0f} MB.")

        dian_bytes = await dian.read()
        siesa_bytes = await siesa.read()

        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(executor, comparar_facturas, dian_bytes, siesa_bytes, limit, offset)

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
        for name, f in [("DIAN", dian), ("Siesa", siesa)]:
            if f.size and f.size > MAX_FILE_SIZE:
                raise HTTPException(status_code=413, detail=f"Archivo {name} demasiado grande.")

        dian_bytes = await dian.read()
        siesa_bytes = await siesa.read()

        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(executor, comparar_facturas, dian_bytes, siesa_bytes)
        excel_bytes = await loop.run_in_executor(executor, generar_excel_reporte_bytes, resultado)

        return StreamingResponse(
            iter([excel_bytes]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=reporte_comparacion_facturas.xlsx"}
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
        loop = asyncio.get_event_loop()
        facturas_odoo = await loop.run_in_executor(
            executor, connector.fetch_invoices, date_from, date_to, 10000, executor
        )
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
        loop = asyncio.get_event_loop()
        facturas_odoo = await loop.run_in_executor(
            executor, connector.fetch_invoices, date_from, date_to, 10000, executor
        )
        resultado = comparar_facturas_odoo(dian_bytes, facturas_odoo)
        ruta_excel = generar_excel_reporte(resultado)

        return FileResponse(
            path=ruta_excel,
            filename="reporte_comparacion_dian_vs_odoo.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generando reporte Odoo: {str(e)}")


# ──────────────────────────────────────────────
# ENDPOINTS INTELIGENCIA ARTIFICIAL
# ──────────────────────────────────────────────

class ItemBanco(BaseModel):
    id: str
    date: str = ""
    description: str = ""
    document: str = ""
    amount: float

class ItemERP(BaseModel):
    id: str
    date: str = ""
    reference: str = ""
    amount: float

class ReconciliacionIARequest(BaseModel):
    banco: List[ItemBanco]
    erp: List[ItemERP]

@app.post("/conciliar-ia")
async def conciliar_con_ia(req: ReconciliacionIARequest):
    """Realiza un cruce semántico de los registros no conciliados usando Groq."""
    try:
        # Por seguridad y límites de tokens, procesamos en bloques de 150 máx
        banco_list = req.banco[:150]
        erp_list = req.erp[:150]
        
        print(f"[IA] Procesando {len(banco_list)} registros bancarios y {len(erp_list)} registros ERP")
        
        matches = await buscar_matches_semanticos_groq(banco_list, erp_list)
        
        print(f"[IA] Se encontraron {len(matches)} coincidencias")
        
        return {
            "success": True,
            "matches": matches,
            "total_procesados": len(banco_list) + len(erp_list),
            "coincidencias": len(matches)
        }
    except Exception as e:
        print(f"[IA] Error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error en IA: {str(e)}")

async def buscar_matches_semanticos_groq(banco: List[ItemBanco], erp: List[ItemERP]) -> list:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY no configurada. Verifica el panel.")
        
    banco_str = json.dumps([b.dict() for b in banco], ensure_ascii=False)
    erp_str = json.dumps([e.dict() for e in erp], ensure_ascii=False)
    
    prompt = f"""Eres un experto contador y conciliador. Analiza estas transacciones bancarias huérfanas y facturas de ERP pendientes.
Trata de encontrar correspondencias basándote en similitud de descripciones, nombres de empresas mal escritos, variaciones y referencias implícitas.

Bancos pendientes (JSON):
{banco_str}

Facturas ERP pendientes (JSON):
{erp_str}

Reglas:
1. Retorna un array JSON estricto con este formato exacto: [{{"id_banco": "B-X", "id_erp": "E-Y", "razon": "explicación de 5 palabras"}}]
2. Relaciona 1 a 1 solamente, donde estés muy seguro (>80% certeza) analizando similitud semántica.
3. No escribas texto markdown, no incluyas ```json, devuelve únicamente el array JSON válido.
4. Si no encuentras ningún cruce seguro, devuelve [] vacío. No inventes cruces."""

    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2048,
                    "temperature": 0.1
                }
            )
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            # Limpiar posible markdown devuelto por Llama 3
            content = content.replace("```json", "").replace("```", "").strip()
            
            cruces = json.loads(content)
            if isinstance(cruces, list):
                return cruces
            return []
    except Exception as e:
        print(f"Excepción llamando a Groq para conciliación: {e}")
        return []

async def generar_narrativa_groq(resultado: dict) -> str:
    if not GROQ_API_KEY:
        return generar_narrativa_local(resultado)

    # Cache: mismo resultado → misma narrativa (5 min TTL)
    cache_key = hashlib.md5(json.dumps(resultado, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cached = groq_cache.get(cache_key)
    if cached is not None:
        return cached

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
            narrativa = data["choices"][0]["message"]["content"]
            groq_cache[cache_key] = narrativa
            return narrativa
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

# ──────────────────────────────────────────────
# ENDPOINTS RECEPCIÓN FACTURAS (IMAP/XML)
# ──────────────────────────────────────────────

@app.post("/sync-emails")
async def sync_invoices_from_email():
    """
    Se conecta al buzón configurado vía IMAP, busca facturas ZIP/XML
    no leídas y las guarda en la base de datos de Neon PostgreSQL.
    """
    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(executor, process_emails)
        if resultado.get("status") == "error":
            raise HTTPException(status_code=500, detail=resultado.get("message"))
        return resultado
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado procesando correos: {str(e)}")

# ──────────────────────────────────────────────
# ENDPOINTS DASHBOARD DE AUDITORÍA (FASE 2)
# ──────────────────────────────────────────────

@app.get("/api/dashboard-auditoria")
async def get_dashboard_data(
    month: int = None,
    year: int = None,
    limit: int = 50,
    offset: int = 0
):
    """Retorna las métricas y la lista paginada de facturas faltantes."""
    try:
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Query con paginación
        query = "SELECT * FROM electronic_documents"
        count_query = "SELECT COUNT(*) as total FROM electronic_documents"
        params = []
        where_clause = ""
        if month and year:
            where_clause = " WHERE EXTRACT(MONTH FROM issue_date) = %s AND EXTRACT(YEAR FROM issue_date) = %s"
            params.extend([month, year])
            
        query += where_clause + " ORDER BY issue_date DESC LIMIT %s OFFSET %s"
        count_query += where_clause
        
        query_params = tuple(params + [limit, offset])
        cursor.execute(query, query_params)
        documents = cursor.fetchall()
        
        cursor.execute(count_query, tuple(params))
        total_recibidas = cursor.fetchone()["total"]
        
        faltantes = [d for d in documents if d['erp_sync_status'] != 'MATCHED']
        
        return {
            "success": True,
            "pagination": {
                "total": total_recibidas,
                "limit": limit,
                "offset": offset
            },
            "metrics": {
                "total_recibidas": total_recibidas,
                "total_cruzadas": total_recibidas - len(faltantes),
                "total_faltantes": len(faltantes),
                "accuracy": round(((total_recibidas - len(faltantes)) / total_recibidas * 100) if total_recibidas > 0 else 0, 1)
            },
            "faltantes": faltantes
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cargando dashboard: {str(e)}")
    finally:
        if 'db' in locals() and db:
            db.close()

@app.post("/api/sync-odoo-invoices")
async def sync_odoo_invoices(credentials: str = Form(...), date_from: str = Form(...), date_to: str = Form(...)):
    """Busca facturas UNMATCHED en DB local y las intenta cruzar contra Odoo."""
    try:
        manager = CredentialManager()
        creds = manager.decrypt(credentials)
        
        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        
        # Traer facturas de Odoo en el rango
        loop = asyncio.get_event_loop()
        facturas_odoo = await loop.run_in_executor(
            executor, connector.fetch_invoices, date_from, date_to, 10000, executor
        )
        
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute("SELECT * FROM electronic_documents WHERE erp_sync_status != 'MATCHED'")
        pendientes = cursor.fetchall()
        
        matches_encontrados = 0
        
        for p in pendientes:
            p_nit = _normalizar_nit_odoo(p['supplier_nit'])
            p_numero = _normalizar_clave_odoo(p['document_number'])
            
            # Buscar en odoo (tolerando que Odoo tenga el dígito de verificación al final)
            match = None
            for o in facturas_odoo:
                o_nit = _normalizar_nit_odoo(o['nit'])
                o_numero = _normalizar_clave_odoo(o['factura_clave'])
                
                nit_match = (o_nit == p_nit) or (o_nit.startswith(p_nit) and len(o_nit) - len(p_nit) <= 1) or (p_nit.startswith(o_nit) and len(p_nit) - len(o_nit) <= 1)
                
                if nit_match and o_numero == p_numero:
                    match = o
                    break
            
            if match:
                # Actualizar DB
                cursor.execute(
                    "UPDATE electronic_documents SET erp_sync_status = 'MATCHED', erp_reference_id = %s WHERE id = %s",
                    (match.get('factura_original', 'ODOO'), p['id'])
                )
                matches_encontrados += 1
                
        db.commit()
        
        return {
            "success": True,
            "message": f"Se encontraron {matches_encontrados} nuevas coincidencias en Odoo.",
            "nuevos_matches": matches_encontrados
        }
        
    except Exception as e:
        if 'db' in locals() and db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"Error en sincronización con Odoo: {str(e)}")
    finally:
        if 'db' in locals() and db:
            db.close()


# ──────────────────────────────────────────────
# AUDITOR IA: CHECKLIST MENSUAL CON GROQ
# ──────────────────────────────────────────────

@app.post("/api/run-ai-checklist")
async def run_ai_checklist(
    month: str = Form(...),
    year: str = Form(...),
    groq_key: Optional[str] = Form(None),
    credentials: Optional[str] = Form(None),
    odoo_url: Optional[str] = Form(None),
    odoo_db: Optional[str] = Form(None),
    odoo_user: Optional[str] = Form(None),
    odoo_pass: Optional[str] = Form(None),
):
    """Ejecuta el auditor contable IA usando Groq sobre datos de Odoo."""
    try:
        # Resolver credenciales de Odoo
        if credentials:
            mgr = CredentialManager()
            creds = mgr.decrypt(credentials)
        elif odoo_url and odoo_db and odoo_user and odoo_pass:
            creds = {"url": odoo_url, "database": odoo_db, "username": odoo_user, "api_key": odoo_pass}
        else:
            raise HTTPException(status_code=400, detail="Credenciales de Odoo requeridas. Configúralas en Configuración.")

        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"]
        )
        connector.authenticate()

        # Construir el rango del mes solicitado
        from calendar import monthrange
        m, y = int(month), int(year)
        days_in_month = monthrange(y, m)[1]
        date_from = f"{y:04d}-{m:02d}-01"
        date_to = f"{y:04d}-{m:02d}-{days_in_month:02d}"

        # Extraer contexto contable de Odoo
        context = build_odoo_context(connector, date_from, date_to)

        # Resolver API key: formulario > variable de entorno
        api_key = groq_key or GROQ_API_KEY
        if not api_key:
            raise HTTPException(status_code=400,
                detail="GROQ_API_KEY no configurada. Defínela como variable de entorno en Render o pásala por formulario.")
        anomalias = run_ai_audit(context, api_key)

        return {
            "success": True,
            "periodo": f"{date_from} / {date_to}",
            "total_facturas_analizadas": context["total_facturas"],
            "anomalias": anomalias
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Auditor IA: {str(e)}")
