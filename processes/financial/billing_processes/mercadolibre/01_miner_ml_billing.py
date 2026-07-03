import requests
import MySQLdb
import xml.etree.ElementTree as ET
import base64
import time
import json
from datetime import datetime
from dotenv import load_dotenv
import os
import logging

# ── Logging setup ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('ml_invoices_debug.log', encoding='utf-8')
    ]
)
log = logging.getLogger(__name__)

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

def xml_to_dict(element):
    """Convierte un elemento XML a dict recursivamente."""
    result = {}
    tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
    result['_tag'] = tag
    if element.attrib:
        result['_attrs'] = dict(element.attrib)
    children = list(element)
    if children:
        result['_children'] = [xml_to_dict(child) for child in children]
    if element.text and element.text.strip():
        result['_text'] = element.text.strip()
    return result

def extract_ml_invoices():
    # ── Conexión DB ────────────────────────────────────────────
    log.info("Conectando a la base de datos...")
    db = MySQLdb.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        passwd=os.getenv("DB_PASSWORD"), db=os.getenv("DB_NAME"),
        local_infile=True, charset='utf8mb4'
    )
    cursor = db.cursor()

    # ── Token ML ───────────────────────────────────────────────
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = '25523702'")
    row = cursor.fetchall()
    if not row:
        log.error("No se encontró token para seller_id=25523702")
        return
    ml_token = str(row[0][0])
    headers = {'Authorization': f'Bearer {ml_token}'}

    # ── Órdenes candidatas (Paciente Cero: 2026-06-01 19:50:58 UTC) ──
    query = """
        SELECT o.order_id, o.date_closed, s.status as invoice_status, o.status as order_status_ml, IFNULL(s.retry_count, 0)
        FROM somos_reyes.ml_order_update o
        LEFT JOIN finance.mkp_billing_staging s ON o.order_id = s.mkp_order_id
        WHERE o.date_created >= GREATEST('2026-06-01 15:50:55', UTC_TIMESTAMP() - INTERVAL 7 DAY)
          AND o.status IN ('paid', 'closed') -- Solo aseguramos órdenes que ya procesaron pago
          AND (s.status IS NULL OR s.status = 'NO_INVOICE_IN_ML')
        ORDER BY o.date_closed ASC;
    """
    
    query = """
        SELECT o.order_id, o.date_closed, s.status as invoice_status, o.status as order_status_ml, IFNULL(s.retry_count, 0)
        FROM somos_reyes.ml_order_update o
        LEFT JOIN finance.mkp_billing_staging s ON o.order_id = s.mkp_order_id
        WHERE o.date_created >= '2026-06-01 15:50:55' -- Fecha UTC -4 exacta del Paciente Cero
        AND o.status IN ('paid', 'closed') 
        AND (s.status IS NULL OR s.status = 'NO_INVOICE_IN_ML')
        ORDER BY o.date_closed ASC;
    """
    
    cursor.execute(query)
    orders = cursor.fetchall()
    log.info(f"Órdenes candidatas encontradas: {len(orders)}")

    if not orders:
        log.info("No hay órdenes pendientes de facturación en ML en este momento.")
        cursor.close()
        db.close()
        return

    inserts_ok  = 0
    skipped_404 = 0
    errors      = 0

    for index, o in enumerate(orders):
        order_id = o[0]
        date_closed = o[1] # Esto es un datetime object o string (UTC 0)
        retry_count = o[4]
        
        url = f"https://api.mercadolibre.com/invoices/io/documents/stream/order/{order_id}/xml"
        
        try:
            r = requests.get(url, headers=headers, timeout=15)
        except requests.exceptions.RequestException as e:
            log.error(f"Error de red en orden {order_id}: {e}")
            errors += 1
            continue

        if r.status_code == 401:
            log.error("Token expirado (401). Abortando script.")
            break

        # ── Manejo Inteligente del 404 (Sin Factura Aún) ──
        if r.status_code == 404:
            new_retry_count = retry_count + 1
            
            # Asegurar que date_closed sea un objeto datetime para calcular horas
            if isinstance(date_closed, str):
                date_closed_obj = datetime.strptime(date_closed[:19], "%Y-%m-%d %H:%M:%S")
            else:
                date_closed_obj = date_closed

            hours_elapsed = (datetime.utcnow() - date_closed_obj).total_seconds() / 3600
            
            # Si pasaron más de 72 horas, ML falló definitivamente
            if hours_elapsed > 72:
                new_status = 'NEVER_BILLED_BY_ML'
                log.warning(f"Orden {order_id} excedió 72 hrs. Marcada como NEVER_BILLED_BY_ML.")
            else:
                new_status = 'NO_INVOICE_IN_ML'
                log.debug(f"Orden {order_id} sin factura aún ({int(hours_elapsed)} hrs desde el pago).")

            cursor.execute("""
                INSERT INTO finance.mkp_billing_staging 
                (marketplace, mkp_order_id, status, retry_count) 
                VALUES ('MERCADO_LIBRE', %s, %s, %s)
                ON DUPLICATE KEY UPDATE status = VALUES(status), retry_count = VALUES(retry_count)
            """, (order_id, new_status, new_retry_count))
            db.commit()
            skipped_404 += 1
            continue

        if r.status_code != 200:
            log.warning(f"Respuesta {r.status_code} inesperada en orden {order_id}")
            errors += 1
            continue

        # ── Parsear XML (Si llegó 200 OK) ──
        try:
            root = ET.fromstring(r.content)
            ns = {
                'cfdi': 'http://www.sat.gob.mx/cfd/4',
                'tfd':  'http://www.sat.gob.mx/TimbreFiscalDigital'
            }

            comp = root.attrib
            tfd_node = root.find('cfdi:Complemento/tfd:TimbreFiscalDigital', ns)

            if tfd_node is None:
                errors += 1
                continue

            tfd = tfd_node.attrib
            uuid = tfd.get('UUID')
            total = float(comp.get('Total', 0))
            xml_base64 = base64.b64encode(r.content).decode('utf-8')
            raw_json_str = json.dumps(xml_to_dict(root), ensure_ascii=False)

            if not uuid:
                errors += 1
                continue

            # Inyectar XML y marcar como PENDING para el Loader
            cursor.execute("""
                INSERT INTO finance.mkp_billing_staging 
                (marketplace, mkp_order_id, cfdi_uuid, total_amount, xml_data, raw_json, status, retry_count) 
                VALUES ('MERCADO_LIBRE', %s, %s, %s, %s, %s, 'PENDING', %s)
                ON DUPLICATE KEY UPDATE 
                    cfdi_uuid = VALUES(cfdi_uuid),
                    total_amount = VALUES(total_amount),
                    xml_data = VALUES(xml_data),
                    raw_json = VALUES(raw_json),
                    status = 'PENDING'
            """, (order_id, uuid, total, xml_base64, raw_json_str, retry_count))
            db.commit()
            inserts_ok += 1
            log.info(f"XML descargado y listo para Loader: {order_id}")

        except Exception as e:
            log.error(f"Error procesando XML de {order_id}: {e}")
            errors += 1

        time.sleep(0.1) # Respetar rate limits de ML

    log.info(f"Resumen -> Extraídas: {inserts_ok} | En espera (404): {skipped_404} | Errores: {errors}")
    cursor.close()
    db.close()

if __name__ == "__main__":
    extract_ml_invoices()