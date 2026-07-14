import os
import requests
import MySQLdb
import xmlrpc.client
import logging
import base64
import zipfile
import io
import json
from datetime import datetime, timedelta
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[
        logging.StreamHandler(sys.stdout)
    ])
log = logging.getLogger(__name__)

#load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# ODOO_URL = os.getenv('odoo_urlV18')
# ODOO_DB = os.getenv('odoo_dbV18')
# ODOO_USER = os.getenv('odoo_user_dataV18')
# ODOO_PWD = os.getenv('odoo_password_dataV18')

ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USER = os.getenv('ODOO_USER')
ODOO_PWD = os.getenv('ODOO_PASSWORD')


def get_db_connection():
    # Cambiamos passwd por password y db por database para pymysql
    return MySQLdb.connect(
        host=os.getenv("DB_HOST"), 
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), 
        database=os.getenv("DB_NAME"),
        local_infile=True, 
        charset='utf8mb4'
    )


def get_ml_token(db):
    cursor = db.cursor()
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = '25523702'")
    return str(cursor.fetchone()[0])


class OdooModelProxy:
    """
    Proxy para envolver las llamadas a Odoo con reintentos automáticos
    y renovación de sesión TLS en caso de 502 Bad Gateway / ProtocolError.
    """
    def __init__(self, url, db, user, pwd):
        self.url = url
        self.db = db
        self.user = user
        self.pwd = pwd
        self.common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
        self.uid = self.common.authenticate(db, user, pwd, {})
        self.models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

    def reauthenticate(self):
        log.info("Cerrando sesión TLS y abriendo una nueva conexión con Odoo...")
        self.common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        self.uid = self.common.authenticate(self.db, self.user, self.pwd, {})
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')

    def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None, max_retries=3, delay=3):
        for attempt in range(1, max_retries + 1):
            try:
                if kwargs is not None:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args, kwargs)
                else:
                    return self.models.execute_kw(self.db, self.uid, self.pwd, model, method, args)
            except xmlrpc.client.ProtocolError as e:
                log.warning(f"ProtocolError ({e.errcode} {e.errmsg}) en Odoo [{model}.{method}]. Intento {attempt}/{max_retries}...")
                if attempt == max_retries:
                    raise e
                time.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception as auth_e:
                    log.warning(f"Error al reautenticar con Odoo: {str(auth_e)}")
            except Exception as e:
                log.warning(f"Error de comunicación en Odoo [{model}.{method}]. Intento {attempt}/{max_retries}: {str(e)}")
                if attempt == max_retries:
                    raise e
                time.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception:
                    pass


def authenticate_odoo():
    proxy = OdooModelProxy(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD)
    return proxy.uid, proxy


def get_zpl_in_memory(shipping_id, access_token):
    """
    Descarga el ZIP de ML y extrae el contenido ZPL (.txt) directamente en la memoria RAM.
    """
    url = f'https://api.mercadolibre.com/shipment_labels?shipment_ids={shipping_id}&response_type=zpl2&access_token={access_token}'
    resp = requests.get(url)

    if resp.status_code != 200:
        return None, f"Error API ML: {resp.status_code} - {resp.text}"

    try:
        #Analizar si ML regresó un JSON de error en lugar de un ZIP
        resp_json = resp.json()
        if "failed_shipments" in resp_json and resp_json["failed_shipments"]:
            msg = resp_json["failed_shipments"][0].get("message", "Motivo desconocido")
            return None, msg
    except json.JSONDecodeError:
        pass  #Si falla el JSON, es porque exitosamente regresó un archivo binario (ZIP)

    try:
        #Extraer ZIP en memoria
        zip_file = zipfile.ZipFile(io.BytesIO(resp.content))
        txt_filename = next((name for name in zip_file.namelist() if name.endswith('.txt')), None)

        if not txt_filename:
            return None, "El ZIP de ML no contiene un archivo .txt"

        zpl_bytes = zip_file.read(txt_filename)
        zpl_string = zpl_bytes.decode('utf-8')
        zpl_base64 = base64.b64encode(zpl_bytes).decode('utf-8')

        return {'raw': zpl_string, 'base64': zpl_base64}, "Éxito"
    except Exception as e:
        return None, f"Error descomprimiendo ZPL: {str(e)}"


