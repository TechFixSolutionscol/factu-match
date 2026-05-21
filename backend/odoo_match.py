"""
Conector Odoo — Extrae facturas de proveedor vía XML-RPC API.

Funciona con Odoo 12+ (Cloud y On-Premise).
Solo requiere permisos de lectura en account.move y res.partner.

Uso:
    connector = OdooConnector(url, db, username, api_key)
    facturas = connector.fetch_invoices(date_from="2025-01-01", date_to="2025-01-31")
"""

import xmlrpc.client
import re
import json
import os
import concurrent.futures
from datetime import date, datetime
from typing import List, Optional, Dict, Any
from cryptography.fernet import Fernet


# ──────────────────────────────────────────────
# EXCEPCIONES
# ──────────────────────────────────────────────

class OdooConnectionError(Exception):
    """No se pudo conectar a Odoo."""
    pass

class OdooAuthError(Exception):
    """Credenciales inválidas."""
    pass

class OdooAccessError(Exception):
    """Sin permisos para leer facturas."""
    pass


# ──────────────────────────────────────────────
# CONECTOR PRINCIPAL
# ──────────────────────────────────────────────

class OdooConnector:
    """
    Conecta a Odoo vía XML-RPC y extrae facturas de proveedor.

    En Odoo, las facturas recibidas de proveedores son:
      - move_type = 'in_invoice'  → Factura de proveedor
      - move_type = 'in_refund'   → Nota crédito de proveedor

    Estas se convierten al mismo formato que leer_siesa() produce,
    para que el motor de comparación existente funcione sin cambios.
    """

    def __init__(self, url: str, database: str, username: str, api_key: str):
        self.url = url.rstrip("/")
        self.database = database
        self.username = username
        self.api_key = api_key
        self._uid = None

    # ── Autenticación ──

    def authenticate(self) -> int:
        """
        Autentica contra Odoo y retorna el UID.
        Lanza OdooAuthError si las credenciales son inválidas.
        """
        try:
            common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                allow_none=True
            )
            uid = common.authenticate(
                self.database, self.username, self.api_key, {}
            )
            if not uid:
                raise OdooAuthError("Credenciales inválidas. Verifique usuario y API key.")
            self._uid = uid
            return uid
        except OdooAuthError:
            raise
        except Exception as e:
            raise OdooConnectionError(f"No se pudo conectar a Odoo: {str(e)}")

    # ── Verificación de conexión ──

    def test_connection(self) -> Dict[str, Any]:
        """
        Prueba la conexión y permisos. Retorna dict con resultado.
        No lanza excepciones — retorna success=True/False.
        """
        try:
            uid = self.authenticate()
            models = self._get_models_proxy()

            # Verificar acceso a facturas
            can_read_invoices = models.execute_kw(
                self.database, uid, self.api_key,
                "account.move", "check_access_rights",
                ["read"], {"raise_exception": False}
            )

            # Verificar acceso a contactos (para obtener NITs)
            can_read_partners = models.execute_kw(
                self.database, uid, self.api_key,
                "res.partner", "check_access_rights",
                ["read"], {"raise_exception": False}
            )

            # Obtener info básica de la instancia
            version_info = self._get_server_version()

            return {
                "success": True,
                "uid": uid,
                "can_read_invoices": can_read_invoices,
                "can_read_partners": can_read_partners,
                "odoo_version": version_info.get("server_version", "desconocida"),
                "message": "Conexión exitosa a Odoo"
            }

        except (OdooAuthError, OdooConnectionError) as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Error inesperado: {str(e)}"}

    # ── Extracción de facturas ──

    def fetch_invoices(
        self,
        date_from: str,
        date_to: str,
        limit: int = 10000,
        executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    ) -> List[Dict[str, Any]]:
        """
        Extrae facturas de proveedor de Odoo y las retorna en formato
        compatible con el motor de comparación existente.

        IMPORTANTE: date_from y date_to son OBLIGATORIOS.
        No se permite extraer facturas sin un rango de fechas definido.
        Esto evita cargar miles de registros históricos accidentalmente.

        Parámetros:
          date_from: Fecha inicio (YYYY-MM-DD) — REQUERIDO
          date_to:   Fecha fin (YYYY-MM-DD) — REQUERIDO
          limit:     Máximo de registros a extraer (safety)
          executor:  ThreadPoolExecutor opcional para batches en paralelo

        Retorna lista de dicts con: nit, nombre, factura_clave,
        factura_original, fecha, monto, tipo_documento
        """
        if not date_from or not date_to:
            raise ValueError(
                "Las fechas (date_from y date_to) son obligatorias. "
                "No se puede extraer facturas de Odoo sin un rango de fechas definido."
            )

        # Validar que date_from <= date_to
        try:
            from_dt = datetime.strptime(date_from, "%Y-%m-%d")
            to_dt = datetime.strptime(date_to, "%Y-%m-%d")
            if from_dt > to_dt:
                raise ValueError(
                    f"La fecha inicio ({date_from}) no puede ser mayor que la fecha fin ({date_to})."
                )
            delta_days = (to_dt - from_dt).days
            if delta_days > 365:
                print(f"⚠️  ADVERTENCIA: Rango de {delta_days} días. Esto puede tardar más de lo esperado.")
        except ValueError as e:
            if "format" in str(e).lower() or "does not match" in str(e).lower():
                raise ValueError(f"Formato de fecha inválido. Use YYYY-MM-DD. Recibido: from={date_from}, to={date_to}")
            raise

        if not self._uid:
            self.authenticate()

        models = self._get_models_proxy()

        # ── 1. Buscar IDs de facturas con rango de fechas obligatorio ──
        domain = [
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "=", "posted"),
            ("invoice_date", ">=", date_from),
            ("invoice_date", "<=", date_to),
        ]

        invoice_ids = models.execute_kw(
            self.database, self._uid, self.api_key,
            "account.move", "search",
            [domain],
            {"limit": limit, "order": "invoice_date desc"}
        )

        if not invoice_ids:
            return []

        # ── 2. Leer datos de facturas en batches (paralelo si hay executor) ──
        BATCH_SIZE = 250
        all_invoices = []

        invoice_batches = [invoice_ids[i:i + BATCH_SIZE] for i in range(0, len(invoice_ids), BATCH_SIZE)]

        if executor:
            futures = [
                executor.submit(self._read_invoice_batch, models, batch_ids)
                for batch_ids in invoice_batches
            ]
            for future in concurrent.futures.as_completed(futures):
                all_invoices.extend(future.result())
        else:
            for batch_ids in invoice_batches:
                all_invoices.extend(self._read_invoice_batch(models, batch_ids))

        # ── 3. Leer datos de proveedores en batches (paralelo si hay executor) ──
        partner_ids = list(set(
            inv["partner_id"][0] for inv in all_invoices
            if isinstance(inv.get("partner_id"), list) and len(inv["partner_id"]) > 0
        ))

        partner_map = {}
        if partner_ids:
            partner_batches = [partner_ids[i:i + BATCH_SIZE] for i in range(0, len(partner_ids), BATCH_SIZE)]
            if executor:
                futures = [
                    executor.submit(self._read_partner_batch, models, batch_ids)
                    for batch_ids in partner_batches
                ]
                for future in concurrent.futures.as_completed(futures):
                    for p in future.result():
                        partner_map[p["id"]] = p
            else:
                for batch_ids in partner_batches:
                    for p in self._read_partner_batch(models, batch_ids):
                        partner_map[p["id"]] = p

        # ── 4. Transformar a formato canónico ──
        facturas = []
        for inv in all_invoices:
            partner_id = inv["partner_id"][0] if isinstance(inv.get("partner_id"), list) else None
            partner = partner_map.get(partner_id, {})

            # NIT: Odoo lo guarda en el campo 'vat'
            nit = _normalizar_nit_odoo(partner.get("vat", ""))
            nombre = partner.get("name", "")
            if not nombre and isinstance(inv.get("partner_id"), list) and len(inv["partner_id"]) > 1:
                nombre = inv["partner_id"][1]

            # Número de factura del proveedor:
            # Cadena de prioridad para localización colombiana Odoo 14-19:
            # 1. l10n_latam_document_number  → número oficial del documento DIAN
            # 2. ref                         → referencia manual ingresada por el usuario
            # 3. name                        → secuencia interna de Odoo (último recurso)
            latam_doc = inv.get("l10n_latam_document_number") or ""
            ref_doc   = inv.get("ref") or ""
            name_doc  = inv.get("name") or ""

            # Usar el primero no vacío de la cadena de prioridad
            factura_original = latam_doc.strip() or ref_doc.strip() or name_doc.strip()
            factura_clave = _normalizar_clave_odoo(factura_original)

            # Fecha
            fecha = inv.get("invoice_date", "") or "Sin fecha"

            # Tipo de documento
            tipo = "Factura" if inv.get("move_type") == "in_invoice" else "Nota Crédito"

            # Monto
            monto = inv.get("amount_total_signed", 0)

            if nit and factura_clave:
                facturas.append({
                    "nit": nit,
                    "nombre": nombre,
                    "factura_clave": factura_clave,
                    "factura_original": factura_original,
                    "fecha": str(fecha),
                    "tipo_documento": tipo,
                    "monto": abs(monto) if monto else 0,
                })

        return facturas

    # ── Helpers internos ──

    def _get_models_proxy(self) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(
            f"{self.url}/xmlrpc/2/object",
            allow_none=True
        )

    def _get_server_version(self) -> dict:
        try:
            common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                allow_none=True
            )
            return common.version()
        except Exception:
            return {}

    def _read_invoice_batch(self, models, batch_ids: list) -> list:
        """Lee un lote de facturas de Odoo (helper para paralelización)."""
        return models.execute_kw(
            self.database, self._uid, self.api_key,
            "account.move", "read",
            [batch_ids],
            {
                "fields": [
                    "name", "invoice_date", "ref", "partner_id",
                    "move_type", "amount_total_signed", "state", "payment_state",
                ]
            }
        )

    def _read_partner_batch(self, models, batch_ids: list) -> list:
        """Lee un lote de proveedores de Odoo (helper para paralelización)."""
        return models.execute_kw(
            self.database, self._uid, self.api_key,
            "res.partner", "read",
            [batch_ids],
            {"fields": ["id", "name", "vat", "l10n_latam_identification_type_id"]}
        )


