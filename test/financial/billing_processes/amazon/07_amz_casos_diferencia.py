"""
07_amz_casos_diferencia.py
--------------------------
EXPEDIENTE DE CASOS CON DIFERENCIA -- SOLO LECTURA

Arma, para Finanzas, el expediente de cada orden de Amazon en la que lo que
pago el cliente, lo que dicen las transacciones de Amazon, lo que se facturo y
lo que Amazon libero NO coinciden. No escribe nada: ni en MySQL ni en Odoo.


PARA QUE ES
-----------
Decisiones de la reunion con Finanzas del 2-oct-2026:

  D3/D4  "Extraer un conjunto de test con estos casos, para que Finanzas pueda
         revisar a detalle lo que sucede y decidir que hacer." Propuesta previa:
         una cuenta por cobrar especifica para el excedente, con el IVA ya
         trasladado. Antes de crearla, Finanzas quiere ver los casos.

  D6     "No deberian existir casos especiales. Reglas especificas para
         diferencias especificas: consultar exactamente que son esas
         diferencias (ej. marketing)."

Este script es la evidencia para esas dos decisiones. No decide nada: junta en
un solo lugar todo lo que hay de cada caso para que la regla se escriba sobre
datos y no sobre suposiciones.


QUE CASOS TOMA
--------------
De las tablas de control (las del lote de pruebas en staging):

  TOTAL_DIFF                  01 no facturo: la SO no cuadra con Amazon.
  PROMO_AMAZON                01 facturo por lo que pago el cliente porque las
                              transacciones traen mas (regla B2C de promocion).
  B2B_CFDI_VS_TRANSACCIONES   B2B donde el CFDI de Amazon (lo facturado) no
                              coincide con las transacciones. Ej.
                              701-2593380-9743412: CFDI 1,741.13, transacciones
                              2,597.69.
  EXCEDENTE                   03 cobro: Amazon libero mas que el saldo de la
                              factura y la diferencia quedo sin aplicar.

Mas las ordenes que se pasen a mano en CASOS_ORDENES (por ejemplo, los casos
de los lotes 1 y 2, cuyas filas de control ya se purgaron).


QUE ENTREGA
-----------
Tres CSV en esta carpeta, con el mismo sello de fecha:

  casos_diferencia_<sello>_resumen.csv
      Una fila por orden: tipo, por que entro, lo que pago el cliente, el
      total segun transacciones, la diferencia (y como se partiria en base e
      IVA si el IVA ya viene incluido), lo facturado, la SO y las facturas de
      Odoo, lo liberado / aplicado / excedente por settlement, los conceptos de
      las transacciones distintos de cero y una PISTA de clasificacion.

  casos_diferencia_<sello>_transacciones.csv
      TODAS las transacciones de esas ordenes, con todas las columnas de
      amz_transaction_items, cualquier description (no solo Order Payment) y
      la marca cuenta_en_factura: si esa fila es la que sobrevive a la
      deduplicacion por pieza que usan 01/02/03. Sirve para ver de donde sale
      cada peso.

  casos_diferencia_<sello>_amazon_orders.csv
      Las filas crudas de amazon_orders (N por orden), con todas sus columnas.
      Ahi viven OrderTotal y, si Amazon los manda, los datos de promocion.


LA PISTA NO ES UNA CONCLUSION
-----------------------------
Es una primera lectura para ordenar la revision:

  PROMO_FINANCIADA_AMAZON     transacciones > lo que pago el cliente. Amazon
                              liquida el precio completo y absorbe el resto.
  ... (MONTO_FIJO)            la diferencia es multiplo de 50: tipico de cupon.
  CLIENTE_PAGO_MAS            el cliente pago mas que las transacciones.
  SIN_DIFERENCIA_AMAZON       cliente = transacciones: la diferencia esta entre
                              la SO de Odoo y Amazon (precio de Yuju, SKU...).
  SIN_DATO_CLIENTE            amazon_orders no trae un OrderTotal inequivoco.
  + PROMO_REBATES             las transacciones traen promo_rebates distinto
                              de cero (promocion pagada por el vendedor).

Con el expediente revisado, cada pista que Finanzas confirme se vuelve una
regla de 01/03 con su propio tratamiento (cuenta, IVA, CFDI).


USO
---
Despues de correr 01 -> 02 -> 03 del lote:

    python 07_amz_casos_diferencia.py

Variables opcionales:

    CASOS_ORDENES   order_id de Amazon separados por coma, se agregan al
                    expediente aunque no esten en control.
    CASOS_DESDE     'YYYY-MM-DD': solo filas de control creadas desde esa
                    fecha (para quedarse con el lote 4).
    CASOS_SIN_ODOO  '1' para no consultar Odoo (sin SO ni facturas).
"""

