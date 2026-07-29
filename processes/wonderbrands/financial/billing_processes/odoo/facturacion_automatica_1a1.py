import os
import sys
import gc
import json
import time as tm
import logging
import urllib.request
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


    ###################################################################
    #  BLINDAJE ANTI DOBLE FACTURACION  (incidente del 26-jul-2026)   #
    ###################################################################

    CAUSA RAIZ: dos ejecuciones concurrentes del flujo (2WBdjxnN, iniciada
    23:30 del 25-jul y matada 06:21 del 26-jul, contra 5pD4GWFm, iniciada
    04:30 del 26-jul) se traslaparon 1h51m. Cada una tomo su propio snapshot
    de facturas existentes al inicio de cada lote y ninguna vio a la otra:
    549 ordenes con doble CFDI timbrado, folios 61332-62441 (1,110 folios
    consecutivos / 549 ordenes = 2.02 por orden).

    La exclusion mutua entre ejecuciones ahora vive en Kestra
    (concurrency: limit 1, behavior FAIL).
    Este archivo aporta las cinco defensas que corresponden al script:

      1. RE-VALIDACION antes del create. El snapshot de facturas existentes
         se toma una vez por lote y puede tener hasta N minutos de
         antiguedad. Se vuelve a preguntar a Odoo inmediatamente antes de
         crear. FAIL-CLOSED: si la comprobacion falla, NO se crea.

      2. CLAVE DE IDEMPOTENCIA en account.move.ref ('AUTOINV:<orden>').
         El campo estaba sin usar. Permite localizar una factura huerfana
         cuando se pierde la respuesta del create.

      3. RETRY CONSCIENTE DEL METODO. Solo se reintentan lecturas. Un 502 o
         un timeout en una escritura NO dice si Odoo hizo commit: se lanza
         OdooWriteUncertain y se reconcilia leyendo, nunca reintentando.

      4. DETECCION REAL DE DUPLICADOS. invoiced_origins era un dict
         {origen: factura}: un dict no puede guardar dos valores con la misma
         llave, asi que cuando una orden tenia dos facturas una desaparecia
         en silencio. Por eso el log reportaba siempre la primera y nunca
         supo de la segunda. Ahora es {origen: [facturas]} y len>1 es un
         incidente fiscal que se alerta y se aparta.

      5. SIN RETRY CIEGO EN main(). Relanzar run() a media corrida vuelve a
         recorrer la misma lista. Un fallo ahora es visible, no automatico.

    Ver tambien: frontera de dia corregida (<= -> <) y limpieza de registros
    'PROCESSING' con antiguedad minima.
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
TEST_ORDER_LIMIT = None  # None -> historico.
# =======================================================================

# --- TAMANOS DE LOTE (controlan el techo de memoria) ---
ORDER_LINE_BATCH_SIZE = 100      # ordenes por lote al descargar sale.order.line
FAILED_ORDERS_BATCH_SIZE = 500   # ordenes fallidas por lote (search_read por id)
LOG_PROGRESS_EVERY = 50          # cada cuantas ordenes se imprime avance
MESSAGE_QUERY_SAFETY_LIMIT = 20000  # limite de seguridad para queries de mensajes

# --- BLINDAJE: prefijo de la clave de idempotencia escrita en account.move.ref ---
IDEMPOTENCY_PREFIX = 'AUTOINV:'

# --- BLINDAJE: antiguedad minima para dar por muerto un registro 'PROCESSING' ---
STUCK_PROCESSING_MIN_AGE_HOURS = 6

# --- VARIABLES DE ENTORNO (Inyectadas por Kestra) ---
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USER = os.getenv('ODOO_USER')
ODOO_PWD = os.getenv('ODOO_PASSWORD')

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'finance')

SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK')
KESTRA_EXECUTION_ID = os.getenv('KESTRA_EXECUTION_ID', 'local')

ERROR_502_COUNTER = 0

UTC_local = -6


class OdooWriteUncertain(Exception):
    """Una ESCRITURA en Odoo fallo por red: se desconoce si hubo commit.

    Es lo que permite distinguir
    "Odoo rechazo la operacion" (xmlrpc Fault: hubo rollback, no hay factura)
    de "no se sabe si Odoo la aplico" (502 / timeout: puede haber factura).
    La primera se puede dar por fallida; la segunda JAMAS se reintenta a
    ciegas, se reconcilia leyendo.
    """


