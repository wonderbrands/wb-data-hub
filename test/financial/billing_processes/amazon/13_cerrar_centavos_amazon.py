"""
13_cerrar_centavos_amazon.py
----------------------------
Cierra contra 704.23.001 los centavos que dejo abiertos el saneamiento
(12_sanear_facturas_amazon.py) en la CxC de Amazon (105.01.006).

POR QUE EXISTE
--------------
En la operacion normal el centavo no queda abierto: 03 registra el pago por lo
que libero Amazon y el asistente de pago salda la diferencia de +-0.01 contra
704.23.001 en el mismo pago (payment_difference_handling='reconcile').

El 12 corrigio facturas que YA tenian el cobro aplicado. Al quitar el ajuste de
centavo la factura volvio al importe de Odoo (1,699.00 -> 1,699.01) y el 12
reconcilio el cobro viejo directo, sin el write-off del asistente:

    INV/2026/164230  factura 1,699.01  cobro 1,699.00  -> parcial, saldo 0.01
    INV/2026/163598  factura 2,998.99  cobro 2,999.00  -> pagada, 0.01 del pago sin aplicar

QUE HACE
--------
Busca en la 105.01.006 las partidas publicadas, no conciliadas, con saldo de
+-0.01 y que ya estan conciliadas en parte (una factura con su cobro, o un
cobro con su factura). Un saldo de un centavo sin conciliacion parcial no es
redondeo y no se toca. La 105.01.006 es solo de Amazon.

Por cada una agrega una linea de la 105.01.006 al asiento de redondeo (contra
704.23.001) y la concilia con la partida abierta. La factura queda "Pagado" y
el cobro sin saldo. Un asiento por fecha (la de la partida abierta), en el
diario general, con ref AUTOREDONDEO:<fecha>:<ts>.

Correrlo DESPUES de que termine el 12 (el 12 sigue creando estos casos en el
bloque B) y con el flujo de Kestra sin ejecucion en curso.

Uso (AMZ_ENTORNO=prod):
    python 13_cerrar_centavos_amazon.py                       # en seco: lista y CSV
    python 13_cerrar_centavos_amazon.py --aplicar --limite 5
    python 13_cerrar_centavos_amazon.py --aplicar

AMZ_FECHA_REDONDEO=AAAA-MM-DD fuerza la fecha de los asientos (por ejemplo si
Contabilidad ya bloqueo el periodo de la partida abierta).
"""

import os
import sys
import csv
import logging
from datetime import datetime
from collections import defaultdict

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ACCOUNT_AMAZON_CXC, ACCOUNT_REDONDEO,
    banner, validar_entorno, OdooModelProxy, OdooWriteUncertain,
    get_account_id, a_float, ejecutar_sin_retorno,
)

APLICAR = '--aplicar' in sys.argv
LIMITE = int(sys.argv[sys.argv.index('--limite') + 1]) if '--limite' in sys.argv else None
# Desde el arranque de 01 en produccion. Lo anterior lo cerro el 1a1 o Contabilidad.
DESDE = os.getenv('AMZ_SANEO_DESDE', '2026-10-30 00:00:00')
FECHA_FORZADA = os.getenv('AMZ_FECHA_REDONDEO', '').strip() or None
JOURNAL_AJUSTES = os.getenv('AMZ_JOURNAL_AJUSTES', '').strip()
# Hasta $0.15 por partida: lo que aprobo Finanzas el 8-nov-2026 (igual que 03).
# Cierra tambien las facturas de la hoja A3 (diferencias de 0.02 a 0.06 contra
# OrderTotal) que ya tienen el cobro aplicado.
TOLERANCIA = 0.15
POR_ASIENTO = 200
PREFIJO_REF = 'AUTOREDONDEO:'
CSV_SALIDA = os.path.join(_dir, f"centavos_amazon_{datetime.now():%Y%m%d_%H%M}.csv")


