import xmlrpc.client
import mysql.connector
import os
import logging
import time
import base64
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

# ── Configuración de Logs ──────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Cargar variables de entorno
#load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# IVA(16%) Marketplaces
TAX_ID_MARKETPLACES = 38 # 37 staging / 38 prod 

def get_account_id(models, db, uid, pwd, code):
    """Busca el ID interno de Odoo para una cuenta contable por su código."""
    acc = models.execute_kw(db, uid, pwd, 'account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"¡ALERTA! No se encontró la cuenta contable con código {code} en Odoo.")
    return acc[0]

def load_ml_invoices_to_odoo():
    # ── Conexión a Base de Datos Staging ──────────────────────
    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), 
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), 
            database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error conectando a BD: {e}")
        return 0

    # Buscar registros pendientes
    cursor.execute("""
        SELECT * FROM finance.mkp_billing_prod 
        WHERE marketplace = 'MERCADO_LIBRE' AND status = 'PENDING' 
        ORDER BY id ASC LIMIT 100
    """)
    pending_records = cursor.fetchall()
    
    if not pending_records:
        log.info("No hay facturas pendientes de inyectar en este lote.")
        cursor.close()
        db.close()
        return 0

    # ── 2. Conexión a Odoo 18 ────────────────────────────────────
    odoo_url = os.getenv("odoo_urlV18")
    odoo_db = os.getenv("odoo_dbV18")
    odoo_user = os.getenv("odoo_user_dataV18")
    odoo_pwd = os.getenv("odoo_password_dataV18")
    
    try:
        common = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/common')
        uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
        models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')
    except Exception as e:
        log.error(f"Error de conexión con Odoo XML-RPC: {e}")
        return 0
    
    # ── 3. Pre-carga de Cuentas y Datos Maestros ─────────────────
    try:
        acc_cxc_ml = get_account_id(models, odoo_db, uid, odoo_pwd, '105.01.004')
        log.info(f"Cuenta CxC Mercado Libre obtenida (ID: {acc_cxc_ml})")
        
        # Pre-cargar ID de México (MX) para evitar llamadas en el ciclo
        mx_country_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.country', 'search', [[('code', '=', 'MX')]], {'limit': 1})
        mx_country_id = mx_country_search[0] if mx_country_search else False
        
    except Exception as e:
        log.error(str(e))
        return 0

    log.info(f"Procesando {len(pending_records)} registros hacia Odoo...")

    # ── 4. Ciclo Principal de Inyección ──────────────────────────
    for record in pending_records:
        mkp_order = record['mkp_order_id']
        uuid = record['cfdi_uuid']
        xml_data = record['xml_data']
        
        try:
            # A) Buscar la Orden de Venta (SO)
            so_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', 
                                          [[('channel_order_reference', '=', mkp_order)]], 
                                          {'fields': ['id', 'name', 'partner_id', 'team_id', 'order_line'], 'limit': 1})
            
            if not so_search:
                raise Exception(f"Orden de Venta con referencia {mkp_order} no encontrada en Odoo.")
            
            so = so_search[0]
            so_lines = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', 
                                         [[('id', 'in', so['order_line'])]])
            
            # -------------------------------------------------------------------------
            so = so_search[0]
            
            # Consultar si la orden ya tiene factura
            existing_invoice = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'search_read', 
                                          [[('invoice_origin', 'ilike', so['name']), 
                                            ('move_type', '=', 'out_invoice'), 
                                            ('state', '!=', 'cancel')]], 
                                          {'fields': ['id', 'name'], 'limit': 1})
            
            if existing_invoice:
                log.warning(f"Bucle evitado: La orden {so['name']} ({mkp_order}) YA TIENE la factura {existing_invoice[0]['name']} en Odoo.")
                # Actualizamos staging para no volver a procesarla
                cursor.execute("UPDATE finance.mkp_billing_prod SET status = 'ALREADY_ODOO_INVOICED', processed_at = NOW() WHERE id = %s", (record['id'],))
                db.commit()
                continue # Saltamos a la siguiente orden de la lista
            # -------------------------------------------------------------------------
            
            # --- XML BASE64 ---
            uso_cfdi = 'S01'
            forma_pago_code = '03'
            metodo_pago = 'PUE' # <-- NUEVA VARIABLE DEFAULT
            fecha_timbrado_str = False
            
            # --- NUEVAS VARIABLES PARA RECEPTOR ---
            rfc_receptor = False
            nombre_receptor = False
            regimen_fiscal_receptor = False
            cp_receptor = False
            
            if xml_data:
                try:
                    # 1. Decodificar el string Base64
                    decoded_xml = base64.b64decode(xml_data).decode('utf-8')
                    
                    # 2. Parsear el XML
                    root = ET.fromstring(decoded_xml)
                    
                    # Definir los namespaces usados en el CFDI para poder buscar nodos
                    ns = {
                        'cfdi': 'http://www.sat.gob.mx/cfd/4',
                        'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'
                    }
                    
                    # Extraer FormaPago y MetodoPago del nodo raíz (cfdi:Comprobante)
                    forma_pago_code = root.get('FormaPago', '03')
                    metodo_pago = root.get('MetodoPago', 'PUE') # <-- EXTRACCIÓN DE METODO DE PAGO
                    
                    # Extraer UsoCFDI y datos fiscales del nodo Receptor
                    receptor = root.find('cfdi:Receptor', ns)
                    if receptor is not None:
                        uso_cfdi = receptor.get('UsoCFDI', 'S01')
                        # --- EXTRACCIÓN DE DATOS DEL CLIENTE ---
                        rfc_receptor = receptor.get('Rfc')
                        nombre_receptor = receptor.get('Nombre')
                        regimen_fiscal_receptor = receptor.get('RegimenFiscalReceptor')
                        cp_receptor = receptor.get('DomicilioFiscalReceptor')
                    
                    # Extraer FechaTimbrado del nodo TimbreFiscalDigital
                    tfd = root.find('.//tfd:TimbreFiscalDigital', ns)
                    if tfd is not None:
                        fecha_timbrado_str = tfd.get('FechaTimbrado')
                        
                except Exception as e:
                    log.warning(f"Error decodificando/parseando XML para orden {mkp_order}: {e}. Se usarán defaults.")

            # Buscar ID del método de pago en Odoo según el código del XML
            pm_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'l10n_mx_edi.payment.method', 'search', 
                                          [[('code', '=', forma_pago_code)]], {'limit': 1})
            # Si lo encuentra usa el ID devuelto, si no, hace fallback al ID 3
            payment_method_id = pm_search[0] if pm_search else 3

            # Formatear la fecha para Odoo (De '2026-06-01T14:51:48' a '2026-06-01')
            invoice_date = False
            if fecha_timbrado_str:
                invoice_date = fecha_timbrado_str.split('T')[0]
            # ---------------------------------------------------
            
            # --- LÓGICA DE PARTNER (CLIENTE) -------------------
            partner_invoice_id = so['partner_id'][0] # Default al partner de Público en General / Genérico de la SO
            
            if rfc_receptor and rfc_receptor != 'XAXX010101000':
                # Buscar si el cliente ya existe en Odoo por su RFC
                partner_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'search_read',
                                                   [[('vat', '=', rfc_receptor)]],
                                                   {'fields': ['id'], 'limit': 1})
                if partner_search:
                    partner_invoice_id = partner_search[0]['id']
                else:
                    new_partner_vals = {
                        'name': nombre_receptor or f'Cliente {rfc_receptor}',
                        'vat': rfc_receptor,
                        'zip': cp_receptor,
                        'l10n_mx_edi_fiscal_regime': regimen_fiscal_receptor,
                        'country_id': mx_country_id, # <-- AQUÍ USAMOS LA VARIABLE PRE-CARGADA
                        'company_type': 'person' if len(rfc_receptor) == 13 else 'company' # Persona Física o Moral
                    }
                    try:
                        partner_invoice_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'res.partner', 'create', [new_partner_vals])
                        log.info(f"Nuevo cliente creado en Odoo: {new_partner_vals['name']} (RFC: {rfc_receptor})")
                    except Exception as e:
                        log.warning(f"No se pudo crear el cliente {rfc_receptor}, se usará el genérico de la SO. Error: {e}")
            # ---------------------------------------------------

            # B) Armar las líneas forzando la cantidad ordenada y el IVA(16%) Marketplaces
            invoice_lines = []
            for line in so_lines:
                inv_line = {
                    'display_type': line.get('display_type') or 'product',
                    'product_id': line['product_id'][0] if line.get('product_id') else False,
                    'quantity': line['product_uom_qty'], # Forzamos cantidad pedida
                    'price_unit': line['price_unit'],
                    'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])], #(IVA COBRADO)
                    'sale_line_ids': [(4, line['id'])]
                }
                invoice_lines.append((0, 0, inv_line))

            # C) Crear Factura en estado BORRADOR
            invoice_vals = {
                'move_type': 'out_invoice',
                'invoice_origin': so['name'],
                'partner_id': partner_invoice_id, # Asigna el cliente real o el genérico
                'team_id': so['team_id'][0],
                'invoice_line_ids': invoice_lines,
                'l10n_mx_edi_usage': uso_cfdi, # Extraído dinámicamente del XML
                'l10n_mx_edi_payment_method_id': payment_method_id, # ID obtenido dinámicamente
                'l10n_mx_edi_payment_policy': metodo_pago, # <-- NUEVO MAPEO DE METODO DE PAGO (PUE/PPD)
            }
            
            # Asignar fechas si se extrajo correctamente del XML
            if invoice_date:
                invoice_vals['invoice_date'] = invoice_date
                invoice_vals['date'] = invoice_date
                
            # --- EVITAR QUE ODOO CAMBIE A PPD AUTOMÁTICAMENTE ---
            # Si es PUE, forzamos que no haya plazos de pago y que venza el mismo día.
            if metodo_pago == 'PUE':
                invoice_vals['invoice_payment_term_id'] = False # Anulamos cualquier plazo del nuevo cliente
                if invoice_date:
                    invoice_vals['invoice_date_due'] = invoice_date # Obligamos vencimiento inmediato
            # ---------------------------------------------------------
                
            inv_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'create', [invoice_vals])

            # D) Interceptar y modificar Cuenta por Cobrar vía API
            move_lines = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move.line', 'search_read', 
                                           [[('move_id', '=', inv_id)]], 
                                           {'fields': ['id', 'account_type']})
            
            lines_to_update = []
            for m_line in move_lines:
                # Modificamos solo la cuenta por cobrar (Clientes) a ML
                if m_line['account_type'] == 'asset_receivable':
                    lines_to_update.append((1, m_line['id'], {'account_id': acc_cxc_ml}))
            
            if lines_to_update:
                models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'write', [[inv_id], {'line_ids': lines_to_update}])

            # E) Adjuntar XML y escribir UUID en la factura
            models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'write', 
                              [[inv_id], {'l10n_mx_edi_cfdi_uuid': uuid, 'narration': uuid}])
            
            attachment_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'ir.attachment', 'create', [{
                'name': f"{uuid}.xml",
                'datas': xml_data,
                'res_model': 'account.move',
                'res_id': inv_id
            }])

            # F) Crear Fake Stamp (Simular envío al PAC en Odoo 18)
            try:
                models.execute_kw(odoo_db, uid, odoo_pwd, 'account.edi.document', 'create', [{
                    'move_id': inv_id, 
                    'edi_format_id': 2, 
                    'attachment_id': attachment_id, 
                    'state': 'sent'
                }])
            except Exception as ex:
                log.warning(f"Aviso EDI en orden {mkp_order}: {ex} (Ignorando y continuando)")

            # G) Publicar Factura (action_post)
            # En Odoo 18, si el método devuelve None por XML-RPC, lanza un "cannot marshal None". Lo capturamos de forma segura.
            try:
                models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'action_post', [[inv_id]])
            except Exception as ex:
                if "cannot marshal None" not in str(ex):
                    raise ex

            # H) Marcar como EXITOSO en Staging DB
            cursor.execute("""
                UPDATE finance.mkp_billing_prod 
                SET status = 'ODOO_INVOICED', odoo_so_name = %s, processed_at = NOW() 
                WHERE id = %s
            """, (so['name'], record['id']))
            db.commit()
            
            log.info(f"✅ ÉXITO: Factura de {so['name']} (ML: {mkp_order}) inyectada correctamente.")
            time.sleep(0.2) # Pequeña pausa para no saturar Odoo XML-RPC

        except Exception as e:
            # En caso de error, marcamos el registro pero el script continúa con el siguiente
            db.rollback()
            cursor.execute("""
                UPDATE finance.mkp_billing_prod 
                SET status = 'ERROR', error_log = %s, processed_at = NOW() 
                WHERE id = %s
            """, (str(e), record['id']))
            db.commit()
            log.error(f"❌ ERROR en orden ML {mkp_order}: {e}")

    # ── 5. Cierre de Conexiones ──────────────────────────────────
    cursor.close()
    db.close()

if __name__ == "__main__":
    log.info("=== Iniciando Inyección de Facturas ML a Odoo ===")
    MAX_EMPTY_RUNS = 5
    SLEEP_SECONDS = 10

    try:
        empty_runs = 0
        while empty_runs < MAX_EMPTY_RUNS:
            result = load_ml_invoices_to_odoo()

            if result == 0:
                empty_runs += 1
            else:
                empty_runs = 0  # Reinicia si se procesó algo

            time.sleep(SLEEP_SECONDS)

        log.info("=== Proceso de Inyección Finalizado ===")

    except Exception:
        log.exception("Error general durante la inyección de facturas.")