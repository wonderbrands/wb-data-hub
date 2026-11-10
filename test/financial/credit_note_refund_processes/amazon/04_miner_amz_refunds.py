"""
04_miner_amz_refunds.py
-----------------------
MINER DE DEVOLUCIONES AMAZON (fase 1 de notas de credito)

Deja en finance.mkp_refunds_{staging|prod} una fila por cada devolucion de
Amazon. No toca Odoo: solo MySQL. No clasifica (A/B/C) ni busca la factura:
eso es de 06_classifier_amz_refunds.py.

ENTRADA
  somos_reyes.amz_transactions + amz_transaction_items, con transaction_type
  Refund, ChargebackRefund o GuaranteeClaimRefund (description 'Refund' o
  'DisbursementOnlyRefund'). LabmanLabelRefund NO entra: es el reembolso de
  una guia a nuestro favor, no una devolucion al cliente.

SALIDA (marketplace='AMAZON', status='DETECTED')
  mkp_order_id         order_id de Amazon
  ml_claim_id          pieza (shipment_id de la venta original)
  mp_refund_id         REFUND_ID de Amazon, sin el prefijo 'amzn1:crow:'
  source_event         'AMZ_REFUND'
  ml_transaction_type  Refund / ChargebackRefund / GuaranteeClaimRefund /
                       DisbursementOnlyRefund
  refund_amount        importe de la NC con IVA, en positivo
  ml_money_taken_date  posted_date en hora de CDMX (mes de la devolucion)
  raw_json             importes, piezas y liberacion (settlement)

DEDUPLICACION: POR REFUND_ID, NO POR PIEZA
  Una devolucion aparece hasta tres veces: DEFERRED y DEFERRED_RELEASED (la
  misma transaccion, retenida) y RELEASED (otra transaccion, en el settlement
  donde Amazon la descuenta). Las tres comparten el REFUND_ID de
  related_identifiers. Verificado el 9-nov-2026 (1-ago a 9-nov): 2,362 filas
  = 1,494 REFUND_ID; ninguno con mas de una RELEASED ni mas de una pieza.
  La regla de ventas ("fila mas reciente por pieza") no sirve aqui: una pieza
  puede tener varios reembolsos parciales (10 casos) y se perderian.
  Manda la fila RELEASED. Sin ella la devolucion sigue retenida
  (liberacion.liberada = false): la NC no espera, el cobro si.

IMPORTE
  base = product_charges + shipping + promo_rebates
  iva  = our_price_tax + shipping_tax + shipping_tax_discount + our_price_tax_disc
  neto = base + iva + amazon_fees + marketplace_vat_* = item_amount
  Se usa promo_rebates (el roll-up) y no our_price_discount: en devoluciones
  el ETL no siempre llena el detalle. En DisbursementOnlyRefund el importe
  solo viene en item_amount: base = item_amount / 1.16.

RELECTURA
  Una fila que ya salio de DETECTED no cambia de importe; solo se le
  actualiza raw_json.liberacion, que es lo que necesita el cobro (fase 5).
"""

import os
import sys
import json
import time as tm
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# _amz_common.py vive con los scripts de facturacion. En produccion estara en
# esta misma carpeta; en test, en billing_processes/amazon.
_dir = os.path.dirname(os.path.abspath(__file__))
for _p in (_dir, os.path.normpath(os.path.join(_dir, '..', '..', 'billing_processes', 'amazon'))):
    if os.path.exists(os.path.join(_p, '_amz_common.py')):
        if _p not in sys.path:
            sys.path.append(_p)
        break

from _amz_common import (
    ENTORNO, MARKETPLACE, UTC_local,
    banner, notify_slack, get_db_connection, a_float,
)

TBL_REFUNDS = f'finance.mkp_refunds_{ENTORNO}'
SOURCE_EVENT = 'AMZ_REFUND'

# Arranque con la tabla vacia, en UTC (posted_date viene en UTC): 1-oct-2026
# 00:00 CDMX, el dia en que Amazon empezo a facturarse con 01.
DESDE_INICIAL = os.getenv('AMZ_REFUNDS_DESDE', '2026-10-01 06:00:00')

# Una devolucion retenida se libera en 1 a 15 dias; 20 da holgura para
# releerla y registrar su settlement.
LOOKBACK_DAYS = 20

# true = lee y reporta, no escribe.
DRY_RUN = os.getenv('AMZ_REFUNDS_DRY_RUN', 'false').lower().strip() in ('1', 'true', 'si')