# =======================================================================
# ALERTA A SLACK DESDE EL SCRIPT
#   El webhook del YAML solo dispara en 'errors:', es decir cuando el
#   pipeline falla. Las dos corridas que timbraron 549 CFDI de mas
#   terminaron con exit code 0 y nadie se entero.
# =======================================================================

def notify_slack(text):
    if not SLACK_WEBHOOK:
        log.warning('SLACK_WEBHOOK no configurado: la alerta no se envio.')
        return
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=json.dumps({'text': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log.warning(f"No se pudo enviar la alerta a Slack: {e}")


# =======================================================================
# AUDITORIA MYSQL
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
    """Devuelve a 'ERROR' los registros trabados en 'PROCESSING'.

    BLINDAJE: se agrego la condicion de antiguedad minima. La version
    anterior hacia UPDATE sobre TODOS los 'PROCESSING' sin filtro alguno,
    de modo que un proceso al arrancar borraba el marcador de trabajo en
    vuelo de cualquier otro proceso activo y reinyectaba sus ordenes en
    get_failed_order_ids() como candidatas a reintento. Era la unica
    estructura que podia haber funcionado como semaforo y la desarmaba el
    segundo proceso al iniciar.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE finance.billing_audit_log
            SET status = 'ERROR', error_type = '502_BAD_GATEWAY', error_log = 'Proceso interrumpido abruptamente (reseteado por limpieza inicial)'
            WHERE status = 'PROCESSING'
              AND updated_at < (CURRENT_TIMESTAMP - INTERVAL {STUCK_PROCESSING_MIN_AGE_HOURS} HOUR)
        """)
        affected = cursor.rowcount
        conn.commit()
        if affected:
            log.warning(f"Limpieza inicial: {affected} registro(s) trabados en 'PROCESSING' "
                        f"por mas de {STUCK_PROCESSING_MIN_AGE_HOURS}h regresados a 'ERROR' para reintento.")
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
# PROXY Y TRANSPORTE DE RESILIENCIA ANTE ERRORES 502 / TIMEOUTS
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

    # BLINDAJE: metodos de LECTURA. Son idempotentes y se pueden reintentar
    # sin ningun riesgo. Cualquier otro metodo (create, write, action_post,
    # action_send_and_print, message_post, button_*) MODIFICA datos: si la
    # respuesta se pierde por red, el cliente NO sabe si Odoo hizo commit.
    # Odoo ejecuta el create en su propia transaccion y la confirma aunque
    # el cliente ya no escuche. Reintentar a ciegas es exactamente lo que
    # produjo las facturas duplicadas.
    IDEMPOTENT_METHODS = frozenset({
        'search', 'search_read', 'read', 'search_count', 'read_group',
        'fields_get', 'default_get', 'name_search', 'name_get',
    })

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

        is_write = method not in self.IDEMPOTENT_METHODS
        if is_write:
            max_retries = 1   # BLINDAJE: las escrituras NO se reintentan

        for attempt in range(1, max_retries + 1):
            try:
                if kwargs is not None:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args, kwargs)
                else:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args)

            except xmlrpc.client.Fault as e:
                # Error de negocio de Odoo: determinista, la transaccion hizo rollback. Se propaga tal cual.
                raise e

            except (xmlrpc.client.ProtocolError, TimeoutError, OSError) as e:
                ERROR_502_COUNTER += 1
                if is_write:
                    log.error(f"Error de red en ESCRITURA [{model}.{method}]: {e}. "
                              f"NO se reintenta: el resultado en Odoo es INDETERMINADO.")
                    raise OdooWriteUncertain(f"{model}.{method}: {e}") from e
                log.warning(f"Error de red/Timeout en Odoo [{model}.{method}]: {str(e)}. Intento {attempt}/{max_retries}...")
                if attempt == max_retries:
                    raise e
                tm.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception as auth_e:
                    log.warning(f"Error al reautenticar con Odoo: {str(auth_e)}")

            except Exception as e:
                ERROR_502_COUNTER += 1
                if is_write:
                    log.error(f"Error de comunicacion en ESCRITURA [{model}.{method}]: {e}. "
                              f"NO se reintenta: el resultado en Odoo es INDETERMINADO.")
                    raise OdooWriteUncertain(f"{model}.{method}: {e}") from e
                log.warning(f"Error de comunicacion en Odoo [{model}.{method}]. Intento {attempt}/{max_retries}: {str(e)}")
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

        # --- BLINDAJE: metricas de duplicidad ---
        self.duplicates_found = []      # ordenes que YA tienen 2+ facturas vivas
        self.duplicates_avoided = 0     # creates abortados por la re-validacion
        self.invoices_adopted = 0       # facturas recuperadas tras un create incierto

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
# BLINDAJE: HELPERS DE IDEMPOTENCIA
# =======================================================================

