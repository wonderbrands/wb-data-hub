import xmlrpc.client
import mysql.connector
import os
import logging
import time
import base64
import xml.etree.ElementTree as ET
import concurrent.futures
from dotenv import load_dotenv
import threading

# Almacenamiento local por hilo para manejar sesiones TLS independientes si MAX_WORKERS > 1
thread_local_proxy = threading.local()

# ── Configuración de Logs ──────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# LOCAL
load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# ----------------------------------------------------------------------------
TAX_ID_MARKETPLACES     = 38 
BATCH_LIMIT             = 50   # Facturas por lote
MAX_WORKERS             = 1    # Hilos concurrentes
SLEEP_BETWEEN_BATCHES   = 10   # Segundos de respiro tras cada lote
MAX_EMPTY_RUNS          = 5    # Intentos vacíos antes de terminar
SLEEP_SECONDS           = 10   # Espera entre intentos vacíos
# ----------------------------------------------------------------------------

# ----------------Reintentos para registros en estado ERROR -----------------------------
# Solo se vuelven a tomar si NO han
# fallado demasiadas veces Y si el registro no es demasiado viejo. Evita que una
# orden con un error permanente (p.ej. "SO no encontrada en Odoo", que jamás va
# a resolverse sola) se reintente para siempre. Confirmado que la tabla
# finance.mkp_billing_prod tiene las columnas retry_count_loader y created_at.
MAX_ERROR_RETRIES        = 2    # "cierto número de veces"
ERROR_RETRY_WINDOW_DAYS  = 60   # "lapso de tiempo de creación"
# -----------------------------------------------------------------------------


