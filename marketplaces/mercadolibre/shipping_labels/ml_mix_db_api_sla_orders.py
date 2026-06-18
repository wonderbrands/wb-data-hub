import os
import requests
import MySQLdb
from datetime import datetime, timedelta
import logging
import concurrent.futures
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from zoneinfo import ZoneInfo
from dateutil.parser import isoparse


logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

CDMX = ZoneInfo("America/Mexico_City")
SELLER_ID = '25523702'

def now_cdmx():
    return datetime.now(CDMX)


def parse_ml_datetime_to_cdmx(value):
    if not value: return None
    dt = isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(CDMX)


def get_db_connection():
    #load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')
    load_dotenv()
    return MySQLdb.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        passwd=os.getenv("DB_PASSWORD"), db=os.getenv("DB_NAME"),
        local_infile=True, charset='utf8mb4'
    )


def get_ml_token(db):
    cursor = db.cursor()
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = %s", (SELLER_ID,))
    token = cursor.fetchone()[0]
    if isinstance(token, bytes):
        return token.decode('utf-8')
    return str(token)


def get_requests_session(access_token):
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {access_token}", "User-Agent": "WonderBrands/1.0"})
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def get_override_config(db):
    cursor = db.cursor(MySQLdb.cursors.DictCursor)
    try:
        cursor.execute(
            "SELECT force_print_days_ahead, active_until, ml_order_lookback_days, cutoff_time_cdmx FROM tools.ml_print_controls WHERE id = 1")
        row = cursor.fetchone()

        #backup
        if not row:
            return {'override_days': 0, 'lookback_days': 10, 'cutoff_time': 15}

        lookback_days = int(row.get('ml_order_lookback_days') or 10)
        days_ahead = int(row.get('force_print_days_ahead') or 0)
        cutoff_time = int(row.get('cutoff_time_cdmx') or 14)  #default 14 (2 PM)
        active_until = row.get('active_until')

        if not active_until:
            return {'override_days': 0, 'lookback_days': lookback_days, 'cutoff_time': cutoff_time}

        active_until_cdmx = active_until.replace(
            tzinfo=CDMX) if active_until.tzinfo is None else active_until.astimezone(CDMX)

        if now_cdmx() <= active_until_cdmx:
            return {'override_days': days_ahead, 'lookback_days': lookback_days, 'cutoff_time': cutoff_time}

        return {'override_days': 0, 'lookback_days': lookback_days, 'cutoff_time': cutoff_time}
    except Exception as e:
        log.warning(f"Error config: {e}")
        return {'override_days': 0, 'lookback_days': 10, 'cutoff_time': 15}

def get_odoo_carrier_mapping(logistic_type, tracking_method):
    logistic_map = {'cross_docking': 'Colecta', 'self_service': 'Flex', 'drop_off': 'Drop Off',
                    'xd_drop_off': 'Cross Docking con Drop Off'}
    carrier_ref_data = logistic_map.get(logistic_type, 'Desconocido')
    carrier_id = None
    if tracking_method:
        t_upper = tracking_method.upper()
        if 'MEL' in t_upper or 'MERCADO LIBRE' in t_upper:
            carrier_id = 10
        elif 'FEDEX' in t_upper:
            carrier_id = 1
        elif 'PAQUETEXPRESS' in t_upper:
            carrier_id = 4
        elif 'J&T' in t_upper:
            carrier_id = 19
        elif 'ALMEX' in t_upper:
            carrier_id = 25
        elif 'Flex' in carrier_ref_data:
            carrier_id = 21 #logistica interna LOIN

    return carrier_ref_data, carrier_id


def classify_sla(expected_date_cdmx, override_days, cutoff_time):
    if not expected_date_cdmx:
        return "IGNORE"

    now = now_cdmx()
    today_date = now.date()
    expected_date = expected_date_cdmx.date()

    if expected_date <= today_date:
        return "TODAY"
    if expected_date == today_date + timedelta(days=1):
        # Usamos el cutoff_time dinámico de la BD
        if now.hour >= cutoff_time:
            return "NEXT_DAY_LABEL_CREATED_EARLY"
        return "FUTURE_DAYS"

    if override_days > 0 and expected_date <= today_date + timedelta(days=override_days):
        return "FORCED_BY_WAREHOUSE"

    return "FUTURE_DAYS"


def process_sla_concurrent(row, session, override_days, cutoff_time):
    """Llama a la API de SLA para una orden validada localmente y la prepara para DB."""
    shipping_id = str(row['shipping_id'])
    url_sla = f"https://api.mercadolibre.com/shipments/{shipping_id}/sla"

    try:
        sla_resp = session.get(url_sla, timeout=(5, 10))
        if sla_resp.status_code != 200:
            return None

        sla_data = sla_resp.json()
        expected_date_cdmx = parse_ml_datetime_to_cdmx(sla_data.get('expected_date'))

        #Pasamos el cutoff_time a la función de clasificación
        sla_status = classify_sla(expected_date_cdmx, override_days, cutoff_time)

        #Limpiamos el valor que viene de la BD local
        raw_pack = row.get('pack_id')
        clean_pack_id = None if raw_pack in ('None', 'NULL', '', None) else str(raw_pack)
        mkp_reference = clean_pack_id if clean_pack_id else str(row['order_id'])

        carrier_ref, carrier_id = get_odoo_carrier_mapping(row['l_type'], row['t_method'])

        #Asignamos el estatus de impresión dependiendo de la clasificación del SLA
        p_status = 'READY_TO_PRINT' if sla_status in ['TODAY', 'NEXT_DAY_LABEL_CREATED_EARLY',
                                                      'FORCED_BY_WAREHOUSE'] else 'FUTURE_WAITING'

        return {
            'order_id': str(row['order_id']),
            'mkp_ref': mkp_reference,
            'pack_id': clean_pack_id,
            'o_status': row['o_status'],
            'shipping_id': shipping_id,
            's_status': row['s_status'],
            's_substatus': row['s_substatus'],
            'l_type': row['l_type'],
            't_method': row['t_method'],
            'sla_date': expected_date_cdmx,
            'sla_class': sla_status,
            'c_ref': carrier_ref,
            'c_id': carrier_id,
            'p_status': p_status
        }
    except Exception as e:
        log.warning(f"Error consultando SLA para shipping {shipping_id}: {e}")
        return None