def process_fase_c():
    db = get_db_connection()
    cursor = db.cursor(MySQLdb.cursors.DictCursor)
    ml_token = get_ml_token(db)

    log.info("Conectando a Odoo...")
    uid, models = authenticate_odoo()

    #ordenes listas para procesar (se incluye zpl_data para reintentar desde BD si Odoo falló antes)
    query_pendientes = """
        SELECT id, marketplace_reference, ml_shipping_id, odoo_carrier_ref, odoo_carrier_id, zpl_data
        FROM tools.ml_api_etl_orders 
        WHERE print_status = 'READY_TO_PRINT' AND processed_successfully = 0
    """
    cursor.execute(query_pendientes)
    orders = cursor.fetchall()

    log.info(f"Se encontraron {len(orders)} órdenes pendientes en Base de Datos.")

    procesadas = 0
    errores_criticos = 0

    for order in orders:
        db_id = order['id']
        mkp_ref = order['marketplace_reference']
        shipping_id = order['ml_shipping_id']
        c_ref = order['odoo_carrier_ref']
        c_id = order['odoo_carrier_id']

        try:
            #Buscar en odoo
            odoo_search = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'search_read',
                                            [['|', ('channel_order_reference', '=', mkp_ref),
                                              ('yuju_pack_id', '=', mkp_ref)]],
                                            {'fields': ['id', 'name', 'data_carrier_selection_relational']})

            if not odoo_search:
                cursor.execute("UPDATE tools.ml_api_etl_orders SET error_message = 'No encontrada en Odoo' WHERE id = %s",
                               (db_id,))
                continue

            so_id = odoo_search[0]['id']
            so_name = odoo_search[0]['name']
            current_carrier = odoo_search[0].get('data_carrier_selection_relational')

            #Validar si existe el stock.picking (PICK) y no está cancelado
            picks = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'stock.picking', 'search_count',
                                      [[('origin', '=', so_name), ('state', '!=', 'cancel'), ('name', 'ilike', '/PICK/')]])

            if picks == 0:
                log.warning(f"SO: {so_name} no tiene PICK activo. Se posterga.")
                cursor.execute("""
                    UPDATE tools.ml_api_etl_orders 
                    SET odoo_so_name = %s, odoo_pick_status = 'NO_PICK', print_status = 'NO_PICK_IN_ODOO' 
                    WHERE id = %s
                """, (so_name, db_id))
                continue

            #Verificar si ya tiene adjuntos en Odoo
            attachments = models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order.attachment', 'search_count',
                                            [[['so_id', '=', so_id]]])

            if attachments > 0:
                log.info(f"SO: {so_name} ya tiene guía adjunta. Marcando como procesada.")
                cursor.execute("""
                    UPDATE tools.ml_api_etl_orders 
                    SET odoo_so_name = %s, odoo_pick_status = 'WITH_PICK', processed_successfully = 1, print_status = 'PRINTED_PREVIOUSLY'
                    WHERE id = %s
                """, (so_name, db_id))
                db.commit()
                continue

            # Evaluar si el ZPL ya fue descargado en un intento anterior para evitar error de ML por estatus cambiado
            if order.get('zpl_data'):
                log.info(f"SO: {so_name} utilizando ZPL recuperado desde la Base de Datos.")
                raw_zpl = order['zpl_data']
                zpl_data = {
                    'raw': raw_zpl,
                    'base64': base64.b64encode(raw_zpl.encode('utf-8')).decode('utf-8')
                }
            else:
                #Descargar ZPL en RAM desde la API de Mercado Libre
                zpl_data, msg = get_zpl_in_memory(shipping_id, ml_token)

                if not zpl_data:
                    log.error(f"Error ZPL para {so_name}: {msg}")
                    cursor.execute("UPDATE tools.ml_api_etl_orders SET error_message = %s WHERE id = %s", (msg, db_id))
                    db.commit()
                    continue

                # Respaldo inmediato en MySQL para no perder la guía en caso de Bad Gateway en Odoo más adelante
                cursor.execute("UPDATE tools.ml_api_etl_orders SET zpl_data = %s WHERE id = %s", (zpl_data['raw'], db_id))
                db.commit()

            #Subir archivo Base64 a Odoo
            attachment_data = {
                'file_name': f"{so_name}.txt",
                'so_id': so_id,
                'attachment': zpl_data['base64']
            }
            models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order.attachment', 'create', [attachment_data])

            #Actualizar tracking y carrier en sale.order
            tracking_string = f"{c_ref} / {so_name}"

            update_vals = {
                'data_tracking_readwrite': tracking_string
            }

            #Si el carrier en Odoo está vacío (False o lista vacía) y tenemos un c_id válido, lo colocamos
            if not current_carrier and c_id and c_id != 'NULL':
                update_vals['data_carrier_selection_relational'] = int(c_id)

            models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'write', [[so_id], update_vals])

            #Insertar mensaje en el Chatter
            cdmx_time = (datetime.now() - timedelta(hours=6)).strftime('%Y-%m-%d %H:%M:%S')
            models.execute_kw(ODOO_DB, uid, ODOO_PWD, 'sale.order', 'message_post', [[so_id]],
                              {'body': f'{cdmx_time}. Se insertó la guía de Mercado Libre extraída vía API / KESTRA.'})

            #Actualizar Base de Datos como Éxito (Guardando el ZPL raw y marcando como procesado)
            cursor.execute("""
                UPDATE tools.ml_api_etl_orders 
                SET odoo_so_name = %s, odoo_pick_status = 'WITH_PICK', 
                    zpl_data = %s, processed_successfully = 1, print_status = 'PRINTED', error_message = NULL
                WHERE id = %s
            """, (so_name, zpl_data['raw'], db_id))

            db.commit()
            procesadas += 1
            log.info(f"ÉXITO: Orden {so_name} ({mkp_ref}) procesada y adjuntada en Odoo.")

        except xmlrpc.client.ProtocolError as e:
            errores_criticos += 1
            error_msg = f"ProtocolError (502/Bad Gateway) tras reintentos: {str(e)}"
            log.error(f"Fallo en orden {mkp_ref} (ID DB: {db_id}): {error_msg}")
            try:
                cursor.execute("UPDATE tools.ml_api_etl_orders SET error_message = %s WHERE id = %s", (error_msg, db_id))
                db.commit()
            except Exception as db_e:
                log.error(f"Error al registrar estado de fallo en BD para orden {db_id}: {str(db_e)}")
            continue
        except Exception as e:
            errores_criticos += 1
            error_msg = f"Error procesando orden: {str(e)}"
            log.error(f"Fallo inesperado en orden {mkp_ref} (ID DB: {db_id}): {error_msg}")
            try:
                cursor.execute("UPDATE tools.ml_api_etl_orders SET error_message = %s WHERE id = %s", (error_msg, db_id))
                db.commit()
            except Exception as db_e:
                log.error(f"Error al registrar estado de fallo en BD para orden {db_id}: {str(db_e)}")
            continue

    db.close()
    log.info(f"Fase C completada. {procesadas} órdenes inyectadas en Odoo.")

    if errores_criticos > 0:
        raise Exception(f"El proceso completó las órdenes posibles, pero {errores_criticos} orden(es) fallaron de manera persistente tras N reintentos.")


if __name__ == "__main__":
    try:
        process_fase_c()
    except Exception as e:
        log.error(f"Fallo crítico en proceso de extracción de guías ML: {str(e)}")
        sys.exit(1)