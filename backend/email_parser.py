import imaplib
import email
from email.header import decode_header
import zipfile
import io
import json
from lxml import etree
import psycopg2
import os
from dotenv import load_dotenv
import re
from datetime import datetime

load_dotenv()

# Configuración
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = os.getenv("IMAP_PORT", "993")
IMAP_USER = os.getenv("IMAP_USER")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD")
NEON_DB_URL = os.getenv("NEON_DB_URL")

# Namespaces UBL 2.1 típicos en Colombia
NAMESPACES = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "fe": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2", # A veces la raíz
    "sts": "dian:gov:co:facturaelectronica:Structures-2-1" # Puede variar
}

def connect_db():
    return psycopg2.connect(NEON_DB_URL)

def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, int(IMAP_PORT))
    mail.login(IMAP_USER, IMAP_PASSWORD)
    return mail

def get_text_from_xml(root, xpath_expr, default=""):
    """Helper para extraer texto de XML usando XPath y namespaces definidos."""
    try:
        nodes = root.xpath(xpath_expr, namespaces=NAMESPACES)
        if nodes and nodes[0].text:
            return nodes[0].text.strip()
    except Exception as e:
        pass
    return default

def parse_invoice_lines(root) -> list:
    """
    Parsea las líneas de detalle de una factura UBL 2.1 (cac:InvoiceLine).
    Retorna lista de dicts con: numero, descripcion, cantidad, precio_unitario, total, codigo_producto.
    """
    lineas = []
    try:
        invoice_lines = root.xpath("//cac:InvoiceLine", namespaces=NAMESPACES)
        for line in invoice_lines:
            numero = get_text_from_xml(line, "cbc:ID", "")
            descripcion = get_text_from_xml(line, "cac:Item/cbc:Description", "")
            cantidad_str = get_text_from_xml(line, "cbc:InvoicedQuantity", "0")
            precio_str = get_text_from_xml(line, "cac:Price/cbc:PriceAmount", "0")
            total_str = get_text_from_xml(line, "cbc:LineExtensionAmount", "0")
            codigo = get_text_from_xml(line, "cac:Item/cac:SellersItemIdentification/cbc:ID", "")

            cantidad = float(cantidad_str) if cantidad_str else 0
            precio = float(precio_str) if precio_str else 0
            total = float(total_str) if total_str else 0

            if descripcion or codigo:
                lineas.append({
                    "numero": numero,
                    "descripcion": descripcion,
                    "codigo_producto": codigo,
                    "cantidad": cantidad,
                    "precio_unitario": precio,
                    "total": total,
                })
    except Exception as e:
        print(f"Error parseando InvoiceLine: {e}")
    return lineas


def parse_ubl_xml(xml_bytes):
    """
    Parsea el XML UBL 2.1 y extrae la metadata clave + líneas de detalle.
    """
    try:
        root = etree.fromstring(xml_bytes)
        
        # Extraer CUFE (Usualmente dentro de UUID con el nombre del schema de CUFE)
        cufe = get_text_from_xml(root, "//cbc:UUID")
        if not cufe:
            return None # Si no hay CUFE, no es factura electrónica colombiana válida
        
        doc_number = get_text_from_xml(root, "//cbc:ID", "DESCONOCIDO")
        doc_type = "FACTURA" # Aquí se podría leer InvoiceTypeCode
        
        # Intentar extraer Emisor (AccountingSupplierParty o SenderParty dependiendo si es AttachedDocument)
        supplier_nit = get_text_from_xml(root, "//cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID")
        supplier_name = get_text_from_xml(root, "//cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:RegistrationName")
        
        if not supplier_nit:
            # En un AttachedDocument (el contenedor usual de la DIAN)
            supplier_nit = get_text_from_xml(root, "//cac:SenderParty/cac:PartyTaxScheme/cbc:CompanyID")
        if not supplier_name:
            supplier_name = get_text_from_xml(root, "//cac:SenderParty/cac:PartyTaxScheme/cbc:RegistrationName")

        issue_date_str = get_text_from_xml(root, "//cbc:IssueDate")
        issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date() if issue_date_str else datetime.now().date()
        
        currency = get_text_from_xml(root, "//cbc:DocumentCurrencyCode", "COP")
        
        # Si es un AttachedDocument, los totales están en el Invoice interno
        invoice_root = root
        desc = root.xpath('//cac:Attachment/cac:ExternalReference/cbc:Description', namespaces=NAMESPACES)
        if desc and desc[0].text:
            try:
                invoice_root = etree.fromstring(desc[0].text.encode('utf-8'))
            except:
                pass

        # Totales
        subtotal_str = get_text_from_xml(invoice_root, "//cac:LegalMonetaryTotal/cbc:LineExtensionAmount", "0")
        tax_amount_str = get_text_from_xml(invoice_root, "//cac:TaxTotal/cbc:TaxAmount", "0")
        total_amount_str = get_text_from_xml(invoice_root, "//cac:LegalMonetaryTotal/cbc:PayableAmount", "0")
        
        # Líneas de detalle
        line_items = parse_invoice_lines(invoice_root)
        metadata = {
            "line_items": line_items,
            "total_lineas": len(line_items),
        } if line_items else {"line_items": [], "total_lineas": 0}
        
        return {
            "cufe": cufe,
            "document_number": doc_number,
            "document_type": doc_type,
            "supplier_nit": supplier_nit or "N/A",
            "supplier_name": supplier_name or "Desconocido",
            "issue_date": issue_date,
            "currency": currency,
            "subtotal": float(subtotal_str or 0),
            "tax_amount": float(tax_amount_str or 0),
            "total_amount": float(total_amount_str or 0),
            "xml_metadata": json.dumps(metadata, ensure_ascii=False)
        }
    except Exception as e:
        print(f"Error parseando XML: {e}")
        return None

