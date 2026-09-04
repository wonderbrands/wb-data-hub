import time
import requests
import json
import logging
import gspread
import mysql.connector
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import xmlrpc.client
import base64
import os
import io
import PyPDF2
import dotenv
from typing import Optional, List
import sys

# *************************
is_test = False
# *************************
current_dir = os.path.dirname(os.path.abspath(__file__))
shared_dir = os.path.join(os.path.dirname(current_dir), '_shared')

if shared_dir not in sys.path:
    sys.path.append(shared_dir)
    
from _00_shipping_labels_db import (
    insert_shipping_label,
    sku_shipping_cost_from_labels,
    sku_shipping_cost_from_rates,
)

# ------------------------------------------------------------

TEST_STR = "_TEST" if is_test else ""
dotenv.load_dotenv()

# ------------------------------------------------------------
from _00_load_carriers_map import load_carrier_map_from_json

# ------------------------------------------------------------


# --- CONFIGURACIÓN PRINCIPAL ---
API_KEY_MIRAKL = os.getenv(f'API_KEY_MIRAKL{TEST_STR}')  # Se ajusta automáticamente si es prod o test
API_URL_LIVE_RATES = "https://wonder-site.duckdns.org/live-rates" if not is_test else "https://wonder-site.duckdns.org/qa_live-rates"
API_URL_GENERATE_LABEL = "https://wonder-site.duckdns.org/generate-label" if not is_test else "https://wonder-site.duckdns.org/qa_generate-label"
MIRAKL_API_BASE_URL = "https://coppel-prod.mirakl.net/api" if not is_test else "https://coppel-dev.mirakl.net/api"

# --- CONFIGURACIÓN DE GOOGLE SHEETS ---
SPREADSHEET_ID_SR = os.getenv(f'SPREADSHEET_COPPEL_ID_SR')

# Ruta de credenciales
GOOGLE_CREDS_JSON = os.getenv('GOOGLE_CREDS_JSON')

CARRIER_JSON = 'carrier_map.json'

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --------------------------------------------
API_USER = os.getenv('AUTH_USER')
API_PASS = os.getenv('AUTH_PASS')
API_AUTH = (API_USER, API_PASS) if API_USER and API_PASS else None

# --- CONFIGURACIÓN DE LOGGING ---
# Lee el nivel de log desde una variable de entorno, con 'INFO' como default
# En Jenkins, puedes establecer LOG_LEVEL=WARNING
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout) # <--- Esta es la pieza clave
    ]
)

logger = logging.getLogger()
logger.info(f"Nivel de logging establecido en: {log_level_str}")


# --------------------------------------------------------------------------
# --- FUNCIONES DE BASE DE DATOS ---

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos."""
    try:
        return mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME", 'tools')
        )
    except Exception as e:
        logger.error(f"Error al conectar con la Base de Datos: {e}")
        return None


def setup_database(conn):
    """Crea la tabla expandida para registrar toda la información de las órdenes de Coppel Flex."""
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coppel_orders_flex (
                order_id VARCHAR(100) PRIMARY KEY,
                order_date VARCHAR(50),
                so_name VARCHAR(100),
                skus TEXT,
                tracking_numbers TEXT,
                carrier VARCHAR(100),
                shipping_cost VARCHAR(50),
                order_total VARCHAR(50),
                error_reason TEXT,
                last_sheet VARCHAR(100),
                updated_at DATETIME
            )
        """)
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Error creando tabla coppel_orders_flex: {e}")


def get_order_db_sheet(conn, order_id):
    """Obtiene la última hoja en la que se registró la orden."""
    if not conn: return None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT last_sheet FROM coppel_orders_flex WHERE order_id = %s", (order_id,))
        res = cursor.fetchone()
        cursor.close()
        return res['last_sheet'] if res else None
    except Exception as e:
        logger.error(f"Error consultando DB para orden {order_id}: {e}")
        return None


def update_order_db_sheet(conn, order_id, sheet_name, log_data):
    """Actualiza o inserta el registro completo de la orden en la BD."""
    if not conn or not log_data: return
    try:
        cursor = conn.cursor()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Extraer variables base comunes (siempre presentes en log_data)
        fecha_orden = log_data[0] if len(log_data) > 0 else None
        so_name = log_data[2] if len(log_data) > 2 else None
        skus = log_data[3] if len(log_data) > 3 else None

        # Variables que dependen del tipo de error / éxito
        tracking_numbers = None
        carrier = None
        shipping_cost = None
        order_total = None
        error_reason = None

        # Mapeo de datos dependiendo a qué sheet iba
        if sheet_name == 'Guias_generadas':
            tracking_numbers = log_data[4] if len(log_data) > 4 else None
            carrier = log_data[5] if len(log_data) > 5 else None
            shipping_cost = log_data[6] if len(log_data) > 6 else None
            order_total = log_data[7] if len(log_data) > 7 else None
        elif sheet_name == 'Guias_incompletas':
            error_reason = log_data[4] if len(log_data) > 4 else None
            tracking_numbers = log_data[5] if len(log_data) > 5 else None
            carrier = log_data[6] if len(log_data) > 6 else None
        elif sheet_name == 'Costo_guia_excesivo':
            shipping_cost = log_data[4] if len(log_data) > 4 else None
            order_total = log_data[5] if len(log_data) > 5 else None
            error_reason = "Costo de guía excesivo"
        elif sheet_name in ['Sin_cobertura', 'Fulfillment']:
            error_reason = log_data[4] if len(log_data) > 4 else None
        elif sheet_name in ['SO_no_bloqueadas / canceladas', 'PICK-pendiente']:
            status = log_data[4] if len(log_data) > 4 else ""
            pick_status = log_data[5] if len(log_data) > 5 else ""
            error_reason = f"SO: {status} | PICK: {pick_status}"

        # Insertar o actualizar la fila completa
        cursor.execute("""
            INSERT INTO coppel_orders_flex 
            (order_id, order_date, so_name, skus, tracking_numbers, carrier, shipping_cost, order_total, error_reason, last_sheet, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                order_date = VALUES(order_date),
                so_name = VALUES(so_name),
                skus = VALUES(skus),
                tracking_numbers = VALUES(tracking_numbers),
                carrier = VALUES(carrier),
                shipping_cost = VALUES(shipping_cost),
                order_total = VALUES(order_total),
                error_reason = VALUES(error_reason),
                last_sheet = VALUES(last_sheet), 
                updated_at = VALUES(updated_at)
        """, (order_id, fecha_orden, so_name, skus, tracking_numbers, carrier, shipping_cost, order_total, error_reason,
              sheet_name, now))

        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Error actualizando DB extendida para orden {order_id}: {e}")


