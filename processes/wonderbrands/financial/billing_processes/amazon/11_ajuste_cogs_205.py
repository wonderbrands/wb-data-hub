"""
11_ajuste_cogs_205.py
---------------------
CIERRE MENSUAL DE LA 205.06.003 (MERCANCIAS FACTURADAS - NO ENVIADAS)

Acuerdos con Finanzas (correos de Jero del 28 y 29-oct-2026). Cuando una SO
de Amazon ya se entrego y se facturo completa, lo que quede abierto en la
205.06.003 no es de tiempos: es una diferencia entre el costo que acredito la
factura y el que debito la salida (OUT). Este script, una vez al mes, la
lleva a 501.01.001 y deja factura, salida y ajuste en UNA conciliacion
completa (una linea por SO, aunque el asiento sea mensual).

Tres casos, cada uno con su etiqueta (+ " | SO | factura | salida"):

  COSTEO  Producto normal; el costo promedio cambio entre la factura y la
          salida. Prueba del 28-oct: +10% en SO10241518 -> 106.14.
          "Ajuste al COGS por diferencia de costeo de producto entre
           facturacion y salida"
  KIT     La factura lleva el kit y la salida sus componentes, y no suman
          lo mismo.
          "Ajuste al COGS por diferencia entre kit facturado y componentes
           despachados"
          Incluye la factura de kit que salio SIN costo (refacturas antes
          de la correccion del 29-oct en 01: SO10232549, SO10308861,
          SO10309861): el ajuste le asienta el costo de lo que salio.

Ademas genera el REPORTE DE KITS del mes (reporte_kits_<mes>.csv), para que
Finanzas lo trabaje con Catalogo y Operaciones:
  Diferencia de costo     salieron los componentes que pide la lista de
                          materiales, pero el costo del kit no es la suma de
                          sus componentes (ej. DNGSALVIA2-MAD).
  Diferencia de cantidad  salieron mas o menos componentes que los de la
                          lista (ej. SO10269094: 3 cajas de DNGLEX4-NEG#BOX1
                          cuando la orden pedia 1).
  Factura sin costo       la factura del kit no tenia linea de costo.

Solo SO del equipo de Amazon, entregadas y facturadas completas, con todas
sus partidas de la 205.06.003 dentro del mes. Lo demas es de tiempos y espera.

Uso:
    python 11_ajuste_cogs_205.py 2026-10          # mes a cerrar
    python 11_ajuste_cogs_205.py anterior         # el mes que acaba de cerrar
    python 11_ajuste_cogs_205.py 2026-10 --seco   # solo lista y reporte, no asienta
"""

import os
import sys
import csv
import logging
import calendar
from datetime import date, datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ACCOUNT_MERC_NO_ENVIADA, KESTRA_EXECUTION_ID,
    banner, validar_entorno, notify_slack, OdooModelProxy, OdooWriteUncertain,
    get_account_id, get_amazon_team_ids, a_float, ejecutar_sin_retorno,
)

ACCOUNT_COSTO_VENTA = os.getenv('AMZ_ACCOUNT_COSTO_VENTA', '501.01.001').strip()
# Diario del asiento. Vacio = el primer diario de tipo 'general' (Operaciones
# varias).
JOURNAL_AJUSTES = os.getenv('AMZ_JOURNAL_AJUSTES', '').strip()

ETIQUETAS = {
    'COSTEO': "Ajuste al COGS por diferencia de costeo de producto entre facturación y salida",
    'KIT': "Ajuste al COGS por diferencia entre kit facturado y componentes despachados",
}
PREFIJO_REF = 'AUTOCOGS:'

SECO = '--seco' in sys.argv
LOTE = 200


def fin_de_mes(mes):
    anio, m = (int(x) for x in mes.split('-'))
    return date(anio, m, calendar.monthrange(anio, m)[1])


def en_trozos(lista):
    for i in range(0, len(lista), LOTE):
        yield lista[i:i + LOTE]


def es_factura(linea):
    """Partida de factura o NC (lo demas en la 205.06.003 son salidas)."""
    return linea['move_id'][1].startswith(('INV/', 'RINV/'))


