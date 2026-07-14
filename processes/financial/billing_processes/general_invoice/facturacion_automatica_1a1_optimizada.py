from datetime import datetime, timedelta
import xmlrpc.client
import time as tm
import logging
import gspread
import os
from dotenv import load_dotenv

__description__ = """
                **** V18 - FACTURACIÓN 1 A 1 (CANTIDAD ORDENADA) OPTIMIZADA & DEBUG ****
"""

dotenv_path = 'C:/Users/Sergio Gil Guerrero/Documents/WonderBrands/Repos/wonderbrands/.env'
load_dotenv(dotenv_path)
credentials_json = r'C:\Users\Sergio Gil Guerrero\PycharmProjects\Herramientas propias\Invoices\google_cred.json'

# PROD KESTRA
#credentials_json = '/var/lib/credentials/credenciales_reportes.json'

# --- CONFIGURACIÓN CONTABLE GLOBAL ---
TAX_ID_MARKETPLACES = 38
PARTNER_ID_PUBLICO_GENERAL = 13436

# =======================================================================
# ⚙️ CONFIGURACIÓN DE PRUEBAS 
TEST_ORDER_LIMIT = 1  # Cambia a 10, 100, o ponlo en None para correr histórico.
# =======================================================================

def insert_log_in_sheets(_path, file_id):
    print("Actualizando GOOGLE DRIVE...")
    try:
        gc = gspread.service_account(filename=credentials_json)
        sh = gc.open_by_key(file_id)
        worksheet = sh.worksheet('log')
        log_filename = _path
        with open(log_filename, 'r') as file:
            lines = file.readlines()
            lines.reverse() 
        current_data = worksheet.get_all_values()
        updated_data = [[line.strip()] for line in lines] + current_data
        worksheet.clear()
        worksheet.update('A1', updated_data)
    except Exception as e:
        print(f"Error subiendo log a Sheets: {e}")

def delete_file(file_path):
    if os.path.exists(file_path):
        os.remove(file_path)

UTC_local = -6
today_date_datetime = datetime.now()
today_date = today_date_datetime.strftime("%Y-%m-%d %H:%M:%S")
today_date_for_log = today_date_datetime + timedelta(hours=UTC_local)
today_date_for_log = today_date_for_log.strftime("%Y-%m-%d -- %H-%M-%S")

logger = logging.getLogger()
logger.setLevel(logging.DEBUG) # Cambiado a DEBUG para mayor detalle
file_handler = logging.FileHandler(f'{today_date_for_log}.log')
file_handler.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logging.Formatter.converter = lambda *args: tm.localtime(tm.time() + UTC_local * 3600)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=300): # Aumentado a 300 por si acaso
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn

def get_chunks(lst, n):
    """Divide una lista en sublistas de tamaño n para no saturar XML-RPC"""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

logging.info('================================================================')
logging.info('BIENVENIDO AL PROCESO DE FACTURACIÓN PARA MARKETPLACES (1 a 1)')
if TEST_ORDER_LIMIT:
    logging.info(f'MODO PRUEBA ACTIVADO: Límite de {TEST_ORDER_LIMIT} órdenes VÁLIDAS')
logging.info('================================================================')

def main():
    conections_count = 0
    while True:
        try:
            run()
            break  
        except ConnectionResetError as e:
            conections_count += 1
            if conections_count < 3:
                logging.error(f"Error de conexión: {e}. Reintentando...")
                tm.sleep(5)  
            else:
                break