# --------------------------------------------------------------------------
def load_dynamic_config():
    """
    Carga la configuración (Worksheet, Límite, ZIP) desde SPREADSHEET_ID_SR.
    Si falla, se usa los valores harcodeados como backup.
    """
    # --- Valores por defecto (backup) ---
    default_worksheet = 'Guías de envío 2026'
    default_percentage = 0.21
    default_zip = '54010'

    # Valores que se cargarán
    worksheet_name = default_worksheet
    percentage_limit = default_percentage
    origin_zip = default_zip

    try:
        # 1. Autenticar (conexión separada)
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        client = gspread.authorize(creds)

        # 2. Abrir el Sheet SR y la hoja 'AUT_DATA'
        sh_sr = client.open_by_key(SPREADSHEET_ID_SR)
        worksheet = sh_sr.worksheet('AUT_DATA')

        # 3. Leer las celdas A2, B2, C2 (1 llamada API)
        config_data = worksheet.batch_get(['A2', 'B2', 'C2'])

        # 4. Parsear Worksheet Name (A2)
        try:
            loaded_name = config_data[0][0][0]
            if loaded_name:
                worksheet_name = loaded_name
        except Exception:
            logger.warning(f"No se pudo leer WORKSHEET_NAME de A2. Usando default: '{default_worksheet}'")

        # 5. Parsear Límite de Costo (B2)
        try:
            percentage_str = config_data[1][0][0]
            if '%' in percentage_str:
                percentage_limit = float(percentage_str.replace('%', '')) / 100.0
            else:
                percentage_limit = float(percentage_str)
        except Exception:
            logger.warning(f"No se pudo leer PERCENTAGE_COST_LIMIT de B2. Usando default: '{default_percentage}'")

        # 6. Parsear CP Origen (C2)
        try:
            loaded_zip = config_data[2][0][0]
            if loaded_zip:
                origin_zip = loaded_zip
        except Exception:
            logger.warning(f"No se pudo leer ORIGIN_ZIP de C2. Usando default: '{default_zip}'")

        logger.info("--- Configuración Dinámica Cargada desde 'AUT_DATA' ---")

    except Exception as e:
        logger.error(f"Error fatal al cargar configuración de 'AUT_DATA': {e}.")
        logger.warning("--- Se usarán todos los valores por defecto (hardcoded) ---")
        # Si falla la conexión, ya tenemos los defaults asignados
        worksheet_name = default_worksheet
        percentage_limit = default_percentage
        origin_zip = default_zip

    logger.info(f"  -> Hoja de Trabajo: {worksheet_name}")
    logger.info(f"  -> Límite de Costo: {percentage_limit:.2%}")
    logger.info(f"  -> CP de Origen: {origin_zip}")

    return percentage_limit, worksheet_name, origin_zip


PERCENTAGE_COST_LIMIT, WORKSHEET_NAME, ORIGIN_ZIP = load_dynamic_config()

# --------------------------------------------------------------------------

# --- CONFIGURACIÓN DE ODOO ---
ODOO_URL = os.getenv(f'odoo_url{TEST_STR}V18')
ODOO_DB = os.getenv(f'odoo_db{TEST_STR}V18')
ODOO_USER = os.getenv('odoo_user_dataV18')
ODOO_PASSWORD = os.getenv('odoo_password_dataV18')

# --- DATOS FIJOS DEL REMITENTE (SHIPPER) ---
SHIPPER_DATA = {
    "name": "Equipo Somos Reyes",
    "company": "SOMOS REYES",
    "email": "info@somos-reyes.com",
    "phone": "5568309828",
    "street1": "BENITO JUAREZ 11/B6",
    "street2": "SAN PEDRO BARRIENTOS",
    "city": "Tlalnepantla de Baz",
    "state": "MEX",
    "country": "MX",
    "zip": ORIGIN_ZIP
}

pickin_status_list = {
    "draft": "Borrador",
    "waiting": "En espera de otra operación",
    "confirmed": "En espera",
    "assigned": "Listo",
    "done": "Hecho",
    "cancel": "Cancelado"
}

order_status_list = {
    "draft": "Cotización",
    "sent": "Cotización enviada",
    "sale": "Orden de venta",
    "cancel": "Cancelado"
}


# ---------------------------------------------
def convert_zpl_to_pdf_bytes(zpl_string: str) -> Optional[bytes]:
    """
    Convierte un string ZPL a bytes de PDF usando la API de Labelary.
    """
    # 8dpmm = 203dpi, 4x6 in
    url = "http://api.labelary.com/v1/printers/8dpmm/labels/4x6/"
    headers = {"Accept": "application/pdf"}

    try:
        response = requests.post(url, headers=headers, data=zpl_string, timeout=15)
        response.raise_for_status()
        if response.content:
            return response.content
        else:
            logger.error("Labelary API devolvió una respuesta vacía.")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error al llamar a Labelary API para convertir ZPL: {e}")
        return None


# --- FUNCIONES DE ODOO ---

def connect_to_odoo(url, db, user, password):
    """Conecta a Odoo y devuelve los objetos necesarios."""
    try:
        common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
        uid = common.authenticate(db, user, password, {})
        models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
        if uid:
            logger.info(f"Conexión exitosa a Odoo URL: {url}, DB: {db}, UID: {uid}")
            return models, db, uid, password
    except Exception as e:
        logger.critical(f"Error fatal al conectar con Odoo: {e}")
    return None, None, None, None


def get_carrier_odoo_id(provider_name: str) -> Optional[int]:
    """Mapea el nombre del carrier a su ID de Odoo."""
    provider_name = provider_name.lower()
    if 'fedex' in provider_name:
        return 1
    if 'estafeta' in provider_name:
        return 2
    if 'dhl' in provider_name:
        return 3
    if 'paqueteexpress' in provider_name:
        return 4
    if 'segmail' in provider_name:
        return 7

    logger.warning(f"Odoo: No se encontró mapeo de ID para carrier: {provider_name}")
    return None


def search_sale_order_by_mkt_ref(models, db, uid, password, mkt_reference):
    """Busca una sale.order por 'channel_order_reference' y devuelve su ID y Nombre (ej. S01234)."""
    try:
        search_domain = [['channel_order_reference', '=', mkt_reference]]
        so_data = models.execute_kw(db, uid, password, 'sale.order', 'search_read',
                                    [search_domain], {'fields': ['id', 'name', 'state', 'locked'], 'limit': 1})
        if so_data:
            so_id = so_data[0]['id']
            so_name = so_data[0]['name']
            status = so_data[0]['state']
            is_locked = so_data[0]['locked']
            if status == 'sale' and is_locked == True:
                status = 'done'  # Retorna done como valor interno, pero en odoo no existe done

            logger.info(f"Odoo: SO encontrada para MKT Ref '{mkt_reference}': ID={so_id}, Name={so_name}")
            return so_id, so_name, status
        else:
            logger.warning(f"Odoo: No se encontró SO para MKT Ref '{mkt_reference}'")
    except Exception as e:
        logger.error(f"Odoo: Error al buscar SO '{mkt_reference}': {e}")
    return None, None, None


def update_sale_order(models, db, uid, password, so_id, tracking_refs_str, carrier_id, num_packages, client_reference):
    """Actualiza la Orden de Venta con los datos de envío."""
    try:
        update_values = {
            'data_tracking_readwrite': tracking_refs_str,
            'client_order_ref': client_reference,
        }
        if carrier_id:
            update_values['data_carrier_selection_relational'] = carrier_id

        models.execute_kw(db, uid, password, 'sale.order', 'write', [[so_id], update_values])
        logger.info(f"Odoo: SO ID {so_id} actualizada con {num_packages} guía(s): {tracking_refs_str}")
        return True
    except Exception as e:
        logger.error(f"Odoo: Error al actualizar SO ID {so_id}: {e}")
    return False


