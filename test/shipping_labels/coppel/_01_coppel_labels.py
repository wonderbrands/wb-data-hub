import time
import requests
import json
import logging
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import xmlrpc.client
import base64
import os
import io
import PyPDF2
import dotenv
from typing import Optional, List

# *******************
is_test = False
# *******************

#ENV_PATH = '.env'
ENV_PATH = '/var/lib/jenkins/m1/.env' #JENKINS
#ENV_PATH = r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\Tools\ML_odoo18\.env2'
TEST_STR = "_TEST" if is_test else ""
dotenv.load_dotenv(dotenv_path=ENV_PATH)

# ------------------------------------------------------------
from _00_load_carriers_map import load_carrier_map_from_json
#from load_carriers_map import load_carrier_map_from_json

# ------------------------------------------------------------

# --- CONFIGURACIÓN PRINCIPAL ---
API_KEY_MIRAKL = os.getenv('API_KEY_MIRAKL')
API_URL_LIVE_RATES = "https://wonder-site.duckdns.org/live-rates"
API_URL_GENERATE_LABEL = "https://wonder-site.duckdns.org/generate-label"
MIRAKL_API_BASE_URL = "https://coppel-prod.mirakl.net/api"



# --- CONFIGURACIÓN DE GOOGLE SHEETS ---
SPREADSHEET_ID = os.getenv(f'SPREADSHEET_COPPEL_ID{TEST_STR}')
SPREADSHEET_ID_SR = os.getenv(f'SPREADSHEET_COPPEL_ID_SR')

# Ruta de credenciales
#CREDENTIALS_JSON_PATH = r'C:\Users\Sergio Gil Guerrero\PycharmProjects\Herramientas propias\coppel_api\shipping_info_coppel.json'
CREDENTIALS_JSON_PATH = '/var/lib/jenkins/m1/shipping_info_coppel.json'

#CARRIER_JSON = 'carrier_map.json'
CARRIER_JSON = '/var/lib/jenkins/workspace/00_Repo/00_Pull/03_Tools/41_Coppel_SRS_get_labels/carrier_map.json'

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

logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()
logger.info(f"Nivel de logging establecido en: {log_level_str}")


# --------------------------------------------------------------------------
def load_dynamic_config():
    """
    Carga la configuración (Worksheet, Límite, ZIP) desde SPREADSHEET_ID_SR.
    Si falla, se usa los valores harcodeados como backup.
    """
    # --- Valores por defecto (backup) ---
    default_worksheet = 'Guías de envío 2025'
    default_percentage = 0.21
    default_zip = '54010'

    # Valores que se cargarán
    worksheet_name = default_worksheet
    percentage_limit = default_percentage
    origin_zip = default_zip

    try:
        # 1. Autenticar (conexión separada)
        creds = Credentials.from_service_account_file(CREDENTIALS_JSON_PATH, scopes=SCOPES)
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
ODOO_URL = os.getenv('odoo_urlV18')
ODOO_DB = os.getenv('odoo_dbV18')
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
                status = 'done' # Retorna done como valor interno, pero en odoo no existe done

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


# --- FUNCIONES DE MIRAKL ---

