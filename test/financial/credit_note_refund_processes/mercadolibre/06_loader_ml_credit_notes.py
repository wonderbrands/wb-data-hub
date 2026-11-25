import xmlrpc.client
import MySQLdb
import xml.etree.ElementTree as ET
import base64
import os
import sys
import time
import csv
import logging
from datetime import datetime, timedelta

# ── 06 · Loader de Notas de Crédito ML → Odoo 18 (flujo v2 de Finanzas, 9-nov; v3 10-nov) ──
# Toma las NC que el 05 dejó en staging (PENDING) y crea en Odoo la out_refund ligada a
# su factura, con el XML de ML adjunto. NO timbra (un solo emisor: Mercado Libre).
#   Clasificación (Playbook de Ingresos 5.3, igual para REFUND y DISPUTE):
#     * sin fecha efectiva en la SO (no salió del almacén) -> A Cancelación   · DEV · 402.01.003
#     * salió y la NC son unidades enteras de la factura    -> B Devolución    · DEV · 402.01.001
#     * cualquier otro caso (importe parcial)               -> C Bonificación  · BON · 402.01.002
#   Monto: el del XML al centavo (si el IVA por línea de Odoo difiere, se ajusta al del XML).
#   Costo (A/B): lo revierte Odoo al costo de la factura original (reversed_entry_id).
#     A -> 205.06.003 / 501.01.001. B -> 107.06.001 / 501.01.001 vía posición fiscal FP_B.
#   Cobro: factura con saldo -> se concilia NC <-> factura (±$0.01 contra 704.23.001).
#          factura ya cobrada -> asiento en el diario Mercado Pago, Debe 105.01.004 / Haber 102.03.001
#          por lo cobrado, con la fecha de la NC.
#   Retorno WH/DEV (Finanzas 11-nov): cada retorno validado de MeLi se reclasifica EN EL MES DEL
#          RETORNO, exista o no la NC: Debe 205.06.003 / Haber 107.06.001 por su costo, conciliado
#          contra el retorno. El crédito en 107.06.001 queda abierto hasta que entra la NC tipo B y se
#          concilia ahí; si la NC nunca llega, marca un retorno sin NC. Barrido en cada corrida.
#   Fecha contable = fecha fiscal de la NC. Si el mes está cerrado -> PERIOD_LOCKED (no se mueve).
#   Sin reembolso ni disputa en MeLi -> MANUAL_REVIEW (posible refactura).
#   Reventa (DeRemate) entra con las mismas reglas (INCLUDE_RESALE).
#   SIN sale_line_ids: la SO no se reabre a facturar.
# Idempotencia: ref 'MLNC:<UUID>' (NC), 'MLNC-COBRO:<UUID>', 'MLNC-CENT:<UUID>' y
#   'MLNC-RET:<UUID>:<retorno>' (asientos).

# Ejecución local: credenciales desde los .env locales (el primero que define una
# variable gana; lo que ya esté en env gana sobre todos). En Kestra llegan por env.
DOTENV_PATHS = os.getenv("DOTENV_PATHS", ";".join([
    r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\.env",
    r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Claude_proyects\wb-data-hub\.env",
])).split(";")
try:
    from dotenv import load_dotenv
    for _path in DOTENV_PATHS:
        if os.path.exists(_path):
            load_dotenv(_path, override=False)
except ImportError:
    pass

# ml_stores.py vive con los procesos productivos de ML (01-04) en
# processes/wonderbrands/financial/billing_processes/mercadolibre/; se reutiliza desde aquí
# para no duplicar la configuración por tienda. Primera ruta: desde la copia de processes/
# (credit_note_refund_processes/mercadolibre -> ../../billing_processes/mercadolibre).
# Segunda: desde la copia de test/ (apunta a processes/). La que no existe se ignora.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, '..', '..', 'billing_processes', 'mercadolibre'),
           os.path.join(_HERE, '..', '..', '..', '..', 'processes', 'wonderbrands', 'financial',
                        'billing_processes', 'mercadolibre')):
    if os.path.exists(os.path.join(_p, 'ml_stores.py')):
        sys.path.insert(0, os.path.abspath(_p))
        break
from ml_stores import get_store_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
#   "preflight" -> valida Odoo/BD y muestra QUÉ HARÍA con cada NC. NO escribe nada.
#   "orders"    -> registra solo las NC de las órdenes de LOCAL_ORDER_IDS.
#   "pending"   -> registra las NC PENDING (más recientes primero; filtro LOCAL_MONTH).
#   "returns"   -> solo el barrido de retornos: reclasifica cada retorno WH/DEV validado de MeLi
#                  (periodo abierto) en el mes del retorno y lo concilia con su NC tipo B si ya existe.
#   "cents"     -> una sola vez (Finanzas 24-nov): facturas con NC de MeLi que quedaron con hasta $0.15 de
#                  saldo por la regla anterior de $0.01 -> se cierran contra 704.23.001 (MLNC-CENT).
#                  Con LOCAL_DRY_RUN = True solo lista.
#   ("orders" y "pending" también corren el barrido de retornos al final; "preflight" solo lo cuenta.)
LOCAL_RUN            = "preflight"
LOCAL_ODOO           = "test"      # test | prod  (prod = Odoo productivo REAL)
LOCAL_DRY_RUN        = True        # solo para "cents": True = solo lista lo que cerraría
LOCAL_ORDER_IDS      = ""
LOCAL_MONTH          = ""          # "2026-10" = solo NC timbradas ese mes; "" = todas
LOCAL_MAX_NC_PER_RUN = 10
LOCAL_PREFLIGHT_ALL  = False       # preflight: True = TODO el universo de NC (corrida en seco), no solo la cola
LOCAL_PREFLIGHT_CSV  = True        # preflight: escribe el CSV con el estado que el proceso asignaría a cada NC
LOCAL_TABLES         = "prod"      # prod = finance.mkp_credit_notes_prod (definitiva) | staging = *_staging (pruebas)
# En Kestra el flujo manda RUN_MODE, ODOO_TARGET, ONLY_ORDER_IDS, NC_MONTH, MAX_NC_PER_RUN,
# PREFLIGHT_ALL y PREFLIGHT_CSV como variables de entorno, y esas ganan.
# ══════════════════════════════════════════════════════════════

RUN_MODE       = os.getenv("RUN_MODE", LOCAL_RUN).strip().lower()
ODOO_TARGET    = os.getenv("ODOO_TARGET", LOCAL_ODOO).strip().lower()
DRY_RUN_CENTS  = os.getenv("DRY_RUN", str(LOCAL_DRY_RUN)).strip().lower() == "true"
ONLY_ORDER_IDS = [o.strip() for o in os.getenv("ONLY_ORDER_IDS", LOCAL_ORDER_IDS if RUN_MODE != "pending" else "").split(",") if o.strip()]
NC_MONTH       = os.getenv("NC_MONTH", LOCAL_MONTH).strip()
MAX_NC_PER_RUN = int(os.getenv("MAX_NC_PER_RUN", LOCAL_MAX_NC_PER_RUN))
PREFLIGHT_ALL  = os.getenv("PREFLIGHT_ALL", str(LOCAL_PREFLIGHT_ALL)).strip().lower() == "true"
PREFLIGHT_CSV  = os.getenv("PREFLIGHT_CSV", str(LOCAL_PREFLIGHT_CSV)).strip().lower() == "true"
if RUN_MODE not in ("preflight", "orders", "pending", "returns", "cents") or ODOO_TARGET not in ("test", "prod"):
    raise ValueError(f"RUN_MODE='{RUN_MODE}' / ODOO_TARGET='{ODOO_TARGET}' inválidos")
if not all(o.isdigit() for o in ONLY_ORDER_IDS):
    raise ValueError(f"ONLY_ORDER_IDS inválidos: {ONLY_ORDER_IDS}")

if ODOO_TARGET == "test":
    ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
    ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
else:
    ODOO_URL = (os.getenv("ODOO_URL") or os.getenv("odoo_urlV18") or "").strip()
    ODOO_DB  = (os.getenv("ODOO_DB") or os.getenv("odoo_dbV18") or "").strip()
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
ODOO_TIMEOUT  = int(os.getenv("ODOO_TIMEOUT", "120"))