def search_picking_id(models, db, uid, password, so_name):
    """Busca el ID de un stock.picking (albarán) que provenga de una SO y contenga 'PICK'."""
    try:
        search_domain = [
            ['origin', '=', so_name],
            ['name', 'ilike', 'PICK']
        ]
        picking_ids = models.execute_kw(db, uid, password, 'stock.picking', 'search_read',
                                        [search_domain], {'fields': ['id', 'name', 'state'], 'limit': 1})
        if picking_ids:
            if len(picking_ids) > 1:
                logger.warning(
                    f"Odoo: Múltiples albaranes 'PICK' para {so_name}. Usando el primero: {picking_ids[0]['id']}.")
            return picking_ids[0]['id'], picking_ids[0]['state']
        logger.warning(f"Odoo: No se encontró albarán 'PICK' para {so_name}.")
    except Exception as e:
        logger.error(f"Odoo: Error al buscar albarán para {so_name}: {e}")
    return None, None


def attach_file_to_so_attachment(models, db, uid, password, so_id, file_name, file_data_b64):
    """Crea un registro directamente en el modelo custom sale.order.attachment vinculado a la SO."""
    try:
        attachment_id = models.execute_kw(db, uid, password, 'sale.order.attachment', 'create', [{
            'file_name': file_name,  # Match con fields.Char("Nombre del Archivo")
            'attachment': file_data_b64,  # Match con fields.Binary("Archivo")
            'so_id': so_id,  # Match con fields.Many2one("sale.order")
        }])

        if attachment_id:
            logger.info(f"Odoo: ÉXITO. Se adjuntó '{file_name}' al modelo sale.order.attachment para la SO {so_id}.")
        else:
            logger.error(f"Odoo: FALLO. No se pudo crear adjunto para la SO {so_id}.")
    except Exception as e:
        logger.error(f"Odoo: FALLO. Error al adjuntar '{file_name}' a la SO {so_id}: {e}")


def consolidate_and_attach_labels_odoo(models, db, uid, password, so_id, labels: list, so_name: str):
    """
    Consolida múltiples guías en un solo archivo y lo adjunta al picking de Odoo.
    Prioritiza PDF (convertido o de URL), con ZPL.txt como fallback.
    """
    if not labels:
        logger.warning("Odoo: No hay etiquetas (labels) para consolidar y adjuntar.")
        return

    first_label = labels[0]
    file_data_b64 = None
    file_name = None

    try:
        # --- NUEVA LÓGICA DE CONSOLIDACIÓN ---
        # 1. Determinar si es un trabajo de PDF
        # Si CUALQUIER guía es PDF (de URL o convertida)
        is_pdf_job = any(l.get('pdf_url') or l.get('pdf_bytes') for l in labels)

        if is_pdf_job:
            # --- Consolidación de PDF (Prioridad) ---
            logger.info(f"Odoo: Consolidando {len(labels)} guías en un solo PDF...")
            pdf_merger = PyPDF2.PdfMerger()

            for label in labels:
                pdf_file_stream = None

                if label.get('pdf_bytes'):
                    # 1. Usar PDF convertido (bytes)
                    pdf_file_stream = io.BytesIO(label['pdf_bytes'])

                elif label.get('pdf_url'):
                    # 2. Descargar PDF de URL
                    try:
                        pdf_response = requests.get(label['pdf_url'], timeout=20)
                        pdf_response.raise_for_status()
                        pdf_file_stream = io.BytesIO(pdf_response.content)
                    except Exception as e:
                        logger.error(f"Odoo: Error al descargar PDF {label.get('tracking_number')}: {e}")
                        continue  # Omitir este PDF si falla la descarga

                else:
                    # 3. Fallback (ej. un ZPL no se pudo convertir)
                    logger.warning(
                        f"Odoo: Omitiendo guía {label.get('tracking_number')} (no es PDF) en consolidación de PDF.")
                    continue

                # Añadir el PDF al consolidador
                if pdf_file_stream:
                    pdf_merger.append(pdf_file_stream)

            merged_pdf_io = io.BytesIO()
            pdf_merger.write(merged_pdf_io)
            pdf_merger.close()

            file_data_b64 = base64.b64encode(merged_pdf_io.getvalue()).decode('utf-8')
            file_name = f"{so_name}.pdf"  # Usar el nombre de la SO

        elif first_label.get('zpl'):
            # --- Consolidación de ZPL (Solo si NO hay NINGÚN PDF) ---
            logger.warning(f"Odoo: Consolidando guías como ZPL (fallback).")
            all_zpl_strings = []
            for label in labels:
                zpl_data = label.get('zpl')
                if zpl_data:
                    all_zpl_strings.append(zpl_data)

            consolidated_zpl_str = "\n\n".join(all_zpl_strings)
            file_data_b64 = base64.b64encode(consolidated_zpl_str.encode('utf-8')).decode('utf-8')
            file_name = f"{so_name}.zpl.txt"  # Usar el nombre de la SO

        else:
            logger.error("Odoo: Las guías no son ni PDF ni ZPL. No se puede adjuntar.")
            return

        # Adjuntar el archivo único (PDF o ZPL.txt)
        if file_data_b64 and file_name:
            attach_file_to_so_attachment(models, db, uid, password, so_id, file_name, file_data_b64)
    except Exception as e:
        logger.error(f"Odoo: Error fatal durante la consolidación de guías: {e}", exc_info=True)


def insert_log_message_pick(models, db, uid, password, picking_id, so_name: str):
    current_utc_time = datetime.now()
    cdmx_time = current_utc_time - timedelta(hours=6)
    current_datetime = cdmx_time.strftime('%Y-%m-%d %H:%M:%S')
    models.execute_kw(
        db, uid, password,
        'stock.picking', 'message_post',
        [[picking_id]],
        {
            'body': f'{current_datetime}. Se insertó la(s) guía(s) de Coppel para la orden {so_name} mediante automatización'}
    )


def insert_log_message_sale(models, db, uid, password, so_id, so_name: str):
    current_utc_time = datetime.now()
    cdmx_time = current_utc_time - timedelta(hours=6)
    current_datetime = cdmx_time.strftime('%Y-%m-%d %H:%M:%S')
    models.execute_kw(
        db, uid, password,
        'sale.order', 'message_post',
        [[so_id]],
        {
            'body': f'{current_datetime}. Se insertó la(s) guía(s) de Coppel para la orden {so_name} mediante automatización'}
    )


# --- FUNCIONES DE MIRAKL  ---

def check_order_flex_via_api(order: dict, mirakl_headers: dict, flex_cache: dict) -> bool:
    """Verifica si la orden es Flex consultando el endpoint OF22 para cada offer_id."""
    for line in order.get('order_lines', []):
        offer_id = str(line.get('offer_id', ''))
        if not offer_id:
            continue

        # Revisar en caché
        if offer_id in flex_cache:
            if flex_cache[offer_id]:
                return True
            continue

        # Consultar endpoint OF22 si el offer_id no está en caché
        url = f"{MIRAKL_API_BASE_URL}/offers/{offer_id}"
        try:
            response = requests.get(url, headers=mirakl_headers, timeout=20)
            response.raise_for_status()
            offer_data = response.json()

            is_flex_offer = False
            for field in offer_data.get('offer_additional_fields', []):
                if field.get('code') == 'cfflex' and str(field.get('value', '')).lower() == 'true':
                    is_flex_offer = True
                    break

            # Guardamos en caché el resultado de este offer_id
            flex_cache[offer_id] = is_flex_offer

            if is_flex_offer:
                return True

        except Exception as e:
            logger.error(f"Error consultando OF22 para offer_id {offer_id} de la orden {order.get('order_id')}: {e}")
            # Si falla la API de ofertas, no cacheamos para permitir reintento,
            # pero asumimos False temporalmente para esta línea.

    return False


