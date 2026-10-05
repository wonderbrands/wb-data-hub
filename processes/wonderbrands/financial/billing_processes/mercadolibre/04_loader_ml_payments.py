import xmlrpc.client
import mysql.connector
import os
import logging
import sys
from ml_stores import get_store_config

# ── Logging setup ──────────────────────────────────────────────
# Solo StreamHandler: Kestra captura stdout/stderr como logs de la ejecución.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Parámetros por variables de entorno ────────────────────────
# Definidas en flows/wonderbrands/financial/mercadolibre/ml_payments_flow.yml
# y ml_oficiales_payments_flow.yml
ODOO_URL     = os.getenv("ODOO_URL")
ODOO_DB       = os.getenv("ODOO_DB")
ODOO_USER     = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

# Tienda de Mercado Libre (Kestra inyecta ML_SELLER_ID; el resto sale de ml_stores.py)
STORE                    = get_store_config()
MKP_MARKETPLACE          = STORE['marketplace']
# Código del diario de Mercado Pago en Odoo y su cuenta por defecto esperada (None = sin candado)
MP_JOURNAL_CODE          = STORE['mp_journal_code']
MP_ACCOUNT_CODE          = STORE['mp_account_code']
# l10n_mx_edi.payment.method: 3 = Transferencia
EDI_PAYMENT_METHOD_ID    = int(os.getenv("EDI_PAYMENT_METHOD_ID", "3"))
ODOO_TIMEOUT             = int(os.getenv("ODOO_TIMEOUT", "120"))  # Timeout XML-RPC en segundos


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """Transporte personalizado para forzar un timeout en XML-RPC."""
    def __init__(self, timeout=ODOO_TIMEOUT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


def load_ml_payments_to_odoo():
    # ── Validación de configuración ────────────────────────────
    missing = [k for k, v in {
        "ODOO_URL": ODOO_URL, "ODOO_DB": ODOO_DB, "ODOO_USER": ODOO_USER,
        "ODOO_PASSWORD": ODOO_PASSWORD, "DB_HOST": DB_HOST, "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD, "DB_NAME": DB_NAME
    }.items() if not v]
    if missing:
        log.error(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
        raise SystemExit(1)

    # ── 1. Conexión a Base de Datos ──────────────────────────────
    try:
        db = mysql.connector.connect(
            host=DB_HOST, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error conectando a BD: {e}")
        raise SystemExit(1)

    cursor.execute("""
        SELECT p.*, b.odoo_so_name
        FROM finance.mkp_payments_prod p
        JOIN finance.mkp_billing_prod b
          ON b.marketplace = p.marketplace
         AND b.mkp_order_id = p.mkp_order_id
        WHERE p.marketplace = %s
          AND p.status = 'PENDING'
    """, (MKP_MARKETPLACE,))
    pending_payments = cursor.fetchall()
    log.info(f"[{MKP_MARKETPLACE}] Pagos pendientes de aplicar en Odoo: {len(pending_payments)}")

    if not pending_payments:
        cursor.close()
        db.close()
        return

    # ── 2. Conexión a Odoo 18 ────────────────────────────────────
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common', transport=TimeoutTransport())
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise Exception("Autenticación rechazada por Odoo.")
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', transport=TimeoutTransport())

        # Buscar el Diario de Mercado Pago (con su cuenta por defecto para el candado)
        journal_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.journal', 'search_read',
                                           [[('code', '=', MP_JOURNAL_CODE)]],
                                           {'fields': ['id', 'default_account_id'], 'limit': 1})

        expected_account = []
        if journal_search and MP_ACCOUNT_CODE:
            expected_account = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.account', 'search',
                                                 [[('code', '=', MP_ACCOUNT_CODE)]], {'limit': 1})
    except Exception as e:
        log.error(f"Error de conexión inicial con Odoo: {e}")
        cursor.close()
        db.close()
        raise SystemExit(1)

    if not journal_search:
        log.error(f"No se encontró el diario de Mercado Pago (code='{MP_JOURNAL_CODE}') en Odoo.")
        cursor.close()
        db.close()
        raise SystemExit(1)
    journal_id = journal_search[0]['id']

    # Candado: el diario debe tener como cuenta por defecto la cuenta de la tienda.
    # Evita aplicar cobros de una tienda en la cuenta de banco de otra.
    if MP_ACCOUNT_CODE:
        default_account = journal_search[0]['default_account_id']
        if not expected_account or not default_account or default_account[0] != expected_account[0]:
            log.error(
                f"El diario '{MP_JOURNAL_CODE}' no tiene como cuenta por defecto la {MP_ACCOUNT_CODE} "
                f"(tiene: {default_account[1] if default_account else 'ninguna'}). Abortando sin aplicar cobros."
            )
            cursor.close()
            db.close()
            raise SystemExit(1)

    # ── Contadores (los detalles por pago NO se loguean: solo el resumen final) ──
    applied     = 0
    not_applied = 0

    for record in pending_payments:
        so_name = record['odoo_so_name']
        try:
            # A) Buscar la Factura Publicada vinculada a la SO
            inv_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search_read',
                                           [[('invoice_origin', '=', so_name), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]],
                                           {'fields': ['id', 'name'], 'limit': 1})

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
                'l10n_mx_edi_payment_method_id': EDI_PAYMENT_METHOD_ID,
            }

            # D) Crear el registro del Wizard en Odoo
            wizard_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'create',
                                          [wizard_vals], {'context': wizard_context})

            # E) Ejecutar la acción del Wizard
            # Esto crea el account.payment real y concilia las líneas (account.move.line) de forma nativa.
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'action_create_payments',
                              [[wizard_id]], {'context': wizard_context})

            # F) Actualizar MySQL
            cursor.execute("UPDATE finance.mkp_payments_prod SET status = 'ODOO_PAID', processed_at = NOW() WHERE id = %s", (record['id'],))
            db.commit()
            applied += 1

        except Exception as e:
            db.rollback()
            cursor.execute("UPDATE finance.mkp_payments_prod SET status = 'ERROR', error_log = %s WHERE id = %s", (str(e), record['id']))
            db.commit()
            not_applied += 1

    # ── Resumen único de la corrida ────────────────────────────
    log.info(
        f"[{MKP_MARKETPLACE}] Resumen -> Pagos pendientes: {len(pending_payments)} | "
        f"Aplicados en Odoo: {applied} | No aplicados (ERROR): {not_applied}"
    )
    cursor.close()
    db.close()


if __name__ == "__main__":
    load_ml_payments_to_odoo()
