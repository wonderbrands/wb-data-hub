import os
import sys
import time as tm
import logging
from datetime import datetime, timedelta
import xmlrpc.client
import mysql.connector
from mysql.connector import Error

__description__ = """
    **** V18 - FACTURACIÓN 1 A 1 OPTIMIZADA (KESTRA COMPATIBLE) ****
"""

# --- CONFIGURACIÓN DE LOGS PARA DOCKER / KESTRA ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[
    logging.StreamHandler(sys.stdout)
])
log = logging.getLogger(__name__)

# --- CONFIGURACIÓN CONTABLE GLOBAL ---
TAX_ID_MARKETPLACES = 38
PARTNER_ID_PUBLICO_GENERAL = 13436

# =======================================================================
# CONFIGURACIÓN DE PRUEBAS 
TEST_ORDER_LIMIT = 4000  # None -> histórico.
# =======================================================================

# --- VARIABLES DE ENTORNO (Inyectadas por Kestra) ---
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USER = os.getenv('ODOO_USER')
ODOO_PWD = os.getenv('ODOO_PASSWORD')

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'finance')

ERROR_502_COUNTER = 0  

def get_db_connection():
    try:
        return mysql.connector.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
        )
    except Error as e:
        log.error(f"Error conectando a la BD de auditoría: {e}")
        return None

