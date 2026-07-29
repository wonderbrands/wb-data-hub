import os
import sys
import gc
import time as tm
import logging
from datetime import datetime, timedelta
import xmlrpc.client
import mysql.connector
from mysql.connector import Error

__description__ = """
    **** V18 - FACTURACION 1 A 1 OPTIMIZADA (KESTRA COMPATIBLE) ****
    **** Version con perfil de memoria plano (streaming / chunking) ****
    
    Automatiza la facturación 1 a 1 de marketplaces en Odoo 18 mediante procesamiento
    incremental por día y por lotes, optimizando el consumo de memoria sin alterar la
    lógica contable. Incluye reintentos automáticos ante errores 502/Timeout, auditoría
    de ejecución, asignación de cuentas por marketplace, timbrado automático y gestión
    eficiente de órdenes fallidas, preservando la integridad del proceso de facturación.
 
"""

# --- CONFIGURACION DE LOGS PARA DOCKER / KESTRA ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[
    logging.StreamHandler(sys.stdout)
])
log = logging.getLogger(__name__)

# --- CONFIGURACION CONTABLE GLOBAL ---
TAX_ID_MARKETPLACES = 38
PARTNER_ID_PUBLICO_GENERAL = 13436

# =======================================================================
# CONFIGURACION DE PRUEBAS
TEST_ORDER_LIMIT = 2000  # None -> historico.
# =======================================================================

# --- TAMANOS DE LOTE (controlan el techo de memoria) ---
ORDER_LINE_BATCH_SIZE = 100      # ordenes por lote al descargar sale.order.line
FAILED_ORDERS_BATCH_SIZE = 500   # ordenes fallidas por lote (search_read por id)
LOG_PROGRESS_EVERY = 50          # cada cuantas ordenes se imprime avance
MESSAGE_QUERY_SAFETY_LIMIT = 20000  # limite de seguridad para queries de mensajes

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

UTC_local = -6


# =======================================================================
# AUDITORIA MYSQL (INTACTA - misma firma, mismo comportamiento)
# =======================================================================

def get_db_connection():
    try:
        return mysql.connector.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
        )
    except Error as e:
        log.error(f"Error conectando a la BD de auditoria: {e}")
        return None


