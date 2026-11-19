"""
15_lote_prueba_auditoria_nov.py
-------------------------------
LOTE DE PRUEBA DE LOS ARREGLOS DE LA AUDITORIA DE NOVIEMBRE -- SOLO STAGING

Prueba, contra el espejo de Odoo y las tablas finance.*_staging, lo que cambia
en la rama fix/amazon-auditoria-nov (incluye el PR #20):

  A  REAPERTURA_B2C_04     factura B2C cancelada sin reemplazo -> la orden se
                           reabre y se reemite con l10n_mx_edi_cfdi_origin
                           '04|<UUID cancelado>'
  B  REAPERTURA_BUSINESS   factura a publico general de una Business cancelada
                           (portal / Finanzas) -> se vincula el CFDI de Amazon
  C  SPLIT_REEVALUADO      B2B_SPLIT_MISMATCH ya no es final: se reevalua y se
                           crea una factura por CFDI de Amazon
  D  BUSINESS_SIN_CFDI     Business sin CFDI de Amazon -> B2B_SIN_CFDI, no se
                           factura a publico general (PR #20)
  E1 GRACIA_DENTRO         B2C comprada hace < 6 dias -> SKIPPED_GRACE (PR #20)
  E2 GRACIA_FUERA          B2C comprada hace >= 6 dias -> sale de gracia
  F  REAPLICAR_COBRO       pago PAMZ cancelado -> el cobro regresa a PENDING y
                           se reaplica sin duplicar el AUTOPROMO
  G  TOLERANCIA_015        cobro con diferencia de 0.03 a 0.15 -> pagada, la
                           diferencia a 704.23.001 (PR #20)
  H  SIN_SO                ordenes cobradas sin SO -> fila NO_SO_IN_ODOO
  I  ALERTA_SAT            la consulta de CFDI cancelado con asiento publicado
                           corre sin error

Pasos: elige los casos (lecturas), prepara el espejo (cancela facturas o pagos
con el mismo rodeo de 06_reset_lote_test.py y siembra filas en *_staging),
corre 01 (con AMZ_ORDER_IDS = las ordenes del lote) y 03, y escribe el CSV
con lo esperado contra lo obtenido.

ESCRIBE en el espejo de Odoo y en finance.*_staging. El candado exige
AMZ_ENTORNO=staging y una URL de pruebas; no tiene variable que lo desactive.

Uso (PowerShell, desde esta carpeta; credenciales en el entorno):
    $env:AMZ_ENTORNO='staging'
    $env:ODOO_TEST_URL='https://somosreyes-test-13-10-2026-41883942.dev.odoo.com'
    $env:ODOO_TEST_DB='somosreyes-test-13-10-2026-41883942'
    ..\\..\\..\\..\\.venv\\Scripts\\python.exe 15_lote_prueba_auditoria_nov.py

    LOTE_SOLO_PLAN=1  elige los casos y escribe el CSV del plan sin tocar nada.
"""

import os
import sys
import csv
import json
import logging
import subprocess
import importlib.util
import xmlrpc.client
from datetime import datetime, timedelta

# 01 se importa como modulo (helpers y sembrar_sin_so). Su ventana se fija aqui,
# antes de importarlo: desde el 1-oct y sin tope superior.
os.environ['AMZ_START_DATE'] = os.getenv('LOTE_SIN_SO_DESDE', '2026-10-01')
os.environ['AMZ_END_DATE'] = ''
os.environ.pop('AMZ_ORDER_IDS', None)
os.environ.pop('AMZ_SETTLEMENT_ID', None)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (  # noqa: E402
    ENTORNO, ODOO_URL, ODOO_DB, TBL_BILLING, TBL_PAYMENTS, MARKETPLACE,
    validar_entorno, get_db_connection, OdooModelProxy, ejecutar_sin_retorno,
    find_journal_by_account, ACCOUNT_LIBERACIONES, a_float,
)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('lote_nov')

SOLO_PLAN = os.getenv('LOTE_SOLO_PLAN', '0') == '1'
# LOTE_CASOS=A,B corre solo esos casos (vacio = todos, A-I). Sirve para repetir
# un caso sin volver a provocar los que ya pasaron.
SOLO_CASOS = {c.strip().upper() for c in os.getenv('LOTE_CASOS', '').split(',') if c.strip()}
TODOS = ('A', 'B', 'C', 'D', 'E1', 'E2', 'F', 'G', 'H', 'I')
ACTIVOS = [c for c in TODOS if not SOLO_CASOS or c in SOLO_CASOS]
TS = datetime.now().strftime('%Y%m%d_%H%M')
SALIDA = os.path.join(_dir, 'lote_auditoria_nov')
CSV_PATH = os.path.join(SALIDA, f'lote_auditoria_nov_{TS}.csv')
PREFIJO_MEMO = 'AMZ '

# Se llenan en main() leyendo el espejo:
#  CANDADO     fecha de cierre contable de la compania. Odoo no deja crear ni
#              modificar asientos con fecha <= esa (Global / Sales Lock Date).
#              Ningun caso puede tocar un documento de un periodo cerrado.
#  ULTIMA_SO / FIN_ESPEJO  ultima SO de la copia y su fecha (la copia se
#              refresca; no se asume una fecha fija).
CANDADO = ''
ULTIMA_SO = 'SO99999999'
FIN_ESPEJO = '2999-12-31'


