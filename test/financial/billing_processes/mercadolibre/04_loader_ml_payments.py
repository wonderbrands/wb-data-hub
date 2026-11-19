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
# Tabla de NC de MeLi (06). Si el pago excede el saldo porque una NC ya bajó la factura (disputa o
# reembolso parcial), el excedente se reversa contra la cuenta del diario MP, como hace el 06.
NC_TABLE                 = os.getenv("NC_TABLE", "finance.mkp_credit_notes_prod")
COBRO_REF_PREFIX         = "MLNC-COBRO-PAGO:"   # ref del reverso: uno por pago (idempotente)
EXCESS_TOLERANCE         = 0.01


def call_no_return(models, uid, model, method, args):
    """reconcile no devuelve valor y XML-RPC falla al responder ('cannot marshal None'),
    aunque Odoo sí ejecutó la operación. Se confirma después leyendo."""
    try:
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args)
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" not in str(e):
            raise


def reverse_payment_excess(models, uid, journal, payment_line, excess, payment_id, date):
    """Debe CxC / Haber cuenta del diario MP por el excedente del pago, conciliado contra el pago.
    Mismo asiento que el reverso de cobro del 06. Idempotente por ref."""
    ref = f"{COBRO_REF_PREFIX}{payment_id}"
    found = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search',
                              [[('ref', '=', ref), ('state', '=', 'posted')]], {'limit': 1})
    if found:
        move_id = found[0]
    else:
        lines = [
            {'account_id': payment_line['account_id'][0], 'partner_id': payment_line['partner_id'][0],
             'debit': excess, 'credit': 0.0, 'name': f"Reverso cobro NC ML (pago {payment_id})"},
            {'account_id': journal['default_account_id'][0], 'partner_id': payment_line['partner_id'][0],
             'debit': 0.0, 'credit': excess, 'name': f"Reverso cobro NC ML (pago {payment_id})"},
        ]
        move_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'create',
                                    [{'move_type': 'entry', 'journal_id': journal['id'], 'date': date,
                                      'ref': ref, 'line_ids': [(0, 0, l) for l in lines]}])
        call_no_return(models, uid, 'account.move', 'action_post', [[move_id]])
    debit_line = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move.line', 'search',
                                   [[('move_id', '=', move_id), ('account_id', '=', payment_line['account_id'][0]),
                                     ('reconciled', '=', False)]])
    if debit_line:
        call_no_return(models, uid, 'account.move.line', 'reconcile', [[payment_line['id']] + debit_line])


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """Transporte personalizado para forzar un timeout en XML-RPC."""
    def __init__(self, timeout=ODOO_TIMEOUT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


def apply_payment(models, uid, journal, record, has_nc):
    """Aplica un cobro de MP a la factura de su SO. Devuelve (status, nota).
    Se aplica el BRUTO del pago. Si excede el saldo porque una NC ya bajó la factura, el
    excedente se reversa contra la cuenta del diario MP (solo si la NC está registrada)."""
    so_name = record['odoo_so_name']
    # A) Buscar la Factura Publicada vinculada a la SO
    inv_search = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search_read',
                                   [[('invoice_origin', '=', so_name), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]],
                                   {'fields': ['id', 'name', 'amount_residual'], 'limit': 1})
    if not inv_search:
        raise Exception(f"Factura publicada para {so_name} no encontrada.")
    inv = inv_search[0]
    amount = float(record['amount'])

    # A2) Factura ya saldada: Odoo no deja registrar el pago. No se toca; queda para revisión.
    if inv['amount_residual'] <= EXCESS_TOLERANCE:
        return 'INVOICE_SETTLED', f"Factura {inv['name']} sin saldo al aplicar el pago de ${amount:,.2f}"
    excess = round(amount - inv['amount_residual'], 2)

    # B) Contexto del Wizard (es vital para que Odoo sepa qué factura estamos pagando)
    wizard_context = {'active_model': 'account.move', 'active_ids': [inv['id']]}
    # C) Valores del Wizard (account.payment.register)
    wizard_vals = {
        'journal_id': journal['id'],
        'amount': amount,
        'payment_date': record['date_released'].strftime("%Y-%m-%d"),
        'l10n_mx_edi_payment_method_id': EDI_PAYMENT_METHOD_ID,
    }
    # D) Crear el registro del Wizard en Odoo
    wizard_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'create',
                                  [wizard_vals], {'context': wizard_context})
    # E) Ejecutar la acción del Wizard: crea el account.payment y concilia de forma nativa.
    #    Si el pago excede el saldo, el excedente queda como crédito abierto del pago.
    action = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'action_create_payments',
                               [[wizard_id]], {'context': wizard_context})
    if excess <= EXCESS_TOLERANCE:
        return 'ODOO_PAID', None

    # E2) Excedente: el asistente devuelve la acción del pago creado (res_id cuando es un solo pago)
    note = f"Excedente ${excess:,.2f} sobre el saldo de {inv['name']}"
    payment_id_odoo = action.get('res_id') if isinstance(action, dict) else None
    open_lines = []
    if payment_id_odoo:
        pay_move = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment', 'read',
                                     [[payment_id_odoo]], {'fields': ['move_id']})[0]['move_id'][0]
        open_lines = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move.line', 'search_read',
                                       [[('move_id', '=', pay_move), ('account_type', '=', 'asset_receivable'),
                                         ('reconciled', '=', False)]],
                                       {'fields': ['id', 'account_id', 'partner_id', 'amount_residual'], 'limit': 1})
    if has_nc and open_lines and abs(-open_lines[0]['amount_residual'] - excess) <= EXCESS_TOLERANCE:
        reverse_payment_excess(models, uid, journal, open_lines[0], excess, record['payment_id'], wizard_vals['payment_date'])
        return 'ODOO_PAID_NC', note + " (reversado contra la cuenta del diario MP por la NC)"
    return 'ODOO_PAID_EXCESS', note + (" (sin NC registrada: queda abierto)" if not has_nc else " (no se ubicó la línea del pago)")


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
    journal = {'id': journal_id, 'default_account_id': journal_search[0]['default_account_id']}

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
    excess_nc   = 0   # Pago mayor al saldo por una NC ya registrada: excedente reversado a la cuenta MP
    excess_open = 0   # Pago mayor al saldo sin NC registrada: queda abierto para revisión
    settled     = 0   # Factura ya saldada (p.ej. por la NC): no se crea el pago

    for record in pending_payments:
        try:
            # ¿La NC de la orden ya está registrada en Odoo? (define qué hacer con un excedente del pago)
            cursor.execute(f"SELECT COUNT(*) AS n FROM {NC_TABLE} WHERE marketplace = %s AND mkp_order_id = %s "
                           f"AND status LIKE 'ODOO_CREDITED%%'", (MKP_MARKETPLACE, record['mkp_order_id']))
            has_nc = cursor.fetchone()['n'] > 0
            status, note = apply_payment(models, uid, journal, record, has_nc)

            cursor.execute("UPDATE finance.mkp_payments_prod SET status = %s, error_log = %s, processed_at = NOW() WHERE id = %s",
                           (status, note, record['id']))
            db.commit()
            if status == 'INVOICE_SETTLED':
                settled += 1
                continue
            applied += 1
            excess_nc += status == 'ODOO_PAID_NC'
            excess_open += status == 'ODOO_PAID_EXCESS'

        except Exception as e:
            db.rollback()
            cursor.execute("UPDATE finance.mkp_payments_prod SET status = 'ERROR', error_log = %s WHERE id = %s", (str(e), record['id']))
            db.commit()
            not_applied += 1

    # ── Resumen único de la corrida ────────────────────────────
    log.info(
        f"[{MKP_MARKETPLACE}] Resumen -> Pagos pendientes: {len(pending_payments)} | "
        f"Aplicados en Odoo: {applied} (excedente reversado por NC: {excess_nc}; excedente abierto: {excess_open}) | "
        f"Factura ya saldada (INVOICE_SETTLED): {settled} | No aplicados (ERROR): {not_applied}"
    )
    cursor.close()
    db.close()


if __name__ == "__main__":
    load_ml_payments_to_odoo()
