"""
00_diag_mkp_billing.py -- SOLO LECTURA. Fase F0 del motor de facturacion de
marketplaces (TikTok y Coppel).

Pregunta que responde: de lo que el marketplace dice que se vendio en el
periodo, que hay en Odoo (SO, estado de facturacion) y cuanto difiere el monto
del marketplace contra la SO. Es el cruce de la regla 6 antes de automatizar.

Reglas de venta (Playbook Soporte de Ventas v2.6, seccion 4):
  TikTok  venta = order_status DELIVERED o COMPLETED, fecha create_time.
          Producto (lo que pago el cliente):
            orden de una linea sin devolucion y con liquidacion:
                revenue_amount_transaction - platform_discount
            resto: suma de sale_price
          Promocion pagada por TikTok = suma de platform_discount
          Envio = customer_paid_shipping_fee_transaction
                  - refunded_customer_shipping_fee_transaction, una vez por orden
                  (solo existe cuando ya hay statement)
          Muestras: venta $0 -> no es venta.
  Coppel  propuesta de facturacion: linea cobrada = debited_date con valor.
          Importe = total_price (c/IVA); base = total_price / 1.16.
          NO se usa total_tax (es 16% del precio con IVA, no cuadra).

Fuentes (MySQL, somos_reyes):
  tiktok_financial_transactions_master   una fila por linea
  coppel_financial_transactions_master   una fila por linea
  odoo_new_sale_order_live               SO de Odoo (al dia). Cruce por
                                         channel_order_reference = orden y por
                                         el campo so que trae el marketplace.

Las replicas de facturas en MySQL (odoo_new_account_move/_invoice) estan
congeladas, asi que la factura se lee de Odoo solo con --odoo (lectura por
XML-RPC; contra produccion la corre Sergio).

Uso:
  python 00_diag_mkp_billing.py [desde] [hasta] [--odoo]
  desde/hasta: AAAA-MM-DD, hasta exclusivo. Default: 2026-10-01 a manana.

Salida: CSV por orden en esta carpeta + resumen en consola.
"""
import os
import sys
import csv
import ast
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timedelta
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

