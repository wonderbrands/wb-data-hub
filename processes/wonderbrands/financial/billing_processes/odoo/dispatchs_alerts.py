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
    Devuelve {sale_id: {'fecha_done': str|None, 'abiertos': set(estados), 'previos_abiertos': bool}}:
      - fecha_done: último OUT (picking_type_code = outgoing) en estado 'done'.
      - abiertos: estados de los OUT que siguen pendientes (ni done ni cancel),
        p. ej. un backorder o un OUT esperando disponibilidad.
      - previos_abiertos: la SO tiene un PICK/PACK (internal) pendiente; en almacenes
        multi-paso el OUT puede no existir todavía mientras el PICK no se valide.
    """
    info = defaultdict(lambda: {'fecha_done': None, 'abiertos': set(), 'previos_abiertos': False})
    for chunk in chunks(list(order_ids)):
        domain = [
            ('sale_id', 'in', chunk),
            ('picking_type_code', 'in', ['outgoing', 'internal']),
            ('state', '!=', 'cancel'),
        ]
        pickings = call_odoo(models, db, uid, pwd, 'stock.picking', 'search_read', [domain],
                             {'fields': ['sale_id', 'state', 'date_done', 'picking_type_code']})
        for p in pickings:
            if not p.get('sale_id'):
                continue
            d = info[p['sale_id'][0]]
            if p['picking_type_code'] == 'internal':
                d['previos_abiertos'] = d['previos_abiertos'] or p['state'] != 'done'
            elif p['state'] == 'done':
                if not d['fecha_done'] or (p.get('date_done') or '') > d['fecha_done']:
                    d['fecha_done'] = p.get('date_done')
            else:
                d['abiertos'].add(p['state'])
    return info

def obtener_ordenes_con_devolucion(models, db, uid, pwd, order_ids):
    """
    Busca si la orden tiene una devolución en estado 'done'.
    Una devolución es cualquier picking de entrada (incoming) ligado a la SO, o
    cuyo folio contenga RET o DEV (según el almacén, las devoluciones de un OUT
    se nombran .../RET/... o .../DEV/...).
    Devuelve un diccionario {sale_id: fecha_done}.
    """
    resultado = {}
    for chunk in chunks(list(order_ids)):
        domain = [
            ('sale_id', 'in', chunk),
            ('state', '=', 'done'),
            '|', '|',
            ('picking_type_code', '=', 'incoming'),
            ('name', 'ilike', 'RET'),
            ('name', 'ilike', 'DEV'),
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

def motivo_out(out):
    """Motivo por el que una SO sigue sin OUT, según el estado de sus OUT abiertos."""
    if out['fecha_done'] and out['abiertos']:
        return 'Backorder pendiente (OUT parcial)'
    if 'confirmed' in out['abiertos']:
        return 'OUT esperando disponibilidad (sin stock en Odoo)'
    if 'waiting' in out['abiertos']:
        return 'OUT esperando otra operación (PICK/PACK)'
    if 'assigned' in out['abiertos']:
        return 'OUT listo, sin validar'
    if 'draft' in out['abiertos']:
        return 'OUT en borrador'
    if out.get('previos_abiertos'):
        return 'Detenida en PICK/PACK (OUT aún no generado)'
    return 'Sin OUT activo (cancelado o nunca generado)'


# ── Resumen HTML para el cuerpo del correo ─────────────────────────────

_TH = 'style="border:1px solid #ccc;padding:4px 8px;background:#f2f2f2;text-align:center"'
_TABLA = '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:12px">'
_ESTILO_FILA = {'normal': '', 'subtotal': 'background:#f7f7f7;font-weight:bold', 'total': 'background:#e8eef7;font-weight:bold'}

def _fmt_n(x):
    return f"{x:,.0f}" if x else "–"

def _fmt_mxn(x):
    return f"${x:,.0f}" if x else "–"

def _monto(r):
    v = r['Valor de la orden de venta [$]']
    return v if isinstance(v, (int, float)) else 0

def _td(contenido, alinear='right', extra=''):
    return f'<td style="border:1px solid #ccc;padding:4px 8px;text-align:{alinear};{extra}">{contenido}</td>'

def _columnas_mes(reporte, max_meses=6):
    """Meses (YYYY-MM) presentes; si hay más de max_meses, los viejos se agrupan en 'Anteriores'."""
    meses = sorted({r['Mes de orden'] for r in reporte if r['Mes de orden'] != 'N/A'})
    if len(meses) <= max_meses:
        return meses, set()
    viejos = set(meses[:len(meses) - max_meses + 1])
    return ['Anteriores'] + meses[-(max_meses - 1):], viejos

def _ventanas(hoy):
    """Fecha desde la que revisa cada concepto; None = sin límite hacia atrás."""
    return {
        'RETRASO_OUT': None,
        'DESFASE_FACTURACION': hoy - timedelta(days=DIAS_VENTANA_DESFASE),
        'REQUIERE_NOTA_CREDITO': hoy - timedelta(days=DIAS_VENTANA_DESFASE),
        'FANTASMA_OUT_ML': hoy - timedelta(days=DIAS_VENTANA_ML),
    }

def _tabla(titulo, subtitulo, encabezados, filas, meses, viejos, valor, fmt, extras=(), fuera_de_ventana=None):
    """
    Tabla pivote por mes de la orden.
      filas: lista de dicts {'izq': [celdas], 'rows': [registros], 'tipo': normal|subtotal|total, 'conceptos': [...]}
      valor/fmt: cómo se agrega y formatea cada celda de mes.
      extras: columnas adicionales al final [(encabezado, fn(rows) -> str)].
      fuera_de_ventana(mes, conceptos) -> True si algún concepto de la fila no revisa ese mes (celda 'n/r').
    """
    out = [f'<p style="margin:18px 0 2px"><b>{titulo}</b></p>']
    if subtitulo:
        out.append(f'<p style="margin:0 0 6px;font-size:12px;color:#555">{subtitulo}</p>')
    out.append(_TABLA)
    out.append('<tr>' + ''.join(f'<th {_TH}>{h}</th>' for h in encabezados)
               + ''.join(f'<th {_TH}>{m}</th>' for m in meses)
               + f'<th {_TH}>Total</th>' + ''.join(f'<th {_TH}>{h}</th>' for h, _ in extras) + '</tr>')
    for f in filas:
        estilo = _ESTILO_FILA[f.get('tipo', 'normal')]
        acum = defaultdict(float)
        for r in f['rows']:
            acum['Anteriores' if r['Mes de orden'] in viejos else r['Mes de orden']] += valor(r)
        celdas = [_td(html.escape(str(c)), 'left', estilo) for c in f['izq']]
        for m in meses:
            if fuera_de_ventana and not acum.get(m) and fuera_de_ventana(m, f.get('conceptos', [])):
                celdas.append(_td('n/r', 'center', 'background:#eeeeee;color:#999'))
            else:
                celdas.append(_td(fmt(acum.get(m, 0)), 'right', estilo))
        celdas.append(_td(fmt(sum(acum.values())), 'right', estilo + ';font-weight:bold'))
        celdas += [_td(fn(f['rows']), 'right', estilo) for _, fn in extras]
        out.append('<tr>' + ''.join(celdas) + '</tr>')
    out.append('</table>')
    return '\n'.join(out)

def _filas_por_grupo(reporte, tipos, clave, etiqueta_subtotal, conceptos_de=None, orden=None):
    """Filas excluyentes: cada registro cae en un solo grupo; subtotal por tipo y total general."""
    filas = []
    for t in tipos:
        del_tipo = [r for r in reporte if r['Tipo_Venta'] == t]
        grupos = defaultdict(list)
        for r in del_tipo:
            grupos[clave(r)].append(r)
        for k in sorted(grupos, key=orden or (lambda k: -len(grupos[k]))):
            filas.append({'izq': [t, k], 'rows': grupos[k], 'conceptos': conceptos_de(k) if conceptos_de else []})
        filas.append({'izq': [t, etiqueta_subtotal], 'rows': del_tipo, 'tipo': 'subtotal'})
    filas.append({'izq': ['TOTAL', 'Total general'], 'rows': reporte, 'tipo': 'total'})
    return filas

def generar_como_leer_html(hoy):
    v = _ventanas(hoy)
    f_des = v['DESFASE_FACTURACION'].strftime('%Y-%m-%d')
    f_ml = v['FANTASMA_OUT_ML'].strftime('%Y-%m-%d')
    caja = 'style="border:1px solid #c9d6ea;background:#f4f8fd;padding:10px 14px;font-size:12px;max-width:900px"'
    return f"""