def count_live_invoices(context, order_name):
    """Cuantas facturas de cliente VIVAS tiene ya esta orden en Odoo, AHORA.

    Se llama inmediatamente antes del create. El snapshot de facturas
    existentes del lote se toma una sola vez y puede tener hasta ~68 minutos
    de antiguedad (medido en la corrida del 26-jul: el lote Amazon del Dia 17
    tardo de 04:57 a 06:05).

    Devuelve -1 si la comprobacion no se pudo realizar, para que el llamador
    aborte por seguridad (FAIL-CLOSED).
    """
    try:
        return context.models.execute_kw(
            ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'search_count',
            [[('invoice_origin', '=', order_name),
              ('move_type', '=', 'out_invoice'),
              ('state', '!=', 'cancel')]])
    except Exception as e:
        log.error(f"No se pudo re-validar {order_name} contra Odoo antes de crear: {e}")
        return -1


def find_invoice_by_key(context, order_name):
    """Localiza la factura de una orden tras un create de resultado incierto.

    Busca primero por la clave de idempotencia en 'ref' y, como respaldo
    (facturas anteriores a esta convencion), por invoice_origin.

    Es la alternativa al reintento ciego: si Odoo confirmo la transaccion y
    la respuesta se perdio, la factura EXISTE y hay que adoptarla, no crear
    otra.
    """
    domains = [
        [('ref', '=', f'{IDEMPOTENCY_PREFIX}{order_name}'),
         ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')],
        [('invoice_origin', '=', order_name),
         ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel')],
    ]
    for domain in domains:
        try:
            found = context.models.execute_kw(
                ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'search_read', [domain],
                {'fields': ['id', 'name', 'state', 'l10n_mx_edi_cfdi_uuid'], 'order': 'id asc'})
        except Exception as e:
            log.error(f"Reconciliacion de {order_name} fallida: {e}")
            return None
        if found:
            if len(found) > 1:
                log.error(f"ALERTA: durante la reconciliacion, {order_name} presenta "
                          f"{len(found)} facturas vivas: {[f.get('name') for f in found]}")
                context.duplicates_found.append(order_name)
            return found[0]
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
    # BLINDAJE: day_end era inclusivo ('<=') y coincide exactamente con el
    # day_start del dia siguiente, tambien inclusivo. Una orden con
    # date_order justo en la frontera se procesaba DOS veces, en dos lotes
    # distintos. Ahora el extremo superior es exclusivo.
    so_domain = [('invoice_status', '=', 'to invoice'), ('locked', '=', 'True'),
                 ('date_order', '>=', day_start), ('date_order', '<', day_end)]
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
            {'fields': ['id', 'invoice_origin', 'name', 'state', 'l10n_mx_edi_cfdi_uuid'],
             'order': 'id asc'})
        existing_invoices_data.extend(data)

    # BLINDAJE: antes era {inv['invoice_origin']: inv}. Un dict NO puede
    # guardar dos valores con la misma llave: si una orden tenia dos
    # facturas, una desaparecia en silencio. Como account.move._order
    # termina en 'id desc', sobrevivia la mas ANTIGUA, y de ahi que el log
    # siempre reportara la primera factura y nunca supiera de la segunda.
    # Ahora se conserva la lista completa y len>1 es detectable.
    invoiced_origins = {}
    for inv in existing_invoices_data:
        if inv.get('invoice_origin'):
            invoiced_origins.setdefault(inv['invoice_origin'], []).append(inv)
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

            facturas_previas = invoiced_origins.get(order_name) or []

            # --- BLINDAJE: duplicidad ya materializada ---
            # La orden tiene dos o mas CFDI vivos: no se puede resolver desde aqui (requiere
            # cancelacion fiscal ante el SAT), asi que se aparta y se alerta.
            if len(facturas_previas) > 1:
                nombres = [f.get('name') for f in facturas_previas]
                log.error(f"DUPLICADO FISCAL: {order_name} tiene {len(facturas_previas)} facturas "
                          f"vivas {nombres}. Se aparta; requiere cancelacion manual.")
                context.duplicates_found.append(order_name)
                update_audit_record(audit_id, status='IGNORED_DUPLICATE', error_type='VALIDATION_ERROR',
                                    error_log=f"DUPLICADO FISCAL: {len(facturas_previas)} facturas vivas: {nombres}",
                                    invoice_name=nombres[0], invoice_id=facturas_previas[0]['id'])
                continue

            if facturas_previas:
                existing_inv = facturas_previas[0]
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
                                    # BLINDAJE: clave de idempotencia. El campo
                                    # 'ref' se enviaba vacio y estaba disponible.
                                    # Permite localizar la factura si se pierde
                                    # la respuesta del create.
                                    'ref': f'{IDEMPOTENCY_PREFIX}{order_name}',
                                    'move_type': 'out_invoice', 'partner_id': PARTNER_ID_PUBLICO_GENERAL,
                                    'invoice_origin': order_name, 'invoice_line_ids': invoice_line_vals_list,
                                    'l10n_mx_edi_usage': 'S01', 'l10n_mx_edi_payment_method_id': 3,
                                    'l10n_mx_edi_payment_policy': 'PUE', 'team_id': team_id,
                                }
                                if context.invoice_date_first_of_month:
                                    invoice_vals['invoice_date'] = context.invoice_date_first_of_month

                                # ===== BLINDAJE: RE-VALIDACION PRE-CREATE =====
                                # invoiced_origins se calculo al inicio del lote
                                # y puede tener hasta N min de antiguedad. Esta
                                # es la ultima oportunidad de ver una factura que
                                # aparecio despues del snapshot.
                                live = count_live_invoices(context, order_name)
                                if live < 0:
                                    # FAIL-CLOSED: sin poder verificar, no se crea.
                                    log.error(f"{order_name}: re-validacion no concluyente. "
                                              f"Se omite para no arriesgar duplicidad.")
                                    update_audit_record(audit_id, status='ERROR', error_type='VALIDATION_ERROR',
                                                        error_log='Re-validacion previa al create no concluyente')
                                    continue
                                if live > 0:
                                    context.duplicates_avoided += 1
                                    log.warning(f"DUPLICADO EVITADO EN CARRERA: {order_name} adquirio "
                                                f"{live} factura(s) despues del snapshot del lote. No se crea otra.")
                                    update_audit_record(audit_id, status='IGNORED_DUPLICATE',
                                                        error_log=f"Re-validacion previa: ya existian {live} factura(s) vivas")
                                    continue
                                # ==============================================

                                try:
                                    inv_id = context.models.execute_kw(ODOO_DB, context.uid, ODOO_PWD, 'account.move', 'create', [invoice_vals])
                                except xmlrpc.client.Fault as e_create:
                                    log.error(f"Fallo al CREAR factura {order_name}: {e_create.faultString}")
                                    update_audit_record(audit_id, status='ERROR', error_type='CREATION_ERROR', error_log=e_create.faultString)
                                    continue
                                except OdooWriteUncertain as e_create:
                                    # BLINDAJE: el create pudo haberse aplicado en
                                    # Odoo aunque la respuesta se perdiera. En vez
                                    # de reintentar (lo que duplicaba la factura),
                                    # se le pregunta a Odoo por la clave de
                                    # idempotencia y se adopta lo que exista.
                                    log.error(f"CREATE de resultado INDETERMINADO en {order_name}: {e_create}")
                                    adoptada = find_invoice_by_key(context, order_name)
                                    if not adoptada:
                                        log.error(f"{order_name}: no existe factura en Odoo. Se reintentara en la proxima corrida.")
                                        update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY',
                                                            error_log=f"Create indeterminado, sin factura en Odoo: {e_create}")
                                        continue

                                    inv_id = adoptada['id']
                                    context.invoices_adopted += 1
                                    _name = adoptada.get('name')
                                    _uuid = adoptada.get('l10n_mx_edi_cfdi_uuid')
                                    _is_stamped = bool(_uuid and _uuid != 'False')
                                    log.warning(f"FACTURA RECUPERADA: {order_name} SI se habia creado en Odoo "
                                                f"(id={inv_id}, state={adoptada.get('state')}). Se adopta en lugar de crear otra.")

                                    if adoptada.get('state') == 'posted':
                                        needs_post = False
                                        real_invoice_name = _name if _name not in (False, 'False') else None
                                        needs_stamping = not _is_stamped
                                        if not needs_stamping:
                                            update_audit_record(audit_id, status='SUCCESS', invoice_name=real_invoice_name,
                                                                invoice_id=inv_id, automation_status='STAMPED')
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
                            except OdooWriteUncertain as e_post:
                                # La factura EXISTE (inv_id conocido). Si quedo
                                # posteada o no lo dira la proxima corrida al
                                # leerla por invoice_origin. Nunca se recrea.
                                log.error(f"CONFIRMACION de resultado INDETERMINADO en {order_name}: {e_post}")
                                update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY',
                                                    error_log=f"Posting indeterminado: {e_post}", invoice_id=inv_id, automation_status='DRAFT')
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
                            except OdooWriteUncertain as e_stamp:
                                # El timbrado pudo haberse enviado al PAC. NO se
                                # reintenta aqui: la proxima corrida leera el UUID
                                # real y decidira si falta timbrar.
                                log.error(f"TIMBRADO de resultado INDETERMINADO en {order_name}: {e_stamp}")
                                update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY',
                                                    error_log=f"Timbrado indeterminado: {e_stamp}", invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='POSTED')
                                continue
                            except Exception as e_stamp:
                                log.error(f"Error al TIMBRAR factura de {order_name}: {e_stamp}")
                                update_audit_record(audit_id, status='ERROR', error_type='502_BAD_GATEWAY' if '502' in str(e_stamp) else 'STAMPING_ERROR', error_log=str(e_stamp), invoice_name=real_invoice_name, invoice_id=inv_id, automation_status='POSTED')
                                continue
                    else:
                        # BLINDAJE: antes no habia rama 'else' y el registro de
                        # auditoria se quedaba en 'PROCESSING' para siempre; la
                        # limpieza inicial de la corrida siguiente lo pasaba a
                        # 'ERROR' y entraba en la cola de reintentos de forma
                        # indefinida.
                        log.debug(f"Orden {order_name} no elegible: invoice_status="
                                  f"{order['invoice_status']}, invoice_count={order['invoice_count']}")
                        update_audit_record(audit_id, status='IGNORED_NO_STOCK', error_type='VALIDATION_ERROR',
                                            error_log=f"No elegible: invoice_status={order['invoice_status']}, "
                                                      f"invoice_count={order['invoice_count']}")
                else:
                    # Mismo motivo que la rama anterior: cerrar el registro.
                    log.debug(f"Orden {order_name} no elegible: state={order['state']}, locked={order.get('locked')}")
                    update_audit_record(audit_id, status='IGNORED_NO_STOCK', error_type='VALIDATION_ERROR',
                                        error_log=f"No elegible: state={order['state']}, locked={order.get('locked')}")

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
    # BLINDAJE: se elimino el bucle 'while True' que reintentaba run()
    # completo ante ConnectionResetError. Relanzar el proceso a media
    # corrida vuelve a recorrer la misma lista de ordenes. Junto con el
    # retry de Kestra (ya retirado del YAML) y el de execute_kw, habia tres
    # capas anidadas: en el peor caso, 27 ejecuciones del mismo trabajo.
    # Un fallo ahora termina el proceso, alerta por Slack y espera decision
    # humana.
    reset_stuck_processing_records()
    run()


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

    # --- BLINDAJE: resumen y alerta de duplicidad ---
    log.info(f"Duplicados evitados por re-validacion previa al create: {context.duplicates_avoided}")
    log.info(f"Facturas recuperadas tras create indeterminado: {context.invoices_adopted}")

    if context.duplicates_found:
        unicos = sorted(set(context.duplicates_found))
        msg = (f":rotating_light: *{len(unicos)} orden(es) con CFDI DUPLICADO detectadas* "
               f"en facturacion 1 a 1.\n"
               f"Requieren cancelacion fiscal manual; el script las aparto sin tocarlas.\n"
               f"Ejecucion Kestra: `{KESTRA_EXECUTION_ID}`\n"
               f"Ordenes: {', '.join(unicos[:20])}"
               + (f" (+{len(unicos) - 20} mas)" if len(unicos) > 20 else ""))
        log.error(f"DUPLICADOS FISCALES DETECTADOS: {len(unicos)} orden(es): {unicos[:20]}")
        notify_slack(msg)

    if context.duplicates_avoided:
        notify_slack(f":warning: Se evitaron *{context.duplicates_avoided}* creaciones duplicadas "
                     f"por re-validacion previa en la ejecucion `{KESTRA_EXECUTION_ID}`. "
                     f"Indica que hubo facturas apareciendo durante la corrida: revisar si "
                     f"otro proceso esta facturando en paralelo.")


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