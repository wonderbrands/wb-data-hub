"""
14_base_historico_amazon.py -- SOLO LECTURA (MySQL). Base para Finanzas.

Ventas de Amazon cobradas entre el 1-ene y el 30-sep-2026 (fecha de posteo de
la transaccion 'Order Payment') que NO quedaron facturadas, separadas por
categoria y por Odoo.

Fuentes:
  amz_transactions          universo: ordenes que Amazon cobro
  odoo_new_sale_order_live  SO de Odoo 15 y Odoo 18 por channel_order_reference
                            (= orden de Amazon). Las de Odoo 15 estan congeladas
                            al 16-abr-2026 (migracion).
  odoo_master_txns_c        segunda fuente de SO cuando la primera no la trae
  amz_b2b_invoices          CFDI que emitio Amazon Business
  amz_transactions Refund   devoluciones
  amazon_orders             OrderTotal, PurchaseDate, canal (FBA / propio)
  finance.mkp_billing_prod  lo que ya facturo el flujo de produccion

Odoo 15: SO3xxxxxx / SO4xxxxxx. Odoo 18: SO10xxxxxx y los numeros bajos (S0...).
"""
import os
import re
import csv
from collections import defaultdict
from datetime import datetime, timedelta
import mysql.connector
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Claude_proyects\wb-data-hub\.env')
DESDE, HASTA = '2026-01-01', '2026-10-01'
CARPETA = os.path.dirname(os.path.abspath(__file__))

db = mysql.connector.connect(host=os.getenv('DB_HOST'), user=os.getenv('DB_USER'),
                             password=os.getenv('DB_PASSWORD'), database='somos_reyes')
cur = db.cursor(dictionary=True)


def por_lotes(ids, sql):
    """Ejecuta sql con IN ({}) en lotes de 1000. Devuelve todas las filas."""
    ids, filas = list(ids), []
    for i in range(0, len(ids), 1000):
        ch = ids[i:i + 1000]
        cur.execute(sql.format(','.join(['%s'] * len(ch))), ch)
        filas += cur.fetchall()
    return filas


# 1. Universo: ordenes cobradas por Amazon
cur.execute("""
    SELECT order_id, MIN(posted_date) AS posted
    FROM amz_transactions
    WHERE description = 'Order Payment' AND order_id IS NOT NULL
      AND posted_date >= %s AND posted_date < %s
    GROUP BY order_id""", (DESDE, HASTA))
universo = {r['order_id']: r for r in cur.fetchall()}
ids = list(universo)
print(f'Ordenes cobradas: {len(ids)}')

# 2. SO por orden (dos fuentes)
so = defaultdict(dict)
for r in por_lotes(ids, """SELECT channel_order_reference k, name, state, invoice_status,
                                  amount_total
                           FROM odoo_new_sale_order_live
                           WHERE channel_order_reference IN ({})"""):
    so[r['k']][r['name']] = r
faltan = [k for k in ids if k not in so]
for r in por_lotes(faltan, """SELECT marketplace_order_id k, order_id name,
                                     MAX(odoo_state) state, MAX(invoice_status) invoice_status,
                                     NULL amount_total
                              FROM odoo_master_txns_c
                              WHERE marketplace_order_id IN ({})
                              GROUP BY marketplace_order_id, order_id"""):
    so[r['k']][r['name']] = r

# 3. Datos de Amazon
ao = {r['k']: r for r in por_lotes(ids, """
    SELECT amazonorderid k, MIN(PurchaseDate) pd, MAX(FulfillmentChannel) canal,
           MAX(CASE WHEN OrderTotalCurrency = 'MXN' AND OrderTotalAmount > 0
                    THEN CAST(OrderTotalAmount AS DECIMAL(14,2)) END) tot
    FROM amazon_orders WHERE amazonorderid IN ({}) GROUP BY amazonorderid""")}
cfdi = {r['k']: r for r in por_lotes(ids, """
    SELECT order_id k, MIN(date) f, GROUP_CONCAT(DISTINCT uuid) uuids
    FROM amz_b2b_invoices
    WHERE receipt_type = 'I' AND (status IS NULL OR status <> 'CANCEL')
      AND (cancellation_date IS NULL OR cancellation_date = '')
      AND order_id IN ({}) GROUP BY order_id""")}
