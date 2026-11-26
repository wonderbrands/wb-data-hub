"""
mkp_sources.py
--------------
Lo unico que cambia entre marketplaces: de donde sale la venta, cuanto es y
si ya se puede facturar. Todo lo demas (Odoo, blindajes, estados) es comun y
vive en 01_mkp_billing.py.

Patron de ml_stores.py: un marketplace nuevo es una entrada en FUENTES mas su
funcion de lectura, nunca un script copiado.

Cada funcion de lectura recibe un cursor (dictionary=True) de MySQL y la
ventana [desde, hasta) y devuelve {order_id: orden}, con:

    order_id         str
    fecha            datetime de la venta (hora de CDMX segun el Playbook)
    estado_mkp       estado del marketplace (informativo)
    cobrada          el marketplace cobro la venta
    muestra          venta de $0 (no es venta: Playbook 3.1)
    reembolso_total  el cliente ya recibio el 100% de vuelta
    referencia       lo que pago el cliente, con IVA: contra esto se valida
                     el total de la factura (TOTAL_DIFF)
    referencias_alt  otros totales validos (ej. producto + envio)
    so_mkp           SO que trae el marketplace pre-cruzada
    detalle          dict que se guarda en raw_json

Reglas: Playbook Soporte de Ventas v2.6, seccion 4, y propuesta del 24-nov.
"""


def _num(v):
    if v is None or v == '':
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _suma(filas, campo):
    return round(sum(_num(f[campo]) for f in filas), 2)


# =============================================================================
# TIKTOK
# =============================================================================
# Una fila por linea de orden. La devolucion viene en la misma fila
# (return_type, refund_subtotal), no en una fila aparte.
#
# Cobrada: cualquier estado salvo UNPAID y CANCELLED. Se factura al OUT o tras
# la gracia, no al DELIVERED (el Playbook reconoce la venta en DELIVERED, pero
# esperar la entrega cruza meses y choca con las 72 h del SAT).
#
# Lo que pago el cliente (= total de la SO; 99.6% cuadra al peso en oct-nov):
#   orden de una linea sin devolucion y con liquidacion:
#       revenue_amount_transaction - platform_discount
#   resto: suma de sale_price
# platform_discount (promocion pagada por TikTok) NO va en la factura: como en
# Amazon, se registra al aplicar el cobro (403.01.004 + 208.01.001).
# =============================================================================
def leer_tiktok(cur, desde, hasta, order_ids=None):
    sql = "SELECT * FROM somos_reyes.tiktok_financial_transactions_master WHERE "
    if order_ids:
        sql += f"order_id IN ({','.join(['%s'] * len(order_ids))})"
        cur.execute(sql, tuple(order_ids))
    else:
        sql += "create_time >= %s AND create_time < %s"
        cur.execute(sql, (desde, hasta))

    por_orden = {}
    for f in cur.fetchall():
        por_orden.setdefault(str(f['order_id']), []).append(f)

    ordenes = {}
    for oid, filas in por_orden.items():
        estado = filas[0]['order_status']
        con_dev = any(f['return_type'] for f in filas)
        promo = _suma(filas, 'platform_discount')
        revenue = _num(filas[0]['revenue_amount_transaction'])
        if len(filas) == 1 and not con_dev and revenue:
            producto, fuente = round(revenue - promo, 2), 'revenue-promo'
        else:
            producto, fuente = _suma(filas, 'sale_price'), 'sale_price'
        con_stmt = any(f['statement_id'] for f in filas)
        envio = None
        if con_stmt:
            envio = round(max(_num(f['customer_paid_shipping_fee_transaction']) for f in filas)
                          - max(_num(f['refunded_customer_shipping_fee_transaction']) for f in filas), 2)
        reembolso = _suma(filas, 'refund_subtotal')

        alt = []
        if envio and envio > 0:
            alt.append(round(producto + envio, 2))
        ordenes[oid] = {
            'order_id': oid,
            'fecha': filas[0]['create_time'],
            'estado_mkp': estado,
            'cobrada': estado not in ('UNPAID', 'CANCELLED'),
            'muestra': producto == 0 and promo == 0,
            'reembolso_total': producto > 0 and reembolso >= producto - 1.0,
            'referencia': producto,
            'referencias_alt': alt,
            'so_mkp': sorted({f['so'] for f in filas if f['so']}),
            'detalle': {
                'producto': producto, 'fuente_producto': fuente,
                'promo_plataforma': promo, 'envio': envio,
                'reembolso': reembolso, 'lineas': len(filas),
                'return_type': ','.join(sorted({f['return_type'] for f in filas if f['return_type']})),
                'statement_id': ','.join(sorted({str(f['statement_id']) for f in filas if f['statement_id']})),
                'rts_time': str(filas[0]['rts_time'] or ''),
            },
        }
    return ordenes


