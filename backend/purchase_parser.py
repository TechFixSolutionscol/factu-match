"""
purchase_parser.py — Mapeo IA de líneas de factura a productos Odoo.

Flujo:
  1. Normaliza descripciones de líneas (stop-words, capacidades, colores)
  2. Pre-filtra catálogo Odoo a top 20 candidatos por línea vía fuzzy
  3. Envía solo candidatos a Groq con prompt semántico mejorado
  4. Groq devuelve JSON con producto_id, nombre, confianza
  5. Post-procesa líneas sin match con fuzzy local mejorado
  6. Cachea con hash que incluye líneas + catálogo + versión del algoritmo
"""

import json
import hashlib
import httpx
import psycopg2
import psycopg2.extras
import re

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
ALGORITHM_VERSION = "3"

_STOP_WORDS = {
    "N/A", "UND", "UNIDAD", "UNIDADES", "COLOR", "NEGRO", "BLANCO",
    "ROJO", "AZUL", "VERDE", "AMARILLO", "GRIS", "PLATEADO", "DORADO",
    "WIFI", "SSD", "HDD", "GB", "TB", "MHZ", "GHZ", "HZ", "N/A",
    "NADA", "SIN", "NO", "APLICA", "VARIOS", "DIVERSOS", "GENÉRICO",
    "GENERICO", "STANDARD", "ESTANDAR", "BASICO", "BASIC", "PRO",
    "LITE", "NUEVO", "NEW", "ORIGINAL", "REACONDICIONADO",
    "CABLE", "ADAPTADOR", "CARGADOR", "MANUAL", "GUIA", "GARANTIA",
    "INCLUYE", "INCLUIDO", "MAS", "PLUS", "KIT", "SET", "PAQUETE",
}

_CAPACITY_RE = re.compile(
    r'\b\d+\s*(?:GB|TB|MHZ|GHZ)\b',
    re.IGNORECASE
)
_FRACTION_CAPACITY_RE = re.compile(
    r'\b\d+/\d+\s*(?:GB|TB)\b',
    re.IGNORECASE
)
_TECH_SPEC_RE = re.compile(
    r'\b\d+x\d+\b',
    re.IGNORECASE
)
_MODEL_SUFFIX_RE = re.compile(
    r'\b(?:N[/-]A|N/A|NA)\b',
    re.IGNORECASE
)
_MULTI_SPACE_RE = re.compile(r'\s+')
_NON_ALPHA_RE = re.compile(r'[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9\s]')


def _normalize_desc(raw: str) -> str:
    if not raw:
        return ""
    s = raw.upper().strip()
    s = _MODEL_SUFFIX_RE.sub(" ", s)
    s = _FRACTION_CAPACITY_RE.sub(" ", s)
    s = _CAPACITY_RE.sub(" ", s)
    s = _TECH_SPEC_RE.sub(" ", s)
    words = _MULTI_SPACE_RE.split(s)
    cleaned = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w in _STOP_WORDS:
            continue
        if re.match(r'^\d+[/\.]\d+$', w):
            continue
        cleaned.append(w)
    return " ".join(cleaned)


def _extract_brand_tokens(desc: str) -> set:
    """Extrae tokens de marca/identificación: palabras largas y códigos."""
    tokens = set()
    for w in desc.upper().split():
        wc = re.sub(r'[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]', '', w)
        if len(wc) >= 3 and not wc.isdigit():
            tokens.add(wc.lower())
    return tokens


def _fuzzy_match_line(
    raw_desc: str,
    norm_desc: str,
    odoo_products: list,
    threshold: int = 35
) -> dict:
    if not norm_desc or not odoo_products:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}

    try:
        from thefuzz import fuzz
    except ImportError:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}

    brand_tokens = _extract_brand_tokens(raw_desc)
    best_score = 0
    best_pid = None
    best_pname = None

    for p in odoo_products:
        pname = (p.get("name") or "").strip()
        pcode = (p.get("default_code") or "").strip()
        pname_norm = _normalize_desc(pname)
        pcode_norm = _normalize_desc(pcode)

        score_name_ts = fuzz.token_set_ratio(norm_desc, pname_norm)
        score_name_partial = fuzz.partial_ratio(norm_desc, pname_norm)
        score_name_full = fuzz.ratio(norm_desc, pname_norm)
        score_code = fuzz.partial_ratio(norm_desc, pcode_norm) if pcode_norm else 0

        score = max(score_name_ts, score_name_partial, score_name_full, score_code)

        if brand_tokens:
            pbrand = _extract_brand_tokens(pname)
            overlap = brand_tokens & pbrand
            if overlap:
                score = max(score, min(95, score + 25))

        if pcode_norm and pcode_norm in norm_desc:
            score = max(score, 88)

        if pname_norm and pname_norm in norm_desc:
            score = max(score, 92)

        if score > best_score:
            best_score = score
            best_pid = p["id"]
            best_pname = pname

    if best_score >= threshold:
        return {
            "producto_id": best_pid,
            "producto_nombre": best_pname,
            "confianza": round(min(best_score / 100.0, 1.0), 2),
        }

    return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}


