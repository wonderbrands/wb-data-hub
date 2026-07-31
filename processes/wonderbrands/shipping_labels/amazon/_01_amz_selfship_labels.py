import os
import time
import requests
import json
import logging
import mysql.connector
from datetime import datetime, timedelta
import xmlrpc.client
import base64
import io
import PyPDF2
import dotenv
from typing import Optional, List
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
shared_dir = os.path.join(os.path.dirname(current_dir), '_shared')

if shared_dir not in sys.path:
    sys.path.append(shared_dir)
    
from _00_shipping_labels_db import insert_shipping_label

# --- CONFIGURACIÓN DE ENTORNO ---
dotenv.load_dotenv()

# ======================================
TEST_API_LABELS_SR = False
LIMIT_RATIO_PERCENTAGE = 0.21
# ======================================

# --- CONFIGURACIÓN PRINCIPAL ---
IS_TEST = 'qa_' if TEST_API_LABELS_SR else ''
API_AUTH = (os.getenv('AUTH_USER'), os.getenv('AUTH_PASS'))
API_URL_LIVE_RATES = f"https://wonder-site.duckdns.org/{IS_TEST}live-rates"
API_URL_GENERATE_LABEL = f"https://wonder-site.duckdns.org/{IS_TEST}generate-label"

# --- CONFIGURACIÓN DE ODOO ---
ODOO_URL = os.getenv('odoo_urlV18')
ODOO_DB = os.getenv('odoo_dbV18')
ODOO_USER = os.getenv('odoo_user_dataV18')
ODOO_PASSWORD = os.getenv('odoo_password_dataV18')

SHIPPER_DATA = {
    "name": "Equipo Somos Reyes", "company": "SOMOS REYES", "email": "info@somos-reyes.com",
    "phone": "5568309828", "street1": "BENITO JUAREZ 11/B6", "street2": "SAN PEDRO BARRIENTOS",
    "city": "Tlalnepantla de Baz", "state": "MEX", "country": "MX", "zip": "54010"
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


# ==========================================
# FUNCIONES DB
# ==========================================
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
    )


def fetch_pending_orders(conn):
    """Extrae las filas a nivel SKU que no tienen guía generada."""
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT id, amazonorderid, sku, quantity_ordered, status, latestshipdate
        FROM amz_label_queue
        WHERE label_generated = 0 AND status = 'pending'
        AND cancellation_reason IS NULL
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def get_order_details_from_crawler(conn, amazonorderid):
    """Obtiene client_data, paid_by_buyer y shipping_total de la tabla crawl.amz_unshipped_checks"""
    cursor = conn.cursor(dictionary=True)
    query = """
        SELECT client_data, paid_by_buyer, shipping_total 
        FROM crawl.amz_unshipped_checks 
        WHERE amazonorderid = %s 
        ORDER BY inserted_at DESC LIMIT 1
    """
    cursor.execute(query, (amazonorderid,))
    row = cursor.fetchone()
    cursor.close()

    if row:
        try:
            client_data = json.loads(row['client_data'])
        except Exception as e:
            logger.error(f"Error parseando client_data JSON para {amazonorderid}: {e}")
            client_data = {}

        paid_by_buyer = float(row['paid_by_buyer']) if row['paid_by_buyer'] else 0.0
        shipping_total = float(row['shipping_total']) if row['shipping_total'] else 0.0
        return client_data, paid_by_buyer, shipping_total

    return None, 0.0, 0.0


def update_db_label_data(conn, row_id, tracking_json_str, carrier, service_level, status='LABELS_GENERATED'):
    """Actualiza el renglón del SKU con el JSON de sus guías o con error por costo excesivo."""
    cursor = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Se marca como 'label_generated' = 1 incluso si es rechazada, para que no vuelva a procesarse en el loop.
    query = """
        UPDATE amz_label_queue
        SET label_generated = 1,
            label_generated_at = %s,
            tracking_number = %s,
            carrier = %s,
            carrier_service_level = %s,
            status = %s
        WHERE id = %s
    """
    cursor.execute(query, (now, tracking_json_str, carrier, service_level, status, row_id))
    conn.commit()
    cursor.close()
    logger.info(f"BD Actualizada para row_id {row_id} con status: {status}.")