def post_shipments_to_mirakl(mirakl_headers: dict, order: dict, labels: list, mirakl_carrier_map: dict):
    """
    Publica las guías generadas en Mirakl (ST01).
    Usa el 'offer_sku' para las 'shipment_lines'.

    --- Solo envía la PRIMERA guía (BOX1) y usa el SKU PADRE ---
    """
    url = f"{MIRAKL_API_BASE_URL}/shipments"

    if not labels:
        logger.error("Mirakl ST01: No hay guías (labels) en la lista. No se puede reportar shipment.")
        return

    if not order.get('order_lines'):
        logger.error("Mirakl ST01: La orden no tiene 'order_lines'. No se puede reportar shipment.")
        return


    # 1. Tomar solo la primera guía (asumimos que es BOX1)
    label_box1 = labels[0]

    carrier_name = label_box1['provider']
    tracking_num = label_box1['tracking_number']
    offer_sku = label_box1.get('offer_sku')  # Este es el SKU padre (ej. DRESMULTI6-CAF)

    if not offer_sku:
        logger.error(
            f"Mirakl ST01: No se encontró 'offer_sku' (SKU padre) en la guía {tracking_num}. Omitiendo shipment.")
        return

    # 2. Buscar Carrier Code
    carrier_name_lower = (carrier_name.lower()).replace(" ", "")
    carrier_code = mirakl_carrier_map.get(carrier_name_lower)

    if not carrier_code:
        logger.warning(
            f"Mirakl ST01: No se encontró standard_code para '{carrier_name_lower}'. Usando el nombre como fallback.")
        carrier_code = carrier_name_lower

    # 3. Construir un ÚNICO payload de shipment
    shipment_entry = {
        "order_id": order['order_id'],
        "shipped": False,  # No se pone ENVIADO
        "tracking": {
            "carrier_name": carrier_name,
            "carrier_standard_code": carrier_code,
            "tracking_number": tracking_num
        },
        "shipment_lines": [
            {
                "offer_sku": offer_sku,  # Usar el SKU padre
                "quantity": 1  # Asumimos que la primera guía/caja representa 1 unidad de la línea de pedido
            }
        ]
    }

    # El payload es una lista con un solo elemento
    shipments_payload = [shipment_entry]

    logger.info(
        f"Mirakl ST01: Preparando 1 shipment para {order['order_id']} con guía {tracking_num} (SKU: {offer_sku}).")

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
        creds = Credentials.from_service_account_file(CREDENTIALS_JSON_PATH, scopes=SCOPES)
        client = gspread.authorize(creds)
        logger.info("Autenticación con Google Sheets exitosa.")
        return client
    except Exception as e:
        logger.error(f"Error de autenticación en Sheets: {e}")
        return None


def get_pending_orders_from_sheet(gc: gspread.Client, spreadsheet_id: str) -> dict:
    """Lee el Sheet e identifica pedidos pendientes."""
    try:
        sh = gc.open_by_key(spreadsheet_id)
        worksheet = sh.get_worksheet(0) if is_test else sh.worksheet(WORKSHEET_NAME)
        logger.info(f"Leyendo pedidos pendientes de la hoja: '{worksheet.title}'")

        all_data = worksheet.get_all_values()
        if not all_data: return {}

        headers = all_data[0]
        # Columna A (Fecha Mirakl) y B (Pedido)
        col_fecha_idx = 0
        col_pedido_idx = 1
        col_carrier_idx = 2
        col_estatus_idx = 9
        col_extra_info_idx = 10

        pending_orders = {}
        for i, row in enumerate(all_data[1:]):
            row_num = i + 2
            if len(row) > max(col_pedido_idx, col_carrier_idx, col_estatus_idx, col_extra_info_idx):
                order_id = row[col_pedido_idx].strip()
                carrier = row[col_carrier_idx].strip()
                estatus = row[col_estatus_idx].strip().lower()
                extra_info = row[col_extra_info_idx].strip().lower()

                val_col_A = row[col_fecha_idx].strip()
                val_col_B = order_id

                if order_id and not carrier and estatus == "" and not extra_info:
                    pending_orders[order_id] = {
                        "row_num": row_num,
                        "col_A_val": val_col_A,
                        "col_B_val": val_col_B
                    }
            elif len(row) > col_pedido_idx and row[col_pedido_idx].strip():
                # Log si una fila tiene pedido pero no cumple criterio (para debugging)
                logger.debug(
                    f"Fila {row_num} omitida (ID: {row[col_pedido_idx].strip()}). No cumple criterios de 'pendiente'.")

        logger.info(f"Encontrados {len(pending_orders)} pedidos pendientes en Sheet.")

        return pending_orders
    except gspread.exceptions.WorksheetNotFound:
        logger.error(f"Error al leer: No se encontró la hoja (pestaña) llamada '{WORKSHEET_NAME}'.")
        return {}
    except Exception as e:
        logger.error(f"Error al leer el Google Sheet: {e}", exc_info=True)
        return {}


