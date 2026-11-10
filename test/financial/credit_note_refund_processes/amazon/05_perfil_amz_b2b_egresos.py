"""
05_perfil_amz_b2b_egresos.py
----------------------------
PERFILADO DE EGRESOS B2B DE AMAZON (fase 2 de notas de credito)

Solo lectura. Cruza las devoluciones de ordenes Amazon Business que dejo
04_miner_amz_refunds.py contra los CFDI de egreso que emite Amazon
(somos_reyes.amz_b2b_invoices, receipt_type='E'). Responde, antes de programar
el clasificador y el loader, si la NC B2B se puede replicar desde esa tabla.

ENTRADA
  finance.mkp_refunds_{staging|prod}   devoluciones (marketplace='AMAZON')
  somos_reyes.amz_b2b_invoices         CFDI de Amazon Business (I y E)
  somos_reyes.amazon_orders            IsBusinessOrder (N filas por orden)
  finance.mkp_billing_{staging|prod}   estado de facturacion de la orden

SALIDA
  Resumen en el log y perfil_b2b_egresos_<fecha>.csv en esta carpeta, una
  fila por devolucion B2B y una por cada egreso que no encontro devolucion.

SITUACIONES
  EGRESO_OK          la devolucion tiene su CFDI de egreso (misma orden, mismo
                     total). Se replica con ese UUID, sin timbrar.
  ESPERA_EGRESO      Amazon facturo la venta (CFDI I) y aun no emite el egreso.
  SIN_CFDI_AMAZON    orden Business sin CFDI de Amazon: la venta la facturamos
                     nosotros a publico general y la NC la timbramos nosotros.
  EGRESO_SIN_REFUND  egreso de Amazon sin devolucion registrada.

EL CRUCE ES POR ORDEN Y TOTAL
  amz_b2b_invoices no trae el UUID relacionado ni la pieza: solo la orden y
  el importe. Cada egreso se asigna a una sola devolucion de su orden con el
  mismo total (+-0.01); dos piezas iguales devueltas dan dos egresos iguales.
"""

import os
import sys
import csv
import logging
from datetime import datetime

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
    banner, get_db_connection, a_float,
)

TBL_REFUNDS = f'finance.mkp_refunds_{ENTORNO}'

# Egresos anteriores a esta fecha no se reportan como EGRESO_SIN_REFUND: su
# devolucion es anterior al arranque del miner (1-oct-2026).
EGRESOS_DESDE = os.getenv('AMZ_REFUNDS_DESDE', '2026-10-01 06:00:00')[:10]


def leer(db_conn, sql, params=()):
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def cargar(db_conn):
    refunds = leer(db_conn, f"""
        SELECT r.id, r.mkp_order_id, r.ml_claim_id AS pieza, r.ml_transaction_type AS tipo,
               r.refund_amount, r.ml_money_taken_date AS fecha, r.status, b.status AS billing
        FROM {TBL_REFUNDS} r
        LEFT JOIN {TBL_BILLING} b
               ON b.marketplace = r.marketplace AND b.mkp_order_id = r.mkp_order_id
        WHERE r.marketplace = %s
        ORDER BY r.ml_money_taken_date""", (MARKETPLACE,))

    cfdis = leer(db_conn, """
        SELECT order_id, receipt_type, uuid, series, invoice, total, date, status,
               cancellation_date, related_cfdi_relation_type AS relacion, receiver_rfc
        FROM somos_reyes.amz_b2b_invoices
        ORDER BY date""")

    ordenes = sorted({r['mkp_order_id'] for r in refunds})
    business = set()
    for i in range(0, len(ordenes), 500):
        lote = ordenes[i:i + 500]
        marcas = ','.join(['%s'] * len(lote))
        business |= {o['AmazonOrderId'] for o in leer(db_conn, f"""
            SELECT AmazonOrderId FROM somos_reyes.amazon_orders
            WHERE AmazonOrderId IN ({marcas})
            GROUP BY AmazonOrderId HAVING MAX(IsBusinessOrder) = 1""", lote)}
    return refunds, cfdis, business


def cruzar(refunds, cfdis, business):
    """Devuelve las filas del perfil. Un egreso se usa una sola vez."""
    por_orden = {}
    for c in cfdis:
        por_orden.setdefault(c['order_id'], {'I': [], 'E': []}).setdefault(
            c['receipt_type'], []).append(c)

    filas, usados = [], set()
    for r in refunds:
        orden = r['mkp_order_id']
        docs = por_orden.get(orden)
        if not docs and orden not in business:
            continue                      # B2C: no es de este perfil
        egreso = None
        for e in (docs or {}).get('E', []):
            if e['uuid'] not in usados and abs(a_float(e['total']) - a_float(r['refund_amount'])) <= 0.01:
                egreso = e
                usados.add(e['uuid'])
                break
        if egreso:
            situacion = 'EGRESO_OK'
        elif docs and docs['I']:
            situacion = 'ESPERA_EGRESO'
        else:
            situacion = 'SIN_CFDI_AMAZON'
        filas.append(fila_perfil(situacion, orden, r, egreso, docs))

    for c in cfdis:
        if (c['receipt_type'] == 'E' and c['uuid'] not in usados
                and str(c['date'])[:10] >= EGRESOS_DESDE):
            filas.append(fila_perfil('EGRESO_SIN_REFUND', c['order_id'], None, c,
                                     por_orden[c['order_id']]))
    return filas


