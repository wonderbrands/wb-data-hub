"""

09_reporte_lote_finanzas.py
---------------------------
REPORTE DEL LOTE DE PRUEBAS PARA FINANZAS (EXCEL) -- SOLO LECTURA

Junta en un .xlsx lo que hizo una corrida 06 -> 01 -> 02 -> 03:

  Resumen       cifras del lote y el cuadre global
  Facturas      una fila por factura creada (B2B contra su CFDI de Amazon)
  Cobros        una fila por cobro: venta acreditada, aplicado, redondeo,
                promocion, comisiones, IVA retenido y deposito neto
  Playbook      la formula del Playbook de Soporte de Ventas v2.6 (p. 8)
                aplicada a cada cobro: lista neta + envio - pagado = promocion
  Promociones   asientos AUTOPROMO en Odoo (403.01.002 + 208.01.001)
  Settlements   cuadre por settlement
  Excepciones   lo que no termino en factura pagada

El lote son las filas de las tablas de control creadas desde AMZ_REPORTE_DESDE
(hora UTC de MySQL; por defecto, hoy 00:00 UTC). La hoja Promociones lista TODOS los asientos
AUTOPROMO de la base de pruebas, no solo los del lote.

MODO SETTLEMENT (cambio del 28-oct-2026, primera corrida por settlement que
pidio Finanzas). Con AMZ_SETTLEMENT_ID el lote es el settlement COMPLETO: todas
sus ordenes y todos sus cobros, aunque una orden ya estuviera en la tabla de
control por un lote anterior. Asi la hoja Settlements cuadra lo liberado del
settlement entero contra la transitoria. AMZ_ORDER_IDS agrega ordenes sueltas
(las mismas que se le pasaron a 01). En la hoja Facturas salen solo las
facturas que 01 proceso desde DESDE.

AMZ_REPORTE_CASOS (opcional) arma la hoja "Casos pedidos": lista
'order_id=descripcion' separada por ';'.

No escribe en MySQL ni en Odoo.

Uso:
    python 09_reporte_lote_finanzas.py                     # lote de hoy
    python 09_reporte_lote_finanzas.py "2026-10-28 15:00"  # desde una hora UTC
"""

import os
import re
import sys
import json
import logging
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, ODOO_DB, TBL_BILLING, TBL_PAYMENTS, MARKETPLACE,
    ACCOUNT_REDONDEO, ACCOUNT_CUPONES_AMAZON, ACCOUNT_IVA_TRASLADADO,
    ACCOUNT_MERC_NO_ENVIADA,
    get_db_connection, OdooModelProxy, a_float, parse_settlement_id,
    get_account_id,
)

AMZ_SETTLEMENT_ID = os.getenv('AMZ_SETTLEMENT_ID', '').strip() or None
AMZ_ORDER_IDS = [o.strip() for o in os.getenv('AMZ_ORDER_IDS', '').split(',') if o.strip()]
CASOS = [c.split('=', 1) for c in os.getenv('AMZ_REPORTE_CASOS', '').split(';') if '=' in c]

# OJO: created_at lo pone MySQL con su reloj, que esta en UTC (CDMX - 6 h).
# DESDE va en esa misma hora: el lote corrido a las 16:26 de CDMX es
# '... 22:26:00'. Con hora local entraban filas de otras corridas del dia.
DESDE = (sys.argv[1] if len(sys.argv) > 1
         else os.getenv('AMZ_REPORTE_DESDE',
                        datetime.utcnow().strftime('%Y-%m-%d 00:00:00')))

LOTE_ODOO = 200

# =============================================================================
# FORMULA DEL PLAYBOOK, POR PIEZA
# =============================================================================
# Literal del Playbook v2.6 p. 8:
#   lista neta = product_charges + our_price_tax
#                + (promo_rebates - shipping_discount) + our_price_tax_disc
#   envio      = shipping_principal + shipping_tax + shipping_discount
#                + shipping_tax_discount
# promo_rebates es el roll-up de descuentos (incluye shipping_discount), por
# eso se le resta: lo que queda son NUESTRAS promociones sobre el producto.
# Misma deduplicacion que 01/02/03: por pieza gana el posted_date mas reciente.
# =============================================================================

QUERY_PIEZAS = """
SELECT COALESCE(a.shipment_id, a.transaction_id) AS pieza,
       a.settlement_id, a.posted_date,
       SUM(IFNULL(b.product_charges,0) + IFNULL(b.our_price_tax,0)
         + IFNULL(b.promo_rebates,0) - IFNULL(b.shipping_discount,0)
         + IFNULL(b.our_price_tax_disc,0)) AS lista_neta,
       SUM(IFNULL(b.shipping_principal,0) + IFNULL(b.shipping_tax,0)
         + IFNULL(b.shipping_discount,0) + IFNULL(b.shipping_tax_discount,0)) AS envio,
       -- amazon_fees ya incluye amazon_for_all_fee y shipping_hb.
       SUM(IFNULL(b.amazon_fees,0)) AS comisiones,
       SUM(IFNULL(b.marketplace_vat_principal,0)
         + IFNULL(b.marketplace_vat_shipping,0)) AS marketplace_vat,
       SUM(IFNULL(b.item_amount,0)) AS neto
FROM somos_reyes.amz_transactions a
JOIN somos_reyes.amz_transaction_items b ON a.transaction_id = b.transaction_id
WHERE a.order_id = %s
  AND a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'
GROUP BY a.transaction_id, a.transaction_status, a.shipment_id,
         a.settlement_id, a.posted_date
"""