def update_sheet_success_multi_row(worksheet, order_info: dict, labels_info: list):
    """
    Inserta una FILA NUEVA por cada guía generada.
    """
    carriers_dic_map = {
        'FEDEX': 'FedEx',
        'DHL': 'DHL',
        'ESTAFETA': 'Estafeta',
        'PAQUETEEXPRESS': 'Paquete Express',
        'SEGMAIL': 'Segmail'

    }
    try:
        row_number = order_info['row_num']
        col_A_val = order_info['col_A_val']
        col_B_val = order_info['col_B_val']

        col_carrier_idx = 3  # Columna C
        col_guia_idx = 4  # Columna D
        col_fecha_guia_idx = 5  # Columna E
        col_estatus_idx = 10  # Columna J

        # col_address = order_info['col_L_val']

        now_str = datetime.now().strftime('%d/%m/%Y')

        for i, label in enumerate(labels_info):
            current_row_index = row_number + i

            carrier = carriers_dic_map.get(label['provider'])
            tracking = label['tracking_number']

            if i == 0:
                # --- 1. Actualizar la fila original ---
                logger.info(f"Sheet: Actualizando fila original {current_row_index} con guía {tracking}...")
                cells_to_update = [
                    (current_row_index, col_carrier_idx, carrier),
                    (current_row_index, col_guia_idx, tracking),
                    (current_row_index, col_fecha_guia_idx, now_str),
                    (current_row_index, col_estatus_idx, "Sin recolectar")
                    # (current_row_index, col_address,"")
                ]
                cells_list = []
                for row, col, val in cells_to_update:
                    cells_list.append({
                        'range': gspread.utils.rowcol_to_a1(row, col),
                        'values': [[val]]
                    })
                worksheet.batch_update(cells_list)

            else:
                # --- 2. Insertar fila nueva para las siguientes cajas ---
                logger.info(f"Sheet: Insertando NUEVA fila en {current_row_index} para guía {tracking}...")
                new_row_data = [
                    col_A_val,  # Col A (copiada)
                    col_B_val,  # Col B (copiada)
                    carrier,  # Col C (nueva)
                    tracking,  # Col D (nueva)
                    now_str,  # Col E (nueva)
                    "", "", "", "",  # Cols F, G, H, I (vacías)
                    "Sin recolectar",  # Col J
                    "Guía Generada (Multi-caja)"  # Col K
                    # col_address # Col L copiada
                ]
                worksheet.insert_row(new_row_data, current_row_index, value_input_option='USER_ENTERED')
                time.sleep(2)  # Pausa para la API de GSheets

        logger.info(f"Sheet actualizado (ÉXITO MULTI-FILA) para Pedido {col_B_val}.")

    except Exception as e:
        logger.error(f"Error al actualizar Sheet (ÉXITO MULTI-FILA) empezando en fila {row_number}: {e}", exc_info=True)


def update_sheet_no_coverage(worksheet, row_number: int, reason: str):
    """Marca el pedido como 'Sin cobertura' o con error en el Sheet."""
    try:
        # worksheet.update_cell(row_number, 10, "Sin cobertura/Error")  # Col J
        worksheet.update_cell(row_number, 11, reason)  # Col K
        logger.warning(f"Sheet actualizado (ERROR/SIN COBERTURA) fila {row_number}: {reason}")
    except Exception as e:
        logger.error(f"Error al actualizar Sheet (ERROR) fila {row_number}: {e}")


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


# --- FUNCIONES CORE (COTIZACIÓN Y GUÍAS) (ACTUALIZADO) ---

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
                        'offer_sku': offer_sku, # Par MIRAKL ST01
                        'tracking_number': str(label_data['tracking_number']),
                        'provider': provider_name,
                        'pdf_url': pdf_url,           # El original (ej. de eShip)
                        'zpl': zpl_data,             # El ZPL original (como fallback)
                        'pdf_bytes': pdf_bytes_data, # los bytes del PDF
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