DB_HOST, DB_USER, DB_PASSWORD, DB_NAME = (os.getenv(k) for k in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME"))

STORE           = get_store_config()
MKP_MARKETPLACE = STORE['marketplace']
NC_TABLE        = os.getenv("NC_TABLE", "finance.mkp_credit_notes_prod" if LOCAL_TABLES == "prod" else "finance.mkp_credit_notes_staging")
NC_SCAN_TABLE   = os.getenv("NC_SCAN_TABLE", "finance.mkp_credit_notes_scan" if LOCAL_TABLES == "prod" else "finance.mkp_credit_notes_scan_staging")
if ODOO_TARGET == "test" and NC_TABLE.endswith("_prod") and RUN_MODE in ("orders", "pending"):
    raise ValueError("Odoo de PRUEBAS con la tabla definitiva: los IDs de staging ensuciarían "
                     f"{NC_TABLE}. Usa LOCAL_TABLES = 'staging' para probar en staging.")

# Configuración contable (flujo v2 de Finanzas)
CLASS_CONFIG = {   # tipo -> (diario, cuenta de ingreso)
    # Un solo diario exclusivo de MeLi para las NC (MLNC, serie MELINC). MLDEV y MLBON se archivaron (20-nov).
    'A': (os.getenv("JOURNAL_CODE_DEV", STORE.get('nc_journal_code') or "MLNC"), os.getenv("ACCOUNT_CODE_A", "402.01.003")),
    'B': (os.getenv("JOURNAL_CODE_DEV", STORE.get('nc_journal_code') or "MLNC"), os.getenv("ACCOUNT_CODE_B", "402.01.001")),
    'C': (os.getenv("JOURNAL_CODE_BON", STORE.get('nc_journal_code') or "MLNC"), os.getenv("ACCOUNT_CODE_C", "402.01.002")),
}
CLASS_DESC = {'A': 'Cancelación', 'B': 'Devolución', 'C': 'Bonificación'}
FP_B_NAME           = os.getenv("FP_B_NAME", "ML NC Devolución (tipo B)")   # 205.06.003 -> 107.06.001
ACCOUNT_CODE_FP_SRC = "205.06.003"
ACCOUNT_CODE_FP_DST = os.getenv("ACCOUNT_CODE_TRANSIT", "107.06.001")
ACCOUNT_CODE_CXC    = STORE['account_code_cxc']                              # 105.01.004
ACCOUNT_CODE_MP     = os.getenv("ACCOUNT_CODE_MP", STORE.get('nc_cobro_account') or "102.03.001")   # Oficiales: 102.03.007
ACCOUNT_CODE_CENTS  = os.getenv("ACCOUNT_CODE_CENTS", "704.23.001")
JOURNAL_CODE_MISC   = os.getenv("JOURNAL_CODE_MISC", "MISC")     # centavos y reclasificación del retorno
JOURNAL_CODE_COBRO  = os.getenv("JOURNAL_CODE_COBRO", STORE.get('mp_journal_code') or "MP")   # reverso del cobro
BON_PRODUCT_CODE    = os.getenv("BON_PRODUCT_CODE", "BON_SRV")
TAX_ID_MARKETPLACES = int(os.getenv("TAX_ID_MARKETPLACES", "38"))
# Reventa (NC a DeRemate): entra al flujo con las mismas reglas (Finanzas, 10-nov).
RFC_RESALE          = "DCM991109KR2"
INCLUDE_RESALE      = os.getenv("INCLUDE_RESALE", "true").strip().lower() == "true"
PREFIX, PREFIX_COBRO, PREFIX_CENT, PREFIX_RET = "MLNC:", "MLNC-COBRO:", "MLNC-CENT:", "MLNC-RET:"
# Retornos WH/DEV que se reclasifican (Finanzas 12-nov): solo órdenes que entraron al flujo nuevo de NC,
# es decir, las que están en la cola del 05 (NC_SCAN_TABLE: facturadas por el autofacturador de MeLi
# desde el 1-jun). Los retornos de ventas anteriores no se reclasifican (sus NC no concilian).
ML_TEAM_NAME      = os.getenv("ML_TEAM_NAME", STORE.get('team_name') or "Team_MercadoLibre")
# Nombre de la NC = serie-folio del CFDI de MeLi (MELINC-11844), pedido de Finanzas (12-nov).
# SOLO con diarios exclusivos de CFDI de terceros: Odoo numera el siguiente documento del diario a
# partir del folio más alto, así que en DEV/BON (compartidos con las NC propias) la siguiente NC
# manual saldría como MELINC-xxxxx (probado en staging). Activar junto con JOURNAL_CODE_DEV/BON
# apuntando a los diarios exclusivos de MeLi.
NC_NAME_FROM_CFDI = os.getenv("NC_NAME_FROM_CFDI", "true").strip().lower() == "true"   # MLNC es exclusivo de MeLi
# Diario de reembolsos fuera de MeLi (Finanzas, 20-nov): NC manuales de Finanzas. El flujo no las toca;
# si MeLi acredita después la misma orden, la NC de MeLi no se carga (doble egreso) y queda para Finanzas.
JOURNAL_CODE_REM  = os.getenv("JOURNAL_CODE_REM", STORE.get('rem_journal_code') or "MLREM")
RETURNS_SINCE     = os.getenv("RETURNS_SINCE", "2026-06-01")

AMOUNT_TOLERANCE  = float(os.getenv("AMOUNT_TOLERANCE", "1.0"))   # tope por factura y casado de unidades
# NC vs saldo de la factura que concilia: hasta $0.15, a favor o en contra, va a 704.23.001 (Finanzas, 24-nov;
# antes $0.01). Por encima, lo que la NC excede al saldo es reverso del cobro.
CENTS_TOLERANCE   = float(os.getenv("CENTS_TOLERANCE", "0.15")) + 0.0001
BATCH_SIZE        = int(os.getenv("BATCH_SIZE", "20"))
MAX_ERROR_RETRIES = int(os.getenv("MAX_ERROR_RETRIES", "2"))

NS = {'cfdi': 'http://www.sat.gob.mx/cfd/4'}
RETRY_STATUSES = ('PENDING', 'REFUND_DRAFT', 'ERROR_BG', 'WAIT_INVOICE_IN_ODOO', 'PERIOD_LOCKED')


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = ODOO_TIMEOUT
        return conn


class Odoo:
    def __init__(self):
        self.connect()

    def connect(self):
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", transport=TimeoutTransport())
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not self.uid:
            raise RuntimeError(f"Odoo rechazó la autenticación ({ODOO_URL}, {ODOO_DB})")
        self.models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", transport=TimeoutTransport(), allow_none=True)

    def call(self, model, method, *args, retry=True, **kwargs):
        """retry=False en create/post: un reintento a ciegas podría duplicar. Si la
        respuesta se pierde, la siguiente corrida encuentra el registro por 'ref'."""
        for attempt in range(1, 4):
            try:
                return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PASSWORD, model, method, list(args), kwargs)
            except xmlrpc.client.Fault as e:
                if "cannot marshal None" in str(e):
                    return None  # El método se ejecutó; solo no devuelve valor
                raise
            except (xmlrpc.client.ProtocolError, OSError):
                if not retry or attempt == 3:
                    raise
                time.sleep(3 * attempt)
                self.connect()

    def read(self, model, domain, fields, **kw):
        return self.call(model, 'search_read', domain, fields=fields, **kw)

    def one(self, model, rec_id, fields):
        return self.call(model, 'read', [rec_id], fields=fields)[0]


def parse_xml(xml_b64):
    """Datos del CFDI de la NC: conceptos, IVA total, uso y forma de pago."""
    root = ET.fromstring(base64.b64decode(xml_b64))
    receptor = root.find('cfdi:Receptor', NS)
    impuestos = root.find('cfdi:Impuestos', NS)
    concepts = []
    for c in root.findall('cfdi:Conceptos/cfdi:Concepto', NS):
        tr = c.find('cfdi:Impuestos/cfdi:Traslados/cfdi:Traslado', NS)
        qty, unit = float(c.get('Cantidad')), float(c.get('ValorUnitario'))
        tax = float(tr.get('Importe')) if tr is not None else 0.0
        concepts.append({'qty': qty, 'unit': unit, 'amount': float(c.get('Importe')) - float(c.get('Descuento') or 0),
                         'tax': tax, 'desc': c.get('Descripcion') or ''})
    return {
        'usage': receptor.get('UsoCFDI') if receptor is not None else None,
        'forma_pago': root.get('FormaPago'),
        'total': float(root.get('Total')),
        'tax': float(impuestos.get('TotalImpuestosTrasladados')) if impuestos is not None else 0.0,
        'concepts': concepts,
    }