import os
import re
import sys
import csv
import json
import logging
from datetime import datetime
from decimal import Decimal

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    TBL_BILLING, TBL_PAYMENTS, MARKETPLACE,
    banner, get_db_connection, OdooModelProxy, a_float, parse_settlement_id,
)

CASOS_ORDENES = [o.strip() for o in os.getenv('CASOS_ORDENES', '').split(',')
                 if o.strip()]
CASOS_DESDE = os.getenv('CASOS_DESDE', '').strip() or None
CONSULTAR_ODOO = os.getenv('CASOS_SIN_ODOO', '').strip().lower() not in ('1', 'true', 'si')

# Misma tolerancia que 01 (AMZ_TOLERANCIA_TOTAL).
TOLERANCIA = float(os.getenv('AMZ_TOLERANCIA_TOTAL', '1.00'))

# Trozos para los IN (...) de MySQL y Odoo.
LOTE = 200

# Columnas de amz_transaction_items que forman el total del CFDI. Es la
# formula de 01 (QUERY_LOTE, cfdi_total), validada al centavo.
COLUMNAS_CFDI = ['product_charges', 'shipping', 'shipping_discount',
                 'our_price_discount', 'our_price_tax', 'shipping_tax',
                 'shipping_tax_discount', 'our_price_tax_disc']

# Columnas de transacciones que no son importes aunque sean numericas.
NO_IMPORTES = {'quantity_shipped', 'id'}


def _en_trozos(lista):
    for i in range(0, len(lista), LOTE):
        yield lista[i:i + LOTE]


def _es_numero(v):
    return isinstance(v, (Decimal, float, int)) and not isinstance(v, bool)


def _campo(fila, nombre):
    """Valor de una columna sin importar mayusculas (amazon_orders mezcla
    AmazonOrderId / amazonorderid segun quien la consulte)."""
    for c, v in fila.items():
        if c.lower() == nombre.lower():
            return v
    return None


def _num(texto, patron):
    """Primer numero que sigue a 'patron' en un error_log, o None."""
    m = re.search(patron + r'\s+(-?[\d.]+)', texto or '')
    return float(m.group(1)) if m else None


# =============================================================================
# 1. CASOS DESDE LAS TABLAS DE CONTROL
# =============================================================================

def leer_control(db_conn):
    """Filas de facturacion y de cobros de Amazon en control.

    Devuelve (billing {order_id: fila}, pagos {order_id: [filas]}).
    """
    cursor = db_conn.cursor(dictionary=True)
    filtro, params = '', [MARKETPLACE]
    if CASOS_DESDE:
        filtro, params = ' AND created_at >= %s', [MARKETPLACE, CASOS_DESDE]

    cursor.execute(f"SELECT mkp_order_id, odoo_so_name, status, total_amount, "
                   f"raw_json, error_log, created_at FROM {TBL_BILLING} "
                   f"WHERE marketplace = %s{filtro}", tuple(params))
    billing = {r['mkp_order_id']: r for r in cursor.fetchall()}

    cursor.execute(f"SELECT mkp_order_id, payment_id, amount, date_released, "
                   f"status, error_log FROM {TBL_PAYMENTS} "
                   f"WHERE marketplace = %s{filtro}", tuple(params))
    pagos = {}
    for r in cursor.fetchall():
        pagos.setdefault(r['mkp_order_id'], []).append(r)
    cursor.close()
    return billing, pagos


def _raw(fila):
    try:
        return json.loads(fila['raw_json']) if fila and fila.get('raw_json') else {}
    except (ValueError, TypeError):
        return {}