def run():
    global uid, models, db_name, password, today_date, orders_list_not_serialize_message, invoice_date_first_of_month, last_day_of_year_flag

    server_url = os.getenv('odoo_urlV18')
    db_name = os.getenv('odoo_dbV18')
    username = os.getenv('odoo_user_dataV18')
    password = os.getenv('odoo_password_dataV18')

    transport = TimeoutTransport(timeout=300)
    common = xmlrpc.client.ServerProxy('{}/xmlrpc/2/common'.format(server_url), transport=transport, allow_none=True, use_datetime=True)
    uid = common.authenticate(db_name, username, password, {})
    models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(server_url), transport=transport, allow_none=True, use_datetime=True)
    logging.info('✅ Conexión con Odoo establecida')

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

    # Medición de las consultas pesadas
    logging.info("Iniciando búsqueda de órdenes con mensaje 'serialize'...")
    t0 = tm.time()
    orders_list_not_serialize_message = search_sales_with_message(start_date, end_date)
    logging.info(f"Terminado en {round(tm.time()-t0, 2)}s. Encontradas: {len(orders_list_not_serialize_message)}")

    logging.info("Iniciando búsqueda de stock insuficiente...")
    t1 = tm.time()
    search_sales_with_stock_insufficient_message(start_date, end_date)
    logging.info(f"Terminado en {round(tm.time()-t1, 2)}s.")

    all_records = []
    date_range = generate_date_range(start_date, end_date)

    logging.info(f"Extrayendo órdenes diarias desde {start_date} hasta {end_date}...")
    for number_day, single_date in enumerate(date_range):
        t_day = tm.time()
        day_start, day_end = adjust_to_cdmx_time(single_date)
        day_records = fetch_records(day_start, day_end)
        all_records.extend(day_records)
        logging.debug(f"Día {number_day + 1} ({single_date.strftime('%Y-%m-%d')}): {len(day_records)} registros recuperados en {round(tm.time()-t_day, 2)}s")

    logging.info(f'Total de registros extraídos antes de filtros: {len(all_records)}')
    process_records(all_records, delta_days)
    logging.info('PROCESO DE FACTURACIÓN TERMINADO')

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
        return models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [so_domain])
    except Exception as e:
        logging.error(f"Error fetch_records: {e}")
        return []

def process_records(records, delta_days):
    global today_date, invoice_date_first_of_month
    today_date = datetime.strptime(today_date, '%Y-%m-%d %H:%M:%S') if isinstance(today_date, str) else today_date

    teams_dict = {}
    cutoff_ml = datetime(2026, 6, 1, 0, 0, 0)
    
    skipped_ml = 0
    skipped_grace = 0

    for record in records:
        if record['invoice_status'] == 'to invoice': 
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

    logging.info(f"Filtros: {skipped_ml} excluidas por ML, {skipped_grace} excluidas por periodo de gracia (1 día).")

    # Exclusión de equipos
    walmart_removed = teams_dict.pop('Team_Walmart', None)
    facebook_removed = teams_dict.pop('Salderos / Facebook', None) 
    if walmart_removed: logging.info(f"Excluidas {len(walmart_removed)} de Team_Walmart.")
    if facebook_removed: logging.info(f"Excluidas {len(facebook_removed)} de Salderos.")

    # APLICAR LÍMITE DE PRUEBA SOBRE ÓRDENES VÁLIDAS
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
        
        # Limpiar equipos vacíos
        teams_dict = {k: v for k, v in teams_dict.items() if v}
        logging.info(f"Límite aplicado: Se procesarán {valid_count} órdenes válidas en total.")

    for team_name, orders_list in teams_dict.items():
        logging.info(f"Procesando equipo: {team_name} ({len(orders_list)} órdenes)")
        execute_invoice(team_name, orders_list)