def _select_top_candidates(
    line_items: list,
    odoo_products: list,
    top_n: int = 25
) -> list:
    """
    Pre-filtra el catálogo Odoo a los mejores top_n candidatos por línea.
    Retorna lista única de productos que cubren todas las líneas.
    """
    if not odoo_products:
        return []

    try:
        from thefuzz import fuzz
    except ImportError:
        return odoo_products[:top_n]

    scored = {}
    for line in line_items:
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        if not norm_desc:
            continue
        brand_tokens = _extract_brand_tokens(raw_desc)
        for p in odoo_products:
            pid = p["id"]
            pname_norm = _normalize_desc(p.get("name") or "")
            if not pname_norm:
                continue
            s = fuzz.token_set_ratio(norm_desc, pname_norm)
            s = max(s, fuzz.partial_ratio(norm_desc, pname_norm))
            s = max(s, fuzz.ratio(norm_desc, pname_norm))
            if brand_tokens:
                pbrand = _extract_brand_tokens(p.get("name") or "")
                if brand_tokens & pbrand:
                    s = max(s, min(95, s + 25))
            if pid not in scored or s > scored[pid]:
                scored[pid] = s

    sorted_pids = sorted(scored, key=lambda pid: scored[pid], reverse=True)
    top_pids = set(sorted_pids[:top_n])

    result = [p for p in odoo_products if p["id"] in top_pids]
    print(f"[purchase_parser] Catálogo completo: {len(odoo_products)} productos → pre-seleccionados: {len(result)}")
    return result if result else odoo_products[:top_n]


def _build_prompt(line_items: list, odoo_products: list) -> str:
    raw_items = []
    for line in line_items:
        raw = line.get("descripcion_original") or line.get("descripcion", "")
        norm = _normalize_desc(raw)
        raw_items.append({
            "numero": line.get("numero", ""),
            "descripcion_original": raw,
            "descripcion_normalizada": norm,
            "cantidad": line.get("cantidad", 0),
            "precio_unitario": line.get("precio_unitario", 0),
        })
    catalogo_json = json.dumps(odoo_products, ensure_ascii=False, indent=2)
    lineas_json = json.dumps(raw_items, ensure_ascii=False, indent=2)

    return f"""Eres un auxiliar contable experto en facturación electrónica colombiana.
Tu tarea es mapear cada línea de una factura electrónica al producto más similar
en el catálogo de Odoo.

Reglas obligatorias:
1. Para cada línea, analiza la 'descripcion_original' y la 'descripcion_normalizada'.
2. Ignora colores, capacidades técnicas (GB, TB, MHZ), conectividad (WIFI, SSD, HDD),
   y palabras irrelevantes (N/A, UND, UNIDAD, COLOR, NEGRO, etc.).
3. Enfócate en la MARCA y el NOMBRE PRINCIPAL del producto.
   Ej: "CELULAR INFINIX N/A SMART 20 4/128GB N/A" → el producto es "CELULAR INFINIX" o similar.
4. Siempre elige el producto más cercano del catálogo. Si hay duda, elige el de marca similar.
5. Solo usa producto_id = null si realmente ningún producto del catálogo se relaciona.
6. Respeta cantidades y precios originales (NO los modifiques).
7. Responde ÚNICAMENTE con un array JSON válido. Sin markdown, sin texto adicional.

Formato de respuesta:
[
  {{
    "numero_linea": "1",
    "descripcion_original": "texto original de la factura",
    "producto_id": 123,
    "producto_nombre": "Nombre exacto en Odoo",
    "codigo_producto": "REF-001",
    "cantidad": 10.0,
    "precio_unitario": 15000.0,
    "confianza": 0.95,
    "razon": "Coincidencia por marca y nombre principal: CELULAR INFINIX"
  }}
]

Líneas de la factura (con descripción normalizada):
{lineas_json}

Catálogo de productos Odoo (pre-seleccionados como candidatos):
{catalogo_json}"""


def _lines_hash(line_items: list, odoo_products: list) -> str:
    raw = json.dumps(line_items, sort_keys=True, ensure_ascii=False)
    cat = json.dumps(odoo_products, sort_keys=True, ensure_ascii=False)
    combined = f"{raw}|{cat}|v{ALGORITHM_VERSION}"
    return hashlib.sha256(combined.encode()).hexdigest()


