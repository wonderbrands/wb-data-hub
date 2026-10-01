import xmlrpc.client
import os
import csv
import html
import logging
import time
import socket
import http.client
from collections import defaultdict
from datetime import datetime, timedelta
import mysql.connector

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

RETRYABLE_EXCEPTIONS = (
    xmlrpc.client.ProtocolError,
    ConnectionError,
    TimeoutError,
    socket.error,
    socket.timeout,
    http.client.HTTPException,  # Cubre BadStatusLine, RemoteDisconnected, etc. cuando
                                 # el gateway responde con HTML/estado malformado en vez de XML-RPC
)

# ── Parámetros de las reglas (se citan también en el glosario del correo) ──
DIAS_TOLERANCIA_OUT = 5     # Días que se dan a una SO para tener su OUT antes de alertar
DIAS_VENTANA_DESFASE = 60   # Antigüedad máxima de las SO revisadas en DESFASE_FACTURACION
DIAS_VENTANA_ML = 30        # Antigüedad máxima de los envíos ML revisados en FANTASMA_OUT_ML
CHUNK_SIZE = 200

CSV_FILENAME = "Reporte_Inconsistencias.csv"
HTML_FILENAME = "email_body.html"

CONCEPTOS = ['RETRASO_OUT', 'DESFASE_FACTURACION', 'FANTASMA_OUT_ML', 'REQUIERE_NOTA_CREDITO']

ESTADOS_PICKING = {
    'draft': 'Borrador',
    'waiting': 'Esperando otra operación',
    'confirmed': 'Esperando disponibilidad',
    'assigned': 'Listo',
}

SO_FIELDS = ['id', 'name', 'date_order', 'team_id', 'channel_order_reference', 'warehouse_id', 'amount_total', 'delivery_status']


def call_odoo(models, db, uid, pwd, model, method, args, kwargs=None, max_retries=4, backoff_base=3):
    kwargs = kwargs or {}
    last_exc = None
    for intento in range(1, max_retries + 1):
        try:
            return models.execute_kw(db, uid, pwd, model, method, args, kwargs)
        except RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            espera = backoff_base * (2 ** (intento - 1))
            log.warning(
                f"[Intento {intento}/{max_retries}] Fallo Odoo -> "
                f"modelo='{model}'. Error: {e}. Reintentando en {espera}s..."
            )
            time.sleep(espera)
    log.error(f"❌ FALLO DEFINITIVO: {last_exc}")
    raise last_exc

def chunks(lista, size=CHUNK_SIZE):
    for i in range(0, len(lista), size):
        yield lista[i:i + size]

def clasificar_venta(warehouse_tuple):
    if not warehouse_tuple:
        return 'N/A', 'N/A'
    wh_name = warehouse_tuple[1]
    tipo = 'FULL' if 'fulfillment' in wh_name.lower() else 'DROP'
    return tipo, wh_name

def dias_desde(fecha_str, hoy):
    try:
        return (hoy - datetime.strptime(fecha_str, '%Y-%m-%d %H:%M:%S')).days
    except (TypeError, ValueError):
        return None

def obtener_pickings_out(models, db, uid, pwd, order_ids):
    """
    Devuelve {sale_id: {'fecha_done': str|None, 'abiertos': set(estados)}} con los
    OUT (picking_type_code = outgoing) de cada orden:
      - fecha_done: último OUT en estado 'done'.
      - abiertos: estados de los OUT que siguen pendientes (ni done ni cancel),
        p. ej. un backorder o un OUT esperando disponibilidad.
    """
    info = defaultdict(lambda: {'fecha_done': None, 'abiertos': set()})
    for chunk in chunks(list(order_ids)):
        domain = [
            ('sale_id', 'in', chunk),
            ('picking_type_code', '=', 'outgoing'),
            ('state', '!=', 'cancel'),
        ]
        pickings = call_odoo(models, db, uid, pwd, 'stock.picking', 'search_read', [domain], {'fields': ['sale_id', 'state', 'date_done']})
        for p in pickings:
            if not p.get('sale_id'):
                continue
            d = info[p['sale_id'][0]]
            if p['state'] == 'done':
                if not d['fecha_done'] or (p.get('date_done') or '') > d['fecha_done']:
                    d['fecha_done'] = p.get('date_done')
            else:
                d['abiertos'].add(p['state'])
    return info

