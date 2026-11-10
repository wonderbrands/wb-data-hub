"""
06_classifier_amz_refunds.py
----------------------------
CLASIFICADOR DE DEVOLUCIONES AMAZON (fase 3 de notas de credito)

Toma las devoluciones que dejo 04_miner_amz_refunds.py y decide, para cada
una, su tipo (A/B/C), su canal (B2B/B2C) y si ya se puede emitir la nota de
credito. No toca Odoo: solo MySQL. La NC la crea 07, que revalida todo en
Odoo antes de crear.

ENTRADA
  finance.mkp_refunds_{staging|prod}       filas en DETECTED, NO_INVOICE, NO_SO
                                           o WAITING_B2B_CFDI (se reevaluan)
  somos_reyes.odoo_new_sale_order_live     SO de la orden (Odoo 15 y 18)
  odoo18.live_psql_sale_order / _stock_picking(_type)   salidas y retornos
  finance.mkp_billing_*                    estado de facturacion de la orden
  somos_reyes.amz_b2b_invoices             CFDI de Amazon Business (I y E)
  somos_reyes.amz_financial_transactions_master + amz_transaction_items
                                           importe de la pieza vendida

SALIDA (por fila)
  refund_type, refund_scope, has_out_validated, has_physical_return,
  classification_reason, original_amount, odoo_so_name, cfdi_uuid (egreso de
  Amazon en B2B), cfdi_uuid_related, raw_json.clasificacion y status:

  CLASSIFIED        lista para 07
  DETECTED          sigue pendiente: la venta de la pieza aun no esta liberada
                    en Amazon y no se puede comparar el importe. Se reevalua.
  NO_SO             la orden no tiene SO en Odoo (Yuju). Se reevalua.
  NO_INVOICE        la venta aun no tiene factura nuestra. Se reevalua.
  WAITING_B2B_CFDI  venta con CFDI de Amazon; falta su egreso. Se reevalua.
  MANUAL_REVIEW     no se emite sola. Para reintentar: regresarla a DETECTED.

REGLAS A/B/C  >>> SUPUESTOS, PENDIENTES DE CONFIRMAR CON FINANZAS <<<
  C  DisbursementOnlyRefund (Amazon lo marca), o producto que salio y cuyo
     reembolso es menor que la pieza (criterio de unidades enteras del
     Playbook de Soporte de Ventas v2.6, seccion 5.3).
  A  la SO no tiene salida de almacen hecha (Odoo 18: OUT en 'done';
     SO de Odoo 15: sin fecha efectiva).
  B  hay salida y se reembolsa la pieza completa.
  ChargebackRefund y las devoluciones FBA siguen el mismo arbol. FBA si
  registra salida en Odoo (FBA/OUT) pero nunca el retorno: has_physical_return
  queda en 0 y la liberacion de la 107.06.001 esta por definir.

CANAL
  B2B solo si la venta esta en ODOO_B2B_LINKED (la factura de Odoo es el CFDI
  de Amazon): la NC replica el egreso de Amazon, cruzado por orden y total
  como en 05. Orden con CFDI de Amazon cuya factura en Odoo es a publico
  general (doble CFDI) -> MANUAL_REVIEW: el egreso de Amazon revierte un CFDI
  que no esta en Odoo.
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
for _p in (_dir, os.path.normpath(os.path.join(_dir, '..', '..', 'billing_processes', 'amazon'))):
    if os.path.exists(os.path.join(_p, '_amz_common.py')):
        if _p not in sys.path:
            sys.path.append(_p)
        break

from _amz_common import (
    ENTORNO, TBL_BILLING, MARKETPLACE,
    banner, notify_slack, get_db_connection, a_float,
)

TBL_REFUNDS = f'finance.mkp_refunds_{ENTORNO}'


# Tabla de facturacion a consultar. En pruebas el Odoo es una copia de
# produccion, asi que las facturas de las ordenes reales estan en
# mkp_billing_prod y no en _staging: se puede apuntar alla (solo lectura).
TBL_BILLING_NC = os.getenv('AMZ_REFUNDS_TBL_BILLING', TBL_BILLING).strip()
if TBL_BILLING_NC not in ('finance.mkp_billing_staging', 'finance.mkp_billing_prod'):
    raise Exception(f"AMZ_REFUNDS_TBL_BILLING invalida: '{TBL_BILLING_NC}'")

DRY_RUN = os.getenv('AMZ_REFUNDS_DRY_RUN', 'false').lower().strip() in ('1', 'true', 'si')

REEVALUABLES = ('DETECTED', 'NO_INVOICE', 'NO_SO', 'WAITING_B2B_CFDI')
FACTURADAS = ('ODOO_INVOICED', 'ODOO_B2B_LINKED', 'ALREADY_ODOO_INVOICED')

# Reembolso >= 99 % de la pieza = pieza completa (Playbook: tolerancia 1 %).
TOLERANCIA_PIEZA = 0.01
# La venta de una pieza devuelta puede ser de meses atras (hay devoluciones
# de octubre sobre ventas de diciembre).
VENTA_LOOKBACK_DAYS = 400


def leer(db_conn, sql, params=()):
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    return rows


def leer_en_lotes(db_conn, sql, valores, extra=(), lote=500):
    """Ejecuta sql (con un {marcas} para el IN) por lotes de valores."""
    rows, valores = [], sorted(valores)
    for i in range(0, len(valores), lote):
        parte = valores[i:i + lote]
        rows += leer(db_conn, sql.format(marcas=','.join(['%s'] * len(parte))),
                     list(extra) + parte)
    return rows


# =============================================================================
# CONTEXTO: todo lo que se necesita de MySQL, leido una sola vez
# =============================================================================

def elegir_so(candidatas, so_facturada):
    """La SO que facturo 01 si existe; si no, Odoo 18 antes que Odoo 15
    (duplicados de la migracion), vigente antes que cancelada, la mas nueva."""
    for s in candidatas:
        if s['name'] == so_facturada:
            return s
    return max(candidatas, key=lambda s: (s['name'].startswith(('SO10', 'S0')),
                                          s['state'] != 'cancel', s['id']))


def cargar_contexto(db_conn, refunds):
    ordenes = {r['mkp_order_id'] for r in refunds}
    piezas = {r['pieza'] for r in refunds}

    billing = {b['mkp_order_id']: b for b in leer_en_lotes(db_conn, f"""
        SELECT mkp_order_id, status, odoo_so_name, cfdi_uuid FROM {TBL_BILLING_NC}
        WHERE marketplace = '{MARKETPLACE}' AND mkp_order_id IN ({{marcas}})""", ordenes)}

    candidatas = {}
    for s in leer_en_lotes(db_conn, """
            SELECT id, name, state, effective_date, channel_order_reference AS orden
            FROM somos_reyes.odoo_new_sale_order_live
            WHERE channel_order_reference IN ({marcas})""", ordenes):
        candidatas.setdefault(s['orden'], []).append(s)
    so = {o: elegir_so(c, (billing.get(o) or {}).get('odoo_so_name'))
          for o, c in candidatas.items()}

    # Salidas y retornos, solo Odoo 18 (la replica odoo18 no tiene Odoo 15).
    nombres = {s['name'] for s in so.values()}
    id_odoo = {s['id']: s['name'] for s in leer_en_lotes(db_conn, """
        SELECT id, name FROM odoo18.live_psql_sale_order WHERE name IN ({marcas})""", nombres)}
    movimientos = {}
    for p in leer_en_lotes(db_conn, """
            SELECT p.sale_id, p.return_id, t.code, t.sequence_code
            FROM odoo18.live_psql_stock_picking p
            JOIN odoo18.live_psql_stock_picking_type t ON t.id = p.picking_type_id
            WHERE p.state = 'done' AND p.sale_id IN ({marcas})""", set(id_odoo)):
        m = movimientos.setdefault(id_odoo[p['sale_id']], {'salida': False, 'retorno': False})
        if p['return_id'] or p['code'] == 'incoming' or p['sequence_code'] in ('DEV', 'RET'):
            m['retorno'] = True
        elif p['code'] == 'outgoing':
            m['salida'] = True

    cfdis = {}
    for c in leer(db_conn, """
            SELECT order_id, receipt_type, uuid, total, date
            FROM somos_reyes.amz_b2b_invoices ORDER BY date"""):
        cfdis.setdefault(c['order_id'], {'I': [], 'E': []}).setdefault(
            c['receipt_type'], []).append(c)

    # Egresos ya asignados a otra devolucion: cada CFDI se usa una sola vez.
    egresos_usados = {u['cfdi_uuid'] for u in leer(db_conn, f"""
        SELECT cfdi_uuid FROM {TBL_REFUNDS}
        WHERE marketplace = %s AND cfdi_uuid IS NOT NULL
          AND status NOT IN {REEVALUABLES}""", (MARKETPLACE,))}

    # Importe de la pieza vendida, con la formula de 01. La master da la
    # transaccion de la pieza (tiene indice por fecha); el detalle sale de
    # amz_transaction_items, que si trae our_price_discount.
    desde = min(r['fecha'] for r in refunds) - timedelta(days=VENTA_LOOKBACK_DAYS)
    tx_venta = {}
    for v in leer_en_lotes(db_conn, """
            SELECT shipment_id, transaction_id, posted_date
            FROM somos_reyes.amz_financial_transactions_master
            WHERE description = 'Order Payment' AND posted_date >= %s
              AND shipment_id IN ({marcas})""", piezas, extra=(desde,)):
        if v['shipment_id'] not in tx_venta or v['posted_date'] > tx_venta[v['shipment_id']][1]:
            tx_venta[v['shipment_id']] = (v['transaction_id'], v['posted_date'])
    total_tx = {t['transaction_id']: a_float(t['total']) for t in leer_en_lotes(db_conn, """
        SELECT transaction_id,
               SUM(IFNULL(product_charges,0) + IFNULL(shipping,0) + IFNULL(shipping_discount,0)
                 + IFNULL(our_price_discount,0) + IFNULL(our_price_tax,0) + IFNULL(shipping_tax,0)
                 + IFNULL(shipping_tax_discount,0) + IFNULL(our_price_tax_disc,0)) AS total
        FROM somos_reyes.amz_transaction_items
        WHERE transaction_id IN ({marcas}) GROUP BY transaction_id""",
        {t[0] for t in tx_venta.values()})}
    venta_pieza = {pieza: total_tx.get(t[0]) for pieza, t in tx_venta.items()}

    # Lo devuelto por pieza, sumando todas sus devoluciones (ya clasificadas o no).
    devuelto = {d['pieza']: a_float(d['total']) for d in leer_en_lotes(db_conn, f"""
        SELECT ml_claim_id AS pieza, SUM(refund_amount) AS total FROM {TBL_REFUNDS}
        WHERE marketplace = '{MARKETPLACE}' AND ml_transaction_type <> 'DisbursementOnlyRefund'
          AND ml_claim_id IN ({{marcas}}) GROUP BY ml_claim_id""", piezas)}

    return {'billing': billing, 'so': so, 'movimientos': movimientos, 'cfdis': cfdis,
            'egresos_usados': egresos_usados, 'venta_pieza': venta_pieza, 'devuelto': devuelto}


# =============================================================================
# CLASIFICACION
# =============================================================================

def clasificar(r, ctx):
    """Devuelve la clasificacion de una devolucion. No escribe."""
    orden, total = r['mkp_order_id'], a_float(r['refund_amount'])
    c = {'status': 'CLASSIFIED', 'tipo': None, 'alcance': None, 'salida': None,
         'retorno': None, 'motivo': '', 'venta_pieza': None, 'so': None,
         'canal': 'B2C', 'egreso': None, 'uuid_factura': None, 'billing': None}

    def salir(status, motivo):
        c['status'], c['motivo'] = status, (c['motivo'] + ' | ' + motivo).strip(' |')
        return c

    if not r['cuadra']:
        return salir('MANUAL_REVIEW', 'el importe no reproduce lo descontado en el settlement')

    so = ctx['so'].get(orden)
    if not so:
        return salir('NO_SO', 'orden sin SO en Odoo')
    c['so'] = so['name']

    # --- Tipo A / B / C -------------------------------------------------------
    mov = ctx['movimientos'].get(so['name'], {'salida': False, 'retorno': False})
    es_odoo18 = so['name'].startswith(('SO10', 'S0'))
    c['salida'] = mov['salida'] if es_odoo18 else bool(so['effective_date'])
    c['retorno'] = mov['retorno']
    venta = ctx['venta_pieza'].get(r['pieza'])
    c['venta_pieza'] = venta

    if r['tipo'] == 'DisbursementOnlyRefund':
        c['tipo'], c['alcance'], c['motivo'] = 'C', 'PARTIAL', 'DisbursementOnlyRefund'
    else:
        if venta and ctx['devuelto'].get(r['pieza'], total) > venta * (1 + TOLERANCIA_PIEZA):
            return salir('MANUAL_REVIEW',
                         f"lo devuelto de la pieza ({ctx['devuelto'][r['pieza']]:,.2f}) "
                         f"supera su venta ({venta:,.2f})")
        completa = bool(venta) and total >= venta * (1 - TOLERANCIA_PIEZA)
        c['alcance'] = 'TOTAL' if completa else 'PARTIAL'
        if not c['salida']:
            c['tipo'] = 'A'
            c['motivo'] = f"SO {so['state']} sin salida de almacen"
        elif not venta:
            # La master solo trae ventas liberadas: una pieza vendida hace dias
            # sigue retenida. Se queda en DETECTED y la siguiente corrida la toma.
            return salir('DETECTED', 'con salida; la venta de la pieza aun no esta liberada')
        elif completa:
            c['tipo'], c['motivo'] = 'B', 'con salida, pieza completa'
        else:
            c['tipo'] = 'C'
            c['motivo'] = f"con salida, reembolso parcial ({total:,.2f} de {venta:,.2f})"

    # --- Factura y canal ------------------------------------------------------
    b = ctx['billing'].get(orden)
    c['billing'] = b['status'] if b else None
    if not b or b['status'] not in FACTURADAS:
        return salir('NO_INVOICE', f"venta sin factura nuestra ({c['billing'] or 'sin fila en facturacion'})")
    c['uuid_factura'] = b['cfdi_uuid']

    docs = ctx['cfdis'].get(orden)
    if docs and docs['I']:
        if b['status'] != 'ODOO_B2B_LINKED':
            return salir('MANUAL_REVIEW', 'Amazon emitio CFDI de la venta pero la factura '
                                          'en Odoo es a publico general (doble CFDI)')
        c['canal'] = 'B2B'
        for e in docs['E']:
            if e['uuid'] not in ctx['egresos_usados'] and abs(a_float(e['total']) - total) <= 0.01:
                ctx['egresos_usados'].add(e['uuid'])
                c['egreso'] = {'uuid': e['uuid'], 'fecha': str(e['date'])[:19]}
                break
        if not c['egreso']:
            return salir('WAITING_B2B_CFDI', 'falta el CFDI de egreso de Amazon')
    return c


def guardar(db_conn, r, c):
    """Solo actualiza si la fila sigue en un estado reevaluable: si 07 ya la
    tomo, la clasificacion no se toca."""
    detalle = {k: c[k] for k in ('canal', 'billing', 'venta_pieza', 'egreso')}
    detalle.update({'reglas': 'supuestos 9-nov-2026, sin confirmar por Finanzas',
                    'clasificado_en': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    egreso = c['egreso'] or {}
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_REFUNDS}
        SET refund_type = %s, refund_scope = %s, has_out_validated = %s,
            has_physical_return = %s, classification_reason = %s, original_amount = %s,
            odoo_so_name = %s, cfdi_uuid = %s, cfdi_date = %s, cfdi_uuid_related = %s,
            raw_json = JSON_SET(raw_json, '$.clasificacion', CAST(%s AS JSON)),
            status = %s
        WHERE id = %s AND status IN {REEVALUABLES}
    """, (c['tipo'], c['alcance'], c['salida'], c['retorno'], c['motivo'][:255],
          c['venta_pieza'], c['so'], egreso.get('uuid'), egreso.get('fecha'),
          c['uuid_factura'], json.dumps(detalle, ensure_ascii=False), c['status'], r['id']))
    db_conn.commit()
    cursor.close()


