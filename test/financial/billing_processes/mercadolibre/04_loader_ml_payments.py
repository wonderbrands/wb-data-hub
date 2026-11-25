import xmlrpc.client
import mysql.connector
import os
import logging
import sys
from collections import Counter
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
# Definidas en flows/wonderbrands/financial/billing_processes/mercadolibre/ml_payments_flow.yml
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
# Diferencias de hasta $0.15 entre el pago y el saldo, a favor o en contra, se saldan contra esta cuenta
# (Finanzas, 24-nov; antes $0.01). Mismo criterio que Amazon.
ACCOUNT_CODE_CENTS       = os.getenv("ACCOUNT_CODE_CENTS", "704.23.001")
CENTS_TOLERANCE          = float(os.getenv("CENTS_TOLERANCE", "0.15")) + 0.0001
# Una factura se da por saldada solo con menos de $0.01 de saldo: un saldo de centavos todavía recibe su pago.
SETTLED_TOLERANCE        = 0.0101
# Los pagos en ERROR (502, timeout, factura que llegó después, ...) se reintentan en cada corrida.
# Antes de crear se busca si el pago ya existe en Odoo: un 502 no dice si Odoo hizo commit.
RETRY_ERRORS             = os.getenv("RETRY_ERRORS", "true").strip().lower() == "true"
# Corrida dirigida: solo estos payment_id (separados por coma). Vacío = todos los pendientes.
ONLY_PAYMENT_IDS         = [p.strip() for p in os.getenv("ONLY_PAYMENT_IDS", "").split(",") if p.strip()]
# Corrida en seco: no escribe en Odoo ni en MySQL; deja en el log qué haría con cada pago.
DRY_RUN                  = os.getenv("DRY_RUN", "false").strip().lower() == "true"
# Periodo cerrado: los pagos con fecha de liberación en o antes de la fecha de bloqueo de Odoo no se
# aplican; se quedan como están y entran solos cuando Finanzas abra el periodo (reapertura).
# RELEASED_SINCE (opcional) acota además a pagos liberados desde esa fecha.
RELEASED_SINCE           = os.getenv("RELEASED_SINCE", "").strip()


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """Transporte personalizado para forzar un timeout en XML-RPC."""
    def __init__(self, timeout=ODOO_TIMEOUT, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


def find_invoice(models, uid, so_name, cfdi_uuid):
    """Factura publicada de la SO; si no aparece por invoice_origin, por el UUID del CFDI.
    Devuelve (factura, None) o (None, status)."""
    opts = {'fields': ['id', 'name', 'amount_residual'], 'limit': 2}
    inv = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search_read',
                            [[('invoice_origin', '=', so_name), ('move_type', '=', 'out_invoice'),
                              ('state', '=', 'posted')]], opts)
    if not inv and cfdi_uuid:
        inv = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search_read',
                                [[('l10n_mx_edi_cfdi_uuid', '=ilike', cfdi_uuid), ('move_type', '=', 'out_invoice'),
                                  ('state', '=', 'posted')]], opts)
    if len(inv) == 1:
        return inv[0], None
    if len(inv) > 1:
        return None, 'MULTIPLE_INVOICES'
    # Factura global (invoice_origin con varias SO): fuera del alcance de este flujo (Finanzas, 21-nov)
    glob = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search',
                             [[('invoice_origin', 'ilike', so_name), ('move_type', '=', 'out_invoice'),
                               ('state', '=', 'posted')]], {'limit': 1})
    return None, ('GLOBAL_INVOICE' if glob else 'INVOICE_MISSING')


def existing_payment(models, uid, memo_field, journal_id, inv_id, amount, date, payment_id):
    """¿El pago ya está en Odoo? Por el memo 'MP <payment_id>' (pagos creados desde el 23-nov) o, en los
    anteriores, por diario + fecha + monto y conciliado contra esta factura."""
    memo = f"MP {payment_id}"
    cands = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment', 'search_read',
                              [[('journal_id', '=', journal_id), ('state', '!=', 'canceled'), '|',
                                (memo_field, '=', memo),
                                '&', ('date', '=', date), '&', ('amount', '>=', amount - CENTS_TOLERANCE),
                                ('amount', '<=', amount + CENTS_TOLERANCE)]],
                              {'fields': ['id', memo_field, 'reconciled_invoice_ids'], 'limit': 500})
    for c in cands:
        if c.get(memo_field) == memo or inv_id in c.get('reconciled_invoice_ids', []):
            return c['id']
    return None


