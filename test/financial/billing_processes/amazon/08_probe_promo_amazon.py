"""

08_probe_promo_amazon.py
------------------------
PRUEBA DE LA API: ¿SE PUEDE DETECTAR UNA PROMOCION PAGADA POR AMAZON?
SOLO LECTURA. No escribe en MySQL, Odoo ni Slack.


LA PREGUNTA (correos de Martin y Jero sobre el lote 4)
-----------------------------------------------------
Finanzas decidio registrar el excedente de las promociones financiadas por
Amazon en 403.01.002 CUPONES AMAZON + 208.01.001 IVA, pero SOLO cuando un dato
de Amazon confirme que es eso, no para cualquier diferencia positiva.

Hoy no tenemos ese dato:
  * cliente_pago del expediente sale de amazon_orders.OrderTotalAmount
    (Orders API getOrders). Dice cuanto pago el cliente, no por que.
  * promocion_amazon_orders busca columnas 'promo'/'discount' en
    amazon_orders, y getOrders no trae ninguna: por eso salio vacia.
  * amz_transaction_items (Finances API) solo trae las promociones que paga
    el VENDEDOR (promo_rebates, our_price_discount). Una promocion pagada por
    Amazon no aparece: Amazon nos liquida el precio de lista completo.
    Es justo lo que vio Martin en el reporte de pagos.

El candidato es getOrderItems (Orders API v0), que trae por linea:
PromotionDiscount, PromotionDiscountTax y PromotionIds. Es lo que el CLIENTE
recibio de descuento, sin importar quien lo pago. Cruzado con las
transacciones (donde NO aparece si lo pago Amazon) da la regla de Martin:

    promocion en el pedido  (getOrderItems.PromotionDiscount > 0)
  + sin descuento del vendedor en el pago  (our_price_discount = 0)
  + diferencia = monto de la promocion

QUE HAY QUE CONFIRMAR CON ESTE SCRIPT ANTES DE USARLO
-----------------------------------------------------
  1. Que PromotionDiscount venga lleno en las promociones pagadas por Amazon.
     Si viene en 0, getOrderItems no sirve y hay que buscar otra fuente.
  2. Si en Mexico PromotionDiscount ya incluye IVA (como OrderTotal) o si hay
     que sumarle PromotionDiscountTax. El script prueba las dos hipotesis.
  3. Que en las ordenes de control (sin excedente) no aparezca una promocion
     que nos haria clasificar de mas.

Casos: los 4 del expediente del lote 4, los 5 conocidos de lotes anteriores y
3 de control. Se pueden pasar otros order_id como argumentos.

Uso:
    python 08_probe_promo_amazon.py
    python 08_probe_promo_amazon.py 701-0635257-8905020 702-...
"""

import os
import sys
import csv
import time as tm
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import get_db_connection, sp_api_token, get_order_items, a_float

TOL = 0.01

# order_id -> (tipo, cliente_pago, transacciones, nota)
# cliente_pago = amazon_orders.OrderTotal; transacciones = base + IVA segun
# amz_transaction_items. diferencia = transacciones - cliente.
CASOS = {
    # Expediente del lote 4 (casos_diferencia_20261005_184629_resumen.csv)
    '701-0635257-8905020': ('B2C', 2900.00, 3200.00, 'lote 4; Seller Central: promo pagada por Amazon 258.62'),
    '701-9938559-8275435': ('B2B', 350.54, 369.00, 'lote 4'),
    '702-0367082-5204206': ('B2C', 1839.00, 1939.00, 'lote 4; ademas envio gratis pagado por el vendedor'),
    '702-6841264-9666630': ('B2B', 1399.29, 1999.00, 'lote 4; ademas envio gratis pagado por el vendedor'),
    # Lotes anteriores (comentarios de 01_amz_billing.py)
    '701-2593380-9743412': ('B2B', 1741.13, 2597.69, 'lote 1'),
    '701-7403714-0375443': ('B2C', 515.00, 615.00, 'lote 2; cupon $100'),
    '701-2640601-6960243': ('B2C', 2149.00, 2249.00, 'lote 2; cupon $100'),
    '702-1844782-3257848': ('B2C', 7898.00, 7998.00, 'lote 2; cupon $100'),
    '702-3221480-9299457': ('B2C', 899.00, 999.00, 'lote 2; cupon $100'),
    # Control: sin excedente. No deberian mostrar promocion que cuadre.
    '701-0614012-2342618': ('CTRL', None, None, 'control: cobro normal'),
    '701-3762419-7700260': ('CTRL', None, None, 'control: 2 envios, POR_ENVIO'),
    '702-7982773-9610618': ('CTRL', None, None, 'control: diferencia de 1 centavo'),
}


def monto(campo):
    return a_float((campo or {}).get('Amount'))


