"""
purchase_parser.py — Mapeo IA de líneas de factura a productos Odoo.

Flujo v4 (corregido):
  1. Lookup correcciones humanas previas (correction_history)
  2. Normaliza descripciones de líneas
  3. Pre-filtra catálogo Odoo a top 15 candidatos por línea vía fuzzy
  4. Envía solo candidatos a Groq con prompt semántico mejorado
  5. Groq devuelve JSON con producto_id, nombre, confianza
  6. Valida confianza (>=0.85 auto, 0.60-0.84 revisión, <0.60 -> null)
  7. Post-procesa líneas sin match con fuzzy local (umbral >= 0.60)
  8. Cachea con hash que incluye lineas + catalogo + version del algoritmo
  9. Reintentos exponenciales en Groq + parseo JSON robusto
"""

import json
import hashlib
import httpx
import re
import time

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
ALGORITHM_VERSION = "4"

# Thresholds de confianza
CONFIDENCE_AUTO = 0.85
CONFIDENCE_REVIEW = 0.60

# Reintentos Groq
GROQ_MAX_RETRIES = 3
GROQ_TIMEOUT = 60
GROQ_RETRY_DELAYS = [1, 2, 4]

# Pre-filtro
TOP_CANDIDATES_PER_LINE = 15
MAX_CANDIDATES_TOTAL = 30

_STOP_WORDS = {
    "N/A", "UND", "UNIDAD", "UNIDADES", "COLOR", "NEGRO", "BLANCO",
    "ROJO", "AZUL", "VERDE", "AMARILLO", "GRIS", "PLATEADO", "DORADO",
    "WIFI", "SSD", "HDD", "GB", "TB", "MHZ", "GHZ", "HZ",
    "NADA", "SIN", "NO", "APLICA", "VARIOS", "DIVERSOS", "GENERICO",
    "GENERICO", "STANDARD", "ESTANDAR", "BASICO", "BASIC", "PRO",
    "LITE", "NUEVO", "NEW", "ORIGINAL", "REACONDICIONADO",
    "CABLE", "ADAPTADOR", "CARGADOR", "MANUAL", "GUIA", "GARANTIA",
    "INCLUYE", "INCLUIDO", "MAS", "PLUS", "KIT", "SET", "PAQUETE",
}

_CAPACITY_RE = re.compile(r'\b\d+\s*(?:GB|TB|MHZ|GHZ)\b', re.IGNORECASE)
_FRACTION_CAPACITY_RE = re.compile(r'\b\d+/\d+\s*(?:GB|TB)\b', re.IGNORECASE)
_TECH_SPEC_RE = re.compile(r'\b\d+x\d+\b', re.IGNORECASE)
_MODEL_SUFFIX_RE = re.compile(r'\b(?:N[/-]A|N/A|NA)\b', re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r'\s+')


def _normalize_desc(raw: str) -> str:
    if not raw:
        return ""
    s = raw.upper().strip()
    s = _MODEL_SUFFIX_RE.sub(" ", s)
    s = _FRACTION_CAPACITY_RE.sub(" ", s)
    s = _CAPACITY_RE.sub(" ", s)
    s = _TECH_SPEC_RE.sub(" ", s)
    s = re.sub(r'[^\w\s]', ' ', s)
    words = _MULTI_SPACE_RE.split(s)
    cleaned = []
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w in _STOP_WORDS:
            continue
        if re.match(r'^\d$', w):
            continue
        cleaned.append(w)
    return " ".join(cleaned)


def _extract_brand_tokens(desc: str) -> set:
    tokens = set()
    for w in desc.upper().split():
        wc = re.sub(r'[^a-záéíóúñA-ZÁÉÍÓÚÑ0-9]', '', w)
        if len(wc) >= 3 and not wc.isdigit():
            tokens.add(wc.lower())
    return tokens


