from datetime import datetime, timedelta
import xmlrpc.client
import time as tm
import logging
import gspread
import os
from dotenv import load_dotenv

__description__ = """
                **** V18 - FACTURACIÓN 1 A 1 (CANTIDAD ORDENADA) ****

- Facturación individual (1 Factura por cada Orden de Venta).
- Política de facturación basada en Cantidad Ordenada.
- Se fuerza el Impuesto de IVA Cobrado para Marketplaces (ID 38).
- * EXCLUSIÓN DE ML como parte de la facturación a partir del 1ro de junio 2026. *
"""

#dotenv_path = "/var/lib/jenkins/m1/.env"
#credentials_json = '/var/lib/jenkins/m1/credenciales_reportes.json'

#dotenv_path = 'C:/Users/Sergio Gil Guerrero/Documents/WonderBrands/Repos/wonderbrands/.env'
credentials_json = '/var/lib/credentials/credenciales_reportes.json'

#load_dotenv()

# --- CONFIGURACIÓN CONTABLE GLOBAL ---
# ID del impuesto "IVA 16% Marketplaces" (Apunta a IVA Cobrado, sin Base de Efectivo)
TAX_ID_MARKETPLACES = 38 # 37 staging / 38 prod 
PARTNER_ID_PUBLICO_GENERAL = 13436 # 13436 staging / 13436 prod

# Subir archivo a Google Sheets
def insert_log_in_sheets(_path,file_id):
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

# Función para eliminar archivo local
def delete_file(file_path):
    if os.path.exists(file_path):
        os.remove(file_path)
        print(f'Archivo {file_path} eliminado.')
    else:
        print(f'El archivo {file_path} no existe.')

# Obtener fecha
UTC_local = -6
today_date_datetime = datetime.now()
today_date = today_date_datetime.strftime("%Y-%m-%d %H:%M:%S")
today_date_for_log = today_date_datetime + timedelta(hours=UTC_local)
today_date_for_log = today_date_for_log.strftime("%Y-%m-%d -- %H-%M-%S")

# Configurar el logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(f'{today_date_for_log}.log')
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logging.Formatter.converter = lambda *args: tm.localtime(tm.time() + UTC_local * 3600)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

class TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=200):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn

logging.info('================================================================')
logging.info('BIENVENIDO AL PROCESO DE FACTURACIÓN PARA MARKETPLACES (1 a 1)')
logging.info('================================================================')