# ──────────────────────────────────────────────
# FUNCIONES DE NORMALIZACIÓN (compatibles con comparador.py)
# ──────────────────────────────────────────────

def _normalizar_nit_odoo(nit_raw) -> str:
    """
    Normaliza NIT desde Odoo.
    Odoo puede guardar NITs con puntos, guiones, DV, etc.
    Ej: "900.123.456-7" → "9001234567"
    """
    if not nit_raw:
        return ""
    return re.sub(r"[^0-9]", "", str(nit_raw)).strip()


def _normalizar_clave_odoo(factura_str: str) -> str:
    """
    Normaliza número de factura de Odoo al formato del comparador.

    Odoo usa "/" como separador: "FEMQ/00004465"
    Siesa usa "-": "FEMQ-00004465"
    Ambos deben normalizar a: "FEMQ4465"

    Esto reutiliza la misma lógica de normalizar_clave() del comparador.
    """
    if not factura_str:
        return ""

    # Reemplazar / por - para compatibilidad
    factura_str = factura_str.replace("/", "-")

    # Misma lógica que normalizar_clave("", factura_str) en comparador.py
    partes = re.split(r"[-_\s]", factura_str, maxsplit=1)
    if len(partes) == 2:
        prefijo = re.sub(r"[^A-Za-z0-9]", "", partes[0]).upper()
        folio_limpio = re.sub(r"[^0-9]", "", partes[1])
        folio_limpio = str(int(folio_limpio)) if folio_limpio else partes[1]
        return f"{prefijo}{folio_limpio}"
    else:
        # Sin separador — intentar detectar prefijo alfabético
        match = re.match(r"^([A-Za-z]+)(\d+)$", factura_str.strip())
        if match:
            prefijo = match.group(1).upper()
            folio = str(int(match.group(2)))
            return f"{prefijo}{folio}"
        # Solo números
        solo_nums = re.sub(r"[^0-9]", "", factura_str)
        return str(int(solo_nums)) if solo_nums else ""