CAMPOS_PIEZA = ('lista_neta', 'envio', 'comisiones', 'marketplace_vat', 'neto')


def piezas_por_settlement(db_conn, order_id):
    """{settlement_id: {lista_neta, envio, comisiones, marketplace_vat, neto}}"""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_PIEZAS, (order_id,))
    ultima = {}
    for r in cursor.fetchall():
        p = str(r['pieza'])
        if p not in ultima or r['posted_date'] > ultima[p]['posted_date']:
            ultima[p] = r
    cursor.close()
    salida = {}
    for r in ultima.values():
        s = salida.setdefault(str(r['settlement_id']), {k: 0.0 for k in CAMPOS_PIEZA})
        for k in CAMPOS_PIEZA:
            s[k] = round(s[k] + a_float(r[k]), 2)
    return salida


def leer_raw(texto):
    try:
        return json.loads(texto) if texto else {}
    except (ValueError, TypeError):
        return {}


def numero(patron, texto):
    m = re.search(patron, texto or '')
    return float(m.group(1)) if m else 0.0


def en_trozos(lista):
    for i in range(0, len(lista), LOTE_ODOO):
        yield lista[i:i + LOTE_ODOO]


# =============================================================================
# LECTURA
# =============================================================================

def leer_tablas(db_conn):
    cursor = db_conn.cursor(dictionary=True)
    if AMZ_SETTLEMENT_ID:
        sueltas = AMZ_ORDER_IDS or ['-']
        marcas = ', '.join(['%s'] * len(sueltas))
        cursor.execute(f"""
            SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, status,
                   error_log, raw_json, created_at, processed_at
            FROM {TBL_BILLING}
            WHERE marketplace = %s
              AND (mkp_order_id IN (SELECT s.order_id FROM somos_reyes.amz_transactions s
                                    WHERE s.settlement_id = %s
                                      AND s.description = 'Order Payment'
                                      AND s.transaction_status <> 'DEFERRED')
                   OR mkp_order_id IN ({marcas}))
            ORDER BY id""", tuple([MARKETPLACE, AMZ_SETTLEMENT_ID] + sueltas))
        billing = cursor.fetchall()
        cursor.execute(f"""
            SELECT p.id, p.mkp_order_id, p.payment_id, p.amount, p.date_released,
                   p.status, p.error_log, p.created_at, p.processed_at,
                   b.odoo_so_name, b.raw_json
            FROM {TBL_PAYMENTS} p
            LEFT JOIN {TBL_BILLING} b
                   ON b.marketplace = p.marketplace AND b.mkp_order_id = p.mkp_order_id
            WHERE p.marketplace = %s
              AND (p.payment_id LIKE %s OR p.mkp_order_id IN ({marcas}))
            ORDER BY p.date_released, p.payment_id""",
                       tuple([MARKETPLACE, f"{AMZ_SETTLEMENT_ID}:%"] + sueltas))
        cobros = cursor.fetchall()
    else:
        cursor.execute(f"""
            SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, status,
                   error_log, raw_json, created_at, processed_at
            FROM {TBL_BILLING}
            WHERE marketplace = %s AND created_at >= %s
            ORDER BY id""", (MARKETPLACE, DESDE))
        billing = cursor.fetchall()
        cursor.execute(f"""
            SELECT p.id, p.mkp_order_id, p.payment_id, p.amount, p.date_released,
                   p.status, p.error_log, p.created_at, p.processed_at,
                   b.odoo_so_name, b.raw_json
            FROM {TBL_PAYMENTS} p
            LEFT JOIN {TBL_BILLING} b
                   ON b.marketplace = p.marketplace AND b.mkp_order_id = p.mkp_order_id
            WHERE p.marketplace = %s AND p.created_at >= %s
            ORDER BY p.date_released, p.payment_id""", (MARKETPLACE, DESDE))
        cobros = cursor.fetchall()
    cursor.execute("""
        SELECT settlement_id, MAX(deposit_date) AS deposit_date,
               MAX(total_amount) AS total_amount
        FROM somos_reyes.amz_settlement_report GROUP BY settlement_id""")
    settlements = {str(r['settlement_id']): r for r in cursor.fetchall()}
    cursor.close()
    for c in cobros:
        c['settlement_id'] = parse_settlement_id(c['payment_id'])
    return billing, cobros, settlements