QUERY_REFUNDS = """
SELECT a.transaction_id, a.transaction_type, a.description, a.transaction_status,
       a.posted_date, a.order_id, a.settlement_id, a.shipment_id, a.related_identifiers,
       b.sku, b.quantity_shipped, b.fulfillment_network, b.item_amount,
       b.product_charges, b.shipping, b.promo_rebates,
       b.our_price_tax, b.shipping_tax, b.shipping_tax_discount, b.our_price_tax_disc,
       b.amazon_fees, b.marketplace_vat_principal, b.marketplace_vat_shipping
FROM somos_reyes.amz_transactions a
JOIN somos_reyes.amz_transaction_items b ON b.transaction_id = a.transaction_id
WHERE a.transaction_type IN ('Refund', 'ChargebackRefund', 'GuaranteeClaimRefund')
  AND a.posted_date >= %s
"""


def leer_desde(db_conn):
    """Marca de agua: la devolucion mas reciente ya registrada, menos el
    lookback. ml_money_taken_date esta en CDMX; la query filtra en UTC."""
    cursor = db_conn.cursor()
    cursor.execute(f"SELECT MAX(ml_money_taken_date) FROM {TBL_REFUNDS} "
                   f"WHERE marketplace = %s", (MARKETPLACE,))
    ultima = cursor.fetchone()[0]
    cursor.close()
    if not ultima:
        return DESDE_INICIAL
    desde = ultima - timedelta(hours=UTC_local) - timedelta(days=LOOKBACK_DAYS)
    return max(desde.strftime('%Y-%m-%d %H:%M:%S'), DESDE_INICIAL)


def extraer_refund_id(related_identifiers):
    try:
        for x in json.loads(related_identifiers or '[]'):
            if x.get('relatedIdentifierName') == 'REFUND_ID':
                return x.get('relatedIdentifierValue')
    except ValueError:
        pass
    return None


def agrupar(rows):
    """{refund_id: {(transaction_id, status): {'cab': fila, 'items': [filas]}}}.
    La llave lleva el status porque la transaccion retenida existe dos veces
    (DEFERRED y DEFERRED_RELEASED) y el JOIN repite sus piezas."""
    eventos, sin_id = {}, 0
    for r in rows:
        rid = extraer_refund_id(r['related_identifiers'])
        if not rid:
            sin_id += 1
            continue
        tx = eventos.setdefault(rid, {}).setdefault(
            (r['transaction_id'], r['transaction_status']), {'cab': r, 'items': []})
        tx['items'].append(r)
    return eventos, sin_id


def calcular_importes(tx):
    """Importes de la devolucion, en positivo. 'cuadra' = la formula
    reproduce item_amount (lo que Amazon descuenta del deposito)."""
    solo_fondos = tx['cab']['description'] == 'DisbursementOnlyRefund'
    base = iva = fees = neto = 0.0
    piezas = []
    for i in tx['items']:
        item = a_float(i['item_amount'])
        if solo_fondos:
            b, v, f = item / 1.16, item - item / 1.16, 0.0
        else:
            b = a_float(i['product_charges']) + a_float(i['shipping']) + a_float(i['promo_rebates'])
            v = (a_float(i['our_price_tax']) + a_float(i['shipping_tax'])
                 + a_float(i['shipping_tax_discount']) + a_float(i['our_price_tax_disc']))
            f = (a_float(i['amazon_fees']) + a_float(i['marketplace_vat_principal'])
                 + a_float(i['marketplace_vat_shipping']))
        base, iva, fees, neto = base + b, iva + v, fees + f, neto + item
        piezas.append({'sku': i['sku'], 'cantidad': int(i['quantity_shipped'] or 0),
                       'red': i['fulfillment_network'], 'total': round(-(b + v), 2)})
    return {'base': round(-base, 2), 'iva': round(-iva, 2), 'total': round(-(base + iva), 2),
            'fees': round(fees, 2), 'neto_settlement': round(neto, 2),
            'cuadra': abs(base + iva + fees - neto) <= 0.01}, piezas