def fetch_pending_orders_from_mirakl(headers_mirakl: dict) -> list:
    """Extrae y filtra TODAS las órdenes en estado SHIPPING usando paginación (offset)"""
    limit = 100
    offset = 0
    valid_orders = []
    total_orders_fetched = 0
    flex_cache = {}  # Caché en memoria para los offer_id

    logger.info("Mirakl: Buscando órdenes pendientes con paginación...")

    while True:
        url = f"{MIRAKL_API_BASE_URL}/orders?order_state_codes=SHIPPING&max={limit}&offset={offset}"

        try:
            response = requests.get(url, headers=headers_mirakl, timeout=40)
            response.raise_for_status()
            data = response.json()

            batch_orders = data.get('orders', [])

            # Si el lote viene vacío, significa que ya llegamos al final de las páginas
            if not batch_orders:
                break

            total_orders_fetched += len(batch_orders)
            logger.info(f"Mirakl: Descargando lote... (Offset: {offset}, Obtenidas: {len(batch_orders)})")

            for order in batch_orders:
                order_id = order.get('order_id', 'Desconocido')

                # ========================================================
                # --- BLOQUE TEMPORAL PARA PRUEBA EN PRODUCCIÓN ---
                # if order_id != "308562644-B":
                #     continue
                # ========================================================

                # --- FILTROS DE VALIDACIÓN EN MEMORIA ---

                if order.get('shipping_tracking') is not None:
                    continue  # Ya tiene guía

                if order.get('shipping_carrier_code') is not None:
                    continue  # Ya tiene carrier

                if order.get('can_shop_ship') is False:
                    continue  # Sin permiso de envío

                if order.get('fully_refunded') is True:
                    continue  # Totalmente reembolsada

                if order.get('has_incident') is True:
                    continue  # Tiene un incidente abierto

                # Consulta a OF22 a través de la función con caché
                if not check_order_flex_via_api(order, headers_mirakl, flex_cache):
                    continue  # No es Flex (ninguna oferta tiene cfflex = true)

                # Si pasa todos los filtros, es una orden procesable
                valid_orders.append(order)

            # Sumamos 100 al offset para pedir la siguiente página en el próximo ciclo
            offset += limit

        except requests.exceptions.RequestException as e:
            logger.error(f"Error al obtener órdenes de Mirakl (Offset {offset}): {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Detalle: {e.response.text}")
            break  # Rompemos el ciclo si hay un error de conexión

    logger.info(
        f"Mirakl: Extracción finalizada. Total descargadas: {total_orders_fetched}. Total válidas (Flex) para procesar: {len(valid_orders)}.")
    return valid_orders


def post_shipments_to_mirakl(mirakl_headers: dict, order: dict, labels: list, mirakl_carrier_map: dict):
    """
    Publica las guías en Mirakl haciendo Split (ST01).
    Asigna 1 guía por unidad comprada, permitiendo envíos multicaja correctos.
    """
    url = f"{MIRAKL_API_BASE_URL}/shipments"

    if not labels or not order.get('order_lines'):
        logger.error("Mirakl ST01: No hay guías o order_lines en la lista. No se puede reportar shipment.")
        return

    # Mapeo de la cantidad comprada originalmente por SKU
    lines_remaining_qty = {}
    for line in order['order_lines']:
        offer_sku = line['offer_sku']
        if offer_sku not in lines_remaining_qty:
            lines_remaining_qty[offer_sku] = 0
        lines_remaining_qty[offer_sku] += int(line['quantity'])

    shipments_payload = []

    for label in labels:
        offer_sku = label.get('offer_sku')
        tracking_num = label['tracking_number']
        carrier_name = label['provider']

        carrier_name_lower = carrier_name.lower().replace(" ", "")
        carrier_code = mirakl_carrier_map.get(carrier_name_lower, carrier_name_lower)

        # Validamos cuánta cantidad de este SKU nos falta por asignarle guía
        qty_for_this_label = 0
        if offer_sku in lines_remaining_qty and lines_remaining_qty[offer_sku] > 0:
            qty_for_this_label = 1  # Asignamos 1 unidad a esta caja
            lines_remaining_qty[offer_sku] -= 1

        if qty_for_this_label > 0:
            shipments_payload.append({
                "order_id": order['order_id'],
                "shipped": False,  # No se pone ENVIADO
                "tracking": {
                    "carrier_name": carrier_name,
                    "carrier_standard_code": carrier_code,
                    "tracking_number": tracking_num
                },
                "shipment_lines": [
                    {
                        "offer_sku": offer_sku,
                        "quantity": qty_for_this_label
                    }
                ]
            })
        else:
            logger.warning(
                f"Mirakl ST01: Guía {tracking_num} para SKU {offer_sku} es una caja adicional. Se omite de ST01, pero se subirá en OR74.")

    if shipments_payload:
        logger.info(f"Mirakl ST01: Preparando {len(shipments_payload)} shipments para la orden {order['order_id']}.")
        try:
            response = requests.post(url, headers=mirakl_headers, json={"shipments": shipments_payload}, timeout=30)
            response.raise_for_status()

            response_data = response.json()
            errors = response_data.get('shipment_errors', [])
            success = response_data.get('shipment_success', [])

            if success:
                logger.info(f"Mirakl ST01: {len(success)} shipments creados exitosamente.")
            if errors:
                logger.error(f"Mirakl ST01: {len(errors)} errores al crear shipments: {json.dumps(errors)}")

        except Exception as e:
            logger.error(f"Mirakl ST01: Excepción al llamar a /api/shipments: {e}")


def upload_documents_to_mirakl(mirakl_headers_auth: dict, order_id: str, labels: list):
    """
    Sube los archivos de las guías a Mirakl (OR74).
    Prioritiza PDF (convertido o de URL), con ZPL.txt como fallback.
    """
    logger.info(f"Mirakl OR74: Subiendo {len(labels)} documentos para la orden {order_id}...")
    url = f"{MIRAKL_API_BASE_URL}/orders/{order_id}/documents"

    headers_multipart = mirakl_headers_auth.copy()
    if 'Content-Type' in headers_multipart:
        del headers_multipart['Content-Type']

    for label in labels:
        try:
            tracking_number = label['tracking_number']
            file_content_bytes = None
            file_name = None
            mime_type = None

            # --- LÓGICA DE SELECCIÓN DE ARCHIVO (Prioridad PDF) ---
            if label.get('pdf_bytes'):
                # 1. Usar PDF convertido de ZPL
                file_content_bytes = label['pdf_bytes']
                file_name = f"{tracking_number}.pdf"
                mime_type = "application/pdf"

            elif label.get('pdf_url'):
                # 2. Descargar PDF de URL (ej. eShip)
                pdf_response = requests.get(label['pdf_url'], timeout=20)
                pdf_response.raise_for_status()
                file_content_bytes = pdf_response.content
                file_name = f"{tracking_number}.pdf"
                mime_type = "application/pdf"

            elif label.get('zpl'):
                # 3. Fallback: Usar ZPL como .txt si la conversión falló
                logger.warning(f"Mirakl OR74: Usando fallback ZPL.txt para guía {tracking_number}.")
                file_content_bytes = label['zpl'].encode('utf-8')
                file_name = f"{tracking_number}.txt"
                mime_type = "text/plain"

            else:
                logger.warning(f"Mirakl OR74: Guía {tracking_number} sin archivo (PDF/ZPL). Omitiendo subida.")
                continue

            # --- Preparar payload multipart ---
            order_documents_json = {"order_documents": [{"file_name": file_name, "type_code": "SHIPPING_LABEL"}]}
            files_payload = {
                'order_documents': (None, json.dumps(order_documents_json), 'application/json'),
                'files': (file_name, file_content_bytes, mime_type)
            }

            logger.info(f"Mirakl OR74: Subiendo archivo {file_name}...")
            response = requests.post(url, headers=headers_multipart, files=files_payload, timeout=30)
            response.raise_for_status()

            response_data = response.json()
            if response_data.get('errors_count', 0) > 0:
                logger.error(f"Mirakl OR74: Error al subir {file_name}: {response.text}")
            else:
                logger.info(f"Mirakl OR74: Archivo {file_name} subido exitosamente.")

        except Exception as e:
            logger.error(f"Mirakl OR74: Excepción al subir el documento de la guía {label.get('tracking_number')}: {e}")
            # Continuar con la siguiente guía


