from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
import os
import io
import json
import re
import httpx
import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pydantic import BaseModel
from typing import List, Optional
from comparador import comparar_facturas, comparar_facturas_odoo, generar_excel_reporte, generar_excel_reporte_bytes
from odoo_match import OdooConnector, CredentialManager, _normalizar_clave_odoo, _normalizar_nit_odoo
from email_parser import process_emails, process_emails_force, connect_db
from ai_auditor import build_odoo_context, run_ai_audit
from purchase_parser import map_lines_to_odoo, _normalize_desc, _save_corrections_to_history
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
GROQ_MODEL = "llama-3.3-70b-specdec"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

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
# ──────────────────────────────────────────────
# AUTO-MIGRACIÓN: purchase_review
# ──────────────────────────────────────────────

# Cache local del catalogo Odoo (5 min TTL)
odoo_catalog_cache = TTLCache(maxsize=32, ttl=300)


def _get_catalog_cache_key(creds: dict) -> str:
    raw = f"{creds['url']}|{creds['database']}|{creds['username']}"
    return hashlib.md5(raw.encode()).hexdigest()


@app.on_event("startup")
def ensure_purchase_tables():
    """Migración idempotente: crea tablas si no existen, agrega columnas faltantes."""
    db = None
    try:
        db = connect_db()
        cursor = db.cursor()

        # purchase_review
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchase_review (
                id SERIAL PRIMARY KEY,
                doc_id UUID NOT NULL,
                company_id VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                ai_suggestions JSONB,
                manual_lines JSONB,
                purchase_order_id VARCHAR(64),
                groq_response_raw TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                reviewed_at TIMESTAMP,
                confirmed_at TIMESTAMP
            )
        """)

        # Migraciones futuras: ADD COLUMN IF NOT EXISTS
        for col_name, col_type in [
            ("groq_response_raw", "TEXT"),
        ]:
            cursor.execute(f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='purchase_review' AND column_name='{col_name}'
                    ) THEN
                        ALTER TABLE purchase_review ADD COLUMN {col_name} {col_type};
                    END IF;
                END $$;
            """)

        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_review_doc_unique ON purchase_review(doc_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_purchase_review_status ON purchase_review(status)")

        # product_mapping_cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product_mapping_cache (
                id SERIAL PRIMARY KEY,
                line_hash VARCHAR(64) UNIQUE NOT NULL,
                line_items_json TEXT NOT NULL,
                catalogo_json TEXT,
                resultado_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)

        # correction_history (aprendizaje automático)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS correction_history (
                id SERIAL PRIMARY KEY,
                normalized_desc TEXT NOT NULL,
                producto_id INTEGER NOT NULL,
                producto_nombre TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_correction_history_desc
            ON correction_history(normalized_desc)
        """)

        # Deduplicar correction_history por si existen registros duplicados antes de crear el índice único
        cursor.execute("""
            DELETE FROM correction_history a
            USING correction_history b
            WHERE a.id < b.id AND a.normalized_desc = b.normalized_desc
        """)

        # Crear índice único sobre normalized_desc para soportar INSERT ... ON CONFLICT
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_correction_history_desc_uniq 
            ON correction_history(normalized_desc)
        """)

        db.commit()
        print("[startup] Tablas aseguradas correctamente (sin DROP).")
    except Exception as e:
        print(f"[startup] Error: {e}")
        if db:
            db.rollback()
    finally:
        if db:
            db.close()


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


@app.post("/sync-emails/force")
async def sync_invoices_force():
    """
    Re-sincronización forzada: escanea TODOS los correos del buzón
    (incluyendo ya leídos), borra los registros de email_inbox_logs anteriores
    y hace UPSERT en electronic_documents para reimportar facturas borradas de la BD.
    Útil cuando se vació la base de datos y se quiere repoblar desde el correo.
    """
    try:
        loop = asyncio.get_event_loop()
        resultado = await loop.run_in_executor(executor, process_emails_force)
        if resultado.get("status") == "error":
            raise HTTPException(status_code=500, detail=resultado.get("message"))
        return resultado
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado en re-sincronización forzada: {str(e)}")

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
# MÓDULO 1 — COMPRAS AUTOMÁTICAS DESDE CORREO
# ──────────────────────────────────────────────