<div {caja}>
<p style="margin:0 0 6px"><b>Cómo leer este reporte</b></p>
<ul style="margin:0 0 8px;padding-left:18px">
<li>Cada SO aparece <b>una sola vez</b> en el CSV. Su columna <i>Tipo_Alerta</i> es la <b>combinación</b> de todos los problemas
que se le detectaron (p. ej. <i>RETRASO_OUT + DESFASE_FACTURACION</i>). Las combinaciones <b>sí son excluyentes</b>:
una SO "RETRASO_OUT + DESFASE_FACTURACION" no aparece en "RETRASO_OUT" solo ni en "DESFASE_FACTURACION" solo.</li>
<li><b>Tablas 1, 2 y 5</b> (por combinación y por canal): cada SO y su monto cuentan <b>una vez</b>. Las filas se suman y el subtotal es el total real.</li>
<li><b>Tablas 3 y 4</b> (por concepto y motivo): responden "¿cuántas SOs tienen este problema?". Una SO con dos problemas
aparece en las dos filas, cada vez con su monto completo. <b>No sumar esas filas</b>; el total es la fila "Órdenes sin duplicar".</li>
</ul>
<p style="margin:0 0 4px"><b>Periodo que revisa cada concepto</b> (corte {hoy.strftime('%Y-%m-%d %H:%M')} UTC):</p>
<ul style="margin:0;padding-left:18px">
<li><b>RETRASO_OUT</b>: sin límite hacia atrás (toda SO confirmada con más de {DIAS_TOLERANCIA_OUT} días). Es el único concepto que llega a meses antiguos.</li>
<li><b>DESFASE_FACTURACION</b> y <b>REQUIERE_NOTA_CREDITO</b>: solo SOs de los últimos {DIAS_VENTANA_DESFASE} días (desde el {f_des}).</li>
<li><b>FANTASMA_OUT_ML</b>: solo envíos de Mercado Libre de los últimos {DIAS_VENTANA_ML} días (desde el {f_ml}).</li>
<li>Celdas <span style="background:#eeeeee;color:#999;padding:0 4px">n/r</span> = <b>no revisado</b> en ese mes por la ventana del concepto;
no significa cero casos. Por eso una SO de un mes antiguo solo puede aparecer como RETRASO_OUT.</li>
</ul>
</div>"""

def generar_resumen_html(reporte, hoy):
    if not reporte:
        return '<p>✅ No se detectaron inconsistencias el día de hoy.</p>'

    tipos = [t for t in ['FULL', 'DROP', 'N/A'] if any(r['Tipo_Venta'] == t for r in reporte)]
    meses, viejos = _columnas_mes(reporte)
    ventanas = _ventanas(hoy)

    def fuera_de_ventana(mes, conceptos):
        if mes == 'Anteriores':
            return any(ventanas.get(c) for c in conceptos)
        return any(ventanas.get(c) and mes < ventanas[c].strftime('%Y-%m') for c in conceptos)

    def conceptos_de_combo(combo):
        return [c.strip() for c in combo.split('+')]

    def orden_combo(combo):
        cs = conceptos_de_combo(combo)
        return (len(cs), [CONCEPTOS.index(c) for c in cs if c in CONCEPTOS])

    mxn = lambda rows: _fmt_mxn(sum(_monto(r) for r in rows))

    # Tablas 1 y 2: combinaciones (excluyentes), por mes
    filas_combo = _filas_por_grupo(reporte, tipos, lambda r: r['Tipo_Alerta'], 'Subtotal',
                                   conceptos_de=conceptos_de_combo, orden=orden_combo)
    partes = [
        _tabla('Tabla 1 · SOs por combinación de alertas y mes de la orden',
               'Excluyente: cada SO cuenta una vez. Las filas se suman; el subtotal es el total real.',
               ['Tipo venta', 'Combinación (Tipo_Alerta)'], filas_combo, meses, viejos,
               lambda r: 1, _fmt_n, fuera_de_ventana=fuera_de_ventana),
        _tabla('Tabla 2 · Monto (MXN, IVA incluido) por combinación de alertas y mes de la orden',
               'Mismas SOs de la Tabla 1, medidas en monto. Las filas se suman.',
               ['Tipo venta', 'Combinación (Tipo_Alerta)'], filas_combo, meses, viejos,
               _monto, _fmt_mxn, fuera_de_ventana=fuera_de_ventana),
    ]

    # Tabla 3: por concepto (no excluyente), separando "solo este concepto" vs "con otro concepto"
    es_solo = lambda r: sum(r[c] for c in CONCEPTOS) == 1
    filas_concepto = []
    for t in tipos:
        del_tipo = [r for r in reporte if r['Tipo_Venta'] == t]
        for c in CONCEPTOS:
            rows = [r for r in del_tipo if r[c]]
            if rows:
                filas_concepto.append({'izq': [t, c], 'rows': rows, 'conceptos': [c]})
        filas_concepto.append({'izq': [t, 'Órdenes sin duplicar'], 'rows': del_tipo, 'tipo': 'subtotal'})
    filas_concepto.append({'izq': ['TOTAL', 'Total general (sin duplicar)'], 'rows': reporte, 'tipo': 'total'})
    partes.append(_tabla(
        'Tabla 3 · SOs por concepto y mes de la orden (cuántas SOs tienen cada problema)',
        'No excluyente: una SO con dos problemas aparece en las dos filas. <b>No sumar las filas de concepto</b>; '
        'el total es "Órdenes sin duplicar". "Solo este concepto" + "Con otro concepto" = Total de la fila.',
        ['Tipo venta', 'Concepto'], filas_concepto, meses, viejos, lambda r: 1, _fmt_n,
        extras=[('MXN', mxn),
                ('Solo este concepto', lambda rows: _fmt_n(sum(1 for r in rows if es_solo(r)))),
                ('Con otro concepto', lambda rows: _fmt_n(sum(1 for r in rows if not es_solo(r))))],
        fuera_de_ventana=fuera_de_ventana))

    # Tabla 4: motivo dentro de cada concepto (excluyente dentro del concepto)
    filas_motivo = []
    for t in tipos:
        del_tipo = [r for r in reporte if r['Tipo_Venta'] == t]
        for c in CONCEPTOS:
            grupos = defaultdict(list)
            for r in del_tipo:
                if r[c]:
                    grupos[r['_motivos'].get(c, 'N/A')].append(r)
            for motivo in sorted(grupos, key=lambda k: -len(grupos[k])):
                filas_motivo.append({'izq': [t, c, motivo], 'rows': grupos[motivo], 'conceptos': [c]})
    partes.append(_tabla(
        'Tabla 4 · Motivo detectado dentro de cada concepto, por mes de la orden',
        'Dentro de un concepto los motivos son excluyentes y suman la fila de ese concepto en la Tabla 3. '
        'Entre conceptos distintos la misma SO puede repetirse.',
        ['Tipo venta', 'Concepto', 'Motivo'], filas_motivo, meses, viejos, lambda r: 1, _fmt_n,
        extras=[('MXN', mxn)], fuera_de_ventana=fuera_de_ventana))

    # Tabla 5: por canal (excluyente)
    partes.append(_tabla(
        'Tabla 5 · SOs por canal y mes de la orden',
        'Excluyente: cada SO pertenece a un solo canal. Las filas se suman.',
        ['Tipo venta', 'Canal'], _filas_por_grupo(reporte, tipos, lambda r: r['Canal'], 'Subtotal'),
        meses, viejos, lambda r: 1, _fmt_n, extras=[('MXN', mxn)]))

    return generar_como_leer_html(hoy) + '\n'.join(partes)

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
         'SO del universo de DESFASE_FACTURACION (facturado &gt; entregado, últimos 60 días) que tiene una devolución en estado '
         '<i>Hecho</i>: un picking de entrada ligado a la SO o con folio RET o DEV. La mercancía regresó pero la factura sigue '
         'vigente, por lo que debe emitirse nota de crédito. Tiene prioridad sobre DESFASE_FACTURACION (la SO va en uno u otro).'),
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
             '<li><b>Motivo</b> (CSV y Tabla 4): causa detectada dentro de cada concepto. <i>Detenida en PICK/PACK</i> = '
             'el OUT no existe aún porque el paso previo sigue abierto; <i>Sin OUT activo</i> = no hay OUT ni PICK/PACK abiertos '
             '(p. ej. OUT cancelado).</li>'
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
                           out_abiertos=None, cantidades=None, motivo='N/A'):
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
                fila['_motivos'][tipo_alerta] = motivo
                fila['Motivo'] += f" | {tipo_alerta}: {motivo}"
            # Completamos los datos si antes no aplicaban y ahora sí tenemos dato
            for col, val in [('Fecha de factura', fecha_factura), ('Fecha OUT', fecha_out),
                             ('Fecha devolución', fecha_ret), ('Estado OUT pendiente', estado_out)]:
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
                'Motivo': f"{tipo_alerta}: {motivo}",
                '_motivos': {tipo_alerta: motivo},
                'Valor de la orden de venta [$]': orden_data.get('amount_total', 'N/A'),
                'Fecha de orden': fecha_orden,
                'Mes de orden': fecha_orden[:7] if fecha_orden != 'N/A' else 'N/A',
                'Dias desde la orden': dias if dias is not None else 'N/A',
                'Estado entrega Odoo': orden_data.get('delivery_status') or 'N/A',
                'Estado OUT pendiente': estado_out,
                'Fecha de factura': fecha_factura,
                'Fecha OUT': fecha_out,
                'Fecha devolución': fecha_ret,
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
            out_abiertos=out['abiertos'],
            motivo=motivo_out(out)
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
                detalle_texto = "Devolución (RET/DEV) hecha pero factura vigente."
                motivo = 'Devolución hecha, factura vigente'
            elif sobrefacturada:
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = "Facturado > pedido (posible doble factura)."
                motivo = 'Facturado > pedido (posible doble factura)'
            elif not out['fecha_done']:
                if dias is not None and dias <= DIAS_TOLERANCIA_OUT:
                    descartadas_tolerancia += 1
                    continue  # Facturada al cobro, aún dentro de la ventana normal de despacho
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = f"Facturada sin OUT ({dias} días)."
                motivo = f'Facturada sin OUT (> {DIAS_TOLERANCIA_OUT} días)'
            else:
                tipo_alerta = 'DESFASE_FACTURACION'
                detalle_texto = "OUT hecho pero facturado > entregado."
                motivo = 'OUT hecho, facturado > entregado'

            agregar_al_reporte(
                o, tipo_alerta, detalle_texto,
                fecha_factura=fechas_factura_desfase.get(o['name']),
                fecha_out=out['fecha_done'],
                fecha_ret=ordenes_con_devolucion.get(o['id']),
                out_abiertos=out['abiertos'],
                cantidades=(pedida, facturada, entregada),
                motivo=motivo
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
                    out_abiertos=out['abiertos'],
                    motivo=motivo_out(out)
                )
        cursor.close()
        db.close()

    # ── Generar CSV ────────────
    columnas = ['Orden', 'Referencia marketplace', 'Canal', 'Tipo_Venta', 'Almacen', 'Tipo_Alerta', 'Motivo', 'Detalle',
                'Valor de la orden de venta [$]', 'Fecha de orden', 'Mes de orden', 'Dias desde la orden',
                'Estado entrega Odoo', 'Estado OUT pendiente', 'Fecha de factura', 'Fecha OUT', 'Fecha devolución',
                'Cant. pedida', 'Cant. facturada', 'Cant. entregada'] + CONCEPTOS

    reporte_final = sorted(reporte_dict.values(), key=lambda r: r['Fecha de orden'], reverse=True)

    with open(CSV_FILENAME, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=columnas, extrasaction='ignore')
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
