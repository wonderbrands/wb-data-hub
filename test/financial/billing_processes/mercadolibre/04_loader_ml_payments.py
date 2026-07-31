import xmlrpc.client
import mysql.connector
import os
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

def load_ml_payments_to_odoo():
    # ── 1. Conexión a Base de Datos ──────────────────────────────
    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error conectando a BD: {e}")
        return

    cursor.execute("""
        SELECT p.*, b.odoo_so_name 
        FROM finance.mkp_payments_prod p
        JOIN finance.mkp_billing_prod b ON p.mkp_order_id = b.mkp_order_id
        WHERE p.status = 'PENDING' LIMIT 1000
    """)
    pending_payments = cursor.fetchall()

    if not pending_payments:
        log.info("No hay pagos pendientes de procesar.")
        return

    # ── 2. Conexión a Odoo 18 ────────────────────────────────────
    odoo_url = os.getenv("odoo_urlV18")
    odoo_db = os.getenv("odoo_dbV18")
    odoo_user = os.getenv("odoo_user_dataV18")
    odoo_pwd = os.getenv("odoo_password_dataV18")
    
    common = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/common')
    uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
    models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')

    # Buscar el ID del Diario de Mercado Pago
    # IMPORTANTE: Reemplaza 'MP' con el código real de tu diario en Odoo
    journal_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.journal', 'search', [[('code', '=', 'MP')]], {'limit': 1})
    if not journal_search:
        log.error("No se encontró el diario de Mercado Pago. Verifica el código en Odoo.")
        return
    journal_id = journal_search[0]

    for record in pending_payments:
        so_name = record['odoo_so_name']
        try:
            # A) Buscar la Factura Publicada vinculada a la SO
            inv_search = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'search_read', 
                                           [[('invoice_origin', '=', so_name), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]], 
                                           {'fields': ['id', 'name'], 'limit': 1})
            
            inv_search_2 = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.move', 'search_read', 
                                           [[('invoice_origin', '=', so_name), ('state', '=', 'posted')]], 
                                           {'fields': ['id', 'name'], 'limit': 1})
            print(inv_search, inv_search_2)
            
            if not inv_search: 
                raise Exception(f"Factura publicada para {so_name} no encontrada.")
            
            inv = inv_search[0]

            # B) Configurar el Contexto del Wizard (Es vital para que Odoo sepa qué factura estamos pagando)
            wizard_context = {
                'active_model': 'account.move',
                'active_ids': [inv['id']]
            }

            # C) Valores del Wizard (account.payment.register)
            wizard_vals = {
                'journal_id': journal_id,
                'amount': float(record['amount']),
                'payment_date': record['date_released'].strftime("%Y-%m-%d"),
                'l10n_mx_edi_payment_method_id': 3, # Transferencia
            }
            
            # D) Crear el registro del Wizard en Odoo
            wizard_id = models.execute_kw(odoo_db, uid, odoo_pwd, 'account.payment.register', 'create', [wizard_vals], {'context': wizard_context})
            
            # E) Ejecutar la acción del Wizard
            # Esto crea el account.payment real y concilia las líneas (account.move.line) de forma nativa.
            models.execute_kw(odoo_db, uid, odoo_pwd, 'account.payment.register', 'action_create_payments', [[wizard_id]], {'context': wizard_context})

            log.info(f"✅ Cobro aplicado y conciliado nativamente para {so_name} (IVA trasladado con éxito)")

            # F) Actualizar MySQL
            cursor.execute("UPDATE finance.mkp_payments_prod SET status = 'ODOO_PAID', processed_at = NOW() WHERE id = %s", (record['id'],))
            db.commit()

        except Exception as e:
            db.rollback()
            cursor.execute("UPDATE finance.mkp_payments_prod SET status = 'ERROR', error_log = %s WHERE id = %s", (str(e), record['id']))
            db.commit()
            log.error(f"❌ Error en cobro de {so_name}: {e}")

    cursor.close()
    db.close()

if __name__ == "__main__":
    load_ml_payments_to_odoo()