devuelta = {r['k'] for r in por_lotes(ids, """
    SELECT DISTINCT order_id k FROM amz_transactions
    WHERE description = 'Refund' AND order_id IN ({})""")}
control = {r['k']: r['status'] for r in por_lotes(ids, """
    SELECT mkp_order_id k, status FROM finance.mkp_billing_prod
    WHERE marketplace = 'AMAZON' AND mkp_order_id IN ({})""")}


def version(nombres):
    v = {'Odoo 15' if re.fullmatch(r'SO[34]\d{6}', n) else 'Odoo 18' for n in nombres}
    return ' + '.join(sorted(v))


def categoria(k, sos):
    """(categoria, accion). Orden de precedencia: lo ya facturado primero."""
    if control.get(k) in ('ODOO_INVOICED', 'ODOO_B2B_LINKED', 'ALREADY_ODOO_INVOICED'):
        return 'YA_FACTURADA', ''
    if any(s['invoice_status'] == 'invoiced' for s in sos.values()):
        return 'YA_FACTURADA', ''
    if not sos:
        return 'SIN_SO', 'La venta no llego a Odoo: revisar con Yuju'
    if k in cfdi:
        return 'B2B_CFDI_AMAZON', 'Registrar el CFDI de Amazon en Odoo (no timbrar)'
    estados = {s['state'] for s in sos.values()}
    if estados == {'cancel'}:
        if k in devuelta:
            return 'SO_CANCELADA_DEVUELTA', 'Cobrada y devuelta: factura + NC, o nada (decidir)'
        return 'SO_CANCELADA', 'Amazon cobro y la SO se cancelo: facturar'
    if estados & {'draft', 'sent'}:
        return 'SO_BORRADOR', 'Confirmar la SO y facturar'
    if any(s['invoice_status'] == 'to invoice' for s in sos.values()):
        return 'SO_POR_FACTURAR', 'Facturar'
    return 'SO_SIN_CANTIDAD_A_FACTURAR', 'SO confirmada sin nada a facturar en Odoo (revisar)'


filas, resumen = [], defaultdict(lambda: [0, 0.0])
for k in ids:
    sos = so.get(k, {})
    cat, accion = categoria(k, sos)
    a = ao.get(k, {})
    venta = a.get('pd') or universo[k]['posted']
    if isinstance(venta, str):
        venta = datetime.fromisoformat(venta.replace('Z', '').replace('T', ' ')[:19])
    venta -= timedelta(hours=6)          # UTC -> CDMX
    odoo = version(sos) if sos else ''
    importe = float(a.get('tot') or 0)
    resumen[(cat, odoo)][0] += 1
    resumen[(cat, odoo)][1] += importe
    if cat == 'YA_FACTURADA':
        continue
    filas.append({
        'categoria': cat,
        'accion_sugerida': accion,
        'orden_amazon': k,
        'so': ', '.join(sorted(sos)),
        'odoo': odoo,
        'estado_so': ', '.join(sorted({str(s['state']) for s in sos.values()})),
        'facturacion_so': ', '.join(sorted({str(s['invoice_status']) for s in sos.values()})),
        'canal': {'AFN': 'FBA', 'MFN': 'Propio'}.get(a.get('canal'), a.get('canal') or ''),
        'fecha_venta': f'{venta:%Y-%m-%d}',
        'mes_venta': f'{venta:%Y-%m}',
        'importe_amazon': f'{importe:.2f}',
        'devolucion_amazon': 'SI' if k in devuelta else '',
        'cfdi_amazon_uuid': cfdi[k]['uuids'] if k in cfdi else '',
        'cfdi_amazon_fecha': f"{cfdi[k]['f']}"[:10] if k in cfdi else '',
    })

filas.sort(key=lambda r: (r['categoria'], r['odoo'], r['mes_venta'], r['orden_amazon']))
nombre = os.path.join(CARPETA, f'base_saneamiento_historico_amazon_{datetime.now():%Y%m%d_%H%M}.csv')
with open(nombre, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=list(filas[0]))
    w.writeheader()
    w.writerows(filas)

print(f'\nArchivo: {nombre} ({len(filas)} filas)\n')
print('categoria|odoo|ordenes|importe_amazon')
for (cat, odoo), (n, imp) in sorted(resumen.items()):
    print(f'{cat}|{odoo}|{n}|{imp:,.0f}')