def cargar_01():
    spec = importlib.util.spec_from_file_location(
        'amz_billing_mod', os.path.join(_dir, '01_amz_billing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def candado():
    if ENTORNO != 'staging':
        raise Exception(f"Este lote solo corre con AMZ_ENTORNO='staging' (actual: '{ENTORNO}').")
    validar_entorno()
    if 'test' not in ODOO_URL.lower() and '.dev.odoo.com' not in ODOO_URL.lower():
        raise Exception(f"ABORTADO: {ODOO_URL} no parece de pruebas.")


def q(db, sql, params=()):
    cur = db.cursor(dictionary=True)
    cur.execute(sql, params)
    filas = cur.fetchall()
    cur.close()
    return filas


def ex(db, sql, params=()):
    cur = db.cursor()
    cur.execute(sql, params)
    n = cur.rowcount
    db.commit()
    cur.close()
    return n


# =============================================================================
# LECTURAS DE ODOO
# =============================================================================
CAMPOS_FACT = ['id', 'name', 'state', 'payment_state', 'amount_total',
               'amount_residual', 'invoice_origin', 'ref', 'l10n_mx_edi_cfdi_uuid',
               'l10n_mx_edi_cfdi_origin', 'create_date']


def facturas_vivas(odoo, so_name):
    return odoo.execute_kw(
        'account.move', 'search_read',
        [['&', ('move_type', 'in', ['out_invoice', 'out_refund']), ('state', '!=', 'cancel'),
          '|', ('invoice_origin', '=', so_name), ('ref', 'like', f'AUTOINV:{so_name}%')]],
        {'fields': CAMPOS_FACT + ['move_type'], 'order': 'id asc'})


def so_existe(odoo, so_name):
    return bool(odoo.execute_kw('sale.order', 'search_count', [[('name', '=', so_name)]]))


def leer_candado(odoo):
    """Fecha de cierre mas reciente de las companias (global, ventas, impuestos,
    candado duro). Se toma la mayor: es la que manda."""
    campos = odoo.execute_kw('res.company', 'fields_get', [], {'attributes': ['type']})
    nombres = [c for c in ('fiscalyear_lock_date', 'sale_lock_date', 'tax_lock_date',
                           'hard_lock_date') if c in campos]
    fechas = []
    for comp in odoo.execute_kw('res.company', 'search_read', [[]], {'fields': nombres}):
        fechas += [str(comp[c]) for c in nombres if comp.get(c)]
    return max(fechas) if fechas else ''


def abiertos(odoo, modelo, ids):
    """True si ningun documento tiene fecha contable en el periodo cerrado."""
    if not ids or not CANDADO:
        return True
    filas = odoo.execute_kw(modelo, 'read', [ids], {'fields': ['date']})
    return all(f['date'] and str(f['date']) > CANDADO for f in filas)


def fila(db, order_id, tabla=None):
    r = q(db, f"SELECT * FROM {tabla or TBL_BILLING} WHERE marketplace=%s AND mkp_order_id=%s",
          (MARKETPLACE, order_id))
    return r[0] if r else None


# =============================================================================
# CAMBIOS EN EL ESPEJO (mismo rodeo que 06_reset_lote_test.py)
# =============================================================================

def cancelar_factura(odoo, move_id):
    """Desconcilia y cancela. Las facturas del espejo vienen timbradas y la
    localizacion bloquea button_draft: si el boton no la mueve, write directo
    (solo en el espejo; en produccion es un tramite ante el SAT)."""
    lineas = odoo.execute_kw(
        'account.move.line', 'search_read',
        [[('move_id', '=', move_id), '|', ('matched_debit_ids', '!=', False),
          ('matched_credit_ids', '!=', False)]], {'fields': ['id']})
    if lineas:
        ejecutar_sin_retorno(odoo, 'account.move.line', 'remove_move_reconcile',
                             [x['id'] for x in lineas])
    metodo = []
    for destino, boton in (('draft', 'button_draft'), ('cancel', 'button_cancel')):
        try:
            ejecutar_sin_retorno(odoo, 'account.move', boton, [move_id])
        except xmlrpc.client.Fault:
            pass
        estado = odoo.execute_kw('account.move', 'read', [[move_id]], {'fields': ['state']})[0]['state']
        if estado != destino:
            odoo.execute_kw('account.move', 'write', [[move_id], {'state': destino}])
            metodo.append('forzado')
        else:
            metodo.append('nativo')
    return '/'.join(metodo)


def cancelar_pago(odoo, pago_id):
    for boton in ('action_draft', 'action_cancel'):
        ejecutar_sin_retorno(odoo, 'account.payment', boton, [pago_id])
    return odoo.execute_kw('account.payment', 'read', [[pago_id]], {'fields': ['state']})[0]['state']


def campo_memo(odoo):
    campos = odoo.execute_kw('account.payment', 'fields_get', [], {'attributes': ['type']})
    return 'memo' if 'memo' in campos else 'ref'


def pagos_por_memo(odoo, journal_id, memo_campo, texto, vivos=True):
    estado = ('state', 'not in', ['canceled', 'cancel']) if vivos else ('state', 'in', ['canceled', 'cancel'])
    return odoo.execute_kw('account.payment', 'search_read',
                           [[('journal_id', '=', journal_id), (memo_campo, '=', texto), estado]],
                           {'fields': ['id', 'name', 'state', 'amount']})


def promos_vivas(odoo, payment_id):
    return odoo.execute_kw('account.move', 'search_count',
                           [[('ref', '=', f"AUTOPROMO:{payment_id}"), ('state', '!=', 'cancel')]])


# =============================================================================
# SELECCION DE CASOS
# =============================================================================

def elegir_a(db, odoo, mod):
    """B2C ya facturada en el espejo, timbrada (con UUID), sin cobro ni NC."""
    filas = q(db, f"""
        SELECT b.mkp_order_id, b.odoo_so_name, b.status FROM {TBL_BILLING} b
        WHERE b.marketplace=%s AND b.status IN ('ALREADY_ODOO_INVOICED','ODOO_INVOICED')
          AND JSON_UNQUOTE(JSON_EXTRACT(b.raw_json,'$.log_conciliacion.tipo'))='B2C_PUBLICO_GENERAL'
          AND NOT EXISTS (SELECT 1 FROM {TBL_PAYMENTS} p WHERE p.marketplace=b.marketplace
                          AND p.mkp_order_id=b.mkp_order_id)
          AND NOT EXISTS (SELECT 1 FROM somos_reyes.amazon_orders o
                          WHERE o.AmazonOrderId=b.mkp_order_id AND o.IsBusinessOrder=1)
        ORDER BY b.id DESC LIMIT 60""", (MARKETPLACE,))
    for f in filas:
        so = mod.buscar_so(odoo, f['odoo_so_name'])
        if not so:
            continue
        vivas = mod.facturas_existentes(odoo, f['odoo_so_name'], so)
        refunds = [x for x in facturas_vivas(odoo, f['odoo_so_name']) if x['move_type'] == 'out_refund']
        if (len(vivas) == 1 and not refunds and vivas[0]['l10n_mx_edi_cfdi_uuid']
                and vivas[0]['payment_state'] == 'not_paid'
                and (vivas[0]['invoice_origin'] or '') == f['odoo_so_name']
                and abiertos(odoo, 'account.move', [vivas[0]['id']])):
            return {**f, 'cancelar': vivas, 'uuid_previo': vivas[0]['l10n_mx_edi_cfdi_uuid']}
    return None


def elegir_b(db, odoo, mod):
    """Business con un CFDI de Amazon, facturada a publico general en el espejo."""
    filas = q(db, f"""
        SELECT b.mkp_order_id, b.odoo_so_name, b.status,
               JSON_UNQUOTE(JSON_EXTRACT(b.raw_json,'$.cfdi_amazon_business.uuid')) uuid_amz
        FROM {TBL_BILLING} b
        WHERE b.marketplace=%s AND b.status IN ('ALREADY_ODOO_INVOICED','ODOO_INVOICED')
          AND JSON_UNQUOTE(JSON_EXTRACT(b.raw_json,'$.log_conciliacion.tipo'))='B2B'
          AND JSON_EXTRACT(b.raw_json,'$.cfdi_amazon_business.uuid') IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM {TBL_PAYMENTS} p WHERE p.marketplace=b.marketplace
                          AND p.mkp_order_id=b.mkp_order_id)
          AND (SELECT COUNT(DISTINCT i.uuid) FROM somos_reyes.amz_b2b_invoices i
               WHERE i.order_id=b.mkp_order_id AND i.receipt_type='I') = 1
          -- la factura vinculada lleva la fecha del CFDI de Amazon: debe caer
          -- en periodo abierto
          AND (SELECT MIN(DATE(i.date)) FROM somos_reyes.amz_b2b_invoices i
               WHERE i.order_id=b.mkp_order_id AND i.receipt_type='I') > %s
        ORDER BY b.id DESC LIMIT 40""", (MARKETPLACE, CANDADO or '1900-01-01'))
    for f in filas:
        so = mod.buscar_so(odoo, f['odoo_so_name'])
        if not so:
            continue
        vivas = mod.facturas_existentes(odoo, f['odoo_so_name'], so)
        todas = facturas_vivas(odoo, f['odoo_so_name'])
        if (vivas and all(v['payment_state'] == 'not_paid' for v in vivas)
                and all(',' not in (v['invoice_origin'] or '') for v in vivas)
                and not any(x['move_type'] == 'out_refund' for x in todas)
                and abiertos(odoo, 'account.move', [v['id'] for v in vivas])):
            return {**f, 'cancelar': vivas}
    return None


def elegir_c(db, odoo, mod):
    """Business con 2+ CFDI de Amazon, todos en periodo abierto y anteriores a la
    copia; SO en el espejo, sin cobro aplicado."""
    filas = q(db, """
        SELECT i.order_id mkp_order_id, COUNT(DISTINCT i.uuid) n_cfdi
        FROM somos_reyes.amz_b2b_invoices i
        WHERE i.receipt_type='I' AND i.date < %s
        GROUP BY i.order_id
        HAVING n_cfdi BETWEEN 2 AND 5 AND MIN(DATE(i.date)) > %s
        ORDER BY n_cfdi, i.order_id""", (FIN_ESPEJO, CANDADO or '1900-01-01'))
    for f in filas:
        so_r = q(db, "SELECT order_id FROM somos_reyes.odoo_master_txns_c "
                     "WHERE marketplace_order_id=%s AND order_id IS NOT NULL LIMIT 1",
                 (f['mkp_order_id'],))
        if not so_r:
            continue
        so_name = so_r[0]['order_id']
        so = mod.buscar_so(odoo, so_name)
        if not so:
            continue
        vivas = mod.facturas_existentes(odoo, so_name, so)
        if (all(v['payment_state'] == 'not_paid' for v in vivas)
                and all(',' not in (v['invoice_origin'] or '') for v in vivas)
                and abiertos(odoo, 'account.move', [v['id'] for v in vivas])):
            return {**f, 'odoo_so_name': so_name, 'cancelar': vivas}
    return None


# --- A y B desde produccion -------------------------------------------------
# Las filas de *_staging sin cobro son casi todas de septiembre (periodo
# cerrado). Se toma una orden real de produccion facturada en oct-nov cuya
# factura sigue SIN PAGAR en la copia, y se copia su fila a staging antes de
# cancelar (preparar). Produccion solo se lee.

def candidatas_sin_pagar(odoo, sos):
    """De 'sos', las que tienen en la copia una factura publicada, sin pagar,
    timbrada y en periodo abierto (filtro barato antes del detalle)."""
    hay = set()
    for i in range(0, len(sos), 200):
        dominio = [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                   ('payment_state', '=', 'not_paid'), ('invoice_origin', 'in', sos[i:i + 200])]
        if CANDADO:
            dominio.append(('date', '>', CANDADO))
        for m in odoo.execute_kw('account.move', 'search_read', [dominio],
                                 {'fields': ['invoice_origin']}):
            hay.add(m['invoice_origin'])
    return [s for s in sos if s in hay]


def elegir_a_prod(db, odoo, mod):
    """B2C de produccion (no Business) con una sola factura viva sin pagar en la copia."""
    filas = q(db, f"""
        SELECT b.mkp_order_id, b.odoo_so_name, b.status FROM finance.mkp_billing_prod b
        WHERE b.marketplace=%s AND b.status IN ('ALREADY_ODOO_INVOICED','ODOO_INVOICED')
          AND JSON_UNQUOTE(JSON_EXTRACT(b.raw_json,'$.log_conciliacion.tipo'))='B2C_PUBLICO_GENERAL'
          AND b.odoo_so_name <= %s
          AND NOT EXISTS (SELECT 1 FROM {TBL_BILLING} s WHERE s.marketplace=b.marketplace
                          AND s.mkp_order_id=b.mkp_order_id)
          AND NOT EXISTS (SELECT 1 FROM {TBL_PAYMENTS} p WHERE p.marketplace=b.marketplace
                          AND p.mkp_order_id=b.mkp_order_id)
          AND NOT EXISTS (SELECT 1 FROM somos_reyes.amazon_orders o
                          WHERE o.AmazonOrderId=b.mkp_order_id AND o.IsBusinessOrder=1)
        ORDER BY b.id DESC LIMIT 400""", (MARKETPLACE, ULTIMA_SO))
    por_so = {f['odoo_so_name']: f for f in filas}
    for so_name in candidatas_sin_pagar(odoo, list(por_so))[:40]:
        f = por_so[so_name]
        so = mod.buscar_so(odoo, so_name)
        if not so:
            continue
        vivas = mod.facturas_existentes(odoo, so_name, so)
        refunds = [x for x in facturas_vivas(odoo, so_name) if x['move_type'] == 'out_refund']
        if (len(vivas) == 1 and not refunds and vivas[0]['l10n_mx_edi_cfdi_uuid']
                and vivas[0]['payment_state'] == 'not_paid'
                and (vivas[0]['invoice_origin'] or '') == so_name
                and abiertos(odoo, 'account.move', [vivas[0]['id']])):
            return {**f, 'cancelar': vivas, 'uuid_previo': vivas[0]['l10n_mx_edi_cfdi_uuid'],
                    'desde_prod': True}
    return None


def elegir_b_prod(db, odoo, mod):
    """Business de produccion facturada a publico general (es lo de la hoja A6:
    en produccion queda como B2C o en B2B_FACTURADA_PG), con un solo CFDI de
    Amazon en periodo abierto y la factura sin pagar en la copia."""
    filas = q(db, f"""
        SELECT b.mkp_order_id, b.odoo_so_name, b.status, x.uuid_amz
        FROM finance.mkp_billing_prod b
        JOIN (SELECT order_id, MIN(uuid) uuid_amz, COUNT(DISTINCT uuid) n, MIN(DATE(date)) f
              FROM somos_reyes.amz_b2b_invoices WHERE receipt_type='I' GROUP BY order_id) x
          ON x.order_id=b.mkp_order_id
        WHERE b.marketplace=%s
          AND b.status IN ('ALREADY_ODOO_INVOICED','ODOO_INVOICED','B2B_FACTURADA_PG')
          AND x.n=1 AND x.f > %s AND b.odoo_so_name <= %s
          AND NOT EXISTS (SELECT 1 FROM {TBL_BILLING} s WHERE s.marketplace=b.marketplace
                          AND s.mkp_order_id=b.mkp_order_id)
          AND NOT EXISTS (SELECT 1 FROM {TBL_PAYMENTS} p WHERE p.marketplace=b.marketplace
                          AND p.mkp_order_id=b.mkp_order_id)
        ORDER BY b.id DESC LIMIT 400""", (MARKETPLACE, CANDADO or '1900-01-01', ULTIMA_SO))
    por_so = {f['odoo_so_name']: f for f in filas}
    for so_name in candidatas_sin_pagar(odoo, list(por_so))[:40]:
        f = por_so[so_name]
        so = mod.buscar_so(odoo, so_name)
        if not so:
            continue
        vivas = mod.facturas_existentes(odoo, so_name, so)
        todas = facturas_vivas(odoo, so_name)
        if (vivas and all(v['payment_state'] == 'not_paid' for v in vivas)
                and all(',' not in (v['invoice_origin'] or '') for v in vivas)
                and not any(x['move_type'] == 'out_refund' for x in todas)
                and abiertos(odoo, 'account.move', [v['id'] for v in vivas])):
            return {**f, 'cancelar': vivas, 'desde_prod': True}
    return None


def copiar_de_prod(db, order_id):
    """Copia la fila de facturacion de produccion a staging (columnas explicitas)."""
    return ex(db, f"""INSERT IGNORE INTO {TBL_BILLING}
                   (marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                    xml_data, raw_json, status, retry_count, retry_count_loader, error_log,
                    processed_at)
               SELECT marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                      xml_data, raw_json, status, retry_count, retry_count_loader, error_log,
                      processed_at
               FROM finance.mkp_billing_prod WHERE marketplace=%s AND mkp_order_id=%s""",
              (MARKETPLACE, order_id))


def ordenes_con_cobro(db, ids):
    """De 'ids', las que tienen Order Payment liberado (una sola lectura de
    amz_transactions: no tiene indice por order_id)."""
    if not ids:
        return set()
    filas = q(db, f"""SELECT DISTINCT order_id FROM somos_reyes.amz_transactions
                      WHERE description='Order Payment' AND transaction_status <> 'DEFERRED'
                        AND order_id IN ({', '.join(['%s'] * len(ids))})""", tuple(ids))
    return {f['order_id'] for f in filas}


def so_de(db, order_id):
    r = q(db, "SELECT order_id FROM somos_reyes.odoo_master_txns_c "
              "WHERE marketplace_order_id=%s AND order_id IS NOT NULL LIMIT 1", (order_id,))
    return r[0]['order_id'] if r else None


def elegir_d(db):
    """Business de octubre sin CFDI de Amazon, cobrada, con SO, sin fila en staging."""
    cands = q(db, f"""
        SELECT o.AmazonOrderId mkp_order_id, MIN(o.PurchaseDate) compra
        FROM somos_reyes.amazon_orders o
        WHERE o.IsBusinessOrder=1 AND o.PurchaseDate >= '2026-10-01' AND o.PurchaseDate < '2026-10-11'
          AND NOT EXISTS (SELECT 1 FROM somos_reyes.amz_b2b_invoices i WHERE i.order_id=o.AmazonOrderId)
          AND NOT EXISTS (SELECT 1 FROM {TBL_BILLING} b WHERE b.marketplace=%s
                          AND b.mkp_order_id=o.AmazonOrderId)
        GROUP BY o.AmazonOrderId ORDER BY compra LIMIT 40""", (MARKETPLACE,))
    cobradas = ordenes_con_cobro(db, [c['mkp_order_id'] for c in cands])
    for c in cands:
        if c['mkp_order_id'] in cobradas:
            so = so_de(db, c['mkp_order_id'])
            if so:
                return {**c, 'odoo_so_name': so}
    return None


def elegir_gracia(db, dias_min, dias_max):
    """B2C comprada hace entre dias_min y dias_max dias, cobrada, con SO."""
    hoy = datetime.utcnow()
    cands = q(db, f"""
        SELECT o.AmazonOrderId mkp_order_id, MIN(o.PurchaseDate) compra
        FROM somos_reyes.amazon_orders o
        WHERE IFNULL(o.IsBusinessOrder,0)=0
          AND o.PurchaseDate >= %s AND o.PurchaseDate < %s
          AND NOT EXISTS (SELECT 1 FROM somos_reyes.amz_b2b_invoices i WHERE i.order_id=o.AmazonOrderId)
          AND NOT EXISTS (SELECT 1 FROM {TBL_BILLING} b WHERE b.marketplace=%s
                          AND b.mkp_order_id=o.AmazonOrderId)
        GROUP BY o.AmazonOrderId ORDER BY compra DESC LIMIT 60""",
              ((hoy - timedelta(days=dias_max)).strftime('%Y-%m-%d %H:%M:%S'),
               (hoy - timedelta(days=dias_min)).strftime('%Y-%m-%d %H:%M:%S'), MARKETPLACE))
    cobradas = ordenes_con_cobro(db, [c['mkp_order_id'] for c in cands])
    for c in cands:
        if c['mkp_order_id'] in cobradas:
            so = so_de(db, c['mkp_order_id'])
            if so:
                return {**c, 'odoo_so_name': so}
    return None


def elegir_f(db, odoo, journal_id, memo_campo):
    """Cobro aplicado en el espejo por un lote anterior, con un solo PAMZ vivo.
    Primero los que llevan promocion asentada (para ver que no se duplica)."""
    filas = q(db, f"""
        SELECT p.mkp_order_id, p.payment_id, p.amount, b.odoo_so_name,
               (p.error_log LIKE 'EXCEDENTE_PROMO_ASENTADO%%') con_promo
        FROM {TBL_PAYMENTS} p
        JOIN {TBL_BILLING} b ON b.marketplace=p.marketplace AND b.mkp_order_id=p.mkp_order_id
        WHERE p.marketplace=%s AND p.status='ODOO_PAID' AND b.status='ODOO_INVOICED'
          AND (SELECT COUNT(*) FROM {TBL_PAYMENTS} x WHERE x.marketplace=p.marketplace
               AND x.mkp_order_id=p.mkp_order_id) = 1
        ORDER BY con_promo DESC, p.id DESC LIMIT 40""", (MARKETPLACE,))
    for f in filas:
        settlement, orden = f['payment_id'].split(':', 1)
        memo = f"{PREFIJO_MEMO}{settlement} / {orden}"
        pagos = pagos_por_memo(odoo, journal_id, memo_campo, memo)
        if len(pagos) == 1 and abiertos(odoo, 'account.payment', [pagos[0]['id']]):
            return {**f, 'memo': memo, 'pago': pagos[0],
                    'promos_antes': promos_vivas(odoo, f['payment_id'])}
    return None


def elegir_g(db, odoo):
    """Cobro de produccion de una B2C facturada antes del 13-oct (la factura esta
    en el espejo sin pagar) con diferencia de 0.03 a 0.15 contra el saldo."""
    filas = q(db, f"""
        SELECT p.mkp_order_id, p.payment_id, p.amount, p.date_released, b.odoo_so_name
        FROM finance.mkp_payments_prod p
        JOIN finance.mkp_billing_prod b ON b.marketplace=p.marketplace AND b.mkp_order_id=p.mkp_order_id
        WHERE p.marketplace=%s AND p.status='ODOO_PAID' AND b.status='ODOO_INVOICED'
          AND JSON_UNQUOTE(JSON_EXTRACT(b.raw_json,'$.log_conciliacion.tipo'))='B2C_PUBLICO_GENERAL'
          AND (SELECT COUNT(*) FROM finance.mkp_payments_prod x WHERE x.marketplace=p.marketplace
               AND x.mkp_order_id=p.mkp_order_id) = 1
          AND NOT EXISTS (SELECT 1 FROM {TBL_BILLING} s WHERE s.marketplace=p.marketplace
                          AND s.mkp_order_id=p.mkp_order_id)
          AND b.odoo_so_name <= %s
        ORDER BY p.id DESC LIMIT 400""", (MARKETPLACE, ULTIMA_SO))
    por_so = {f['odoo_so_name']: f for f in filas}
    sos = list(por_so)
    for i in range(0, len(sos), 100):
        facts = odoo.execute_kw(
            'account.move', 'search_read',
            [[('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
              ('invoice_origin', 'in', sos[i:i + 100])]],
            {'fields': ['invoice_origin', 'amount_residual', 'amount_total', 'payment_state', 'name']})
        cuenta = {}
        for x in facts:
            cuenta[x['invoice_origin']] = cuenta.get(x['invoice_origin'], 0) + 1
        for x in facts:
            f = por_so[x['invoice_origin']]
            dif = round(abs(a_float(f['amount']) - a_float(x['amount_residual'])), 2)
            if (cuenta[x['invoice_origin']] == 1 and x['payment_state'] == 'not_paid'
                    and 0.02 < dif <= 0.15):
                return {**f, 'factura': x['name'], 'dif': dif}
    return None


# =============================================================================
# PREPARACION
# =============================================================================

def preparar(db, odoo, casos):
    notas = {}
    for clave in ('A', 'B', 'C'):
        c = casos.get(clave)
        if not c:
            continue
        hechos = []
        if c.get('desde_prod'):
            copiar_de_prod(db, c['mkp_order_id'])
            hechos.append(f"fila copiada de produccion ({c['status']})")
        try:
            for v in c['cancelar']:
                hechos.append(f"{v['name']} [{cancelar_factura(odoo, v['id'])}]")
        except xmlrpc.client.Fault as e:
            # Un caso que no se puede preparar se reporta y no tumba el lote.
            log.error(f"Caso {clave}: no se pudo preparar ({e.faultString[:200]})")
            notas[clave] = (f"NO SE PUDO PREPARAR: {e.faultString[:200]}"
                            f"{' | ya canceladas: ' + ', '.join(hechos) if hechos else ''}")
            casos[clave] = None
            continue
        notas[clave] = ('Cancelada(s) en el espejo: ' + ', '.join(hechos)) if hechos else 'Sin factura que cancelar'
    if casos.get('C'):
        c = casos['C']
        raw = json.dumps({'log_conciliacion': {'amazon_order_id': c['mkp_order_id'],
                                               'odoo_so': c['odoo_so_name'], 'tipo': 'B2B'},
                          'generado_por': '15_lote_prueba_auditoria_nov.py'})
        ex(db, f"""INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, odoo_so_name, raw_json,
                       status, retry_count, retry_count_loader, error_log)
                   VALUES (%s,%s,%s,%s,'B2B_SPLIT_MISMATCH',0,0,'Sembrado por el lote de prueba')
                   ON DUPLICATE KEY UPDATE status='B2B_SPLIT_MISMATCH', retry_count_loader=0,
                       error_log='Sembrado por el lote de prueba'""",
           (MARKETPLACE, c['mkp_order_id'], c['odoo_so_name'], raw))
        notas['C'] = notas.get('C', '') + ' | fila sembrada en B2B_SPLIT_MISMATCH'
    if casos.get('F'):
        c = casos['F']
        estado = cancelar_pago(odoo, c['pago']['id'])
        notas['F'] = f"{c['pago']['name']} cancelado en el espejo (estado {estado})"
    if casos.get('G'):
        c = casos['G']
        ex(db, f"""INSERT IGNORE INTO {TBL_BILLING}
                       (marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                        xml_data, raw_json, status, retry_count, retry_count_loader, error_log,
                        processed_at)
                   SELECT marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                          xml_data, raw_json, status, retry_count, retry_count_loader, error_log,
                          processed_at
                   FROM finance.mkp_billing_prod WHERE marketplace=%s AND mkp_order_id=%s""",
           (MARKETPLACE, c['mkp_order_id']))
        ex(db, f"""INSERT IGNORE INTO {TBL_PAYMENTS} (marketplace, payment_id, mkp_order_id, amount,
                       date_released, status)
                   VALUES (%s,%s,%s,%s,%s,'PENDING')""",
           (MARKETPLACE, c['payment_id'], c['mkp_order_id'], c['amount'], c['date_released']))
        notas['G'] = (f"Fila de facturacion y cobro copiados de produccion (cobro en PENDING); "
                      f"{c['factura']} sin pagar en el espejo, diferencia {c['dif']:.2f}")
    return notas


# =============================================================================
# EJECUCION
# =============================================================================

def correr(script, extra_env, log_path):
    env = dict(os.environ)
    env.update(extra_env)
    log.info(f"Corriendo {script} {extra_env} -> {log_path}")
    with open(log_path, 'w', encoding='utf-8') as fh:
        r = subprocess.run([sys.executable, os.path.join(_dir, script)], env=env,
                           stdout=fh, stderr=subprocess.STDOUT, cwd=_dir)
    log.info(f"{script} termino con codigo {r.returncode}")
    return r.returncode


# =============================================================================
# EVALUACION
# =============================================================================

def evaluar(db, odoo, casos, notas, extra, memo_campo, journal_id):
    filas = []

    def agregar(caso, que, c, esperado, obtenido, ok, detalle=''):
        filas.append({'caso': caso, 'que_prueba': que,
                      'orden': (c or {}).get('mkp_order_id', ''),
                      'so': (c or {}).get('odoo_so_name', ''),
                      'preparacion': notas.get(caso[:1] if caso[:1] in 'ABCFG' else caso, ''),
                      'esperado': esperado, 'obtenido': obtenido,
                      'resultado': 'SIN_CANDIDATO' if c is None else ('OK' if ok else 'FALLA'),
                      'detalle': detalle})

    c = casos.get('A')
    if c:
        r = fila(db, c['mkp_order_id'])
        nuevas = [v for v in facturas_vivas(odoo, c['odoo_so_name']) if v['move_type'] == 'out_invoice']
        origen = nuevas[-1]['l10n_mx_edi_cfdi_origin'] if nuevas else None
        ok = (r['status'] == 'ODOO_INVOICED' and len(nuevas) == 1
              and (origen or '').upper() == f"04|{c['uuid_previo']}".upper())
        agregar('A', 'Factura B2C cancelada sin reemplazo: se reabre y se reemite con sustitucion 04', c,
                f"ODOO_INVOICED; 1 factura nueva con origen 04|{c['uuid_previo']}",
                f"{r['status']}; {', '.join(v['name'] for v in nuevas) or 'sin factura'}; origen {origen}",
                ok, (r['error_log'] or '')[:200])
    else:
        agregar('A', 'Reapertura B2C con sustitucion 04', None, '', '', False, notas.get('A', ''))

    c = casos.get('B')
    if c:
        r = fila(db, c['mkp_order_id'])
        nuevas = [v for v in facturas_vivas(odoo, c['odoo_so_name']) if v['move_type'] == 'out_invoice']
        uuids = [(v['l10n_mx_edi_cfdi_uuid'] or '').upper() for v in nuevas]
        ok = r['status'] == 'ODOO_B2B_LINKED' and uuids == [c['uuid_amz'].upper()]
        agregar('B', 'Business con factura a publico general cancelada: se vincula el CFDI de Amazon', c,
                f"ODOO_B2B_LINKED; 1 factura con UUID {c['uuid_amz']}",
                f"{r['status']}; {', '.join(f'{v['name']} {v['l10n_mx_edi_cfdi_uuid']}' for v in nuevas) or 'sin factura'}",
                ok, (r['error_log'] or '')[:200])
    else:
        agregar('B', 'Reapertura Business', None, '', '', False, notas.get('B', ''))

    c = casos.get('C')
    if c:
        r = fila(db, c['mkp_order_id'])
        nuevas = [v for v in facturas_vivas(odoo, c['odoo_so_name']) if v['move_type'] == 'out_invoice']
        ok = r['status'] == 'ODOO_B2B_LINKED' and len(nuevas) == c['n_cfdi']
        agregar('C', 'B2B_SPLIT_MISMATCH se reevalua y se vincula una factura por CFDI', c,
                f"ODOO_B2B_LINKED; {c['n_cfdi']} facturas (una por CFDI de Amazon)",
                f"{r['status']}; {len(nuevas)} factura(s): {', '.join(v['name'] for v in nuevas)}",
                ok, (r['error_log'] or '')[:200])
    else:
        agregar('C', 'Split reevaluado', None, '', '', False, notas.get('C', ''))

    c = casos.get('D')
    if c:
        r = fila(db, c['mkp_order_id'])
        despues = len([v for v in facturas_vivas(odoo, c['odoo_so_name']) if v['move_type'] == 'out_invoice'])
        ok = r and r['status'] == 'B2B_SIN_CFDI' and despues == c['facturas_antes']
        agregar('D', 'Business sin CFDI de Amazon: no se factura a publico general', c,
                f"B2B_SIN_CFDI; facturas de la SO sin cambio ({c['facturas_antes']})",
                f"{r['status'] if r else 'sin fila'}; facturas de la SO: {despues}", ok,
                f"compra {c['compra']}")
    else:
        agregar('D', 'Business sin CFDI', None, '', '', False)

    for clave, que, esperado in (
            ('E1', 'Gracia: B2C comprada hace menos de 6 dias', 'SKIPPED_GRACE'),
            ('E2', 'Gracia: B2C comprada hace 6 dias o mas',
             'fuera de gracia (ODOO_INVOICED, o NO_SO_IN_ODOO si la SO no esta en la copia)')):
        c = casos.get(clave)
        if c:
            r = fila(db, c['mkp_order_id'])
            st = r['status'] if r else 'sin fila'
            ok = (st == 'SKIPPED_GRACE') if clave == 'E1' else (st not in ('SKIPPED_GRACE', 'sin fila'))
            agregar(clave, que, c, esperado, st, ok, f"compra {c['compra']} UTC")
        else:
            agregar(clave, que, None, esperado, '', False)

    c = casos.get('F')
    if c:
        r = fila(db, c['mkp_order_id'], TBL_PAYMENTS)
        vivos = pagos_por_memo(odoo, journal_id, memo_campo, c['memo'])
        promos = promos_vivas(odoo, c['payment_id'])
        fact = odoo.execute_kw('account.move', 'search_read',
                               [[('invoice_origin', '=', c['odoo_so_name']), ('move_type', '=', 'out_invoice'),
                                 ('state', '=', 'posted')]], {'fields': ['name', 'payment_state']})
        ok = (r['status'] == 'ODOO_PAID' and len(vivos) == 1 and vivos[0]['id'] != c['pago']['id']
              and promos == c['promos_antes']
              and all(x['payment_state'] in ('paid', 'in_payment') for x in fact))
        agregar('F', 'Pago PAMZ cancelado: el cobro se reaplica solo, sin duplicar AUTOPROMO', c,
                f"ODOO_PAID; 1 pago nuevo con memo '{c['memo']}'; AUTOPROMO vivos = {c['promos_antes']}; factura pagada",
                f"{r['status']}; pagos vivos: {', '.join(p['name'] for p in vivos) or 'ninguno'}; "
                f"AUTOPROMO vivos = {promos}; factura {', '.join(f'{x['name']} {x['payment_state']}' for x in fact)}",
                ok, (r['error_log'] or '')[:200])
    else:
        agregar('F', 'Reaplicacion de cobro', None, '', '', False)

    c = casos.get('G')
    if c:
        r = fila(db, c['mkp_order_id'], TBL_PAYMENTS)
        fact = odoo.execute_kw('account.move', 'search_read', [[('name', '=', c['factura'])]],
                               {'fields': ['payment_state', 'amount_residual']})[0]
        ok = r['status'] == 'ODOO_PAID' and fact['payment_state'] in ('paid', 'in_payment')
        agregar('G', 'Diferencia de 0.03 a 0.15: pagada con el redondeo a 704.23.001', c,
                f"ODOO_PAID; {c['factura']} pagada (diferencia {c['dif']:.2f})",
                f"{r['status']}; {c['factura']} {fact['payment_state']} saldo {fact['amount_residual']}",
                ok, (r['error_log'] or '')[:200])
    else:
        agregar('G', 'Tolerancia 0.15', None, 'cobro con diferencia 0.03-0.15', '', False,
                'No hay en el espejo una factura sin pagar con esa diferencia')

    sin_so = extra.get('sin_so')
    if sin_so is not None:
        n_tabla = q(db, f"SELECT COUNT(*) n FROM {TBL_BILLING} WHERE marketplace=%s "
                        f"AND status='NO_SO_IN_ODOO' AND error_log LIKE 'Amazon cobro la orden y no hay SO%%'",
                    (MARKETPLACE,))[0]['n']
        filas.append({'caso': 'H', 'que_prueba': 'Ordenes cobradas sin SO: se siembran y se alertan',
                      'orden': '', 'so': '', 'preparacion': f"sembrar_sin_so desde {os.environ['AMZ_START_DATE']}",
                      'esperado': 'filas NO_SO_IN_ODOO (en produccion, 40 al 17-nov)',
                      'obtenido': f"{sin_so} en la ventana; {n_tabla} filas NO_SO_IN_ODOO sembradas en staging",
                      'resultado': 'OK' if sin_so and n_tabla else 'FALLA', 'detalle': ''})
    alerta = extra.get('alerta')
    if alerta is None:
        return filas
    filas.append({'caso': 'I', 'que_prueba': 'Alerta SAT cancelado con asiento publicado',
                  'orden': '', 'so': '', 'preparacion': '', 'esperado': 'la consulta corre sin error',
                  'obtenido': alerta if isinstance(alerta, str) else f"{len(alerta)} documento(s): {'; '.join(alerta[:5])}",
                  'resultado': 'FALLA' if isinstance(alerta, str) else 'OK', 'detalle': ''})
    return filas


def escribir_csv(filas):
    os.makedirs(SALIDA, exist_ok=True)
    campos = ['caso', 'que_prueba', 'orden', 'so', 'preparacion', 'esperado', 'obtenido',
              'resultado', 'detalle']
    with open(CSV_PATH, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        w.writerows(filas)
    log.info(f"CSV: {CSV_PATH}")


def main():
    global CANDADO, ULTIMA_SO, FIN_ESPEJO
    candado()
    mod = cargar_01()
    db = get_db_connection()
    odoo = OdooModelProxy(timeout=300)
    ultima = odoo.execute_kw('sale.order', 'search_read', [[]],
                             {'fields': ['name', 'create_date'], 'order': 'id desc', 'limit': 1})
    ULTIMA_SO = ultima[0]['name']
    FIN_ESPEJO = str(ultima[0]['create_date'])[:10]
    CANDADO = leer_candado(odoo)
    log.info(f"Espejo {ODOO_DB}: ultima SO {ULTIMA_SO} creada {ultima[0]['create_date']}")
    log.info(f"Periodo cerrado en el espejo hasta {CANDADO or '(sin candado)'}: "
             f"ningun caso toca documentos con fecha <= esa")
    journal_id = find_journal_by_account(odoo, ACCOUNT_LIBERACIONES)
    memo_campo = campo_memo(odoo)

    selectores = {
        'A': lambda: elegir_a(db, odoo, mod) or elegir_a_prod(db, odoo, mod),
        'B': lambda: elegir_b(db, odoo, mod) or elegir_b_prod(db, odoo, mod),
        'C': lambda: elegir_c(db, odoo, mod),
        'D': lambda: elegir_d(db),
        'E1': lambda: elegir_gracia(db, 0, 5),
        'E2': lambda: elegir_gracia(db, 6, 9),
        'F': lambda: elegir_f(db, odoo, journal_id, memo_campo),
        'G': lambda: elegir_g(db, odoo),
    }
    log.info(f"Casos de esta corrida: {', '.join(ACTIVOS)}")
    casos = {k: sel() for k, sel in selectores.items() if k in ACTIVOS}
    if casos.get('D'):
        casos['D']['facturas_antes'] = len([v for v in facturas_vivas(odoo, casos['D']['odoo_so_name'])
                                            if v['move_type'] == 'out_invoice']) \
            if so_existe(odoo, casos['D']['odoo_so_name']) else 0
    for k, c in casos.items():
        log.info(f"Caso {k}: {(c or {}).get('mkp_order_id', 'SIN CANDIDATO')} "
                 f"{(c or {}).get('odoo_so_name', '')}")

    if SOLO_PLAN:
        plan = [{'caso': k, 'que_prueba': 'PLAN', 'orden': (c or {}).get('mkp_order_id', ''),
                 'so': (c or {}).get('odoo_so_name', ''), 'preparacion': '', 'esperado': '',
                 'obtenido': '', 'resultado': 'PLAN' if c else 'SIN_CANDIDATO', 'detalle': ''}
                for k, c in casos.items()]
        escribir_csv(plan)
        return

    notas = preparar(db, odoo, casos)
    for k, v in notas.items():
        log.info(f"Preparado {k}: {v}")

    extra = {}
    if 'H' in ACTIVOS:
        extra['sin_so'] = mod.sembrar_sin_so(db)
    if 'I' in ACTIVOS:
        try:
            extra['alerta'] = mod.alertar_sat_cancelado_vivo(odoo)
        except Exception as e:
            extra['alerta'] = f"ERROR: {e}"

    os.makedirs(SALIDA, exist_ok=True)
    ordenes = [c['mkp_order_id'] for k, c in casos.items() if c and k in ('A', 'B', 'C', 'D', 'E1', 'E2')]
    if ordenes:
        correr('01_amz_billing.py', {'AMZ_ORDER_IDS': ','.join(ordenes)},
               os.path.join(SALIDA, f'lote_auditoria_nov_{TS}_01.log'))
    if 'F' in ACTIVOS or 'G' in ACTIVOS:
        correr('03_loader_amz_payments.py', {}, os.path.join(SALIDA, f'lote_auditoria_nov_{TS}_03.log'))

    db.close()
    db = get_db_connection()
    filas = [f for f in evaluar(db, odoo, casos, notas, extra, memo_campo, journal_id)
             if f['caso'] in ACTIVOS]
    escribir_csv(filas)
    for f in filas:
        log.info(f"{f['caso']:<3} {f['resultado']:<13} {f['obtenido'][:110]}")
    db.close()


if __name__ == '__main__':
    main()
