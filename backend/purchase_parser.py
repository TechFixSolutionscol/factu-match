"""
purchase_parser.py — Mapeo IA de líneas de factura a productos Odoo.

Flujo:
  1. map_lines_to_odoo() recibe las líneas XML + catálogo Odoo
  2. Construye prompt y llama a Groq
  3. Groq devuelve JSON con producto_id, nombre, confianza
  4. Resultado se cachea en product_mapping_cache (Neon) para no repetir
"""

import json
import hashlib
import httpx
import psycopg2
import psycopg2.extras

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
1. Compára la descripción de cada línea contra el nombre y código del producto en Odoo.
2. Si no hay coincidencia clara (< 60% de confianza), usa producto_id = null.
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
    "confianza": 0.95
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

        # Guardar cache
        if db_cursor:
            _save_cache(db_cursor, line_hash, line_items, odoo_products, resultado)

        return resultado

    except Exception as e:
        print(f"[purchase_parser] Error llamando a Groq: {e}")
        # Fallback: devolver líneas sin mapeo
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