@app.get("/api/purchase/pending")
async def purchase_pending(limit: int = 50, offset: int = 0):
    """
    Retorna documentos electrónicos pendientes de revisión (no están en
    purchase_review o están con status PENDING).
    """
    db = None
    try:
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            SELECT ed.*, pr.id as review_id, pr.status as review_status,
                   pr.ai_suggestions, pr.manual_lines, pr.purchase_order_id
            FROM electronic_documents ed
            LEFT JOIN purchase_review pr ON pr.doc_id = ed.id
            WHERE pr.id IS NULL OR pr.status IN ('PENDING', 'REVIEWED_OK', 'CORRECTED')
            ORDER BY ed.issue_date DESC
            LIMIT %s OFFSET %s
        """, (limit, offset))
        docs = cursor.fetchall()

        cursor.execute("""
            SELECT COUNT(*) as total FROM electronic_documents ed
            LEFT JOIN purchase_review pr ON pr.doc_id = ed.id
            WHERE pr.id IS NULL OR pr.status IN ('PENDING', 'REVIEWED_OK', 'CORRECTED')
        """)
        total = cursor.fetchone()["total"]

        # Parsear xml_metadata y JSONB columns (vienen como string crudo)
        resultados = []
        for d in docs:
            row = dict(d)
            # xml_metadata
            try:
                meta = json.loads(row.get("xml_metadata") or "{}")
                row["line_items"] = meta.get("line_items", [])
            except (json.JSONDecodeError, TypeError):
                row["line_items"] = []
            # ai_suggestions (JSONB → string crudo)
            try:
                raw = row.get("ai_suggestions")
                row["ai_suggestions"] = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                row["ai_suggestions"] = None
            # manual_lines (JSONB → string crudo)
            try:
                raw = row.get("manual_lines")
                row["manual_lines"] = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                row["manual_lines"] = None
            resultados.append(row)

        return {
            "success": True,
            "pagination": {"total": total, "limit": limit, "offset": offset},
            "documentos": resultados,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error cargando pendientes: {str(e)}")
    finally:
        if db:
            db.close()


@app.post("/api/purchase/parse-lines")
async def purchase_parse_lines(doc_id: str = Form(...)):
    """
    Parsea y retorna las líneas de detalle de un documento específico.
    """
    db = None
    try:
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("SELECT id, xml_metadata FROM electronic_documents WHERE id = %s", (doc_id,))
        doc = cursor.fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Documento no encontrado.")

        try:
            meta = json.loads(doc["xml_metadata"] or "{}")
            line_items = meta.get("line_items", [])
        except (json.JSONDecodeError, TypeError):
            line_items = []

        return {
            "success": True,
            "doc_id": doc_id,
            "line_items": line_items,
            "total_lineas": len(line_items),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parseando líneas: {str(e)}")
    finally:
        if db:
            db.close()


@app.post("/api/purchase/ai-map")
async def purchase_ai_map(
    doc_id: str = Form(...),
    credentials: str = Form(...),
    groq_key: Optional[str] = Form(None),
):
    """
    Toma un documento pendiente, extrae sus líneas, obtiene el catálogo
    de productos desde Odoo, y usa Groq para mapear cada línea a un producto.
    Guarda el resultado en purchase_review.ai_suggestions.
    """
    db = None
    try:
        # 1. Leer documento de BD (con bloqueo FOR UPDATE para evitar concurrencia)
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cursor.execute("""
            SELECT ed.*, pr.id as review_id
            FROM electronic_documents ed
            LEFT JOIN purchase_review pr ON pr.doc_id = ed.id
            WHERE ed.id = %s
            FOR UPDATE
        """, (doc_id,))
        doc = cursor.fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Documento no encontrado.")

        # 2. Parsear line_items del xml_metadata
        try:
            meta = json.loads(doc["xml_metadata"] or "{}")
            line_items = meta.get("line_items", [])
        except (json.JSONDecodeError, TypeError):
            meta = {}
            line_items = []

        # Fallback: si el doc se insertó antes de que existiera line_items,
        # crear una línea virtual a partir del total
        if not line_items:
            line_items = [{
                "numero": "1",
                "descripcion": doc.get("supplier_name") or meta.get("supplier_name", "Sin descripción"),
                "codigo_producto": "",
                "cantidad": 1,
                "precio_unitario": float(doc.get("total_amount", 0) or 0),
                "total": float(doc.get("total_amount", 0) or 0),
            }]

        # 3. Conectar a Odoo y obtener catálogo de productos (con cache local e invalidación automática)
        manager = CredentialManager()
        creds = manager.decrypt(credentials)

        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"],
        )

        catalog_ver = connector.get_catalog_last_update()
        cache_key = f"{_get_catalog_cache_key(creds)}_{catalog_ver}"
        if cache_key in odoo_catalog_cache:
            odoo_products = odoo_catalog_cache[cache_key]
            print(f"[ai-map] Cache local catalogo (ver: {catalog_ver}): {len(odoo_products)} productos")
        else:
            # Solicitar únicamente campos necesarios con un alto límite para optimizar memoria
            odoo_products = connector.search_products(limit=40000, fields=["id", "name", "default_code"])
            odoo_catalog_cache[cache_key] = odoo_products
            print(f"[ai-map] Catalogo descargado de Odoo (ver: {catalog_ver}): {len(odoo_products)} productos")

        # 4. Ejecutar mapeo IA
        api_key = groq_key or os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=400, detail="GROQ_API_KEY no configurada.")

        sugerencias = map_lines_to_odoo(line_items, odoo_products, api_key, db_cursor=cursor)

        # 5. Guardar en purchase_review (crear si no existe)
        #    Verificar concurrencia: si ya existe una OC completada, no sobrescribir
        cursor.execute("""
            SELECT status FROM purchase_review WHERE doc_id = %s FOR UPDATE
        """, (doc_id,))
        existing = cursor.fetchone()
        if existing and existing["status"] in ("COMPLETED",):
            raise HTTPException(
                status_code=409,
                detail=f"El documento ya tiene una OC generada (estado: {existing['status']}). No se puede re-mapear."
            )

        cursor.execute("""
            INSERT INTO purchase_review (doc_id, company_id, status, ai_suggestions)
            VALUES (%s, %s, 'PENDING', %s::jsonb)
            ON CONFLICT (doc_id)
            DO UPDATE SET ai_suggestions = EXCLUDED.ai_suggestions, status = 'PENDING'
        """, (doc_id, str(doc["company_id"]), json.dumps(sugerencias, ensure_ascii=False)))
        db.commit()

        return {
            "success": True,
            "doc_id": doc_id,
            "sugerencias": sugerencias,
            "total_lineas": len(sugerencias),
        }

    except HTTPException:
        if db:
            db.rollback()
        raise
    except Exception as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"Error en mapeo IA: {str(e)}")
    finally:
        if db:
            db.close()


@app.post("/api/purchase/review")
async def purchase_review(
    doc_id: str = Form(...),
    action: str = Form(...),
    manual_lines: Optional[str] = Form(None),
):
    """
    Aprueba o corrige el mapeo IA de un documento.
    action = "accept" → status = 'REVIEWED_OK'
    action = "correct" → status = 'CORRECTED', guarda manual_lines
    """
    if action not in ("accept", "correct"):
        raise HTTPException(status_code=400, detail="action debe ser 'accept' o 'correct'.")

    db = None
    try:
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Bloquear fila para evitar condiciones de carrera
        cursor.execute("""
            SELECT status FROM purchase_review WHERE doc_id = %s FOR UPDATE
        """, (doc_id,))
        existing = cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="No hay registro de purchase_review para este documento. Ejecuta /api/purchase/ai-map primero.")
        if existing["status"] == "COMPLETED":
            raise HTTPException(status_code=409, detail="El documento ya fue completado y tiene una OC generada. No se puede modificar.")

        cursor.execute("""
            UPDATE purchase_review
            SET status = %s,
                manual_lines = CASE WHEN %s THEN %s::jsonb ELSE manual_lines END,
                reviewed_at = NOW()
            WHERE doc_id = %s
            RETURNING id
        """, (
            "REVIEWED_OK" if action == "accept" else "CORRECTED",
            action == "correct",
            manual_lines or "[]",
            doc_id,
        ))
        updated = cursor.fetchone()

        # Si es correccion, guardar en correction_history para aprendizaje automatico
        if action == "correct" and manual_lines:
            try:
                parsed_lines = json.loads(manual_lines) if isinstance(manual_lines, str) else manual_lines
                _save_corrections_to_history(cursor, parsed_lines)
            except Exception as hist_err:
                print(f"[review] Error guardando historial: {hist_err}")

        db.commit()
        return {"success": True, "review_id": updated["id"], "status": "REVIEWED_OK" if action == "accept" else "CORRECTED"}

    except HTTPException:
        if db:
            db.rollback()
        raise
    except Exception as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"Error guardando revisión: {str(e)}")
    finally:
        if db:
            db.close()


@app.post("/api/purchase/search-products")
async def purchase_search_products(
    query: str = Form(""),
    credentials: str = Form(...),
    limit: int = Form(50),
):
    """
    Busca productos en Odoo por nombre o código (autocomplete para el modal).
    """
    try:
        manager = CredentialManager()
        creds = manager.decrypt(credentials)
        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"],
        )
        products = connector.search_products(query=query, limit=limit)
        return {"success": True, "products": products, "total": len(products)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error buscando productos: {str(e)}")


@app.post("/api/purchase/create-oc")
async def purchase_create_oc(
    doc_id: str = Form(...),
    credentials: str = Form(...),
    action: str = Form("create"),  # "create" | "create_and_confirm"
):
    """
    Crea una Orden de Compra en Odoo a partir de un documento revisado.
    Requiere que el documento tenga status REVIEWED_OK o CORRECTED.
    """
    if action not in ("create", "create_and_confirm"):
        raise HTTPException(status_code=400, detail="action debe ser 'create' o 'create_and_confirm'.")

    db = None
    try:
        db = connect_db()
        cursor = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1. Leer documento + purchase_review (con bloqueo de fila para evitar concurrencia)
        cursor.execute("""
            SELECT ed.*, pr.id as review_id, pr.status as review_status,
                   pr.ai_suggestions, pr.manual_lines, pr.purchase_order_id
            FROM electronic_documents ed
            JOIN purchase_review pr ON pr.doc_id = ed.id
            WHERE ed.id = %s
            FOR UPDATE OF pr NOWAIT
        """, (doc_id,))
        doc = cursor.fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Documento no encontrado en purchase_review.")

        if doc["review_status"] not in ("REVIEWED_OK", "CORRECTED"):
            raise HTTPException(status_code=400, detail=f"Estado inválido: {doc['review_status']}. Debe ser REVIEWED_OK o CORRECTED.")

        if doc.get("purchase_order_id"):
            raise HTTPException(status_code=409, detail=f"Ya existe una OC asociada: {doc['purchase_order_id']}.")

        # 2. Obtener líneas finales (manual_lines si CORRECTED, sino ai_suggestions)
        #    JSONB columns retornan como string — parsear con json.loads
        lines = []
        try:
            if doc["review_status"] == "CORRECTED" and doc.get("manual_lines"):
                raw = doc["manual_lines"]
                lines = json.loads(raw) if isinstance(raw, str) else raw
            elif doc.get("ai_suggestions"):
                raw = doc["ai_suggestions"]
                lines = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            lines = []

        if not lines:
            # Fallback a line_items del xml_metadata
            try:
                meta = json.loads(doc.get("xml_metadata") or "{}")
                lines = meta.get("line_items", [])
            except (json.JSONDecodeError, TypeError):
                lines = []
        if not lines:
            raise HTTPException(status_code=400, detail="No hay líneas de detalle para crear la OC.")

        # 3. Extraer NIT del proveedor desde el documento
        try:
            meta = json.loads(doc.get("xml_metadata") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        supplier_nit = doc.get("supplier_nit") or meta.get("supplier_nit", "")
        supplier_nit_clean = re.sub(r"[^0-9]", "", supplier_nit)
        supplier_name = doc.get("supplier_name") or meta.get("supplier_name", "Proveedor")
        doc_number = doc.get("document_number", "")
        if not supplier_nit_clean:
            raise HTTPException(status_code=400, detail="No se pudo determinar el NIT del proveedor.")

        # 4. Conectar a Odoo y buscar proveedor
        manager = CredentialManager()
        creds = manager.decrypt(credentials)

        connector = OdooConnector(
            url=creds["url"],
            database=creds["database"],
            username=creds["username"],
            api_key=creds["api_key"],
        )

        partner = connector.search_partner_by_vat(supplier_nit_clean)
        if not partner:
            raise HTTPException(status_code=404, detail=f"Proveedor con NIT {supplier_nit_clean} no encontrado en Odoo.")

        # 5. Preparar líneas para Odoo — validar producto_id
        #    Si el doc fue CORRECTED o REVIEWED_OK, se confía en el mapeo sin importar
        #    el valor de confianza almacenado (puede ser 0 en registros previos al fix).
        oc_lines = []
        errores = []
        is_reviewed = doc["review_status"] in ("REVIEWED_OK", "CORRECTED")
        is_corrected = doc["review_status"] == "CORRECTED"

        for i, line in enumerate(lines):
            pid = line.get("producto_id")
            nombre_linea = (
                line.get("producto_nombre")
                or line.get("descripcion_original")
                or line.get("descripcion")
                or f"Línea {i+1}"
            )

            if not pid:
                errores.append(f"Línea {i+1} ('{nombre_linea}'): Sin producto asignado (producto_id es nulo)")
                continue

            # Para documentos revisados/corregidos, la confianza NO bloquea la creación.
            # El usuario ya validó manualmente o aprobó el mapeo IA.
            if not is_reviewed:
                conf = float(line.get("confianza", 0.0) or 0.0)
                if conf < 0.60:
                    errores.append(f"Línea {i+1} ('{nombre_linea}'): Confianza muy baja ({conf:.0%}). Usa MAPEAR IA y corrige.")
                    continue

            oc_lines.append({
                "producto_id": pid,
                "cantidad": line.get("cantidad", 1),
                "precio_unitario": line.get("precio_unitario", 0),
                "nombre": nombre_linea,
            })

        if errores:
            mensaje = "No se puede crear la Orden de Compra debido a errores en las líneas:\n" + "\n".join(errores)
            raise HTTPException(status_code=400, detail=mensaje)

        # 6. Crear OC
        reference = f"FACTUMATCH-{doc_number}" if doc_number else f"FACTUMATCH-{doc_id}"
        order = connector.create_purchase_order(
            partner_id=partner["id"],
            lines=oc_lines,
            reference=reference,
        )

        # 7. Confirmar si se solicita
        state = order.get("state", "draft")
        if action == "create_and_confirm":
            confirmed = connector.confirm_purchase_order(order["id"])
            state = confirmed.get("state", state)

        # 8. Guardar en BD
        order_ref = f"{order.get('name', '')} (ID: {order['id']})"
        cursor.execute("""
            UPDATE purchase_review
            SET status = 'COMPLETED',
                purchase_order_id = %s,
                confirmed_at = NOW()
            WHERE doc_id = %s
        """, (order_ref, doc_id))
        db.commit()

        return {
            "success": True,
            "doc_id": doc_id,
            "purchase_order_id": order["id"],
            "purchase_order_name": order.get("name", ""),
            "partner_name": partner.get("name", ""),
            "total_lineas": len(lines),
            "lineas_procesadas": len(oc_lines),
            "lineas_omitidas": 0,
            "state": state,
            "message": f"OC {order.get('name', '')} creada exitosamente." +
                      (" Confirmada." if action == "create_and_confirm" else " En estado borrador.")
        }

    except HTTPException:
        if db:
            db.rollback()
        raise
    except Exception as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"Error creando OC: {str(e)}")
    finally:
        if db:
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

        # Resolver API key: formulario > variable global > os.getenv direct
        api_key = groq_key or GROQ_API_KEY or os.getenv("GROQ_API_KEY", "")
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


# ──────────────────────────────────────────────
# DIAGNÓSTICO DE CONECTIVIDAD CON GROQ
# ──────────────────────────────────────────────

def _sanitize_groq_error(text: str, api_key: str) -> str:
    """Elimina secretos de un mensaje de error antes de exponerlo."""
    safe = text or ""
    if api_key and len(api_key) > 4:
        safe = safe.replace(api_key, "***")
    # Por defensa, ocultar cualquier patrón de key gsk_
    safe = re.sub(r"gsk_[A-Za-z0-9]{8,}", "gsk_***", safe)
    return safe.strip()


@app.get("/api/ai/test-groq")
async def test_groq():
    """
    Prueba la conectividad Factu Match → Groq con la configuración desplegada.

    Devuelve un JSON estructurado que identifica la etapa exacta de la falla:
      - "configuration": GROQ_API_KEY ausente
      - "network": timeout, DNS o error de conexión
      - "groq": error HTTP de Groq (400, 401, 403, 429, 5xx...)
      - "success": respuesta válida

    NUNCA devuelve la API key. Solo los últimos 4 caracteres para identificación.
    """
    api_key = GROQ_API_KEY or os.getenv("GROQ_API_KEY", "")
    key_last4 = api_key[-4:] if len(api_key) >= 4 else ""

    base: dict = {
        "success": False,
        "stage": "unknown",
        "model": GROQ_MODEL,
    }
    if key_last4:
        base["key_last4"] = f"...{key_last4}"

    if not api_key:
        base["stage"] = "configuration"
        base["error"] = "GROQ_API_KEY no está configurada como variable de entorno."
        return base

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": "Responde únicamente: GROQ_OK"}],
        "max_tokens": 10,
        "temperature": 0,
    }

    try:
        response = httpx.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
    except httpx.TimeoutException as e:
        base["stage"] = "network"
        base["error"] = f"Timeout conectando a Groq: {_sanitize_groq_error(str(e), api_key)}"
        return base
    except httpx.ConnectError as e:
        base["stage"] = "network"
        base["error"] = f"Error de conexión con Groq (DNS/red): {_sanitize_groq_error(str(e), api_key)}"
        return base
    except httpx.NetworkError as e:
        base["stage"] = "network"
        base["error"] = f"Error de red con Groq: {_sanitize_groq_error(str(e), api_key)}"
        return base
    except httpx.HTTPStatusError as e:
        base["stage"] = "groq"
        base["status_code"] = e.response.status_code
        body_safe = _sanitize_groq_error(e.response.text or "", api_key)[:400]
        base["error"] = f"HTTP {e.response.status_code}: {body_safe}"
        return base

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("content no es string")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        base["stage"] = "groq"
        base["status_code"] = response.status_code
        base["error"] = "Respuesta HTTP 200 con estructura inesperada de Groq."
        return base

    return {
        "success": True,
        "stage": "success",
        "status_code": response.status_code,
        "model": GROQ_MODEL,
        "message": content.strip(),
    }