def run_etl_hybrid(db, token):
    config = get_override_config(db)
    session = get_requests_session(token)
    cursor = db.cursor(MySQLdb.cursors.DictCursor)

    log.info(f"Corte CDMX configurado a las {config['cutoff_time']}:00 horas. (Adelantamiento de guias de 'MAÑANA')")

    #cargar cache de BD (ahorra peticiones SLA)
    cursor.execute("""
        SELECT marketplace_reference, processed_successfully, print_status, ml_sla_expected_date 
        FROM tools.ml_api_etl_orders
    """)
    cache = {row['marketplace_reference']: row for row in cursor.fetchall()}
    log.info(f"Caché cargada: {len(cache)} órdenes en historial.")

    #candidatos locales
    date_from = (now_cdmx() - timedelta(days=config['lookback_days'])).strftime('%Y-%m-%d %H:%M:%S')

    query_extract = f"""
        SELECT 
            o.order_id, o.pack_id, o.status AS o_status, o.shipping_id, 
            s.status AS s_status, s.substatus AS s_substatus, 
            s.logistic_type AS l_type, s.tracking_method AS t_method
        FROM somos_reyes.ml_order_update o
        JOIN somos_reyes.ml_shipping s ON o.shipping_id = s.shipping_id
        WHERE o.seller_id = '{SELLER_ID}'
          AND o.status = 'paid'
          AND s.status = 'ready_to_ship' AND s.substatus = 'ready_to_print'
          AND o.last_updated >= %s
    """
    cursor.execute(query_extract, (date_from,))
    raw_orders = cursor.fetchall()

    #Filtra candidatos usando la caché
    candidates_to_api = []
    skipped_count = 0

    for row in raw_orders:
        raw_pack = row.get('pack_id')
        clean_pack_id = None if raw_pack in ('None', 'NULL', '', None) else str(raw_pack)
        mkp_ref = clean_pack_id if clean_pack_id else str(row['order_id'])

        cached = cache.get(mkp_ref)
        if cached:
            if cached['processed_successfully'] == 1:
                skipped_count += 1
                continue

            if cached['print_status'] == 'FUTURE_WAITING' and cached['ml_sla_expected_date']:
                saved_date = cached['ml_sla_expected_date']
                if saved_date.tzinfo is None:
                    saved_date = saved_date.replace(tzinfo=CDMX)

                re_eval_sla = classify_sla(saved_date, config['override_days'], config['cutoff_time'])
                if re_eval_sla == 'FUTURE_DAYS':
                    skipped_count += 1
                    continue

        candidates_to_api.append(row)

    log.info(
        f"De {len(raw_orders)} órdenes, omitimos {skipped_count} por caché. {len(candidates_to_api)} van a la API de ML.")

    #concurrencia en api
    valid_orders = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_sla_concurrent, row, session, config['override_days'], config['cutoff_time'])
                   for row in
                   candidates_to_api]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                valid_orders.append(res)

    #Inserción / Actualización en BD
    insert_cursor = db.cursor()
    procesadas = 0
    for item in valid_orders:
        query_insert = """
            INSERT INTO tools.ml_api_etl_orders 
            (order_id, marketplace_reference, pack_id, ml_order_status, ml_shipping_id, ml_shipping_status, ml_shipping_substatus, 
             ml_logistic_type, ml_tracking_method, ml_sla_expected_date, sla_classification, odoo_carrier_ref, odoo_carrier_id, print_status) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
            ml_order_status=VALUES(ml_order_status), ml_shipping_id=VALUES(ml_shipping_id), ml_shipping_status=VALUES(ml_shipping_status), 
            ml_shipping_substatus=VALUES(ml_shipping_substatus), ml_sla_expected_date=VALUES(ml_sla_expected_date), 
            sla_classification=VALUES(sla_classification), odoo_carrier_ref=VALUES(odoo_carrier_ref), odoo_carrier_id=VALUES(odoo_carrier_id),
            print_status=IF(processed_successfully=1, print_status, VALUES(print_status))
        """
        insert_cursor.execute(query_insert, (
            item['order_id'], item['mkp_ref'], item['pack_id'], item['o_status'], item['shipping_id'],
            item['s_status'], item['s_substatus'], item['l_type'], item['t_method'], item['sla_date'],
            item['sla_class'], item['c_ref'], item['c_id'], item['p_status']
        ))
        procesadas += 1

    db.commit()
    log.info(f"Proceso ETL completado. {procesadas} órdenes insertadas/actualizadas (Listas o Futuras).")


if __name__ == "__main__":
    db = None
    try:
        db = get_db_connection()
        ml_token = get_ml_token(db)
        run_etl_hybrid(db, ml_token)
    except Exception as e:
        log.error(f"Error en ejecución: {e}")
    finally:
        if db:
            db.close()