def update_crawler_checks(conn, order_id, checks_status, checks_detail):
    """Actualiza el estatus del crawler para una orden específica."""
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crawl.amz_unshipped_checks
            SET crawler_checks = %s, 
                crawler_checks_detail = %s 
            WHERE amazonorderid = %s
        """, (checks_status, checks_detail, order_id))
        conn.commit()
        cursor.close()
    except Exception as e:
        logger.error(f"Error actualizando la tabla crawler para la orden {order_id}: {e}")


# ==========================================
# FUNCIONES DE ODOO
# ==========================================
def connect_to_odoo():
    logger.info("Conectando a Odoo...")
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')
        logger.info("Conexión a Odoo exitosa.")
        return models, uid
    except Exception as e:
        logger.error(f"Error al conectar a Odoo: {e}")
        return None, None


def get_order_ids_from_odoo(models, uid, amazonorderid):
    """AHORA SOLO BUSCA LOS IDs EN ODOO (Ya no la dirección)."""
    search_domain = [['channel_order_id', '=', amazonorderid]]
    so_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'search_read',
                                [search_domain],
                                {'fields': ['id', 'name'], 'limit': 1})
    if not so_data:
        return None, None
    return so_data[0]['id'], so_data[0]['name']


def get_carrier_odoo_id(provider_name: str) -> Optional[int]:
    p = provider_name.lower()
    if 'fedex' in p: return 1
    if 'estafeta' in p: return 2
    if 'dhl' in p: return 3
    if 'paqueteexpress' in p: return 4
    return None


def insert_log_message_sale(models, uid, so_id, so_name: str):
    """Inserta un mensaje en el chatter de la orden en Odoo."""
    current_utc_time = datetime.now()
    cdmx_time = current_utc_time - timedelta(hours=6)
    current_datetime = cdmx_time.strftime('%Y-%m-%d %H:%M:%S')

    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'message_post',
            [[so_id]],
            {
                'body': f'{current_datetime}. Se insertó la(s) guía(s) de Amazon Self-Ship para la orden {so_name} mediante automatización.'
            }
        )
        logger.info(f"Odoo: Mensaje en el chatter insertado para {so_name}")
    except Exception as e:
        logger.error(f"Odoo: Error al insertar mensaje en chatter para {so_name}: {e}")


def add_shipping_line_to_odoo(models, uid, so_id, so_name, shipping_total):
    """Agrega la línea de costo de envío (C-ENVIO) a Odoo, inyectando los impuestos correspondientes."""
    try:
        product_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[['default_code', '=', 'C-ENVIO']]],
            {'fields': ['id', 'taxes_id'], 'limit': 1}
        )

        if not product_data:
            logger.error(f"Odoo: No se encontró el producto 'C-ENVIO' para la orden {so_name}.")
            return

        product_id = product_data[0]['id']
        #taxes del producto
        taxes_ids = product_data[0].get('taxes_id', [])

        price_unit_no_tax = shipping_total / 1.16  # Quitar IVA

        #nuevos valores
        line_vals = {
            'order_id': so_id,
            'product_id': product_id,
            'price_unit': price_unit_no_tax,
            'product_uom_qty': 1.0
        }

        #forza impuestos
        iva16_id = 14
        line_vals['tax_id'] = [(6, 0, taxes_ids or [iva16_id])]

        #linea de c-envio
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order.line', 'create',
            [line_vals]
        )

        #mensaje chetter
        current_utc_time = datetime.now()
        cdmx_time = current_utc_time - timedelta(hours=6)
        current_datetime = cdmx_time.strftime('%Y-%m-%d %H:%M:%S')

        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'message_post',
            [[so_id]],
            {
                'body': f'{current_datetime}. Se agregó línea de costo de envío (C-ENVIO) cobrado por Amazon por ${shipping_total:.2f} (IVA incluido) mediante automatización.'
            }
        )
        logger.info(f"Odoo: Línea C-ENVIO inyectada con éxito en {so_name} (con impuestos aplicados).")
    except Exception as e:
        logger.error(f"Odoo: Error al agregar línea de envío a {so_name}: {e}")

def process_odoo_integration(models, uid, so_id, so_name, all_labels, latest_ship_formatted):
    """Inyecta los trackings (con fecha) y adjunta el PDF consolidado a Odoo."""
    logger.info(f"Iniciando integración con Odoo para {so_name}")

    # 1. Armar el tracking y actualizar la orden
    tracking_numbers_str = ",".join([l['tracking_number'] for l in all_labels])
    if latest_ship_formatted:
        tracking_numbers_str = f"{tracking_numbers_str}##{latest_ship_formatted}"

    carrier_odoo_id = get_carrier_odoo_id(all_labels[0]['provider'])

    update_values = {'data_tracking_readwrite': tracking_numbers_str}
    if carrier_odoo_id: update_values['data_carrier_selection_relational'] = carrier_odoo_id

    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'write', [[so_id], update_values])
        logger.info(f"Trackings escritos en Odoo para {so_name}")
    except Exception as e:
        logger.error(f"Error escribiendo trackings en Odoo: {e}")

    # 2. Consolidar PDFs/ZPLs
    logger.info(f"Consolidando PDFs/ZPL para {so_name}")
    pdf_merger = PyPDF2.PdfMerger()
    pdfs_added = False
    zpl_fallbacks = []

    for label in all_labels:
        if label.get('pdf_bytes'):
            pdf_merger.append(io.BytesIO(label['pdf_bytes']))
            pdfs_added = True
        elif label.get('pdf_url'):
            try:
                pdf_res = requests.get(label['pdf_url'], timeout=20)
                pdf_res.raise_for_status()
                pdf_merger.append(io.BytesIO(pdf_res.content))
                pdfs_added = True
            except Exception as e:
                logger.error(f"Error descargando PDF {label['tracking_number']}: {e}")
        elif label.get('zpl'):
            zpl_fallbacks.append(label['zpl'])

    file_data_b64 = None
    file_name = None

    if pdfs_added:
        try:
            merged_pdf_io = io.BytesIO()
            pdf_merger.write(merged_pdf_io)
            pdf_merger.close()
            file_data_b64 = base64.b64encode(merged_pdf_io.getvalue()).decode('utf-8')
            file_name = f"{so_name}.pdf"
        except Exception as e:
            logger.error(f"Error uniendo los PDFs: {e}")
    elif zpl_fallbacks:
        consolidated_zpl = "\n\n".join(zpl_fallbacks)
        file_data_b64 = base64.b64encode(consolidated_zpl.encode('utf-8')).decode('utf-8')
        file_name = f"{so_name}.zpl.txt"

    if file_data_b64 and file_name:
        try:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order.attachment', 'create', [{
                'file_name': file_name,
                'attachment': file_data_b64,
                'so_id': so_id,
            }])
            logger.info(f"Odoo: Archivo consolidado ({file_name}) guardado en la orden {so_name}")
        except Exception as e:
            logger.error(f"Error subiendo el adjunto consolidado a Odoo: {e}")
    else:
        logger.warning(f"No se procesaron archivos válidos para adjuntar en {so_name}")

    # 3. Insertar mensaje en el Chatter
    insert_log_message_sale(models, uid, so_id, so_name)


# ==========================================
# FUNCIONES DE LA API DE GUÍAS
# ==========================================
def convert_zpl_to_pdf_bytes(zpl_string: str) -> Optional[bytes]:
    """Convierte un string ZPL a bytes de PDF usando Labelary."""
    url = "http://api.labelary.com/v1/printers/8dpmm/labels/4x6/"
    headers = {"Accept": "application/pdf"}
    try:
        response = requests.post(url, headers=headers, data=zpl_string, timeout=15)
        response.raise_for_status()
        return response.content if response.content else None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error convirtiendo ZPL a PDF: {e}")
        return None


def get_best_rates_per_box(payload: dict) -> Optional[dict]:
    logger.info("Solicitando cotización a API...")
    try:
        response = requests.post(API_URL_LIVE_RATES, json=payload, auth=API_AUTH, timeout=30)
        response.raise_for_status()
        all_rates = response.json()

        if not all_rates:
            logger.warning("API devolvió lista vacía de tarifas.")
            return None

        # -------------------------------
        # Verificamos si la respuesta no es una lista o si el primer elemento no es un diccionario
        if not isinstance(all_rates, list) or (len(all_rates) > 0 and not isinstance(all_rates[0], dict)):
            logger.error(f"Formato inesperado recibido de la API de tarifas. Respuesta: {all_rates}")
            return None
        # -------------------------------

        rates_by_box = {}
        for rate in all_rates:
            if not isinstance(rate, dict):
                continue

            box_id = rate.get('package_id')
            if not box_id: continue
            if box_id not in rates_by_box: rates_by_box[box_id] = []
            rates_by_box[box_id].append(rate)

        box_keys = list(rates_by_box.keys())
        first_box_rates = rates_by_box[box_keys[0]]
        all_available_services = {r['service_code']: r['service_name'] for r in first_box_rates}

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
                    possible = False;
                    break
            if possible:
                best_rates_per_service[service_code] = {
                    'total_cost_cents': total_cost, 'rates_map': rates_for_this_service, 'service_name': service_name
                }

        if not best_rates_per_service:
            logger.warning("No se pudo encontrar un servicio común para todas las cajas.")
            return None

        best_service_code = min(best_rates_per_service, key=lambda k: best_rates_per_service[k]['total_cost_cents'])
        best_option = best_rates_per_service[best_service_code]
        logger.info(
            f"Mejor tarifa seleccionada: {best_option['service_name']} - Costo total: ${best_option['total_cost_cents'] / 100:.2f}")
        return best_option['rates_map']
    except Exception as e:
        logger.error(f"Error en cotización: {e}")
        return None


def generate_labels(best_rates_map: dict, recipient_data: dict, total_order_value: float) -> list:
    logger.info("Iniciando generación de etiquetas...")
    generated_labels = []
    val_per_box = total_order_value / len(best_rates_map) if len(best_rates_map) > 0 else 0

    for box_id, rate in best_rates_map.items():
        sku_child = rate.get('sku_child')
        sku_parent = rate.get('sku_parent')
        logger.info(f"Generando etiqueta para box {box_id}, sku_child {sku_child}...")

        payload = {
            "service_code": rate['service_code'], "rate_id": rate['rate_id'],
            "shipper": SHIPPER_DATA, "recipient": recipient_data, "sku": sku_child,
            "data_sat": {"bienesTransp": "50161815", "valorMercancia": val_per_box}
        }

        try:
            response = requests.post(API_URL_GENERATE_LABEL, json=payload, auth=API_AUTH, timeout=45)
            if response.status_code == 200:
                label_data = response.json()
                if 'tracking_number' in label_data:
                    pdf_url = label_data.get('pdf_url')
                    zpl_data = label_data.get('zpl')
                    pdf_bytes_data = None
                    tracking_num = str(label_data['tracking_number'])

                    if zpl_data and not pdf_url:
                        logger.info(f"Guía {tracking_num} es ZPL. Convirtiendo a PDF...")
                        pdf_bytes_data = convert_zpl_to_pdf_bytes(zpl_data)

                    generated_labels.append({
                        'sku_parent': sku_parent,
                        'sku_child': sku_child,
                        'package_id': box_id,
                        'tracking_number': tracking_num,
                        'provider': rate.get('service_name', 'Desconocido').split(' - ')[0],
                        'service_level': rate.get('service_name', 'Estándar'),
                        'pdf_url': pdf_url,
                        'zpl': zpl_data,
                        'pdf_bytes': pdf_bytes_data,
                        'shipping_label_cost': rate.get('total_price', 0) / 100.0
                    })
                    logger.info(f"Etiqueta generada exitosamente. Tracking: {tracking_num}")
                else:
                    logger.error(f"La respuesta 200 no contenía 'tracking_number'. Data: {label_data}")
            else:
                logger.error(f"SRS API: Fallo al generar etiqueta (Status {response.status_code}): {response.text}")
        except Exception as e:
            logger.error(f"Excepción al generar etiqueta para {box_id}: {e}")

    return generated_labels


# ==========================================
# MAIN
# ==========================================
def process_amazon_self_ship_labels():
    logger.info("=== Iniciando Script: Generación de Guías Amazon SelfShip===")
    conn = get_db_connection()
    models, uid = connect_to_odoo()

    rows = fetch_pending_orders(conn)
    if not rows:
        logger.info("No hay órdenes pendientes.")
        if conn: conn.close()
        return

    # Agrupar filas por Order ID
    orders_grouped = {}
    for row in rows:
        order_id = row['amazonorderid']
        if order_id not in orders_grouped: orders_grouped[order_id] = []
        orders_grouped[order_id].append(row)

    for order_id, order_rows in orders_grouped.items():

        logger.info(f"\n>>> Procesando Orden: {order_id}<<<")

        # ids Odoo
        so_id, so_name = get_order_ids_from_odoo(models, uid, order_id)
        if not so_id:
            logger.warning(f"Orden {order_id} no encontrada en Odoo. Aún así se intentará generar guía.")

        # direccion y valores de DB crawler
        client_data, paid_by_buyer, shipping_total = get_order_details_from_crawler(conn, order_id)
        if not client_data:
            logger.error(f"No se encontró 'client_data' en crawler para la orden {order_id}. Saltando.")
            continue

        # --- Limpieza de Datos para API de PaqueteExpress y FedEx ---
        raw_phone = str(client_data.get('phone', '0000000000'))
        clean_phone = ''.join(filter(str.isdigit, raw_phone))
        final_phone = clean_phone[-10:] if len(clean_phone) >= 10 else clean_phone.zfill(10)

        recipient_data = {
            "name": client_data.get('name', 'Cliente Amazon'),
            "company": "",
            "email": "sin-correo@amazon.com",
            "phone": final_phone,
            "street1": client_data.get('street', '.'),
            "street2": client_data.get('neighborhood', ''),
            "city": str(client_data.get('city', '')).replace(',', '').strip(),
            "state": client_data.get('state', ''),
            "country": "MX",
            "zip": client_data.get('zip_code', '')
        }

        # Extraer fecha limite de envío
        raw_date = order_rows[0].get('latestshipdate')
        latest_ship_formatted = ""
        if isinstance(raw_date, datetime):
            latest_ship_formatted = raw_date.strftime('%d-%m')
        elif isinstance(raw_date, str) and raw_date:
            try:
                dt_obj = datetime.strptime(raw_date.split(' ')[0], '%Y-%m-%d')
                latest_ship_formatted = dt_obj.strftime('%d-%m')
            except:
                pass

        logger.info(f"Preparando payload para cotizar {len(order_rows)} sku(s)...")

        total_items_qty = sum(int(row['quantity_ordered']) for row in order_rows)
        # El unit price usa paid_by_buyer ahora
        avg_unit_price = paid_by_buyer / total_items_qty if total_items_qty > 0 else 0

        items_to_quote = []
        for row in order_rows:
            items_to_quote.append({
                "sku": row['sku'],
                "quantity": int(row['quantity_ordered']),
                "price": avg_unit_price,
                "name": f"Producto Amazon {row['sku']}"
            })

        payload_rates = {"origin": {"zip": SHIPPER_DATA["zip"], "country": "MX"}, "destination": recipient_data,
                         "items": items_to_quote}

        # Cotizar
        best_rates_map = get_best_rates_per_box(payload_rates)
        if not best_rates_map:
            logger.error("No se obtuvieron tarifas. Saltando a la siguiente orden.")
            continue

        # --- VALIDACIÓN DEL 21% DEL COSTO TOTAL (REGLA DE NEGOCIO) ---
        total_shipping_cost_cents = sum(r['total_price'] for r in best_rates_map.values())
        total_shipping_cost_mxn = total_shipping_cost_cents / 100.0

        # El ratio se calcula contra paid_by_buyer
        cost_ratio = total_shipping_cost_mxn / paid_by_buyer if paid_by_buyer > 0 else 0

        if cost_ratio > LIMIT_RATIO_PERCENTAGE:
            logger.warning(
                f"RECHAZADA: Costo de envío excesivo (${total_shipping_cost_mxn:.2f}) representa el {cost_ratio:.1%} del valor (${paid_by_buyer:.2f}) de la orden {order_id}.")
            for row in order_rows:
                update_db_label_data(conn, row['id'], None, None, None, "LIMIT_RATIO_OVERCOME")
                update_crawler_checks(conn, order_id, 0, "Costo de guia supera el 21%")

                # --- NUEVO: registro adicional en tools.shipping_labels (no sustituye el insert/update anterior) ---
                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Amazon',
                    sku=row['sku'],
                    qty_ordered=row['quantity_ordered'],
                    status='LIMIT_RATIO_OVERCOME',
                    label_generated=False,
                    tracking_number=None,
                    shipping_cost=total_shipping_cost_mxn,
                    carrier=None,
                    carrier_service_level=None,
                    error_log=(
                        f"Costo de envío (${total_shipping_cost_mxn:.2f}) representa el "
                        f"{cost_ratio:.1%} del valor (${paid_by_buyer:.2f}) de la orden, "
                        f"superando el límite permitido de {LIMIT_RATIO_PERCENTAGE:.0%}."
                    )
                )
            continue

        logger.info(f"Costo Aprobado: Envío representa el {cost_ratio:.1%} de la orden.")

        # Generar Guías (usando paid_by_buyer para el valorSAT)
        all_labels = generate_labels(best_rates_map, recipient_data, paid_by_buyer)
        if not all_labels:
            logger.error("No se generaron etiquetas. Se manda manual.")
            for row in order_rows:
                update_db_label_data(conn, row['id'], None, None, None, "SKU_NOT_SUPPORT")
                update_crawler_checks(conn, order_id, 0, "SKU no soportado por generador de guías")

                # --- NUEVO: registro adicional en tools.shipping_labels (no sustituye el insert/update anterior) ---
                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Amazon',
                    sku=row['sku'],
                    qty_ordered=row['quantity_ordered'],
                    status='SKU_NOT_SUPPORT',
                    label_generated=False,
                    tracking_number=None,
                    shipping_cost=total_shipping_cost_mxn,
                    carrier=None,
                    carrier_service_level=None,
                    error_log="No se pudo generar ninguna etiqueta para la orden; SKU no soportado por el generador de guías."
                )
            continue

        # Inyectar en Odoo
        if so_id:
            process_odoo_integration(models, uid, so_id, so_name, all_labels, latest_ship_formatted)

            # --- Inyectar línea de costo de envío si aplica ---
            if shipping_total > 0:
                add_shipping_line_to_odoo(models, uid, so_id, so_name, shipping_total)

        # Mapear de vuelta a la Base de Datos
        logger.info("Mapeando etiquetas generadas a la base de datos...")
        for row in order_rows:
            row_sku = row['sku']
            labels_for_this_sku = [label for label in all_labels if label['sku_parent'] == row_sku]

            if labels_for_this_sku:
                #Se añade 'shipping_label_cost' al JSON
                tracking_json_str = json.dumps([{
                    "tracking_number": l['tracking_number'],
                    "carrier": 'PAQUETEXPRESS' if l['provider'] == 'PAQUETEEXPRESS' else l['provider'],
                    "package_id": l['package_id'],
                    "sku_child": l['sku_child'],
                    "shipping_label_cost": l['shipping_label_cost']
                } for l in labels_for_this_sku])

                main_carrier = labels_for_this_sku[0]['provider']
                main_carrier = 'PAQUETEXPRESS' if main_carrier == 'PAQUETEEXPRESS' else main_carrier
                main_service = labels_for_this_sku[0]['service_level']

                update_db_label_data(conn, row['id'], tracking_json_str, main_carrier, main_service)
                logger.info(f"SKU {row_sku} actualizado con {len(labels_for_this_sku)} guía(s) en BD.")

                # --- NUEVO: registro adicional en tools.shipping_labels (no sustituye el update anterior) ---
                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Amazon',
                    sku=row_sku,
                    qty_ordered=row['quantity_ordered'],
                    status='LABELS_GENERATED',
                    label_generated=True,
                    tracking_number=tracking_json_str,  # ya viene serializado a JSON arriba
                    shipping_cost=total_shipping_cost_mxn,  # costo TOTAL de envío de la orden
                    carrier=main_carrier,
                    carrier_service_level=main_service,
                    error_log=None
                )
            else:
                logger.warning(f"No se encontraron etiquetas para el SKU parent {row_sku} en esta orden.")

                # --- NUEVO: registro adicional en tools.shipping_labels para dejar rastro del fallo ---
                insert_shipping_label(
                    conn,
                    marketplace_id=order_id,
                    marketplace='Amazon',
                    sku=row_sku,
                    qty_ordered=row['quantity_ordered'],
                    status='NO_LABEL_FOR_SKU',
                    label_generated=False,
                    tracking_number=None,
                    shipping_cost=total_shipping_cost_mxn,
                    carrier=None,
                    carrier_service_level=None,
                    error_log=f"Se generaron guías para la orden {order_id}, pero ninguna correspondió a este SKU parent."
                )

    if conn:
        conn.close()
    logger.info("=== Fin del Script ===")


if __name__ == "__main__":
    process_amazon_self_ship_labels()