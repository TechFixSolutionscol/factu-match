"""
purchase_parser.py — Mapeo IA de líneas de factura a productos Odoo.

Flujo:
  1. map_lines_to_odoo() recibe las líneas XML + catálogo Odoo
  2. Construye prompt y llama a Groq
  3. Groq devuelve JSON con producto_id, nombre, confianza
  4. Resultado se cachea en product_mapping_cache (Neon) para no repetir
  5. Si Groq falla o no encuentra match, usa fuzzy matching local (thefuzz)
"""

import json
import hashlib
import httpx
import psycopg2
import psycopg2.extras
import re

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"


def _build_prompt(line_items: list, odoo_products: list) -> str:
    """Construye el prompt para Groq con las líneas XML y el catálogo Odoo."""
    lineas_json = json.dumps(line_items, ensure_ascii=False, indent=2)
    catalogo_json = json.dumps(odoo_products, ensure_ascii=False, indent=2)

    return f"""Eres un auxiliar contable experto en facturación electrónica colombiana.
Tu tarea es mapear cada línea de una factura electrónica (XML UBL 2.1) al producto
más similar en el catálogo de Odoo.

Reglas:
1. Compara la descripción de cada línea contra el nombre y código del producto en Odoo.
2. Siempre elige el producto más parecido del catálogo, incluso con baja confianza.
   Si absolutamente ningún producto se acerca, usa producto_id = null.
3. Respeta cantidades y precios originales de la factura (NO los modifiques).
4. Responde ÚNICAMENTE con un array JSON válido. Sin markdown, sin texto adicional.

Formato de respuesta:
[
  {{
    "numero_linea": "1",
    "descripcion_original": "texto de la factura",
    "producto_id": 123,
    "producto_nombre": "Nombre en Odoo",
    "codigo_producto": "REF-001",
    "cantidad": 10.0,
    "precio_unitario": 15000.0,
    "confianza": 0.95,
    "razon": "La descripción coincide con el producto X en el catálogo Odoo"
  }}
]

Líneas de la factura:
{lineas_json}

Catálogo de productos Odoo:
{catalogo_json}"""


def _lines_hash(line_items: list) -> str:
    """Hash único del contenido de las líneas para cache."""
    raw = json.dumps(line_items, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_cache(cursor, line_hash: str) -> list:
    """Busca en product_mapping_cache por hash. Retorna lista o None."""
    cursor.execute(
        "SELECT resultado_json FROM product_mapping_cache WHERE line_hash = %s",
        (line_hash,)
    )
    row = cursor.fetchone()
    if row:
        try:
            return json.loads(row["resultado_json"])
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _save_cache(cursor, line_hash: str, line_items: list, odoo_products: list, resultado: list):
    """Guarda el resultado en cache."""
    cursor.execute("""
        INSERT INTO product_mapping_cache (line_hash, line_items_json, catalogo_json, resultado_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (line_hash) DO NOTHING
    """, (
        line_hash,
        json.dumps(line_items, ensure_ascii=False),
        json.dumps(odoo_products, ensure_ascii=False),
        json.dumps(resultado, ensure_ascii=False),
    ))


def _fuzzy_match_line(desc: str, odoo_products: list, threshold: int = 40) -> dict:
    """
    Fuzzy matching local para cuando Groq falla o no encuentra match.
    Compara la descripción de la línea contra nombre y default_code
    de cada producto Odoo usando token set ratio + partial ratio.

    Retorna dict con producto_id, producto_nombre, confianza (0-1).
    Si no hay match sobre threshold, retorta confianza 0.
    """
    if not desc or not odoo_products:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}

    try:
        from thefuzz import fuzz, process
    except ImportError:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}

    desc_lower = desc.lower().strip()
    # Construir candidatos: (nombre, default_code, id)
    candidates = []
    for p in odoo_products:
        name = (p.get("name") or "").lower().strip()
        code = (p.get("default_code") or "").lower().strip()
        candidates.append((name, code, p["id"], p.get("name", "")))

    best_score = 0
    best_pid = None
    best_pname = None

    for name, code, pid, pname in candidates:
        # Token set ratio sobre el nombre
        score_name = fuzz.token_set_ratio(desc_lower, name)
        # Partial ratio si hay código
        score_code = fuzz.partial_ratio(desc_lower, code) if code else 0
        # También probar el ratio directo
        score_direct = fuzz.ratio(desc_lower, name)

        score = max(score_name, score_code, score_direct)

        # Bonus si el código está contenido en la descripción
        if code and code in desc_lower:
            score = max(score, 85)

        if score > best_score:
            best_score = score
            best_pid = pid
            best_pname = pname

    if best_score >= threshold:
        return {
            "producto_id": best_pid,
            "producto_nombre": best_pname,
            "confianza": round(best_score / 100.0, 2),
        }

    return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}