def partidas_abiertas(odoo, cxc):
    domain = [('account_id', '=', cxc), ('parent_state', '=', 'posted'),
              ('reconciled', '=', False), ('amount_residual', '!=', 0),
              ('amount_residual', '>=', -TOLERANCIA), ('amount_residual', '<=', TOLERANCIA),
              ('create_date', '>=', DESDE)]
    filas = odoo.execute_kw('account.move.line', 'search_read', [domain],
                            {'fields': ['id', 'move_id', 'move_name', 'date', 'partner_id',
                                        'amount_residual', 'debit', 'credit',
                                        'matched_debit_ids', 'matched_credit_ids'],
                             'order': 'date, id'})
    # Solo las conciliadas en parte: factura con cobro aplicado o cobro aplicado
    # a una factura. Esas son las que dejo el 12.
    return [f for f in filas if f['matched_debit_ids'] or f['matched_credit_ids']]


def asentar(odoo, diario, cuenta_cxc, cuenta_redondeo, fecha, grupo):
    """Un asiento con una linea de la 105.01.006 por partida abierta y el total
    contra 704.23.001. Devuelve (move_id, nombre, {etiqueta: linea_id})."""
    lineas, total = [], 0.0
    for p in grupo:
        r = round(a_float(p['amount_residual']), 2)   # + factura con saldo / - cobro sobrante
        total = round(total + r, 2)
        lineas.append((0, 0, {
            'account_id': cuenta_cxc,
            'partner_id': p['partner_id'][0] if p['partner_id'] else False,
            'name': p['etiqueta'],
            'debit': -r if r < 0 else 0.0, 'credit': r if r > 0 else 0.0}))
    if total:
        lineas.append((0, 0, {
            'account_id': cuenta_redondeo, 'name': 'Redondeo Amazon (saneamiento)',
            'debit': total if total > 0 else 0.0, 'credit': -total if total < 0 else 0.0}))
    else:
        # Los centavos de un lado y del otro se compensan: igual hace falta una
        # contrapartida para que el asiento cuadre. Una linea en cero no se
        # admite, asi que se divide en dos lineas de redondeo opuestas.
        lineas += [(0, 0, {'account_id': cuenta_redondeo, 'name': 'Redondeo Amazon (saneamiento)',
                           'debit': 0.01, 'credit': 0.0}),
                   (0, 0, {'account_id': cuenta_redondeo, 'name': 'Redondeo Amazon (saneamiento)',
                           'debit': 0.0, 'credit': 0.01})]
    ref = f"{PREFIJO_REF}{fecha}:{datetime.now():%Y%m%d%H%M%S%f}"
    try:
        move_id = odoo.execute_kw('account.move', 'create', [{
            'move_type': 'entry', 'journal_id': diario, 'date': fecha,
            'ref': ref, 'line_ids': lineas}])
    except OdooWriteUncertain:
        ya = odoo.execute_kw('account.move', 'search', [[('ref', '=', ref)]])
        if not ya:
            raise
        move_id = ya[0]
    odoo.execute_kw('account.move', 'action_post', [[move_id]])
    nombre = odoo.execute_kw('account.move', 'read', [[move_id]], {'fields': ['name']})[0]['name']
    nuevas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', move_id), ('account_id', '=', cuenta_cxc)]],
                             {'fields': ['id', 'name']})
    return move_id, nombre, {l['name']: l['id'] for l in nuevas}


def estado_documento(odoo, move_id):
    m = odoo.execute_kw('account.move', 'read', [[move_id]],
                        {'fields': ['move_type', 'payment_state', 'amount_residual']})[0]
    if m['move_type'] == 'out_invoice':
        return f"factura {m['payment_state']} saldo {a_float(m['amount_residual']):.2f}"
    return 'cobro sin saldo'