def obtener_ordenes_con_devolucion(models, db, uid, pwd, order_ids):
    """
    Busca si la orden tiene un movimiento de retorno (RET) en estado 'done'.
    Devuelve un diccionario {sale_id: fecha_done}.
    """
    resultado = {}
    for chunk in chunks(list(order_ids)):
        domain = [
            ('sale_id', 'in', chunk),
            ('state', '=', 'done'),
            ('name', 'ilike', '%RET%')  # Validamos que el folio del movimiento contenga RET
        ]
        pickings = call_odoo(models, db, uid, pwd, 'stock.picking', 'search_read', [domain], {'fields': ['sale_id', 'date_done']})
        resultado.update({p['sale_id'][0]: p.get('date_done') for p in pickings if p.get('sale_id')})
    return resultado

def obtener_fechas_factura(models, db, uid, pwd, order_names):
    """
    Busca la fecha de factura (account.move) cuyo invoice_origin coincide
    con el nombre de la orden de venta. Devuelve {order_name: invoice_date}.
    """
    fechas = {}
    for chunk in chunks(list(order_names)):
        domain = [
            ('invoice_origin', 'in', chunk),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted')
        ]
        facturas = call_odoo(models, db, uid, pwd, 'account.move', 'search_read', [domain], {'fields': ['invoice_origin', 'invoice_date']})
        for f in facturas:
            origen = f.get('invoice_origin') or ''
            for nombre in [o.strip() for o in origen.split(',')]:
                if nombre:
                    fechas[nombre] = f.get('invoice_date')
    return fechas

def leer_ordenes(models, db, uid, pwd, order_ids):
    ordenes = []
    for chunk in chunks(list(order_ids)):
        ordenes += call_odoo(models, db, uid, pwd, 'sale.order', 'search_read', [[('id', 'in', chunk)]], {'fields': SO_FIELDS})
    return ordenes

def describir_out_abierto(estados):
    return ', '.join(sorted(ESTADOS_PICKING.get(e, e) for e in estados)) or 'N/A'


# ── Resumen HTML para el cuerpo del correo ─────────────────────────────

def _fmt_n(x):
    return f"{x:,.0f}" if x else "–"

def _fmt_mxn(x):
    return f"${x:,.0f}" if x else "–"

def _columnas_mes(reporte, max_meses=6):
    """Meses (YYYY-MM) presentes; si hay más de max_meses, los viejos se agrupan."""
    meses = sorted({r['Mes de orden'] for r in reporte if r['Mes de orden'] != 'N/A'})
    if len(meses) <= max_meses:
        return meses, set()
    viejos = set(meses[:len(meses) - max_meses + 1])
    return ['Anteriores'] + meses[-(max_meses - 1):], viejos

