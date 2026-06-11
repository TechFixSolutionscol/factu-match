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


def _fuzzy_match_line(
    raw_desc: str,
    norm_desc: str,
    odoo_products: list,
    threshold: int = 60,
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

        score_name_ts = fuzz.token_set_ratio(norm_desc, pname_norm) if pname_norm else 0
        score_name_partial = fuzz.partial_ratio(norm_desc, pname_norm) if pname_norm else 0
        score_name_full = fuzz.ratio(norm_desc, pname_norm) if pname_norm else 0
        score_code = fuzz.partial_ratio(norm_desc, pcode_norm) if pcode_norm else 0

        score = max(score_name_ts, score_name_partial, score_name_full, score_code)

        if brand_tokens:
            pbrand = _extract_brand_tokens(pname)
            overlap = brand_tokens & pbrand
            if overlap:
                score = max(score, min(95, score + 25))

        if pcode_norm and pcode_norm in norm_desc:
            score = max(score, 90)
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


def _select_candidates_per_line(
    line_items: list,
    odoo_products: list,
    top_n: int = TOP_CANDIDATES_PER_LINE,
    max_total: int = MAX_CANDIDATES_TOTAL,
) -> list:
    if not odoo_products:
        return []

    try:
        from thefuzz import fuzz
    except ImportError:
        return odoo_products[:max_total]

    all_candidates = {}
    for line in line_items:
        raw_desc = line.get("descripcion_original") or line.get("descripcion", "")
        norm_desc = _normalize_desc(raw_desc)
        if not norm_desc:
            continue
        brand_tokens = _extract_brand_tokens(raw_desc)
        scored = []
        for p in odoo_products:
            pid = p["id"]
            pname_norm = _normalize_desc(p.get("name") or "")
            pcode = p.get("default_code") or ""
            if not pname_norm and not pcode:
                continue
            s = fuzz.token_set_ratio(norm_desc, pname_norm) if pname_norm else 0
            s = max(s, fuzz.partial_ratio(norm_desc, pname_norm) if pname_norm else 0)
            if brand_tokens:
                pbrand = _extract_brand_tokens(p.get("name") or "")
                if brand_tokens & pbrand:
                    s = max(s, min(95, s + 25))
            if pcode and pcode in norm_desc:
                s = max(s, 90)
            scored.append((pid, s))

        scored.sort(key=lambda x: x[1], reverse=True)
        for pid, score in scored[:top_n]:
            if pid not in all_candidates or score > all_candidates[pid]:
                all_candidates[pid] = score

    sorted_pids = sorted(all_candidates, key=lambda pid: all_candidates[pid], reverse=True)
    top_pids = set(sorted_pids[:max_total])

    result = [p for p in odoo_products if p["id"] in top_pids]
    print(f"[purchase_parser] Catalogo completo: {len(odoo_products)} -> pre-seleccionados: {len(result)}")
    return result if result else odoo_products[:max_total]


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

    return f"""Eres un auxiliar contable experto en facturacion electronica colombiana.
Tu tarea es mapear cada linea de una factura electronica al producto mas similar
en el catalogo de Odoo.

Reglas obligatorias:
1. Para cada linea, analiza la 'descripcion_original' y la 'descripcion_normalizada'.
2. Ignora colores, capacidades tecnicas (GB, TB, MHZ), conectividad (WIFI, SSD, HDD),
   y palabras irrelevantes (N/A, UND, UNIDAD, COLOR, NEGRO, etc.).
3. Enfocate en la MARCA y el NOMBRE PRINCIPAL del producto.
   Ej: "CELULAR INFINIX N/A SMART 20 4/128GB N/A" -> el producto es "CELULAR INFINIX" o similar.
4. Asigna producto_id = null si la confianza es baja (< 60%). NO fuerces matches incorrectos.
5. Incluye un score de confianza REALISTA (0.0 a 1.0):
   - 0.90-1.00: Coincidencia exacta de marca y modelo
   - 0.70-0.89: Coincidencia de marca pero con dudas en el modelo exacto
   - 0.50-0.69: Coincidencia parcial, algunas palabras clave coinciden
   - 0.00-0.49: Baja similitud, asigna producto_id = null
6. Respeta cantidades y precios originales (NO los modifiques).
7. Responde UNICAMENTE con un array JSON valido. Sin markdown, sin texto adicional.
8. Si ninguna linea tiene match, devuelve [] vacio.

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
    "razon": "Coincidencia por marca y nombre: CELULAR INFINIX SMART 20"
  }}
]

Lineas de la factura (con descripcion normalizada):
{lineas_json}

Catalogo de productos Odoo (pre-seleccionados como candidatos):
{catalogo_json}"""


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


def _repair_json(raw: str) -> str:
    s = raw.strip()
    s = re.sub(r'```(?:json)?\s*', '', s)
    s = s.replace('True', 'true').replace('False', 'false').replace('None', 'null')
    s = re.sub(r',\s*([\]}])', r'\1', s)
    s = re.sub(r'([{\[,])\s*([}\]])', r'\1\2', s)
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
    for attempt in range(GROQ_MAX_RETRIES):
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
            print(f"[purchase_parser] Intento {attempt+1}/{GROQ_MAX_RETRIES}: {last_error}")
            if attempt < GROQ_MAX_RETRIES - 1:
                delay = GROQ_RETRY_DELAYS[min(attempt, len(GROQ_RETRY_DELAYS) - 1)]
                time.sleep(delay)
        except httpx.TimeoutException as e:
            last_error = f"Timeout: {e}"
            print(f"[purchase_parser] Intento {attempt+1}/{GROQ_MAX_RETRIES}: {last_error}")
            if attempt < GROQ_MAX_RETRIES - 1:
                delay = GROQ_RETRY_DELAYS[min(attempt, len(GROQ_RETRY_DELAYS) - 1)]
                time.sleep(delay)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503):
                last_error = f"HTTP {e.response.status_code}"
                print(f"[purchase_parser] Intento {attempt+1}/{GROQ_MAX_RETRIES}: {last_error}")
                if attempt < GROQ_MAX_RETRIES - 1:
                    delay = GROQ_RETRY_DELAYS[min(attempt, len(GROQ_RETRY_DELAYS) - 1)]
                    time.sleep(delay)
            else:
                raise

    raise RuntimeError(f"Groq fallo tras {GROQ_MAX_RETRIES} intentos: {last_error}")