def candidatas(odoo, cuenta, equipos, fin):
    """Nombres de SO de Amazon con algo abierto en la 205.06.003 al cierre."""
    lineas = odoo.execute_kw(
        'account.move.line', 'search_read',
        [[('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
          ('reconciled', '=', False), ('date', '<=', fin.isoformat()),
          '|', ('move_id.team_id', 'in', equipos),
          ('move_id.stock_move_id.sale_line_id.order_id.team_id', 'in', equipos)]],
        {'fields': ['move_id']})
    movs = sorted({l['move_id'][0] for l in lineas})
    nombres = set()
    for trozo in en_trozos(movs):
        sms = []
        for m in odoo.execute_kw('account.move', 'read', [trozo],
                                 {'fields': ['invoice_origin', 'stock_move_id']}):
            if m.get('stock_move_id'):
                sms.append(m['stock_move_id'][0])
            elif m.get('invoice_origin') and ',' not in m['invoice_origin']:
                nombres.add(m['invoice_origin'])
        if not sms:
            continue
        sols = {sm['sale_line_id'][0] for sm in
                odoo.execute_kw('stock.move', 'read', [sms], {'fields': ['sale_line_id']})
                if sm.get('sale_line_id')}
        for sl in odoo.execute_kw('sale.order.line', 'read', [sorted(sols)],
                                  {'fields': ['order_id']}) if sols else []:
            nombres.add(sl['order_id'][1])
    log.info(f"{len(lineas)} partida(s) abiertas de {ACCOUNT_MERC_NO_ENVIADA} de Amazon al "
             f"{fin}: {len(nombres)} SO.")
    return sorted(nombres)


def so_completa(odoo, so_id):
    """Cada linea almacenable salio y se facturo completa.

    delivery_status='full' no basta: una SO con devolucion (WH/DEV) sigue en
    'full' aunque la linea quede con qty_delivered 0, y la devolucion acredita
    la 205.06.003 mientras espera su NC (dry-run del 29-oct: SO10163539,
    SO10243216, SO10202341). Eso es una NC pendiente, no costo.
    """
    for l in odoo.execute_kw('sale.order.line', 'search_read', [[('order_id', '=', so_id)]],
                             {'fields': ['product_id', 'product_uom_qty', 'qty_delivered',
                                         'qty_invoiced', 'display_type', 'is_service']}):
        if l['display_type'] or not l['product_id'] or l.get('is_service'):
            continue
        if l['qty_delivered'] != l['product_uom_qty'] or l['qty_invoiced'] != l['product_uom_qty']:
            return False
    return True


def tiene_kit(odoo, facturas, cache):
    """Alguna linea de las facturas es un producto con lista de materiales kit."""
    for l in odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', 'in', [f['id'] for f in facturas]),
                               ('display_type', '=', 'product'), ('product_id', '!=', False)]],
                             {'fields': ['product_id']}):
        pid = l['product_id'][0]
        if pid not in cache:
            tmpl = odoo.execute_kw('product.product', 'read', [[pid]],
                                   {'fields': ['product_tmpl_id']})[0]['product_tmpl_id'][0]
            cache[pid] = bool(odoo.execute_kw(
                'mrp.bom', 'search_count',
                [['|', ('product_id', '=', pid),
                  '&', ('product_id', '=', False), ('product_tmpl_id', '=', tmpl),
                  ('type', '=', 'phantom')]]))
        if cache[pid]:
            return True
    return False