def leer_odoo(odoo, so_names, memos):
    facturas = []
    for trozo in en_trozos(sorted(so_names)):
        facturas += odoo.execute_kw(
            'account.move', 'search_read',
            [[('invoice_origin', 'in', trozo), ('move_type', '=', 'out_invoice'),
              ('state', '=', 'posted'), ('ref', '=like', 'AUTOINV:%')]],
            {'fields': ['id', 'name', 'ref', 'invoice_origin', 'invoice_date',
                        'partner_id', 'amount_untaxed', 'amount_tax',
                        'amount_total', 'amount_residual', 'payment_state',
                        'l10n_mx_edi_cfdi_uuid']})

    pagos = []
    for trozo in en_trozos(sorted(memos)):
        pagos += odoo.execute_kw(
            'account.payment', 'search_read',
            [[('memo', 'in', trozo), ('state', '!=', 'canceled')]],
            {'fields': ['id', 'name', 'amount', 'date', 'memo', 'state', 'move_id']})

    # Linea de redondeo dentro de cada pago (write-off del wizard).
    redondeo_por_move = {}
    cuenta_redondeo = get_account_id(odoo, ACCOUNT_REDONDEO)
    moves = [p['move_id'][0] for p in pagos if p.get('move_id')]
    for trozo in en_trozos(moves):
        for l in odoo.execute_kw(
                'account.move.line', 'search_read',
                [[('move_id', 'in', trozo),
                  ('account_id', '=', cuenta_redondeo)]],
                {'fields': ['move_id', 'balance']}):
            k = l['move_id'][0]
            redondeo_por_move[k] = round(redondeo_por_move.get(k, 0.0)
                                         - a_float(l['balance']), 2)

    promos = odoo.execute_kw(
        'account.move', 'search_read',
        [[('ref', '=like', 'AUTOPROMO:%'), ('state', '=', 'posted')]],
        {'fields': ['id', 'name', 'ref', 'date', 'amount_total'], 'order': 'name'})
    lineas_promo = {}
    for trozo in en_trozos([p['id'] for p in promos]):
        for l in odoo.execute_kw(
                'account.move.line', 'search_read', [[('move_id', 'in', trozo)]],
                {'fields': ['move_id', 'account_id', 'debit', 'credit']}):
            codigo = (l['account_id'][1] or '').split(' ')[0] if l['account_id'] else ''
            d = lineas_promo.setdefault(l['move_id'][0], {})
            d[codigo] = round(d.get(codigo, 0.0) + a_float(l['debit'])
                              - a_float(l['credit']), 2)
    return facturas, pagos, redondeo_por_move, promos, lineas_promo