def run():
    db_conn = get_db_connection()
    refunds = leer(db_conn, f"""
        SELECT id, mkp_order_id, ml_claim_id AS pieza, ml_transaction_type AS tipo,
               refund_amount, ml_money_taken_date AS fecha, status,
               JSON_EXTRACT(raw_json, '$.importes.cuadra') = true AS cuadra
        FROM {TBL_REFUNDS}
        WHERE marketplace = %s AND status IN {REEVALUABLES}
        ORDER BY ml_money_taken_date""", (MARKETPLACE,))
    log.info(f"{len(refunds)} devolucion(es) por clasificar | facturacion: {TBL_BILLING_NC}"
             f"{' | DRY_RUN: no se escribe' if DRY_RUN else ''}")
    if not refunds:
        db_conn.close()
        return

    ctx = cargar_contexto(db_conn, refunds)
    resumen, revisar = {}, []
    for r in refunds:
        c = clasificar(r, ctx)
        clave = (c['status'], c['tipo'] or '-', c['canal'])
        acc = resumen.setdefault(clave, [0, 0.0])
        acc[0] += 1
        acc[1] += a_float(r['refund_amount'])
        if c['status'] == 'MANUAL_REVIEW':
            revisar.append(f"{r['mkp_order_id']} {a_float(r['refund_amount']):,.2f}: {c['motivo']}")
        if not DRY_RUN:
            guardar(db_conn, r, c)
    db_conn.close()

    log.info('')
    log.info(f"{'ESTADO':<18}{'TIPO':<6}{'CANAL':<7}{'N':>5}{'IMPORTE':>16}")
    for (status, tipo, canal), (n, importe) in sorted(resumen.items()):
        log.info(f"{status:<18}{tipo:<6}{canal:<7}{n:>5}{importe:>16,.2f}")
    if revisar:
        log.warning(f"{len(revisar)} devolucion(es) a revision manual:")
        for linea in revisar[:20]:
            log.warning(f"  {linea}")
        if ENTORNO == 'prod' and not DRY_RUN:
            notify_slack(f":warning: Devoluciones Amazon: {len(revisar)} a revision manual "
                         f"(MANUAL_REVIEW en {TBL_REFUNDS}).")


if __name__ == '__main__':
    banner('CLASIFICADOR DE DEVOLUCIONES -- AMAZON')
    try:
        run()
    except Exception as e:
        log.error(f'Fallo critico en el clasificador de devoluciones: {e}')
        sys.exit(1)