def _check_cache(cursor, line_hash: str) -> list:
    cursor.execute(
        "SELECT resultado_json FROM product_mapping_cache WHERE line_hash = %s",
        (line_hash,)
    )
    row = cursor.fetchone()
    if row:
        try:
            raw = row["resultado_json"]
            return json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _save_cache(cursor, line_hash: str, line_items: list, odoo_products: list, resultado: list):
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


def _run_fuzzy_fallback(line_items: list, odoo_products: list) -> list:
    resultado = []
    for i, line in enumerate(line_items):
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        match = _fuzzy_match_line(raw_desc, norm_desc, odoo_products)
        razon = (
            f"Fuzzy match local: {match['confianza']*100:.0f}% con '{match['producto_nombre']}'"
            if match["confianza"] > 0
            else "Sin coincidencia en catálogo Odoo"
        )
        print(f"[purchase_parser] FUZZY | orig='{raw_desc}' norm='{norm_desc}' → pid={match['producto_id']} conf={match['confianza']}")
        resultado.append({
            "numero_linea": line.get("numero", str(i)),
            "descripcion_original": raw_desc,
            "producto_id": match["producto_id"],
            "producto_nombre": match["producto_nombre"],
            "codigo_producto": line.get("codigo_producto", ""),
            "cantidad": line.get("cantidad", 0),
            "precio_unitario": line.get("precio_unitario", 0),
            "confianza": match["confianza"],
            "razon": razon,
        })
    return resultado


def map_lines_to_odoo(
    line_items: list,
    odoo_products: list,
    groq_api_key: str,
    db_cursor=None,
) -> list:
    if not line_items:
        return []

    if not odoo_products:
        print("[purchase_parser] Catálogo Odoo vacío — retornando líneas sin mapeo")
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
                "razon": "Catálogo Odoo vacío",
            }
            for i, it in enumerate(line_items)
        ]

    line_hash = _lines_hash(line_items, odoo_products)

    if db_cursor:
        cached = _check_cache(db_cursor, line_hash)
        if cached is not None:
            print(f"[purchase_parser] CACHE HIT v{ALGORITHM_VERSION} — {len(cached)} líneas")
            return cached

    print(f"[purchase_parser] v{ALGORITHM_VERSION} | {len(line_items)} líneas, {len(odoo_products)} productos en catálogo")

    for line in line_items:
        raw = line.get("descripcion_original") or line.get("descripcion", "")
        norm = _normalize_desc(raw)
        print(f"[purchase_parser] LINE orig='{raw}' norm='{norm}'")

    top_candidates = _select_top_candidates(line_items, odoo_products)

    for line in line_items:
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        fuzzy_pre = _fuzzy_match_line(raw_desc, norm_desc, top_candidates)
        print(f"[purchase_parser] PRE-FUZZY '{raw_desc}' → pid={fuzzy_pre['producto_id']} conf={fuzzy_pre['confianza']}")

    prompt = _build_prompt(line_items, top_candidates)

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
        print(f"[purchase_parser] GROQ respuesta cruda: {content[:500]}...")

        content = content.replace("```json", "").replace("```", "").strip()
        resultado = json.loads(content)
        if not isinstance(resultado, list):
            resultado = []

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
            print(f"[purchase_parser] GROQ → línea={r['numero_linea']} pid={r['producto_id']} conf={r['confianza']}")

        for r in resultado:
            if r.get("producto_id") is None:
                raw_desc = r.get("descripcion_original", "")
                norm_desc = _normalize_desc(raw_desc)
                fuzzy = _fuzzy_match_line(raw_desc, norm_desc, top_candidates)
                if fuzzy["producto_id"]:
                    r["producto_id"] = fuzzy["producto_id"]
                    r["producto_nombre"] = fuzzy["producto_nombre"]
                    r["confianza"] = fuzzy["confianza"]
                    r["razon"] = f"Groq sin match → Fuzzy: {fuzzy['confianza']*100:.0f}% con '{fuzzy['producto_nombre']}'"
                    print(f"[purchase_parser] POST-FUZZY línea={r['numero_linea']} → pid={fuzzy['producto_id']} conf={fuzzy['confianza']}")

        if db_cursor:
            _save_cache(db_cursor, line_hash, line_items, odoo_products, resultado)

        return resultado

    except Exception as e:
        print(f"[purchase_parser] Error llamando a Groq: {e}")
        return _run_fuzzy_fallback(line_items, top_candidates)