def insert_audit_record(order_name, order_id, team_name):
    conn = get_db_connection()
    if not conn: return None
    try:
        cursor = conn.cursor()
        query = """
            INSERT INTO finance.billing_audit_log (odoo_order_name, odoo_order_id, team_name, status, attempt_count) 
            VALUES (%s, %s, %s, 'PROCESSING', 1)
            ON DUPLICATE KEY UPDATE 
                status = 'PROCESSING',
                attempt_count = attempt_count + 1,
                updated_at = CURRENT_TIMESTAMP
        """
        cursor.execute(query, (order_name, order_id, team_name))
        conn.commit()
        cursor.execute("SELECT id FROM finance.billing_audit_log WHERE odoo_order_id = %s", (order_id,))
        record = cursor.fetchone()
        return record[0] if record else None
    except Error as e:
        log.error(f"Error insertando/actualizando auditoría para {order_name}: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()
    return None

def update_audit_record(record_id, status, error_type='NONE', error_log=None, invoice_name=None, invoice_id=None, automation_status='NONE'):
    if not record_id: return
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        invoiced_at = datetime.now() if status == 'SUCCESS' else None
        query = """UPDATE finance.billing_audit_log 
                   SET status = %s, error_type = %s, error_log = %s, 
                       invoice_name = %s, invoice_id = %s, invoice_automation_status = %s, invoiced_at = %s
                   WHERE id = %s"""
        cursor.execute(query, (status, error_type, error_log, invoice_name, invoice_id, automation_status, invoiced_at, record_id))
        conn.commit()
    except Error as e:
        log.error(f"Error actualizando auditoría ID {record_id}: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

def reset_stuck_processing_records():
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE finance.billing_audit_log 
            SET status = 'ERROR', error_type = '502_BAD_GATEWAY', error_log = 'Proceso interrumpido abruptamente (reseteado por limpieza inicial)' 
            WHERE status = 'PROCESSING'
        """)
        affected = cursor.rowcount
        conn.commit()
        if affected:
            log.warning(f"Limpieza inicial: {affected} registro(s) trabados en 'PROCESSING' regresados a 'ERROR' para reintento.")
    except Error as e:
        log.error(f"No se pudo ejecutar la limpieza de registros 'PROCESSING': {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

def get_failed_order_ids():
    conn = get_db_connection()
    if not conn: return []
    try:
        cursor = conn.cursor()
        query = """
            SELECT DISTINCT odoo_order_id 
            FROM finance.billing_audit_log 
            WHERE status = 'ERROR' 
              AND IFNULL(invoice_automation_status, '') != 'STAMPED'
              AND IFNULL(attempt_count, 0) <= 5
              AND updated_at >= (CURRENT_TIMESTAMP - INTERVAL 30 DAY)
        """
        cursor.execute(query)
        records = cursor.fetchall()
        return [r[0] for r in records if r[0]]
    except Error as e:
        log.error(f"Error consultando órdenes fallidas en BD: {e}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

def get_account_id(models, db, uid, pwd, code):
    acc = models.execute_kw(db, uid, pwd, 'account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"¡ALERTA! No se encontró la cuenta contable con código {code} en Odoo.")
    return acc[0]

UTC_local = -6
today_date_datetime = datetime.now()
today_date = today_date_datetime.strftime("%Y-%m-%d %H:%M:%S")

# ── Proxy y Transporte de Resiliencia ante Errores 502 Bad Gateway ──
class TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=300, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn

class OdooModelProxy:
    def __init__(self, url, db, user, pwd, timeout=300, max_retries_init=3, delay_init=2):
        global ERROR_502_COUNTER
        self.url = url
        self.db = db
        self.user = user
        self.pwd = pwd
        self.timeout = timeout
        self.max_retries_init = max_retries_init
        self.delay_init = delay_init

        for attempt in range(1, self.max_retries_init + 1):
            try:
                transport = TimeoutTransport(timeout=self.timeout)
                self.common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common', transport=transport)
                self.uid = self.common.authenticate(db, user, pwd, {})
                
                transport_models = TimeoutTransport(timeout=self.timeout)
                self.models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object', transport=transport_models)
                log.info(f"Conectado a Odoo correctamente (intento {attempt}).")
                return  
            except (xmlrpc.client.ProtocolError, TimeoutError, OSError, ConnectionError) as e:
                log.warning(f"Error de red/Timeout en conexión inicial (intento {attempt}/{self.max_retries_init}): {e}")
                ERROR_502_COUNTER += 1
                if attempt == self.max_retries_init:
                    raise  
                tm.sleep(self.delay_init * attempt)
            except Exception as e:
                log.error(f"Error fatal en conexión inicial (no reintentable): {e}")
                raise

    def reauthenticate(self):
        log.info("Cerrando sesión TLS y abriendo una nueva conexión con Odoo...")
        transport = TimeoutTransport(timeout=self.timeout)
        self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common', transport=transport)
        self.uid = self.common.authenticate(self.db, self.user, self.pwd, {})
        
        transport_models = TimeoutTransport(timeout=self.timeout)
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object', transport=transport_models)

    def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None, max_retries=3, delay=2):
        global ERROR_502_COUNTER
        for attempt in range(1, max_retries + 1):
            try:
                if kwargs is not None:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args, kwargs)
                else:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args)
            except xmlrpc.client.Fault as e:
                raise e
            except (xmlrpc.client.ProtocolError, TimeoutError, OSError) as e:
                ERROR_502_COUNTER += 1
                log.warning(f"Error de red/Timeout en Odoo [{model}.{method}]: {str(e)}. Intento {attempt}/{max_retries}...")
                if attempt == max_retries:
                    raise e
                tm.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception as auth_e:
                    log.warning(f"Error al reautenticar con Odoo: {str(auth_e)}")
            except Exception as e:
                log.warning(f"Error de comunicación en Odoo [{model}.{method}]. Intento {attempt}/{max_retries}: {str(e)}")
                ERROR_502_COUNTER += 1
                if attempt == max_retries:
                    raise e
                tm.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception:
                    pass

def get_chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def main():
    conections_count = 0
    reset_stuck_processing_records()
    while True:
        try:
            run()
            break  
        except ConnectionResetError as e:
            conections_count += 1
            if conections_count < 3:
                log.error(f"Error de conexion: {e}. Reintentando...")
                tm.sleep(5)  
            else:
                raise e

def run():
    global uid, models, today_date, orders_list_not_serialize_message, invoice_date_first_of_month, last_day_of_year_flag, acc_cxc_amazon, acc_cxc_walmart, acc_cxc_walmart_1p, acc_cxc_coppel, acc_cxc_elektra, acc_cxc_tiktok, acc_cxc_mayoreo

    models = OdooModelProxy(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD, timeout=300)
    uid = models.uid
    log.info('Conexion con Odoo establecida (via OdooModelProxy)')

    acc_cxc_amazon = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.006')
    acc_cxc_walmart = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.007')
    acc_cxc_walmart_1p = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.008')
    acc_cxc_coppel = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.009')
    acc_cxc_elektra = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.010')
    acc_cxc_tiktok = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.011')
    acc_cxc_mayoreo = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.012')

    formated_date = today_date.split(' ')[0].split('-') 

    if int(formated_date[2]) == 1: 
        start_date = (today_date_datetime - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")  
        end_date = (today_date_datetime - timedelta(days=1)).strftime("%Y-%m-%d")  
        delta_days = False
        invoice_date_first_of_month = end_date
        last_day_of_year_flag = True if end_date.endswith("-12-31") else False
    else: 
        start_date = today_date_datetime.replace(day=1).strftime("%Y-%m-%d")  
        end_date = today_date.split(' ')[0]  
        delta_days = True
        invoice_date_first_of_month = None
        last_day_of_year_flag = False

    log.info("Iniciando busqueda de ordenes con mensaje 'serialize'...")
    t0 = tm.time()
    orders_list_not_serialize_message = search_sales_with_message(start_date, end_date)
    log.info(f"Terminado en {round(tm.time()-t0, 2)}s. Encontradas: {len(orders_list_not_serialize_message)}")

    log.info("Iniciando busqueda de stock insuficiente...")
    t1 = tm.time()
    search_sales_with_stock_insufficient_message(start_date, end_date)
    log.info(f"Terminado en {round(tm.time()-t1, 2)}s.")

    all_records = []
    date_range = generate_date_range(start_date, end_date)

    log.info(f"Extrayendo ordenes diarias desde {start_date} hasta {end_date}...")
    for number_day, single_date in enumerate(date_range):
        t_day = tm.time()
        day_start, day_end = adjust_to_cdmx_time(single_date)
        day_records = fetch_records(day_start, day_end)
        all_records.extend(day_records)
        log.debug(f"Dia {number_day + 1} ({single_date.strftime('%Y-%m-%d')}): {len(day_records)} registros recuperados")

    failed_ids = get_failed_order_ids()
    if failed_ids:
        existing_ids = {r['id'] for r in all_records}
        missing_failed_ids = [oid for oid in failed_ids if oid not in existing_ids]
        if missing_failed_ids:
            log.info(f"Recuperando {len(missing_failed_ids)} ordenes con errores previos en BD para reanudar su facturacion...")
            for chunk in get_chunks(missing_failed_ids, 500):
                try:
                    failed_records = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read', [[('id', 'in', chunk)]])
                    all_records.extend(failed_records)
                except Exception as e:
                    log.error(f"Error extrayendo ordenes fallidas: {e}")

    log.info(f'Total de registros extraidos antes de filtros: {len(all_records)}')
    process_records(all_records, delta_days, failed_ids)
    log.info('PROCESO DE FACTURACION TERMINADO')
    log.info(f"ERROR_502_COUNTER FINAL: {ERROR_502_COUNTER}")

def generate_date_range(start_date, end_date):
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    return [start_dt + timedelta(days=x) for x in range((end_dt - start_dt).days + 1)]

def get_current_year_cdmx():
    current_year = datetime.now().year
    first_day_of_year = datetime(current_year, 1, 1)
    last_day_of_year = datetime(current_year, 12, 31, 23, 59, 59)
    first_day_of_year_cdmx, last_day_of_year_cdmx = adjust_to_cdmx_time(first_day_of_year, last_day_of_year)
    if last_day_of_year_flag:
        return first_day_of_year_cdmx.replace(year=first_day_of_year_cdmx.year - 1), last_day_of_year_cdmx.replace(year=last_day_of_year_cdmx.year - 1)
    return first_day_of_year_cdmx, last_day_of_year_cdmx

def adjust_to_cdmx_time(first_date, last_day = None):
    start_date = first_date - timedelta(hours=UTC_local)
    end_date = start_date + timedelta(hours=24) if not last_day else last_day - timedelta(hours=UTC_local)
    return start_date, end_date

def fetch_records(day_start, day_end):
    so_domain = [('invoice_status', '=', 'to invoice'), ('locked', '=', 'True'), ('date_order', '>=', day_start), ('date_order', '<=', day_end)]
    try:
        return models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read', [so_domain])
    except Exception as e:
        log.error(f"Error fetch_records: {e}")
        return []

def process_records(records, delta_days, failed_ids=None):
    global today_date, invoice_date_first_of_month, procees_invoices_start_time
    today_date = datetime.strptime(today_date, '%Y-%m-%d %H:%M:%S') if isinstance(today_date, str) else today_date

    teams_dict = {}
    cutoff_ml = datetime(2026, 6, 1, 0, 0, 0)
    
    skipped_ml = 0
    skipped_grace = 0

    for record in records:
        is_failed_retry = (failed_ids and record['id'] in failed_ids)
        if record['invoice_status'] == 'to invoice' or is_failed_retry: 
            order_date_str = record.get('date_order', False)
            if not order_date_str: continue
            
            real_order_date = datetime.strptime(order_date_str, '%Y-%m-%d %H:%M:%S')
            difference_days = (today_date - real_order_date).days
            team_name = record['team_id'][1]

            if 'MercadoLibre' in team_name and real_order_date >= cutoff_ml: 
                skipped_ml += 1
                continue

            grace_days = 1 
            if not delta_days or (delta_days and difference_days >= grace_days):
                teams_dict.setdefault(team_name, []).append(record)
            else:
                skipped_grace += 1

    log.info(f"Filtros: {skipped_ml} excluidas por ML, {skipped_grace} excluidas por periodo de gracia (1 dia).")

    walmart_removed = teams_dict.pop('Team_Walmart', None)
    facebook_removed = teams_dict.pop('Salderos / Facebook', None) 
    
    if walmart_removed: log.info(f"Excluidas {len(walmart_removed)} de Team_Walmart.")
    if facebook_removed: log.info(f"Excluidas {len(facebook_removed)} de Salderos / Facebook.")
    
    if TEST_ORDER_LIMIT:
        valid_count = 0
        for team, orders in list(teams_dict.items()):
            remaining = TEST_ORDER_LIMIT - valid_count
            if remaining <= 0:
                teams_dict[team] = []
            elif len(orders) > remaining:
                teams_dict[team] = orders[:remaining]
                valid_count += remaining
            else:
                valid_count += len(orders)
        
        teams_dict = {k: v for k, v in teams_dict.items() if v}
        log.info(f"Limite aplicado: Se procesaran {valid_count} ordenes validas en total.")
        
    procees_invoices_start_time = tm.time()
    for team_name, orders_list in teams_dict.items():
        log.info(f"Procesando equipo: {team_name} ({len(orders_list)} ordenes)")
        invoiced_qty = execute_invoice(team_name, orders_list)
        if invoiced_qty > 0:
            log.info(f"-> {invoiced_qty} ordenes facturadas exitosamente de {team_name}.")

def execute_invoice(team_name, orders_list):
    global acc_cxc_amazon, acc_cxc_walmart, acc_cxc_walmart_1p, acc_cxc_coppel, acc_cxc_elektra, acc_cxc_tiktok, acc_cxc_mayoreo
    if not orders_list: return 0
        
    team_id = orders_list[0]['team_id'][0] 
    total_orders = len(orders_list)
    success_count = 0

    acc_cxc_team = None
    if 'Amazon' in team_name: acc_cxc_team = acc_cxc_amazon
    elif 'Walmart_1P' in team_name or '1P' in team_name: acc_cxc_team = acc_cxc_walmart_1p
    elif 'Walmart' in team_name: acc_cxc_team = acc_cxc_walmart
    elif 'Coppel' in team_name: acc_cxc_team = acc_cxc_coppel
    elif 'Elektra' in team_name: acc_cxc_team = acc_cxc_elektra
    elif 'TikTok' in team_name: acc_cxc_team = acc_cxc_tiktok
    elif 'Mayoreo' in team_name: acc_cxc_team = acc_cxc_mayoreo

    order_names = [order['name'] for order in orders_list]
    existing_invoices_data = []
    for chunk in get_chunks(order_names, 500):
        data = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'search_read', 
                                 [[('invoice_origin', 'in', chunk), ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')]], 
                                 {'fields': ['id', 'invoice_origin', 'name', 'state', 'l10n_mx_edi_cfdi_uuid']})
        existing_invoices_data.extend(data)
    
    invoiced_origins = {inv['invoice_origin']: inv for inv in existing_invoices_data if inv['invoice_origin']}

    all_line_ids = [line_id for order in orders_list for line_id in order['order_line']]
    all_lines_data = []
    for chunk in get_chunks(all_line_ids, 1000):
        data = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order.line', 'search_read', [[('id', 'in', chunk)]])
        all_lines_data.extend(data)
        
    lines_dict = {line['id']: line for line in all_lines_data}

    for index, order in enumerate(orders_list): 
        order_name = order['name']
        order_id = order['id']
        
        audit_id = insert_audit_record(order_name, order_id, team_name)
        
        inv_id = None
        real_invoice_name = None
        needs_creation = True
        needs_post = True
        needs_stamping = True

        if order_name in invoiced_origins:
            existing_inv = invoiced_origins[order_name]
            inv_id = existing_inv['id']
            inv_state = existing_inv['state']
            inv_name = existing_inv.get('name')
            inv_uuid = existing_inv.get('l10n_mx_edi_cfdi_uuid')
            
            is_stamped = bool(inv_uuid and inv_uuid != 'False')
            
            if inv_state == 'posted' and is_stamped:
                log.warning(f"BUCLE EVITADO: {order_name} YA TIENE la factura timbrada {inv_name}. Se ignorará.")
                update_audit_record(audit_id, status='IGNORED_DUPLICATE', error_log=f"Ya tiene factura timbrada {inv_name}", invoice_name=inv_name, invoice_id=inv_id, automation_status='STAMPED')
                continue
            elif inv_state == 'posted' and not is_stamped:
                log.info(f"Reanudando orden {order_name}: Factura {inv_name} ya confirmada. Continuando con timbrado SAT...")
                real_invoice_name = inv_name
                needs_creation = False
                needs_post = False
                needs_stamping = True
            elif inv_state == 'draft' or inv_name in (False, 'False'):
                log.info(f"Reanudando orden {order_name}: Factura en borrador (ID: {inv_id}). Continuando desde confirmación...")
                needs_creation = False
                needs_post = True
                needs_stamping = True
            else:
                log.warning(f"BUCLE EVITADO: {order_name} YA TIENE la factura {inv_name} en estado {inv_state}. Se ignorará.")
                update_audit_record(audit_id, status='IGNORED_DUPLICATE', error_log=f"Ya tiene factura {inv_name}")
                continue

        if (order['state'] == 'sale' and order['locked']) or (order_name in orders_list_not_serialize_message):
            if (order['invoice_status'] == 'to invoice' and order['invoice_count'] == 0) or not needs_creation:
                try:
                    if needs_creation:
                        invoice_line_vals_list = []
                        abortar_orden = False
                        
                        for line_id in order['order_line']:
                            line = lines_dict.get(line_id)
                            if not line: continue

                            qty_ordered = line['product_uom_qty']
                            qty_invoiced = line['qty_invoiced']
                            qty_delivered = line['qty_delivered']
                            
                            product_name = line['product_id'][1].upper() if line.get('product_id') else ""
                            is_shipping = 'C-ENVIO' in product_name

                            if is_shipping and qty_delivered < qty_ordered:
                                try:
                                    models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order.line', 'write', [[line['id']], {'qty_delivered': qty_ordered}])
                                    qty_delivered = qty_ordered  
                                except Exception as e:
                                    log.error(f"No se pudo actualizar la cantidad entregada de C-ENVIO para {order_name}: {e}")

                            if not is_shipping and qty_delivered < qty_ordered:
                                log.debug(f"Orden {order_name} ignorada: Falta entrega física.")
                                update_audit_record(audit_id, status='IGNORED_NO_STOCK', error_type='VALIDATION_ERROR', error_log=f"Falta entrega: Ord {qty_ordered}, Entr {qty_delivered}")
                                abortar_orden = True
                                break 

                            if qty_invoiced >= qty_ordered: continue

                            tax_ids = [(6, 0, [TAX_ID_MARKETPLACES])] if line.get('tax_id') else False
                            invoice_line_vals_list.append((0, 0, {
                                'display_type': line.get('display_type') or 'product',
                                'sequence': int(line['sequence']) if line.get('sequence') else 10,
                                'name': line['name'],
                                'product_uom_id': line['product_uom'][0] if line.get('product_uom') else False,
                                'product_id': line['product_id'][0] if line.get('product_id') else False,
                                'quantity': qty_ordered, 
                                'discount': line['discount'],
                                'price_unit': line['price_unit'],
                                'tax_ids': tax_ids,
                                'sale_line_ids': [(4, line['id'])],
                            }))

                        if invoice_line_vals_list and not abortar_orden:
                            invoice_vals = {
                                'ref': '', 'move_type': 'out_invoice', 'partner_id': PARTNER_ID_PUBLICO_GENERAL, 
                                'invoice_origin': order_name, 'invoice_line_ids': invoice_line_vals_list,
                                'l10n_mx_edi_usage': 'S01', 'l10n_mx_edi_payment_method_id': 3, 
                                'l10n_mx_edi_payment_policy': 'PUE', 'team_id': team_id,
                            }
                            if invoice_date_first_of_month: invoice_vals['invoice_date'] = invoice_date_first_of_month

                            try:
                                inv_id = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'create', [invoice_vals])
                            except xmlrpc.client.Fault as e_create:
                                log.error(f"Fallo al CREAR factura {order_name}: {e_create.faultString}")
                                update_audit_record(audit_id, status='ERROR', error_type='CREATION_ERROR', error_log=e_create.faultString)
                                continue 
                            except Exception as e_create:
                                log.error(f"Error al CREAR factura {order_name}: {e_create}")
                                update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY' if '502' in str(e_create) else 'CREATION_ERROR', error_log=str(e_create))
                                continue
                        else:
                            continue

                    if needs_post and inv_id:
                        try:
                            if needs_creation and acc_cxc_team:
                                move_lines = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move.line', 'search_read', 
                                                               [[('move_id', '=', inv_id)]], {'fields': ['id', 'account_type']})
                                lines_to_update = [(1, m_line['id'], {'account_id': acc_cxc_team}) for m_line in move_lines if m_line['account_type'] == 'asset_receivable']
                                if lines_to_update:
                                    models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'write', [[inv_id], {'line_ids': lines_to_update}])
                            
                            if needs_creation:
                                models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'message_post', [inv_id], {'body': f'Factura 1 a 1 para {order_name}. Creada vía API.', 'message_type': 'comment'})
                            
                            models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'action_post', [[inv_id]])    
                            
                            inv_data = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'read', [[inv_id]], {'fields': ['name']})
                            real_invoice_name = inv_data[0]['name'] if inv_data else str(inv_id)
                            
                        except xmlrpc.client.Fault as e_post:
                            log.error(f"Fallo al CONFIRMAR factura {order_name}: {e_post.faultString}")
                            update_audit_record(audit_id, status='ERROR', error_type='POSTING_ERROR', error_log=e_post.faultString, invoice_id=inv_id, automation_status='DRAFT')
                            continue
                        except Exception as e_post:
                            log.error(f"Error al CONFIRMAR factura {order_name}: {e_post}")
                            update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY' if '502' in str(e_post) else 'POSTING_ERROR', error_log=str(e_post), invoice_id=inv_id, automation_status='DRAFT')
                            continue

                    if needs_stamping and inv_id:
                        try:
                            if not real_invoice_name:
                                inv_data = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move', 'read', [[inv_id]], {'fields': ['name']})
                                real_invoice_name = inv_data[0]['name'] if inv_data else str(inv_id)

                            wizard_context = {'active_model': 'account.move', 'active_ids': [inv_id]}
                            wizard_id = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move.send.wizard', 'create', [{'is_download_only': False}], {'context': wizard_context})
                            models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'account.move.send.wizard', 'action_send_and_print', [[wizard_id]], {'context': wizard_context})
                            
                            update_audit_record(audit_id, status='SUCCESS', invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='STAMPED')
                            
                            success_count += 1
                            log.info(f"[{success_count}/{total_orders}] Factura individual {real_invoice_name} creada/reanudada y timbrada para {order_name}")
                            tm.sleep(0.3)
                            
                        except xmlrpc.client.Fault as e_stamp:
                            log.error(f"Fallo al TIMBRAR factura de {order_name}: {e_stamp.faultString}")
                            update_audit_record(audit_id, status='ERROR', error_type='STAMPING_ERROR', error_log=e_stamp.faultString, invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='POSTED')
                            continue
                        except Exception as e_stamp:
                            log.error(f"Error al TIMBRAR factura de {order_name}: {e_stamp}")
                            update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY' if '502' in str(e_stamp) else 'STAMPING_ERROR', error_log=str(e_stamp), invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='POSTED')
                            continue

                except Exception as e:
                    log.error(f"Error general procesando orden {order_name}: {e}")
                    update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY' if '502' in str(e) else 'UNKNOWN_ERROR', error_log=str(e))
                    continue
                    
    return success_count

def search_sales_with_message(start_day, end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        first_day_of_year_cdmx, last_day_of_year_cdmx = get_current_year_cdmx()
        domain = [('state', '=', 'sale'), ('effective_date', '>=', first_day), ('effective_date', '<=', last_day), 
                  ('message_ids.body', 'ilike', 'serialize'), ('effective_date', 'ilike', '-'),
                  ('create_date', '>=', first_day_of_year_cdmx), ('create_date', '<=', last_day_of_year_cdmx)]
        sales_orders = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read', [domain], {'fields': ['name'], 'limit':0})
        return [order_name['name'] for order_name in sales_orders]
    except Exception as e:
        log.error(f"Error en query serialize: {e}")
        return []

def search_sales_with_stock_insufficient_message(start_day, end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        domain = [('state', '=', 'sale'), ('message_ids.body', 'ilike', 'insufficient stock 0'),
                  ('date_order', '>=', first_day), ('date_order', '<=', last_day), ('invoice_count', '<', '2')]
        sales_orders = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read', [domain], {'fields': ['name'], 'limit': 0})
        return [order['name'] for order in sales_orders]
    except Exception as e:
        log.error(f"Error en query stock insuficiente: {e}")
        return []

if __name__ == '__main__':
    log.info('================================================================')
    log.info('BIENVENIDO AL PROCESO DE FACTURACIÓN PARA MARKETPLACES (1 a 1)')
    if TEST_ORDER_LIMIT:
        log.info(f'MODO PRUEBA ACTIVADO: Límite de {TEST_ORDER_LIMIT} órdenes VÁLIDAS')
    log.info('================================================================')
    
    start_time = tm.time()
    try:
        main()
        log.info(f'Tiempo de ejecución TOTAL: {round(tm.time() - start_time, 2)} segundos')
    except Exception as e:
        log.error(f"Fallo crítico en el proceso de facturación: {str(e)}")
        sys.exit(1)