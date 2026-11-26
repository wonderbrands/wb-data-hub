"""
20_prueba_iva_al_cobro.py -- prueba en Odoo PRUEBAS del IVA al cobro (A1 de
Finanzas, correo de Jero del 25-nov-2026): las facturas de ML y Amazon pasan
del impuesto 38 (al facturar, 208.01.001) al 14 (al cobro, transitoria
209.01.001). Solo corre con AMZ_ENTORNO='staging'. No toca MySQL.

Casos (facturas de prueba con ref PRUEBA-IVA209:<caso>; la copia se borra):
  V   validar_impuesto de Amazon (_amz_common) y de ML (02) aceptan el 14 y
      rechazan el 38.
  ML  Factura como 02_loader_ml_billing (CxC 105.01.004, sin timbrar) y cobro
      como 04 (diario MP) con 1 centavo de mas contra 704.23.001.
  B2C Factura como 01 (CxC 105.01.006), cobro parcial y luego el resto con
      redondeo, como 03 (diario de la 102.02.008).
  B2B Factura en borrador con el centavo de IVA ajustado por cuadrar_con_cfdi
      (01) sobre la linea de impuesto, publicada y cobrada completa.
  En cada caso se lee el asiento de la factura y los asientos de base de flujo
  (tax_cash_basis_origin_move_id) que genera Odoo al conciliar, y el saldo de
  la 209.01.001 de la factura (debe quedar en 0 al cobrar todo).
  TIMBRE: tambien se cuentan facturas de la copia (= produccion al 13-nov) con
  el impuesto 14 y CFDI timbrado, como evidencia de que el PAC lo acepta.

Uso (desde esta carpeta):
  $env:AMZ_ENTORNO='staging'; ..\\..\\..\\..\\.venv\\Scripts\\python.exe 20_prueba_iva_al_cobro.py
"""
import os
import sys
import logging
import importlib.util
from datetime import date

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