def _score_product_for_line(norm_desc: str, brand_tokens: set, p: dict) -> float:
    pname = (p.get("name") or "").strip()
    pcode = (p.get("default_code") or "").strip()
    pname_norm = _normalize_desc(pname)
    pcode_norm = _normalize_desc(pcode)
    
    if not pname_norm and not pcode_norm:
        return 0.0
        
    try:
        from thefuzz import fuzz
    except ImportError:
        return 0.0
        
    score_name_ts = fuzz.token_set_ratio(norm_desc, pname_norm) if pname_norm else 0
    score_name_partial = fuzz.partial_ratio(norm_desc, pname_norm) if pname_norm else 0
    score_name_full = fuzz.ratio(norm_desc, pname_norm) if pname_norm else 0
    score_code = fuzz.partial_ratio(norm_desc, pcode_norm) if pcode_norm else 0
    
    score = max(score_name_ts, score_name_partial, score_name_full, score_code)
    
    if brand_tokens:
        pbrand = _extract_brand_tokens(pname)
        overlap = brand_tokens & pbrand
        if overlap:
            score = max(score, min(95.0, score + 25.0))
            
    if pcode_norm and pcode_norm in norm_desc:
        score = max(score, 90.0)
    if pname_norm and pname_norm in norm_desc:
        score = max(score, 92.0)
        
    return score


def _get_top_candidates_for_line(
    line_desc: str,
    odoo_products: list,
    limit_n: int = 15
) -> list:
    norm_desc = _normalize_desc(line_desc)
    if not norm_desc:
        return odoo_products[:limit_n]
        
    brand_tokens = _extract_brand_tokens(line_desc)
    desc_words = set(norm_desc.lower().split())
    
    # Etapa 1: Filtrado rápido por palabras clave si el catálogo es grande
    if len(odoo_products) > 200:
        candidates_stage1 = []
        for p in odoo_products:
            pname = (p.get("name") or "").lower()
            pcode = (p.get("default_code") or "").lower()
            
            overlap = sum(1 for w in desc_words if w in pname or w in pcode)
            if overlap > 0:
                candidates_stage1.append((p, overlap))
                
        candidates_stage1.sort(key=lambda x: x[1], reverse=True)
        filtered_products = [x[0] for x in candidates_stage1[:100]]
        
        if len(filtered_products) < 50:
            added_ids = {p["id"] for p in filtered_products}
            for p in odoo_products:
                if p["id"] not in added_ids:
                    filtered_products.append(p)
                    if len(filtered_products) >= 100:
                        break
    else:
        filtered_products = odoo_products
        
    # Etapa 2: Scoring detallado con fuzzy matching y re-ranking
    scored = []
    for p in filtered_products:
        score = _score_product_for_line(norm_desc, brand_tokens, p)
        scored.append((p, score))
        
    scored.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in scored[:limit_n]]


def _fuzzy_match_line(
    raw_desc: str,
    norm_desc: str,
    odoo_products: list,
    threshold: int = 60,
) -> dict:
    if not norm_desc or not odoo_products:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}

    candidates = _get_top_candidates_for_line(raw_desc, odoo_products, limit_n=1)
    if not candidates:
        return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}
        
    p = candidates[0]
    brand_tokens = _extract_brand_tokens(raw_desc)
    score = _score_product_for_line(norm_desc, brand_tokens, p)
    
    if score >= threshold:
        return {
            "producto_id": p["id"],
            "producto_nombre": p.get("name") or "",
            "confianza": round(min(score / 100.0, 1.0), 2),
        }
        
    return {"producto_id": None, "producto_nombre": None, "confianza": 0.0}


def _select_candidates_per_line(
    line_items: list,
    odoo_products: list,
    top_n: int = TOP_CANDIDATES_PER_LINE,
    max_total: int = MAX_CANDIDATES_TOTAL,
) -> list:
    """
    Retorna la unión de los top_n candidatos para cada línea del documento.
    """
    if not odoo_products:
        return []
        
    all_selected = {}
    for line in line_items:
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        candidates = _get_top_candidates_for_line(raw_desc, odoo_products, limit_n=top_n)
        for p in candidates:
            pid = p["id"]
            if pid not in all_selected:
                all_selected[pid] = p
                
    return list(all_selected.values())