def load_config(odoo):
    """IDs fijos de Odoo. Cualquier faltante es error de configuración (aborta)."""
    def one(model, domain, label):
        res = odoo.call(model, 'search', domain, limit=1)
        if not res:
            raise RuntimeError(f"No existe en Odoo: {label}")
        return res[0]
    acc = lambda code: one('account.account', [('code', '=', code)], f"cuenta {code}")
    fields = odoo.call('account.move', 'fields_get', [], attributes=['selection'])
    cfg = {
        'journals': {c: one('account.journal', [('code', '=', code), ('type', '=', 'sale')], f"diario {code}")
                     for c, (code, _) in CLASS_CONFIG.items()},
        'accounts': {c: acc(code) for c, (_, code) in CLASS_CONFIG.items()},
        'journal_misc': one('account.journal', [('code', '=', JOURNAL_CODE_MISC)], f"diario {JOURNAL_CODE_MISC}"),
        'journal_cobro': one('account.journal', [('code', '=', JOURNAL_CODE_COBRO)], f"diario {JOURNAL_CODE_COBRO}"),
        'team': one('crm.team', [('name', '=', ML_TEAM_NAME)], f"equipo de ventas {ML_TEAM_NAME}"),
        'acc_cxc': acc(ACCOUNT_CODE_CXC), 'acc_mp': acc(ACCOUNT_CODE_MP), 'acc_cents': acc(ACCOUNT_CODE_CENTS),
        'acc_fp_src': acc(ACCOUNT_CODE_FP_SRC), 'acc_fp_dst': acc(ACCOUNT_CODE_FP_DST),
        'bon_product': one('product.product', [('default_code', '=', BON_PRODUCT_CODE)], f"producto {BON_PRODUCT_CODE}"),
        'usages': {k for k, _ in fields['l10n_mx_edi_usage'].get('selection', [])},
        'pay_methods': {pm['code']: pm['id'] for pm in odoo.read('l10n_mx_edi.payment.method', [], ['code'])},
    }
    fp = odoo.call('account.fiscal.position', 'search', [('name', '=', FP_B_NAME)], limit=1)
    mapping = odoo.read('account.fiscal.position.account', [('position_id', 'in', fp)], ['account_src_id', 'account_dest_id'])
    if not any(m['account_src_id'][0] == cfg['acc_fp_src'] and m['account_dest_id'][0] == cfg['acc_fp_dst'] for m in mapping):
        if RUN_MODE != "preflight":
            raise RuntimeError(f"Falta la posición fiscal '{FP_B_NAME}' ({ACCOUNT_CODE_FP_SRC} -> {ACCOUNT_CODE_FP_DST})")
        log.warning(f"Falta la posición fiscal '{FP_B_NAME}': el preflight sigue, pero no se puede registrar ninguna NC tipo B")
    cfg['fp_b'] = fp[0] if fp else False
    rem = odoo.call('account.journal', 'search', [('code', '=', JOURNAL_CODE_REM)], limit=1)
    cfg['journal_rem'] = rem[0] if rem else False
    for code in (ACCOUNT_CODE_FP_SRC, ACCOUNT_CODE_FP_DST):   # la reclasificación del retorno concilia en ambas
        if not odoo.one('account.account', acc(code), ['reconcile'])['reconcile']:
            raise RuntimeError(f"La cuenta {code} no permite conciliación")
    company = odoo.read('res.company', [], ['fiscalyear_lock_date', 'tax_lock_date', 'sale_lock_date'])[0]
    cfg['lock_date'] = max([d for d in (company['fiscalyear_lock_date'], company['tax_lock_date'], company['sale_lock_date']) if d] or [''])
    return cfg