import _amz_common as amz  # noqa: E402
from _amz_common import (ENTORNO, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD, validar_entorno,  # noqa: E402
                         OdooModelProxy, get_account_id, find_journal_by_account, a_float,
                         validar_impuesto, TAX_ID_MARKETPLACES, PARTNER_ID_PUBLICO_GENERAL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('prueba_iva209')

HOY = date.today().isoformat()
PAGO_METODO = 3   # transferencia


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cuenta(odoo, code):
    return get_account_id(odoo, code)


def journal_por_codigo(odoo, code):
    j = odoo.execute_kw('account.journal', 'search', [[('code', '=', code)]], {'limit': 1})
    if not j:
        raise Exception(f"No existe el diario {code}")
    return j[0]


def lineas(odoo, move_id):
    ls = odoo.execute_kw('account.move.line', 'search_read', [[('move_id', '=', move_id)]],
                         {'fields': ['account_id', 'debit', 'credit', 'display_type', 'tax_ids',
                                     'tax_line_id', 'name']})
    return [(l['account_id'][1].split(' ')[0], round(a_float(l['debit']), 2),
             round(a_float(l['credit']), 2), l['display_type']) for l in ls]


def imprimir(titulo, move):
    log.info(f"  {titulo}: {move['name']} ({move.get('journal_id', ['', ''])[1]})")
    for acc, dr, cr, tipo in sorted(lineas(ODOO, move['id']), key=lambda x: (x[1] == 0, x[0])):
        log.info(f"      {acc:<12} Dr {dr:>10,.2f}  Cr {cr:>10,.2f}  [{tipo}]")


def leer_move(move_id):
    return ODOO.execute_kw('account.move', 'read', [[move_id]],
                           {'fields': ['name', 'state', 'amount_untaxed', 'amount_tax', 'amount_total',
                                       'amount_residual', 'payment_state', 'journal_id']})[0]


def saldo_209_factura(inv_id, acc_209):
    """Saldo de la 209.01.001 que deja la factura: su linea de IVA menos lo
    que han traspasado los asientos de base de flujo."""
    caba = ODOO.execute_kw('account.move', 'search', [[('tax_cash_basis_origin_move_id', '=', inv_id),
                                                       ('state', '=', 'posted')]])
    ids = [inv_id] + caba
    ls = ODOO.execute_kw('account.move.line', 'search_read',
                         [[('move_id', 'in', ids), ('account_id', '=', acc_209)]],
                         {'fields': ['balance']})
    return round(sum(a_float(l['balance']) for l in ls), 2), caba


def crear_factura(caso, cxc, journal_id=None, precio=862.07, qty=1):
    vals = {
        'move_type': 'out_invoice',
        'partner_id': PARTNER_ID_PUBLICO_GENERAL,
        'invoice_date': HOY, 'date': HOY,
        'ref': f'PRUEBA-IVA209:{caso}',
        'l10n_mx_edi_usage': 'S01',
        'l10n_mx_edi_payment_method_id': PAGO_METODO,
        'invoice_payment_term_id': False,
        'invoice_line_ids': [(0, 0, {
            'display_type': 'product', 'product_id': PRODUCTO, 'quantity': qty,
            'price_unit': precio, 'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])]})],
    }
    if journal_id:
        vals['journal_id'] = journal_id
    inv_id = ODOO.execute_kw('account.move', 'create', [vals])
    # CxC del marketplace (lo que hacen 02 y 01 antes de publicar)
    rec = ODOO.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('account_type', '=', 'asset_receivable')]],
                          {'fields': ['id']})
    ODOO.execute_kw('account.move', 'write',
                    [[inv_id], {'line_ids': [(1, r['id'], {'account_id': cxc}) for r in rec]}])
    return inv_id


def publicar(inv_id):
    try:
        ODOO.execute_kw('account.move', 'action_post', [[inv_id]])
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            raise


def cobrar(inv_id, journal_id, importe, writeoff=None, etiqueta='cobro'):
    """Mismo asistente y mismos valores que 04 (ML) y aplicar_pago de 03 (Amazon)."""
    ctx = {'active_model': 'account.move', 'active_ids': [inv_id]}
    vals = {'journal_id': journal_id, 'amount': round(importe, 2), 'payment_date': HOY,
            'l10n_mx_edi_payment_method_id': PAGO_METODO, 'communication': f'PRUEBA-IVA209 {etiqueta}'}
    if writeoff:
        vals.update({'payment_difference_handling': 'reconcile', 'writeoff_account_id': writeoff,
                     'writeoff_label': f'Redondeo PRUEBA-IVA209 {etiqueta}'})
    wiz = ODOO.execute_kw('account.payment.register', 'create', [vals], {'context': ctx})
    try:
        ODOO.execute_kw('account.payment.register', 'action_create_payments', [[wiz]], {'context': ctx})
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            raise


def reportar(caso, inv_id, acc_209):
    inv = leer_move(inv_id)
    saldo, caba = saldo_209_factura(inv_id, acc_209)
    log.info(f"  {caso}: {inv['name']} total {inv['amount_total']:,.2f} IVA {inv['amount_tax']:,.2f} "
             f"saldo {inv['amount_residual']:,.2f} ({inv['payment_state']}) | 209 que queda: {saldo:,.2f} "
             f"| asientos base de flujo: {len(caba)}")
    return inv, saldo, caba


def main():
    global ODOO, PRODUCTO
    if ENTORNO != 'staging':
        raise SystemExit("Solo en staging (AMZ_ENTORNO='staging').")
    validar_entorno()
    ODOO = OdooModelProxy(timeout=300)
    acc_209 = cuenta(ODOO, '209.01.001')
    acc_cxc_ml = cuenta(ODOO, '105.01.004')
    acc_cxc_amz = cuenta(ODOO, '105.01.006')
    acc_redondeo = cuenta(ODOO, '704.23.001')
    j_mp = journal_por_codigo(ODOO, 'MP')
    j_amz = find_journal_by_account(ODOO, '102.02.008')
    j_amz = j_amz if isinstance(j_amz, int) else j_amz['id'] if isinstance(j_amz, dict) else j_amz[0]
    resultados = []

    # --- V: validaciones -----------------------------------------------------
    log.info("V. validar_impuesto")
    t = validar_impuesto(ODOO)
    log.info(f"  Amazon acepta el {TAX_ID_MARKETPLACES} ({t['name']}, {t['tax_exigibility']}, "
             f"{t['cash_basis_transition_account_id'][1]})")
    amz.TAX_ID_MARKETPLACES = 38
    try:
        amz.validar_impuesto(ODOO)
        log.error("  Amazon NO rechazo el 38")
        resultados.append(('V-amz-38', False))
    except Exception as e:
        log.info(f"  Amazon rechaza el 38: {e}")
        resultados.append(('V-amz-38', True))
    amz.TAX_ID_MARKETPLACES = TAX_ID_MARKETPLACES

    # 02 de ML apuntando a pruebas (no toca MySQL al importarse)
    os.environ.update({'ODOO_URL': ODOO_URL, 'ODOO_DB': ODOO_DB, 'ODOO_USER': ODOO_USER,
                       'ODOO_PASSWORD': ODOO_PWD, 'ML_SELLER_ID': '25523702'})
    os.environ.pop('TAX_ID_MARKETPLACES', None)
    ml_dir = os.path.join(_dir, '..', 'mercadolibre')
    sys.path.insert(0, ml_dir)
    # ml_stores.py solo vive en processes/
    sys.path.insert(0, os.path.join(_dir, '..', '..', '..', '..', 'processes', 'wonderbrands', 'financial',
                                    'billing_processes', 'mercadolibre'))
    ml02 = cargar('ml02', os.path.join(ml_dir, '02_loader_ml_billing.py'))
    proxy = ml02.OdooModelProxy(ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD)
    ml02.validar_impuesto(proxy)
    log.info(f"  ML 02 acepta el {ml02.TAX_ID_MARKETPLACES}")
    ml02.TAX_ID_MARKETPLACES = 38
    try:
        ml02.validar_impuesto(proxy)
        resultados.append(('V-ml-38', False))
    except Exception as e:
        log.info(f"  ML 02 rechaza el 38: {e}")
        resultados.append(('V-ml-38', True))

    # Producto real de una factura de Amazon reciente (para ver costo y 205)
    ln = ODOO.execute_kw('account.move.line', 'search_read',
                         [[('move_id.move_type', '=', 'out_invoice'), ('move_id.state', '=', 'posted'),
                           ('account_id', '=', acc_cxc_amz)]], {'fields': ['move_id'], 'limit': 1,
                                                                 'order': 'id desc'})
    prod = ODOO.execute_kw('account.move.line', 'search_read',
                           [[('move_id', '=', ln[0]['move_id'][0]), ('display_type', '=', 'product'),
                             ('product_id', '!=', False)]], {'fields': ['product_id'], 'limit': 1})
    PRODUCTO = prod[0]['product_id'][0]
    log.info(f"Producto de prueba: {prod[0]['product_id'][1]}")

    # --- ML ------------------------------------------------------------------
    log.info("ML. Factura (02) y cobro con +0.01 a 704.23.001 (04)")
    inv = crear_factura('ML', acc_cxc_ml)
    publicar(inv)
    imprimir('Factura', leer_move(inv))
    total = leer_move(inv)['amount_total']
    cobrar(inv, j_mp, total + 0.01, writeoff=acc_redondeo, etiqueta='ML')
    r, saldo, caba = reportar('ML', inv, acc_209)
    for c in caba:
        imprimir('Base de flujo', leer_move(c))
    resultados.append(('ML', r['payment_state'] in ('paid', 'in_payment') and saldo == 0 and caba))

    # --- B2C -----------------------------------------------------------------
    log.info("B2C. Factura (01), cobro parcial y resto con redondeo -0.01 (03)")
    inv = crear_factura('B2C', acc_cxc_amz, precio=431.03, qty=2)
    publicar(inv)
    imprimir('Factura', leer_move(inv))
    total = leer_move(inv)['amount_total']
    mitad = round(total / 2, 2)
    cobrar(inv, j_amz, mitad, etiqueta='B2C-parcial')
    r, saldo_p, caba = reportar('B2C parcial', inv, acc_209)
    for c in caba:
        imprimir('Base de flujo (parcial)', leer_move(c))
    iva = r['amount_tax']
    esperado = round(iva - iva * mitad / total, 2)
    cobrar(inv, j_amz, round(total - mitad - 0.01, 2), writeoff=acc_redondeo, etiqueta='B2C-resto')
    r, saldo, caba2 = reportar('B2C resto', inv, acc_209)
    for c in caba2:
        if c not in caba:
            imprimir('Base de flujo (resto)', leer_move(c))
    resultados.append(('B2C', abs(-saldo_p - esperado) <= 0.01 and saldo == 0
                       and r['payment_state'] in ('paid', 'in_payment')))

    # --- B2B con centavo de IVA ----------------------------------------------
    log.info("B2B. Centavo de IVA ajustado con cuadrar_con_cfdi (01) y cobro completo")
    os.environ['AMZ_ENTORNO'] = 'staging'
    amz01 = cargar('amz01', os.path.join(_dir, '01_amz_billing.py'))
    inv = crear_factura('B2B', acc_cxc_amz, precio=1206.29)
    b = leer_move(inv)
    cfdi = {'total': round(b['amount_total'] + 0.01, 2), 'taxes': round(b['amount_tax'] + 0.01, 2),
            'subtotal': b['amount_untaxed'], 'discount': 0}
    ajuste = amz01.cuadrar_con_cfdi(ODOO, inv, cfdi, 'PRUEBA-IVA209:B2B')
    publicar(inv)
    b = leer_move(inv)
    imprimir(f'Factura (ajuste IVA {ajuste:+.2f})', b)
    cobrar(inv, j_amz, b['amount_total'], etiqueta='B2B')
    r, saldo, caba = reportar('B2B', inv, acc_209)
    for c in caba:
        imprimir('Base de flujo', leer_move(c))
    resultados.append(('B2B', ajuste == 0.01 and r['amount_total'] == cfdi['total'] and saldo == 0
                       and r['payment_state'] in ('paid', 'in_payment')))

    # --- Evidencia de timbrado con el 14 -------------------------------------
    timbradas = ODOO.execute_kw('account.move', 'search_count', [[
        ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
        ('l10n_mx_edi_cfdi_state', '=', 'sent'),
        ('invoice_line_ids.tax_ids', 'in', [TAX_ID_MARKETPLACES])]])
    ej = ODOO.execute_kw('account.move', 'search_read', [[
        ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
        ('l10n_mx_edi_cfdi_state', '=', 'sent'),
        ('invoice_line_ids.tax_ids', 'in', [TAX_ID_MARKETPLACES])]],
        {'fields': ['name', 'invoice_date', 'l10n_mx_edi_cfdi_uuid'], 'limit': 3, 'order': 'id desc'})
    log.info(f"TIMBRE. Facturas timbradas con el impuesto {TAX_ID_MARKETPLACES} en la copia: {timbradas}")
    for e in ej:
        log.info(f"    {e['name']} {e['invoice_date']} {e['l10n_mx_edi_cfdi_uuid']}")

    log.info('=' * 70)
    for caso, ok in resultados:
        log.info(f"  {caso:<10} {'OK' if ok else 'FALLA'}")


if __name__ == '__main__':
    main()