def execute_invoice(team_name, orders_list):
    if not orders_list: return
        
    team_id = orders_list[0]['team_id'][0] 
    total_orders = len(orders_list)
    success_count = 0

    # =========================================================================
    # OPTIMIZACIÓN 1: BÚSQUEDA DE DUPLICADOS EN CHUNKS DE 500
    # =========================================================================
    order_names = [order['name'] for order in orders_list]
    existing_invoices_data = []
    for chunk in get_chunks(order_names, 500):
        data = models.execute_kw(db_name, uid, password, 'account.move', 'search_read', 
                                    [[('invoice_origin', 'in', chunk), ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')]], 
                                    {'fields': ['invoice_origin', 'name']})
        existing_invoices_data.extend(data)
    
    invoiced_origins = {inv['invoice_origin']: inv['name'] for inv in existing_invoices_data if inv['invoice_origin']}

    # =========================================================================
    # OPTIMIZACIÓN 2: LECTURA DE LÍNEAS DE ORDEN EN CHUNKS DE 1000
    # =========================================================================
    all_line_ids = [line_id for order in orders_list for line_id in order['order_line']]
    all_lines_data = []
    
    for chunk in get_chunks(all_line_ids, 1000):
        data = models.execute_kw(db_name, uid, password, 'sale.order.line', 'search_read', [[('id', 'in', chunk)]])
        all_lines_data.extend(data)
        
    lines_dict = {line['id']: line for line in all_lines_data}

    for index, order in enumerate(orders_list): 
        order_name = order['name']
        
        if order_name in invoiced_origins:
            logging.warning(f"BUCLE EVITADO: {order_name} YA TIENE la factura {invoiced_origins[order_name]}. Se ignorará.")
            continue

        if (order['state'] == 'sale' and order['locked']) or (order_name in orders_list_not_serialize_message):
            if order['invoice_status'] == 'to invoice' and order['invoice_count'] == 0:
                try:
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

                        if not is_shipping and qty_delivered < qty_ordered:
                            logging.debug(f"Orden {order_name} ignorada: Falta entrega física (Ordenado: {qty_ordered}, Entregado: {qty_delivered})")
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

                        # Crear, Publicar y Timbrar
                        invoice_id = models.execute_kw(db_name, uid, password, 'account.move', 'create', [invoice_vals])
                        models.execute_kw(db_name, uid, password, 'account.move', 'message_post', [invoice_id], 
                                          {'body': f'Factura 1 a 1 para {order_name}. Creada vía API.', 'message_type': 'comment'})
                        models.execute_kw(db_name, uid, password, 'account.move', 'action_post', [[invoice_id]])    
                        
                        wizard_context = {'active_model': 'account.move', 'active_ids': [invoice_id]}
                        wizard_id = models.execute_kw(db_name, uid, password, 'account.move.send.wizard', 'create', [{'is_download_only': False}], {'context': wizard_context})
                        models.execute_kw(db_name, uid, password, 'account.move.send.wizard', 'action_send_and_print', [[wizard_id]], {'context': wizard_context})

                        success_count += 1
                        logging.info(f"[{success_count}/{total_orders}] ✅ Factura individual creada y timbrada para {order_name}")
                        tm.sleep(0.3)

                except Exception as e:
                    logging.error(f"❌ Error al procesar la orden {order_name}: {e}")
                    continue

def search_sales_with_message(start_day,end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        first_day_of_year_cdmx, last_day_of_year_cdmx = get_current_year_cdmx()
        domain = [('state', '=', 'sale'), ('effective_date', '>=', first_day), ('effective_date', '<=', last_day), 
                  ('message_ids.body', 'ilike', 'serialize'), ('effective_date', 'ilike', '-'),
                  ('create_date', '>=', first_day_of_year_cdmx), ('create_date', '<=', last_day_of_year_cdmx)]
        sales_orders = models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [domain], {'fields': ['name'], 'limit':0})
        return [order_name['name'] for order_name in sales_orders]
    except Exception as e:
        logging.error(f"Error en query serialize: {e}")
        return []

def search_sales_with_stock_insufficient_message(start_day, end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        domain = [('state', '=', 'sale'), ('message_ids.body', 'ilike', 'insufficient stock 0'),
                  ('date_order', '>=', first_day), ('date_order', '<=', last_day), ('invoice_count', '<', '2')]
        sales_orders = models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [domain], {'fields': ['name'], 'limit': 0})
        return [order['name'] for order in sales_orders]
    except Exception as e:
        logging.error(f"Error en query stock insuficiente: {e}")
        return []

if __name__ == '__main__':
    try:
        start_time = tm.time()
        main()
        logging.info(f'Tiempo de ejecución total: {round(tm.time() - start_time, 2)} segundos')
    except KeyboardInterrupt:
        pass
    finally:
        for handler in logger.handlers:
            handler.close()
            logger.removeHandler(handler)
        log_file_path = f'{today_date_for_log}.log'
        insert_log_in_sheets(log_file_path, file_id='1y6dgwAk0uHmJ7DPVTVTb5ZiGom1gK0nQem0LAz6C9Zs')
        delete_file(log_file_path)