def _run_fuzzy_fallback(line_items: list, odoo_products: list) -> list:
    """Ejecuta fuzzy matching sobre todas las líneas. Retorna lista de sugerencias."""
    resultado = []
    for i, line in enumerate(line_items):
        desc = line.get("descripcion_original") or line.get("descripcion", "")
        match = _fuzzy_match_line(desc, odoo_products)
        resultado.append({
            "numero_linea": line.get("numero", str(i)),
            "descripcion_original": desc,
            "producto_id": match["producto_id"],
            "producto_nombre": match["producto_nombre"],
            "codigo_producto": line.get("codigo_producto", ""),
            "cantidad": line.get("cantidad", 0),
            "precio_unitario": line.get("precio_unitario", 0),
            "confianza": match["confianza"],
            "razon": f"Fuzzy match local: {match['confianza']*100:.0f}% similitud con '{match['producto_nombre'] or 'ninguno'}'" if match["confianza"] > 0 else "Sin coincidencia en catálogo Odoo",
        })
    return resultado


def map_lines_to_odoo(
    line_items: list,
    odoo_products: list,
    groq_api_key: str,
    db_cursor=None,
) -> list:
    """
    Mapea líneas de factura a productos Odoo usando Groq.
    Si recibe db_cursor, consulta/guarda cache automáticamente.

    Retorna lista de dicts:
      [{numero_linea, descripcion_original, producto_id, producto_nombre,
        codigo_producto, cantidad, precio_unitario, confianza}]
    """
    if not line_items:
        return []

    if not odoo_products:
        # Sin catálogo Odoo, devolver líneas sin mapeo
        return [
            {
                "numero_linea": it.get("numero", str(i)),
                "descripcion_original": it.get("descripcion", ""),
                "producto_id": None,
                "producto_nombre": None,
                "codigo_producto": it.get("codigo_producto", ""),
                "cantidad": it.get("cantidad", 0),
                "precio_unitario": it.get("precio_unitario", 0),
                "confianza": 0.0,
            }
            for i, it in enumerate(line_items)
        ]

    line_hash = _lines_hash(line_items)

    # Cache hit
    if db_cursor:
        cached = _check_cache(db_cursor, line_hash)
        if cached is not None:
            return cached

    prompt = _build_prompt(line_items, odoo_products)

    try:
        response = httpx.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                "temperature": 0.1,
            },
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]

        # Limpiar posible markdown
        content = content.replace("```json", "").replace("```", "").strip()

        resultado = json.loads(content)
        if not isinstance(resultado, list):
            resultado = []

        # Asegurar campos requeridos
        for r in resultado:
            r.setdefault("numero_linea", "")
            r.setdefault("descripcion_original", "")
            r.setdefault("producto_id", None)
            r.setdefault("producto_nombre", None)
            r.setdefault("codigo_producto", "")
            r.setdefault("cantidad", 0)
            r.setdefault("precio_unitario", 0)
            r.setdefault("confianza", 0.0)
            r.setdefault("razon", "")

        # Fuzzy fallback: para líneas que Groq dejó sin producto_id
        for r in resultado:
            if r.get("producto_id") is None:
                fuzzy = _fuzzy_match_line(
                    r.get("descripcion_original", ""), odoo_products
                )
                if fuzzy["producto_id"]:
                    r["producto_id"] = fuzzy["producto_id"]
                    r["producto_nombre"] = fuzzy["producto_nombre"]
                    r["confianza"] = fuzzy["confianza"]
                    r["razon"] = f"Groq no encontró match → Fuzzy local: {fuzzy['confianza']*100:.0f}%"

        # Guardar cache
        if db_cursor:
            _save_cache(db_cursor, line_hash, line_items, odoo_products, resultado)

        return resultado

    except Exception as e:
        print(f"[purchase_parser] Error llamando a Groq: {e}")
        # Fallback: fuzzy matching local
        return _run_fuzzy_fallback(line_items, odoo_products)