def refund_evidence(db, order_ids):
    """Origen del reembolso en MeLi por orden: DISPUTE / REFUND (transacciones) o pago reembolsado."""
    if not order_ids:
        return {}
    ph = ','.join(['%s'] * len(order_ids))
    cur = db.cursor()
    cur.execute(f"""SELECT order_id, GROUP_CONCAT(DISTINCT transaction_type) FROM somos_reyes.ml_financial_transactions_master
                    WHERE order_id IN ({ph}) AND transaction_type IN ('REFUND','DISPUTE','REFUND_SHIPPING','DISPUTE_SHIPPING')
                    GROUP BY order_id""", order_ids)
    ev = {str(o): t for o, t in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT order_id FROM somos_reyes.ml_order_payments WHERE order_id IN ({ph}) AND refunded_amt > 0", order_ids)
    for (o,) in cur.fetchall():
        ev.setdefault(str(o), 'PAGO REEMBOLSADO (MP)')
    cur.close()
    return ev


def load_context(odoo, db, recs):
    """Búsquedas en lote (como el 02): SO, facturas, líneas, NC existentes y movimientos de almacén."""
    refs = [r['mkp_order_id'] for r in recs]
    sos = odoo.read('sale.order', [('channel_order_reference', 'in', refs)], ['id', 'name', 'channel_order_reference', 'effective_date'])
    inv_fields = ['id', 'name', 'state', 'payment_state', 'partner_id', 'team_id', 'amount_total',
                  'amount_residual', 'l10n_mx_edi_cfdi_uuid', 'invoice_origin']
    invs = odoo.read('account.move', [('move_type', '=', 'out_invoice'),
                                      ('l10n_mx_edi_cfdi_uuid', 'in', [r['related_invoice_uuid'] for r in recs])], inv_fields)
    by_origin = odoo.read('account.move', [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                                           ('invoice_origin', 'in', [s['name'] for s in sos])],
                          ['name', 'invoice_origin', 'l10n_mx_edi_cfdi_uuid'])
    inv_ids = [i['id'] for i in invs]
    lines = odoo.read('account.move.line', [('move_id', 'in', inv_ids), ('display_type', '=', 'product')],
                      ['move_id', 'product_id', 'name', 'quantity', 'price_unit', 'price_total'])
    receivables = odoo.read('account.move.line', [('move_id', 'in', inv_ids), ('account_type', '=', 'asset_receivable')],
                            ['move_id', 'account_id'])
    keys = [PREFIX + r['cfdi_uuid'] for r in recs]
    refunds = odoo.read('account.move', [('move_type', '=', 'out_refund'), ('state', '!=', 'cancel'),
                                         '|', ('reversed_entry_id', 'in', inv_ids), ('ref', 'in', keys)],
                        ['id', 'name', 'state', 'ref', 'reversed_entry_id', 'amount_total', 'journal_id'])
    nc_uuid_moves = odoo.read('account.move', [('l10n_mx_edi_cfdi_uuid', 'in', [r['cfdi_uuid'] for r in recs])],
                              ['id', 'name', 'ref', 'l10n_mx_edi_cfdi_uuid'])
    # NC manuales de reembolsos fuera de MeLi (diario MLREM) de estas órdenes: doble egreso si MeLi también acredita
    mlrem = odoo.read('account.move', [('move_type', '=', 'out_refund'), ('state', '=', 'posted'),
                                       ('journal_id.code', '=', JOURNAL_CODE_REM),
                                       ('invoice_origin', 'in', [s['name'] for s in sos])], ['name', 'invoice_origin'])
    returns = odoo.read('stock.picking', [('sale_id', 'in', [s['id'] for s in sos]), ('state', '=', 'done'),
                                          ('location_id.usage', '=', 'customer')], ['sale_id', 'name'])   # WH/DEV
    ctx = {'so_by_ref': {s['channel_order_reference']: s for s in sos},
           'inv_by_uuid': {(i['l10n_mx_edi_cfdi_uuid'] or '').upper(): i for i in invs},
           'by_origin': {}, 'lines': {}, 'receivable_acc': {}, 'refunds': refunds, 'nc_uuid_moves': {},
           'returns': {}, 'evidence': refund_evidence(db, refs), 'mlrem': {}}
    for m in mlrem:
        ctx['mlrem'].setdefault(m['invoice_origin'], []).append(m['name'])
    for m in nc_uuid_moves:
        ctx['nc_uuid_moves'].setdefault((m['l10n_mx_edi_cfdi_uuid'] or '').upper(), []).append(m)
    for r in refunds:
        r['ref'] = r['ref'] or ''
    for i in by_origin:
        ctx['by_origin'].setdefault(i['invoice_origin'], []).append(i)
    for ln in lines:
        ctx['lines'].setdefault(ln['move_id'][0], []).append(ln)
    for ln in receivables:
        ctx['receivable_acc'][ln['move_id'][0]] = ln['account_id'][0]
    for p in returns:
        ctx['returns'].setdefault(p['sale_id'][0], []).append(p['name'])
    return ctx


def match_units(concepts, inv_lines):
    """¿La NC son unidades enteras de líneas de la factura? Devuelve [(línea, cantidad)] o None."""
    remaining = {ln['id']: ln['quantity'] for ln in inv_lines}
    matched = []
    for c in concepts:
        unit_gross = (c['amount'] + c['tax']) / c['qty'] if c['qty'] else 0
        if c['qty'] != int(c['qty']):
            return None
        hit = next((ln for ln in inv_lines if remaining[ln['id']] >= c['qty'] and ln['quantity']
                    and abs(ln['price_total'] / ln['quantity'] - unit_gross) <= 0.02 * max(1, c['qty'])), None)
        if not hit:
            return None
        remaining[hit['id']] -= c['qty']
        matched.append((hit, c))
    return matched


def plan_credit_note(rec, ctx, cfg):
    """Decide qué hacer con una NC SIN escribir nada. Devuelve dict con 'status'
    ('READY' o el estado final de rechazo) y, si READY, el encabezado a crear."""
    uuid, related, total = rec['cfdi_uuid'], rec['related_invoice_uuid'], float(rec['total_amount'])
    key = PREFIX + uuid
    so = ctx['so_by_ref'].get(rec['mkp_order_id'])
    inv = ctx['inv_by_uuid'].get(related)
    origin = ctx['evidence'].get(rec['mkp_order_id'])
    base = {'origin': origin or '-', 'so': so, 'returns': ctx['returns'].get(so['id'], []) if so else []}

    if not inv:
        other = ctx['by_origin'].get(so['name'], []) if so else []
        if other:
            return {**base, 'status': 'UUID_MISMATCH', 'error': f"Refactura: la NC relaciona {related}; en Odoo la SO tiene "
                                                                f"{[(o['name'], o['l10n_mx_edi_cfdi_uuid']) for o in other]}"}
        return {**base, 'status': 'WAIT_INVOICE_IN_ODOO', 'error': f"No hay factura en Odoo con UUID {related}"}
    base['inv'] = inv
    if inv['state'] == 'cancel':
        return {**base, 'status': 'INVOICE_CANCELLED_IN_ODOO', 'error': f"Factura {inv['name']} cancelada en Odoo"}
    if inv['state'] != 'posted':
        return {**base, 'status': 'WAIT_INVOICE_IN_ODOO', 'error': f"Factura {inv['name']} en {inv['state']}"}
    for move in ctx['nc_uuid_moves'].get(uuid, []):
        if (move['ref'] or '') != key:
            return {**base, 'status': 'ALREADY_ODOO_CREDITED', 'error': f"El UUID de la NC ya está en Odoo: {move['name']}"}
    inv_refunds = [r for r in ctx['refunds'] if r['reversed_entry_id'] and r['reversed_entry_id'][0] == inv['id']]
    # NC de MLREM: por su documento origen (la SO) o por estar ligada a la factura (botón "Nota de crédito")
    rem_ncs = set(ctx['mlrem'].get(so['name'], []) if so else [])
    rem_ncs |= {r['name'] for r in inv_refunds if r['state'] == 'posted' and cfg.get('journal_rem')
                and r['journal_id'] and r['journal_id'][0] == cfg['journal_rem']}
    if rem_ncs:
        return {**base, 'status': 'MLREM_DOUBLE_EGRESS', 'error': f"Doble egreso: la orden ya tiene NC de reembolso fuera de "
                                                                 f"MeLi {sorted(rem_ncs)} y MeLi también la acreditó. No se carga; revisa Finanzas"}
    foreign = [r for r in inv_refunds if r['state'] == 'posted' and not r['ref'].startswith(PREFIX)]
    if foreign:
        return {**base, 'status': 'MANUAL_REVIEW', 'error': f"Doble egreso: la factura ya tiene NC de Odoo {[r['name'] for r in foreign]}; "
                                                            f"se cancela la de Odoo y queda la de MeLi"}
    previous = sum(r['amount_total'] for r in inv_refunds if r['state'] == 'posted' and r['ref'] != key)
    if previous + total > inv['amount_total'] + AMOUNT_TOLERANCE:
        return {**base, 'status': 'AMOUNT_EXCEEDS_INVOICE', 'error': f"Tope: NC previas ${previous:,.2f} + esta ${total:,.2f} > "
                                                                     f"factura ${inv['amount_total']:,.2f}"}
    if not origin:
        return {**base, 'status': 'MANUAL_REVIEW', 'error': "Sin reembolso ni disputa en MeLi para la orden: posible refactura"}
    if rec.get('rfc_receptor') == RFC_RESALE and not INCLUDE_RESALE:
        return {**base, 'status': 'MANUAL_REVIEW', 'error': "Reventa: en discusión con Finanzas si entra al flujo (INCLUDE_RESALE)"}

    # ── Clasificación y líneas desde el XML ──
    xml = parse_xml(rec['xml_data'])
    shipped = bool(so and so.get('effective_date'))
    units = match_units(xml['concepts'], ctx['lines'].get(inv['id'], []))
    op_class = ('B' if shipped else 'A') if units else ('C' if shipped else 'A')
    journal_code, _ = CLASS_CONFIG[op_class]
    account = cfg['accounts'][op_class]
    base.update({'op_class': op_class, 'journal': journal_code, 'account_code': CLASS_CONFIG[op_class][1]})
    if units:
        lines = [{'product_id': ln['product_id'][0] if ln['product_id'] else False, 'name': ln['name'],
                  'quantity': c['qty'], 'price_unit': c['unit']} for ln, c in units]
    else:   # importe parcial: servicio sin costo (C bonificación; A si no hubo salida)
        prefix = 'BON_SRV' if op_class == 'C' else 'CANC_SRV'
        lines = [{'product_id': cfg['bon_product'], 'name': f"{prefix}-{c['desc']}"[:250], 'quantity': 1,
                  'price_unit': c['amount']} for c in xml['concepts']]
    for ln in lines:
        ln.update({'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])], 'account_id': account})

    subtotal = round(sum(round(ln['quantity'] * ln['price_unit'], 2) for ln in lines), 2)
    if abs(subtotal + xml['tax'] - xml['total']) > 0.005:
        return {**base, 'status': 'TOTAL_DIFF', 'error': f"Subtotal ${subtotal:,.2f} + IVA XML ${xml['tax']:,.2f} != Total XML ${xml['total']:,.2f}"}

    date = rec['fecha_timbrado'].strftime('%Y-%m-%d')
    if cfg['lock_date'] and date <= cfg['lock_date']:
        return {**base, 'status': 'PERIOD_LOCKED', 'error': f"Fecha fiscal {date} en periodo cerrado (bloqueo {cfg['lock_date']})"}

    header = {
        **({'name': f"{rec['serie']}-{rec['folio']}"} if NC_NAME_FROM_CFDI else {}),   # serie-folio del CFDI de MeLi
        'move_type': 'out_refund', 'journal_id': cfg['journals'][op_class], 'partner_id': inv['partner_id'][0],
        'team_id': inv['team_id'][0] if inv.get('team_id') else False,
        'invoice_date': date, 'date': date, 'invoice_date_due': date, 'invoice_payment_term_id': False,
        'invoice_origin': so['name'] if so else inv['invoice_origin'], 'reversed_entry_id': inv['id'],
        'ref': key, 'payment_reference': f"{inv['name']} | UUID origen {related} | NC ML {rec['serie']}-{rec['folio']}",
        'l10n_mx_edi_cfdi_origin': f"01|{related}",
        'fiscal_position_id': cfg['fp_b'] if op_class == 'B' else False,
        'invoice_line_ids': [(0, 0, ln) for ln in lines],
    }
    if xml['usage'] in cfg['usages']:
        header['l10n_mx_edi_usage'] = xml['usage']
    if xml['forma_pago'] in cfg['pay_methods']:
        header['l10n_mx_edi_payment_method_id'] = cfg['pay_methods'][xml['forma_pago']]

    inv_paid = inv['amount_total'] - inv['amount_residual']
    cobro = ('concilia' if inv['amount_residual'] >= total - CENTS_TOLERANCE else
             f'reverso {ACCOUNT_CODE_MP}' if inv['amount_residual'] < 0.01 else f'concilia + reverso {ACCOUNT_CODE_MP}')
    return {**base, 'status': 'READY', 'key': key, 'xml': xml, 'header': header, 'units': bool(units),
            'existing': next((r for r in ctx['refunds'] if r['ref'] == key), None),
            'cxc': ctx['receivable_acc'].get(inv['id'], cfg['acc_cxc']), 'cobro': cobro, 'inv_paid': inv_paid,
            'lines_desc': [f"{ln['quantity']:g} x {ln['name'][:40]} @ {ln['price_unit']:,.2f}" for ln in lines]}


def adjust_tax_to_xml(odoo, refund_id, xml_tax):
    """Si el IVA por línea de Odoo difiere del XML (±$0.01), lo iguala al del XML con el
    mismo mecanismo que usa la pantalla de Odoo al editar el impuesto (tax_totals)."""
    move = odoo.one('account.move', refund_id, ['tax_totals', 'amount_tax'])
    if abs(move['amount_tax'] - xml_tax) < 0.005:
        return False
    totals = move['tax_totals']
    groups = [g for st in totals.get('subtotals', []) for g in st.get('tax_groups', [])] or \
             [g for v in (totals.get('groups_by_subtotal') or {}).values() for g in v]
    if len(groups) != 1:
        raise RuntimeError(f"La NC tiene {len(groups)} grupos de impuestos; no se puede ajustar al XML")
    key = 'tax_amount_currency' if 'tax_amount_currency' in groups[0] else 'tax_group_amount'
    groups[0][key] = xml_tax
    odoo.call('account.move', 'write', [refund_id], {'tax_totals': totals})
    return True


def get_or_post_entry(odoo, ref, journal, date, lines):
    """Asiento MISC idempotente por ref. Devuelve id publicado."""
    found = odoo.call('account.move', 'search', [('ref', '=', ref), ('state', '!=', 'cancel')], limit=1)
    if found:
        move_id = found[0]
    else:
        move_id = odoo.call('account.move', 'create', {'move_type': 'entry', 'journal_id': journal, 'date': date, 'ref': ref,
                                                       'line_ids': [(0, 0, ln) for ln in lines]}, retry=False)
    if odoo.one('account.move', move_id, ['state'])['state'] == 'draft':
        odoo.call('account.move', 'action_post', [move_id], retry=False)
    return move_id


def reconcile_lines(odoo, move_ids, account_id):
    lines = odoo.read('account.move.line', [('move_id', 'in', move_ids), ('account_id', '=', account_id),
                                            ('reconciled', '=', False), ('parent_state', '=', 'posted')], ['id', 'move_id'])
    if len({ln['move_id'][0] for ln in lines}) >= 2:
        odoo.call('account.move.line', 'reconcile', [ln['id'] for ln in lines])


def residual(odoo, move_id, account_id):
    """Saldo con signo de la CxC del movimiento (+ deudor, - acreedor)."""
    return round(sum(ln['amount_residual'] for ln in odoo.read('account.move.line', [('move_id', '=', move_id), ('account_id', '=', account_id)],
                                                                ['amount_residual'])), 2)


def _chunks(items, size=500):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def link_returns_to_nc(odoo, cfg, so_id, so_name):
    """Concilia en 107.06.001 el costo de la NC tipo B (Debe) contra los créditos abiertos de las
    reclasificaciones de los retornos de su orden (Haber). Devuelve notas."""
    dst = cfg['acc_fp_dst']
    nc_lines = odoo.read('account.move.line', [('account_id', '=', dst), ('debit', '>', 0), ('reconciled', '=', False),
                                               ('parent_state', '=', 'posted'), ('move_id.move_type', '=', 'out_refund'),
                                               '|', ('move_id.ref', '=like', PREFIX + '%'),
                                               ('move_id.journal_id.code', '=', JOURNAL_CODE_REM),   # NC tipo B de MLREM
                                               ('move_id.invoice_origin', '=', so_name)],
                         ['id', 'amount_residual'])
    if not nc_lines:
        return []
    refs = [PREFIX_RET + p['name'] for p in odoo.read('stock.picking', [('sale_id', '=', so_id), ('state', '=', 'done'),
                                                                        ('location_id.usage', '=', 'customer')], ['name'])]
    ret_lines = odoo.read('account.move.line', [('account_id', '=', dst), ('credit', '>', 0), ('reconciled', '=', False),
                                                ('parent_state', '=', 'posted'), ('move_id.ref', 'in', refs)],
                          ['id', 'amount_residual']) if refs else []
    if not ret_lines:
        return []
    before = sum(ln['amount_residual'] for ln in nc_lines)
    odoo.call('account.move.line', 'reconcile', [ln['id'] for ln in nc_lines + ret_lines])
    after = sum(ln['amount_residual'] for ln in odoo.call('account.move.line', 'read', [ln['id'] for ln in nc_lines],
                                                          fields=['amount_residual']))
    return [f"conciliada ${before - after:,.2f} en {ACCOUNT_CODE_FP_DST} contra el retorno; abierto ${after:,.2f}"]


def in_nc_flow(db, order_refs):
    """Órdenes que entraron al flujo nuevo de NC (están en la cola del 05)."""
    found = set()
    refs = [r for r in order_refs if r]
    for ch in _chunks(refs, 1000):
        cur = db.cursor()
        cur.execute(f"SELECT mkp_order_id FROM {NC_SCAN_TABLE} WHERE marketplace = %s AND mkp_order_id IN ({','.join(['%s'] * len(ch))})",
                    (MKP_MARKETPLACE, *ch))
        found |= {str(o) for (o,) in cur.fetchall()}
        cur.close()
    return found


def sweep_returns(odoo, cfg, db, so_refs=None, dry=False, out_notes=None):
    """Retorno WH/DEV de MeLi (Finanzas, 11-nov): cada retorno validado se reclasifica en el MES DEL
    RETORNO, exista o no la NC: Debe 205.06.003 / Haber 107.06.001 por su costo, conciliado contra el
    retorno en 205.06.003. El crédito en 107.06.001 queda abierto hasta que entra la NC tipo B
    (link_returns_to_nc); si nunca llega, marca un retorno sin NC.
    Solo productos que salieron en la orden (los kits regresan por componente; un producto ajeno o
    una orden sin salida no se reclasifica). Retornos en periodo cerrado: se dejan para la reapertura.
    Idempotente: ref MLNC-RET:<retorno>. dry=True solo cuenta (preflight)."""
    src = cfg['acc_fp_src']
    dom = [('account_id', '=', src), ('credit', '>', 0), ('reconciled', '=', False), ('parent_state', '=', 'posted'),
           ('date', '>=', RETURNS_SINCE), ('move_id.stock_move_id.picking_id.location_id.usage', '=', 'customer'),
           ('move_id.stock_move_id.picking_id.sale_id.team_id', '=', cfg['team'])]
    if so_refs:
        dom.append(('move_id.stock_move_id.picking_id.sale_id.channel_order_reference', 'in', list(so_refs)))
    lines = odoo.read('account.move.line', dom, ['id', 'move_id', 'product_id', 'amount_residual', 'date'], order='date, id')
    counts = {}
    bump = lambda k, n=1: counts.__setitem__(k, counts.get(k, 0) + n)
    if not lines:
        return counts
    # retorno de cada línea: asiento de valuación -> stock.move -> picking -> SO
    smove_of = {}
    for ch in _chunks({ln['move_id'][0] for ln in lines}):
        smove_of.update({m['id']: m['stock_move_id'][0] for m in odoo.call('account.move', 'read', ch, fields=['stock_move_id'])
                         if m['stock_move_id']})
    pick_of = {}
    for ch in _chunks(set(smove_of.values())):
        pick_of.update({m['id']: m['picking_id'][0] for m in odoo.call('stock.move', 'read', ch, fields=['picking_id'])
                        if m['picking_id']})
    picks = {}
    for ch in _chunks(set(pick_of.values())):
        picks.update({p['id']: p for p in odoo.call('stock.picking', 'read', ch, fields=['name', 'sale_id'])})
    so_ids = {p['sale_id'][0] for p in picks.values() if p['sale_id']}
    sos, shipped = {}, {}
    for ch in _chunks(so_ids):
        sos.update({s['id']: s for s in odoo.call('sale.order', 'read', ch, fields=['name', 'partner_id', 'channel_order_reference'])})
        out_picks = {p['id']: p['sale_id'][0] for p in odoo.read('stock.picking', [('sale_id', 'in', ch), ('state', '=', 'done'),
                                                                                  ('location_dest_id.usage', '=', 'customer')],
                                                                 ['sale_id'])}
        for ch2 in _chunks(out_picks):
            for m in odoo.read('stock.move', [('picking_id', 'in', ch2), ('state', '=', 'done')], ['product_id', 'picking_id']):
                shipped.setdefault(out_picks[m['picking_id'][0]], set()).add(m['product_id'][0])

    in_flow = in_nc_flow(db, [s['channel_order_reference'] for s in sos.values()])
    outside = {sid for sid, so_ in sos.items() if so_['channel_order_reference'] not in in_flow}
    by_pick = {}
    for ln in lines:
        pid = pick_of.get(smove_of.get(ln['move_id'][0]))
        if pid:
            by_pick.setdefault(pid, []).append(ln)
    for pid, plines in by_pick.items():
        pick = picks[pid]
        so = sos.get(pick['sale_id'][0]) if pick['sale_id'] else None
        if not so or so['id'] in outside:
            bump('retornos fuera del flujo nuevo de NC (no se reclasifican)')
            continue
        out_products = shipped.get(so['id'], set())
        valid = [ln for ln in plines if ln['product_id'] and ln['product_id'][0] in out_products]
        if len(valid) < len(plines):
            bump('líneas sin salida en la orden (no se reclasifican)', len(plines) - len(valid))
        if not valid:
            continue
        date = max(ln['date'] for ln in valid)
        amount = round(-sum(ln['amount_residual'] for ln in valid), 2)
        period = 'periodo cerrado' if cfg['lock_date'] and date <= cfg['lock_date'] else 'periodo abierto'
        bump(f"retornos {period}")
        bump(f"$ {period}", amount)
        if dry or period == 'periodo cerrado':
            continue
        try:
            entry_lines = [{'account_id': src, 'partner_id': so['partner_id'][0], 'product_id': ln['product_id'][0],
                            'debit': round(-ln['amount_residual'], 2), 'credit': 0, 'name': f"Retorno {pick['name']} · {so['name']}"}
                           for ln in valid]
            entry_lines.append({'account_id': cfg['acc_fp_dst'], 'partner_id': so['partner_id'][0], 'debit': 0, 'credit': amount,
                                'name': f"Retorno {pick['name']} · {so['name']}"})
            entry = get_or_post_entry(odoo, PREFIX_RET + pick['name'], cfg['journal_misc'], date, entry_lines)
            mine = odoo.call('account.move.line', 'search', [('move_id', '=', entry), ('account_id', '=', src), ('reconciled', '=', False)])
            if mine:
                odoo.call('account.move.line', 'reconcile', mine + [ln['id'] for ln in valid])
            notes = link_returns_to_nc(odoo, cfg, so['id'], so['name'])
            bump('reclasificados')
            msg = (f"retorno {pick['name']} {date} ${amount:,.2f} {ACCOUNT_CODE_FP_SRC} -> {ACCOUNT_CODE_FP_DST}"
                   + (f" ({notes[0]})" if notes else " (sin NC aún: queda abierto en 107.06.001)"))
            log.info(f"[RETORNO] {so['name']} {msg}")
            if out_notes is not None:
                out_notes.append(msg)
        except Exception as e:
            bump('error')
            log.error(f"[RETORNO ERROR] {pick['name']}: {str(e)[:500]}")
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in counts.items()}


def sweep_cents(odoo, cfg, dry):
    """Facturas con NC de MeLi (ref MLNC:) que quedaron con un saldo de $0.01 a CENTS_TOLERANCE: asiento
    MLNC-CENT:<UUID> contra 704.23.001 con la fecha de la NC, conciliado con la factura. Idempotente."""
    ncs = odoo.read('account.move', [('move_type', '=', 'out_refund'), ('ref', '=like', PREFIX + '%'), ('state', '=', 'posted')],
                    ['id', 'name', 'ref', 'date', 'reversed_entry_id', 'partner_id'])
    by_inv = {n['reversed_entry_id'][0]: n for n in ncs if n['reversed_entry_id']}
    counts, rows = {}, []
    for ch in _chunks(list(by_inv), 500):
        for inv in odoo.call('account.move', 'read', ch, fields=['id', 'name', 'amount_residual', 'state']):
            res = round(inv['amount_residual'], 2)
            if inv['state'] != 'posted' or not (0.005 < res <= CENTS_TOLERANCE):
                continue
            nc = by_inv[inv['id']]
            uuid = nc['ref'][len(PREFIX):]
            if cfg['lock_date'] and nc['date'] <= cfg['lock_date']:
                counts['periodo cerrado'] = counts.get('periodo cerrado', 0) + 1
                continue
            cxc = odoo.read('account.move.line', [('move_id', '=', inv['id']), ('account_type', '=', 'asset_receivable')], ['account_id'])[0]['account_id'][0]
            msg = f"{inv['name']} saldo ${res:,.2f} tras {nc['name']} -> {ACCOUNT_CODE_CENTS}"
            if not dry:
                cent = get_or_post_entry(odoo, PREFIX_CENT + uuid, cfg['journal_misc'], nc['date'], [
                    {'account_id': cxc, 'partner_id': nc['partner_id'][0], 'debit': 0, 'credit': res, 'name': f"Centavos NC ML {uuid}"},
                    {'account_id': cfg['acc_cents'], 'partner_id': nc['partner_id'][0], 'debit': res, 'credit': 0, 'name': f"Centavos NC ML {uuid}"}])
                reconcile_lines(odoo, [inv['id'], cent], cxc)
                msg += f" ({odoo.one('account.move', cent, ['name'])['name']}; saldo ahora ${residual(odoo, inv['id'], cxc):,.2f})"
            log.info(f"[CENTAVOS] {'[SECO] ' if dry else ''}{msg}")
            counts['cerradas' if not dry else 'por cerrar'] = counts.get('cerradas' if not dry else 'por cerrar', 0) + 1
    return counts


def execute_plan(odoo, db, rec, plan, cfg):
    """Crea (o retoma), ajusta al XML, adjunta, publica, aplica el cobro y deja log."""
    inv, uuid, xml = plan['inv'], rec['cfdi_uuid'], plan['xml']
    refund_id = plan['existing']['id'] if plan['existing'] else None
    if not refund_id:
        refund_id = odoo.call('account.move', 'create', plan['header'], retry=False)
        set_status(db, rec['id'], 'REFUND_DRAFT', odoo_refund_id=refund_id, odoo_invoice_id=inv['id'])

    tax_adjusted = False
    if odoo.one('account.move', refund_id, ['state'])['state'] == 'draft':
        # 1) IVA al del XML (recalcula la línea por cobrar), 2) después la CxC a la de la factura
        tax_adjusted = adjust_tax_to_xml(odoo, refund_id, xml['tax'])
        receivable = odoo.read('account.move.line', [('move_id', '=', refund_id), ('account_type', '=', 'asset_receivable')], ['id'])
        odoo.call('account.move', 'write', [refund_id], {
            'l10n_mx_edi_cfdi_uuid': uuid, 'narration': uuid,
            'line_ids': [(1, ln['id'], {'account_id': plan['cxc']}) for ln in receivable]},
            context={'tracking_disable': True})
        draft = odoo.one('account.move', refund_id, ['amount_total', 'amount_tax'])
        bad_cxc = odoo.read('account.move.line', [('move_id', '=', refund_id), ('account_type', '=', 'asset_receivable'),
                                                  ('account_id', '!=', plan['cxc'])], ['id'])
        if bad_cxc:
            raise RuntimeError("La línea por cobrar de la NC no quedó en la CxC de la factura")
        if abs(draft['amount_total'] - xml['total']) > 0.005:
            odoo.call('account.move', 'unlink', [refund_id], retry=False)
            set_status(db, rec['id'], 'TOTAL_DIFF', odoo_refund_id=None)
            raise RuntimeError(f"TOTAL_DIFF: borrador ${draft['amount_total']:,.2f} != XML ${xml['total']:,.2f} (borrador eliminado)")
        if not odoo.call('ir.attachment', 'search', [('res_model', '=', 'account.move'), ('res_id', '=', refund_id),
                                                     ('name', '=', f"{uuid}.xml")], limit=1):
            odoo.call('ir.attachment', 'create', {'name': f"{uuid}.xml", 'datas': rec['xml_data'],
                                                  'res_model': 'account.move', 'res_id': refund_id})
        odoo.call('account.move', 'action_post', [refund_id], retry=False)

    refund = odoo.one('account.move', refund_id, ['name', 'state', 'date', 'invoice_date', 'amount_total'])
    if refund['state'] != 'posted':
        raise RuntimeError(f"La NC {refund_id} quedó en estado {refund['state']} tras action_post")
    if refund['date'] != refund['invoice_date']:
        raise RuntimeError(f"Odoo movió la fecha contable a {refund['date']} (fecha fiscal {refund['invoice_date']})")

    # ── Cobro ──
    cxc, date, partner = plan['cxc'], refund['date'], inv['partner_id'][0]
    notes = []
    # 1) concilia contra el saldo de la factura (Odoo ya lo hace al publicar una NC con
    #    reversed_entry_id si la factura tiene saldo; aquí se completa si faltó)
    if residual(odoo, inv['id'], cxc) > 0:
        reconcile_lines(odoo, [inv['id'], refund_id], cxc)
    if residual(odoo, inv['id'], cxc) < inv['amount_residual'] - 0.005:
        notes.append(f"conciliada ${inv['amount_residual'] - max(residual(odoo, inv['id'], cxc), 0):,.2f} contra la factura")
    inv_res, nc_res = residual(odoo, inv['id'], cxc), residual(odoo, refund_id, cxc)
    if 0 < inv_res <= CENTS_TOLERANCE or (0 > nc_res >= -CENTS_TOLERANCE and inv_res <= 0):   # 2) ±$0.15 -> 704.23.001
        diff, target = (inv_res, inv['id']) if inv_res > 0 else (nc_res, refund_id)
        cent = get_or_post_entry(odoo, PREFIX_CENT + uuid, cfg['journal_misc'], date, [
            {'account_id': cxc, 'partner_id': partner, 'debit': max(-diff, 0), 'credit': max(diff, 0), 'name': f"Centavos NC ML {uuid}"},
            {'account_id': cfg['acc_cents'], 'partner_id': partner, 'debit': max(diff, 0), 'credit': max(-diff, 0), 'name': f"Centavos NC ML {uuid}"}])
        reconcile_lines(odoo, [target, cent], cxc)
        notes.append(f"centavos ${abs(diff):.2f} a {ACCOUNT_CODE_CENTS}")
        nc_res = residual(odoo, refund_id, cxc)
    if nc_res < -CENTS_TOLERANCE:                                 # 3) lo ya cobrado: baja 102.03.001 (diario MP)
        amount = -nc_res
        cobro = get_or_post_entry(odoo, PREFIX_COBRO + uuid, cfg['journal_cobro'], date, [
            {'account_id': cxc, 'partner_id': partner, 'debit': amount, 'credit': 0, 'name': f"Reverso cobro NC ML {rec['serie']}-{rec['folio']}"},
            {'account_id': cfg['acc_mp'], 'partner_id': partner, 'debit': 0, 'credit': amount, 'name': f"Reverso cobro NC ML {rec['serie']}-{rec['folio']}"}])
        reconcile_lines(odoo, [refund_id, cobro], cxc)
        notes.append(f"reverso de cobro ${amount:,.2f} en {ACCOUNT_CODE_MP}")
    status = 'ODOO_CREDITED' if abs(residual(odoo, refund_id, cxc)) < 0.005 else 'ODOO_CREDITED_CHECK'
    if plan['op_class'] == 'B' and plan['so']:                    # 4) retorno WH/DEV ya reclasificado -> concilia 107.06.001
        notes += link_returns_to_nc(odoo, cfg, plan['so']['id'], plan['so']['name'])   # retornos reclasificados antes
        sweep_returns(odoo, cfg, db, [rec['mkp_order_id']], out_notes=notes)                # retornos aún sin reclasificar

    body = (f"Nota de crédito ML {refund['name']} · NC {rec['serie']}-{rec['folio']} · UUID {uuid} · "
            f"${refund['amount_total']:,.2f} · Tipo {plan['op_class']} ({CLASS_DESC[plan['op_class']]}, {plan['account_code']}) · "
            f"Origen {plan['origin']} · Factura {inv['name']} (UUID {inv['l10n_mx_edi_cfdi_uuid']}) · Cobro: {', '.join(notes) or '-'}")
    targets = [('account.move', inv['id'])] + ([('sale.order', plan['so']['id'])] if plan['so'] else [])
    for model, res_id in targets:
        if not odoo.call('mail.message', 'search_count', [('model', '=', model), ('res_id', '=', res_id), ('body', 'ilike', uuid)]):
            odoo.call(model, 'message_post', [res_id], body=body, message_type='comment', subtype_xmlid='mail.mt_note')

    return status, {'odoo_refund_id': refund_id, 'odoo_refund_name': refund['name'], 'odoo_invoice_id': inv['id'],
                    'odoo_so_name': plan['so']['name'] if plan['so'] else None, 'operation_class': plan['op_class'],
                    'invoice_payment_state': inv['payment_state']}, refund, notes, tax_adjusted


def set_status(db, rec_id, status, error_log=None, bump_retry=False, **fields):
    sets = ["status = %s", "error_log = %s", "processed_at = UTC_TIMESTAMP()"] + [f"{k} = %s" for k in fields]
    if bump_retry:
        sets.append("retry_count_loader = IFNULL(retry_count_loader, 0) + 1")
    cur = db.cursor()
    cur.execute(f"UPDATE {NC_TABLE} SET {', '.join(sets)} WHERE id = %s", (status, error_log, *fields.values(), rec_id))
    db.commit()
    cur.close()


def fetch_records(db, limit, exclude_ids):
    if RUN_MODE == "preflight" and PREFLIGHT_ALL:   # corrida en seco: todo el universo de NC
        where, params = ["marketplace = %s"], [MKP_MARKETPLACE]
    else:
        where = ["marketplace = %s", f"(status IN {RETRY_STATUSES} OR (status = 'ERROR' AND IFNULL(retry_count_loader, 0) < %s))"]
        params = [MKP_MARKETPLACE, MAX_ERROR_RETRIES]
    if ONLY_ORDER_IDS:
        where.append(f"mkp_order_id IN ({','.join(['%s'] * len(ONLY_ORDER_IDS))})")
        params += ONLY_ORDER_IDS
    if NC_MONTH:
        where.append("DATE_FORMAT(fecha_timbrado, '%%Y-%%m') = %s")
        params.append(NC_MONTH)
    if exclude_ids:
        where.append(f"id NOT IN ({','.join(['%s'] * len(exclude_ids))})")
        params += list(exclude_ids)
    cur = db.cursor(MySQLdb.cursors.DictCursor)
    cur.execute(f"""SELECT id, mkp_order_id, cfdi_uuid, related_invoice_uuid, serie, folio, subtotal, tax_amount, total_amount,
                           fecha_timbrado, xml_data, nc_scope, status, rfc_receptor
                    FROM {NC_TABLE} WHERE {' AND '.join(where)}
                    ORDER BY fecha_timbrado DESC, id LIMIT %s""", (*params, limit))
    rows = cur.fetchall()
    cur.close()
    for r in rows:
        r['cfdi_uuid'] = r['cfdi_uuid'].strip().upper()
        r['related_invoice_uuid'] = (r['related_invoice_uuid'] or '').strip().upper()
    return rows


def classify_error(e):
    msg = str(e)
    if msg.startswith("TOTAL_DIFF"):
        return 'TOTAL_DIFF', False
    if any(s in msg.lower() for s in ("lock date", "fecha de bloqueo", "periodo bloqueado", "locked", "movió la fecha")):
        return 'PERIOD_LOCKED', False
    if isinstance(e, (xmlrpc.client.ProtocolError, OSError)) or "502" in msg or "bad gateway" in msg.lower():
        return 'ERROR_BG', False
    return 'ERROR', True


STATUS_LABEL = {   # estado del proceso, en el lenguaje del listado para Finanzas
    'READY': 'Para registrar', 'PERIOD_LOCKED': 'Periodo cerrado', 'MANUAL_REVIEW': 'Revisión manual',
    'WAIT_INVOICE_IN_ODOO': 'Espera factura en Odoo', 'UUID_MISMATCH': 'Refactura (relaciona otra factura)',
    'AMOUNT_EXCEEDS_INVOICE': 'Tope (excede la factura)', 'INVOICE_CANCELLED_IN_ODOO': 'Factura cancelada en Odoo',
    'ALREADY_ODOO_CREDITED': 'UUID ya en Odoo (otra NC)', 'TOTAL_DIFF': 'Diferencia de total', 'ERROR': 'Error al planear',
    'REGISTERED': 'Ya registrada', 'MLREM_DOUBLE_EGRESS': 'Doble egreso con NC fuera de MeLi (MLREM)'}
CSV_COLUMNS = ['Orden ML', 'Tipo de orden', 'NC Mercado Libre', 'UUID NC', 'Fecha NC (fiscal)', 'Subtotal', 'IVA', 'Total',
               'UUID factura relacionada', 'Estado proceso', 'Código', 'Detalle', 'Tipo NC', 'Diario', 'Cuenta',
               'SO Odoo', 'Factura Odoo', 'Factura cobrada', 'Saldo factura', 'Cobro', 'Retorno WH/DEV validado',
               'Origen en MeLi', 'NC Odoo']


def csv_row(rec, plan):
    inv, so = plan.get('inv'), plan.get('so')
    status = 'REGISTERED' if plan['status'] == 'READY' and plan.get('existing') and plan['existing']['state'] == 'posted' \
        else plan['status']
    paid = '' if not inv else {'paid': 'Sí', 'in_payment': 'Sí', 'partial': 'Parcial', 'not_paid': 'No',
                               'reversed': 'Revertida'}.get(inv['payment_state'], inv['payment_state'])
    return {'Orden ML': rec['mkp_order_id'], 'Tipo de orden': 'Reventa' if rec.get('rfc_receptor') == RFC_RESALE else 'Regular',
            'NC Mercado Libre': f"{rec['serie']}-{rec['folio']}", 'UUID NC': rec['cfdi_uuid'],
            'Fecha NC (fiscal)': rec['fecha_timbrado'].strftime('%Y-%m-%d'), 'Subtotal': float(rec['subtotal'] or 0),
            'IVA': float(rec['tax_amount'] or 0), 'Total': float(rec['total_amount']), 'UUID factura relacionada': rec['related_invoice_uuid'],
            'Estado proceso': STATUS_LABEL.get(status, status), 'Código': status, 'Detalle': plan.get('error') or '',
            'Tipo NC': f"{plan['op_class']} · {CLASS_DESC[plan['op_class']]}" if plan.get('op_class') else '',
            'Diario': plan.get('journal', ''), 'Cuenta': plan.get('account_code', ''),
            'SO Odoo': so['name'] if so else '', 'Factura Odoo': inv['name'] if inv else '', 'Factura cobrada': paid,
            'Saldo factura': inv['amount_residual'] if inv else '', 'Cobro': plan.get('cobro', '') if status == 'READY' else '',
            'Retorno WH/DEV validado': ', '.join(plan.get('returns') or []), 'Origen en MeLi': plan.get('origin', ''),
            'NC Odoo': plan['existing']['name'] if plan.get('existing') else ''}


def main():
    log.info(f"=== 06 Loader NC ML | modo={RUN_MODE} | Odoo={ODOO_TARGET.upper()} {ODOO_URL} ({ODOO_DB}) | "
             f"órdenes={ONLY_ORDER_IDS or ('TODO el universo' if PREFLIGHT_ALL and RUN_MODE == 'preflight' else 'cola PENDING')} | "
             f"mes={NC_MONTH or 'todos'} | tope={MAX_NC_PER_RUN} ===")
    if ODOO_TARGET == "prod" and RUN_MODE != "preflight" and not (RUN_MODE == "cents" and DRY_RUN_CENTS):
        log.warning("*** ESCRIBIENDO EN ODOO PRODUCTIVO ***")
    missing = [k for k, v in {"ODOO_URL": ODOO_URL, "ODOO_DB": ODOO_DB, "ODOO_USER": ODOO_USER, "ODOO_PASSWORD": ODOO_PASSWORD,
                              "DB_HOST": DB_HOST, "DB_USER": DB_USER, "DB_PASSWORD": DB_PASSWORD, "DB_NAME": DB_NAME}.items() if not v]
    if missing:
        log.error(f"Faltan variables: {missing}")
        raise SystemExit(1)

    db = MySQLdb.connect(host=DB_HOST, user=DB_USER, passwd=DB_PASSWORD, db=DB_NAME, charset='utf8mb4')
    odoo = Odoo()
    cfg = load_config(odoo)
    log.info(f"Odoo OK (uid {odoo.uid}) | diarios {cfg['journals']} | cuentas {cfg['accounts']} | posición fiscal B {cfg['fp_b']} | "
             f"bloqueo hasta {cfg['lock_date'] or '-'}")

    if RUN_MODE == "cents":
        counts = sweep_cents(odoo, cfg, dry=DRY_RUN_CENTS)
        db.close()
        log.info(f"=== Resumen centavos de NC previas -> {counts} ===")
        return

    if RUN_MODE == "returns":
        counts = sweep_returns(odoo, cfg, db, ONLY_ORDER_IDS or None)
        db.close()
        log.info(f"=== Resumen barrido de retornos -> {counts} ===")
        return

    if RUN_MODE != "preflight":
        cur = db.cursor()
        cur.execute(f"UPDATE {NC_TABLE} SET status = 'PENDING' WHERE marketplace = %s AND status = 'PROCESSING'", (MKP_MARKETPLACE,))
        db.commit()
        cur.close()

    csv_path, csv_file, writer = None, None, None
    if RUN_MODE == "preflight" and PREFLIGHT_CSV:
        csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas",
                                f"Corrida_en_seco_NC_ML_{ODOO_TARGET}_{datetime.now():%Y-%m-%d_%H%M}.csv")
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)   # finanzas/ no está en el repo
        csv_file = open(csv_path, "w", newline="", encoding="utf-8-sig")
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()

    counts, done_ids, processed = {}, set(), 0
    bump = lambda k: counts.__setitem__(k, counts.get(k, 0) + 1)
    while processed < MAX_NC_PER_RUN:
        recs = fetch_records(db, min(BATCH_SIZE, MAX_NC_PER_RUN - processed), done_ids)
        if not recs:
            break
        done_ids.update(r['id'] for r in recs)
        processed += len(recs)
        ctx = load_context(odoo, db, recs)

        for rec in recs:
            tag = f"{rec['mkp_order_id']} NC {rec['serie']}-{rec['folio']} ${float(rec['total_amount']):,.2f}"
            try:
                plan = plan_credit_note(rec, ctx, cfg)
            except Exception as e:
                plan = {'status': 'ERROR', 'error': f"Al planear: {e}"}

            if RUN_MODE == "preflight":
                if writer:
                    writer.writerow(csv_row(rec, plan))
                if plan['status'] == 'READY':
                    inv = plan['inv']
                    log.info(f"[PLAN] {tag} -> {'RETOMA ' + plan['existing']['name'] if plan['existing'] else 'CREA'} "
                             f"{plan['journal']} tipo {plan['op_class']} {plan['account_code']} | origen {plan['origin']} | "
                             f"factura {inv['name']} ({inv['payment_state']}, saldo ${inv['amount_residual']:,.2f}) -> {plan['cobro']} | "
                             f"fecha {plan['header']['date']} | {plan['lines_desc']}"
                             + (f" | retorno {plan['returns']}" if plan['returns'] else ""))
                    bump(f"READY {plan['op_class']}")
                else:
                    log.info(f"[PLAN] {tag} -> {plan['status']}: {plan.get('error')}")
                    bump(plan['status'])
                continue

            if plan['status'] != 'READY':
                set_status(db, rec['id'], plan['status'], plan.get('error'), bump_retry=plan['status'] == 'ERROR')
                bump(plan['status'])
                continue
            try:
                set_status(db, rec['id'], 'PROCESSING')
                status, fields, refund, notes, tax_adj = execute_plan(odoo, db, rec, plan, cfg)
                set_status(db, rec['id'], status, None, **fields)
                log.info(f"[OK] {tag} -> {refund['name']} {plan['journal']} tipo {plan['op_class']} {plan['account_code']} | {status} | "
                         f"{', '.join(notes)}{' | IVA ajustado al XML' if tax_adj else ''}")
                bump(status)
            except Exception as e:
                status, retry = classify_error(e)
                set_status(db, rec['id'], status, str(e)[:2000], bump_retry=retry)
                log.error(f"[{status}] {tag}: {str(e)[:500]}")
                bump(status)

    # retornos WH/DEV: en preflight solo se cuentan; en orders/pending se reclasifican
    counts['retornos'] = sweep_returns(odoo, cfg, db, ONLY_ORDER_IDS or None, dry=RUN_MODE == "preflight")
    if csv_file:
        csv_file.close()
        log.info(f"CSV de la corrida en seco: {csv_path}")
    db.close()
    log.info(f"=== Resumen ({'preflight: NO se escribió nada' if RUN_MODE == 'preflight' else 'escrito'}) -> "
             f"NC tomadas: {processed} | {counts} ===")


if __name__ == "__main__":
    main()