def elegir_casos(billing, pagos):
    """{order_id: [origenes]} de las ordenes que entran al expediente."""
    casos = {}

    for oid, f in billing.items():
        if f['status'] == 'TOTAL_DIFF':
            casos.setdefault(oid, []).append('TOTAL_DIFF')
        if (f.get('error_log') or '').startswith('PROMO_AMAZON'):
            casos.setdefault(oid, []).append('PROMO_AMAZON')
        raw = _raw(f)
        cfdi = raw.get('cfdi_amazon_business') or {}
        trans = (raw.get('importes') or {}).get('cfdi_total')
        if cfdi.get('total') and trans is not None \
                and abs(a_float(cfdi['total']) - a_float(trans)) > TOLERANCIA:
            casos.setdefault(oid, []).append('B2B_CFDI_VS_TRANSACCIONES')

    for oid, filas in pagos.items():
        if any('EXCEDENTE_NO_APLICADO' in (p.get('error_log') or '') for p in filas):
            casos.setdefault(oid, []).append('EXCEDENTE')
        # Las promociones ya asentadas por 03 (AMZ_PROMO_MODO=asentar) siguen
        # entrando al expediente: D6 pide que todo tipo de diferencia quede
        # en el reporte, este resuelta o no.
        if any('EXCEDENTE_PROMO_ASENTADO' in (p.get('error_log') or '') for p in filas):
            casos.setdefault(oid, []).append('PROMO_ASENTADA')

    for oid in CASOS_ORDENES:
        casos.setdefault(oid, []).append('MANUAL')
    return casos


# =============================================================================
# 2. TRANSACCIONES DE AMAZON
# =============================================================================

QUERY_TRANSACCIONES = """
SELECT a.order_id, a.transaction_id AS trx_id, a.settlement_id,
       a.transaction_status, a.description,
       DATE_SUB(a.posted_date, INTERVAL 6 HOUR) AS posted_cdmx,
       a.posted_date, a.shipment_id, a.total_amount AS total_cabecera,
       b.*
FROM somos_reyes.amz_transactions a
LEFT JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id
WHERE a.order_id IN ({marcas})
ORDER BY a.order_id, a.posted_date, a.transaction_id
"""


def leer_transacciones(db_conn, ordenes):
    """Todas las transacciones (cualquier description) de esas ordenes.

    Devuelve (filas, columnas). Cada fila lleva 'cuenta_en_factura': True si es
    la fila autoritativa de su pieza, con la misma regla que 01/02/03:
    Order Payment, no DEFERRED, pieza = COALESCE(shipment_id, transaction_id),
    gana el posted_date mas reciente.
    """
    filas, columnas = [], []
    cursor = db_conn.cursor(dictionary=True)
    for trozo in _en_trozos(ordenes):
        cursor.execute(QUERY_TRANSACCIONES.format(
            marcas=', '.join(['%s'] * len(trozo))), tuple(trozo))
        filas += cursor.fetchall()
        if not columnas:
            columnas = list(cursor.column_names)
    cursor.close()

    ultima = {}
    for r in filas:
        if r['description'] != 'Order Payment' or r['transaction_status'] == 'DEFERRED':
            continue
        pieza = r['shipment_id'] or r['trx_id']
        if pieza not in ultima or r['posted_date'] > ultima[pieza]:
            ultima[pieza] = r['posted_date']
    for r in filas:
        pieza = r['shipment_id'] or r['trx_id']
        r['cuenta_en_factura'] = (
            r['description'] == 'Order Payment'
            and r['transaction_status'] != 'DEFERRED'
            and ultima.get(pieza) == r['posted_date'])

    # dict.fromkeys: b.* puede repetir nombres de 'a' (el dict ya los fusiono).
    columnas = [c for c in dict.fromkeys(columnas) if c != 'transaction_id']
    return filas, columnas + ['cuenta_en_factura']


def resumir_transacciones(filas):
    """Por orden: total CFDI, conceptos distintos de cero y otros movimientos."""
    por_orden = {}
    for r in filas:
        o = por_orden.setdefault(r['order_id'], {
            'total': 0.0, 'conceptos': {}, 'otros': set(), 'piezas': set()})
        if not r['cuenta_en_factura']:
            if r['description'] != 'Order Payment':
                o['otros'].add(r['description'])
            continue
        o['piezas'].add(r['shipment_id'] or r['trx_id'])
        o['total'] += sum(a_float(r.get(c)) for c in COLUMNAS_CFDI)
        for c, v in r.items():
            if c in ('total_cabecera',) or c in NO_IMPORTES or not _es_numero(v):
                continue
            if c in ('order_id', 'settlement_id', 'shipment_id'):
                continue
            o['conceptos'][c] = o['conceptos'].get(c, 0.0) + a_float(v)

    for o in por_orden.values():
        o['total'] = round(o['total'], 2)
        o['conceptos'] = {c: round(v, 2) for c, v in o['conceptos'].items()
                          if abs(v) >= 0.005}
    return por_orden


# =============================================================================
# 3. AMAZON_ORDERS
# =============================================================================