def _tabla_pivot(reporte, filas, valor, fmt, meses, viejos, titulo):
    """filas: lista de (tipo_venta, etiqueta_fila, funcion_filtro)."""
    th = 'style="border:1px solid #ccc;padding:4px 8px;background:#f2f2f2;text-align:center"'
    td = 'style="border:1px solid #ccc;padding:4px 8px;text-align:right"'
    tdl = 'style="border:1px solid #ccc;padding:4px 8px;text-align:left"'
    out = [f'<p><b>{titulo}</b></p>',
           '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px">',
           f'<tr><th {th}>Tipo venta</th><th {th}>Concepto</th>'
           + ''.join(f'<th {th}>{m}</th>' for m in meses) + f'<th {th}>Total</th></tr>']
    for tipo, etiqueta, filtro in filas:
        acum = defaultdict(float)
        for r in reporte:
            if r['Tipo_Venta'] == tipo and filtro(r):
                mes = 'Anteriores' if r['Mes de orden'] in viejos else r['Mes de orden']
                acum[mes] += valor(r)
        total = sum(acum.values())
        negrita = etiqueta.startswith('Órdenes únicas')
        estilo = ';font-weight:bold' if negrita else ''
        out.append(f'<tr style="{"background:#fafafa" if negrita else ""}">'
                   f'<td {tdl}>{tipo}</td><td {tdl[:-1]}{estilo}">{etiqueta}</td>'
                   + ''.join(f'<td {td[:-1]}{estilo}">{fmt(acum.get(m, 0))}</td>' for m in meses)
                   + f'<td {td[:-1]};font-weight:bold">{fmt(total)}</td></tr>')
    out.append('</table>')
    return '\n'.join(out)

def generar_resumen_html(reporte, hoy):
    if not reporte:
        return '<p>✅ No se detectaron inconsistencias el día de hoy.</p>'

    def monto(r):
        return r['Valor de la orden de venta [$]'] if isinstance(r['Valor de la orden de venta [$]'], (int, float)) else 0

    tipos = [t for t in ['FULL', 'DROP', 'N/A'] if any(r['Tipo_Venta'] == t for r in reporte)]
    filas = []
    for t in tipos:
        for c in CONCEPTOS:
            if any(r['Tipo_Venta'] == t and r[c] for r in reporte):
                filas.append((t, c, lambda r, c=c: r[c]))
        filas.append((t, 'Órdenes únicas', lambda r: True))

    meses, viejos = _columnas_mes(reporte)
    partes = [
        _tabla_pivot(reporte, filas, lambda r: 1, _fmt_n, meses, viejos,
                     'Tabla 1 · Cantidad de SOs por concepto y mes de la orden'),
        _tabla_pivot(reporte, filas, monto, _fmt_mxn, meses, viejos,
                     'Tabla 2 · Monto de las SOs (MXN, IVA incluido) por concepto y mes de la orden'),
    ]

    # Tabla 3: combinaciones (mutuamente excluyentes) para cuadrar contra el total de filas del CSV
    combos = defaultdict(lambda: [0, 0.0])
    for r in reporte:
        k = (r['Tipo_Venta'], r['Tipo_Alerta'])
        combos[k][0] += 1
        combos[k][1] += monto(r)
    th = 'style="border:1px solid #ccc;padding:4px 8px;background:#f2f2f2"'
    td = 'style="border:1px solid #ccc;padding:4px 8px;text-align:right"'
    tdl = 'style="border:1px solid #ccc;padding:4px 8px"'
    t3 = ['<p><b>Tabla 3 · Combinaciones de alertas (cada SO aparece una sola vez; suma = total del CSV)</b></p>',
          '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px">',
          f'<tr><th {th}>Tipo venta</th><th {th}>Tipo_Alerta (columna del CSV)</th><th {th}>SOs</th><th {th}>MXN</th></tr>']
    for (t, combo), (n, m) in sorted(combos.items(), key=lambda x: (tipos.index(x[0][0]), -x[1][0])):
        t3.append(f'<tr><td {tdl}>{t}</td><td {tdl}>{html.escape(combo)}</td><td {td}>{_fmt_n(n)}</td><td {td}>{_fmt_mxn(m)}</td></tr>')
    t3.append(f'<tr><td {tdl} colspan="2"><b>Total</b></td><td {td}><b>{_fmt_n(len(reporte))}</b></td>'
              f'<td {td}><b>{_fmt_mxn(sum(monto(r) for r in reporte))}</b></td></tr></table>')
    partes.append('\n'.join(t3))

    nota = ('<p style="font-size:12px;color:#555">Los conceptos <b>no son mutuamente excluyentes</b>: una misma SO puede estar, '
            'por ejemplo, en RETRASO_OUT y en DESFASE_FACTURACION a la vez (facturada y sin OUT después de '
            f'{DIAS_TOLERANCIA_OUT} días). Por eso en las Tablas 1 y 2 la suma de los conceptos puede ser mayor que '
            'la fila <i>Órdenes únicas</i>, que es la que debe usarse como total. '
            f'Corte: {hoy.strftime("%Y-%m-%d %H:%M")} UTC.</p>')
    return '\n'.join(partes) + nota