def run():
    odoo = OdooModelProxy(timeout=300)
    cuenta_cxc = get_account_id(odoo, ACCOUNT_AMAZON_CXC)
    if not ACCOUNT_REDONDEO:
        raise Exception("AMZ_ACCOUNT_REDONDEO vacio.")
    cuenta_redondeo = get_account_id(odoo, ACCOUNT_REDONDEO)

    abiertas = partidas_abiertas(odoo, cuenta_cxc)
    if LIMITE:
        abiertas = abiertas[:LIMITE]
    for p in abiertas:
        r = a_float(p['amount_residual'])
        p['lado'] = 'FACTURA CON SALDO' if r > 0 else 'COBRO SOBRANTE'
        p['etiqueta'] = f"Redondeo Amazon {p['move_name']} #{p['id']}"
    log.info(f"{len(abiertas)} partida(s) de la {ACCOUNT_AMAZON_CXC} con +-{TOLERANCIA} abierto "
             f"desde {DESDE}: {sum(1 for p in abiertas if p['lado'] == 'FACTURA CON SALDO')} "
             f"factura(s) con saldo, {sum(1 for p in abiertas if p['lado'] == 'COBRO SOBRANTE')} "
             f"cobro(s) con sobrante.")

    salida = []
    if not APLICAR or not abiertas:
        for p in abiertas:
            salida.append([p['lado'], p['move_name'], p['date'],
                           f"{a_float(p['amount_residual']):+.2f}", '', 'en seco'])
    else:
        diario = odoo.execute_kw('account.journal', 'search',
                                 [[('code', '=', JOURNAL_AJUSTES)] if JOURNAL_AJUSTES
                                  else [('type', '=', 'general')]], {'limit': 1})
        if not diario:
            raise Exception(f"No hay diario de ajustes ({JOURNAL_AJUSTES or 'tipo general'}).")
        por_fecha = defaultdict(list)
        for p in abiertas:
            por_fecha[FECHA_FORZADA or str(p['date'])].append(p)

        for fecha, todas in sorted(por_fecha.items()):
            for i in range(0, len(todas), POR_ASIENTO):
                grupo = todas[i:i + POR_ASIENTO]
                try:
                    move_id, nombre, lineas = asentar(odoo, diario[0], cuenta_cxc,
                                                      cuenta_redondeo, fecha, grupo)
                except Exception as e:
                    log.error(f"Asiento del {fecha} rechazado: {e}")
                    for p in grupo:
                        salida.append([p['lado'], p['move_name'], p['date'],
                                       f"{a_float(p['amount_residual']):+.2f}", '',
                                       f"ERROR asiento: {str(e)[:200]}"])
                    continue
                log.info(f"Asiento {nombre} del {fecha}: {len(grupo)} partida(s).")
                for p in grupo:
                    linea = lineas.get(p['etiqueta'])
                    try:
                        ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile',
                                             [p['id'], linea])
                        ok = odoo.execute_kw('account.move.line', 'read', [[p['id']]],
                                             {'fields': ['reconciled']})[0]['reconciled']
                        res = (estado_documento(odoo, p['move_id'][0]) if ok
                               else 'NO QUEDO CONCILIADA: revisar')
                    except Exception as e:
                        res = f"ERROR conciliando: {str(e)[:200]}"
                    salida.append([p['lado'], p['move_name'], p['date'],
                                   f"{a_float(p['amount_residual']):+.2f}", nombre, res])

    with open(CSV_SALIDA, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['Caso', 'Documento', 'Fecha', 'Centavo abierto', 'Asiento de redondeo',
                    'Resultado'])
        w.writerows(salida)

    malas = [s for s in salida if s[5].startswith(('ERROR', 'NO QUEDO'))
             or 'partial' in s[5]]
    log.info('')
    log.info('=' * 82)
    log.info(f"CENTAVOS AMAZON {'CERRADOS' if APLICAR else '(EN SECO)'}: {len(salida)} partida(s), "
             f"{len(malas)} con problema")
    log.info(f"  CSV: {CSV_SALIDA}")
    log.info('=' * 82)
    for s in malas[:20]:
        log.info(f"  {s[1]}: {s[5]}")


if __name__ == '__main__':
    banner('CIERRE DE CENTAVOS -- CxC AMAZON (105.01.006)')
    validar_entorno()
    run()