def insert_audit_record(order_name, order_id, team_name):
    conn = get_db_connection()
    if not conn:
        return None
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
        log.error(f"Error insertando/actualizando auditoria para {order_name}: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()
    return None


def update_audit_record(record_id, status, error_type='NONE', error_log=None, invoice_name=None, invoice_id=None, automation_status='NONE'):
    if not record_id:
        return
    conn = get_db_connection()
    if not conn:
        return
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
        log.error(f"Error actualizando auditoria ID {record_id}: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def reset_stuck_processing_records():
    conn = get_db_connection()
    if not conn:
        return
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
    if not conn:
        return []
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
        log.error(f"Error consultando ordenes fallidas en BD: {e}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_account_id(models, db, uid, pwd, code):
    acc = models.execute_kw(db, uid, pwd, 'account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"iALERTA! No se encontro la cuenta contable con codigo {code} en Odoo.")
    return acc[0]


# =======================================================================
# PROXY Y TRANSPORTE DE RESILIENCIA ANTE ERRORES 502 / TIMEOUTS (INTACTO)
# =======================================================================

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
                log.warning(f"Error de red/Timeout en conexion inicial (intento {attempt}/{self.max_retries_init}): {e}")
                ERROR_502_COUNTER += 1
                if attempt == self.max_retries_init:
                    raise
                tm.sleep(self.delay_init * attempt)
            except Exception as e:
                log.error(f"Error fatal en conexion inicial (no reintentable): {e}")
                raise

    def reauthenticate(self):
        log.info("Cerrando sesion TLS y abriendo una nueva conexion con Odoo...")
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
                log.warning(f"Error de comunicacion en Odoo [{model}.{method}]. Intento {attempt}/{max_retries}: {str(e)}")
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


# =======================================================================
# CONTEXTO DE EJECUCION (reemplaza a las variables globales acumulativas)
# =======================================================================

class BillingContext:
    """
    Encapsula todo el estado necesario para procesar la facturacion.
    Al vivir en un solo objeto local (creado dentro de run() y descartado
    al finalizar), el Garbage Collector puede liberarlo por completo en
    cuanto termina la ejecucion, en vez de mantener referencias globales
    vivas durante horas.
    """

    def __init__(self, models, uid, today_date_dt):
        self.models = models
        self.uid = uid
        self.today_date = today_date_dt

        # Cuentas contables CxC por marketplace
        self.accounts = {}

        # Ids de ordenes con mensaje 'serialize' (solo nombres -> set, ligero)
        self.orders_not_serialize = set()

        self.invoice_date_first_of_month = None
        self.last_day_of_year_flag = False

        # Solo se guardan IDs (enteros) de ordenes ya vistas en el recorrido
        # diario, para poder des-duplicar contra los reintentos fallidos
        # sin tener que retener los registros completos en memoria.
        self.seen_order_ids = set()

        # Contadores globales de progreso (viven en memoria constante)
        self.success_count = 0
        self.skipped_ml = 0
        self.skipped_grace = 0
        self.test_limit_remaining = TEST_ORDER_LIMIT  # None o int

        self.start_time = tm.time()

    def account_for_team(self, team_name):
        if 'Amazon' in team_name:
            return self.accounts.get('amazon')
        if 'Walmart_1P' in team_name or '1P' in team_name:
            return self.accounts.get('walmart_1p')
        if 'Walmart' in team_name:
            return self.accounts.get('walmart')
        if 'Coppel' in team_name:
            return self.accounts.get('coppel')
        if 'Elektra' in team_name:
            return self.accounts.get('elektra')
        if 'TikTok' in team_name:
            return self.accounts.get('tiktok')
        if 'Mayoreo' in team_name:
            return self.accounts.get('mayoreo')
        return None


# =======================================================================
# BUSQUEDAS EN ODOO (con limites de seguridad, sin depender de globals)
# =======================================================================

def get_current_year_cdmx(last_day_of_year_flag):
    current_year = datetime.now().year
    first_day_of_year = datetime(current_year, 1, 1)
    last_day_of_year = datetime(current_year, 12, 31, 23, 59, 59)
    first_day_of_year_cdmx, last_day_of_year_cdmx = adjust_to_cdmx_time(first_day_of_year, last_day_of_year)
    if last_day_of_year_flag:
        return first_day_of_year_cdmx.replace(year=first_day_of_year_cdmx.year - 1), last_day_of_year_cdmx.replace(year=last_day_of_year_cdmx.year - 1)
    return first_day_of_year_cdmx, last_day_of_year_cdmx


def adjust_to_cdmx_time(first_date, last_day=None):
    start_date = first_date - timedelta(hours=UTC_local)
    end_date = start_date + timedelta(hours=24) if not last_day else last_day - timedelta(hours=UTC_local)
    return start_date, end_date


def generate_date_range(start_date, end_date):
    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    return [start_dt + timedelta(days=x) for x in range((end_dt - start_dt).days + 1)]


def search_sales_with_message(context, start_day, end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        first_day_of_year_cdmx, last_day_of_year_cdmx = get_current_year_cdmx(context.last_day_of_year_flag)
        domain = [('state', '=', 'sale'), ('effective_date', '>=', first_day), ('effective_date', '<=', last_day),
                  ('message_ids.body', 'ilike', 'serialize'), ('effective_date', 'ilike', '-'),
                  ('create_date', '>=', first_day_of_year_cdmx), ('create_date', '<=', last_day_of_year_cdmx)]
        # Limite de seguridad: antes 'limit': 0 (sin tope) descargaba historicos
        # completos de texto. Un catalogo de ordenes rara vez excede este tope
        # en una ventana mensual; si lo excede, se registra advertencia.
        sales_orders = context.models.execute_kw(
            ODOO_DB, context.uid, ODOO_PWD, 'sale.order', 'search_read',
            [domain], {'fields': ['name'], 'limit': MESSAGE_QUERY_SAFETY_LIMIT}
        )
        if len(sales_orders) >= MESSAGE_QUERY_SAFETY_LIMIT:
            log.warning(f"search_sales_with_message alcanzo el limite de seguridad ({MESSAGE_QUERY_SAFETY_LIMIT}).")
        names = {o['name'] for o in sales_orders}
        del sales_orders
        return names
    except Exception as e:
        log.error(f"Error en query serialize: {e}")
        return set()


def search_sales_with_stock_insufficient_message(context, start_day, end_day):
    try:
        first_day, last_day = adjust_to_cdmx_time(datetime.strptime(start_day, '%Y-%m-%d'), datetime.strptime(end_day, '%Y-%m-%d'))
        domain = [('state', '=', 'sale'), ('message_ids.body', 'ilike', 'insufficient stock 0'),
                  ('date_order', '>=', first_day), ('date_order', '<=', last_day), ('invoice_count', '<', '2')]
        sales_orders = context.models.execute_kw(
            ODOO_DB, context.uid, ODOO_PWD, 'sale.order', 'search_read',
            [domain], {'fields': ['name'], 'limit': MESSAGE_QUERY_SAFETY_LIMIT}
        )
        count = len(sales_orders)
        if count >= MESSAGE_QUERY_SAFETY_LIMIT:
            log.warning(f"search_sales_with_stock_insufficient_message alcanzo el limite de seguridad ({MESSAGE_QUERY_SAFETY_LIMIT}).")
        del sales_orders
        return count
    except Exception as e:
        log.error(f"Error en query stock insuficiente: {e}")
        return 0


def fetch_records(context, day_start, day_end):
    so_domain = [('invoice_status', '=', 'to invoice'), ('locked', '=', 'True'), ('date_order', '>=', day_start), ('date_order', '<=', day_end)]
    try:
        return context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'sale.order', 'search_read', [so_domain])
    except Exception as e:
        log.error(f"Error fetch_records: {e}")
        return []


# =======================================================================
# FILTRADO Y AGRUPACION POR EQUIPO (por lote, no sobre el historico completo)
# =======================================================================

def filter_and_group_by_team(context, records, delta_days, failed_ids_set):
    """
    Aplica exactamente las mismas reglas de negocio que la version original
    (estado de facturacion, corte MercadoLibre, periodo de gracia) pero
    sobre un lote pequeno de registros en vez de sobre el acumulado total.
    """
    teams_dict = {}
    cutoff_ml = datetime(2026, 6, 1, 0, 0, 0)

    for record in records:
        is_failed_retry = bool(failed_ids_set) and record['id'] in failed_ids_set
        if record['invoice_status'] != 'to invoice' and not is_failed_retry:
            continue

        order_date_str = record.get('date_order', False)
        if not order_date_str:
            continue

        real_order_date = datetime.strptime(order_date_str, '%Y-%m-%d %H:%M:%S')
        difference_days = (context.today_date - real_order_date).days
        team_name = record['team_id'][1]

        if 'MercadoLibre' in team_name and real_order_date >= cutoff_ml:
            context.skipped_ml += 1
            continue

        grace_days = 1
        if not delta_days or (delta_days and difference_days >= grace_days):
            teams_dict.setdefault(team_name, []).append(record)
        else:
            context.skipped_grace += 1

    teams_dict.pop('Team_Walmart', None)
    teams_dict.pop('Salderos / Facebook', None)

    return teams_dict


def apply_test_limit(context, teams_dict):
    """Misma logica de TEST_ORDER_LIMIT del original, pero con estado
    persistente en el contexto para que el limite aplique a traves de
    todos los lotes (dias) procesados, no solo dentro de un lote."""
    if context.test_limit_remaining is None:
        return teams_dict

    for team, orders in list(teams_dict.items()):
        remaining = context.test_limit_remaining
        if remaining <= 0:
            teams_dict[team] = []
        elif len(orders) > remaining:
            teams_dict[team] = orders[:remaining]
            context.test_limit_remaining -= len(teams_dict[team])
        else:
            context.test_limit_remaining -= len(orders)

    return {k: v for k, v in teams_dict.items() if v}


# =======================================================================
# FACTURACION 1 A 1 (logica contable / timbrado sin cambios)
# =======================================================================

def execute_invoice(context, team_name, orders_list):
    if not orders_list:
        return 0

    team_id = orders_list[0]['team_id'][0]
    total_orders = len(orders_list)
    success_count = 0

    acc_cxc_team = context.account_for_team(team_name)

    # --- Facturas existentes: solo para las ordenes de ESTE lote ---
    order_names = [order['name'] for order in orders_list]
    existing_invoices_data = []
    for chunk in get_chunks(order_names, 500):
        data = context.models.execute_kw(
            ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'search_read',
            [[('invoice_origin', 'in', chunk), ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')]],
            {'fields': ['id', 'invoice_origin', 'name', 'state', 'l10n_mx_edi_cfdi_uuid']})
        existing_invoices_data.extend(data)

    invoiced_origins = {inv['invoice_origin']: inv for inv in existing_invoices_data if inv['invoice_origin']}
    del existing_invoices_data, order_names

    # --- Procesamiento en sub-lotes de ORDER_LINE_BATCH_SIZE ordenes ---
    # Antes: se descargaban TODAS las lineas de TODAS las ordenes del
    # equipo (para todo el rango de fechas) en un solo diccionario
    # gigante (`lines_dict`). Ahora solo se descargan las lineas del
    # sub-lote que se esta procesando en ese instante, y se liberan
    # (`del` + `gc.collect()`) antes de pasar al siguiente sub-lote.
    for batch_start, orders_batch in enumerate(get_chunks(orders_list, ORDER_LINE_BATCH_SIZE)):
        line_ids_batch = [line_id for order in orders_batch for line_id in order['order_line']]
        lines_dict = {}
        for chunk in get_chunks(line_ids_batch, 1000):
            data = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'sale.order.line', 'search_read', [[('id', 'in', chunk)]])
            for line in data:
                lines_dict[line['id']] = line
            del data

        for order in orders_batch:
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
                    log.warning(f"BUCLE EVITADO: {order_name} YA TIENE la factura timbrada {inv_name}. Se ignorara.")
                    update_audit_record(audit_id, status='IGNORED_DUPLICATE', error_log=f"Ya tiene factura timbrada {inv_name}", invoice_name=inv_name, invoice_id=inv_id, automation_status='STAMPED')
                    continue
                elif inv_state == 'posted' and not is_stamped:
                    log.info(f"Reanudando orden {order_name}: Factura {inv_name} ya confirmada. Continuando con timbrado SAT...")
                    real_invoice_name = inv_name
                    needs_creation = False
                    needs_post = False
                    needs_stamping = True
                elif inv_state == 'draft' or inv_name in (False, 'False'):
                    log.info(f"Reanudando orden {order_name}: Factura en borrador (ID: {inv_id}). Continuando desde confirmacion...")
                    needs_creation = False
                    needs_post = True
                    needs_stamping = True
                else:
                    log.warning(f"BUCLE EVITADO: {order_name} YA TIENE la factura {inv_name} en estado {inv_state}. Se ignorara.")
                    update_audit_record(audit_id, status='IGNORED_DUPLICATE', error_log=f"Ya tiene factura {inv_name}")
                    continue

            try:
                if (order['state'] == 'sale' and order['locked']) or (order_name in context.orders_not_serialize):
                    if (order['invoice_status'] == 'to invoice' and order['invoice_count'] == 0) or not needs_creation:
                        if needs_creation:
                            invoice_line_vals_list = []
                            abortar_orden = False

                            for line_id in order['order_line']:
                                line = lines_dict.get(line_id)
                                if not line:
                                    continue

                                qty_ordered = line['product_uom_qty']
                                qty_invoiced = line['qty_invoiced']
                                qty_delivered = line['qty_delivered']

                                product_name = line['product_id'][1].upper() if line.get('product_id') else ""
                                is_shipping = 'C-ENVIO' in product_name

                                if is_shipping and qty_delivered < qty_ordered:
                                    try:
                                        context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'sale.order.line', 'write', [[line['id']], {'qty_delivered': qty_ordered}])
                                        qty_delivered = qty_ordered
                                    except Exception as e:
                                        log.error(f"No se pudo actualizar la cantidad entregada de C-ENVIO para {order_name}: {e}")

                                if not is_shipping and qty_delivered < qty_ordered:
                                    log.debug(f"Orden {order_name} ignorada: Falta entrega fisica.")
                                    update_audit_record(audit_id, status='IGNORED_NO_STOCK', error_type='VALIDATION_ERROR', error_log=f"Falta entrega: Ord {qty_ordered}, Entr {qty_delivered}")
                                    abortar_orden = True
                                    break

                                if qty_invoiced >= qty_ordered:
                                    continue

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
                                if context.invoice_date_first_of_month:
                                    invoice_vals['invoice_date'] = context.invoice_date_first_of_month

                                try:
                                    inv_id = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'create', [invoice_vals])
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
                                    move_lines = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move.line', 'search_read',
                                                                            [[('move_id', '=', inv_id)]], {'fields': ['id', 'account_type']})
                                    lines_to_update = [(1, m_line['id'], {'account_id': acc_cxc_team}) for m_line in move_lines if m_line['account_type'] == 'asset_receivable']
                                    if lines_to_update:
                                        context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'write', [[inv_id], {'line_ids': lines_to_update}])

                                if needs_creation:
                                    context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'message_post', [inv_id], {'body': f'Factura 1 a 1 para {order_name}. Creada via API.', 'message_type': 'comment'})

                                context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'action_post', [[inv_id]])

                                inv_data = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'read', [[inv_id]], {'fields': ['name']})
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
                                    inv_data = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'read', [[inv_id]], {'fields': ['name']})
                                    real_invoice_name = inv_data[0]['name'] if inv_data else str(inv_id)

                                wizard_context = {'active_model': 'account.move', 'active_ids': [inv_id]}
                                wizard_id = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move.send.wizard', 'create', [{'is_download_only': False}], {'context': wizard_context})
                                context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move.send.wizard', 'action_send_and_print', [[wizard_id]], {'context': wizard_context})

                                update_audit_record(audit_id, status='SUCCESS', invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='STAMPED')

                                success_count += 1
                                context.success_count += 1
                                if success_count % LOG_PROGRESS_EVERY == 0 or success_count == total_orders:
                                    log.info(f"[{team_name}] Avance: {success_count}/{total_orders} facturadas en este lote (total acumulado: {context.success_count}).")
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

        # --- Liberacion explicita de memoria al cerrar el sub-lote ---
        del lines_dict, line_ids_batch
        gc.collect()

    del invoiced_origins
    return success_count