# --- FUNCIONES DE GOOGLE SHEETS ---

def authenticate_google_sheets():
    """Autentica la conexión a Google Sheets."""
    try:
        creds_info = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        
        client = gspread.authorize(creds)
        logger.info("Autenticación con Google Sheets exitosa.")
        return client
    except Exception as e:
        logger.error(f"Error de autenticación en Sheets: {e}")
        return None


def log_to_sr_sheet(sh_sr, sheet_name: str, row_data: list):
    """
    Registra una fila de datos en una hoja específica del SPREADSHEET_ID_SR.
    Añade un timestamp (Fecha_Log) al inicio de la fila.
    INCLUYE: Mecanismo de seguridad para verificar dónde se escribió.
    """
    if not sh_sr:
        logger.error(f"Sheet SR: No se puede registrar en '{sheet_name}'. Objeto Spreadsheet no inicializado.")
        return

    # Preparar datos
    log_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    full_row_data = [log_time] + row_data

    try:
        # Refrescamos la selección de la hoja para evitar objetos 'stale'
        sh_sr.fetch_sheet_metadata()
        worksheet = sh_sr.worksheet(sheet_name)

        response = worksheet.append_row(full_row_data, value_input_option='USER_ENTERED')

        updates = response.get('updates', {})
        updated_range = updates.get('updatedRange')  # Ej: 'Guias_generadas!A15:J15'

        logger.info(
            f"Sheet SR: Log exitoso en '{sheet_name}'. Rango: {updated_range}. Orden: {row_data[1] if len(row_data) > 1 else 'N/A'}")

    except gspread.exceptions.APIError as api_err:
        # Si es error 429 (Quota) o 500, esperamos y reintentamos
        logger.warning(f"Sheet SR: Error API ({api_err}). Reintentando en 2 segundos...")
        time.sleep(2)
        try:
            worksheet = sh_sr.worksheet(sheet_name)
            worksheet.append_row(full_row_data, value_input_option='USER_ENTERED')
            logger.info(f"Sheet SR: Log exitoso (en reintento) en '{sheet_name}'.")
        except Exception as e2:
            logger.error(f"Sheet SR: FALLO DEFINITIVO en reintento '{sheet_name}': {e2}")

    except Exception as e:
        logger.error(f"Sheet SR: ERROR FATAL al registrar en '{sheet_name}': {e}", exc_info=True)


# --- FUNCIONES CORE (COTIZACIÓN Y GUÍAS) ---

def get_best_rates_per_box(payload: dict) -> Optional[dict]:
    """
    Llama a /live-rates y selecciona la MEJOR tarifa (más barata) PARA CADA PAQUETE,
    asegurando que sean del mismo carrier/plataforma.

    Agrupa por 'package_id' para manejar paquetes idénticos.
    """
    try:
        logger.info(f"Cotizando en /live-rates para {len(payload['items'])} items...")
        response = requests.post(API_URL_LIVE_RATES, json=payload, auth=API_AUTH, timeout=30)
        response.raise_for_status()
        all_rates = response.json()
        if not all_rates:
            logger.warning("API /live-rates devolvió una lista vacía.")
            return None

        # --- Lógica de Selección de Carrier ---
        rates_by_box = {}
        for rate in all_rates:

            # --- Agrupar por 'package_id' (único) en lugar de 'sku_child' (tipo) ---
            box_id = rate.get('package_id')
            # ---------------------------------------------------------------------------------

            if not box_id:
                logger.warning(f"Omitiendo tarifa sin 'package_id': {rate.get('service_name')}")
                continue

            if box_id not in rates_by_box: rates_by_box[box_id] = []
            rates_by_box[box_id].append(rate)

        if not rates_by_box:
            logger.warning("No se encontraron tarifas asociadas a 'package_id'.")
            return None

        box_keys = list(rates_by_box.keys())
        if not box_keys: return None

        first_box_rates = rates_by_box[box_keys[0]]
        all_available_services = {r['service_code']: r['service_name'] for r in first_box_rates}
        if not all_available_services:
            logger.warning(f"La primera caja ({box_keys[0]}) no tiene tarifas disponibles.")
            return None

        logger.info(f"Servicios disponibles a comparar: {list(all_available_services.values())}")

        best_rates_per_service = {}
        for service_code, service_name in all_available_services.items():
            total_cost = 0
            rates_for_this_service = {}
            possible = True
            for box_id, rates in rates_by_box.items():
                rate_for_box = next((r for r in rates if r['service_code'] == service_code), None)
                if rate_for_box:
                    total_cost += rate_for_box['total_price']
                    rates_for_this_service[box_id] = rate_for_box
                else:
                    possible = False
                    logger.debug(f"Servicio {service_name} descartado (falta en caja {box_id}).")
                    break

            if possible:
                best_rates_per_service[service_code] = {
                    'total_cost_cents': total_cost,
                    'rates_map': rates_for_this_service,
                    'service_name': service_name
                }

        if not best_rates_per_service:
            logger.error("Error catastrófico: Ningún carrier pudo cotizar TODAS las cajas del pedido.")
            return None

        best_service_code = min(best_rates_per_service, key=lambda k: best_rates_per_service[k]['total_cost_cents'])
        best_option = best_rates_per_service[best_service_code]

        logger.info(
            f"Selección final: {best_option['service_name']} (Total: ${best_option['total_cost_cents'] / 100.0:.2f}) para {len(box_keys)} cajas.")

        return best_option['rates_map']

    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión con /live-rates: {e}")
        return 'CONNECTION-ERROR'
    except Exception as e:
        logger.error(f"Error inesperado en get_best_rates_per_box: {e}", exc_info=True)
        return None