def get_account_id(models, db, uid, pwd, code):
    acc = models.execute_kw(db, uid, pwd, 'account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"¡ALERTA! No se encontró la cuenta contable con código {code} en Odoo.")
    return acc[0]


# -------------------------------------------------------------------------------
def reset_stuck_processing_records(marketplace='MERCADO_LIBRE'):
    """
    Si una corrida anterior murió a la mitad de un lote (Bad Gateway, kill -9,
    caída del servidor, la excepción que se dispara en process_batch_concurrently
    ANTES de llegar a los hilos, etc.), los registros que ya se marcaron como
    'PROCESSING' se quedan así para siempre, porque nadie los regresa a 'PENDING'.
    Esto se ejecuta UNA sola vez, al arrancar el script, antes de entrar al while
    loop principal — no dentro de cada iteración (dentro de una misma corrida no
    hace falta: si process_batch_concurrently lanza una excepción no controlada,
    se cae al except de __main__ y el script termina, así que no hay una "próxima
    iteración" en la que reaparezcan registros trabados de ESTA misma corrida).
    """
    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor()
        cursor.execute("""
            UPDATE finance.mkp_billing_prod 
            SET status = 'PENDING' 
            WHERE status = 'PROCESSING' AND marketplace = %s
        """, (marketplace,))
        affected = cursor.rowcount
        db.commit()
        cursor.close()
        db.close()
        if affected:
            log.warning(f"Limpieza inicial: {affected} registro(s) trabados en 'PROCESSING' regresados a 'PENDING'.")
    except Exception as e:
        log.error(f"No se pudo ejecutar la limpieza de registros 'PROCESSING': {e}")
# -------------------------------------------------------------------------------


def process_single_invoice(record, context):
    thread_name = threading.current_thread().name
    
    odoo_db = context['odoo_db']
    uid = context['uid']
    odoo_pwd = context['odoo_pwd']
    acc_cxc_ml = context['acc_cxc_ml']
    odoo_url = context.get('odoo_url')

    # Antes se creaba un xmlrpc.client.ServerProxy NUEVO en
    # cada llamada a esta función (es decir, una vez por CADA factura procesada).
    # Cada ServerProxy nuevo implica una conexión HTTP/TLS nueva contra Odoo. Ahora
    # se reutiliza el MISMO ServerProxy que ya se creó una vez en
    # process_batch_concurrently (Python cachea la conexión HTTP subyacente entre
    # llamadas de un mismo ServerProxy), lo que reduce handshakes TLS repetidos
    # contra el proxy/nginx de Odoo.sh.
    
    # Advertencia si algún día subes MAX_WORKERS por encima de 1: compartir un
    # mismo ServerProxy/socket entre hilos que llaman a la vez SÍ puede corromper
    # el tráfico HTTP (no es thread-safe para uso concurrente real). Con
    # MAX_WORKERS=1 (uso secuencial) esto es seguro. Si subes los workers, cada
    # hilo necesitaría su propio ServerProxy.
    
    # MODIFICACIÓN PARA MAX_WORKERS > 1 (Manejo de múltiples sesiones TLS):
    # Se utiliza threading.local() para garantizar que cada hilo trabajador del pool
    # tenga y reutilice su propia instancia independiente de ServerProxy (y por ende su
    # propia sesión/socket TLS), evitando colisiones y corrupción de tráfico HTTP.
    if odoo_url:
        if not hasattr(thread_local_proxy, 'models'):
            thread_local_proxy.models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')
        models = thread_local_proxy.models
    else:
        models = context['models']

    # Datos cacheados del lote (batch fetching)
    so_data = context['so_map'].get(record['mkp_order_id'])
    lines_map = context['lines_map']
    existing_invoices = context['invoice_map']
    partner_map = context['partner_map']                          
    xml_parsed = context['xml_parsed_map'].get(record['id'], {})
    payment_methods_cache = context['payment_methods_cache']
    
    mkp_order = record['mkp_order_id']
    uuid = record['cfdi_uuid']
    xml_data = record['xml_data']
    record_id = record['id']

    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error de BD en Hilo (Orden {mkp_order}): {e}")
        return

    try:
        if not so_data:
            raise Exception(f"Orden de venta con referencia {mkp_order} no encontrada en Odoo.")

        inv_id = None
        resuming_draft = False

        # Validación de bucle cacheada
        if so_data['name'] in existing_invoices:
            existing_inv = existing_invoices[so_data['name']]
            inv_name = existing_inv.get('name')
            inv_state = existing_inv.get('state')

            # ---------------------------------------------------
            # Antes: CUALQUIER account.move encontrado con ese invoice_origin (aunque
            # fuera un borrador que nunca se posteó porque el script se cortó a la
            # mitad) se marcaba como ALREADY_ODOO_INVOICED y se abandonaba tal cual,
            # dejando en Odoo una factura huérfana: sin CxC redirigida, sin XML
            # adjunto, sin EDI y sin postear.
            # Odoo solo asigna el folio/"name" real al hacer action_post; mientras
            # sigue en borrador, ese name viaja como False (booleano) por XML-RPC —
            # exactamente el "YA TIENE la factura False".
            # Si detectamos ese caso, NO se crea una factura nueva: se reutiliza el
            # ID existente y se retoma el flujo justo en "Buscar líneas para CxC".
            invoice_is_orphan_draft = (inv_name is False or inv_name == 'False' or inv_state == 'draft')

            if not invoice_is_orphan_draft:
                log.warning(f"Bucle evitado: La orden {so_data['name']} ({mkp_order}) YA TIENE la factura {inv_name}.")
                # CAMBIO adicional: ahora también se guarda odoo_so_name aquí. Antes
                # esta rama NO lo guardaba, y eso deja el registro en mkp_billing_prod
                # sin so_name -> el script 04 luego busca la factura con so_name=NULL
                # y falla con "Factura publicada para None no encontrada" (justo el
                # patrón de error que confirmaste que ya habías notado).
                cursor.execute(
                    "UPDATE finance.mkp_billing_prod SET status = 'ALREADY_ODOO_INVOICED', odoo_so_name = %s, processed_at = NOW() WHERE id = %s",
                    (so_data['name'], record_id)
                )
                db.commit()
                return
            else:
                resuming_draft = True
                inv_id = existing_inv['id']
                log.warning(f"[{thread_name}] Factura huérfana (borrador sin postear) para {so_data['name']} ({mkp_order}) -> move ID {inv_id}. Reanudando sin recrear.")
            # -----------------------------------------------------------------------

        if not resuming_draft:
            # --- XML BASE64 ---
            # El XML ya viene decodificado/parseado desde el
            # batch (xml_parsed_map).
            uso_cfdi = xml_parsed.get('uso_cfdi', 'S01')
            forma_pago_code = xml_parsed.get('forma_pago_code', '03')
            metodo_pago = xml_parsed.get('metodo_pago', 'PUE')
            fecha_timbrado_str = xml_parsed.get('fecha_timbrado_str', False)
            rfc_receptor = xml_parsed.get('rfc_receptor', False)

            payment_method_id = payment_methods_cache.get(forma_pago_code, 3)
            invoice_date = fecha_timbrado_str.split('T')[0] if fecha_timbrado_str else False

            # --- LÓGICA DE PARTNER ---
            # CAMBIO (punto 1): antes esto hacía SIEMPRE una llamada
            # res.partner.search_read por factura (y a veces una segunda de
            # 'create'). Ahora se resuelve contra partner_map, armado UNA sola vez
            # para TODO el lote en process_batch_concurrently (búsqueda con 'vat'
            # 'in' [...] + creación de los faltantes, también agrupada). Esto reduce
            # hasta 2 llamadas a Odoo por factura a, en el peor caso, 2 llamadas
            # para el LOTE COMPLETO.
            partner_invoice_id = so_data['partner_id'][0]
            if rfc_receptor and rfc_receptor != 'XAXX010101000':
                partner_invoice_id = partner_map.get(rfc_receptor, partner_invoice_id)

            # Armar líneas desde caché
            invoice_lines = []
            total_odoo = 0.0
            total_xml = float(xml_parsed.get('total_xml', 0.0))

            for line_id in so_data['order_line']:
                line = lines_map.get(line_id)
                if not line: continue
                
                # Excluir líneas de envío (por SKU/nombre o por producto específico si lo tienes cacheado)
                # Si en display_type es nota o sección, o si es el producto de envío, lo saltamos:
                nombre_linea = str(line.get('name', '')).upper()
                if 'ENVIO' in nombre_linea or 'C-ENVIO' in nombre_linea:
                    log.info(f"[{thread_name}] Saltando línea de envío en SO {so_data['name']}: {nombre_linea}")
                    continue

                inv_line = {
                    'display_type': line.get('display_type') or 'product',
                    'product_id': line['product_id'][0] if line.get('product_id') else False,
                    'quantity': line['product_uom_qty'], 
                    'price_unit': line['price_unit'],
                    'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])], 
                    'sale_line_ids': [(4, line['id'])]
                }
                invoice_lines.append((0, 0, inv_line))
                
                # Calcular total estimado (Costo * Qty * 1.16 de IVA marketplace)
                total_odoo += (line['product_uom_qty'] * line['price_unit']) * 1.16

            # Validación de tolerancia +- $1.00 peso contra el XML
            diff = abs(total_odoo - total_xml)
            if total_xml > 0 and diff > 1.0:
                mensaje_log = (
                    f"Total no coincide: XML ML (${total_xml:,.2f}) vs "
                    f"Odoo sin envío (${total_odoo:,.2f}). "
                    f"Diferencia: ${total_odoo - total_xml:,.2f}"
                )
                log.warning(f"[{thread_name}] ⚠️ {mensaje_log} en orden {mkp_order}. Abortando inyección.")
                
                # Actualizamos al estado TOTAL_DIFF sin tocar retry_count_loader
                cursor.execute("""
                    UPDATE finance.mkp_billing_prod 
                    SET status = 'TOTAL_DIFF', error_log = %s, processed_at = NOW() 
                    WHERE id = %s
                """, (mensaje_log, record_id))
                db.commit()
                return  # Detenemos el flujo aquí para no crear ni postear la factura en Odoo
            # ------------------------------------------------------------------
            # Crear Factura
            invoice_vals = {
                'move_type': 'out_invoice',
                'invoice_origin': so_data['name'],
                'partner_id': partner_invoice_id, 
                'team_id': so_data['team_id'][0],
                'invoice_line_ids': invoice_lines,
                'l10n_mx_edi_usage': uso_cfdi, 
                'l10n_mx_edi_payment_method_id': payment_method_id, 
                'l10n_mx_edi_payment_policy': metodo_pago, 
            }
            
            if invoice_date:
                invoice_vals['invoice_date'] = invoice_date
                invoice_vals['date'] = invoice_date
            if metodo_pago == 'PUE':
                invoice_vals['invoice_payment_term_id'] = False 
                if invoice_date:
                    invoice_vals['invoice_date_due'] = invoice_date 

            # --- context 'tracking_disable': True -----------------
            # Le indica a Odoo que NO ejecute las funciones de mail.thread (chatter):
            # auto-suscripción, historial de "campo X cambió de A a B", mensaje de
            # "creado por...", etc. account.move hereda mail.thread y tiene bastantes
            # campos marcados como "tracked"; ese tracking se recalcula en cada
            # create/write. Esto NO toca contabilidad ni el resultado de la factura —
            # solo evita el cálculo/escritura de mensajes de chatter. Confirmado
            # vigente en Odoo (documentación oficial de mail.thread, Odoo 18/19).
            # MODIFICACIÓN: Se retira tracking_disable en la creación (create) para
            # permitir el mensaje inicial en el chatter, y se agrega un mensaje custom.
            inv_id = models.execute_kw(
                odoo_db, uid, odoo_pwd, 'account.move', 'create', [invoice_vals]
            )
            
            # ---- Mensaje en chatter: creación por API Data ----
            # try:
            #     models.execute_kw(
            #         odoo_db, uid, odoo_pwd, 'account.move', 'message_post', [[inv_id]],
            #         {'body': 'Factura exportada de MercadoLibre hacia Odoo por medio de API. Equipo Data.'}
            #     )
            # except Exception as ex_msg:
            #     log.debug(f"Aviso al publicar mensaje en chatter para orden {mkp_order}: {ex_msg}")
            # ------------------------------------------------------------------------------------------------

        # Buscar líneas para CxC (se ejecuta SIEMPRE: factura nueva O reanudada)
        move_lines = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move.line', 'search_read', 
                                       [[('move_id', '=', inv_id)]], {'fields': ['id', 'account_type']})
        
        # Consolidamos la escritura de CxC, UUID y narration en una sola llamada a account.move
        lines_to_update = [(1, m_line['id'], {'account_id': acc_cxc_ml}) for m_line in move_lines if m_line['account_type'] == 'asset_receivable']
        
        update_vals = {
            'l10n_mx_edi_cfdi_uuid': uuid, 
            'narration': uuid
        }
        if lines_to_update:
            update_vals['line_ids'] = lines_to_update
            
        models.execute_kw(
            odoo_db, uid, odoo_pwd, 'account.move', 'write', [[inv_id], update_vals],
            {'context': {'tracking_disable': True}}
        )

        # Adjuntar XML
        # --- Si estamos reanudando una factura
        # huérfana, puede que el intento anterior ya haya alcanzado a crear este
        # adjunto antes de morir. Revisamos si ya existe para no duplicarlo. ---
        attachment_id = None
        if resuming_draft:
            existing_attachment = models.execute_kw(
                odoo_db, uid, odoo_pwd, 'ir.attachment', 'search_read',
                [[('res_model', '=', 'account.move'), ('res_id', '=', inv_id), ('name', '=', f"{uuid}.xml")]],
                {'fields': ['id'], 'limit': 1}
            )
            if existing_attachment:
                attachment_id = existing_attachment[0]['id']

        if not attachment_id:
            attachment_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'ir.attachment', 'create', [{
                'name': f"{uuid}.xml",
                'datas': xml_data,
                'res_model': 'account.move',
                'res_id': inv_id
            }])

        try:
            models.execute_kw(odoo_db, uid, odoo_pwd, 'account.edi.document', 'create', [{
                'move_id': inv_id, 'edi_format_id': 2, 'attachment_id': attachment_id, 'state': 'sent'
            }])
        except Exception as ex:
            log.debug(f"Aviso EDI en orden {mkp_order}: {ex} (Ignorando)")

        # Publicar Factura (Operación pesada) - Se remueve tracking_disable para ver la confirmación action_post en el chatter
        try:
            models.execute_kw(
                odoo_db, uid, odoo_pwd, 'account.move', 'action_post', [[inv_id]]
            )
        except Exception as ex:
            if "cannot marshal None" not in str(ex):
                raise ex

        cursor.execute("UPDATE finance.mkp_billing_prod SET status = 'ODOO_INVOICED', odoo_so_name = %s, processed_at = NOW() WHERE id = %s", (so_data['name'], record_id))
        db.commit()
        log.info(f"[{thread_name}] ✅ ÉXITO: Factura de {so_data['name']} (ML: {mkp_order}) inyectada correctamente.")

    except Exception as e:
        db.rollback()
        # --- Se incrementa retry_count_loader para poder limitar
        # cuántas veces se reintenta un registro en ERROR (ver la consulta en
        # process_batch_concurrently). Antes este UPDATE no tocaba retry_count_loader. ---
        error_str = str(e)
        if "502" in error_str or "<html" in error_str.lower() or "doctype" in error_str.lower():
            if hasattr(thread_local_proxy, 'models'):
                del thread_local_proxy.models
            cursor.execute("""
            UPDATE finance.mkp_billing_prod 
            SET status = 'ERROR', error_log = %s, retry_count_loader = retry_count_loader + 1, processed_at = NOW() 
            WHERE id = %s
            """, (error_str, record_id))
            log.warning(f"[{thread_name}]Servidor Odoo saturado/reinicio detectado. Esperando 1s...")
            time.sleep(1)
        else:
            cursor.execute("""
            UPDATE finance.mkp_billing_prod 
            SET status = 'ERROR', error_log = %s, retry_count_loader = retry_count_loader + 1, processed_at = NOW() 
            WHERE id = %s
            """, (error_str, record_id))
            
        db.commit()
        log.error(f"[{thread_name}] ❌ ERROR en orden ML {mkp_order}: {e}")
    finally:
        cursor.close()
        db.close()