def _build_prompt(line_items: list, odoo_products: list) -> str:
    raw_items = []
    for line in line_items:
        raw = line.get("descripcion_original") or line.get("descripcion", "")
        norm = _normalize_desc(raw)
        
        # Obtener candidatos individuales para esta línea específica
        line_candidates = _get_top_candidates_for_line(raw, odoo_products, limit_n=15)
        light_candidates = [
            {
                "id": p["id"],
                "name": p.get("name", ""),
                "default_code": p.get("default_code") or ""
            }
            for p in line_candidates
        ]
        
        raw_items.append({
            "numero": line.get("numero", ""),
            "descripcion_original": raw,
            "descripcion_normalizada": norm,
            "cantidad": line.get("cantidad", 0),
            "precio_unitario": line.get("precio_unitario", 0),
            "candidatos_odoo": light_candidates
        })
        
    lineas_json = json.dumps(raw_items, ensure_ascii=False, indent=2)

    return f"""Eres un auxiliar contable experto en facturación electrónica colombiana.
Tu tarea es mapear cada línea de una factura electrónica al producto más similar en su lista de 'candidatos_odoo'.

Reglas obligatorias:
1. Para cada línea, analiza la 'descripcion_original', 'descripcion_normalizada' y compárala con los elementos en 'candidatos_odoo'.
2. Selecciona el 'id' de la lista 'candidatos_odoo' que tenga mejor correspondencia.
3. Ignora colores, capacidades técnicas (GB, TB, MHZ, GHZ), conectividad (WIFI, SSD, HDD) y palabras irrelevantes (N/A, UND, UNIDAD, COLOR, NEGRO, etc.).
4. Enfócate en la MARCA y el NOMBRE PRINCIPAL del producto.
5. Asigna producto_id = null si la confianza es baja (< 60%) o si ninguno de los 'candidatos_odoo' es una coincidencia correcta. NO fuerces matches incorrectos.
6. Incluye un score de confianza REALISTA y estricto (0.0 a 1.0):
   - 0.90-1.00: Coincidencia exacta de marca y nombre principal
   - 0.70-0.89: Coincidencia de marca pero con dudas en el modelo o variaciones menores
   - 0.60-0.69: Coincidencia parcial dudosa (requiere revisión)
   - 0.00-0.59: Baja similitud o sin match correcto. Asigna producto_id = null.
7. Respeta cantidades y precios originales (NO los modifiques).
8. Responde ÚNICAMENTE con un array JSON válido. Sin markdown, sin texto adicional.
9. Si ninguna línea tiene match, devuelve [] vacío.

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
    "razon": "Coincidencia exacta por marca y nombre: CELULAR INFINIX"
  }}
]

Líneas de la factura (cada una con su lista de candidatos pre-seleccionados):
{lineas_json}"""


def _lines_hash(line_items: list, odoo_products: list) -> str:
    raw = json.dumps(line_items, sort_keys=True, ensure_ascii=False)
    cat = json.dumps(odoo_products, sort_keys=True, ensure_ascii=False)
    combined = f"{raw}|{cat}|v{ALGORITHM_VERSION}"
    return hashlib.sha256(combined.encode()).hexdigest()