dir_path = os.path.dirname(os.path.realpath(__file__))
logging.info('Fecha local: ' + today_date_for_log)
logging.info('Fecha UTC: ' + today_date)

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

    logging.info('----------------------------------------------------------------')
    logging.info(f'Conectando API Odoo V18: {db_name}')
    transport = TimeoutTransport(timeout=200)
    common = xmlrpc.client.ServerProxy('{}/xmlrpc/2/common'.format(server_url), transport=transport, allow_none=True, use_datetime=True)
    uid = common.authenticate(db_name, username, password, {})
    models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(server_url), transport=transport, allow_none=True, use_datetime=True)
    logging.info('Conexión con Odoo establecida')
    logging.info('----------------------------------------------------------------')

    formated_date = today_date.split(' ')[0].split('-') 

    if int(formated_date[2]) == 1: 
        start_date = (today_date_datetime - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")  
        end_date = (today_date_datetime - timedelta(days=1)).strftime("%Y-%m-%d")  
        delta_days = False
        invoice_date_first_of_month = end_date
        last_day_of_year_flag = True if end_date.endswith("-12-31") else False
        orders_list_not_serialize_message = search_sales_with_message(start_date, end_date)
    else: 
        start_date = today_date_datetime.replace(day=1).strftime("%Y-%m-%d")  
        end_date = today_date.split(' ')[0]  
        delta_days = True
        invoice_date_first_of_month = None
        last_day_of_year_flag = False
        orders_list_not_serialize_message = search_sales_with_message(start_date, end_date)

    orders_stock_insufficient = search_sales_with_stock_insufficient_message(start_date, end_date)
    logging.info('----------------------------------------------------------------')
    logging.info(f'ÓRDENES CON STOCK INSUFICIENTE (Informativo): Total: {len(orders_stock_insufficient)}')
    logging.info('----------------------------------------------------------------')

    all_records = []
    date_range = generate_date_range(start_date, end_date)

    logging.info('Resultados de llamadas a la API de Odoo')
    logging.info('-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*')
    for number_day, single_date in enumerate(date_range):
        day_start, day_end = adjust_to_cdmx_time(single_date)
        day_records = fetch_records(day_start, day_end)
        logging.info(f'Número de órdenes en el día {number_day + 1}: {len(day_records)}')
        all_records.extend(day_records)

    process_records(all_records, delta_days)
    logging.info('PROCESO DE FACTURACIÓN TERMINADO')
    logging.info('********************************************************')

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
        first_day_prev_year = first_day_of_year_cdmx.replace(year=first_day_of_year_cdmx.year - 1)
        last_day_prev_year = last_day_of_year_cdmx.replace(year=last_day_of_year_cdmx.year - 1)
        return first_day_prev_year, last_day_prev_year
    else:
        return first_day_of_year_cdmx, last_day_of_year_cdmx

def adjust_to_cdmx_time(first_date, last_day = None):
    if last_day == None:
        start_date = first_date - timedelta(hours=UTC_local)
        end_date = start_date + timedelta(hours=24)
    else:
        start_date = first_date - timedelta(hours=UTC_local)
        end_date = last_day - timedelta(hours=UTC_local)
    return start_date, end_date

def fetch_records(day_start, day_end):
    so_domain = [
        ('invoice_status', '=', 'to invoice'),
        ('locked', '=', 'True'),  # Validamos que esté confirmada y bloqueada
        ('date_order', '>=', day_start), # Usamos la fecha real de venta
        ('date_order', '<=', day_end),
    ]
    try:
        records = models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [so_domain])
        return records
    except xmlrpc.client.Fault as e:
        logging.error(f"Error XML-RPC: {e}")
        return []
    except Exception as e:
        logging.error(f"Error fetch_records: {e}")
        return []

def process_records(records, delta_days):
    global today_date, invoice_date_first_of_month
    today_date = datetime.strptime(today_date, '%Y-%m-%d %H:%M:%S')

    teams_dict = {}
    logging.info(f'Tamaño total de records en estado "to invoice": {len(records)}')

    cutoff_ml = datetime(2026, 6, 1, 0, 0, 0)
    orders_invoiced_list = []
    
    for record in records:
        if record['invoice_status'] == 'to invoice': 
            #Extraemos date_order en lugar de effective_date
            # Usamos .get() por si acaso viniera vacío (aunque en estado sale no debería)
            order_date_str = record.get('date_order', False)
            if not order_date_str:
                continue # Si no tiene fecha de orden por alguna anomalía, lo saltamos

            #Odoo devuelve date_order en formato string 'YYYY-MM-DD HH:MM:SS'
            real_order_date = datetime.strptime(order_date_str, '%Y-%m-%d %H:%M:%S')
            difference_days = (today_date - real_order_date).days
            
            team_name = record['team_id'][1]

            # EXCLUSIÓN MERCADO LIBRE evaluado con la fecha real de la venta
            if 'MercadoLibre' in team_name and real_order_date >= cutoff_ml:
                continue

            grace_days = 1 

            if delta_days == True:  
                if difference_days >= grace_days:
                    if team_name not in teams_dict: teams_dict[team_name] = []
                    teams_dict[team_name].append(record)
            else:  
                if team_name not in teams_dict: teams_dict[team_name] = []
                teams_dict[team_name].append(record)
        else:
            orders_invoiced_list.append(record['name'])

    try:
        teams_dict.pop('Team_Walmart')
        teams_dict.pop('Salderos / Facebook') 
    except KeyError:
        pass

    for team_name, orders_list in teams_dict.items():
        logging.info('********************************************************')
        logging.info(f"Procesando el equipo: {team_name} con {len(orders_list)} órdenes.")
        execute_invoice(team_name, orders_list)