def process_batch_concurrently():
    odoo_url = os.getenv("odoo_urlV18")
    odoo_db = os.getenv("odoo_dbV18")
    odoo_user = os.getenv("odoo_user_dataV18")
    odoo_pwd = os.getenv("odoo_password_dataV18")
    
    try:
        common = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/common')
        uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
        models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')
        
        acc_cxc_ml = get_account_id(models, odoo_db, uid, odoo_pwd, '105.01.004')
        mx_country_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.country', 'search', [[('code', '=', 'MX')]], {'limit': 1})
        mx_country_id = mx_country_search[0] if mx_country_search else False
        
        # Cache de Métodos de pago
        pm_data = models.execute_kw(odoo_db, uid, odoo_pwd, 'l10n_mx_edi.payment.method', 'search_read', [[]], {'fields': ['id', 'code']})
        payment_methods_cache = {pm['code']: pm['id'] for pm in pm_data}

    except Exception as e:
        log.error(f"Error de conexión inicial con Odoo: {e}")
        return 0

    try:
        db_main = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor_main = db_main.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error conectando a BD principal: {e}")
        return 0

    # --- Ordenes pendientes ---------------------------------------------------------------
    # Antes solo se tomaban registros 'PENDING'. Ahora también se toman los 'ERROR' que:
    #   a) no llevan más de MAX_ERROR_RETRIES reintentos, Y
    #   b) el registro se creó dentro de los últimos ERROR_RETRY_WINDOW_DAYS días.
    # Se exige AMBAS condiciones (AND): así, una orden con error PERMANENTE (ej. "SO no
    # encontrada en Odoo", que nunca se va a resolver sola) deja de reintentarse en
    # cuanto se cumple CUALQUIERA de los dos límites, en vez de reintentarse para siempre.

    # También: ya no se usa SELECT * -> solo se piden las columnas que el resto del
    # script realmente usa (antes se traía, por ejemplo, raw_json completo -- el dump
    # de todo el XML a JSON -- para cada registro, sin usarlo en ningún lado de este
    # archivo).
    cursor_main.execute("""
        SELECT id, mkp_order_id, cfdi_uuid, xml_data, retry_count_loader
        FROM finance.mkp_billing_prod 
        WHERE marketplace = 'MERCADO_LIBRE' 
          AND (
                status = 'PENDING'
                OR (
                     status = 'ERROR'
                     AND IFNULL(retry_count_loader, 0) < %s
                     AND created_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
                   )
              )
        ORDER BY id ASC LIMIT %s
    """, (MAX_ERROR_RETRIES, ERROR_RETRY_WINDOW_DAYS, BATCH_LIMIT))
    records = cursor_main.fetchall()
    # ---------------------------------------------------------------------------------------
    
    if not records:
        cursor_main.close()
        db_main.close()
        return 0

    record_ids = tuple(r['id'] for r in records)
    format_strings = ','.join(['%s'] * len(record_ids))
    cursor_main.execute(f"UPDATE finance.mkp_billing_prod SET status = 'PROCESSING' WHERE id IN ({format_strings})", record_ids)
    db_main.commit()
    cursor_main.close()
    db_main.close()

    # ── OPTIMIZACIÓN: Búsquedas en Lote (Batch Fetching) antes de los hilos ──
    mkp_orders = [r['mkp_order_id'] for r in records]
    
    # 1. Buscar todas las SO de golpe
    so_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', 
                                  [[('channel_order_reference', 'in', mkp_orders)]], 
                                  {'fields': ['id', 'name', 'partner_id', 'team_id', 'order_line', 'channel_order_reference']})
    
    so_map = {so['channel_order_reference']: so for so in so_search}
    
    # 2. Buscar todas las líneas de golpe
    # --- Antes se pedían TODOS los campos de sale.order.line (no
    # se pasaba 'fields'), lo que obliga a Odoo a leer/computar cada campo -- incluidos
    # posibles campos calculados de módulos custom -- para cada línea del lote. Se
    # limita a los 5 campos que el resto del script realmente usa. ---
    all_line_ids = [line_id for so in so_search for line_id in so.get('order_line', [])]
    lines_search = []
    if all_line_ids:
        lines_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', 
                                         [[('id', 'in', all_line_ids)]],
                                         {'fields': ['id', 'display_type', 'product_id', 'product_uom_qty', 'price_unit']})
    lines_map = {line['id']: line for line in lines_search}

    # 3. Buscar facturas existentes de golpe (Anti-bucle)
    # --- Se agrega 'state' a los campos, para poder distinguir una
    # factura ya posteada de un borrador huérfano (ver process_single_invoice). ---
    so_names = [so['name'] for so in so_search]
    invoice_map = {}
    if so_names:
        existing_inv_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'search_read', 
                                          [[('invoice_origin', 'in', so_names), 
                                            ('move_type', '=', 'out_invoice'), 
                                            ('state', '!=', 'cancel')]], 
                                          {'fields': ['id', 'name', 'invoice_origin', 'state']})
        invoice_map = {inv['invoice_origin']: inv for inv in existing_inv_search}

    # ------------------- Registros PARTNER en map --------------------------------------------------
    # Parseamos el XML de cada registro del lote UNA vez, aquí (es trabajo local, no
    # llamadas a Odoo, así que no penaliza el batch), y con eso armamos la lista de RFCs
    # distintos para buscarlos/crearlos en Odoo EN BLOQUE -- en vez de una búsqueda (y a
    # veces una creación) de res.partner POR CADA factura del lote, como hacía antes.
    xml_parsed_map = {}
    rfcs_needed = set()

    for r in records:
        parsed = {
            'uso_cfdi': 'S01', 'forma_pago_code': '03', 'metodo_pago': 'PUE',
            'fecha_timbrado_str': False, 'rfc_receptor': False, 'nombre_receptor': False,
            'regimen_fiscal_receptor': False, 'cp_receptor': False
        }
        if r['xml_data']:
            try:
                decoded_xml = base64.b64decode(r['xml_data']).decode('utf-8')
                root = ET.fromstring(decoded_xml)
                ns = {'cfdi': 'http://www.sat.gob.mx/cfd/4', 'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'}

                parsed['forma_pago_code'] = root.get('FormaPago', '03')
                parsed['metodo_pago'] = root.get('MetodoPago', 'PUE')
                parsed['total_xml'] = float(root.get('Total', 0.0))

                receptor = root.find('cfdi:Receptor', ns)
                if receptor is not None:
                    parsed['uso_cfdi'] = receptor.get('UsoCFDI', 'S01')
                    parsed['rfc_receptor'] = receptor.get('Rfc')
                    parsed['nombre_receptor'] = receptor.get('Nombre')
                    parsed['regimen_fiscal_receptor'] = receptor.get('RegimenFiscalReceptor')
                    parsed['cp_receptor'] = receptor.get('DomicilioFiscalReceptor')

                tfd = root.find('.//tfd:TimbreFiscalDigital', ns)
                if tfd is not None:
                    parsed['fecha_timbrado_str'] = tfd.get('FechaTimbrado')
            except Exception as e:
                log.warning(f"Error parseando XML para orden {r['mkp_order_id']}: {e}. Se usarán defaults.")

        xml_parsed_map[r['id']] = parsed
        if parsed['rfc_receptor'] and parsed['rfc_receptor'] != 'XAXX010101000':
            rfcs_needed.add(parsed['rfc_receptor'])

    # Búsqueda en lote de partners existentes por RFC + creación de los faltantes
    # (secuencial, ANTES de lanzar los hilos, para no crear el mismo RFC dos veces)
    partner_map = {}
    if rfcs_needed:
        partner_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'search_read',
                                            [[('vat', 'in', list(rfcs_needed))]], {'fields': ['id', 'vat']})
        partner_map = {p['vat']: p['id'] for p in partner_search}

        missing_rfcs = rfcs_needed - set(partner_map.keys())
        for rfc in missing_rfcs:
            sample = next(p for p in xml_parsed_map.values() if p['rfc_receptor'] == rfc)
            new_partner_vals = {
                'name': sample['nombre_receptor'] or f'Cliente {rfc}',
                'vat': rfc,
                'zip': sample['cp_receptor'],
                'l10n_mx_edi_fiscal_regime': sample['regimen_fiscal_receptor'],
                'country_id': mx_country_id,
                'company_type': 'person' if len(rfc) == 13 else 'company'
            }
            try:
                new_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'create', [new_partner_vals],
                                            {'context': {'tracking_disable': True}})
                partner_map[rfc] = new_id
                log.info(f"Nuevo cliente creado en Odoo: {new_partner_vals['name']} (RFC: {rfc})")
            except Exception as e:
                log.warning(f"No se pudo crear el cliente {rfc}. Error: {e}")
    # -----------------------------------------------------------------------------------------

    # Contexto enriquecido para los hilos
    context = {
        'odoo_url': odoo_url,  # Agregado para soportar múltiples sesiones TLS por hilo
        'odoo_db': odoo_db, 'uid': uid, 'odoo_pwd': odoo_pwd,
        'acc_cxc_ml': acc_cxc_ml,
        'models': models,  # mismo ServerProxy para todos los hilos (ver nota en process_single_invoice)
        'so_map': so_map, 'lines_map': lines_map, 'invoice_map': invoice_map,
        'partner_map': partner_map, 'xml_parsed_map': xml_parsed_map,
        'payment_methods_cache': payment_methods_cache
    }

    log.info(f"Lote de {len(records)} facturas pre-cargado. Iniciando Multithreading...")

    # Mantenemos 1 worker para no estresar el CPU de Odoo con el action_post simultáneo
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_single_invoice, rec, context) for rec in records]
        concurrent.futures.wait(futures)
        
        # Aumentamos ligeramente el respiro para que el Garbage Collector de Odoo actúe
        log.info(f"Lote terminado. Dando {SLEEP_BETWEEN_BATCHES} segundos de respiro a la RAM de Odoo...")
        time.sleep(SLEEP_BETWEEN_BATCHES)

    return len(records)

if __name__ == "__main__":
    log.info("=== Iniciando Inyección CONCURRENTE de Facturas ML a Odoo ===")

    # --- Limpieza de registros trabados de una corrida anterior ---
    reset_stuck_processing_records()

    try:
        empty_runs = 0
        while empty_runs < MAX_EMPTY_RUNS:
            processed_count = process_batch_concurrently()

            if processed_count == 0:
                empty_runs += 1
                time.sleep(SLEEP_SECONDS)
            else:
                empty_runs = 0  

        log.info("=== Proceso de Inyección Finalizado (Sin registros pendientes) ===")

    except Exception:
        log.exception("Error general durante la orquestación de la inyección.")