def apply_payment(models, uid, ctx_cfg, record):
    """Aplica un cobro de MP a la factura de su SO. Devuelve (status, nota).
    Tope al saldo (Finanzas, 21-nov): nunca se aplica más que el saldo de la factura. Lo que sobra
    (envío pagado por el comprador, pago posterior a una NC, ...) queda sin aplicar y se reporta.
    Diferencia de hasta $0.15 entre pago y saldo: se salda contra 704.23.001."""
    inv, missing = find_invoice(models, uid, record['odoo_so_name'], record.get('cfdi_uuid'))
    if missing:
        return missing, f"{record['odoo_so_name']} (UUID {record.get('cfdi_uuid') or '-'})"
    amount = float(record['amount'])
    date = record['date_released'].strftime("%Y-%m-%d")

    dup = existing_payment(models, uid, ctx_cfg['memo_field'], ctx_cfg['journal_id'], inv['id'], amount, date,
                           record['payment_id'])
    if dup:
        return 'ODOO_PAID', f"Ya estaba en Odoo (account.payment {dup}) en {inv['name']}"

    residual = inv['amount_residual']
    if residual <= SETTLED_TOLERANCE:
        return 'INVOICE_SETTLED', f"Factura {inv['name']} sin saldo al aplicar el pago de ${amount:,.2f}"

    vals = {
        'journal_id': ctx_cfg['journal_id'],
        'amount': amount,
        'payment_date': date,
        'l10n_mx_edi_payment_method_id': EDI_PAYMENT_METHOD_ID,
        'communication': f"MP {record['payment_id']}",
    }
    status, note = 'ODOO_PAID', None
    diff = round(amount - residual, 2)
    if abs(diff) <= CENTS_TOLERANCE and diff != 0:
        # Centavos: se aplica el pago completo y la diferencia se salda contra 704.23.001
        vals.update({'payment_difference_handling': 'reconcile', 'writeoff_account_id': ctx_cfg['acc_cents'],
                     'writeoff_label': 'Redondeo cobro ML'})
        note = f"Centavos {diff:+.2f} a {ACCOUNT_CODE_CENTS} en {inv['name']}"
    elif diff > CENTS_TOLERANCE:
        vals['amount'] = round(residual, 2)
        status = 'ODOO_PAID_CAPPED'
        note = f"Tope al saldo: aplicado ${residual:,.2f} de ${amount:,.2f} a {inv['name']}; sin aplicar ${diff:,.2f}"

    if DRY_RUN:
        return status, f"[SECO] {inv['name']} saldo ${residual:,.2f}: " + (note or f"aplicaría ${amount:,.2f}")

    ctx = {'active_model': 'account.move', 'active_ids': [inv['id']]}
    wizard_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'create',
                                  [vals], {'context': ctx})
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment.register', 'action_create_payments',
                          [[wizard_id]], {'context': ctx})
    except xmlrpc.client.Fault as e:
        if "cannot marshal None" not in str(e):   # Odoo sí creó el pago; solo falla al responder
            raise
    return status, note


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

    statuses = ['PENDING', 'ERROR'] if RETRY_ERRORS else ['PENDING']
    where, params = [f"p.status IN ({','.join(['%s'] * len(statuses))})"], [MKP_MARKETPLACE, *statuses]
    if ONLY_PAYMENT_IDS:
        where.append(f"p.payment_id IN ({','.join(['%s'] * len(ONLY_PAYMENT_IDS))})")
        params += ONLY_PAYMENT_IDS
    if RELEASED_SINCE:
        where.append("p.date_released >= %s")
        params.append(RELEASED_SINCE)
    cursor.execute(f"""
        SELECT p.*, b.odoo_so_name, b.cfdi_uuid
        FROM finance.mkp_payments_prod p
        JOIN finance.mkp_billing_prod b
          ON b.marketplace = p.marketplace
         AND b.mkp_order_id = p.mkp_order_id
        WHERE p.marketplace = %s AND {' AND '.join(where)}
        ORDER BY p.date_released
    """, params)
    pending_payments = cursor.fetchall()
    log.info(f"[{MKP_MARKETPLACE}] Pagos por aplicar en Odoo: {len(pending_payments)} "
             f"(reintento de ERROR: {sum(1 for p in pending_payments if p['status'] == 'ERROR')})"
             + (" — CORRIDA EN SECO" if DRY_RUN else ""))

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
        acc_cents = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.account', 'search',
                                      [[('code', '=', ACCOUNT_CODE_CENTS)]], {'limit': 1})
        # Odoo 18 guarda la referencia del pago en 'memo' (antes 'ref')
        pay_fields = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'account.payment', 'fields_get',
                                       [], {'attributes': ['type']})
        memo_field = 'memo' if 'memo' in pay_fields else 'ref'
        comp_fields = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.company', 'fields_get',
                                        [], {'attributes': ['type']})
        lock_fields = [f for f in ('fiscalyear_lock_date', 'tax_lock_date', 'sale_lock_date', 'hard_lock_date')
                       if f in comp_fields]
        company = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.company', 'search_read',
                                    [[]], {'fields': lock_fields, 'limit': 1})[0]
        lock_date = max([company[f] for f in lock_fields if company.get(f)] or [''])
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
    if not acc_cents:
        log.error(f"No existe la cuenta de redondeo {ACCOUNT_CODE_CENTS} en Odoo.")
        cursor.close()
        db.close()
        raise SystemExit(1)

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

    ctx_cfg = {'journal_id': journal_search[0]['id'], 'acc_cents': acc_cents[0], 'memo_field': memo_field}

    # ── 3. Aplicar (los detalles por pago van a la tabla; al log solo el resumen) ──
    log.info(f"Fecha de bloqueo en Odoo: {lock_date or 'ninguna'} (los pagos liberados hasta esa fecha esperan la reapertura)")
    counts, amounts = Counter(), Counter()
    for record in pending_payments:
        if lock_date and record['date_released'].strftime("%Y-%m-%d") <= lock_date:
            counts['PERIODO_CERRADO (sin tocar)'] += 1
            amounts['PERIODO_CERRADO (sin tocar)'] += float(record['amount'])
            continue
        try:
            status, note = apply_payment(models, uid, ctx_cfg, record)
        except Exception as e:
            status, note = 'ERROR', str(e)[:2000]
            if not DRY_RUN:
                db.rollback()
        counts[status] += 1
        amounts[status] += float(record['amount'])
        if DRY_RUN:
            log.info(f"[SECO] pago {record['payment_id']} orden {record['mkp_order_id']} "
                     f"${float(record['amount']):,.2f} -> {status} | {note or ''}")
            continue
        prev = record.get('error_log') or ''
        if prev.startswith('Carrito:'):   # marca del 03: la orden real del pago de carrito se conserva
            note = prev.split(' | ')[0] + (f" | {note}" if note else '')
        cursor.execute("UPDATE finance.mkp_payments_prod SET status = %s, error_log = %s, processed_at = NOW() "
                       "WHERE id = %s", (status, note, record['id']))
        db.commit()

    # ── Resumen único de la corrida ────────────────────────────
    log.info(f"[{MKP_MARKETPLACE}] Resumen{' (EN SECO)' if DRY_RUN else ''} -> pagos: {len(pending_payments)} | " +
             " | ".join(f"{s}: {n} (${amounts[s]:,.2f})" for s, n in counts.most_common()))
    log.info("  ODOO_PAID_CAPPED = tope al saldo (lo que sobra no se aplica) | INVOICE_SETTLED = factura ya pagada, "
             "no se aplica | GLOBAL_INVOICE = factura global, fuera del flujo | INVOICE_MISSING = la SO no tiene "
             "factura publicada en Odoo (revisar) | MULTIPLE_INVOICES = más de una factura para la SO (revisar)")
    cursor.close()
    db.close()


if __name__ == "__main__":
    load_ml_payments_to_odoo()