def generate_labels(best_rates_map: dict, recipient_data: dict, total_order_value: float) -> list:
    """
    Genera UNA guía por CADA tarifa seleccionada.
    Si la guía es ZPL, intenta convertirla a PDF.
    """
    generated_labels = []
    num_boxes = len(best_rates_map)
    value_per_box = total_order_value / num_boxes if num_boxes > 0 else 0

    # best_rates_map usa 'package_id' como clave, pero 'rate' contiene el resto de la info
    for box_id, rate in best_rates_map.items():
        logger.info(f"Generando guía para paquete {box_id} con {rate['service_name']}...")

        offer_sku = rate.get('sku_parent')  # El sku_parent es el offer_sku de Mirakl
        sku_child = rate.get('sku_child')  # El sku_child se usa para buscar medidas

        payload = {
            "service_code": rate['service_code'],
            "rate_id": rate['rate_id'],
            "shipper": SHIPPER_DATA,
            "recipient": recipient_data,
            "sku": sku_child,  # Enviar SKU hijo para que app.py busque medidas
            "data_sat": {
                "bienesTransp": "50161815",
                "valorMercancia": value_per_box
            }
        }

        try:
            response = requests.post(API_URL_GENERATE_LABEL, json=payload, auth=API_AUTH, timeout=45)
            if response.status_code == 200:
                label_data = response.json()
                if label_data.get('tracking_number'):
                    provider_name = "Desconocido"
                    if 'service_name' in rate:
                        provider_name = rate['service_name'].split(' - ')[0]

                    # --- INICIO DE LÓGICA DE CONVERSIÓN ZPL ---
                    pdf_url = label_data.get('pdf_url')
                    zpl_data = label_data.get('zpl')
                    pdf_bytes_data = None

                    if zpl_data and not pdf_url:
                        # Convertir ZPL a PDF
                        tracking_num = label_data['tracking_number']
                        logger.info(f"  -> Guía {tracking_num} es ZPL. Intentando convertir a PDF vía Labelary...")
                        pdf_bytes_data = convert_zpl_to_pdf_bytes(zpl_data)
                        if pdf_bytes_data:
                            logger.info(f"  -> [ÉXITO] Conversión ZPL a PDF completada.")
                        else:
                            logger.warning(f"  -> [FALLO] No se pudo convertir ZPL. Se usará .txt como fallback.")

                    generated_labels.append({
                        'box_id': box_id,  # Este es el package_id
                        'offer_sku': offer_sku,  # Par MIRAKL ST01
                        'sku_child': sku_child,  # SKU hijo/caja, para el JSON de tools.shipping_labels
                        'tracking_number': str(label_data['tracking_number']),
                        'provider': provider_name,
                        'service_name': rate.get('service_name'),  # Nombre completo del servicio (carrier_service_level)
                        'shipping_label_cost': rate.get('total_price', 0) / 100.0,  # Costo de ESTA guía/caja
                        'pdf_url': pdf_url,  # El original (ej. de eShip)
                        'zpl': zpl_data,  # El ZPL original (como fallback)
                        'pdf_bytes': pdf_bytes_data,  # los bytes del PDF
                        'carrier_odoo_id': get_carrier_odoo_id(provider_name)
                    })
                    logger.info(f"  -> [ÉXITO] Guía generada para {box_id}: {label_data['tracking_number']}")
                else:
                    logger.error(f"  -> [ERROR] API respondió 200 pero sin tracking para {box_id}: {label_data}")
            else:
                logger.error(f"  -> [FALLO] API respondió {response.status_code} para {box_id}: {response.text}")
        except Exception as e:
            logger.error(f"  -> [EXCEPCIÓN] Fallo al llamar /generate-label para {box_id}: {e}")

    return generated_labels


# --- BUCLE PRINCIPAL REFACTORIZADO ---