def so_de_ordenes(db_conn, ordenes):
    """{order_id: SO} desde el puente de Odoo, para las ordenes sin fila en
    control (las de CASOS_ORDENES). Mismo criterio que QUERY_LOTE."""
    so = {}
    cursor = db_conn.cursor(dictionary=True)
    for trozo in _en_trozos(ordenes):
        cursor.execute(
            f"SELECT marketplace_order_id, MAX(order_id) AS so "
            f"FROM somos_reyes.odoo_master_txns_c WHERE marketplace_order_id IN "
            f"({', '.join(['%s'] * len(trozo))}) GROUP BY marketplace_order_id",
            tuple(trozo))
        so.update({r['marketplace_order_id']: r['so'] for r in cursor.fetchall()})
    cursor.close()
    return so


def leer_amazon_orders(db_conn, ordenes):
    """Filas crudas de amazon_orders. Devuelve (filas, columnas)."""
    filas, columnas = [], []
    cursor = db_conn.cursor(dictionary=True)
    for trozo in _en_trozos(ordenes):
        cursor.execute(
            f"SELECT * FROM somos_reyes.amazon_orders WHERE amazonorderid IN "
            f"({', '.join(['%s'] * len(trozo))})", tuple(trozo))
        filas += cursor.fetchall()
        if not columnas:
            columnas = list(cursor.column_names)
    cursor.close()
    return filas, columnas


def resumir_amazon_orders(filas):
    """Por orden: OrderTotal inequivoco (o None) y datos de promocion.

    Mismo criterio que total_cobrado_cliente() de 01: MXN, > 0, y todas las
    filas con el mismo valor. Las columnas de promocion se detectan por nombre
    (promo / discount) porque no todas las ordenes las traen.
    """
    por_orden = {}
    for r in filas:
        o = por_orden.setdefault(_campo(r, 'amazonorderid'),
                                 {'totales': set(), 'promo': set()})
        importe = a_float(_campo(r, 'OrderTotalAmount'))
        if _campo(r, 'OrderTotalCurrency') == 'MXN' and importe > 0:
            o['totales'].add(round(importe, 2))
        for c, v in r.items():
            nombre = c.lower()
            if ('promo' in nombre or 'discount' in nombre) and v not in (None, ''):
                try:
                    if abs(float(v)) < 0.005:
                        continue             # 0, 0.00, '0.00': no hay promocion
                except (TypeError, ValueError):
                    pass                     # texto (PromotionIds): se conserva
                o['promo'].add(f"{c}={v}")
    for o in por_orden.values():
        o['cliente'] = next(iter(o['totales'])) if len(o['totales']) == 1 else None
    return por_orden


# =============================================================================
# 4. ODOO (SOLO LECTURA)
# =============================================================================

def leer_odoo(nombres_so):
    """{so: {'total', 'state', 'facturas': 'nombre tipo total saldo estado; ...'}}."""
    if not nombres_so:
        return {}
    odoo = OdooModelProxy(timeout=300)
    datos = {}
    for trozo in _en_trozos(sorted(nombres_so)):
        for so in odoo.execute_kw('sale.order', 'search_read',
                                  [[('name', 'in', trozo)]],
                                  {'fields': ['name', 'amount_total', 'state']}):
            datos[so['name']] = {'total': so['amount_total'], 'state': so['state'],
                                 'facturas': []}
        for mv in odoo.execute_kw(
                'account.move', 'search_read',
                [[('move_type', 'in', ['out_invoice', 'out_refund']),
                  ('state', '!=', 'cancel'), ('invoice_origin', 'in', trozo)]],
                {'fields': ['name', 'invoice_origin', 'move_type', 'amount_total',
                            'amount_residual', 'payment_state'],
                 'order': 'id asc'}):
            d = datos.setdefault(mv['invoice_origin'],
                                 {'total': None, 'state': None, 'facturas': []})
            tipo = 'NC' if mv['move_type'] == 'out_refund' else 'FAC'
            d['facturas'].append(
                f"{mv['name']} {tipo} total {mv['amount_total']:,.2f} saldo "
                f"{mv['amount_residual']:,.2f} {mv['payment_state']}")
    for d in datos.values():
        d['facturas'] = '; '.join(d['facturas'])
    return datos


# =============================================================================
# 5. COBROS
# =============================================================================

