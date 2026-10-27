import requests
import MySQLdb
import xml.etree.ElementTree as ET
import base64
import time
import json
from datetime import datetime, timedelta
import os
import logging
import sys
import re

# Ejecución local: credenciales desde el .env del repo wonderbrands. En Kestra el archivo
# no existe y las variables llegan por env (override=False: lo que ya esté en env gana).
DOTENV_PATH = os.getenv("DOTENV_PATH", r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env")
DOTENV_STATUS = "no aplica (sin archivo .env)"
if os.path.exists(DOTENV_PATH):
    try:
        from dotenv import load_dotenv
        load_dotenv(DOTENV_PATH, override=False)
        DOTENV_STATUS = f"cargado {DOTENV_PATH}"
    except ImportError:
        DOTENV_STATUS = "python-dotenv no está instalado: pip install python-dotenv"

# ml_stores.py vive con los procesos productivos de ML (01-04); se reutiliza desde aquí
# para no duplicar la configuración por tienda.
sys.path.insert(0, os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', '..', 'processes', 'wonderbrands', 'financial', 'billing_processes', 'mercadolibre')))
from ml_stores import get_store_config

# ── 05 · Miner de Notas de Crédito ML ──────────────────────────
# Homólogo del 01: baja de ML el CFDI de egreso (NC) que ML timbra con nuestro
# sello y lo deja en NC_TABLE (finance.mkp_credit_notes_*) para que el 06 lo registre en
# Odoo. Este script NO toca Odoo.
#
# Qué órdenes se consultan: NC_SCAN_TABLE es la cola de revisión,
# una fila por orden con factura viva en Odoo:
#   * SWEEP  -> TODA orden facturada. Se revisa en días fijos contados desde que
#               entró a mkp_billing_prod (SWEEP_CHECK_OFFSETS_DAYS) y se cierra.
#               Garantiza cobertura total; el barrido histórico es este mismo modo.
#   * Señal  -> orden cancelada, pago reembolsado, claim ganado por el comprador o
#               REFUND en movimientos financieros. Se revisa en la corrida actual y
#               luego con backoff hasta SIGNAL_MAX_DAYS. Si ML nunca emite NC queda
#               NEVER_CREDITED_BY_ML (posible factura cancelada por ML en el SAT).
# Una orden puede tener varias NC (llave = UUID de la NC): si la acumulada no
# cubre la factura, la orden sigue en revisión (FOUND_PARTIAL).

# ── Logging setup ──────────────────────────────────────────────
# Solo StreamHandler: Kestra captura stdout/stderr como logs de la ejecución.
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

# Tienda de Mercado Libre (Kestra inyecta ML_SELLER_ID; el resto sale de ml_stores.py)
STORE           = get_store_config()
ML_SELLER_ID    = STORE['seller_id']
MKP_MARKETPLACE = STORE['marketplace']
# Corte de facturación: las señales anteriores no pueden tener NC de ML que nos toque
ML_BILLING_START = STORE['billing_start']

# Tablas de control. Fase de pruebas: *_staging (finance.mkp_billing_prod solo se LEE).
NC_TABLE      = os.getenv("NC_TABLE", "finance.mkp_credit_notes_staging")
NC_SCAN_TABLE = os.getenv("NC_SCAN_TABLE", "finance.mkp_credit_notes_scan_staging")

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
# Qué hace la corrida (UN solo modo a la vez):
#   "preflight" -> solo valida BD, tablas, señales y 1 llamada a ML. NO escribe nada.
#   "orders"    -> prueba dirigida: consulta SOLO las órdenes de LOCAL_ORDER_IDS.
#                  No llena la cola con nada más.
#   "signals"   -> llena la cola con las órdenes facturadas que tienen señal de reembolso
#                  (cancelada, pago reembolsado, claim perdido, REFUND) y consulta solo esas.
#   "sweep"     -> llena la cola con TODAS las órdenes facturadas (barrido histórico)
#                  y consulta las que ya tocan por calendario.
#   "both"      -> signals + sweep; consulta primero las de señal. Es el modo de producción.
LOCAL_RUN               = "both"
# Órdenes para "orders" (y la primera se usa como orden de prueba en "preflight")
LOCAL_ORDER_IDS         = "2000018238217954,2000016723575980,2000016949680478,2000017527328146"
# Tope de órdenes que se CONSULTAN en ML en esta corrida (llenar la cola no cuenta)
LOCAL_MAX_CALLS_PER_RUN = 200
# En Kestra no aplica nada de este bloque: el flujo manda PREFLIGHT_ONLY, SCAN_MODE,
# ONLY_ORDER_IDS y MAX_CALLS_PER_RUN como variables de entorno, y esas ganan.
# ══════════════════════════════════════════════════════════════
LOCAL_RUN_OPTIONS = ("preflight", "orders", "signals", "sweep", "both")
if LOCAL_RUN not in LOCAL_RUN_OPTIONS:
    raise ValueError(f"LOCAL_RUN='{LOCAL_RUN}' inválido; opciones: {LOCAL_RUN_OPTIONS}")


def _env_bool(name, default):
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "si", "sí")


def _parse_order_ids(value):
    """Acepta texto separado por comas o lista. Devuelve SIEMPRE lista de strings.
    (Un texto suelto rompería el IN (...): cada carácter se volvería un parámetro.)"""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    ids = [str(i).strip() for i in items if str(i).strip()]
    invalid = [i for i in ids if not i.isdigit()]
    if invalid:
        raise ValueError(f"ONLY_ORDER_IDS tiene valores que no son order_id numéricos: {invalid}")
    return ids


# PREFLIGHT_ONLY: valida BD, tablas, token y endpoint SIN escribir nada, y termina
PREFLIGHT_ONLY = _env_bool("PREFLIGHT_ONLY", LOCAL_RUN == "preflight")
# Orden para la llamada de prueba del preflight (por defecto, la primera de LOCAL_ORDER_IDS)
PREFLIGHT_ORDER_ID = os.getenv("PREFLIGHT_ORDER_ID", (_parse_order_ids(LOCAL_ORDER_IDS) or [""])[0])

# RFC emisor esperado en la NC (ML timbra a nombre de Somos Reyes)
COMPANY_RFC = os.getenv("COMPANY_RFC", "SRE1410278MA")

# signals = solo órdenes con señal de reembolso | sweep = barrido completo | both
SCAN_MODE = os.getenv("SCAN_MODE", LOCAL_RUN if LOCAL_RUN in ("signals", "sweep", "both") else "both").strip().lower()
SIGNALS   = [s.strip().upper() for s in
             os.getenv("SIGNALS", "ORDER_CANCELLED,PAYMENT_REFUNDED,CLAIM_COMPLAINANT,FIN_REFUND").split(",")
             if s.strip()]
# Prueba dirigida: consulta solo estas órdenes, sin importar su estado en la cola
ONLY_ORDER_IDS = _parse_order_ids(os.getenv("ONLY_ORDER_IDS", LOCAL_ORDER_IDS if LOCAL_RUN == "orders" else ""))

# Cadencia de revisión
SWEEP_CHECK_OFFSETS_DAYS = sorted(int(x) for x in os.getenv("SWEEP_CHECK_OFFSETS_DAYS", "15,45,90").split(","))
SIGNAL_MAX_DAYS          = int(os.getenv("SIGNAL_MAX_DAYS", "45"))
SIGNAL_MAX_INTERVAL_DAYS = int(os.getenv("SIGNAL_MAX_INTERVAL_DAYS", "7"))
ERROR_RETRY_HOURS        = int(os.getenv("ERROR_RETRY_HOURS", "6"))

# Tolerancia de montos NC vs factura (misma regla de ±$1.00 que TOTAL_DIFF en el 02)
AMOUNT_TOLERANCE = float(os.getenv("AMOUNT_TOLERANCE", "1.0"))

# Límites de la corrida (el barrido histórico se completa en varias corridas si hace falta)
MAX_CALLS_PER_RUN   = int(os.getenv("MAX_CALLS_PER_RUN", LOCAL_MAX_CALLS_PER_RUN))
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "300"))

REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "15"))
# Pausa entre llamadas a la API de ML para respetar rate limits
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.1"))
MAX_HTTP_RETRIES    = int(os.getenv("MAX_HTTP_RETRIES", "3"))
# 429 seguidos tras los cuales se corta la corrida limpiamente (la siguiente retoma la cola)
MAX_CONSECUTIVE_429 = int(os.getenv("MAX_CONSECUTIVE_429", "5"))
# Las anomalías (documento inválido, NC adicional, UUID repetido) sí se loguean por orden, con tope
MAX_ANOMALY_LOGS    = int(os.getenv("MAX_ANOMALY_LOGS", "50"))

DEVOLUTION_URL = ("https://api.mercadolibre.com/invoices/io/documents/stream/order/"
                  "{order_id}/xml?transaction_type=devolution")
NS = {
    'cfdi': 'http://www.sat.gob.mx/cfd/4',
    'tfd':  'http://www.sat.gob.mx/TimbreFiscalDigital'
}
# 'ALREADY_ODOO_INVOICE' (sin D) es el mismo estado truncado por el largo de
# mkp_billing_prod.status (visto en la consulta 0.b de la migración 002)
INVOICED_STATUSES_SQL = "('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED', 'ALREADY_ODOO_INVOICE')"

# Señales de reembolso: cada query parte de SU tabla, filtrada por tienda y fecha de corte
# (conjunto chico), y devuelve order_ids. El cruce contra la cola se hace después por lotes
# con IN (...). No usar EXISTS correlacionados contra mkp_billing_prod: recorren TODA la
# facturación y, sin índice útil en estas tablas, la consulta no termina.
# Parámetros de cada query: (ML_SELLER_ID, ML_BILLING_START).
# Tablas pobladas por las ETL de somos_reyes (MeLi_ventas_order_update, MeLi_ventas_claims,
# 80_finance_report). '%%' porque el texto pasa por el formateo de parámetros de MySQLdb.
SIGNAL_QUERIES = {
    'ORDER_CANCELLED': """SELECT DISTINCT order_id FROM somos_reyes.ml_order_update
                          WHERE seller_id = %s AND date_created >= %s AND status = 'cancelled'""",
    'PAYMENT_REFUNDED': """SELECT DISTINCT order_id FROM somos_reyes.ml_order_payments
                           WHERE seller_id = %s AND date_created >= %s AND refunded_amt > 0""",
    'CLAIM_COMPLAINANT': """SELECT DISTINCT resource_id FROM somos_reyes.ml_claims
                            WHERE seller_user_id = %s AND date_created >= %s AND status = 'closed'
                              AND resolution_benefited LIKE '%%complainant%%'""",
    'FIN_REFUND': """SELECT DISTINCT order_id FROM somos_reyes.ml_financial_transactions_master
                     WHERE user_id = %s AND transaction_date >= %s
                       AND transaction_type IN ('REFUND', 'REFUND_SHIPPING')""",
}
SIGNAL_CHUNK_SIZE = int(os.getenv("SIGNAL_CHUNK_SIZE", "1000"))


class InvalidDocument(Exception):
    """El endpoint respondió 200 pero el contenido no es una NC nuestra válida."""


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