def estado_205(odoo, facturas, canceladas=()):
    """{factura_id: estado de la 205.06.003} (correo de Jero, punto 2).

    Conciliada con la salida / Sin salida todavia (Odoo concilia al validarla)
    / Abierta (hay salida y no se concilio) / Sin costo (no lleva 205.06.003)
    / SO cancelada (no habra salida: se concilia con la NC, correo del 28-oct, segundo).
    """
    cuenta = get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA)
    lineas = {}
    for trozo in en_trozos([f['id'] for f in facturas]):
        for l in odoo.execute_kw('account.move.line', 'search_read',
                                 [[('move_id', 'in', trozo), ('account_id', '=', cuenta)]],
                                 {'fields': ['move_id', 'reconciled']}):
            lineas.setdefault(l['move_id'][0], []).append(l['reconciled'])
    # SO que ya tienen salida de almacen: se pregunta por SO (pocas llamadas),
    # con el mismo camino que usa conciliar_salida() en 01.
    sos_con_salida = set()
    abiertas = {f['invoice_origin'] for f in facturas
                if lineas.get(f['id']) and not all(lineas[f['id']])}
    for so in sorted(abiertas):
        if odoo.execute_kw('account.move.line', 'search_count',
                           [[('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
                             ('balance', '>', 0),
                             ('move_id.stock_move_id.sale_line_id.order_id.name', '=', so)]]):
            sos_con_salida.add(so)

    salida = {}
    for f in facturas:
        ls = lineas.get(f['id'])
        if not ls:
            salida[f['id']] = 'Sin costo'
        elif all(ls):
            salida[f['id']] = 'Conciliada con la salida'
        elif f['invoice_origin'] in canceladas:
            salida[f['id']] = 'SO cancelada: sin salida, se concilia con la NC'
        elif f['invoice_origin'] not in sos_con_salida:
            salida[f['id']] = 'Sin salida todavia (Odoo concilia al validarla)'
        else:
            salida[f['id']] = 'Abierta'
    return salida


# =============================================================================
# EXCEL
# =============================================================================

NEGRITA = Font(bold=True, color='FFFFFF')
FONDO = PatternFill('solid', fgColor='1F4E78')
TITULO = Font(bold=True, size=14)
MONEDA = '#,##0.00'


def hoja(wb, nombre, encabezados, filas, monedas=()):
    ws = wb.create_sheet(nombre)
    ws.append(encabezados)
    for celda in ws[1]:
        celda.font = NEGRITA
        celda.fill = FONDO
        celda.alignment = Alignment(wrap_text=True, vertical='center')
    for f in filas:
        ws.append(f)
    for i, enc in enumerate(encabezados, start=1):
        letra = get_column_letter(i)
        if enc in monedas:
            for celda in ws[letra][1:]:
                celda.number_format = MONEDA
        largo = max([len(str(enc))] + [len(str(f[i - 1])) for f in filas
                                        if f[i - 1] is not None] or [10])
        ws.column_dimensions[letra].width = min(max(largo + 2, 10), 60)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    return ws


def run():
    db_conn = get_db_connection()
    billing, cobros, settlements = leer_tablas(db_conn)
    # Solo los cobros de las ordenes del lote. 02 puede sembrar en la misma
    # corrida cobros de ordenes de lotes anteriores; se cuentan aparte.
    # En modo settlement el lote es el settlement completo: no se filtra.
    ordenes_lote = {b['mkp_order_id'] for b in billing}
    if AMZ_SETTLEMENT_ID:
        otros_cobros = []
    else:
        otros_cobros = [c for c in cobros if c['mkp_order_id'] not in ordenes_lote]
        cobros = [c for c in cobros if c['mkp_order_id'] in ordenes_lote]
    log.info(f"Lote {'settlement ' + AMZ_SETTLEMENT_ID if AMZ_SETTLEMENT_ID else 'desde ' + DESDE}: "
             f"{len(billing)} filas de facturacion, "
             f"{len(cobros)} cobros ({len(otros_cobros)} cobros de otras "
             f"ordenes quedan fuera).")

    so_names = {b['odoo_so_name'] for b in billing if b['odoo_so_name']}
    so_names |= {c['odoo_so_name'] for c in cobros if c['odoo_so_name']}
    memos = {f"AMZ {c['settlement_id']} / {c['mkp_order_id']}" for c in cobros}

    odoo = OdooModelProxy(timeout=300)
    facturas, pagos, redondeo_por_move, promos, lineas_promo = \
        leer_odoo(odoo, so_names, memos)
    fact_por_so = {}
    for f in facturas:
        fact_por_so.setdefault(f['invoice_origin'], []).append(f)
    pagos_por_memo = {}
    for p in pagos:
        pagos_por_memo.setdefault(p['memo'], []).append(p)
    promo_por_ref = {p['ref']: p for p in promos}

    # Facturas que este proceso creo en la corrida (en modo settlement, las
    # que 01 proceso desde DESDE; las demas del settlement son de lotes
    # anteriores o del 1a1).
    def en_corrida(b):
        if not AMZ_SETTLEMENT_ID:
            return True
        return bool(b['processed_at']) and str(b['processed_at']) >= DESDE

    facturados = ('ODOO_INVOICED', 'ODOO_B2B_LINKED')

    def facturas_de(b):
        """Facturas que este proceso creo para la fila: todas las de la SO si
        la facturo, o solo la de envio aparte si ya estaba facturada."""
        todas = fact_por_so.get(b['odoo_so_name'], [])
        if b['status'] in facturados:
            return todas
        if 'ENVIO_FACTURADO_APARTE' in (b['error_log'] or ''):
            return [f for f in todas if (f['ref'] or '').endswith(':ENVIO')]
        return []

    propias = [f for b in billing if en_corrida(b) for f in facturas_de(b)]
    canceladas = {b['odoo_so_name'] for b in billing
                  if 'SO_CANCELADA' in (b['error_log'] or '')}
    est_205 = estado_205(odoo, propias, canceladas)

    wb = Workbook()
    wb.remove(wb.active)
    resumen = wb.create_sheet('Resumen')

    # --- Facturas ---
    filas_fact, b2b_iguales, b2b_total = [], 0, 0
    tipo_por_so = {}
    for b in billing:
        raw = leer_raw(b['raw_json'])
        cfdi = raw.get('cfdi_amazon_business') or {}
        tipo = 'B2B' if cfdi else 'B2C'
        tipo_por_so[b['odoo_so_name']] = tipo
        # Solo las facturas que creo este proceso en la corrida. Una orden
        # ALREADY_ODOO_INVOICED trae la factura que ya existia (va a Excepciones).
        if not en_corrida(b) or not facturas_de(b):
            continue
        # Orden partida (un CFDI de Amazon por envio): el CFDI del raw_json
        # es la suma de los comprobantes, asi que se compara contra la suma
        # de las facturas de la orden, no factura por factura.
        sus_facturas = facturas_de(b)
        total_orden = round(sum(a_float(x['amount_total']) for x in sus_facturas), 2)
        for f in sus_facturas:
            cfdi_total = a_float(cfdi.get('total')) if cfdi else None
            cfdi_base = (round(a_float(cfdi.get('subtotal')) - a_float(cfdi.get('discount')), 2)
                         if cfdi else None)
            dif = round(total_orden - cfdi_total, 2) if cfdi else None
            if cfdi:
                b2b_total += 1
                b2b_iguales += 1 if dif == 0 else 0
            filas_fact.append([
                b['mkp_order_id'], tipo, b['odoo_so_name'], f['name'],
                f['invoice_date'] or '',
                f['partner_id'][1] if f['partner_id'] else '',
                a_float(f['amount_untaxed']), a_float(f['amount_tax']),
                a_float(f['amount_total']),
                cfdi_base, a_float(cfdi.get('taxes')) if cfdi else None, cfdi_total,
                dif, 'IGUAL AL CFDI' if dif == 0 else ('DIFERENCIA' if cfdi else ''),
                f['l10n_mx_edi_cfdi_uuid'] or '',
                (raw.get('amazon') or {}).get('order_total_cliente'),
                a_float(f['amount_residual']), f['payment_state'], b['status'],
                est_205.get(f['id'], ''), b['error_log'] or '',
            ])
    hoja(wb, 'Facturas',
         ['Amazon order_id', 'Tipo', 'SO', 'Factura', 'Fecha', 'Cliente',
          'Base Odoo', 'IVA Odoo', 'Total Odoo',
          'Base CFDI Amazon', 'IVA CFDI Amazon', 'Total CFDI Amazon',
          'Dif Odoo (orden) - CFDI', 'Cuadre B2B', 'UUID', 'Pagado comprador (OrderTotal)',
          'Saldo', 'Estado de pago', 'Estado control',
          f'{ACCOUNT_MERC_NO_ENVIADA} Mercancias facturadas - no enviadas',
          'Nota del proceso'],
         filas_fact,
         monedas={'Base Odoo', 'IVA Odoo', 'Total Odoo', 'Base CFDI Amazon',
                  'IVA CFDI Amazon', 'Total CFDI Amazon', 'Dif Odoo (orden) - CFDI',
                  'Pagado comprador (OrderTotal)', 'Saldo'})

    # --- Cobros y Playbook ---
    filas_cob, filas_pb = [], []
    por_stl = {}
    # no_aplicado: cobros que 03 no aplico (sin factura, error). Siguen en
    # la tabla y se reintentan; el control del settlement los separa.
    tot = {'venta': 0.0, 'aplicado': 0.0, 'promo': 0.0, 'sin_aplicar': 0.0,
           'no_aplicado': 0.0, 'redondeo': 0.0, 'comisiones': 0.0, 'mvat': 0.0,
           'neto': 0.0}
    n_redondeo = n_promo = n_sin_clasif = 0
    for c in cobros:
        raw = leer_raw(c['raw_json'])
        cliente = (raw.get('amazon') or {}).get('order_total_cliente')
        piezas = piezas_por_settlement(db_conn, c['mkp_order_id']).get(
            str(c['settlement_id']), {k: 0.0 for k in CAMPOS_PIEZA})
        venta = a_float(c['amount'])
        # Envio acreditado fuera de los campos de envio (02 lo suma al cobro):
        # Amazon retiene IVA sobre el y ese IVA no viene en marketplace_vat.
        # Se suma al IVA retenido para que el neto cuadre (701-4769487:
        # 4.07 sobre 59.00 de envio).
        if venta > round(piezas['lista_neta'] + piezas['envio'], 2) + 0.005:
            piezas = dict(piezas, marketplace_vat=round(
                piezas['neto'] - venta - piezas['comisiones'], 2))
        nota = c['error_log'] or ''
        promo = numero(r'promo ([\d\.]+)', nota) if 'PROMO_ASENTADO' in nota else 0.0
        sin_aplicar = numero(r'sin_aplicar ([\d\.]+)', nota)
        aplicado = round(venta - promo - sin_aplicar, 2) if c['status'] == 'ODOO_PAID' else 0.0
        memo = f"AMZ {c['settlement_id']} / {c['mkp_order_id']}"
        sus_pagos = pagos_por_memo.get(memo, [])
        redondeo = round(sum(redondeo_por_move.get(p['move_id'][0], 0.0)
                             for p in sus_pagos if p.get('move_id')), 2)
        n_redondeo += 1 if redondeo else 0
        ref_promo = f"AUTOPROMO:{c['settlement_id']}:{c['mkp_order_id']}"
        asiento = promo_por_ref.get(ref_promo)
        n_promo += 1 if promo else 0
        n_sin_clasif += 1 if 'SIN_CLASIFICAR' in nota else 0
        so = c['odoo_so_name'] or ''
        facts = fact_por_so.get(so, [])
        filas_cob.append([
            c['settlement_id'], str(c['date_released'] or ''), c['mkp_order_id'],
            tipo_por_so.get(so, ''), so, ', '.join(f['name'] for f in facts),
            ', '.join(p['name'] for p in sus_pagos),
            venta, cliente, aplicado, redondeo, promo, sin_aplicar,
            piezas['comisiones'], piezas['marketplace_vat'], piezas['neto'],
            ', '.join(f['payment_state'] for f in facts),
            c['status'], asiento['name'] if asiento else '', nota,
        ])

        transacciones = round(piezas['lista_neta'] + piezas['envio'], 2)
        promo_pb = round(transacciones - a_float(cliente), 2) if cliente is not None else None
        total_fact = round(sum(a_float(f['amount_total']) for f in facts), 2)
        if 'PROMO_ASENTADO' in nota:
            resultado = 'PROMO AMAZON ASENTADA'
        elif 'SIN_CLASIFICAR' in nota:
            resultado = 'SIN CLASIFICAR: ' + nota.split('EXCEDENTE_SIN_CLASIFICAR', 1)[-1].strip()
        elif promo_pb is not None and promo_pb > 0.01:
            resultado = 'Promocion en la formula, pero no hubo excedente en el cobro'
        else:
            resultado = 'Sin promocion (pagado = transacciones)'
        filas_pb.append([
            c['settlement_id'], c['mkp_order_id'], so,
            piezas['lista_neta'], piezas['envio'], transacciones, venta,
            round(transacciones - venta, 2),
            cliente, 'Amazon OrderTotal' if cliente is not None else 'SIN DATO',
            total_fact, round(total_fact - a_float(cliente), 2) if cliente is not None else None,
            promo_pb if promo_pb is None or promo_pb > 0 else 0.0,
            round(venta - aplicado + redondeo, 2) if c['status'] == 'ODOO_PAID' else None,
            promo, round(promo / 1.16, 2) if promo else 0.0,
            round(promo - round(promo / 1.16, 2), 2) if promo else 0.0, resultado,
        ])

        s = por_stl.setdefault(str(c['settlement_id']), {k: 0.0 for k in tot})
        s.setdefault('n', 0)
        s['n'] += 1
        no_aplicado = venta if c['status'] != 'ODOO_PAID' else 0.0
        for k, v in (('venta', venta), ('aplicado', aplicado), ('promo', promo),
                     ('sin_aplicar', sin_aplicar), ('no_aplicado', no_aplicado),
                     ('redondeo', redondeo),
                     ('comisiones', piezas['comisiones']),
                     ('mvat', piezas['marketplace_vat']), ('neto', piezas['neto'])):
            s[k] = round(s[k] + v, 2)
            tot[k] = round(tot[k] + v, 2)

    hoja(wb, 'Cobros',
         ['Settlement', 'Fecha liberacion', 'Amazon order_id', 'Tipo', 'SO',
          'Factura(s)', 'Pago(s) Odoo', 'Venta acreditada por Amazon',
          'Pagado comprador (OrderTotal)', 'Aplicado a factura', 'Redondeo',
          'Promocion Amazon asentada', 'Sin aplicar', 'Comisiones Amazon',
          'IVA retenido (marketplace_vat)', 'Deposito neto', 'Estado factura',
          'Estado control', 'Asiento promocion', 'Nota del proceso'],
         filas_cob,
         monedas={'Venta acreditada por Amazon', 'Pagado comprador (OrderTotal)',
                  'Aplicado a factura', 'Redondeo', 'Promocion Amazon asentada',
                  'Sin aplicar', 'Comisiones Amazon',
                  'IVA retenido (marketplace_vat)', 'Deposito neto'})

    hoja(wb, 'Playbook',
         ['Settlement', 'Amazon order_id', 'SO', '(a) Lista neta', '(b) Envio',
          '(c) = a + b Transacciones', 'Venta acreditada (cobro)',
          'Control c - cobro', '(d) Pagado por el cliente', 'Fuente de (d)',
          '(e) Factura Odoo', 'Control e - d', '(iii) = c - d Promocion Amazon',
          'Excedente del cobro', 'Asentado', 'Base 403.01.002',
          'IVA 208.01.001', 'Resultado'],
         filas_pb,
         monedas={'(a) Lista neta', '(b) Envio', '(c) = a + b Transacciones',
                  'Venta acreditada (cobro)', 'Control c - cobro',
                  '(d) Pagado por el cliente', '(e) Factura Odoo', 'Control e - d',
                  '(iii) = c - d Promocion Amazon', 'Excedente del cobro',
                  'Asentado', 'Base 403.01.002', 'IVA 208.01.001'})

    # --- Promociones (todas las de la base) ---
    filas_promo = []
    for p in promos:
        _, stl, orden = p['ref'].split(':', 2)
        l = lineas_promo.get(p['id'], {})
        filas_promo.append([
            p['name'], p['date'], stl, orden,
            l.get('102.02.008', 0.0), -l.get(ACCOUNT_CUPONES_AMAZON, 0.0),
            -l.get(ACCOUNT_IVA_TRASLADADO, 0.0),
            'Si' if any(c['mkp_order_id'] == orden for c in cobros) else 'Lote anterior',
        ])
    hoja(wb, 'Promociones',
         ['Asiento', 'Fecha', 'Settlement', 'Amazon order_id',
          'Debe 102.02.008', f'Haber {ACCOUNT_CUPONES_AMAZON} (base)',
          f'Haber {ACCOUNT_IVA_TRASLADADO} (IVA)', 'De este lote'],
         filas_promo,
         monedas={'Debe 102.02.008', f'Haber {ACCOUNT_CUPONES_AMAZON} (base)',
                  f'Haber {ACCOUNT_IVA_TRASLADADO} (IVA)'})

    # --- Settlements ---
    filas_stl = []
    for sid in sorted(por_stl):
        s = por_stl[sid]
        info = settlements.get(sid) or {}
        filas_stl.append([
            sid, str(info.get('deposit_date') or ''), s['n'], s['venta'],
            s['aplicado'], s['redondeo'], s['promo'], s['sin_aplicar'], s['no_aplicado'],
            round(s['venta'] - s['aplicado'] - s['promo'] - s['sin_aplicar']
                  - s['no_aplicado'], 2),
            s['comisiones'], s['mvat'], s['neto'],
            round(s['venta'] + s['comisiones'] + s['mvat'] - s['neto'], 2),
            a_float(info.get('total_amount')) if info else None,
        ])
    hoja(wb, 'Settlements',
         ['Settlement', 'Fecha deposito', 'Cobros del lote', 'Venta acreditada',
          'Aplicado a facturas', 'Redondeo (incluido en aplicado)',
          'Promocion asentada', 'Sin aplicar (excedente sin clasificar)',
          'Cobro no aplicado (sin factura / error)',
          'Control venta - aplicado - promo - sin aplicar - no aplicado', 'Comisiones',
          'IVA retenido', 'Deposito neto del lote', 'Control neto',
          'Deposito total del settlement (todas sus ordenes)'],
         filas_stl,
         monedas={'Venta acreditada', 'Aplicado a facturas',
                  'Redondeo (incluido en aplicado)', 'Promocion asentada',
                  'Sin aplicar (excedente sin clasificar)',
                  'Cobro no aplicado (sin factura / error)',
                  'Control venta - aplicado - promo - sin aplicar - no aplicado',
                  'Comisiones', 'IVA retenido', 'Deposito neto del lote',
                  'Control neto',
                  'Deposito total del settlement (todas sus ordenes)'})

    # --- Excepciones ---
    # ALREADY_ODOO_INVOICED y PENDING_WB_INVOICE no son excepciones: el espejo
    # trae las facturas que ya emitio el 1a1 (o una global), y las pendientes
    # quedan para la siguiente corrida. Se cuentan en el Resumen.
    no_excepcion = facturados + ('ALREADY_ODOO_INVOICED', 'PENDING_WB_INVOICE',
                                 'PENDING_B2B_LINK', 'SKIPPED_GRACE')
    filas_exc = []
    for b in billing:
        if b['status'] not in no_excepcion:
            filas_exc.append(['Facturacion', b['mkp_order_id'], b['odoo_so_name'] or '',
                              b['status'], b['error_log'] or ''])
    for c in cobros:
        if c['status'] != 'ODOO_PAID':
            filas_exc.append(['Cobro', c['mkp_order_id'], c['odoo_so_name'] or '',
                              c['status'], c['error_log'] or ''])
    cur_exc = db_conn.cursor(dictionary=True)
    for f in filas_fact:
        if f[17] not in ('paid', 'in_payment'):
            # Settlement de la ultima publicacion de la orden: casi siempre el
            # cobro vive en otro settlement (Amazon re-publico la pieza).
            cur_exc.execute("""
                SELECT a.settlement_id, MAX(e.deposit_date) AS deposito
                FROM somos_reyes.amz_transactions a
                LEFT JOIN somos_reyes.amz_settlement_report e ON e.settlement_id = a.settlement_id
                WHERE a.order_id = %s AND a.description = 'Order Payment'
                  AND a.transaction_status <> 'DEFERRED'
                GROUP BY a.settlement_id, a.posted_date
                ORDER BY a.posted_date DESC LIMIT 1""", (f[0],))
            ult = cur_exc.fetchone() or {}
            donde = (f"cobro en el settlement {ult.get('settlement_id')} "
                     f"({'depositado ' + str(ult['deposito'])[:10] if ult.get('deposito') else 'SIN fecha de deposito en Amazon'})"
                     if ult else 'sin cobro en Amazon')
            filas_exc.append(['Factura sin saldar', f[0], f[2], f[17],
                              f"{f[3]} saldo {a_float(f[16]):,.2f}; {donde}"])
    cur_exc.close()
    for f in filas_fact:
        if f[19] == 'Abierta':
            filas_exc.append([f'{ACCOUNT_MERC_NO_ENVIADA} abierta', f[0], f[2], f[19],
                              f"{f[3]}: hay salida de almacen y no quedo conciliada"])
    hoja(wb, 'Excepciones', ['Etapa', 'Amazon order_id', 'SO', 'Estado', 'Detalle'],
         filas_exc)

    # --- Casos pedidos por Finanzas ---
    if CASOS:
        b_por_orden = {b['mkp_order_id']: b for b in billing}
        c_por_orden = {}
        for c in cobros:
            c_por_orden.setdefault(c['mkp_order_id'], []).append(c)
        f_por_orden = {}
        for f in filas_fact:
            f_por_orden.setdefault(f[0], []).append(f)
        filas_casos = []
        for orden, caso in CASOS:
            orden = orden.strip()
            b = b_por_orden.get(orden) or {}
            so = b.get('odoo_so_name') or ''
            facts = f_por_orden.get(orden) or []
            todas = fact_por_so.get(so, [])
            cs = c_por_orden.get(orden, [])
            filas_casos.append([
                caso.strip(), orden, so, b.get('status', 'NO ESTA EN EL ALCANCE'),
                ', '.join(f['name'] for f in todas),
                sum(a_float(f['amount_total']) for f in todas) if todas else None,
                ', '.join(f"{c['settlement_id']} {c['status']}" for c in cs),
                ', '.join(f['payment_state'] for f in todas),
                ', '.join(f[19] for f in facts),
                b.get('error_log') or '',
                ' | '.join(c['error_log'] for c in cs if c['error_log']),
            ])
        hoja(wb, 'Casos pedidos',
             ['Caso', 'Amazon order_id', 'SO', 'Estado facturacion', 'Factura(s)',
              'Total factura(s)', 'Cobro(s)', 'Estado de pago',
              ACCOUNT_MERC_NO_ENVIADA, 'Nota facturacion', 'Nota cobro'],
             filas_casos, monedas={'Total factura(s)'})

    # --- Resumen ---
    estados = {}
    for b in billing:
        estados[b['status']] = estados.get(b['status'], 0) + 1
    n_b2b = sum(1 for f in filas_fact if f[1] == 'B2B')
    n_b2c = sum(1 for f in filas_fact if f[1] == 'B2C')
    pagadas = sum(1 for f in filas_fact if f[17] in ('paid', 'in_payment'))
    parciales = sum(1 for f in filas_fact if f[17] == 'partial')
    conc = {}
    for f in filas_fact:
        conc[f[19]] = conc.get(f[19], 0) + 1
    notas = ' '.join(b['error_log'] or '' for b in billing)
    n_canceladas = notas.count('SO_CANCELADA')
    n_confirmadas = notas.count('SO_CONFIRMADA')
    n_global = notas.count('En factura global')

    resumen['A1'] = 'Lote de pruebas Amazon -- facturacion y cobros'
    resumen['A1'].font = TITULO
    filas = [
        ('Generado', datetime.now().strftime('%Y-%m-%d %H:%M')),
        ('Entorno / base Odoo', f"{ENTORNO} / {ODOO_DB}"),
        ('Lote', f"settlement {AMZ_SETTLEMENT_ID} completo"
                 + (f" + {len(AMZ_ORDER_IDS)} ordenes sueltas" if AMZ_ORDER_IDS else '')
         if AMZ_SETTLEMENT_ID else f"filas creadas desde {DESDE}"),
        ('Facturas de esta corrida: procesadas desde (UTC)', DESDE),
        ('', ''),
        ('FACTURACION (01)', ''),
        ('Ordenes del lote en la tabla de control', len(billing)),
    ] + [(f"   {k}", v) for k, v in sorted(estados.items())] + [
        ('   (ALREADY_ODOO_INVOICED: ya tenian factura en el espejo, del 1a1 o '
         'de un lote anterior)', ''),
        ('   de ellas, en una factura global (no se refacturan)', n_global),
        ('Facturas creadas en esta corrida', len(filas_fact)),
        ('   B2B (replican el CFDI de Amazon)', n_b2b),
        ('   B2B iguales al CFDI al centavo', f"{b2b_iguales} de {b2b_total}"),
        ('   B2C (publico en general)', n_b2c),
        ('   con SO cancelada en Odoo (llevan NC por devoluciones)', n_canceladas),
        ('   con SO en borrador que se confirmo', n_confirmadas),
        (f'{ACCOUNT_MERC_NO_ENVIADA} de las facturas creadas', ''),
    ] + [(f"   {k}", v) for k, v in sorted(conc.items())] + [
        ('', ''),
        ('COBROS (02 + 03)', ''),
        ('Cobros del lote', len(cobros)),
        ('   (cobros de otras ordenes sembrados en la corrida, fuera del reporte)',
         len(otros_cobros)),
        ('   aplicados (ODOO_PAID)', sum(1 for c in cobros if c['status'] == 'ODOO_PAID')),
        ('Facturas en Pagado', pagadas),
        ('Facturas en Pagado parcial', parciales),
        ('Cobros con ajuste de redondeo (+-0.01)', n_redondeo),
        ('Promociones pagadas por Amazon asentadas', n_promo),
        ('Excedentes sin clasificar', n_sin_clasif),
        ('', ''),
        ('CUADRE GLOBAL DEL LOTE', ''),
        ('Venta acreditada por Amazon', tot['venta']),
        ('   Aplicado a facturas (incluye redondeo)', tot['aplicado']),
        ('   Promocion asentada (403.01.002 + 208.01.001)', tot['promo']),
        ('   Sin aplicar (excedente sin clasificar, queda en revision)', tot['sin_aplicar']),
        ('   Cobro no aplicado (sin factura o con error; se reintenta)', tot['no_aplicado']),
        ('   Control: venta - aplicado - promo - sin aplicar - no aplicado',
         round(tot['venta'] - tot['aplicado'] - tot['promo'] - tot['sin_aplicar']
               - tot['no_aplicado'], 2)),
        ('Redondeo neto contra ' + ACCOUNT_REDONDEO, tot['redondeo']),
        ('Comisiones Amazon', tot['comisiones']),
        ('IVA retenido por Amazon (marketplace_vat)', tot['mvat']),
        ('Deposito neto de las ordenes del lote', tot['neto']),
        ('', ''),
        ('FORMULA DEL PLAYBOOK (hoja Playbook)', ''),
        ('(iii) Promocion = lista neta + envio - pagado por el cliente', ''),
        ('   Lista neta', 'product_charges + our_price_tax + (promo_rebates - shipping_discount) + our_price_tax_disc'),
        ('   Envio', 'shipping_principal + shipping_tax + shipping_discount + shipping_tax_discount'),
        ('   Pagado por el cliente', 'Amazon OrderTotal (sin dato: no se clasifica)'),
        ('Se asienta solo si coinciden tres fuentes', '1) hay OrderTotal; 2) factura Odoo = OrderTotal (+-0.01); 3) excedente del cobro = (iii) (+-0.01) con la orden completa en el settlement'),
        ('Asiento', 'Debe 102.02.008 promo | Haber 403.01.002 promo/1.16 | Haber 208.01.001 IVA'),
    ]
    for i, (k, v) in enumerate(filas, start=3):
        resumen.cell(row=i, column=1, value=k)
        celda = resumen.cell(row=i, column=2, value=v)
        if isinstance(v, float):
            celda.number_format = MONEDA
        if k and k == k.upper() and not v:
            resumen.cell(row=i, column=1).font = Font(bold=True)
    resumen.column_dimensions['A'].width = 52
    resumen.column_dimensions['B'].width = 70

    db_conn.close()
    sello = datetime.now().strftime('%Y%m%d_%H%M')
    ruta = os.path.join(_dir, f'reporte_lote_amazon_{sello}.xlsx')
    wb.save(ruta)
    log.info(f"Reporte escrito: {ruta}")
    log.info(f"  facturas {len(filas_fact)} (B2B {n_b2b}, B2C {n_b2c}) | "
             f"B2B = CFDI {b2b_iguales}/{b2b_total} | cobros {len(cobros)} | "
             f"pagadas {pagadas} | parciales {parciales} | redondeos "
             f"{n_redondeo} | promos {n_promo} | sin clasificar {n_sin_clasif}")
    return ruta


if __name__ == '__main__':
    run()