def process_batch(context, records, delta_days, failed_ids_set, batch_label):
    """Filtra, agrupa por equipo y factura UN lote (un dia o un lote de
    reintentos fallidos). No retiene nada del lote una vez terminado."""
    teams_dict = filter_and_group_by_team(context, records, delta_days, failed_ids_set)
    teams_dict = apply_test_limit(context, teams_dict)

    if not teams_dict:
        del teams_dict
        return

    for team_name, orders_list in teams_dict.items():
        log.info(f"[{batch_label}] Procesando equipo: {team_name} ({len(orders_list)} ordenes)")
        invoiced_qty = execute_invoice(context, team_name, orders_list)
        if invoiced_qty > 0:
            log.info(f"[{batch_label}] -> {invoiced_qty} ordenes facturadas exitosamente de {team_name}.")

    del teams_dict
    gc.collect()


# =======================================================================
# ORQUESTACION PRINCIPAL
# =======================================================================

def main():
    connections_count = 0
    reset_stuck_processing_records()
    while True:
        try:
            run()
            break
        except ConnectionResetError as e:
            connections_count += 1
            if connections_count < 3:
                log.error(f"Error de conexion: {e}. Reintentando...")
                tm.sleep(5)
            else:
                raise e


def run():
    today_date_dt = datetime.now()

    models = OdooModelProxy(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD, timeout=300)
    uid = models.uid
    log.info('Conexion con Odoo establecida (via OdooModelProxy)')

    context = BillingContext(models, uid, today_date_dt)

    context.accounts['amazon'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.006')
    context.accounts['walmart'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.007')
    context.accounts['walmart_1p'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.008')
    context.accounts['coppel'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.009')
    context.accounts['elektra'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.010')
    context.accounts['tiktok'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.011')
    context.accounts['mayoreo'] = get_account_id(models, ODOO_DB, uid, ODOO_PWD, '105.01.012')

    today_date_str = today_date_dt.strftime("%Y-%m-%d %H:%M:%S")
    formated_date = today_date_str.split(' ')[0].split('-')

    if int(formated_date[2]) == 1:
        start_date = (today_date_dt - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
        end_date = (today_date_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        delta_days = False
        context.invoice_date_first_of_month = end_date
        context.last_day_of_year_flag = end_date.endswith("-12-31")
    else:
        start_date = today_date_dt.replace(day=1).strftime("%Y-%m-%d")
        end_date = today_date_str.split(' ')[0]
        delta_days = True
        context.invoice_date_first_of_month = None
        context.last_day_of_year_flag = False

    log.info("Iniciando busqueda de ordenes con mensaje 'serialize'...")
    t0 = tm.time()
    context.orders_not_serialize = search_sales_with_message(context, start_date, end_date)
    log.info(f"Terminado en {round(tm.time() - t0, 2)}s. Encontradas: {len(context.orders_not_serialize)}")

    log.info("Iniciando busqueda de stock insuficiente...")
    t1 = tm.time()
    stock_insuf_count = search_sales_with_stock_insufficient_message(context, start_date, end_date)
    log.info(f"Terminado en {round(tm.time() - t1, 2)}s. Encontradas: {stock_insuf_count}")

    date_range = generate_date_range(start_date, end_date)
    failed_ids = get_failed_order_ids()
    failed_ids_set = set(failed_ids)

    log.info(f"Procesando dia por dia desde {start_date} hasta {end_date} (lotes de hasta {ORDER_LINE_BATCH_SIZE} ordenes)...")

    # --- STREAMING: se procesa y factura un dia, se libera memoria, se avanza ---
    for number_day, single_date in enumerate(date_range):
        day_start, day_end = adjust_to_cdmx_time(single_date)
        day_records = fetch_records(context, day_start, day_end)

        if day_records:
            context.seen_order_ids.update(r['id'] for r in day_records)
            process_batch(context, day_records, delta_days, failed_ids_set, batch_label=f"Dia {number_day + 1}")

        del day_records
        gc.collect()

    # --- Reintento de ordenes previamente fallidas (no cubiertas por el rango 'to invoice') ---
    missing_failed_ids = [oid for oid in failed_ids if oid not in context.seen_order_ids]
    if missing_failed_ids:
        log.info(f"Recuperando {len(missing_failed_ids)} ordenes con errores previos en BD para reanudar su facturacion...")
        for chunk_num, chunk in enumerate(get_chunks(missing_failed_ids, FAILED_ORDERS_BATCH_SIZE)):
            try:
                failed_records = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read', [[('id', 'in', chunk)]])
            except Exception as e:
                log.error(f"Error extrayendo ordenes fallidas: {e}")
                continue

            if failed_records:
                process_batch(context, failed_records, delta_days, failed_ids_set, batch_label=f"Reintentos lote {chunk_num + 1}")

            del failed_records
            gc.collect()

    log.info(f"Filtros aplicados: {context.skipped_ml} excluidas por ML, {context.skipped_grace} excluidas por periodo de gracia (1 dia).")
    log.info('PROCESO DE FACTURACION TERMINADO')
    log.info(f"Total de ordenes facturadas exitosamente: {context.success_count}")
    log.info(f"ERROR_502_COUNTER FINAL: {ERROR_502_COUNTER}")


if __name__ == '__main__':
    log.info('================================================================')
    log.info('BIENVENIDO AL PROCESO DE FACTURACION PARA MARKETPLACES (1 a 1)')
    if TEST_ORDER_LIMIT:
        log.info(f'MODO PRUEBA ACTIVADO: Limite de {TEST_ORDER_LIMIT} ordenes VALIDAS')
    log.info('================================================================')

    start_time = tm.time()
    try:
        main()
        log.info(f'Tiempo de ejecucion TOTAL: {round(tm.time() - start_time, 2)} segundos')
    except Exception as e:
        log.error(f"Fallo critico en el proceso de facturacion: {str(e)}")
        sys.exit(1)