# --- BUCLE PRINCIPAL (ACTUALIZADO) ---

def procesar_ordenes_coppel():
    logger.info("=== Iniciando Procesamiento de Órdenes Coppel (CON ODOO Y MIRAKL v2) ===")

    # 1. Autenticación GSheets
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

    # 2. Conexión Odoo
    models, db, uid, password = connect_to_odoo(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD)
    if not models: return
    logger.info("Conectado a Odoo.")

    # 3. Headers Mirakl
    headers_mirakl = {
        "Authorization": API_KEY_MIRAKL,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # 4. Cargar Mapa de Carriers de Mirakl
    MIRAKL_CARRIER_MAP = load_carrier_map_from_json(CARRIER_JSON)
    if not MIRAKL_CARRIER_MAP:
        logger.critical("No se pudo cargar el mapa de carriers de Mirakl. El script no puede continuar.")
        return

    # 5. Leer pedidos pendientes
    pending_map = get_pending_orders_from_sheet(gc, SPREADSHEET_ID)
    if not pending_map:
        logger.info("No hay pedidos pendientes para procesar.")
        return

    try:
        # Usa el WORKSHEET_NAME
        worksheet = gc.open_by_key(SPREADSHEET_ID).get_worksheet(0) if is_test else gc.open_by_key(
            SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
        logger.info(f"Conectado a la hoja de trabajo '{worksheet.title}' para escritura.")

    except gspread.exceptions.WorksheetNotFound:
        logger.critical(
            f"No se pudo abrir la hoja de trabajo llamada '{WORKSHEET_NAME}'. Verifica 'AUT_DATA' en el sheet SR.")
        return
    except Exception as e:
        logger.critical(f"No se pudo abrir la hoja de trabajo: {e}")
        return

    order_ids_to_fetch = list(pending_map.keys())

    # --- Procesar en orden de fila inverso ---
    # 1. Convertir el mapa a una lista de tuplas (order_id, order_info)
    pending_list = pending_map.items()

    # 2. Ordenar la lista por 'row_num' de mayor a menor (descendente)
    try:
        sorted_pending_list = sorted(pending_list, key=lambda item: item[1]['row_num'], reverse=True)
        logger.info(
            f"Se procesarán {len(sorted_pending_list)} órdenes en orden de fila inverso (de la {sorted_pending_list[0][1]['row_num']} a la {sorted_pending_list[-1][1]['row_num']}).")
    except Exception as e:
        logger.error(f"Error al ordenar la lista de pendientes: {e}. Procesando en desorden.")
        sorted_pending_list = pending_list  # Fallback a procesar en desorden

    # 6. Procesar por lotes
    BATCH_SIZE = 20

    # --- Iterar sobre la lista ordenada de IDs ---
    # Extraer solo los IDs ordenados para la lógica de batch de Mirakl
    order_ids_to_process = [item[0] for item in sorted_pending_list]

    for i in range(0, len(order_ids_to_process), BATCH_SIZE):
        batch_ids = order_ids_to_process[i:i + BATCH_SIZE]
        logger.info(f"--- Procesando lote {i // BATCH_SIZE + 1}: {len(batch_ids)} órdenes ---")

        mirakl_url = f"{MIRAKL_API_BASE_URL}/orders/?order_ids={','.join(batch_ids)}&max={BATCH_SIZE}"

        try:
            response_mirakl = requests.get(mirakl_url, headers=headers_mirakl, timeout=30)
            response_mirakl.raise_for_status()
            data_mirakl = response_mirakl.json()
        except Exception as e:
            logger.error(f"Error al obtener órdenes de Mirakl para el lote actual: {e}")
            continue

        orders_in_batch = data_mirakl.get('orders', [])
        if not orders_in_batch: continue

        # ---  Re-ordenar el lote de Mirakl para que coincida con nuestro orden inverso ---
        # La API de Mirakl no garantiza el orden de respuesta
        orders_map_mirakl = {o['order_id']: o for o in orders_in_batch}
        sorted_orders_in_batch = []
        for order_id in batch_ids:  # batch_ids SÍ está en orden inverso
            if order_id in orders_map_mirakl:
                sorted_orders_in_batch.append(orders_map_mirakl[order_id])
            else:
                logger.warning(f"La API de Mirakl no devolvió datos para la orden {order_id} en el lote.")

        for order in sorted_orders_in_batch:  # Iterar sobre la lista RE-ORDENADA
            order_id = order['order_id']
            base_order_id = order_id.split('-')[0]
            # Usar el mapa original (pending_map) para obtener la info de fila
            order_info = pending_map.get(order_id) or pending_map.get(base_order_id)

            if not order_info:
                logger.warning(f"Orden {order_id} (ni {base_order_id}) no encontrada en mapa de pendientes. Omitiendo.")
                continue

            row_num = order_info['row_num']
            logger.info(f"\n>>> Procesando Orden: {order_id} (Fila {row_num}) <<<")

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
                update_sheet_no_coverage(worksheet, row_num, reason)
                continue

            if status_raw != 'done':
                reason = f"La orden está {status.upper()} en Odoo"
                update_sheet_no_coverage(worksheet, row_num, reason)
                # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'No_bloqueados'
                fecha_orden = order_info.get('col_A_val', '')
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Status SO, Status PICK
                log_data = [fecha_orden, order_id, so_name, status, pick_status]
                log_to_sr_sheet(sh_sr, 'SO_no_bloqueadas / canceladas', log_data)
                continue

            fulfillment_code = order["fulfillment"]["center"]["code"]
            if fulfillment_code.lower() == 'coppel':
                reason = f"La orden es Fulfillment"
                update_sheet_no_coverage(worksheet, row_num, reason)
                # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'Fulfillment'
                fecha_orden = order_info.get('col_A_val', '')
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Fulfillment Code
                log_data = [fecha_orden, order_id, so_name, fulfillment_code]
                log_to_sr_sheet(sh_sr, 'Fulfillment', log_data)
                continue

            if not picking_id or pick_status_raw != 'assigned':
                reason = f"La orden tiene el PICK {pick_status}" if picking_id else "La orden NO tiene PICK"
                if reason == "La orden NO tiene PICK":
                    pick_status = "La orden NO tiene PICK"
                update_sheet_no_coverage(worksheet, row_num, reason)
                # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'PICK-pendiente'
                fecha_orden = order_info.get('col_A_val', '')
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Status SO, Status PICK
                log_data = [fecha_orden, order_id, so_name, status, pick_status]
                log_to_sr_sheet(sh_sr, 'PICK-pendiente', log_data)
                continue

            if order.get('order_state') != 'SHIPPING':
                logger.warning(f"Omitiendo orden {order_id}: Estado es '{order.get('order_state')}'")
                reason = f"La orden está {order.get('order_state')} en Mirakl"
                update_sheet_no_coverage(worksheet, row_num, reason)
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
                update_sheet_no_coverage(worksheet, row_num, reason)
                # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'Sin_cobertura'
                fecha_orden = order_info.get('col_A_val', '')
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Razon Error
                log_data = [fecha_orden, order_id, so_name, reason]
                log_to_sr_sheet(sh_sr, 'Sin_cobertura', log_data)
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
                    update_sheet_no_coverage(worksheet, row_num, reason)
                    # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'Sin_cobertura'
                    fecha_orden = order_info.get('col_A_val', '')
                    # Columnas: Fecha Orden, ID Coppel, ID Odoo, Razon Error
                    log_data = [fecha_orden, order_id, so_name, reason]
                    log_to_sr_sheet(sh_sr, 'Sin_cobertura', log_data)
                    continue
                elif best_rates_map == 'CONNECTION-ERROR':
                    reason = "Error conexión con API SRS"
                    # Error de conexion con API de Shipping Routing System, deja pasar para reintento posterior.
                    log_data = [fecha_orden, order_id, so_name, reason]
                    log_to_sr_sheet(sh_sr, 'Sin_cobertura', log_data)
                    continue
                else:
                    reason = f"Error desconocido. {type(best_rates_map)}"
                    update_sheet_no_coverage(worksheet, row_num, reason)
                    continue

            # --- PASO C: Validar Costo Total ---
            total_shipping_cost_cents = sum(r['total_price'] for r in best_rates_map.values())
            total_shipping_cost_mxn = total_shipping_cost_cents / 100.0
            cost_ratio = total_shipping_cost_mxn / total_order_value
            if cost_ratio > PERCENTAGE_COST_LIMIT:
                reason_int = f"Costo de envío excesivo: ${total_shipping_cost_mxn:.2f} ({cost_ratio:.1%})"
                reason = 'No fue posible generar guía: RSL' # Rate Superior al Limite
                logger.warning(f"Orden {order_id} RECHAZADA: {reason_int}")
                update_sheet_no_coverage(worksheet, row_num, reason)
                # Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'Costo_guia'
                fecha_orden = order_info.get('col_A_val', '')
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Costo Guia, Total orden, Ratio
                log_data = [fecha_orden, order_id, so_name, f'${total_shipping_cost_mxn:.2f}',
                            f'${total_order_value:.2f}', f'{cost_ratio:.001%}']
                log_to_sr_sheet(sh_sr, 'Costo_guia_excesivo', log_data)
                continue

            logger.info(f"Orden {order_id} APROBADA. Generando {len(best_rates_map)} guías...")

            # --- PASO D: Generar Guías ---
            labels = generate_labels(best_rates_map, recipient_data, total_order_value)
            if not labels or len(labels) != len(best_rates_map):
                reason = f"Fallo al generar guías (Se generaron {len(labels)} de {len(best_rates_map)})."
                logger.error(f"Orden {order_id}: {reason}")
                update_sheet_no_coverage(worksheet, row_num, reason)  # Marca la hoja principal

                # --- LÓGICA DE GUIAS PARCIALES ---
                fecha_orden = order_info.get('col_A_val', '')

                if not labels:
                    # --- Caso: FALLO TOTAL (0 de N) ---
                    # No hay guías, loguear solo el error de la orden
                    logger.warning(f"Orden {order_id}: No se generó NINGUNA guía.")
                    # Columnas: [Fecha, ID Coppel, ID Odoo, Razón, Guía, Carrier, Tipo Archivo, Data Base64]
                    log_data = [fecha_orden, order_id, so_name, reason, "NINGUNA", "N/A", "N/A", "N/A", "", "N/A"]
                    log_to_sr_sheet(sh_sr, 'Guias_incompletas', log_data)

                else:
                    # --- Caso: FALLO PARCIAL (X de N) ---
                    # Hay al menos una guía. Iterar y loguearlas individualmente.
                    logger.warning(f"Orden {order_id}: Guardando {len(labels)} guía(s) generada(s) en 'Guias_incompletas'...")

                    for label in labels:
                        tracking_num_str = "'" + label.get('tracking_number', 'ERROR_NO_TRACKING')
                        provider_name = label.get('provider', 'N/A')
                        source = label.get('source', 'N/A')

                        file_data_b64 = ""
                        file_type = "N/A"

                        try:
                            # 1. Prioridad: PDF ya convertido (de ZPL)
                            if label.get('pdf_bytes'):
                                file_data_b64 = base64.b64encode(label['pdf_bytes']).decode('utf-8')
                                file_type = "PDF (de ZPL)"

                            # 2. Segunda prioridad: PDF de URL (ej. eShip)
                            elif label.get('pdf_url'):
                                # Descargamos el PDF para guardarlo
                                pdf_response = requests.get(label['pdf_url'], timeout=20)
                                pdf_response.raise_for_status()
                                file_data_b64 = base64.b64encode(pdf_response.content).decode('utf-8')
                                file_type = "PDF (de URL)"

                            # 3. Fallback: ZPL (ej. FedEx, PQT)
                            elif label.get('zpl'):
                                file_data_b64 = base64.b64encode(label['zpl'].encode('utf-8')).decode('utf-8')
                                file_type = "ZPL (Fallback)"

                        except Exception as e:
                            logger.error(
                                f"No se pudo codificar/descargar el archivo para la guía {tracking_num_str}: {e}")
                            file_type = f"Error al procesar archivo: {e}"

                        # Loguear una fila POR GUÍA
                        # Columnas: [Fecha, ID Coppel, ID Odoo, Razón, Guía, Carrier, Tipo Archivo, Data Base64]
                        log_data = [fecha_orden, order_id, so_name, reason, tracking_num_str, provider_name, source, file_type,
                                    file_data_b64,  "Aun No"]
                        log_to_sr_sheet(sh_sr, 'Guias_incompletas', log_data)

                continue

            # --- PASO E: Actualizar Mirakl (ST01 y OR74) ---
            logger.info(f"Iniciando actualización de Mirakl para la orden {order_id}...")
            post_shipments_to_mirakl(headers_mirakl, order, labels, MIRAKL_CARRIER_MAP)  # Pasa el mapa
            upload_documents_to_mirakl(headers_mirakl, order_id, labels)

            # --- PASO F: Actualizar Odoo (SO y Picking) ---
            logger.info(f"Iniciando actualización de Odoo para la orden {order_id}...")

            if so_id and so_name:
                tracking_numbers_str = ",".join([l['tracking_number'] for l in labels])
                carrier_odoo_id = labels[0]['carrier_odoo_id']
                num_packages = len(labels)

                client_reference = full_name  # Referencia del cliente es NOMBRE
                update_sale_order(models, db, uid, password, so_id, tracking_numbers_str, carrier_odoo_id,
                                  num_packages, client_reference)

                if picking_id:
                    consolidate_and_attach_labels_odoo(models, db, uid, password, so_id, labels, so_name)
                    insert_log_message_sale(models, db, uid, password, so_id, so_name)
                else:
                    logger.error(f"Odoo: No se encontró PICKING para SO {so_name}. Las guías no fueron adjuntadas.")
            else:
                logger.error(f"Odoo: No se encontró SO para MKT Ref {order_id}. No se pudo actualizar Odoo.")

            # --- PASO G: Actualizar Sheet (Éxito Multi-fila) ---
            update_sheet_success_multi_row(worksheet, order_info, labels)
            # --- Logica para colocar info de la orden en sheets AUTOMATIZAION_COPPEL (SPREADSHEET_ID_SR) en hoja 'Guias_generadas' ---
            try:
                fecha_orden = order_info.get('col_A_val', '')
                # Añadir una comilla simple al inicio para forzar a Sheets a tratarlo como TEXTO
                tracking_numbers_str = "'" + ",".join([l['tracking_number'] for l in labels])
                provider_name = labels[0]['provider'] if labels else 'N/A'
                # Columnas: Fecha Orden, ID Coppel, ID Odoo, Guias, Carrier
                log_data = [fecha_orden, order_id, so_name, tracking_numbers_str, provider_name,
                            f'${total_shipping_cost_mxn:.2f}', f'${total_order_value:.2f}', f'{cost_ratio:.001%}']
                log_to_sr_sheet(sh_sr, 'Guias_generadas', log_data)
            except Exception as e_log_sr:
                logger.error(f"Sheet SR: Error al registrar éxito para {order_id}: {e_log_sr}")
            time.sleep(4)

    logger.info("=== Procesamiento finalizado ===")


if __name__ == "__main__":
    procesar_ordenes_coppel()