ARGS = [a for a in sys.argv[1:] if not a.startswith('--')]
DESDE = ARGS[0] if len(ARGS) > 0 else '2026-10-01'
HASTA = ARGS[1] if len(ARGS) > 1 else (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
CON_ODOO = '--odoo' in sys.argv
CARPETA = os.path.dirname(os.path.abspath(__file__))
TOLERANCIA = 1.00          # pesos, igual que el C-16 del Playbook

CXC_ESPERADA = {'TIKTOK': '105.01.011', 'COPPEL': '105.01.009'}

db = mysql.connector.connect(host=os.getenv('DB_HOST'), user=os.getenv('DB_USER'),
                             password=os.getenv('DB_PASSWORD'), database='somos_reyes')
cur = db.cursor(dictionary=True)


def num(v):
    """Decimal, texto o None -> float (None se queda en None)."""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def suma(filas, campo):
    return round(sum(num(f[campo]) or 0 for f in filas), 2)


def por_lotes(ids, sql):
    """Ejecuta sql con IN ({}) en lotes de 1000. Devuelve todas las filas."""
    ids, filas = list(ids), []
    for i in range(0, len(ids), 1000):
        ch = ids[i:i + 1000]
        cur.execute(sql.format(','.join(['%s'] * len(ch))), ch)
        filas += cur.fetchall()
    return filas


# =============================================================================
# 1. TIKTOK: una fila por orden
# =============================================================================
def ordenes_tiktok():
    cur.execute("""
        SELECT * FROM tiktok_financial_transactions_master
        WHERE create_time >= %s AND create_time < %s""", (DESDE, HASTA))
    por_orden = defaultdict(list)
    for f in cur.fetchall():
        por_orden[str(f['order_id'])].append(f)

    ordenes = {}
    for oid, filas in por_orden.items():
        estado = filas[0]['order_status']
        con_dev = any(f['return_type'] for f in filas)
        promo = suma(filas, 'platform_discount')
        revenue = num(filas[0]['revenue_amount_transaction'])
        if len(filas) == 1 and not con_dev and revenue:
            producto, fuente = round(revenue - promo, 2), 'revenue-promo'
        else:
            producto, fuente = suma(filas, 'sale_price'), 'sale_price'
        pagado_envio = max((num(f['customer_paid_shipping_fee_transaction']) or 0) for f in filas)
        reemb_envio = max((num(f['refunded_customer_shipping_fee_transaction']) or 0) for f in filas)
        con_stmt = any(f['statement_id'] for f in filas)
        envio = round(pagado_envio - reemb_envio, 2) if con_stmt else None

        if estado in ('DELIVERED', 'COMPLETED'):
            venta = 'VENTA'
        elif estado in ('CANCELLED', 'UNPAID'):
            venta = 'NO_VENTA_CANCELADA'
        else:
            venta = 'NO_VENTA_EN_CAMINO'      # IN_TRANSIT, AWAITING_*, ON_HOLD
        if venta == 'VENTA' and producto == 0 and promo == 0:
            venta = 'MUESTRA_0'

        ordenes[oid] = {
            'mkp': 'TIKTOK', 'order_id': oid,
            'fecha': filas[0]['create_time'],
            'estado_mkp': estado, 'venta': venta, 'lineas': len(filas),
            'con_devolucion': 'SI' if con_dev else '',
            'producto': producto, 'fuente_producto': fuente,
            'promo_plataforma': promo, 'envio': envio,
            'con_statement': 'SI' if con_stmt else '',
            'reembolso': suma(filas, 'refund_subtotal'),
            'so_mkp': {f['so'] for f in filas if f['so']},
        }
    return ordenes


# =============================================================================
# 2. COPPEL: una fila por orden
# =============================================================================
def ordenes_coppel():
    cur.execute("""
        SELECT * FROM coppel_financial_transactions_master
        WHERE created_date >= %s AND created_date < %s""", (DESDE, HASTA))
    por_orden = defaultdict(list)
    for f in cur.fetchall():
        por_orden[f['order_id']].append(f)

    ordenes = {}
    for oid, filas in por_orden.items():
        cobradas = [f for f in filas if f['debited_date']]
        estados = sorted({f['order_line_state'] for f in filas})
        if cobradas:
            venta = 'VENTA'
        elif 'CANCELED' in estados:
            venta = 'NO_VENTA_CANCELADA'
        else:
            venta = 'NO_VENTA_SIN_COBRO'      # WAITING_DEBIT_PAYMENT, etc.

        ordenes[oid] = {
            'mkp': 'COPPEL', 'order_id': oid,
            'fecha': filas[0]['created_date'],
            'estado_mkp': '/'.join(estados), 'venta': venta, 'lineas': len(filas),
            'con_devolucion': 'SI' if any(f['order_line_state'] == 'REFUNDED' for f in filas) else '',
            # Coppel: todo es producto (sin envio ni promociones)
            'producto': suma(cobradas or filas, 'total_price'), 'fuente_producto': 'total_price',
            'promo_plataforma': 0.0, 'envio': 0.0, 'con_statement': '',
            'reembolso': suma(filas, 'refund_amount'),
            'mes_debited': min(f['debited_date'] for f in cobradas).strftime('%Y-%m') if cobradas else '',
            'so_mkp': {f['so'] for f in filas if f['so']},
        }
    return ordenes


# =============================================================================
# 3. SO de Odoo por orden
# =============================================================================
def cruzar_so(ordenes):
    campos = """name, state, invoice_status, team_name, amount_total, effective_date,
                channel_order_reference, invoice_ids, date_order"""
    filas = por_lotes(ordenes.keys(), f"""
        SELECT {campos} FROM odoo_new_sale_order_live
        WHERE channel_order_reference IN ({{}})""")
    nombres = {n for o in ordenes.values() for n in o['so_mkp']}
    filas += por_lotes(nombres, f"SELECT {campos} FROM odoo_new_sale_order_live WHERE name IN ({{}})")

    so_por_nombre = {f['name']: f for f in filas}
    so_por_orden = defaultdict(dict)
    for f in filas:
        if f['channel_order_reference'] in ordenes:
            so_por_orden[f['channel_order_reference']][f['name']] = f
    for oid, o in ordenes.items():
        for n in o['so_mkp']:
            if n in so_por_nombre:
                so_por_orden[oid][n] = so_por_nombre[n]
    return so_por_orden


def ids_factura(texto):
    """'[904610, 904611]' -> [904610, 904611]."""
    try:
        v = ast.literal_eval(texto) if texto else []
        return [int(x) for x in v] if isinstance(v, (list, tuple)) else []
    except (ValueError, SyntaxError):
        return []


def clasificar(o, sos):
    """Estatus F0 de la orden. Una SO cancelada tambien se factura (regla del
    proyecto); se separa para medirla."""
    vigentes = [s for s in sos if s['state'] in ('sale', 'done')]
    if o['venta'] != 'VENTA':
        facturada = any(s['invoice_status'] == 'invoiced' for s in sos)
        return o['venta'] + ('_PERO_FACTURADA' if facturada else '')
    if not sos:
        return 'SIN_SO'
    if len(vigentes) > 1:
        return 'SO_MULTIPLE'
    if not vigentes:
        estados = {s['state'] for s in sos}
        return 'SO_CANCELADA' if 'cancel' in estados else 'SO_BORRADOR'
    s = vigentes[0]
    if s['invoice_status'] == 'invoiced':
        return 'FACTURADA'
    if s['invoice_status'] == 'to invoice':
        return 'POR_FACTURAR_SIN_OUT' if not s['effective_date'] else 'POR_FACTURAR_CON_OUT'
    return 'SO_NO_FACTURABLE'        # invoice_status 'no'


# =============================================================================
# 4. (Opcional) facturas en Odoo, solo lectura
# =============================================================================
def leer_facturas(move_ids):
    """{move_id: {...}} con nombre, estado, tipo, ref, total, UUID y CxC."""
    if os.getenv('MKP_ENTORNO', 'prod') == 'staging':
        url, base = os.getenv('ODOO_TEST_URL'), os.getenv('ODOO_TEST_DB')
    else:
        url = os.getenv('ODOO_URL') or os.getenv('odoo_urlV18')
        base = os.getenv('ODOO_DB') or os.getenv('odoo_dbV18')
    user = os.getenv('ODOO_USER') or os.getenv('odoo_user_dataV18')
    pwd = os.getenv('ODOO_PASSWORD') or os.getenv('odoo_password_dataV18')
    uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(base, user, pwd, {})
    if not uid:
        raise Exception(f'No se pudo autenticar contra Odoo ({base}).')
    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object', allow_none=True)

    ids, facturas = list(move_ids), {}
    for i in range(0, len(ids), 500):
        ch = ids[i:i + 500]
        for m in models.execute_kw(base, uid, pwd, 'account.move', 'read', [ch], {
                'fields': ['name', 'state', 'move_type', 'ref', 'amount_total',
                           'invoice_date', 'l10n_mx_edi_cfdi_uuid']}):
            m['cxc'] = ''
            facturas[m['id']] = m
        for ln in models.execute_kw(base, uid, pwd, 'account.move.line', 'search_read', [[
                ('move_id', 'in', ch), ('account_id.account_type', '=', 'asset_receivable')]],
                {'fields': ['move_id', 'account_id']}):
            facturas[ln['move_id'][0]]['cxc'] = ln['account_id'][1].split(' ')[0]
    print(f'Facturas leidas de Odoo ({base}): {len(facturas)}')
    return facturas


# =============================================================================
# MAIN
# =============================================================================
print(f'Periodo: {DESDE} a {HASTA} (exclusivo)')
ordenes = {}
ordenes.update(ordenes_tiktok())
ordenes.update(ordenes_coppel())
print(f'Ordenes: TikTok {sum(o["mkp"] == "TIKTOK" for o in ordenes.values())}, '
      f'Coppel {sum(o["mkp"] == "COPPEL" for o in ordenes.values())}')

so_por_orden = cruzar_so(ordenes)

facturas = {}
if CON_ODOO:
    todos = {mid for sos in so_por_orden.values() for s in sos.values()
             for mid in ids_factura(s['invoice_ids'])}
    facturas = leer_facturas(todos)

filas_csv = []
for oid, o in ordenes.items():
    sos = list(so_por_orden.get(oid, {}).values())
    estatus = clasificar(o, sos)
    vig = [s for s in sos if s['state'] in ('sale', 'done')]
    so_total = round(sum(float(s['amount_total'] or 0) for s in vig), 2) if vig else None
    dif = round(so_total - o['producto'], 2) if so_total is not None else None
    dif_envio = (round(so_total - o['producto'] - o['envio'], 2)
                 if so_total is not None and o['envio'] is not None else None)

    fila = {
        'mkp': o['mkp'], 'order_id': oid,
        'mes': o['fecha'].strftime('%Y-%m'), 'fecha': o['fecha'],
        'mes_debited': o.get('mes_debited', ''),
        'estado_mkp': o['estado_mkp'], 'estatus_f0': estatus,
        'lineas': o['lineas'], 'con_devolucion': o['con_devolucion'],
        'producto': o['producto'], 'fuente_producto': o['fuente_producto'],
        'promo_plataforma': o['promo_plataforma'], 'envio': o['envio'],
        'con_statement': o['con_statement'], 'reembolso': o['reembolso'],
        'so': ','.join(s['name'] for s in sos),
        'so_equipo': ','.join(sorted({s['team_name'] or '' for s in sos})),
        'so_estado': ','.join(f"{s['state']}/{s['invoice_status']}" for s in sos),
        'so_effective_date': ','.join(str(s['effective_date'] or '') for s in vig),
        'so_total': so_total,
        'dif_so_vs_producto': dif,
        'dif_so_vs_producto_mas_envio': dif_envio,
        'monto_cuadra': ('SI' if dif is not None and (abs(dif) <= TOLERANCIA or
                         (dif_envio is not None and abs(dif_envio) <= TOLERANCIA))
                         else ('NO' if dif is not None else '')),
    }

    if CON_ODOO:
        movs = [facturas[mid] for s in sos for mid in ids_factura(s['invoice_ids']) if mid in facturas]
        inv = [m for m in movs if m['move_type'] == 'out_invoice' and m['state'] == 'posted']
        nc = [m for m in movs if m['move_type'] == 'out_refund' and m['state'] == 'posted']
        fila.update({
            'facturas': ','.join(m['name'] for m in inv),
            'facturas_ref': ','.join(m['ref'] or '' for m in inv),
            'facturas_cxc': ','.join(sorted({m['cxc'] for m in inv})),
            'facturas_total': round(sum(m['amount_total'] for m in inv), 2),
            'ncs': ','.join(m['name'] for m in nc),
            'flag_odoo': ' '.join(x for x in [
                'DOBLE_FACTURA' if len(inv) > 1 else '',
                'CXC_INCORRECTA' if inv and any(m['cxc'] != CXC_ESPERADA[o['mkp']] for m in inv) else '',
                'SIN_UUID' if any(not m['l10n_mx_edi_cfdi_uuid'] for m in inv) else '',
            ] if x),
        })
    filas_csv.append(fila)

nombre = os.path.join(CARPETA, f'diag_mkp_billing_{DESDE}_{HASTA}_{datetime.now():%Y%m%d_%H%M}.csv')
with open(nombre, 'w', newline='', encoding='utf-8-sig') as fh:
    w = csv.DictWriter(fh, fieldnames=list(filas_csv[0]))
    w.writeheader()
    w.writerows(filas_csv)


# =============================================================================
# RESUMEN
# =============================================================================
def resumen(titulo, clave):
    print(f'\n== {titulo}')
    tabla = defaultdict(lambda: [0, 0.0])
    for f in filas_csv:
        k = clave(f)
        tabla[k][0] += 1
        tabla[k][1] += f['producto'] or 0
    for k in sorted(tabla):
        n, monto = tabla[k]
        print(f'  {" | ".join(k):<55} {n:>6}  ${monto:>14,.2f}')


resumen('Estatus por marketplace y mes (ordenes, producto c/IVA)',
        lambda f: (f['mkp'], f['mes'], f['estatus_f0']))
resumen('Monto: SO contra marketplace (solo VENTA con SO vigente)',
        lambda f: (f['mkp'], f['fuente_producto'], f['monto_cuadra'] or 'sin SO'))
resumen('Equipo de la SO (si no es el del marketplace, sale en el 1a1 con otra CxC)',
        lambda f: (f['mkp'], f['so_equipo'] or 'sin SO'))
if CON_ODOO:
    resumen('Flags de Odoo', lambda f: (f['mkp'], f.get('flag_odoo') or 'OK'))

no_cuadra = sorted((f for f in filas_csv if f['monto_cuadra'] == 'NO'),
                   key=lambda f: -abs(f['dif_so_vs_producto']))
print('\n== Mayores diferencias SO vs marketplace')
for f in no_cuadra[:15]:
    envio = f'{f["envio"]:,.2f}' if f['envio'] is not None else '-'
    print(f"  {f['mkp']:<6} {f['order_id']:<22} {f['so']:<12} producto {f['producto']:>10,.2f} "
          f"envio {envio:>8} SO {f['so_total']:>10,.2f} "
          f"dif {f['dif_so_vs_producto']:>9,.2f} ({f['fuente_producto']}, lineas {f['lineas']})")

print(f'\nCSV: {nombre}')
db.close()