def _process_emails_core(force_resync: bool = False) -> dict:
    """
    Núcleo de procesamiento de correos.
    force_resync=False → solo correos UNSEEN, idempotente por Message-ID.
    force_resync=True  → todos los correos (ALL), re-procesa aunque ya existan en logs,
                         y hace UPSERT en electronic_documents (sobrescribe por CUFE).
    """
    if not all([IMAP_USER, IMAP_PASSWORD, NEON_DB_URL]):
        return {"status": "error", "message": "Credenciales de correo o BD faltantes en .env"}

    db = None
    mail = None
    processed_count = 0
    skipped_count = 0
    errors = []

    try:
        db = connect_db()
        cursor = db.cursor()

        cursor.execute("SELECT id FROM companies LIMIT 1;")
        company_row = cursor.fetchone()
        if not company_row:
            return {"status": "error", "message": "No hay empresas configuradas en la BD."}
        company_id = company_row[0]

        try:
            mail = connect_imap()
            mail.select("inbox")
        except Exception as e:
            return {"status": "error", "message": f"Error conectando a IMAP: {str(e)}"}

        # En modo forzado buscamos TODOS los correos; en normal solo los no leídos
        imap_criteria = "ALL" if force_resync else "UNSEEN"
        status, messages = mail.search(None, imap_criteria)
        if status != "OK" or not messages[0]:
            label = "correos" if force_resync else "correos nuevos"
            return {"status": "success", "message": f"No hay {label} por procesar.", "processed_invoices": 0, "skipped": 0}

        msg_ids = messages[0].split()
        print(f"[email_parser] {'FORCE' if force_resync else 'NORMAL'} — {len(msg_ids)} correos a revisar")

        for msg_id in msg_ids:
            res, msg_data = mail.fetch(msg_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])

                    email_msg_id = msg.get("Message-ID", f"unknown-{msg_id.decode('utf-8')}").strip()
                    subject_header = decode_header(msg.get("Subject", ""))[0]
                    subject = subject_header[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(subject_header[1] or "utf-8", errors="ignore")
                    sender = msg.get("From", "Unknown")

                    if force_resync:
                        # En modo forzado: eliminar log anterior para permitir re-inserción
                        cursor.execute(
                            "DELETE FROM email_inbox_logs WHERE message_id = %s",
                            (email_msg_id,)
                        )
                    else:
                        # En modo normal: saltar correos ya procesados
                        cursor.execute(
                            "SELECT id FROM email_inbox_logs WHERE message_id = %s",
                            (email_msg_id,)
                        )
                        if cursor.fetchone():
                            skipped_count += 1
                            continue

                    # Registrar correo en logs
                    cursor.execute("""
                        INSERT INTO email_inbox_logs (company_id, message_id, sender_email, subject, received_at, status)
                        VALUES (%s, %s, %s, %s, NOW(), 'PROCESSING') RETURNING id
                    """, (company_id, email_msg_id, sender, subject))
                    log_id = cursor.fetchone()[0]

                    xml_parsed = False

                    for part in msg.walk():
                        if part.get_content_maintype() == "multipart":
                            continue
                        filename = part.get_filename()
                        if not filename:
                            continue

                        if filename.lower().endswith(".zip"):
                            try:
                                zip_data = part.get_payload(decode=True)
                                with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                                    for zinfo in z.infolist():
                                        if zinfo.filename.lower().endswith(".xml"):
                                            xml_bytes = z.read(zinfo.filename)
                                            doc_data = parse_ubl_xml(xml_bytes)
                                            if doc_data:
                                                if force_resync:
                                                    # UPSERT: si ya existe por CUFE, actualizar datos
                                                    cursor.execute("""
                                                        INSERT INTO electronic_documents
                                                        (company_id, email_log_id, cufe, document_number, document_type,
                                                         supplier_nit, supplier_name, issue_date, currency, subtotal,
                                                         tax_amount, total_amount, xml_metadata)
                                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                                        ON CONFLICT (cufe) DO UPDATE SET
                                                            email_log_id   = EXCLUDED.email_log_id,
                                                            document_number= EXCLUDED.document_number,
                                                            supplier_nit   = EXCLUDED.supplier_nit,
                                                            supplier_name  = EXCLUDED.supplier_name,
                                                            issue_date     = EXCLUDED.issue_date,
                                                            xml_metadata   = EXCLUDED.xml_metadata
                                                    """, (
                                                        company_id, log_id, doc_data["cufe"],
                                                        doc_data["document_number"], doc_data["document_type"],
                                                        doc_data["supplier_nit"], doc_data["supplier_name"],
                                                        doc_data["issue_date"], doc_data["currency"],
                                                        doc_data["subtotal"], doc_data["tax_amount"],
                                                        doc_data["total_amount"], doc_data["xml_metadata"],
                                                    ))
                                                else:
                                                    cursor.execute("""
                                                        INSERT INTO electronic_documents
                                                        (company_id, email_log_id, cufe, document_number, document_type,
                                                         supplier_nit, supplier_name, issue_date, currency, subtotal,
                                                         tax_amount, total_amount, xml_metadata)
                                                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                                        ON CONFLICT (cufe) DO NOTHING
                                                    """, (
                                                        company_id, log_id, doc_data["cufe"],
                                                        doc_data["document_number"], doc_data["document_type"],
                                                        doc_data["supplier_nit"], doc_data["supplier_name"],
                                                        doc_data["issue_date"], doc_data["currency"],
                                                        doc_data["subtotal"], doc_data["tax_amount"],
                                                        doc_data["total_amount"], doc_data["xml_metadata"],
                                                    ))
                                                xml_parsed = True
                                                processed_count += 1
                            except Exception as e:
                                errors.append(f"Error procesando ZIP {filename}: {str(e)}")

                    final_status = "SUCCESS" if xml_parsed else "NO_XML_FOUND"
                    cursor.execute(
                        "UPDATE email_inbox_logs SET status = %s WHERE id = %s",
                        (final_status, log_id)
                    )
                    db.commit()

        mode_label = "re-sincronización forzada" if force_resync else "escaneo de bandeja"
        return {
            "status": "success",
            "message": f"{mode_label.capitalize()} completado.",
            "processed_invoices": processed_count,
            "skipped": skipped_count,
            "errors": errors,
            "force_resync": force_resync,
        }

    except Exception as e:
        if db:
            db.rollback()
        return {"status": "error", "message": f"Error global: {str(e)}"}
    finally:
        if db:
            db.close()
        if mail:
            try:
                mail.close()
                mail.logout()
            except:
                pass


def process_emails() -> dict:
    """Escanea correos NO LEÍDOS (modo normal, idempotente)."""
    return _process_emails_core(force_resync=False)


def process_emails_force() -> dict:
    """
    Re-sincronización forzada: escanea TODOS los correos del buzón
    (incluyendo ya leídos), limpia logs previos y re-inserta documentos.
    Útil cuando se borraron registros de la BD y se quiere reimportar.
    """
    return _process_emails_core(force_resync=True)