# =============================================================================
# COPPEL
# =============================================================================
# Una fila por linea. Cobrada = debited_date con valor (Coppel proceso el
# pago). Lo que pago el cliente = suma de total_price de las lineas cobradas.
# NO se usa total_tax: es el 16% del precio CON IVA y no cuadra (dic-2025:
# 645,834 vs 556,752 reales). La base que pone Odoo (precio / 1.16) es la
# correcta: total_price_without_tax = total_price / 1.16 en todas las filas.
# Todo es producto: Coppel no tiene envio ni promociones (Playbook 4).
# =============================================================================
def leer_coppel(cur, desde, hasta, order_ids=None):
    sql = "SELECT * FROM somos_reyes.coppel_financial_transactions_master WHERE "
    if order_ids:
        sql += f"order_id IN ({','.join(['%s'] * len(order_ids))})"
        cur.execute(sql, tuple(order_ids))
    else:
        sql += "created_date >= %s AND created_date < %s"
        cur.execute(sql, (desde, hasta))

    por_orden = {}
    for f in cur.fetchall():
        por_orden.setdefault(f['order_id'], []).append(f)

    ordenes = {}
    for oid, filas in por_orden.items():
        cobradas = [f for f in filas if f['debited_date']]
        base = cobradas or filas
        total = _suma(base, 'total_price')
        reembolsadas = [f for f in cobradas
                        if f['order_line_state'] == 'REFUNDED'
                        and _num(f['refund_amount']) >= _num(f['total_price']) - 1.0]
        ordenes[oid] = {
            'order_id': oid,
            'fecha': filas[0]['created_date'],
            'estado_mkp': '/'.join(sorted({f['order_line_state'] for f in filas})),
            'cobrada': bool(cobradas),
            'muestra': total == 0,
            'reembolso_total': bool(cobradas) and len(reembolsadas) == len(cobradas),
            'referencia': total,
            'referencias_alt': [],
            'so_mkp': sorted({f['so'] for f in filas if f['so']}),
            'detalle': {
                'total_price': total,
                'base_sin_iva': round(total / 1.16, 2),
                'reembolso': _suma(filas, 'refund_amount'),
                'lineas': len(filas),
                'debited_date': str(min(f['debited_date'] for f in cobradas)) if cobradas else '',
                'shipped_date': str(min((f['shipped_date'] for f in filas if f['shipped_date']),
                                        default='') or ''),
                'order_line_ids': [f['order_line_id'] for f in filas],
            },
        }
    return ordenes


# =============================================================================
# CATALOGO
# =============================================================================
# grace_days: dias desde la venta antes de facturar sin OUT. Con OUT se
# factura sin esperar. Evita CFDI de ordenes que se cancelan enseguida.
FUENTES = {
    'TIKTOK': {
        'marketplace': 'TIKTOK',
        'equipo': 'Team_TikTok',
        'cxc': '105.01.011',
        'leer': leer_tiktok,
        'grace_days': 3,
        'etiqueta': 'TikTok Shop',
    },
    'COPPEL': {
        'marketplace': 'COPPEL',
        'equipo': 'Team_Coppel',
        'cxc': '105.01.009',
        'leer': leer_coppel,
        'grace_days': 3,
        'etiqueta': 'Coppel',
    },
}