def construir_fila(rid, txs):
    """Elige la transaccion que manda y arma la fila. None si no trae importe."""
    por_status = {}
    for (_, status), tx in txs.items():
        actual = por_status.get(status)
        if not actual or tx['cab']['posted_date'] > actual['cab']['posted_date']:
            por_status[status] = tx
    liberada = por_status.get('RELEASED')
    tx = liberada or por_status.get('DEFERRED_RELEASED') or por_status.get('DEFERRED')
    cab = tx['cab']
    importes, piezas = calcular_importes(tx)
    if importes['total'] <= 0:
        return None

    refund_id = rid.split(':')[-1]
    if len(refund_id) > 30 or len(cab['order_id']) > 30:
        raise Exception(f"REFUND_ID u orden no caben en la tabla: {rid} / {cab['order_id']}")

    # El mes de la devolucion es el de su primera publicacion, no el de la
    # liberacion (que puede caer en el mes siguiente).
    primera = min(t['cab']['posted_date'] for t in txs.values())
    raw = {
        'refund_id': rid, 'pieza': cab['shipment_id'],
        'importes': importes, 'piezas': piezas,
        'liberacion': {'liberada': bool(liberada),
                       'settlement_id': liberada['cab']['settlement_id'] if liberada else None,
                       'posted_date_utc': str(liberada['cab']['posted_date']) if liberada else None},
        'entorno': ENTORNO, 'generado_por': '04_miner_amz_refunds.py',
        'generado_en': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    tipo = cab['description'] if cab['description'] == 'DisbursementOnlyRefund' else cab['transaction_type']
    return {'order_id': cab['order_id'], 'pieza': cab['shipment_id'], 'refund_id': refund_id,
            'tipo': tipo, 'total': importes['total'],
            'fecha': primera + timedelta(hours=UTC_local),
            'liberada': bool(liberada), 'cuadra': importes['cuadra'],
            'raw_json': json.dumps(raw, ensure_ascii=False)}


def upsert_refunds(db_conn, filas):
    """uq_event (marketplace, orden, source_event, pieza, refund_id) hace el
    insert idempotente. rowcount: 1 = insert, 2 = update, 0 = sin cambios."""
    cursor = db_conn.cursor()
    res = {'insertadas': 0, 'refrescadas': 0, 'sin_cambios': 0}
    for f in filas:
        cursor.execute(f"""
            INSERT INTO {TBL_REFUNDS}
                (marketplace, mkp_order_id, ml_claim_id, mp_refund_id, source_event,
                 ml_transaction_type, refund_amount, ml_money_taken_date, raw_json, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'DETECTED')
            ON DUPLICATE KEY UPDATE
                refund_amount = IF(status = 'DETECTED', VALUES(refund_amount), refund_amount),
                raw_json = IF(status = 'DETECTED', VALUES(raw_json),
                              JSON_SET(raw_json, '$.liberacion',
                                       JSON_EXTRACT(VALUES(raw_json), '$.liberacion')))
        """, (MARKETPLACE, f['order_id'], f['pieza'], f['refund_id'], SOURCE_EVENT,
              f['tipo'], f['total'], f['fecha'], f['raw_json']))
        db_conn.commit()
        clave = {1: 'insertadas', 2: 'refrescadas'}.get(cursor.rowcount, 'sin_cambios')
        res[clave] += 1
    cursor.close()
    return res


def run():
    db_conn = get_db_connection()
    desde = leer_desde(db_conn)
    log.info(f"Leyendo devoluciones con posted_date (UTC) >= {desde}"
             f"{' | DRY_RUN: no se escribe' if DRY_RUN else ''}")

    t0 = tm.time()
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_REFUNDS, (desde,))
    rows = cursor.fetchall()
    cursor.close()
    log.info(f"{len(rows)} fila(s) leidas en {round(tm.time() - t0, 1)}s")

    eventos, sin_id = agrupar(rows)
    filas = [f for f in (construir_fila(rid, txs) for rid, txs in eventos.items()) if f]
    retenidas = [f for f in filas if not f['liberada']]
    no_cuadran = [f for f in filas if not f['cuadra']]

    log.info(f"Devoluciones (REFUND_ID)   : {len(eventos)}")
    log.info(f"  sin importe (se omiten)  : {len(eventos) - len(filas)}")
    log.info(f"  a registrar              : {len(filas)}  "
             f"total c/IVA {sum(f['total'] for f in filas):,.2f}")
    log.info(f"  aun retenidas (DEFERRED) : {len(retenidas)}  "
             f"total c/IVA {sum(f['total'] for f in retenidas):,.2f}")
    for tipo in sorted({f['tipo'] for f in filas}):
        del_tipo = [f for f in filas if f['tipo'] == tipo]
        log.info(f"  {tipo:<24} : {len(del_tipo):>5}  {sum(f['total'] for f in del_tipo):>14,.2f}")
    if sin_id:
        log.warning(f"{sin_id} fila(s) sin REFUND_ID: no se registran. Revisar el ETL.")
    if no_cuadran:
        log.warning(f"{len(no_cuadran)} devolucion(es) cuya formula no reproduce "
                    f"item_amount: {', '.join(f['order_id'] for f in no_cuadran[:10])}")

    if not DRY_RUN:
        res = upsert_refunds(db_conn, filas)
        log.info(f"Insertadas {res['insertadas']} | refrescadas {res['refrescadas']} | "
                 f"sin cambios {res['sin_cambios']}")
    db_conn.close()

    if (sin_id or no_cuadran) and ENTORNO == 'prod' and not DRY_RUN:
        notify_slack(f":warning: Devoluciones Amazon: {sin_id} fila(s) sin REFUND_ID y "
                     f"{len(no_cuadran)} cuyo importe no cuadra contra el settlement.")


if __name__ == '__main__':
    banner('MINER DE DEVOLUCIONES -- AMAZON')
    t0 = tm.time()
    try:
        run()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico en el miner de devoluciones: {e}')
        sys.exit(1)