def execute_invoice(team_name, orders_list):
    if not orders_list:
        return
        
    team_id = orders_list[0]['team_id'][0] 
    logging.info(f'Iniciando facturación 1 a 1 para {team_name} (Team ID: {team_id})')

    total_orders = len(orders_list)
    success_count = 0

    for index, order in enumerate(orders_list): 
        order_line_id = order['order_line']
        order_name = order['name']
        order_state = order['state']
        is_locked = order['locked']
        order_inv_count = order['invoice_count']

        if (order_state == 'sale' and is_locked == True) or (order_name in orders_list_not_serialize_message):
            # Validamos estrictamente que invoice_count sea 0 para evitar el bug de las SOs editadas por CS
            if order['invoice_status'] == 'to invoice' and order_inv_count == 0:
                
                try:
                    # ============ ANTI-DUPLICADOS (Búsqueda dura en Contabilidad) ============ 
                    existing_invoice = models.execute_kw(db_name, uid, password, 'account.move', 'search_read', 
                                              [[('invoice_origin', 'ilike', order_name), 
                                                ('move_type', '=', 'out_invoice'), 
                                                ('state', '!=', 'cancel')]], 
                                              {'fields': ['id', 'name'], 'limit': 1})
                
                    if existing_invoice:
                        logging.warning(f"BUCLE EVITADO: La orden {order_name} fue modificada, pero YA TIENE la factura {existing_invoice[0]['name']} viva en Odoo. Se ignorará.")
                        continue # Saltamos a la siguiente orden
                    
                    # ===========================================================================
                    
                    sale_order_line = models.execute_kw(db_name, uid, password, 'sale.order.line', 'search_read',[[['id', 'in', order_line_id]]])
                    
                    invoice_line_vals_list = []
                    abortar_orden = False
                    
                    for line in sale_order_line:
                        qty_ordered = line['product_uom_qty']
                        qty_invoiced = line['qty_invoiced']
                        qty_delivered = line['qty_delivered']
                        
                        # Manejo seguro del nombre del producto para detectar envíos
                        try:
                            product_name = line['product_id'][1].upper()
                        except:
                            product_name = ""
                            
                        is_shipping = 'C-ENVIO' in product_name

                        # EL CANDADO PARA AMAZON/WALMART/OTROS MARKETPLACES
                        # Si no es envío, y la cantidad entregada es menor a la ordenada, ABORTAMOS toda la orden.
                        # No se facturará hasta que el almacén entregue todo.
                        if not is_shipping and qty_delivered < qty_ordered:
                            logging.warning(f"Orden {order_name} ignorada: Falta entrega física (Ordenado: {qty_ordered}, Entregado: {qty_delivered})")
                            abortar_orden = True
                            break # Rompemos el ciclo de líneas de esta orden

                        # Saltar si ya facturamos todo lo ordenado (Seguro extra)
                        if qty_invoiced >= qty_ordered:
                            continue

                        # FORZAR IMPUESTO 37 (IVA COBRADO MARKETPLACES) SI TIENE IMPUESTO ASIGNADO
                        tax_ids = [(6, 0, [TAX_ID_MARKETPLACES])] if line.get('tax_id') else False

                        invoice_line_vals = {
                            'display_type': line.get('display_type') or 'product',
                            'sequence': int(line['sequence']) if line.get('sequence') else 10,
                            'name': line['name'],
                            'product_uom_id': line['product_uom'][0] if line.get('product_uom') else False,
                            'product_id': line['product_id'][0] if line.get('product_id') else False,
                            'quantity': qty_ordered,  # USAMOS CANTIDAD ORDENADA PARA TODOS
                            'discount': line['discount'],
                            'price_unit': line['price_unit'],
                            'tax_ids': tax_ids,
                            'sale_line_ids': [(4, line['id'])],
                        }
                        invoice_line_vals_list.append((0, 0, invoice_line_vals))

                    # Si el candado no saltó y tenemos líneas para facturar, procedemos.
                    if invoice_line_vals_list and not abortar_orden:
                        
                        # 1 FACTURA POR ORDEN
                        invoice_vals = {
                            'ref': '',
                            'move_type': 'out_invoice',
                            'partner_id': PARTNER_ID_PUBLICO_GENERAL, 
                            'invoice_origin': order_name,  
                            'invoice_line_ids': invoice_line_vals_list,
                            'l10n_mx_edi_usage': 'S01',  
                            'l10n_mx_edi_payment_method_id': 3, 
                            'l10n_mx_edi_payment_policy': 'PUE',
                            'team_id': team_id,
                        }
                        
                        if invoice_date_first_of_month != None:
                            invoice_vals['invoice_date'] = invoice_date_first_of_month

                        # Crear Factura
                        invoice_id = models.execute_kw(db_name, uid, password, 'account.move', 'create', [invoice_vals])

                        # Mensajes en el Chatter
                        models.execute_kw(db_name, uid, password, 'account.move', 'message_post', [invoice_id], 
                                          {'body': f'Factura 1 a 1 para {order_name} / {team_name}. Creada por Data vía API.', 'message_type': 'comment'})

                        # Publicar
                        models.execute_kw(db_name, uid, password, 'account.move', 'action_post', [[invoice_id]])    

                        # Timbrar (account.move.send.wizard)
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
        start_day = datetime.strptime(start_day, '%Y-%m-%d')
        end_day = datetime.strptime(end_day, '%Y-%m-%d')
        first_day, last_day = adjust_to_cdmx_time(start_day, end_day)
        first_day_of_year_cdmx, last_day_of_year_cdmx = get_current_year_cdmx()

        domain = [
            ('state', '=', 'sale'), 
            ('effective_date', '>=', first_day),  
            ('effective_date', '<=', last_day), 
            ('message_ids.body', 'ilike', 'serialize'),
            ('effective_date', 'ilike', '-'),
            ('create_date', '>=', first_day_of_year_cdmx),
            ('create_date', '<=', last_day_of_year_cdmx)
        ]

        sales_orders = models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [domain], {'fields': ['name'], 'limit':0})
        sales_orders_list = [order_name['name'] for order_name in sales_orders]
        return sales_orders_list
    except Exception as e:
        print("Error al buscar las órdenes de venta:", e)
        return []