# ──────────────────────────────────────────────
# GESTIÓN SEGURA DE CREDENCIALES
# ──────────────────────────────────────────────

class CredentialManager:
    """
    Encripta/desencripta credenciales Odoo para almacenamiento seguro.

    Uso:
        manager = CredentialManager()
        encrypted = manager.encrypt({"url": "...", "api_key": "..."})
        decrypted = manager.decrypt(encrypted)
    """

    def __init__(self):
        key = os.environ.get("ENCRYPTION_KEY", "")
        if not key:
            # Generar una key para desarrollo (NO usar en producción)
            key = Fernet.generate_key().decode()
            print("⚠️  ENCRYPTION_KEY no configurada. Usando key temporal (no persistente).")
        self._cipher = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, credentials: dict) -> str:
        """Encripta credenciales → string seguro para almacenar."""
        json_data = json.dumps(credentials, ensure_ascii=False)
        return self._cipher.encrypt(json_data.encode()).decode()

    def decrypt(self, encrypted: str) -> dict:
        """Desencripta string → dict de credenciales."""
        try:
            json_data = self._cipher.decrypt(encrypted.encode()).decode()
            return json.loads(json_data)
        except Exception as e:
            raise ValueError(f"Error desencriptando credenciales: {str(e)}")

    @staticmethod
    def generate_key() -> str:
        """Genera una nueva Fernet key para configurar en .env"""
        return Fernet.generate_key().decode()


# ──────────────────────────────────────────────
# CONVERSIÓN: Odoo → Formato Siesa (compatible con comparador.py)
# ──────────────────────────────────────────────

def odoo_to_siesa_format(facturas_odoo: List[Dict]) -> Dict:
    """
    Convierte las facturas extraídas de Odoo al formato que produce
    leer_siesa(), para que comparar_facturas() funcione sin cambios.

    Esto permite que el motor de comparación existente reciba datos
    de Odoo como si vinieran de un Excel de Siesa.

    Retorna dict con:
      - 'facturas': lista de dicts (nit, clave, nombre, docto_original, fecha)
      - 'metadata': info de la conexión Odoo
    """
    facturas = []
    for f in facturas_odoo:
        facturas.append({
            "nit": f["nit"],
            "clave": f["factura_clave"],
            "nombre": f["nombre"],
            "docto_original": f["factura_original"],
            "fecha": f.get("fecha", "Sin fecha"),
            "tipo_documento": f.get("tipo_documento", ""),
            "monto": f.get("monto", 0),
        })

    return {
        "facturas": facturas,
        "metadata": {
            "source": "odoo_api",
            "total_facturas": len(facturas),
            "extraido_en": datetime.now().isoformat(),
        }
    }