def revisar_kit(odoo, so, facturas):
    """Tipo de diferencia de un kit: compara los componentes que pide la lista
    de materiales (por las piezas facturadas) contra los que salieron.

    Devuelve (tipo, detalle) con tipo 'Diferencia de costo' o 'Diferencia de
    cantidad'. La lista de materiales es la que uso la salida (bom_line_id del
    movimiento); si no la trae, la primera de tipo kit del producto.
    """
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', 'in', [f['id'] for f in facturas]),
                               ('display_type', '=', 'product')]],
                             {'fields': ['product_id', 'quantity']})
    movs = odoo.execute_kw('stock.move', 'search_read',
                           [[('sale_line_id.order_id', '=', so['id']), ('state', '=', 'done'),
                             ('picking_code', '=', 'outgoing')]],
                           {'fields': ['product_id', 'quantity', 'bom_line_id']})
    salio = {}
    for m in movs:
        k = m['product_id'][0]
        salio[k] = salio.get(k, 0.0) + a_float(m['quantity'])
    boms_usadas = set()
    for m in movs:
        if m.get('bom_line_id'):
            bl = odoo.execute_kw('mrp.bom.line', 'read', [[m['bom_line_id'][0]]], {'fields': ['bom_id']})[0]
            boms_usadas.add(bl['bom_id'][0])

    pide, kits = {}, []
    for l in lineas:
        if not l['product_id']:
            continue
        p = odoo.execute_kw('product.product', 'read', [[l['product_id'][0]]],
                            {'fields': ['product_tmpl_id', 'default_code', 'standard_price']})[0]
        boms = odoo.execute_kw('mrp.bom', 'search_read',
                               [['|', ('product_id', '=', l['product_id'][0]),
                                 '&', ('product_id', '=', False),
                                 ('product_tmpl_id', '=', p['product_tmpl_id'][0]),
                                 ('type', '=', 'phantom')]],
                               {'fields': ['id', 'product_qty', 'bom_line_ids']})
        if not boms:
            continue
        # La lista que uso la salida; si la salida no la trae, la que mas
        # componentes comparte con lo que salio (un kit puede tener 3 o 4).
        bom = next((b for b in boms if b['id'] in boms_usadas), None)
        if not bom:
            def comunes(b):
                prods = {x['product_id'][0] for x in odoo.execute_kw(
                    'mrp.bom.line', 'read', [b['bom_line_ids']], {'fields': ['product_id']})}
                return len(prods & set(salio))
            bom = max(boms, key=comunes)
        kits.append(f"{p['default_code']} x{a_float(l['quantity']):g}")
        for bl in odoo.execute_kw('mrp.bom.line', 'read', [bom['bom_line_ids']],
                                  {'fields': ['product_id', 'product_qty']}):
            k = bl['product_id'][0]
            pide[k] = pide.get(k, 0.0) + a_float(bl['product_qty']) * a_float(l['quantity']) \
                / (a_float(bom['product_qty']) or 1)

    codigos = {}
    for k in set(pide) | set(salio):
        codigos[k] = odoo.execute_kw('product.product', 'read', [[k]],
                                     {'fields': ['default_code']})[0]['default_code']
    distintos = [k for k in set(pide) | set(salio)
                 if round(pide.get(k, 0.0), 4) != round(salio.get(k, 0.0), 4)]
    comp = '; '.join(f"{codigos[k]}: pide {pide.get(k, 0):g}, salio {salio.get(k, 0):g}"
                     for k in sorted(set(pide) | set(salio), key=lambda x: codigos[x] or ''))
    tipo = 'Diferencia de cantidad' if distintos else 'Diferencia de costo'
    return tipo, ', '.join(kits), comp