def evaluar(order_id, items, caso):
    tipo, cliente, trans, nota = caso
    p = sum(monto(i.get('PromotionDiscount')) for i in items)
    pt = sum(monto(i.get('PromotionDiscountTax')) for i in items)
    ip = sum(monto(i.get('ItemPrice')) for i in items)
    it = sum(monto(i.get('ItemTax')) for i in items)
    ids = sorted({x for i in items for x in (i.get('PromotionIds') or [])})

    dif = round(trans - cliente, 2) if trans is not None else None
    if dif is None:
        veredicto = ('CONTROL_CON_PROMO' if p > TOL else 'CONTROL_SIN_PROMO')
    elif p <= TOL:
        veredicto = 'API_SIN_PROMO'
    elif abs(dif - p) <= TOL:
        veredicto = 'CUADRA_IVA_INCLUIDO'
    elif abs(dif - (p + pt)) <= TOL:
        veredicto = 'CUADRA_SUMANDO_IVA'
    else:
        veredicto = 'NO_CUADRA'

    return {
        'order_id': order_id, 'tipo': tipo, 'nota': nota,
        'cliente_pago': cliente, 'transacciones': trans, 'diferencia': dif,
        'item_price': round(ip, 2), 'item_tax': round(it, 2),
        'promotion_discount': round(p, 2),
        'promotion_discount_tax': round(pt, 2),
        'item_price_menos_promo': round(ip - p, 2),
        'promotion_ids': ' | '.join(ids), 'n_items': len(items),
        'veredicto': veredicto,
    }


def run(ordenes):
    db_conn = get_db_connection()
    token = sp_api_token(db_conn)
    db_conn.close()

    sello = datetime.now().strftime('%Y%m%d_%H%M%S')
    ruta_items = os.path.join(_dir, f'probe_promo_{sello}_items.csv')
    ruta_res = os.path.join(_dir, f'probe_promo_{sello}_resumen.csv')

    resumen, filas_items = [], []
    for n, oid in enumerate(ordenes):
        caso = CASOS.get(oid, ('ARG', None, None, 'pasado por argumento'))
        try:
            items = get_order_items(token, oid)
        except Exception as e:
            log.error(f"{oid}: fallo getOrderItems: {e}")
            resumen.append({'order_id': oid, 'tipo': caso[0], 'nota': caso[3],
                            'veredicto': f'ERROR_API {str(e)[:80]}'})
            continue
        for i in items:
            filas_items.append({
                'order_id': oid, 'OrderItemId': i.get('OrderItemId'),
                'SellerSKU': i.get('SellerSKU'),
                'QuantityOrdered': i.get('QuantityOrdered'),
                'ItemPrice': monto(i.get('ItemPrice')),
                'ItemTax': monto(i.get('ItemTax')),
                'PromotionDiscount': monto(i.get('PromotionDiscount')),
                'PromotionDiscountTax': monto(i.get('PromotionDiscountTax')),
                'ShippingPrice': monto(i.get('ShippingPrice')),
                'ShippingDiscount': monto(i.get('ShippingDiscount')),
                'PromotionIds': ' | '.join(i.get('PromotionIds') or []),
            })
        r = evaluar(oid, items, caso)
        resumen.append(r)
        log.info(f"{oid} [{r['tipo']}] dif={r['diferencia']} "
                 f"promo={r['promotion_discount']} "
                 f"promo_iva={r['promotion_discount_tax']} -> {r['veredicto']}")
        if n < len(ordenes) - 1:
            tm.sleep(2.1)   # getOrderItems: 0.5 req/s

    for ruta, filas in ((ruta_items, filas_items), (ruta_res, resumen)):
        if not filas:
            continue
        cab = list(dict.fromkeys(k for f in filas for k in f))
        with open(ruta, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.DictWriter(fh, fieldnames=cab)
            w.writeheader()
            w.writerows(filas)

    log.info('')
    log.info('=' * 78)
    log.info('VEREDICTO')
    log.info('=' * 78)
    cuenta = {}
    for r in resumen:
        cuenta[r['veredicto']] = cuenta.get(r['veredicto'], 0) + 1
    for v in sorted(cuenta):
        log.info(f"  {v:<28}{cuenta[v]:>4}")
    log.info('')
    log.info("  CUADRA_IVA_INCLUIDO / CUADRA_SUMANDO_IVA en los casos con")
    log.info("  excedente y CONTROL_SIN_PROMO en los de control = la API sirve")
    log.info("  como flag. API_SIN_PROMO = getOrderItems no expone la promocion.")
    log.info(f"  Detalle: {ruta_res}")
    log.info(f"           {ruta_items}")
    log.info('=' * 78)


if __name__ == '__main__':
    ordenes = sys.argv[1:] or list(CASOS)
    try:
        run(ordenes)
    except Exception as e:
        log.error(f"Fallo la prueba: {e}")
        sys.exit(1)