def resumir_cobros(filas):
    """Liberado / aplicado / excedente de una orden, y detalle por settlement.

    'amount' es lo LIBERADO. Lo aplicado y el excedente se leen de la nota que
    deja 03 en error_log (EXCEDENTE_NO_APLICADO liberado X aplicado Y
    sin_aplicar Z); sin nota, un ODOO_PAID se aplico completo.
    """
    liberado = aplicado = excedente = 0.0
    detalle = []
    for p in sorted(filas or [], key=lambda x: str(x['date_released'])):
        monto = a_float(p['amount'])
        liberado += monto
        nota = p.get('error_log') or ''
        sin_aplicar = _num(nota, 'sin_aplicar') or 0.0
        if p['status'] == 'ODOO_PAID':
            # 'aplicado' en minusculas: no confunde con EXCEDENTE_NO_APLICADO.
            leido = _num(nota, 'aplicado') if 'EXCEDENTE' in nota else None
            aplicado += leido if leido is not None else monto
        excedente += sin_aplicar
        detalle.append(f"{parse_settlement_id(p['payment_id'])} "
                       f"{str(p['date_released'])[:10]} {p['status']} "
                       f"liberado {monto:,.2f}"
                       + (f" excedente {sin_aplicar:,.2f}" if sin_aplicar else ''))
    return round(liberado, 2), round(aplicado, 2), round(excedente, 2), '; '.join(detalle)


# =============================================================================
# 6. PISTA
# =============================================================================

def pista(cliente, transacciones, conceptos):
    if cliente is None:
        p = 'SIN_DATO_CLIENTE'
    else:
        dif = round(transacciones - cliente, 2)
        if abs(dif) <= TOLERANCIA:
            p = 'SIN_DIFERENCIA_AMAZON'
        elif dif > 0:
            p = 'PROMO_FINANCIADA_AMAZON'
            if abs(dif / 50 - round(dif / 50)) < 0.0002:
                p += ' (MONTO_FIJO)'
        else:
            p = 'CLIENTE_PAGO_MAS'
    if abs(conceptos.get('promo_rebates', 0.0)) >= 0.005:
        p += ' + PROMO_REBATES'
    return p


# =============================================================================

CABECERA_RESUMEN = [
    'order_id', 'odoo_so', 'tipo', 'origen_caso', 'pista',
    'status_facturacion', 'cliente_pago', 'transacciones_total', 'diferencia',
    'diferencia_base_si_incluye_iva', 'diferencia_iva_si_incluye_iva',
    'cfdi_amazon_total', 'facturado', 'so_total_odoo', 'so_estado_odoo',
    'documentos_odoo', 'n_piezas', 'liberado_total', 'aplicado_total',
    'excedente_total', 'cobros_por_settlement', 'conceptos_transacciones',
    'otros_movimientos', 'promocion_amazon_orders', 'error_log_facturacion',
]