def run(mes):
    fin = fin_de_mes(mes)
    odoo = OdooModelProxy(timeout=300)
    cuenta = get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA)
    costo = get_account_id(odoo, ACCOUNT_COSTO_VENTA)
    equipos = get_amazon_team_ids(odoo)

    nombres = candidatas(odoo, cuenta, equipos, fin)
    ajustes, reporte, kits_cache = [], [], {}
    omitidas = {'devolucion o entrega incompleta': 0, 'factura global u otra': 0,
                'salida fuera de la 205.06.003': 0, 'productos distintos sin kit': 0}
    for trozo in en_trozos(nombres):
        # Entregadas y facturadas completas: si no, lo abierto es de tiempos.
        for so in odoo.execute_kw('sale.order', 'search_read',
                                  [[('name', 'in', trozo), ('team_id', 'in', equipos),
                                    ('state', '=', 'sale'), ('invoice_status', '=', 'invoiced'),
                                    ('delivery_status', '=', 'full')]],
                                  {'fields': ['id', 'name', 'picking_ids']}):
            todas = odoo.execute_kw(
                'account.move.line', 'search_read',
                [[('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
                  '|', ('move_id.stock_move_id.sale_line_id.order_id', '=', so['id']),
                  ('move_id.invoice_origin', '=', so['name'])]],
                {'fields': ['id', 'move_id', 'product_id', 'date', 'amount_residual',
                            'reconciled', 'balance']})
            if any(l['date'] > fin.isoformat() for l in todas):
                continue                       # algo es del mes siguiente: espera
            if not so_completa(odoo, so['id']):
                omitidas['devolucion o entrega incompleta'] += 1
                continue
            abiertas = [l for l in todas if not l['reconciled']]
            residuo = round(sum(a_float(l['amount_residual']) for l in abiertas), 2)
            if not abiertas or abs(residuo) < 0.005:
                continue

            facturas = odoo.execute_kw(
                'account.move', 'search_read',
                [[('invoice_origin', '=', so['name']), ('move_type', '=', 'out_invoice'),
                  ('state', '=', 'posted')]], {'fields': ['id', 'name']})
            salidas = odoo.execute_kw(
                'stock.picking', 'search_read',
                [[('id', 'in', so['picking_ids']), ('picking_type_code', '=', 'outgoing'),
                  ('state', '=', 'done')]], {'fields': ['name']})
            de_factura = {l['product_id'][0] for l in todas if l['product_id'] and es_factura(l)}
            de_salida = {l['product_id'][0] for l in todas if l['product_id'] and not es_factura(l)}
            # Solo se ajusta lo que se puede explicar. Dry-run del 29-oct: sin
            # estas reglas salian 969 SO por -949,843.87, casi todas facturas
            # del 1a1 (jun-sep) cuya SALIDA no esta en la 205.06.003: no es una
            # diferencia de costo, es un hueco historico. Se cuenta aparte.
            if not facturas:
                omitidas['factura global u otra'] += 1     # sin factura propia de la SO
                continue
            if not de_salida:
                omitidas['salida fuera de la 205.06.003'] += 1
                continue
            if de_factura == de_salida:
                caso = 'COSTEO'
            elif tiene_kit(odoo, facturas, kits_cache):
                caso = 'KIT'                               # incluye factura de kit sin costo
            else:
                omitidas['productos distintos sin kit'] += 1
                continue
            nombres_fac = ', '.join(f['name'] for f in facturas) or 'sin factura'
            nombres_sal = ', '.join(p['name'] for p in salidas) or 'sin salida'
            ajustes.append({
                'so': so['name'], 'caso': caso, 'residuo': residuo,
                'partidas': [l['id'] for l in abiertas],
                'etiqueta': ' | '.join([ETIQUETAS[caso], so['name'], nombres_fac, nombres_sal]),
            })
            if caso == 'KIT':
                tipo, kits, comp = revisar_kit(odoo, so, facturas)
                facturado = round(-sum(a_float(l['balance']) for l in todas if es_factura(l)), 2)
                despachado = round(sum(a_float(l['balance']) for l in todas if not es_factura(l)), 2)
                if not de_factura:
                    tipo = 'Factura sin costo'
                reporte.append([mes, tipo, so['name'], nombres_fac, nombres_sal, kits, comp,
                                facturado, despachado, residuo])

    log.info('')
    log.info(f"SO completas con algo abierto que NO se ajustan: "
             f"{', '.join(f'{k} {v}' for k, v in omitidas.items())}")
    log.info(f"SO a ajustar: {len(ajustes)} | neto {sum(a['residuo'] for a in ajustes):,.2f}")
    for a in ajustes:
        log.info(f"  {a['residuo']:>10,.2f}  {a['etiqueta']}")

    if reporte:
        ruta = os.path.join(_dir, f'reporte_kits_{mes}.csv')
        with open(ruta, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['Mes', 'Tipo', 'SO', 'Factura(s)', 'Salida(s)', 'Kit facturado',
                        'Componentes (pide la lista / salio)', 'Costo facturado',
                        'Costo de lo que salio', 'Ajuste a 501.01.001'])
            w.writerows(reporte)
        log.info(f"REPORTE DE KITS: {ruta}")
        for r in reporte:
            log.info(f"  {r[1]:<22} {r[2]}  {r[5]}  {r[6]}  ajuste {r[9]:,.2f}")
        notify_slack(f":package: *{len(reporte)} kit(s) de Amazon con diferencia en {mes}* "
                     f"({sum(1 for r in reporte if r[1] == 'Diferencia de cantidad')} de cantidad, "
                     f"{sum(1 for r in reporte if r[1] == 'Diferencia de costo')} de costo, "
                     f"{sum(1 for r in reporte if r[1] == 'Factura sin costo')} sin costo). "
                     f"Reporte: reporte_kits_{mes}.csv")

    if SECO or not ajustes:
        log.info('EN SECO: no se asento nada.' if SECO else 'Nada que ajustar.')
        return

    diario = odoo.execute_kw('account.journal', 'search',
                             [[('code', '=', JOURNAL_AJUSTES)] if JOURNAL_AJUSTES
                              else [('type', '=', 'general')]], {'limit': 1})
    if not diario:
        raise Exception(f"No hay diario de ajustes ({JOURNAL_AJUSTES or 'tipo general'}).")

    # Un asiento al cierre del mes, dos lineas por SO. El residuo de la
    # 205.06.003 es deudor cuando la salida costo mas que lo facturado: se
    # acredita la 205.06.003 y se carga el costo de venta (y al reves).
    lineas = []
    for a in ajustes:
        r = a['residuo']
        lineas += [
            (0, 0, {'account_id': cuenta, 'name': a['etiqueta'],
                    'debit': -r if r < 0 else 0.0, 'credit': r if r > 0 else 0.0}),
            (0, 0, {'account_id': costo, 'name': a['etiqueta'],
                    'debit': r if r > 0 else 0.0, 'credit': -r if r < 0 else 0.0}),
        ]
    ref = f"{PREFIJO_REF}{mes}:{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        move_id = odoo.execute_kw('account.move', 'create', [{
            'move_type': 'entry', 'journal_id': diario[0], 'date': fin.isoformat(),
            'ref': ref, 'line_ids': lineas}])
    except OdooWriteUncertain:
        ya = odoo.execute_kw('account.move', 'search', [[('ref', '=', ref)]])
        if not ya:
            raise
        move_id = ya[0]
    odoo.execute_kw('account.move', 'action_post', [[move_id]])
    nombre = odoo.execute_kw('account.move', 'read', [[move_id]], {'fields': ['name']})[0]['name']
    log.info(f"Asiento {nombre} ({ref}) con {len(ajustes)} SO.")

    # Cada linea del ajuste en la 205.06.003 contra lo abierto de su SO.
    nuevas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', move_id), ('account_id', '=', cuenta)]],
                             {'fields': ['id', 'name']})
    por_etiqueta = {l['name']: l['id'] for l in nuevas}
    completas = 0
    for a in ajustes:
        linea = por_etiqueta.get(a['etiqueta'])
        ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', a['partidas'] + [linea])
        estado = odoo.execute_kw('account.move.line', 'read', [a['partidas'] + [linea]],
                                 {'fields': ['reconciled']})
        if all(e['reconciled'] for e in estado):
            completas += 1
        else:
            log.warning(f"  {a['so']}: la conciliacion no quedo completa; revisar.")
    try:
        odoo.execute_kw('account.move', 'message_post', [move_id], {
            'body': (f"CIERRE MENSUAL DE LA {ACCOUNT_MERC_NO_ENVIADA} ({mes})\n"
                     f"{sum(1 for a in ajustes if a['caso'] == 'COSTEO')} SO por diferencia de costeo; "
                     f"{sum(1 for a in ajustes if a['caso'] == 'KIT')} por kit.\n"
                     f"Conciliaciones completas: {completas} de {len(ajustes)}.\n"
                     f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}"),
            'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo postear el chatter: {e}")
    log.info(f"Conciliaciones completas: {completas} de {len(ajustes)}.")


if __name__ == '__main__':
    banner('CIERRE MENSUAL DE LA 205.06.003 -- AMAZON')
    validar_entorno()
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    mes = args[0] if args else date.today().strftime('%Y-%m')
    # 'anterior': el mes que acaba de cerrar (lo usa el flujo de Kestra el dia 1).
    if mes == 'anterior':
        hoy = date.today()
        mes = (date(hoy.year - 1, 12, 1) if hoy.month == 1
               else date(hoy.year, hoy.month - 1, 1)).strftime('%Y-%m')
    run(mes)
