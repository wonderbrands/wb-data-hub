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

# ── Configuración de Logs ──────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

TAX_ID_MARKETPLACES = 38 

def get_account_id(models, db, uid, pwd, code):
    acc = models.execute_kw(db, uid, pwd, 'account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"¡ALERTA! No se encontró la cuenta contable con código {code} en Odoo.")
    return acc[0]

def process_single_invoice(record, context):
    thread_name = threading.current_thread().name
    
    odoo_url = context['odoo_url']
    odoo_db = context['odoo_db']
    uid = context['uid']
    odoo_pwd = context['odoo_pwd']
    acc_cxc_ml = context['acc_cxc_ml']
    mx_country_id = context['mx_country_id']
    payment_methods_cache = context['payment_methods_cache']
    
    # Datos cacheados del lote
    so_data = context['so_map'].get(record['mkp_order_id'])
    lines_map = context['lines_map']
    existing_invoices = context['invoice_map']
    
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
        log.error(f"❌ Error de BD en Hilo (Orden {mkp_order}): {e}")
        return

    models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')
    
    try:
        if not so_data:
            raise Exception(f"Orden de Venta con referencia {mkp_order} no encontrada en Odoo.")
        
        # Validación de bucle cacheada
        if so_data['name'] in existing_invoices:
            inv_name = existing_invoices[so_data['name']]['name']
            log.warning(f"Bucle evitado: La orden {so_data['name']} ({mkp_order}) YA TIENE la factura {inv_name}.")
            cursor.execute("UPDATE finance.mkp_billing_prod SET status = 'ALREADY_ODOO_INVOICED', processed_at = NOW() WHERE id = %s", (record_id,))
            db.commit()
            return 
            
        # --- XML BASE64 ---
        uso_cfdi = 'S01'
        forma_pago_code = '03'
        metodo_pago = 'PUE' 
        fecha_timbrado_str = False
        rfc_receptor = False
        nombre_receptor = False
        regimen_fiscal_receptor = False
        cp_receptor = False
        
        if xml_data:
            try:
                decoded_xml = base64.b64decode(xml_data).decode('utf-8')
                root = ET.fromstring(decoded_xml)
                ns = {'cfdi': 'http://www.sat.gob.mx/cfd/4', 'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'}
                
                forma_pago_code = root.get('FormaPago', '03')
                metodo_pago = root.get('MetodoPago', 'PUE') 
                
                receptor = root.find('cfdi:Receptor', ns)
                if receptor is not None:
                    uso_cfdi = receptor.get('UsoCFDI', 'S01')
                    rfc_receptor = receptor.get('Rfc')
                    nombre_receptor = receptor.get('Nombre')
                    regimen_fiscal_receptor = receptor.get('RegimenFiscalReceptor')
                    cp_receptor = receptor.get('DomicilioFiscalReceptor')
                
                tfd = root.find('.//tfd:TimbreFiscalDigital', ns)
                if tfd is not None:
                    fecha_timbrado_str = tfd.get('FechaTimbrado')
            except Exception as e:
                log.warning(f"Error parseando XML para orden {mkp_order}: {e}. Se usarán defaults.")

        payment_method_id = payment_methods_cache.get(forma_pago_code, 3)
        invoice_date = fecha_timbrado_str.split('T')[0] if fecha_timbrado_str else False
        
        # --- LÓGICA DE PARTNER ---
        partner_invoice_id = so_data['partner_id'][0] 
        
        if rfc_receptor and rfc_receptor != 'XAXX010101000':
            partner_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'search_read',
                                               [[('vat', '=', rfc_receptor)]], {'fields': ['id'], 'limit': 1})
            if partner_search:
                partner_invoice_id = partner_search[0]['id']
            else:
                new_partner_vals = {
                    'name': nombre_receptor or f'Cliente {rfc_receptor}',
                    'vat': rfc_receptor,
                    'zip': cp_receptor,
                    'l10n_mx_edi_fiscal_regime': regimen_fiscal_receptor,
                    'country_id': mx_country_id, 
                    'company_type': 'person' if len(rfc_receptor) == 13 else 'company' 
                }
                try:
                    partner_invoice_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'create', [new_partner_vals])
                except Exception as e:
                    log.warning(f"No se pudo crear el cliente {rfc_receptor}. Error: {e}")

        # Armar líneas desde caché
        invoice_lines = []
        for line_id in so_data['order_line']:
            line = lines_map.get(line_id)
            if not line: continue
            inv_line = {
                'display_type': line.get('display_type') or 'product',
                'product_id': line['product_id'][0] if line.get('product_id') else False,
                'quantity': line['product_uom_qty'], 
                'price_unit': line['price_unit'],
                'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])], 
                'sale_line_ids': [(4, line['id'])]
            }
            invoice_lines.append((0, 0, inv_line))

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
            
        inv_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'create', [invoice_vals])

        # Buscar líneas para CxC
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
            
        models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'write', [[inv_id], update_vals])

        # Adjuntar XML
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

        # Publicar Factura (Operación pesada)
        try:
            models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'action_post', [[inv_id]])
        except Exception as ex:
            if "cannot marshal None" not in str(ex):
                raise ex

        cursor.execute("UPDATE finance.mkp_billing_prod SET status = 'ODOO_INVOICED', odoo_so_name = %s, processed_at = NOW() WHERE id = %s", (so_data['name'], record_id))
        db.commit()
        log.info(f"[{thread_name}] ✅ ÉXITO: Factura de {so_data['name']} (ML: {mkp_order}) inyectada correctamente.")

    except Exception as e:
        db.rollback()
        cursor.execute("UPDATE finance.mkp_billing_prod SET status = 'ERROR', error_log = %s, processed_at = NOW() WHERE id = %s", (str(e), record_id))
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

    cursor_main.execute("""
        SELECT * FROM finance.mkp_billing_prod 
        WHERE marketplace = 'MERCADO_LIBRE' AND status = 'PENDING' 
        ORDER BY id ASC LIMIT 40
    """)
    records = cursor_main.fetchall()
    
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
    all_line_ids = [line_id for so in so_search for line_id in so.get('order_line', [])]
    lines_search = []
    if all_line_ids:
        lines_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', 
                                         [[('id', 'in', all_line_ids)]])
    lines_map = {line['id']: line for line in lines_search}

    # 3. Buscar facturas existentes de golpe (Anti-bucle)
    so_names = [so['name'] for so in so_search]
    invoice_map = {}
    if so_names:
        existing_inv_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'search_read', 
                                          [[('invoice_origin', 'in', so_names), 
                                            ('move_type', '=', 'out_invoice'), 
                                            ('state', '!=', 'cancel')]], 
                                          {'fields': ['id', 'name', 'invoice_origin']})
        invoice_map = {inv['invoice_origin']: inv for inv in existing_inv_search}

    # Contexto enriquecido para los hilos
    context = {
        'odoo_url': odoo_url, 'odoo_db': odoo_db, 'uid': uid, 'odoo_pwd': odoo_pwd,
        'acc_cxc_ml': acc_cxc_ml, 'mx_country_id': mx_country_id,
        'payment_methods_cache': payment_methods_cache,
        'so_map': so_map, 'lines_map': lines_map, 'invoice_map': invoice_map
    }

    log.info(f"Lote de {len(records)} facturas pre-cargado. Iniciando Multithreading...")

    # Mantenemos 2 workers para no estresar el CPU de Odoo con el action_post simultáneo
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        futures = [executor.submit(process_single_invoice, rec, context) for rec in records]
        concurrent.futures.wait(futures)
        
        # Aumentamos ligeramente el respiro para que el Garbage Collector de Odoo actúe
        log.info("Lote terminado. Dando 20 segundos de respiro a la RAM de Odoo...")
        time.sleep(20)

    return len(records)

if __name__ == "__main__":
    log.info("=== Iniciando Inyección CONCURRENTE de Facturas ML a Odoo ===")
    MAX_EMPTY_RUNS = 5
    SLEEP_SECONDS = 10

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