def _lines_hash_light(line_items: list) -> str:
    raw = json.dumps(line_items, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _check_cache(cursor, line_hash: str) -> list:
    cursor.execute(
        "SELECT resultado_json FROM product_mapping_cache WHERE line_hash = %s",
        (line_hash,)
    )
    row = cursor.fetchone()
    if row:
        try:
            raw = row["resultado_json"] if isinstance(row, dict) else row[0]
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


def _balance_json_brackets(s: str) -> str:
    """
    Intenta cerrar corchetes y llaves abiertos si el JSON fue truncado.
    """
    stack = []
    in_string = False
    escaped = False
    
    clean_s = []
    for i, char in enumerate(s):
        if escaped:
            escaped = False
            clean_s.append(char)
            continue
        if char == '\\':
            escaped = True
            clean_s.append(char)
            continue
        if char == '"':
            in_string = not in_string
            clean_s.append(char)
            continue
        
        if not in_string:
            if char in ('{', '['):
                stack.append(char)
            elif char in ('}', ']'):
                if not stack:
                    continue
                top = stack[-1]
                if (char == '}' and top == '{') or (char == ']' and top == '['):
                    stack.pop()
        clean_s.append(char)
        
    s = "".join(clean_s)
    
    if in_string:
        s += '"'
        
    while stack:
        top = stack.pop()
        if top == '{':
            s += '}'
        elif top == '[':
            s += ']'
            
    return s


def _repair_json(raw: str) -> str:
    s = raw.strip()
    # Limpiar formato de bloques de código markdown
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    s = s.strip()
    
    # Reemplazar valores no válidos en JSON (estilo Python/Javascript)
    s = s.replace('True', 'true').replace('False', 'false').replace('None', 'null')
    
    # Eliminar comas finales superfluas antes de llaves o corchetes de cierre
    s = re.sub(r',\s*([\]}])', r'\1', s)
    s = re.sub(r'([{\[,])\s*([}\]])', r'\1\2', s)
    
    # Balancear llaves y corchetes abiertos en caso de truncado
    s = _balance_json_brackets(s)
    return s.strip()


def _call_groq(prompt: str, api_key: str) -> str:
    response = httpx.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
            "temperature": 0.1,
        },
        timeout=GROQ_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _call_groq_with_retry(prompt: str, api_key: str) -> list:
    last_error = None
    base_delay = 2.0
    max_retries = 5
    
    for attempt in range(max_retries):
        try:
            content = _call_groq(prompt, api_key)
            content = _repair_json(content)
            resultado = json.loads(content)
            if not isinstance(resultado, list):
                raise ValueError(f"Se esperaba lista, se obtuvo {type(resultado).__name__}")
            print(f"[purchase_parser] GROQ intento {attempt+1} OK - {len(resultado)} lineas")
            return resultado
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            last_error = f"Parse error: {e}"
            print(f"[purchase_parser] Intento {attempt+1}/{max_retries}: {last_error}")
        except httpx.TimeoutException as e:
            last_error = f"Timeout: {e}"
            print(f"[purchase_parser] Intento {attempt+1}/{max_retries}: {last_error}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {e.response.status_code}: {e.response.text}"
                print(f"[purchase_parser] Intento {attempt+1}/{max_retries}: {last_error}")
            else:
                raise e
        except Exception as e:
            last_error = f"Unexpected: {e}"
            print(f"[purchase_parser] Intento {attempt+1}/{max_retries}: {last_error}")
            
        if attempt < max_retries - 1:
            import random
            delay = (base_delay * (2 ** attempt)) + random.uniform(0.1, 1.0)
            print(f"[purchase_parser] Esperando {delay:.2f} segundos antes del reintento...")
            time.sleep(delay)

    raise RuntimeError(f"Groq fallo tras {max_retries} intentos: {last_error}")


def _lookup_corrections_bulk(norms: list, cursor) -> dict:
    if not norms or not cursor:
        return {}
    try:
        placeholders = ", ".join(["%s"] * len(norms))
        query = f"""
            SELECT DISTINCT ON (normalized_desc) normalized_desc, producto_id, producto_nombre
            FROM correction_history
            WHERE normalized_desc IN ({placeholders})
            ORDER BY normalized_desc, created_at DESC
        """
        cursor.execute(query, tuple(norms))
        rows = cursor.fetchall()
        
        corrections = {}
        for row in rows:
            if isinstance(row, dict):
                norm = row["normalized_desc"]
                pid = row["producto_id"]
                pname = row["producto_nombre"]
            else:
                norm = row[0]
                pid = row[1]
                pname = row[2]
                
            if pid:
                corrections[norm] = {
                    "producto_id": pid,
                    "producto_nombre": pname,
                    "confianza": 1.0,
                    "razon": "Basado en correccion humana previa"
                }
        return corrections
    except Exception as e:
        print(f"[purchase_parser] Error en consulta bulk de correcciones: {e}")
        return {}


def _validate_confidence(resultado: list) -> list:
    for r in resultado:
        conf = r.get("confianza", 0.0) or 0.0
        pid = r.get("producto_id")
        if pid is None:
            if not r.get("razon"):
                r["razon"] = "Sin producto asignado"
            continue
        if conf < CONFIDENCE_REVIEW:
            r["producto_id"] = None
            r["producto_nombre"] = None
            r["confianza"] = conf
            r["razon"] = f"Confianza baja ({conf:.0%}). Se requiere revision manual."
        elif conf < CONFIDENCE_AUTO:
            r["razon"] = f"Confianza media ({conf:.0%}). Se recomienda revision."
        else:
            r["razon"] = r.get("razon") or f"Confianza alta ({conf:.0%})."
    return resultado


def _save_corrections_to_history(cursor, manual_lines: list):
    if not cursor or not manual_lines:
        return
    try:
        saved = 0
        for line in manual_lines:
            raw = line.get("descripcion_original") or line.get("descripcion", "")
            norm = _normalize_desc(raw)
            pid = line.get("producto_id")
            pname = line.get("producto_nombre", "")
            if norm and pid:
                # Verificar si ya existe registro para esta descripción normalizada
                cursor.execute(
                    "SELECT id FROM correction_history WHERE normalized_desc = %s",
                    (norm,)
                )
                row = cursor.fetchone()
                if row:
                    row_id = row["id"] if isinstance(row, dict) else row[0]
                    cursor.execute("""
                        UPDATE correction_history
                        SET producto_id = %s, producto_nombre = %s, created_at = NOW()
                        WHERE id = %s
                    """, (pid, pname, row_id))
                else:
                    cursor.execute("""
                        INSERT INTO correction_history (normalized_desc, producto_id, producto_nombre, created_at)
                        VALUES (%s, %s, %s, NOW())
                    """, (norm, pid, pname))
                saved += 1
        cursor.connection.commit()
        print(f"[purchase_parser] {saved} correccion(es) guardada(s)/actualizada(s) en history")
    except Exception as e:
        print(f"[purchase_parser] Error guardando historial: {e}")
        cursor.connection.rollback()


def _run_fuzzy_fallback(line_items: list, odoo_products: list) -> list:
    resultado = []
    for i, line in enumerate(line_items):
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        match = _fuzzy_match_line(raw_desc, norm_desc, odoo_products, threshold=60)
        razon = (
            f"Fuzzy match local: {match['confianza']*100:.0f}% con '{match['producto_nombre']}'"
            if match["confianza"] > 0
            else "Sin coincidencia en catalogo Odoo"
        )
        print(f"[purchase_parser] FUZZY | orig='{raw_desc}' norm='{norm_desc}' -> pid={match['producto_id']} conf={match['confianza']}")
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
        print("[purchase_parser] Catalogo Odoo vacio — retornando lineas sin mapeo")
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
                "razon": "Catalogo Odoo vacio",
            }
            for i, it in enumerate(line_items)
        ]

    line_hash = _lines_hash(line_items, odoo_products)
    print(f"[purchase_parser] v{ALGORITHM_VERSION} | {len(line_items)} lineas, {len(odoo_products)} productos en catalogo")

    for line in line_items:
        raw = line.get("descripcion_original") or line.get("descripcion", "")
        norm = _normalize_desc(raw)
        print(f"[purchase_parser] LINE orig='{raw}' norm='{norm}'")

    # 1. Intentar cache (solo si hay db_cursor y no hay correcciones pendientes)
    cache_hit = False
    resultado_cache = None
    if db_cursor:
        cached = _check_cache(db_cursor, line_hash)
        if cached is not None:
            resultado_cache = cached
            cache_hit = True
            print(f"[purchase_parser] CACHE HIT v{ALGORITHM_VERSION} — {len(cached)} lineas")

    # 2. Obtener correcciones humanas previas (consulta bulk)
    corrections = {}
    if db_cursor:
        norms = []
        for line in line_items:
            raw = line.get("descripcion_original") or line.get("descripcion", "")
            norm = _normalize_desc(raw)
            if norm:
                norms.append(norm)
        if norms:
            corrections = _lookup_corrections_bulk(norms, db_cursor)

    # 3. Si hay cache y no hay correcciones, aplicar validacion y retornar
    if cache_hit and not corrections:
        return _validate_confidence(resultado_cache)

    # 4. Cache miss o hay correcciones: ejecutar mapeo completo
    top_candidates = _select_candidates_per_line(line_items, odoo_products)

    for line in line_items:
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        fuzzy_pre = _fuzzy_match_line(raw_desc, norm_desc, top_candidates)
        print(f"[purchase_parser] PRE-FUZZY '{raw_desc}' -> pid={fuzzy_pre['producto_id']} conf={fuzzy_pre['confianza']}")

    # 5. Separar lineas con correccion vs sin correccion
    resultado = []
    lines_for_groq = []

    for line in line_items:
        raw = line.get("descripcion_original") or line.get("descripcion", "")
        norm = _normalize_desc(raw)
        if norm in corrections:
            c = corrections[norm]
            resultado.append({
                "numero_linea": line.get("numero", str(len(resultado))),
                "descripcion_original": raw,
                "producto_id": c["producto_id"],
                "producto_nombre": c["producto_nombre"],
                "codigo_producto": line.get("codigo_producto", ""),
                "cantidad": line.get("cantidad", 0),
                "precio_unitario": line.get("precio_unitario", 0),
                "confianza": c["confianza"],
                "razon": c["razon"],
            })
        else:
            lines_for_groq.append(line)

    # 6. Enviar a Groq solo las lineas sin correccion
    if lines_for_groq:
        try:
            prompt = _build_prompt(lines_for_groq, odoo_products)
            groq_result = _call_groq_with_retry(prompt, groq_api_key)

            for r in groq_result:
                r.setdefault("numero_linea", "")
                r.setdefault("descripcion_original", "")
                r.setdefault("producto_id", None)
                r.setdefault("producto_nombre", None)
                r.setdefault("codigo_producto", "")
                r.setdefault("cantidad", 0)
                r.setdefault("precio_unitario", 0)
                r.setdefault("confianza", 0.0)
                r.setdefault("razon", "")

            # Post-procesar lineas que Groq dejo sin match
            for r in groq_result:
                if r.get("producto_id") is None:
                    raw_desc = r.get("descripcion_original", "")
                    norm_desc = _normalize_desc(raw_desc)
                    fuzzy = _fuzzy_match_line(raw_desc, norm_desc, top_candidates, threshold=60)
                    if fuzzy["producto_id"]:
                        r["producto_id"] = fuzzy["producto_id"]
                        r["producto_nombre"] = fuzzy["producto_nombre"]
                        r["confianza"] = fuzzy["confianza"]
                        r["razon"] = f"Groq sin match -> Fuzzy: {fuzzy['confianza']*100:.0f}%"

            resultado.extend(groq_result)

            # Guardar en cache solo si NO hay correcciones (la cache no depende de correction_history)
            if db_cursor and not corrections:
                _save_cache(db_cursor, line_hash, line_items, odoo_products, resultado)

        except Exception as e:
            print(f"[purchase_parser] Groq fallo: {e}")
            fallback = _run_fuzzy_fallback(lines_for_groq, top_candidates)
            resultado.extend(fallback)

    # 7. Validar thresholds de confianza
    resultado = _validate_confidence(resultado)

    return resultado