def escribir(ruta, cabecera, filas):
    with open(ruta, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(cabecera)
        for f in filas:
            w.writerow([f.get(c, '') if isinstance(f, dict) else '' for c in cabecera])


def run():
    log.info(f"CASOS_DESDE    : {CASOS_DESDE or '(todas las filas de control)'}")
    log.info(f"CASOS_ORDENES  : {len(CASOS_ORDENES)} orden(es) agregadas a mano")
    log.info(f"Consultar Odoo : {CONSULTAR_ODOO}")
    log.info('')

    db_conn = get_db_connection()
    billing, pagos = leer_control(db_conn)
    casos = elegir_casos(billing, pagos)
    ordenes = sorted(casos)
    log.info(f"{len(billing)} fila(s) de facturacion y {len(pagos)} orden(es) "
             f"con cobros en control. Casos para el expediente: {len(ordenes)}.")
    if not ordenes:
        log.info('No hay casos con diferencia. Nada que exportar.')
        db_conn.close()
        return

    trx, cols_trx = leer_transacciones(db_conn, ordenes)
    por_trx = resumir_transacciones(trx)
    aor, cols_aor = leer_amazon_orders(db_conn, ordenes)
    por_aor = resumir_amazon_orders(aor)
    so_puente = so_de_ordenes(db_conn, [o for o in ordenes
                                        if not (billing.get(o) or {}).get('odoo_so_name')])
    db_conn.close()

    so_de = {o: (billing.get(o) or {}).get('odoo_so_name') or so_puente.get(o)
             for o in ordenes}
    nombres_so = {s for s in so_de.values() if s}
    odoo = {}
    if CONSULTAR_ODOO:
        try:
            odoo = leer_odoo(nombres_so)
        except Exception as e:
            log.error(f"No se pudo consultar Odoo; el resumen sale sin SO ni "
                      f"facturas: {e}")

    resumen = []
    for oid in ordenes:
        f = billing.get(oid) or {}
        raw = _raw(f)
        cfdi = raw.get('cfdi_amazon_business') or {}
        t = por_trx.get(oid, {'total': 0.0, 'conceptos': {}, 'otros': set(),
                              'piezas': set()})
        a = por_aor.get(oid, {'cliente': None, 'promo': set()})
        so = so_de.get(oid) or ''
        o = odoo.get(so, {})
        liberado, aplicado, excedente, det_cobros = resumir_cobros(pagos.get(oid))

        cliente = a['cliente']
        dif = round(t['total'] - cliente, 2) if cliente is not None else None
        base = round(dif / 1.16, 2) if dif is not None else None

        resumen.append({
            'order_id': oid,
            'odoo_so': so,
            'tipo': ('B2B' if cfdi.get('uuid') else 'B2C') if f else '',
            'origen_caso': ' + '.join(casos[oid]),
            'pista': pista(cliente, t['total'], t['conceptos']),
            'status_facturacion': f.get('status', 'SIN_FILA_EN_CONTROL'),
            'cliente_pago': cliente,
            'transacciones_total': t['total'],
            'diferencia': dif,
            'diferencia_base_si_incluye_iva': base,
            'diferencia_iva_si_incluye_iva': (round(dif - base, 2)
                                              if dif is not None else None),
            'cfdi_amazon_total': cfdi.get('total'),
            'facturado': f.get('total_amount'),
            'so_total_odoo': o.get('total'),
            'so_estado_odoo': o.get('state'),
            'documentos_odoo': o.get('facturas'),
            'n_piezas': len(t['piezas']),
            'liberado_total': liberado,
            'aplicado_total': aplicado,
            'excedente_total': excedente,
            'cobros_por_settlement': det_cobros,
            'conceptos_transacciones': '; '.join(
                f"{c} {v:,.2f}" for c, v in sorted(t['conceptos'].items())),
            'otros_movimientos': ', '.join(sorted(t['otros'])),
            'promocion_amazon_orders': '; '.join(sorted(a['promo'])),
            'error_log_facturacion': f.get('error_log'),
        })

    sello = datetime.now().strftime('%Y%m%d_%H%M%S')
    base_ruta = os.path.join(_dir, f'casos_diferencia_{sello}')
    escribir(f'{base_ruta}_resumen.csv', CABECERA_RESUMEN, resumen)
    escribir(f'{base_ruta}_transacciones.csv', cols_trx, trx)
    escribir(f'{base_ruta}_amazon_orders.csv', cols_aor, aor)

    log.info('')
    log.info('=' * 78)
    log.info('EXPEDIENTE DE CASOS CON DIFERENCIA')
    log.info('=' * 78)
    cuenta = {}
    for r in resumen:
        cuenta[r['pista']] = cuenta.get(r['pista'], 0) + 1
    for p in sorted(cuenta):
        log.info(f"  {p:<45}{cuenta[p]:>6}")
    log.info('-' * 78)
    log.info(f"  {'ORDEN':<22}{'TIPO':<5}{'CLIENTE':>11}{'TRANSAC.':>11}"
             f"{'DIF':>10}{'EXCED.':>10}  PISTA")
    for r in resumen:
        log.info(f"  {r['order_id']:<22}{r['tipo']:<5}"
                 f"{a_float(r['cliente_pago']):>11,.2f}"
                 f"{a_float(r['transacciones_total']):>11,.2f}"
                 f"{a_float(r['diferencia']):>10,.2f}"
                 f"{a_float(r['excedente_total']):>10,.2f}  {r['pista']}")
    log.info('-' * 78)
    log.info(f"  Resumen       : {base_ruta}_resumen.csv ({len(resumen)} ordenes)")
    log.info(f"  Transacciones : {base_ruta}_transacciones.csv ({len(trx)} filas)")
    log.info(f"  amazon_orders : {base_ruta}_amazon_orders.csv ({len(aor)} filas)")
    log.info('=' * 78)


if __name__ == '__main__':
    banner('EXPEDIENTE DE CASOS CON DIFERENCIA -- AMAZON (SOLO LECTURA)')
    try:
        run()
    except Exception as e:
        log.error(f'Fallo el expediente: {e}')
        sys.exit(1)
