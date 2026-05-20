"""
ai_auditor.py - Motor de Auditoría Contable con IA (Groq)

Extrae datos de Odoo y los envía a Groq para análisis de anomalías.
Devuelve una lista de hallazgos con gravedad, título, hallazgo y recomendación.
"""

import httpx
import json
from datetime import datetime
from odoo_match import OdooConnector, _normalizar_nit_odoo


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama3-70b-8192"


def build_odoo_context(connector: OdooConnector, date_from: str, date_to: str) -> dict:
    """
    Extrae de Odoo los datos relevantes para el periodo dado.
    Retorna un dict con proveedores, facturas y pagos.
    """
    models = connector._get_models_proxy()
    uid = connector._uid

    # 1. Facturas de compra del periodo
    invoices = models.execute_kw(
        connector.database, uid, connector.api_key,
        "account.move", "search_read",
        [[
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "=", "posted"),
            ("invoice_date", ">=", date_from),
            ("invoice_date", "<=", date_to),
        ]],
        {
            "fields": [
                "name", "ref", "invoice_date", "partner_id",
                "amount_untaxed", "amount_tax", "amount_total",
                "payment_state", "move_type"
            ],
            "limit": 200,
            "order": "invoice_date desc"
        }
    )

    # 2. Proveedores involucrados - verificar duplicados por NIT
    partner_ids = list(set(
        inv["partner_id"][0] for inv in invoices
        if isinstance(inv.get("partner_id"), list)
    ))

    partners = []
    if partner_ids:
        partners = models.execute_kw(
            connector.database, uid, connector.api_key,
            "res.partner", "read",
            [partner_ids],
            {"fields": ["id", "name", "vat", "supplier_rank"]}
        )

    # Detectar NITs duplicados
    vat_map = {}
    for p in partners:
        vat = _normalizar_nit_odoo(p.get("vat", ""))
        if vat:
            vat_map.setdefault(vat, []).append(p["name"])

    duplicate_vats = {vat: names for vat, names in vat_map.items() if len(names) > 1}

    # 3. Facturas sin impuesto (posible error o exento no documentado)
    zero_tax_invoices = [
        {"nombre": inv["partner_id"][1] if isinstance(inv.get("partner_id"), list) else "N/A",
         "factura": inv.get("ref") or inv.get("name", ""),
         "total": inv["amount_total"]}
        for inv in invoices
        if inv["amount_tax"] == 0 and inv["amount_total"] > 100000
    ]

    # 4. Facturas con tax fuera del rango esperado (ni 0%, ni ~5%, ni ~19%)
    suspicious_tax = []
    for inv in invoices:
        base = inv["amount_untaxed"]
        tax = inv["amount_tax"]
        if base > 0 and tax > 0:
            rate = (tax / base) * 100
            # Si la tasa no es ~0, ~5 o ~19 con tolerancia del 1%
            expected_rates = [0, 5, 19]
            if not any(abs(rate - r) < 1.5 for r in expected_rates):
                suspicious_tax.append({
                    "factura": inv.get("ref") or inv.get("name", ""),
                    "proveedor": inv["partner_id"][1] if isinstance(inv.get("partner_id"), list) else "N/A",
                    "base": round(base, 2),
                    "impuesto": round(tax, 2),
                    "tasa_calculada": round(rate, 2)
                })

    # 5. Facturas sin pagar con más de 60 días
    overdue = [
        {"factura": inv.get("ref") or inv.get("name", ""),
         "proveedor": inv["partner_id"][1] if isinstance(inv.get("partner_id"), list) else "N/A",
         "total": inv["amount_total"],
         "fecha": inv["invoice_date"]}
        for inv in invoices
        if inv.get("payment_state") in ("not_paid", "partial")
        and inv.get("invoice_date")
        and (datetime.now().date() - datetime.strptime(inv["invoice_date"], "%Y-%m-%d").date()).days > 60
    ]

    return {
        "total_facturas": len(invoices),
        "period": f"{date_from} al {date_to}",
        "proveedores_con_nit_duplicado": duplicate_vats,
        "facturas_sin_impuesto_monto_alto": zero_tax_invoices[:10],
        "impuestos_sospechosos": suspicious_tax[:10],
        "facturas_vencidas_60d": overdue[:10],
    }


def run_ai_audit(context: dict, groq_api_key: str) -> list:
    """
    Envía el contexto contable a Groq y obtiene los hallazgos de anomalías.
    Retorna lista de dicts: {titulo, hallazgo, recomendacion, gravedad}
    """

    # Serializar el contexto como texto para el prompt
    context_json = json.dumps(context, ensure_ascii=False, indent=2)

    system_prompt = """Eres un auditor contable experto en normativa tributaria colombiana (NIIF, DIAN, Estatuto Tributario).
Tu tarea es analizar datos de un ERP y producir un reporte de anomalías en formato JSON.
Responde ÚNICAMENTE con un arreglo JSON (sin texto adicional, sin markdown). Cada objeto debe tener exactamente estas propiedades:
{
  "titulo": "Nombre corto del hallazgo (máx 60 caracteres)",
  "hallazgo": "Descripción clara del problema encontrado en el ERP (2-3 oraciones)",
  "recomendacion": "Acción específica que debe tomar el contador (1-2 oraciones)",
  "gravedad": "alta" | "media" | "baja"
}
Si no hay anomalías reales, devuelve un arreglo vacío [].
Sé preciso, menciona valores y nombres concretos de los datos cuando los tengas."""

    user_prompt = f"""Analiza los siguientes datos contables del periodo {context['period']} y genera el reporte de anomalías:

{context_json}

Evalúa principalmente:
1. Terceros con NIT duplicado (riesgo de pagos dobles)
2. Facturas con impuesto sospechoso fuera de las tarifas colombianas (0%, 5%, 19%)
3. Facturas de alto valor sin IVA sin justificación aparente
4. Facturas vencidas hace más de 60 días sin pagar (riesgo de sanción por mora)
5. Cualquier otra irregularidad que veas en los datos

Responde SOLO con el arreglo JSON."""

    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 2000
    }

    response = httpx.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()

    raw_content = response.json()["choices"][0]["message"]["content"].strip()

    # Limpiar si viene con markdown ```json
    if raw_content.startswith("```"):
        raw_content = raw_content.split("```")[1]
        if raw_content.startswith("json"):
            raw_content = raw_content[4:]

    anomalias = json.loads(raw_content.strip())
    return anomalias
