import requests
import MySQLdb
import xml.etree.ElementTree as ET
import base64
import time
import json
from datetime import datetime
import os
import logging
import sys

# ── Logging setup ──────────────────────────────────────────────
# Solo StreamHandler: Kestra captura stdout/stderr como logs de la ejecución,
# un FileHandler dentro del WorkingDirectory se pierde al terminar la tarea.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Parámetros por variables de entorno ────────────────────────
DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

# Vendedor de Mercado Libre y ventana de búsqueda (Paciente Cero: fecha UTC-4 exacta)
ML_SELLER_ID       = os.getenv("ML_SELLER_ID", "25523702")
ML_BILLING_START   = os.getenv("ML_BILLING_START", "2026-06-01 15:50:55")
# Horas tras el pago a partir de las cuales se asume que ML jamás va a facturar (30 días)
NEVER_BILLED_HOURS = int(os.getenv("NEVER_BILLED_HOURS", "720"))
REQUEST_TIMEOUT    = int(os.getenv("REQUEST_TIMEOUT", "15"))
# Pausa entre llamadas a la API de ML para respetar rate limits
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.1"))


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
    # ── Validación de configuración ────────────────────────────
    missing = [k for k, v in {
        "DB_HOST": DB_HOST, "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD, "DB_NAME": DB_NAME
    }.items() if not v]
    if missing:
        log.error(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
        raise SystemExit(1)

    # ── Conexión DB ────────────────────────────────────────────
    db = MySQLdb.connect(
        host=DB_HOST, user=DB_USER,
        passwd=DB_PASSWORD, db=DB_NAME,
        local_infile=True, charset='utf8mb4'
    )
    cursor = db.cursor()

    # ── Token ML ───────────────────────────────────────────────
    cursor.execute(
        "SELECT token FROM somos_reyes.tokens WHERE seller_id = %s",
        (ML_SELLER_ID,)
    )
    row = cursor.fetchall()
    if not row:
        log.error(f"No se encontró token para seller_id={ML_SELLER_ID}")
        cursor.close()
        db.close()
        raise SystemExit(1)
    ml_token = str(row[0][0])
    headers = {'Authorization': f'Bearer {ml_token}'}

    # ── Órdenes candidatas ─────────────────────────────────────
    cursor.execute("""
        SELECT o.order_id, o.date_closed, s.status as invoice_status, o.status as order_status_ml, IFNULL(s.retry_count, 0)
        FROM somos_reyes.ml_order_update o
        LEFT JOIN finance.mkp_billing_prod s ON o.order_id = s.mkp_order_id
        WHERE o.date_created >= %s
          AND o.status IN ('paid', 'closed') -- Solo aseguramos órdenes que ya procesaron pago
          AND (s.status IS NULL OR s.status = 'NO_INVOICE_IN_ML')
        ORDER BY o.date_closed ASC;
    """, (ML_BILLING_START,))
    orders = cursor.fetchall()
    log.info(f"Órdenes candidatas encontradas: {len(orders)}")

    if not orders:
        cursor.close()
        db.close()
        return

    # ── Contadores (los detalles por orden NO se loguean: solo el resumen final) ──
    inserts_ok    = 0
    waiting_404   = 0
    never_billed  = 0
    net_errors    = 0
    http_errors   = 0
    xml_errors    = 0
    token_expired = False

    for o in orders:
        order_id = o[0]
        date_closed = o[1]  # datetime object o string (UTC 0)
        retry_count = o[4]

        url = f"https://api.mercadolibre.com/invoices/io/documents/stream/order/{order_id}/xml"

        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException:
            net_errors += 1
            continue

        if r.status_code == 401:
            token_expired = True
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

            # Si pasaron más de NEVER_BILLED_HOURS, ML falló definitivamente
            if hours_elapsed > NEVER_BILLED_HOURS:
                new_status = 'NEVER_BILLED_BY_ML'
                never_billed += 1
            else:
                new_status = 'NO_INVOICE_IN_ML'
                waiting_404 += 1

            cursor.execute("""
                INSERT INTO finance.mkp_billing_prod
                (marketplace, mkp_order_id, status, retry_count)
                VALUES ('MERCADO_LIBRE', %s, %s, %s)
                ON DUPLICATE KEY UPDATE status = VALUES(status), retry_count = VALUES(retry_count)
            """, (order_id, new_status, new_retry_count))
            db.commit()
            continue

        if r.status_code != 200:
            http_errors += 1
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
                xml_errors += 1
                continue

            tfd = tfd_node.attrib
            uuid = tfd.get('UUID')
            total = float(comp.get('Total', 0))
            xml_base64 = base64.b64encode(r.content).decode('utf-8')
            raw_json_str = json.dumps(xml_to_dict(root), ensure_ascii=False)

            if not uuid:
                xml_errors += 1
                continue

            # Inyectar XML y marcar como PENDING para el Loader
            cursor.execute("""
                INSERT INTO finance.mkp_billing_prod
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

        except Exception:
            xml_errors += 1

        time.sleep(SLEEP_BETWEEN_CALLS)  # Respetar rate limits de ML

    # ── Resumen único de la corrida ────────────────────────────
    total_errors = net_errors + http_errors + xml_errors
    log.info(
        f"Resumen -> Candidatas: {len(orders)} | XMLs extraídos: {inserts_ok} | "
        f"En espera (404): {waiting_404} | Nunca facturadas por ML: {never_billed} | "
        f"Errores: {total_errors} (red={net_errors}, http={http_errors}, xml={xml_errors})"
    )
    cursor.close()
    db.close()

    # Si el token murió a media corrida, la tarea debe fallar para que Kestra reintente/alerte
    if token_expired:
        raise SystemExit(1)


if __name__ == "__main__":
    extract_ml_invoices()