def _lookup_correction_history(norm_desc: str, cursor) -> dict:
    if not norm_desc or not cursor:
        return None
    try:
        cursor.execute(
            "SELECT producto_id, producto_nombre FROM correction_history WHERE normalized_desc = %s ORDER BY created_at DESC LIMIT 1",
            (norm_desc,)
        )
        row = cursor.fetchone()
        if row:
            row_dict = row if isinstance(row, dict) else {"producto_id": row[0], "producto_nombre": row[1]}
            if row_dict.get("producto_id"):
                return {
                    "producto_id": row_dict["producto_id"],
                    "producto_nombre": row_dict.get("producto_nombre", ""),
                    "confianza": 0.90,
                    "razon": "Basado en correccion humana previa",
                }
    except Exception:
        pass
    return None


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
                cursor.execute(
                    "INSERT INTO correction_history (normalized_desc, producto_id, producto_nombre) VALUES (%s, %s, %s)",
                    (norm, pid, pname)
                )
                saved += 1
        cursor.connection.commit()
        print(f"[purchase_parser] {saved} correccion(es) guardada(s) en history")
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

    # 2. Obtener correcciones humanas previas
    corrections = {}
    if db_cursor:
        for line in line_items:
            raw = line.get("descripcion_original") or line.get("descripcion", "")
            norm = _normalize_desc(raw)
            if norm:
                corr = _lookup_correction_history(norm, db_cursor)
                if corr:
                    corrections[norm] = corr

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
            prompt = _build_prompt(lines_for_groq, top_candidates)
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