def generar_glosario_html():
    reglas = [
        ('RETRASO_OUT',
         f'SO confirmada (estado <i>Orden de venta</i>) hace más de {DIAS_TOLERANCIA_OUT} días cuyo estado de entrega en Odoo '
         'es <i>No entregado</i>, <i>Iniciado</i> (p. ej. PICK hecho pero OUT no) o <i>Parcialmente entregado</i>, y que además: '
         '(a) no tiene ningún OUT en estado <i>Hecho</i>, o (b) tiene un OUT hecho pero sigue un OUT abierto (backorder). '
         'Las SO con OUT hecho y sin OUT abierto se descartan: son las que quedaron "parciales" por una devolución.'),
        ('DESFASE_FACTURACION',
         f'SO de los últimos {DIAS_VENTANA_DESFASE} días con al menos una línea de producto donde la cantidad facturada '
         'es mayor a la entregada (se ignoran las líneas de envío C-ENVIO). Casos: '
         f'(a) <i>Facturada sin OUT</i> con más de {DIAS_TOLERANCIA_OUT} días (las más recientes no se alertan, porque '
         'marketplaces como Mercado Libre facturan al cobro, antes de despachar); '
         '(b) <i>Facturado &gt; pedido</i>: posible doble factura; '
         '(c) <i>OUT hecho pero facturado &gt; entregado</i>: revisar cantidades, kits o sustitución de producto.'),
        ('FANTASMA_OUT_ML',
         f'Envío de Mercado Libre de los últimos {DIAS_VENTANA_ML} días con estado <i>delivered</i> en ML, cuya SO en Odoo '
         'sigue sin entregar o parcial y sin ningún OUT hecho.'),
        ('REQUIERE_NOTA_CREDITO',
         'SO del universo de DESFASE_FACTURACION que tiene una devolución (RET) en estado <i>Hecho</i>: la mercancía regresó '
         'pero la factura sigue vigente, por lo que debe emitirse nota de crédito.'),
    ]
    th = 'style="border:1px solid #ccc;padding:4px 8px;background:#f2f2f2;text-align:left"'
    td = 'style="border:1px solid #ccc;padding:4px 8px;vertical-align:top"'
    filas = ''.join(f'<tr><td {td}><b>{c}</b></td><td {td}>{d}</td></tr>' for c, d in reglas)
    otras = ('<ul style="font-size:12px">'
             '<li><b>Tipo_Venta</b>: FULL si el almacén de la SO es un almacén <i>Fulfillment</i> (ML Full, Amazon FBA, Walmart WFS); DROP en cualquier otro caso.</li>'
             '<li><b>Valor</b>: <i>amount_total</i> de la SO completa (con IVA), no solo de las líneas en conflicto.</li>'
             '<li><b>Mes de orden</b>: mes de <i>date_order</i> (UTC).</li>'
             '<li><b>Estado OUT pendiente</b> (CSV): estado del OUT abierto. <i>Esperando disponibilidad</i> = Odoo no tiene stock '
             'en ese almacén para validarlo; <i>Listo</i> = tiene stock, falta validarlo.</li>'
             '<li>El CSV incluye una columna 1/0 por concepto para filtrar o hacer pivots sin depender de las combinaciones.</li>'
             '</ul>')
    return ('<p><b>Glosario · Reglas de cada concepto</b></p>'
            '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px;max-width:900px">'
            f'<tr><th {th}>Concepto</th><th {th}>Regla</th></tr>{filas}</table>{otras}')