def _to_float(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def parse_credit_note(content):
    """
    Valida y extrae la NC. Nunca se confía solo en el HTTP 200: el endpoint puede
    devolver JSON de error, o un CFDI que no sea de egreso. Cualquier cosa que no
    sea un CFDI 4.0 tipo E, timbrado, emitido por nuestro RFC y relacionado a otro
    CFDI se rechaza con InvalidDocument.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise InvalidDocument(f"la respuesta no es XML: {content[:150]!r}")

    if root.tag != f"{{{NS['cfdi']}}}Comprobante":
        raise InvalidDocument(f"raíz inesperada {root.tag}")

    tipo = root.get('TipoDeComprobante')
    if tipo != 'E':
        raise InvalidDocument(f"TipoDeComprobante={tipo} (se esperaba E)")

    tfd = root.find('cfdi:Complemento/tfd:TimbreFiscalDigital', NS)
    uuid = tfd.get('UUID') if tfd is not None else None
    if not uuid:
        raise InvalidDocument("sin TimbreFiscalDigital/UUID")

    emisor = root.find('cfdi:Emisor', NS)
    rfc_emisor = emisor.get('Rfc') if emisor is not None else None
    if rfc_emisor != COMPANY_RFC:
        raise InvalidDocument(f"RFC emisor {rfc_emisor} distinto de {COMPANY_RFC}")

    rel_nodes = root.findall('cfdi:CfdiRelacionados', NS)
    related = [r.get('UUID').strip().upper()
               for node in rel_nodes for r in node.findall('cfdi:CfdiRelacionado', NS) if r.get('UUID')]
    if not related:
        raise InvalidDocument("sin CfdiRelacionados")

    receptor = root.find('cfdi:Receptor', NS)
    impuestos = root.find('cfdi:Impuestos', NS)
    fecha_timbrado = tfd.get('FechaTimbrado')

    return {
        'uuid': uuid.strip().upper(),
        'related_uuids': related,
        'tipo_relacion': ','.join(sorted({n.get('TipoRelacion') for n in rel_nodes if n.get('TipoRelacion')})),
        'serie': root.get('Serie'),
        'folio': root.get('Folio'),
        'moneda': root.get('Moneda'),
        'rfc_receptor': receptor.get('Rfc') if receptor is not None else None,
        'subtotal': _to_float(root.get('SubTotal')),
        'tax_amount': _to_float(impuestos.get('TotalImpuestosTrasladados')) if impuestos is not None else 0.0,
        'total': _to_float(root.get('Total')),
        'fecha_timbrado': datetime.strptime(fecha_timbrado[:19], "%Y-%m-%dT%H:%M:%S") if fecha_timbrado else None,
        'root': root,
    }


def classify_credit_note(nc, invoice_uuid, invoice_total, cumulative_total):
    """
    Estado inicial de la NC en staging. El 06 vuelve a validar todo contra Odoo;
    aquí se separa desde ya lo que jamás debe llegar a Odoo sin revisión humana.
    Devuelve (status, nc_scope, error_log).
    """
    nc_scope = 'FULL' if invoice_total > 0 and abs(invoice_total - nc['total']) <= AMOUNT_TOLERANCE else 'PARTIAL'

    if len(nc['related_uuids']) != 1:
        return 'MANUAL_REVIEW', nc_scope, f"La NC relaciona {len(nc['related_uuids'])} CFDI: {nc['related_uuids']}"
    if nc['tipo_relacion'] != '01':
        return 'MANUAL_REVIEW', nc_scope, f"TipoRelacion={nc['tipo_relacion']} (se esperaba 01)"
    if nc['moneda'] != 'MXN':
        return 'MANUAL_REVIEW', nc_scope, f"Moneda={nc['moneda']}"
    if not invoice_uuid or nc['related_uuids'][0] != invoice_uuid.strip().upper():
        return 'UUID_MISMATCH', nc_scope, (f"La NC relaciona {nc['related_uuids'][0]}; "
                                           f"la factura en mkp_billing_prod es {invoice_uuid}")
    if invoice_total <= 0:
        return 'MANUAL_REVIEW', nc_scope, "La factura no tiene total en mkp_billing_prod"
    if cumulative_total > invoice_total + AMOUNT_TOLERANCE:
        return 'AMOUNT_EXCEEDS_INVOICE', nc_scope, (f"NC acumuladas ${cumulative_total:,.2f} > "
                                                    f"factura ${invoice_total:,.2f}")
    return 'PENDING', nc_scope, None


def next_check_after_miss(signal_source, scan_status, reference_date, now):
    """
    Siguiente revisión cuando ML no trae una NC nueva (404, o la misma NC de antes).
    Devuelve (scan_status, next_check_at).
    """
    if scan_status != 'FOUND_PARTIAL' and signal_source == 'SWEEP':
        # Barrido: días fijos desde que la orden entró a facturación; luego se cierra
        for offset in SWEEP_CHECK_OFFSETS_DAYS:
            due = reference_date + timedelta(days=offset)
            if due > now:
                return 'WAITING', due
        return 'CLOSED', None

    # Señal, prueba dirigida o NC parcial: backoff 1,1,2,4,7,7... días desde reference_date
    elapsed_days = (now - reference_date).days
    if elapsed_days > SIGNAL_MAX_DAYS:
        return ('FOUND' if scan_status == 'FOUND_PARTIAL' else 'NEVER_CREDITED_BY_ML'), None
    interval = min(max(elapsed_days, 1), SIGNAL_MAX_INTERVAL_DAYS)
    return scan_status, now + timedelta(days=interval)


def get_devolution_xml(session, order_id, state):
    """GET al endpoint de devolución con reintentos para red/5xx y backoff para 429."""
    url = DEVOLUTION_URL.format(order_id=order_id)
    r = None
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException:
            r = None
            if attempt < MAX_HTTP_RETRIES:
                time.sleep(2 ** attempt)
            continue

        if r.status_code == 429:
            state['consecutive_429'] += 1
            if state['consecutive_429'] >= MAX_CONSECUTIVE_429:
                return r
            try:
                wait = int(r.headers.get('Retry-After', 0))
            except ValueError:
                wait = 0
            # Tope de 5 min por si ML manda un Retry-After anómalo
            time.sleep(min(wait, 300) if wait > 0 else min(60, 5 * 2 ** (attempt - 1)))
            continue

        state['consecutive_429'] = 0
        if r.status_code >= 500 and attempt < MAX_HTTP_RETRIES:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


def seed_scan_queue(cursor, db):
    """Encola órdenes facturadas: barrido (SWEEP) y/o señales de reembolso. Devuelve conteos."""
    seeded = {}

    if SCAN_MODE in ('sweep', 'both'):
        # Anti-join en vez de solo INSERT IGNORE: en corridas diarias casi todas las
        # órdenes ya están en la cola y así no se intenta re-insertar todo el histórico.
        cursor.execute(f"""
            INSERT IGNORE INTO {NC_SCAN_TABLE}
                (marketplace, mkp_order_id, signal_source, reference_date, scan_status, next_check_at)
            SELECT b.marketplace, b.mkp_order_id, 'SWEEP', b.created_at, 'WAITING',
                   GREATEST(UTC_TIMESTAMP(), b.created_at + INTERVAL %s DAY)
            FROM finance.mkp_billing_prod b
            LEFT JOIN {NC_SCAN_TABLE} s
                ON s.marketplace = b.marketplace AND s.mkp_order_id = b.mkp_order_id
            WHERE b.marketplace = %s
              AND b.status IN {INVOICED_STATUSES_SQL}
              AND s.id IS NULL
        """, (SWEEP_CHECK_OFFSETS_DAYS[0], MKP_MARKETPLACE))
        seeded['SWEEP'] = cursor.rowcount
        db.commit()

    if SCAN_MODE in ('signals', 'both'):
        for signal in SIGNALS:
            query = SIGNAL_QUERIES.get(signal)
            if not query:
                log.warning(f"[{MKP_MARKETPLACE}] Señal desconocida '{signal}', se ignora.")
                continue
            try:
                cursor.execute(query, (ML_SELLER_ID, ML_BILLING_START))
                order_ids = sorted({str(r[0]).strip() for r in cursor.fetchall() if r[0]})
                touched = 0

                for i in range(0, len(order_ids), SIGNAL_CHUNK_SIZE):
                    chunk = order_ids[i:i + SIGNAL_CHUNK_SIZE]
                    placeholders = ','.join(['%s'] * len(chunk))

                    # a) Órdenes que ya estaban en la cola por el barrido y siguen sin NC:
                    #    se promueven a señal y se revisan en esta misma corrida.
                    cursor.execute(f"""
                        UPDATE {NC_SCAN_TABLE}
                        SET signal_source = %s, reference_date = UTC_TIMESTAMP(),
                            scan_status = 'WAITING', next_check_at = UTC_TIMESTAMP()
                        WHERE marketplace = %s
                          AND mkp_order_id IN ({placeholders})
                          AND signal_source = 'SWEEP'
                          AND scan_status IN ('WAITING', 'CLOSED')
                    """, (signal, MKP_MARKETPLACE, *chunk))
                    touched += cursor.rowcount

                    # b) Órdenes facturadas con señal que aún no estaban en la cola (modo 'signals')
                    cursor.execute(f"""
                        INSERT IGNORE INTO {NC_SCAN_TABLE}
                            (marketplace, mkp_order_id, signal_source, reference_date, scan_status, next_check_at)
                        SELECT b.marketplace, b.mkp_order_id, %s, UTC_TIMESTAMP(), 'WAITING', UTC_TIMESTAMP()
                        FROM finance.mkp_billing_prod b
                        LEFT JOIN {NC_SCAN_TABLE} s
                            ON s.marketplace = b.marketplace AND s.mkp_order_id = b.mkp_order_id
                        WHERE b.marketplace = %s
                          AND b.mkp_order_id IN ({placeholders})
                          AND b.status IN {INVOICED_STATUSES_SQL}
                          AND s.id IS NULL
                    """, (signal, MKP_MARKETPLACE, *chunk))
                    touched += cursor.rowcount
                    db.commit()

                seeded[signal] = f"{len(order_ids)} con señal / {touched} encoladas"
            except MySQLdb.Error as e:
                # Una señal caída no detiene la corrida: el barrido sigue cubriendo la orden
                db.rollback()
                seeded[signal] = 'ERROR'
                log.warning(f"[{MKP_MARKETPLACE}] No se pudo evaluar la señal {signal}: {e}")

    return seeded


def fetch_due_orders(cursor, db):
    """Órdenes a consultar en esta corrida (o las de ONLY_ORDER_IDS)."""
    columns = """s.id, s.mkp_order_id, s.signal_source, s.reference_date, s.scan_status,
                 b.cfdi_uuid, b.total_amount"""

    if ONLY_ORDER_IDS:
        placeholders = ','.join(['%s'] * len(ONLY_ORDER_IDS))
        cursor.execute(f"""
            INSERT IGNORE INTO {NC_SCAN_TABLE}
                (marketplace, mkp_order_id, signal_source, reference_date, scan_status, next_check_at)
            SELECT %s, b.mkp_order_id, 'MANUAL', UTC_TIMESTAMP(), 'WAITING', UTC_TIMESTAMP()
            FROM finance.mkp_billing_prod b
            WHERE b.marketplace = %s AND b.mkp_order_id IN ({placeholders})
        """, (MKP_MARKETPLACE, MKP_MARKETPLACE, *ONLY_ORDER_IDS))
        db.commit()
        cursor.execute(f"""
            SELECT {columns}
            FROM {NC_SCAN_TABLE} s
            LEFT JOIN finance.mkp_billing_prod b
                ON b.marketplace = s.marketplace AND b.mkp_order_id = s.mkp_order_id
            WHERE s.marketplace = %s AND s.mkp_order_id IN ({placeholders})
        """, (MKP_MARKETPLACE, *ONLY_ORDER_IDS))
        rows = cursor.fetchall()
        found = {str(r[1]) for r in rows}
        missing = [o for o in ONLY_ORDER_IDS if o not in found]
        if missing:
            log.warning(f"[{MKP_MARKETPLACE}] ONLY_ORDER_IDS sin fila en mkp_billing_prod "
                        f"(marketplace {MKP_MARKETPLACE}), se omiten: {missing}")
        return rows

    sweep_filter = "AND s.signal_source <> 'SWEEP'" if SCAN_MODE == 'signals' else ""
    cursor.execute(f"""
        SELECT {columns}
        FROM {NC_SCAN_TABLE} s
        JOIN finance.mkp_billing_prod b
            ON b.marketplace = s.marketplace AND b.mkp_order_id = s.mkp_order_id
        WHERE s.marketplace = %s
          AND s.scan_status IN ('WAITING', 'FOUND_PARTIAL')
          AND s.next_check_at <= UTC_TIMESTAMP()
          {sweep_filter}
        ORDER BY (s.signal_source = 'SWEEP') ASC, s.next_check_at ASC
        LIMIT %s
    """, (MKP_MARKETPLACE, MAX_CALLS_PER_RUN))
    return cursor.fetchall()


def update_scan(cursor, scan_id, scan_status, next_check_at, http_status, reset_reference=False):
    reference_sql = ", reference_date = UTC_TIMESTAMP()" if reset_reference else ""
    cursor.execute(f"""
        UPDATE {NC_SCAN_TABLE}
        SET scan_status = %s, next_check_at = %s, last_http_status = %s,
            check_count = check_count + 1,
            first_checked_at = IFNULL(first_checked_at, UTC_TIMESTAMP()),
            last_checked_at = UTC_TIMESTAMP()
            {reference_sql}
        WHERE id = %s
    """, (scan_status, next_check_at, http_status, scan_id))


# Columnas que el 05 escribe -> largo mínimo (None = no es texto o no importa).
# El largo se valida porque sin sql_mode estricto MySQL TRUNCA en silencio
# (p.ej. 'NEVER_CREDITED_BY_ML' en un VARCHAR(15)) y la cola quedaría corrupta.
SCAN_REQUIRED_COLUMNS = {
    'id': None, 'marketplace': len('MERCADO_LIBRE_OFICIALES'), 'mkp_order_id': 16,
    'signal_source': len('CLAIM_COMPLAINANT'), 'reference_date': None,
    'scan_status': len('NEVER_CREDITED_BY_ML'), 'check_count': None, 'last_http_status': None,
    'first_checked_at': None, 'last_checked_at': None, 'next_check_at': None,
}
NC_REQUIRED_COLUMNS = {
    'marketplace': len('MERCADO_LIBRE_OFICIALES'), 'mkp_order_id': 16, 'cfdi_uuid': 36,
    'related_invoice_uuid': 36, 'tipo_relacion': 2, 'serie': None, 'folio': None, 'rfc_receptor': 13,
    'subtotal': None, 'tax_amount': None, 'total_amount': None, 'fecha_timbrado': None,
    'xml_data': None, 'raw_json': None, 'nc_scope': len('PARTIAL'),
    'status': len('AMOUNT_EXCEEDS_INVOICE'), 'error_log': None,
}
# Llave única de la que depende la idempotencia (INSERT IGNORE / anti-join)
SCAN_UNIQUE_KEY = ['marketplace', 'mkp_order_id']
NC_UNIQUE_KEY   = ['marketplace', 'cfdi_uuid']


def _split_table(full_name):
    if not re.fullmatch(r"[A-Za-z0-9_]+\.[A-Za-z0-9_]+", full_name):
        raise ValueError(f"Nombre de tabla inválido: '{full_name}' (se espera esquema.tabla)")
    return full_name.split('.')


def check_schema(cursor):
    """
    Valida que las tablas de control tengan lo que el 05 necesita. Devuelve
    (errores, avisos, info): con errores la corrida NO debe escribir.
    """
    errors, warnings, info = [], [], {}

    for full_name, required, unique_key in ((NC_SCAN_TABLE, SCAN_REQUIRED_COLUMNS, SCAN_UNIQUE_KEY),
                                            (NC_TABLE, NC_REQUIRED_COLUMNS, NC_UNIQUE_KEY)):
        schema, table = _split_table(full_name)
        cursor.execute("""
            SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH, COLUMN_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """, (schema, table))
        columns = {name.lower(): (max_len, col_type) for name, max_len, col_type in cursor.fetchall()}
        if not columns:
            errors.append(f"{full_name}: la tabla no existe o el usuario no la ve")
            continue
        info[full_name] = columns

        for col, min_len in required.items():
            if col not in columns:
                errors.append(f"{full_name}: falta la columna '{col}'")
            elif min_len and columns[col][0] is not None and columns[col][0] < min_len:
                errors.append(f"{full_name}.{col} es {columns[col][1]}: se necesitan >= {min_len} caracteres")

        cursor.execute("""
            SELECT INDEX_NAME, GROUP_CONCAT(LOWER(COLUMN_NAME) ORDER BY SEQ_IN_INDEX)
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND NON_UNIQUE = 0
            GROUP BY INDEX_NAME
        """, (schema, table))
        unique_keys = [cols.split(',') for _, cols in cursor.fetchall()]
        if unique_key not in unique_keys:
            errors.append(f"{full_name}: falta UNIQUE ({', '.join(unique_key)}); sin ella se duplican registros")

    # El JOIN de cada corrida es por mkp_order_id: con tipos distintos MySQL no usa índices
    cursor.execute("""
        SELECT COLUMN_TYPE FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = 'finance' AND TABLE_NAME = 'mkp_billing_prod' AND COLUMN_NAME = 'mkp_order_id'
    """)
    row = cursor.fetchone()
    billing_type = row[0] if row else None
    for full_name in (NC_SCAN_TABLE, NC_TABLE):
        own = info.get(full_name, {}).get('mkp_order_id')
        if own and billing_type and own[1] != billing_type:
            warnings.append(f"{full_name}.mkp_order_id es {own[1]} y en mkp_billing_prod es {billing_type}: "
                            f"conviene igualarlos (rendimiento del JOIN)")

    cursor.execute("SELECT @@SESSION.sql_mode")
    sql_mode = cursor.fetchone()[0] or ''
    if 'STRICT' not in sql_mode:
        warnings.append(f"sql_mode sin STRICT ('{sql_mode}'): MySQL trunca textos largos sin error")

    return errors, warnings, info


def run_preflight(cursor, session):
    """
    PREFLIGHT_ONLY=true -> NO escribe nada. Revisa esquema, datos de entrada, señales
    y hace UNA llamada real al endpoint de devolución para una orden de prueba.
    Devuelve True si se puede correr el 05.
    """
    ok = True
    log.info("=== PREFLIGHT 05 (solo lectura) ===")
    log.info(f"Tienda: {MKP_MARKETPLACE} (seller {ML_SELLER_ID}) | corte: {ML_BILLING_START} | "
             f"tablas: {NC_SCAN_TABLE}, {NC_TABLE} | .env: {DOTENV_STATUS}")

    errors, warnings, _ = check_schema(cursor)
    for e in errors:
        log.error(f"[ESQUEMA] {e}")
    for w in warnings:
        log.warning(f"[ESQUEMA] {w}")
    if errors:
        ok = False
    else:
        log.info("[ESQUEMA] OK: columnas, largos y llaves únicas")
        for full_name in (NC_SCAN_TABLE, NC_TABLE):
            cursor.execute(f"SELECT COUNT(*) FROM {full_name} WHERE marketplace = %s", (MKP_MARKETPLACE,))
            log.info(f"[ESQUEMA] {full_name}: {cursor.fetchone()[0]} filas actuales de {MKP_MARKETPLACE}")

    # Estados de facturación: detecta valores truncados (p.ej. ALREADY_ODOO_INVOICE)
    cursor.execute("""
        SELECT status, LENGTH(status), COUNT(*) FROM finance.mkp_billing_prod
        WHERE marketplace = %s GROUP BY status ORDER BY COUNT(*) DESC
    """, (MKP_MARKETPLACE,))
    for status, length, count in cursor.fetchall():
        log.info(f"[FACTURACIÓN] {status!s:<25} len={length} -> {count}")
        if status and status.startswith('ALREADY_ODOO_INVOICE') and status != 'ALREADY_ODOO_INVOICED':
            log.warning("[FACTURACIÓN] 'ALREADY_ODOO_INVOICED' llega truncado: la columna status de "
                        "mkp_billing_prod es más corta que el valor. El 05 lo contempla; revisar 03/04.")

    # Señales (mismas queries que el seeding)
    for signal in SIGNALS:
        query = SIGNAL_QUERIES.get(signal)
        if not query:
            log.warning(f"[SEÑALES] '{signal}' no existe")
            continue
        t0 = time.monotonic()
        try:
            cursor.execute(query, (ML_SELLER_ID, ML_BILLING_START))
            log.info(f"[SEÑALES] {signal}: {len(cursor.fetchall())} órdenes ({time.monotonic() - t0:.1f}s)")
        except MySQLdb.Error as e:
            log.warning(f"[SEÑALES] {signal}: ERROR {e} (el barrido cubre esas órdenes)")

    # Llamada real al endpoint para una orden de prueba
    order_id = PREFLIGHT_ORDER_ID or (ONLY_ORDER_IDS[0] if ONLY_ORDER_IDS else "2000018238217954")
    cursor.execute("""
        SELECT status, cfdi_uuid, total_amount FROM finance.mkp_billing_prod
        WHERE marketplace = %s AND mkp_order_id = %s
    """, (MKP_MARKETPLACE, order_id))
    billing = cursor.fetchone()
    log.info(f"[API] Orden {order_id} en mkp_billing_prod: "
             f"{'status=%s uuid=%s total=%s' % billing if billing else 'NO EXISTE (ONLY_ORDER_IDS la ignoraría)'}")
    r = get_devolution_xml(session, order_id, {'consecutive_429': 0})
    if r is None:
        log.error("[API] Sin respuesta (red/timeout)")
        ok = False
    elif r.status_code == 401:
        log.error("[API] 401: token inválido o expirado")
        ok = False
    elif r.status_code == 200:
        try:
            nc = parse_credit_note(r.content)
            status, nc_scope, error_log = classify_credit_note(
                nc, billing[1] if billing else None, float(billing[2] or 0) if billing else 0.0, nc['total'])
            log.info(f"[API] 200 -> NC {nc['uuid']} {nc['serie']}-{nc['folio']} total={nc['total']} "
                     f"relaciona={nc['related_uuids']} -> se guardaría como {status}/{nc_scope}"
                     f"{' (' + error_log + ')' if error_log else ''}")
        except InvalidDocument as e:
            log.warning(f"[API] 200 pero documento inválido: {e}")
    else:
        log.info(f"[API] HTTP {r.status_code}: {r.text[:200]}")

    log.info(f"=== PREFLIGHT {'OK' if ok else 'CON ERRORES'} — no se escribió nada ===")
    return ok


def extract_ml_credit_notes():
    # ── Validación de configuración ────────────────────────────
    missing = [k for k, v in {
        "DB_HOST": DB_HOST, "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD, "DB_NAME": DB_NAME
    }.items() if not v]
    if missing:
        log.error(f"Faltan variables de entorno obligatorias: {', '.join(missing)} (.env: {DOTENV_STATUS})")
        raise SystemExit(1)
    if SCAN_MODE not in ('signals', 'sweep', 'both'):
        log.error(f"SCAN_MODE inválido: '{SCAN_MODE}' (signals | sweep | both)")
        raise SystemExit(1)

    # ── Conexión DB ────────────────────────────────────────────
    db = MySQLdb.connect(
        host=DB_HOST, user=DB_USER,
        passwd=DB_PASSWORD, db=DB_NAME,
        charset='utf8mb4'
    )
    cursor = db.cursor()
    # Sesión estricta: el servidor corre sin STRICT y trunca textos en silencio (así se
    # cortó 'ALREADY_ODOO_INVOICED' en mkp_billing_prod). Con esto un UPDATE que no
    # quepa falla en vez de corromper la cola. (INSERT IGNORE sigue degradándolo a
    # warning; para eso está check_schema.)
    cursor.execute("SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_TRANS_TABLES')")

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
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {row[0][0]}'})

    # ── Preflight / validación de esquema ─────────────────────
    if PREFLIGHT_ONLY:
        ok = run_preflight(cursor, session)
        cursor.close()
        db.close()
        if not ok:
            raise SystemExit(1)
        return

    schema_errors, _, _ = check_schema(cursor)
    if schema_errors:
        for e in schema_errors:
            log.error(f"[ESQUEMA] {e}")
        log.error("Esquema de tablas de control inválido; no se escribe nada. Correr con PREFLIGHT_ONLY=true.")
        cursor.close()
        db.close()
        raise SystemExit(1)

    run_desc = (f"orders (prueba dirigida, {len(ONLY_ORDER_IDS)} órdenes; no se llena la cola)"
                if ONLY_ORDER_IDS else SCAN_MODE)
    log.info(f"[{MKP_MARKETPLACE}] Modo: {run_desc} | tope de consultas a ML: {MAX_CALLS_PER_RUN} | "
             f"tablas: {NC_SCAN_TABLE}, {NC_TABLE}")

    # ── Cola de revisión ───────────────────────────────────────
    if not ONLY_ORDER_IDS:
        seeded = seed_scan_queue(cursor, db)
        log.info(f"[{MKP_MARKETPLACE}] Órdenes encoladas/promovidas (modo {SCAN_MODE}): {seeded}")

    orders = fetch_due_orders(cursor, db)
    log.info(f"[{MKP_MARKETPLACE}] Órdenes a consultar en esta corrida: {len(orders)}")
    if not orders:
        cursor.close()
        db.close()
        return

    # ── Contadores (los detalles por orden NO se loguean: solo el resumen y anomalías) ──
    counters = {
        'nc_new': 0, 'nc_known': 0, 'full': 0, 'partial': 0, 'multi_nc': 0,
        'miss_404': 0, 'closed': 0, 'never_credited': 0, 'invalid': 0,
        'net_errors': 0, 'http_errors': 0, 'db_errors': 0,
    }
    nc_status_counts = {}
    http_by_code = {}
    anomalies_logged = 0
    state = {'consecutive_429': 0}
    token_expired = False
    stop_reason = None
    started = time.monotonic()

    def anomaly(msg):
        nonlocal anomalies_logged
        if anomalies_logged < MAX_ANOMALY_LOGS:
            log.warning(f"[{MKP_MARKETPLACE}] {msg}")
        anomalies_logged += 1

    for scan_id, order_id, signal_source, reference_date, scan_status, invoice_uuid, invoice_total in orders:
        if time.monotonic() - started > MAX_RUNTIME_MINUTES * 60:
            stop_reason = f"tiempo máximo ({MAX_RUNTIME_MINUTES} min)"
            break

        now = datetime.utcnow()
        reference_date = reference_date or now
        invoice_total = float(invoice_total or 0)

        r = get_devolution_xml(session, order_id, state)

        if r is None:
            counters['net_errors'] += 1
            update_scan(cursor, scan_id, scan_status, now + timedelta(hours=ERROR_RETRY_HOURS), None)
            db.commit()
            continue

        if r.status_code == 401:
            token_expired = True
            log.error("Token expirado (401). Abortando script.")
            break

        if r.status_code == 429:
            stop_reason = f"rate limit ({MAX_CONSECUTIVE_429} respuestas 429 seguidas)"
            break

        try:
            if r.status_code == 404:
                # Sin NC en ML (por ahora)
                counters['miss_404'] += 1
                new_status, next_at = next_check_after_miss(signal_source, scan_status, reference_date, now)
                counters['closed'] += new_status in ('CLOSED', 'FOUND')
                counters['never_credited'] += new_status == 'NEVER_CREDITED_BY_ML'
                update_scan(cursor, scan_id, new_status, next_at, 404)
                db.commit()

            elif r.status_code != 200:
                counters['http_errors'] += 1
                # Solo la primera respuesta de cada código se loguea (con su cuerpo recortado)
                if r.status_code not in http_by_code:
                    log.warning(f"[{MKP_MARKETPLACE}] HTTP {r.status_code} en orden {order_id}: {r.text[:300]}")
                http_by_code[r.status_code] = http_by_code.get(r.status_code, 0) + 1
                update_scan(cursor, scan_id, scan_status, now + timedelta(hours=ERROR_RETRY_HOURS), r.status_code)
                db.commit()

            else:
                try:
                    nc = parse_credit_note(r.content)
                except InvalidDocument as e:
                    counters['invalid'] += 1
                    anomaly(f"Orden {order_id}: documento inválido en endpoint de devolución -> {e}")
                    update_scan(cursor, scan_id, 'INVALID_DOCUMENT', None, 200)
                    db.commit()
                    continue

                # NC previas de la orden: detectar la misma NC otra vez y llevar el acumulado
                cursor.execute(f"""
                    SELECT cfdi_uuid, total_amount FROM {NC_TABLE}
                    WHERE marketplace = %s AND mkp_order_id = %s
                """, (MKP_MARKETPLACE, order_id))
                previous = {u.upper(): float(t or 0) for u, t in cursor.fetchall()}
                is_new = nc['uuid'] not in previous
                cumulative = sum(previous.values()) + (nc['total'] if is_new else 0)

                if is_new:
                    if previous:
                        counters['multi_nc'] += 1
                        anomaly(f"Orden {order_id}: NC adicional {nc['uuid']} (${nc['total']:,.2f}); "
                                f"previas: {list(previous)}")

                    status, nc_scope, error_log = classify_credit_note(nc, invoice_uuid, invoice_total, cumulative)
                    cursor.execute(f"""
                        INSERT IGNORE INTO {NC_TABLE}
                        (marketplace, mkp_order_id, cfdi_uuid, related_invoice_uuid, tipo_relacion, serie, folio,
                         rfc_receptor, subtotal, tax_amount, total_amount, fecha_timbrado, xml_data, raw_json,
                         nc_scope, status, error_log)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (MKP_MARKETPLACE, order_id, nc['uuid'], nc['related_uuids'][0], nc['tipo_relacion'],
                          nc['serie'], nc['folio'], nc['rfc_receptor'], nc['subtotal'], nc['tax_amount'],
                          nc['total'], nc['fecha_timbrado'], base64.b64encode(r.content).decode('utf-8'),
                          json.dumps(xml_to_dict(nc['root']), ensure_ascii=False), nc_scope, status, error_log))

                    if cursor.rowcount == 0:
                        # Mismo UUID ya registrado bajo OTRA orden: no se sobreescribe, se reporta
                        anomaly(f"Orden {order_id}: la NC {nc['uuid']} ya existe en staging ligada a otra orden.")
                        update_scan(cursor, scan_id, 'INVALID_DOCUMENT', None, 200)
                        db.commit()
                        counters['invalid'] += 1
                        continue

                    counters['nc_new'] += 1
                    counters['full' if nc_scope == 'FULL' else 'partial'] += 1
                    nc_status_counts[status] = nc_status_counts.get(status, 0) + 1
                else:
                    counters['nc_known'] += 1

                if invoice_total > 0 and cumulative >= invoice_total - AMOUNT_TOLERANCE:
                    # La factura quedó cubierta: no hay más NC que esperar
                    update_scan(cursor, scan_id, 'FOUND', None, 200)
                elif is_new:
                    # NC parcial: se sigue revisando por si ML emite otra (backoff desde hoy)
                    update_scan(cursor, scan_id, 'FOUND_PARTIAL', now + timedelta(days=1), 200,
                                reset_reference=True)
                else:
                    new_status, next_at = next_check_after_miss(signal_source, 'FOUND_PARTIAL', reference_date, now)
                    counters['closed'] += new_status == 'FOUND'
                    update_scan(cursor, scan_id, new_status, next_at, 200)
                db.commit()

        except MySQLdb.Error as e:
            db.rollback()
            counters['db_errors'] += 1
            log.error(f"[{MKP_MARKETPLACE}] Error de BD en orden {order_id}: {e}")

        time.sleep(SLEEP_BETWEEN_CALLS)  # Respetar rate limits de ML

    # ── Resumen único de la corrida ────────────────────────────
    total_errors = counters['net_errors'] + counters['http_errors'] + counters['db_errors']
    log.info(
        f"[{MKP_MARKETPLACE}] Resumen -> Consultadas: {len(orders)} | "
        f"NC nuevas: {counters['nc_new']} {nc_status_counts} (totales={counters['full']}, "
        f"parciales={counters['partial']}) | NC ya conocidas: {counters['nc_known']} | "
        f"Órdenes con NC adicional: {counters['multi_nc']} | Sin NC (404): {counters['miss_404']} | "
        f"Cerradas: {counters['closed']} | NEVER_CREDITED_BY_ML: {counters['never_credited']} | "
        f"Documentos inválidos: {counters['invalid']} | "
        f"Errores: {total_errors} (red={counters['net_errors']}, http={counters['http_errors']} "
        f"{http_by_code}, bd={counters['db_errors']})"
    )
    if anomalies_logged > MAX_ANOMALY_LOGS:
        log.warning(f"[{MKP_MARKETPLACE}] {anomalies_logged - MAX_ANOMALY_LOGS} anomalías adicionales no se loguearon.")
    if stop_reason:
        log.warning(f"[{MKP_MARKETPLACE}] Corrida cortada por {stop_reason}; la siguiente corrida retoma la cola.")
    cursor.close()
    db.close()

    # Si el token murió a media corrida, la tarea debe fallar para que Kestra reintente/alerte
    if token_expired:
        raise SystemExit(1)


if __name__ == "__main__":
    extract_ml_credit_notes()