def procesar_ordenes_coppel():
    logger.info("=== Iniciando Procesamiento Flex Híbrido (100% API) ===")

    conn = get_db_connection()
    # setup_database(conn) Creacion de tablas


    gc = authenticate_google_sheets()
    if not gc: return
    logger.info("Conectado a Google Sheets.")

    # --- Conexión al Sheet de Logs (SR) ---
    try:
        sh_sr = gc.open_by_key(SPREADSHEET_ID_SR)
        logger.info(f"Conectado a Google Sheet de Logs (SR): {SPREADSHEET_ID_SR}")
    except Exception as e:
        logger.error(f"No se pudo abrir el Google Sheet de Logs (SR) con ID {SPREADSHEET_ID_SR}: {e}")
        sh_sr = None  # El script continuará, pero sin loguear en SR

    #Conexión Odoo
    models, db, uid, password = connect_to_odoo(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)
    if not models: return
    logger.info("Conectado a Odoo.")

    #Headers Mirakl
    headers_mirakl = {
        "Authorization": API_KEY_MIRAKL,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    #Cargar Mapa de Carriers de Mirakl
    MIRAKL_CARRIER_MAP = load_carrier_map_from_json(CARRIER_JSON)
    if not MIRAKL_CARRIER_MAP:
        logger.critical("No se pudo cargar el mapa de carriers de Mirakl. El script no puede continuar.")
        return

    #Obtener órdenes directo de Mirakl
    orders_to_process = fetch_pending_orders_from_mirakl(headers_mirakl)
    if not orders_to_process:
        logger.info("No hay órdenes SHIPPING pendientes para procesar.")
        if conn: conn.close()
        return

    #Invertimos la lista para procesar en orden de más antiguas a más recientes
    orders_to_process.reverse()

    for order in orders_to_process:
        order_id = order['order_id']
        base_order_id = order_id.split('-')[0]

        # Validar el estatus de la orden en la BD
        db_sheet = get_order_db_sheet(conn, order_id)

        # Si ya se procesó con éxito o es Fulfillment, la omitimos por completo y ahorramos procesamiento
        if db_sheet in ['Guias_generadas', 'Fulfillment']:
            logger.info(f"Orden {order_id} ya procesada exitosamente o permanentemente ({db_sheet}). Omitiendo.")
            continue

        # Función auxiliar para registrar en Sheets y DB evitando el spam
        def smart_log(sheet_name, data):
            if db_sheet == sheet_name:
                logger.info(f"  -> Orden {order_id}: Ya registrada en '{sheet_name}'. Evitando spam en Sheets.")
            else:
                log_to_sr_sheet(sh_sr, sheet_name, data)
                update_order_db_sheet(conn, order_id, sheet_name, data)

        # Extracción de SKUs
        skus_list = []
        for line in order.get('order_lines', []):
            skus_list.append(line.get('offer_sku', ''))
        skus_str = ", ".join(skus_list)

        # Fecha de creación para logs de Sheets SR
        raw_date = order.get('created_date', '')
        fecha_orden = raw_date.split('T')[0] if raw_date else datetime.now().strftime('%Y-%m-%d')

        logger.info(f"\n>>> Procesando Orden: {order_id} <<<")

        # --- Filtros de Ejecución ---
        if order.get('shipping_tracking') is not None:
            logger.info(f"Orden {order_id} ya tiene guía ({order['shipping_tracking']}). Omitiendo.")
            continue

        # ------- Validacion de orden en Odoo --------------------
        so_id, so_name, status_raw = search_sale_order_by_mkt_ref(models, db, uid, password, order_id)
        if not so_id:
            so_id, so_name, status_raw = search_sale_order_by_mkt_ref(models, db, uid, password, base_order_id)
            status = order_status_list.get(status_raw)
        else:
            status = order_status_list.get(status_raw)

        picking_id, pick_status_raw = None, None
        if so_name:
            picking_id, pick_status_raw = search_picking_id(models, db, uid, password, so_name)
            pick_status = pickin_status_list.get(pick_status_raw)
        # --------------------------------------------------------

        if not so_id:
            reason = f"La orden NO existe en Odoo"
            log_data = [fecha_orden, order_id, so_name or 'N/A', skus_str, reason]
            smart_log('Sin_cobertura', log_data)
            continue

        if status_raw != 'done':
            reason = f"La orden está {status.upper() if status else status_raw} en Odoo"
            log_data = [fecha_orden, order_id, so_name, skus_str, status, pick_status]
            smart_log('SO_no_bloqueadas / canceladas', log_data)
            continue

        fulfillment_code = order["fulfillment"]["center"]["code"]
        if fulfillment_code.lower() == 'coppel':
            reason = f"La orden es Fulfillment"
            log_data = [fecha_orden, order_id, so_name, skus_str, fulfillment_code]
            smart_log('Fulfillment', log_data)
            continue

        if not picking_id or pick_status_raw != 'assigned':
            reason = f"La orden tiene el PICK {pick_status}" if picking_id else "La orden NO tiene PICK"
            if reason == "La orden NO tiene PICK":
                pick_status = "La orden NO tiene PICK"
            log_data = [fecha_orden, order_id, so_name, skus_str, status, pick_status]
            smart_log('PICK-pendiente', log_data)
            continue

        if order.get('order_state') != 'SHIPPING':
            logger.warning(f"Omitiendo orden {order_id}: Estado es '{order.get('order_state')}'")
            continue

        # --- PASO A: Extraer datos de Mirakl ---
        try:
            customer = order.get('customer', {})
            shipping_addr = customer.get('shipping_address', {})
            firstname = customer.get('firstname', '').strip()
            lastname = customer.get('lastname', '').strip()
            full_name = f"{firstname} {lastname}".strip() or "Cliente Coppel"
            recipient_data = {
                "name": full_name, "company": "",
                "email": customer.get('customer_notification_email', 'no-email@coppel.com'),
                "phone": shipping_addr.get('phone', '0000000000')[:10],
                "street1": shipping_addr.get('street_1', '.'),
                "street2": shipping_addr.get('street_2', ''),
                "city": shipping_addr.get('city', ''),
                "state": shipping_addr.get('state', ''),
                "country": shipping_addr.get('country', 'MX'),
                "zip": shipping_addr.get('zip_code', '')
            }
            items_to_quote = []
            total_order_value = 0.0
            for line in order.get('order_lines', []):
                items_to_quote.append({
                    "sku": line['offer_sku'], "quantity": int(line['quantity']),
                    "price": float(line['price']), "name": line.get('product_title', 'Producto Coppel')
                })
                total_order_value += float(line['price'])
            if total_order_value == 0: raise ValueError("Valor total de la orden es 0.")
        except Exception as e:
            reason = f"Error al extraer datos de Mirakl: {e}"
            logger.error(f"Orden {order_id}: {reason}")
            log_data = [fecha_orden, order_id, so_name, skus_str, reason]
            smart_log('Sin_cobertura', log_data)
            continue

        # --- PASO B: Cotizar (Multi-Caja) ---
        payload_rates = {
            "origin": {"zip": ORIGIN_ZIP, "country": "MX"},
            "destination": recipient_data, "items": items_to_quote
        }
        best_rates_map = get_best_rates_per_box(payload_rates)
        if not isinstance(best_rates_map, dict):
            if best_rates_map == None:
                reason = "API no devolvió tarifas válidas"
                log_data = [fecha_orden, order_id, so_name, skus_str, reason]
                smart_log('Sin_cobertura', log_data)
                continue
            elif best_rates_map == 'CONNECTION-ERROR':
                reason = "Error conexión con API SRS"
                log_data = [fecha_orden, order_id, so_name, skus_str, reason]
                smart_log('Sin_cobertura', log_data)
                continue
            else:
                reason = f"Error desconocido. {type(best_rates_map)}"
                continue

        # --- PASO C: Validar Costo Total ---
        total_shipping_cost_cents = sum(r['total_price'] for r in best_rates_map.values())
        total_shipping_cost_mxn = total_shipping_cost_cents / 100.0
        cost_ratio = total_shipping_cost_mxn / total_order_value
        if cost_ratio > PERCENTAGE_COST_LIMIT:
            reason_int = f"Costo de envío excesivo: ${total_shipping_cost_mxn:.2f} ({cost_ratio:.1%})"
            reason = 'No fue posible generar guía: RSL'  # Rate Superior al Limite
            logger.warning(f"Orden {order_id} RECHAZADA: {reason_int}")
            log_data = [fecha_orden, order_id, so_name, skus_str, f'${total_shipping_cost_mxn:.2f}',
                        f'${total_order_value:.2f}', f'{cost_ratio:.001%}']
            smart_log('Costo_guia_excesivo', log_data)

            # --- NUEVO: registro adicional en tools.shipping_labels (no sustituye smart_log/BD existentes) ---
            for line in order.get('order_lines', []):
                # `tools.shipping_labels` es a nivel SKU: se registra el costo
                # cotizado de las cajas de ESTE SKU, no el total de la orden.
                sku_shipping_cost = sku_shipping_cost_from_rates(
                    best_rates_map, line.get('offer_sku'),
                    total_fallback=total_shipping_cost_mxn
                )
                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Coppel',
                    sku=line.get('offer_sku'),
                    qty_ordered=int(line.get('quantity', 0)),
                    status='LIMIT_RATIO_OVERCOME',
                    label_generated=False,
                    label_origin='SRS_GENERATED',
                    tracking_number=None,
                    shipping_cost=sku_shipping_cost,
                    carrier=None,
                    carrier_service_level=None,
                    error_log=reason_int
                )
            continue

        logger.info(f"Orden {order_id} APROBADA. Generando {len(best_rates_map)} guías...")

        # --- PASO D: Generar Guías ---
        labels = generate_labels(best_rates_map, recipient_data, total_order_value)
        if not labels or len(labels) != len(best_rates_map):
            reason = f"Fallo al generar guías (Se generaron {len(labels)} de {len(best_rates_map)})."
            logger.error(f"Orden {order_id}: {reason}")

            # --- LÓGICA DE GUIAS PARCIALES ---
            if not labels:
                logger.warning(f"Orden {order_id}: No se generó NINGUNA guía.")
                log_data = [fecha_orden, order_id, so_name, skus_str, reason, "NINGUNA", "N/A", "N/A", "N/A", "", "N/A"]
                smart_log('Guias_incompletas', log_data)

                #registro adicional en tools.shipping_labels (no sustituye smart_log/BD existentes) ---
                # Análogo a Amazon: cuando NO se generó ninguna guía para la orden -> SKU_NOT_SUPPORT
                for line in order.get('order_lines', []):
                    # Costo cotizado de las cajas de ESTE SKU (registro a nivel SKU).
                    sku_shipping_cost = sku_shipping_cost_from_rates(
                        best_rates_map, line.get('offer_sku'),
                        total_fallback=total_shipping_cost_mxn
                    )
                    insert_shipping_label(
                        conn,
                        marketplace_id=order_id,
                        marketplace='Coppel',
                        sku=line.get('offer_sku'),
                        qty_ordered=int(line.get('quantity', 0)),
                        status='SKU_NOT_SUPPORT',
                        label_generated=False,
                        label_origin='SRS_GENERATED',
                        tracking_number=None,
                        shipping_cost=sku_shipping_cost,
                        carrier=None,
                        carrier_service_level=None,
                        error_log=reason
                    )
            else:
                logger.warning(
                    f"Orden {order_id}: Guardando {len(labels)} guía(s) generada(s) en 'Guias_incompletas'...")
                for label in labels:
                    tracking_num_str = "'" + label.get('tracking_number', 'ERROR_NO_TRACKING')
                    provider_name = label.get('provider', 'N/A')
                    source = label.get('source', 'N/A')
                    file_data_b64 = ""
                    file_type = "N/A"

                    try:
                        if label.get('pdf_bytes'):
                            file_data_b64 = base64.b64encode(label['pdf_bytes']).decode('utf-8')
                            file_type = "PDF (de ZPL)"
                        elif label.get('pdf_url'):
                            pdf_response = requests.get(label.get('pdf_url'), timeout=20)
                            pdf_response.raise_for_status()
                            file_data_b64 = base64.b64encode(pdf_response.content).decode('utf-8')
                            file_type = "PDF (de URL)"
                        elif label.get('zpl'):
                            file_data_b64 = base64.b64encode(label['zpl'].encode('utf-8')).decode('utf-8')
                            file_type = "ZPL (Fallback)"
                    except Exception as e:
                        logger.error(f"No se pudo codificar/descargar el archivo para la guía {tracking_num_str}: {e}")
                        file_type = f"Error al procesar archivo: {e}"

                    log_data = [fecha_orden, order_id, so_name, skus_str, reason, tracking_num_str, provider_name,
                                source,
                                file_type,
                                file_data_b64, "Aun No"]
                    smart_log('Guias_incompletas', log_data)

                #registro en tools.shipping_labels, un renglón por SKU, análogo a Amazon ---
                # Los SKUs que sí obtuvieron guía -> LABELS_GENERATED; los que no -> NO_LABEL_FOR_SKU.
                for line in order.get('order_lines', []):
                    sku = line.get('offer_sku')
                    qty = int(line.get('quantity', 0))
                    labels_for_sku = [l for l in labels if l.get('offer_sku') == sku]

                    if labels_for_sku:
                        # Costo de envío de ESTE SKU: suma de TODAS sus cajas.
                        sku_shipping_cost = sku_shipping_cost_from_labels(labels_for_sku)

                        main_carrier = labels_for_sku[0]['provider']
                        main_carrier = 'PAQUETEXPRESS' if main_carrier == 'PAQUETEEXPRESS' else main_carrier
                        main_service = labels_for_sku[0].get('service_name')
                        tracking_json_list = [{
                            "carrier": 'PAQUETEXPRESS' if l['provider'] == 'PAQUETEEXPRESS' else l['provider'],
                            "sku_child": l.get('sku_child'),
                            "package_id": l.get('box_id'),
                            "tracking_number": l['tracking_number'],
                            "shipping_label_cost": l.get('shipping_label_cost')
                        } for l in labels_for_sku]

                        insert_shipping_label(
                            conn,
                            marketplace_id=order_id,
                            marketplace='Coppel',
                            sku=sku,
                            qty_ordered=qty,
                            status='LABELS_GENERATED',
                            label_generated=True,
                            label_origin='SRS_GENERATED',
                            tracking_number=tracking_json_list,
                            shipping_cost=sku_shipping_cost,
                            carrier=main_carrier,
                            carrier_service_level=main_service,
                            error_log=None
                        )
                    else:
                        # Sin guías para este SKU: se conserva el costo cotizado
                        # de sus cajas (nunca el total de la orden).
                        sku_shipping_cost = sku_shipping_cost_from_rates(
                            best_rates_map, sku,
                            total_fallback=total_shipping_cost_mxn
                        )
                        insert_shipping_label(
                            conn,
                            marketplace_id=order_id,
                            marketplace='Coppel',
                            sku=sku,
                            qty_ordered=qty,
                            status='NO_LABEL_FOR_SKU',
                            label_generated=False,
                            label_origin='SRS_GENERATED',
                            tracking_number=None,
                            shipping_cost=sku_shipping_cost,
                            carrier=None,
                            carrier_service_level=None,
                            error_log=reason
                        )
            continue

        # --- PASO E: Actualizar Mirakl (ST01 y OR74) ---
        logger.info(f"Iniciando actualización de Mirakl para la orden {order_id}...")
        post_shipments_to_mirakl(headers_mirakl, order, labels, MIRAKL_CARRIER_MAP)
        upload_documents_to_mirakl(headers_mirakl, order_id, labels)

        # --- PASO F: Actualizar Odoo (SO y Picking) ---
        logger.info(f"Iniciando actualización de Odoo para la orden {order_id}...")
        if so_id and so_name:
            tracking_numbers_str = ",".join([l['tracking_number'] for l in labels])
            carrier_odoo_id = labels[0]['carrier_odoo_id']
            num_packages = len(labels)
            client_reference = full_name

            update_sale_order(models, db, uid, password, so_id, tracking_numbers_str, carrier_odoo_id,
                              num_packages, client_reference)

            if picking_id:
                consolidate_and_attach_labels_odoo(models, db, uid, password, so_id, labels, so_name)
                insert_log_message_sale(models, db, uid, password, so_id, so_name)
            else:
                logger.error(f"Odoo: No se encontró PICKING para SO {so_name}. Las guías no fueron adjuntadas.")
        else:
            logger.error(f"Odoo: No se encontró SO para MKT Ref {order_id}. No se pudo actualizar Odoo.")

        # --- PASO G: Actualizar Sheet (Éxito Log SR) ---
        try:
            tracking_numbers_str = "'" + ",".join([l['tracking_number'] for l in labels])
            provider_name = labels[0]['provider'] if labels else 'N/A'
            log_data = [fecha_orden, order_id, so_name, skus_str, tracking_numbers_str, provider_name,
                        f'${total_shipping_cost_mxn:.2f}', f'${total_order_value:.2f}', f'{cost_ratio:.001%}']
            smart_log('Guias_generadas', log_data)

            # --- NUEVO: registro adicional en tools.shipping_labels, uno por SKU (no sustituye smart_log/BD existentes) ---
            order_lines = order.get('order_lines', [])
            for line in order_lines:
                sku = line.get('offer_sku')
                labels_for_sku = [l for l in labels if l.get('offer_sku') == sku]
                if not labels_for_sku:
                    continue

                # Costo de envío de ESTE SKU: suma de TODAS sus cajas (mismo
                # valor que la suma de `shipping_label_cost` del JSON de abajo).
                sku_shipping_cost = sku_shipping_cost_from_labels(labels_for_sku)

                main_carrier = labels_for_sku[0]['provider']
                main_carrier = 'PAQUETEXPRESS' if main_carrier == 'PAQUETEEXPRESS' else main_carrier
                main_service = labels_for_sku[0].get('service_name')

                # JSON completo por caja, igual que en el script de Amazon:
                # carrier, sku_child, package_id, tracking_number y shipping_label_cost por guía.
                tracking_json_list = [{
                    "carrier": 'PAQUETEXPRESS' if l['provider'] == 'PAQUETEEXPRESS' else l['provider'],
                    "sku_child": l.get('sku_child'),
                    "package_id": l.get('box_id'),
                    "tracking_number": l['tracking_number'],
                    "shipping_label_cost": l.get('shipping_label_cost')
                } for l in labels_for_sku]

                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Coppel',
                    sku=sku,
                    qty_ordered=int(line.get('quantity', 0)),
                    status='LABELS_GENERATED',
                    label_generated=True,
                    label_origin='SRS_GENERATED',
                    tracking_number=tracking_json_list,
                    shipping_cost=sku_shipping_cost,  # costo de envío de ESTE SKU (todas sus cajas)
                    carrier=main_carrier,
                    carrier_service_level=main_service,
                    error_log=None
                )
        except Exception as e_log_sr:
            logger.error(f"Sheet SR: Error al registrar éxito para {order_id}: {e_log_sr}")

        time.sleep(4)

    if conn: conn.close()
    logger.info("=== Procesamiento finalizado ===")


if __name__ == "__main__":
    procesar_ordenes_coppel()