def generar_reporte_alertas():
    odoo_url = os.getenv("odoo_urlV18")
    odoo_db = os.getenv("odoo_dbV18")
    odoo_user = os.getenv("odoo_user_dataV18")
    odoo_pwd = os.getenv("odoo_password_dataV18")

    common = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/common')
    uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
    models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')

    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error BD: {e}")
        db = None

    # Odoo guarda date_order en UTC
    hoy = datetime.utcnow()
    corte_tolerancia = (hoy - timedelta(days=DIAS_TOLERANCIA_OUT)).strftime('%Y-%m-%d %H:%M:%S')

    # ── Diccionario para consolidar por orden ──
    reporte_dict = {}

    def agregar_al_reporte(orden_data, tipo_alerta, detalle, fecha_factura=None, fecha_out=None, fecha_ret=None,
                           out_abiertos=None, cantidades=None):
        """Función auxiliar para agregar o actualizar una orden en el reporte"""
        nombre = orden_data['name']
        fecha_factura = fecha_factura or 'N/A'
        fecha_out = fecha_out or 'N/A'
        fecha_ret = fecha_ret or 'N/A'
        estado_out = describir_out_abierto(out_abiertos or set())
        if nombre in reporte_dict:
            fila = reporte_dict[nombre]
            # Si la orden ya existe, concatenamos la nueva alerta para no duplicar filas
            if not fila[tipo_alerta]:
                fila[tipo_alerta] = 1
                fila['Tipo_Alerta'] += f" + {tipo_alerta}"
                fila['Detalle'] += f" | {detalle}"
            # Completamos los datos si antes no aplicaban y ahora sí tenemos dato
            for col, val in [('Fecha de factura', fecha_factura), ('Fecha OUT', fecha_out),
                             ('Fecha RET', fecha_ret), ('Estado OUT pendiente', estado_out)]:
                if fila[col] == 'N/A' and val != 'N/A':
                    fila[col] = val
            if cantidades and fila['Cant. facturada'] == 'N/A':
                fila['Cant. pedida'], fila['Cant. facturada'], fila['Cant. entregada'] = cantidades
        else:
            tipo_venta, almacen_nombre = clasificar_venta(orden_data.get('warehouse_id'))
            fecha_orden = orden_data.get('date_order') or 'N/A'
            dias = dias_desde(fecha_orden, hoy)
            fila = {
                'Orden': nombre,
                'Referencia marketplace': orden_data.get('channel_order_reference') or 'N/A',
                'Canal': orden_data['team_id'][1] if orden_data.get('team_id') else 'N/A',
                'Tipo_Venta': tipo_venta,
                'Almacen': almacen_nombre,
                'Tipo_Alerta': tipo_alerta,
                'Detalle': detalle,
                'Valor de la orden de venta [$]': orden_data.get('amount_total', 'N/A'),
                'Fecha de orden': fecha_orden,
                'Mes de orden': fecha_orden[:7] if fecha_orden != 'N/A' else 'N/A',
                'Dias desde la orden': dias if dias is not None else 'N/A',
                'Estado entrega Odoo': orden_data.get('delivery_status') or 'N/A',
                'Estado OUT pendiente': estado_out,
                'Fecha de factura': fecha_factura,
                'Fecha OUT': fecha_out,
                'Fecha RET': fecha_ret,
                'Cant. pedida': 'N/A',
                'Cant. facturada': 'N/A',
                'Cant. entregada': 'N/A',
            }
            for c in CONCEPTOS:
                fila[c] = 1 if c == tipo_alerta else 0
            if cantidades:
                fila['Cant. pedida'], fila['Cant. facturada'], fila['Cant. entregada'] = cantidades
            reporte_dict[nombre] = fila

    # ── ALERTA 1: SO confirmada sin OUT ──────────────
    log.info(f"Buscando Alerta 1: Retrasos > {DIAS_TOLERANCIA_OUT} días...")
    # 'started' = algún picking hecho (p. ej. PICK en almacenes multi-paso) pero el OUT todavía no
    domain_retraso = [('state', '=', 'sale'), ('date_order', '<', corte_tolerancia),
                      ('delivery_status', 'in', ['pending', 'started', 'partial'])]
    retrasos = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', [domain_retraso], {'fields': SO_FIELDS})
    outs_retrasos = obtener_pickings_out(models, odoo_db, uid, odoo_pwd, [r['id'] for r in retrasos])
    fechas_factura_retrasos = obtener_fechas_factura(models, odoo_db, uid, odoo_pwd, [r['name'] for r in retrasos])

    for r in retrasos:
        out = outs_retrasos.get(r['id'], {'fecha_done': None, 'abiertos': set()})
        if out['fecha_done'] and not out['abiertos']:
            continue  # Ya surtida; queda parcial/pendiente por una devolución
        detalle = (f"OUT parcial el {out['fecha_done']}; backorder pendiente." if out['fecha_done']
                   else f"Confirmada el {r['date_order']} sin OUT.")
        agregar_al_reporte(
            r, 'RETRASO_OUT', detalle,
            fecha_factura=fechas_factura_retrasos.get(r['name']),
            fecha_out=out['fecha_done'],
            out_abiertos=out['abiertos']
        )

    # ── ALERTA 2: Desfase Facturación–Despacho ──────────────
    log.info("Buscando Alerta 2: Facturado vs Entregado...")
    domain_desfase = [
        ('state', 'in', ['sale', 'done']),
        ('qty_invoiced', '>', 0),
        ('order_id.date_order', '>=', (hoy - timedelta(days=DIAS_VENTANA_DESFASE)).strftime('%Y-%m-%d %H:%M:%S')),
    ]
    lineas = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', [domain_desfase],
                       {'fields': ['order_id', 'product_id', 'product_uom_qty', 'qty_invoiced', 'qty_delivered']})

    # {order_id: [pedida, facturada, entregada, sobrefacturada]} solo de las líneas en desfase
    desfase_por_orden = {}
    for l in lineas:
        prod_name = l['product_id'][1].upper() if l.get('product_id') else ""
        if l['qty_invoiced'] > l['qty_delivered'] and 'C-ENVIO' not in prod_name and l.get('order_id'):
            d = desfase_por_orden.setdefault(l['order_id'][0], [0.0, 0.0, 0.0, False])
            d[0] += l.get('product_uom_qty') or 0
            d[1] += l['qty_invoiced']
            d[2] += l['qty_delivered']
            d[3] = d[3] or l['qty_invoiced'] > (l.get('product_uom_qty') or 0)

    descartadas_tolerancia = 0
    if desfase_por_orden:
        ids = list(desfase_por_orden)
        orders_data = leer_ordenes(models, odoo_db, uid, odoo_pwd, ids)
        ordenes_con_devolucion = obtener_ordenes_con_devolucion(models, odoo_db, uid, odoo_pwd, ids)
        outs_desfase = obtener_pickings_out(models, odoo_db, uid, odoo_pwd, ids)
        fechas_factura_desfase = obtener_fechas_factura(models, odoo_db, uid, odoo_pwd, [o['name'] for o in orders_data])

        for o in orders_data:
            pedida, facturada, entregada, sobrefacturada = desfase_por_orden[o['id']]
            out = outs_desfase.get(o['id'], {'fecha_done': None, 'abiertos': set()})
            dias = dias_desde(o.get('date_order'), hoy)

            if o['id'] in ordenes_con_devolucion:
                tipo_alerta = 'REQUIERE_NOTA_CREDITO'
                detalle_texto = "RET confirmado pero factura vigente."
            elif sobrefacturada:
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = "Facturado > pedido (posible doble factura)."
            elif not out['fecha_done']:
                if dias is not None and dias <= DIAS_TOLERANCIA_OUT:
                    descartadas_tolerancia += 1
                    continue  # Facturada al cobro, aún dentro de la ventana normal de despacho
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = f"Facturada sin OUT ({dias} días)."
            else:
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = "OUT hecho pero facturado > entregado."

            agregar_al_reporte(
                o, tipo_alerta, detalle_texto,
                fecha_factura=fechas_factura_desfase.get(o['name']),
                fecha_out=out['fecha_done'],
                fecha_ret=ordenes_con_devolucion.get(o['id']),
                out_abiertos=out['abiertos'],
                cantidades=(pedida, facturada, entregada)
            )
    log.info(f"Alerta 2: {descartadas_tolerancia} órdenes facturadas sin OUT descartadas por estar dentro de {DIAS_TOLERANCIA_OUT} días.")

    # ── ALERTA 3: Entrega sin OUT (Cruce ML Shipping vs Odoo) ────────────
    log.info("Buscando Alerta 3: Entregado en ML sin OUT en Odoo...")
    if db:
        cursor.execute(f"""
            SELECT order_id FROM somos_reyes.ml_shipping
            WHERE status = 'delivered' AND date_created >= UTC_TIMESTAMP() - INTERVAL {DIAS_VENTANA_ML} DAY
        """)
        ml_delivered = [str(row['order_id']) for row in cursor.fetchall()]

        for chunk in chunks(ml_delivered):
            domain_odoo = [('channel_order_reference', 'in', chunk), ('delivery_status', 'in', ['pending', 'started', 'partial'])]
            odoo_pendientes = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', [domain_odoo], {'fields': SO_FIELDS})

            pendientes_ids = [op['id'] for op in odoo_pendientes]
            outs_ml = obtener_pickings_out(models, odoo_db, uid, odoo_pwd, pendientes_ids)
            ordenes_con_devolucion_ml = obtener_ordenes_con_devolucion(models, odoo_db, uid, odoo_pwd, pendientes_ids)
            fechas_factura_ml = obtener_fechas_factura(models, odoo_db, uid, odoo_pwd, [op['name'] for op in odoo_pendientes])

            for op in odoo_pendientes:
                out = outs_ml.get(op['id'], {'fecha_done': None, 'abiertos': set()})
                if out['fecha_done']:
                    continue
                agregar_al_reporte(
                    op, 'FANTASMA_OUT_ML', "ML entregado, Odoo sin OUT.",
                    fecha_factura=fechas_factura_ml.get(op['name']),
                    fecha_ret=ordenes_con_devolucion_ml.get(op['id']),
                    out_abiertos=out['abiertos']
                )
        cursor.close()
        db.close()

    # ── Generar CSV ────────────
    columnas = ['Orden', 'Referencia marketplace', 'Canal', 'Tipo_Venta', 'Almacen', 'Tipo_Alerta', 'Detalle',
                'Valor de la orden de venta [$]', 'Fecha de orden', 'Mes de orden', 'Dias desde la orden',
                'Estado entrega Odoo', 'Estado OUT pendiente', 'Fecha de factura', 'Fecha OUT', 'Fecha RET',
                'Cant. pedida', 'Cant. facturada', 'Cant. entregada'] + CONCEPTOS

    reporte_final = sorted(reporte_dict.values(), key=lambda r: r['Fecha de orden'], reverse=True)

    with open(CSV_FILENAME, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=columnas)
        writer.writeheader()
        writer.writerows(reporte_final)

    # ── Generar cuerpo HTML (resumen + glosario) ────────────
    with open(HTML_FILENAME, 'w', encoding='utf-8') as f:
        f.write(generar_resumen_html(reporte_final, hoy))
        f.write(generar_glosario_html())

    if reporte_final:
        log.info(f"✅ Reporte generado con {len(reporte_final)} órdenes únicas: {CSV_FILENAME}")
    else:
        log.info("✅ Todo en orden. Cero inconsistencias hoy.")

    return CSV_FILENAME


if __name__ == "__main__":
    generar_reporte_alertas()