def fila_perfil(situacion, orden, refund, egreso, docs):
    ingresos = (docs or {}).get('I', [])
    dias = None
    if refund and egreso:
        dias = (datetime.strptime(str(egreso['date'])[:10], '%Y-%m-%d').date()
                - refund['fecha'].date()).days
    return {
        'situacion': situacion,
        'order_id': orden,
        'refund_id_fila': refund['id'] if refund else '',
        'pieza': refund['pieza'] if refund else '',
        'tipo': refund['tipo'] if refund else '',
        'refund_total': a_float(refund['refund_amount']) if refund else '',
        'refund_fecha': str(refund['fecha'])[:10] if refund else '',
        'billing_status': (refund['billing'] or '') if refund else '',
        'egreso_uuid': egreso['uuid'] if egreso else '',
        'egreso_folio': f"{egreso['series']}{egreso['invoice']}" if egreso else '',
        'egreso_total': a_float(egreso['total']) if egreso else '',
        'egreso_fecha': str(egreso['date'])[:10] if egreso else '',
        'egreso_relacion': egreso['relacion'] if egreso else '',
        'egreso_status': egreso['status'] if egreso else '',
        'dias_egreso_vs_refund': '' if dias is None else dias,
        'ingresos_amazon': ' | '.join(f"{i['uuid']} {a_float(i['total']):.2f} {str(i['date'])[:10]}"
                                      for i in ingresos),
        # Un egreso sin ingreso del mismo total no tiene factura que revertir.
        'ingreso_mismo_total': any(
            abs(a_float(i['total']) - a_float((egreso or {}).get('total') or
                                              (refund or {}).get('refund_amount'))) <= 0.01
            for i in ingresos),
    }


def run():
    db_conn = get_db_connection()
    refunds, cfdis, business = cargar(db_conn)
    db_conn.close()
    log.info(f"{len(refunds)} devolucion(es) en {TBL_REFUNDS} | "
             f"{sum(c['receipt_type'] == 'E' for c in cfdis)} egreso(s) y "
             f"{sum(c['receipt_type'] == 'I' for c in cfdis)} ingreso(s) de Amazon Business")

    filas = cruzar(refunds, cfdis, business)
    if not filas:
        log.info("Ninguna devolucion de orden Business ni egreso en la ventana.")
        return

    log.info('')
    log.info(f"{'SITUACION':<20}{'N':>5}{'IMPORTE':>16}")
    for situacion in ('EGRESO_OK', 'ESPERA_EGRESO', 'SIN_CFDI_AMAZON', 'EGRESO_SIN_REFUND'):
        grupo = [f for f in filas if f['situacion'] == situacion]
        importe = sum(a_float(f['refund_total'] or f['egreso_total']) for f in grupo)
        log.info(f"{situacion:<20}{len(grupo):>5}{importe:>16,.2f}")

    con_egreso = [f for f in filas if f['situacion'] == 'EGRESO_OK']
    if con_egreso:
        dias = sorted(f['dias_egreso_vs_refund'] for f in con_egreso)
        log.info('')
        log.info(f"Egreso vs devolucion (dias): min {dias[0]}, max {dias[-1]}. "
                 f"Negativo = Amazon emitio el CFDI antes de publicar el refund.")
        log.info(f"Tipo de relacion: {sorted({f['egreso_relacion'] for f in con_egreso})} | "
                 f"sin ingreso del mismo total: "
                 f"{sum(not f['ingreso_mismo_total'] for f in con_egreso)}")
    sin_ingreso = [f for f in filas if f['situacion'] == 'EGRESO_SIN_REFUND']
    for f in sin_ingreso:
        log.warning(f"  Egreso sin devolucion: {f['order_id']} {f['egreso_folio']} "
                    f"{f['egreso_total']:,.2f} del {f['egreso_fecha']}")

    salida = os.path.join(_dir, f"perfil_b2b_egresos_{datetime.now():%Y%m%d_%H%M}.csv")
    with open(salida, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(filas[0].keys()))
        writer.writeheader()
        writer.writerows(filas)
    log.info('')
    log.info(f"Detalle: {salida}")


if __name__ == '__main__':
    banner('PERFILADO DE EGRESOS B2B -- AMAZON')
    try:
        run()
    except Exception as e:
        log.error(f'Fallo en el perfilado de egresos B2B: {e}')
        sys.exit(1)