def search_sales_with_stock_insufficient_message(start_day, end_day):
    # Este método ahora es meramente informativo. Ya no bloquea la facturación.
    try:
        start_day = datetime.strptime(start_day, '%Y-%m-%d')
        end_day = datetime.strptime(end_day, '%Y-%m-%d')
        first_day, last_day = adjust_to_cdmx_time(start_day, end_day)

        domain = [
            ('state', '=', 'sale'),
            ('message_ids.body', 'ilike', 'insufficient stock 0'),
            ('date_order', '>=', first_day),
            ('date_order', '<=', last_day),
            ('invoice_count', '<', '2'),
        ]

        sales_orders = models.execute_kw(db_name, uid, password, 'sale.order', 'search_read', [domain], {'fields': ['name', 'order_line'], 'limit': 0})
        
        # Lógica omitida por brevedad en visualización, pero mantenemos compatibilidad
        sales_orders_list = [order['name'] for order in sales_orders]
        return sales_orders_list
    except Exception as e:
        print("Error al buscar las órdenes de venta con stock insuficiente:", e)
        return []

if __name__ == '__main__':
    try:
        start_time = tm.time()
        main()
        end_time = tm.time()
        elapsed_time = round(end_time - start_time, 2)
        logging.info(f'El tiempo de ejecución de este script fue de: {elapsed_time} segundos')
    except KeyboardInterrupt:
        pass
    finally:
        for handler in logger.handlers:
            handler.close()
            logger.removeHandler(handler)

        log_file_path = f'{today_date_for_log}.log'
        file_id = '1y6dgwAk0uHmJ7DPVTVTb5ZiGom1gK0nQem0LAz6C9Zs'
        insert_log_in_sheets(log_file_path, file_id=file_id)
        delete_file(log_file_path)