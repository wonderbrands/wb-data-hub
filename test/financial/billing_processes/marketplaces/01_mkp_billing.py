"""
01_mkp_billing.py
-----------------
FACTURACION DE MARKETPLACES (TikTok, Coppel) -- MINER + LOADER

Un solo script para los marketplaces en los que NOSOTROS emitimos el CFDI a
publico general (ninguno tiene autofacturador). Kestra inyecta MKP=TIKTOK o
MKP=COPPEL; lo que cambia entre ellos vive en mkp_sources.py.

Reemplaza, para estos dos equipos, a facturacion_automatica_1a1.py. Al pasar
a produccion hay que sacar Team_TikTok y Team_Coppel del 1a1 EN EL MISMO
RELEASE (como con Amazon el 1-oct).


===============================================================================
QUE HACE
===============================================================================
  MINER   Lee las ventas del marketplace (mkp_sources), les cruza la SO por la
          replica de Odoo en MySQL (odoo_new_sale_order_live, al dia) y las
          siembra en finance.mkp_billing_{entorno}, una fila por orden.
  LOADER  Por cada orden pendiente, contra Odoo:
            1. SO por nombre. Sin SO -> NO_SO_IN_ODOO (solo alerta: la crea Yuju).
            2. La SO tiene que ser del equipo del marketplace.
            3. Re-validacion FAIL-CLOSED: si ya hay factura viva (de este
               script, del 1a1 o global) -> ALREADY_ODOO_INVOICED. Se revisa
               que este en la CxC del marketplace (CXC_INCORRECTA en la nota).
            4. Reembolsada al 100% antes de facturar -> no se factura.
            5. Lista para facturar: con salida de almacen (OUT) o pasados
               grace_days desde la venta. Si no, sigue pendiente.
            6. SO en borrador -> se confirma. SO cancelada -> se factura igual
               (la venta se cobro; la NC sale por el flujo de devoluciones).
            7. Total de la SO contra lo que pago el cliente (+-1 peso). Si no
               cuadra -> TOTAL_DIFF, no se factura.
            8. Factura a publico general, CxC del marketplace, publicada (el
               costo va a la 205.06.003 y se concilia con la salida). Timbra
               solo en produccion.


===============================================================================
REGLAS (Playbook Soporte de Ventas v2.6 + propuesta del 24-nov-2026)
===============================================================================
  * Toda venta cobrada se factura, haya salida o no (sin restriccion de OUT).
  * La venta se factura en su mes: los primeros MKP_DIAS_CIERRE_MES dias del
    mes, la venta del mes anterior sale con fecha del ultimo dia, sin gracia.
  * Muestras ($0) no se facturan: no son venta.
  * La promocion que paga TikTok (platform_discount) NO va en la factura: la
    factura es por lo que pago el cliente (= la SO). Como en Amazon, la
    promocion se registra al aplicar el cobro (403.01.004 + 208.01.001).
  * Envio: si el cliente pago envio y la SO no lo trae, la orden sale en
    TOTAL_DIFF (pendiente de confirmar con Finanzas si va en el CFDI).


===============================================================================
BLINDAJES (incidente del 26-jul-2026)
===============================================================================
  1. concurrency limit 1 en Kestra (vive en el YAML).
  2. Candado en BD: el paso a PROCESSING es un UPDATE condicionado.
  3. ref = 'AUTOINV:<SO>', el mismo prefijo del 1a1 y de Amazon.
  4. Re-validacion FAIL-CLOSED antes del create.
  5. Escrituras sin reintento ciego (OdooWriteUncertain -> se busca por ref).


===============================================================================
ESTADOS EN finance.mkp_billing_{staging|prod}
===============================================================================
  PENDING                  por facturar (incluye las que esperan OUT o gracia)
  PROCESSING               tomada por una corrida (candado)
  ODOO_INVOICED            factura creada por este script (raw_json.facturas_odoo)
  ALREADY_ODOO_INVOICED    ya tenia factura viva (1a1, global u otra corrida)
  REEMBOLSADA_SIN_FACTURA  el cliente recibio el 100% antes de facturar: no
                           hay venta que soportar, ni factura ni NC
  MUESTRA                  venta de $0
  NO_VENTA                 el marketplace no la cobro (UNPAID, CANCELLED,
                           sin debited_date). Se reevalua.
  NO_SO_IN_ODOO            sin SO. Solo alerta. Se reevalua.
  SO_MULTIPLE              la orden tiene mas de una SO vigente. Se reevalua.
  EQUIPO_DISTINTO          la SO es de otro equipo de ventas. Se reevalua.
  SO_DRAFT                 SO en borrador que Odoo no dejo confirmar. Se reevalua.
  TOTAL_DIFF               la SO no cuadra con lo que pago el cliente. Se reevalua.
  ERROR                    fallo recuperable (hasta 3 intentos)

Uso:
  MKP=TIKTOK MKP_ENTORNO=staging python 01_mkp_billing.py
  Variables: MKP_START_DATE, MKP_END_DATE (AAAA-MM-DD, fin exclusivo),
  MKP_ORDER_IDS (lista separada por comas: alcance de pruebas),
  MKP_MAX_FACTURAS (tope de facturas por corrida; staging 10 por defecto).
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
import xmlrpc.client

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _mkp_common import (
    ENTORNO, TBL_BILLING, MARKETPLACE, FUENTE,
    TAX_ID_MARKETPLACES, PARTNER_ID_PUBLICO_GENERAL, ACCOUNT_MERC_NO_ENVIADA,
    IDEMPOTENCY_PREFIX, KESTRA_EXECUTION_ID, UTC_local,
    validar_entorno, banner, notify_slack, get_db_connection, ahora_cdmx,
    OdooModelProxy, OdooWriteUncertain, get_account_id, a_float,
    ejecutar_sin_retorno,
)


# =============================================================================
# CONFIGURACION
# =============================================================================
RUN_MINER = os.getenv('MKP_RUN_MINER', '1') == '1'
RUN_LOADER = os.getenv('MKP_RUN_LOADER', '1') == '1'

# El dia de la corrida se fija una sola vez (incidente del 3-nov-2026 en
# Amazon: una corrida que pasa la medianoche no puede cambiar de fecha).
HOY_CORRIDA = ahora_cdmx()


def inicio_de_mes(hoy=None):
    return (hoy or HOY_CORRIDA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# Ventana. Produccion: desde el dia 1 del mes anterior hasta hoy. Staging: la
# base de Odoo de pruebas es una copia congelada; su ultima SO es del 13-nov.
_DEFECTO_INICIO = '' if ENTORNO == 'prod' else '2026-11-01'
_DEFECTO_FIN = '' if ENTORNO == 'prod' else '2026-11-14'
START_DATE = os.getenv('MKP_START_DATE', _DEFECTO_INICIO).strip() \
    or f"{inicio_de_mes() - timedelta(days=1):%Y-%m}-01"
END_DATE = os.getenv('MKP_END_DATE', _DEFECTO_FIN).strip() \
    or f"{HOY_CORRIDA + timedelta(days=1):%Y-%m-%d}"

ORDER_IDS = [o.strip() for o in os.getenv('MKP_ORDER_IDS', '').split(',') if o.strip()]

_DEFECTO_MAX = '0' if ENTORNO == 'prod' else '10'
MAX_FACTURAS = int(os.getenv('MKP_MAX_FACTURAS', _DEFECTO_MAX) or 0) or None

DIAS_CIERRE_MES = int(os.getenv('MKP_DIAS_CIERRE_MES', '1'))
if not 0 <= DIAS_CIERRE_MES <= 3:
    raise Exception("MKP_DIAS_CIERRE_MES debe estar entre 0 y 3 (72 h del SAT).")

GRACE_DAYS = int(os.getenv('MKP_GRACE_DAYS', str(FUENTE['grace_days'])))
TOLERANCIA_TOTAL = float(os.getenv('MKP_TOLERANCIA_TOTAL', '1.00'))
DO_STAMPING = (ENTORNO == 'prod')
STUCK_PROCESSING_HORAS = 6

# Estados que el miner puede refrescar con los datos nuevos del marketplace,
# y los que ademas regresa a PENDING (todos menos ERROR, que tiene su propio
# conteo de reintentos).
REFRESCABLES = ('PENDING', 'NO_VENTA', 'NO_SO_IN_ODOO', 'SO_MULTIPLE',
                'EQUIPO_DISTINTO', 'SO_DRAFT', 'TOTAL_DIFF', 'ERROR')
REABRIBLES = tuple(s for s in REFRESCABLES if s != 'ERROR')


# =============================================================================
# CIERRE DE MES (mismas reglas que 01_amz_billing.py)
# =============================================================================
def en_cierre_de_mes(hoy=None):
    return (hoy or HOY_CORRIDA).day <= DIAS_CIERRE_MES


def fecha_cdmx(valor_utc):
    """date_order de Odoo (UTC, texto) -> datetime en CDMX."""
    return datetime.strptime(str(valor_utc)[:19], '%Y-%m-%d %H:%M:%S') + timedelta(hours=UTC_local)


def venta_de_mes_anterior(so, hoy=None):
    if not so.get('date_order'):
        return False
    return fecha_cdmx(so['date_order']) < inicio_de_mes(hoy)


def fecha_factura(so, hoy=None):
    """invoice_date. None = la de la corrida (Odoo la pone al publicar)."""
    if not en_cierre_de_mes(hoy) or not venta_de_mes_anterior(so, hoy):
        return None
    return (inicio_de_mes(hoy) - timedelta(days=1)).strftime('%Y-%m-%d')


def limite_72h(hoy=None):
    """Ultimo momento para timbrar con fecha del ultimo dia del mes anterior."""
    return inicio_de_mes(hoy) + timedelta(days=3) - timedelta(minutes=20)


# =============================================================================
# FASE A -- MINER
# =============================================================================
def buscar_so_replica(cur, ordenes):
    """{order_id: [SO]} desde odoo_new_sale_order_live: por el numero de orden
    (channel_order_reference, indexado) y por la SO que trae el marketplace."""
    campos = "name, state, invoice_status, team_name, channel_order_reference"
    filas, ids = [], list(ordenes)
    for i in range(0, len(ids), 1000):
        ch = ids[i:i + 1000]
        cur.execute(f"SELECT {campos} FROM somos_reyes.odoo_new_sale_order_live "
                    f"WHERE channel_order_reference IN ({','.join(['%s'] * len(ch))})", tuple(ch))
        filas += cur.fetchall()
    nombres = sorted({n for o in ordenes.values() for n in o['so_mkp']})
    for i in range(0, len(nombres), 1000):
        ch = nombres[i:i + 1000]
        cur.execute(f"SELECT {campos} FROM somos_reyes.odoo_new_sale_order_live "
                    f"WHERE name IN ({','.join(['%s'] * len(ch))})", tuple(ch))
        filas += cur.fetchall()

    por_nombre = {f['name']: f for f in filas}
    so_por_orden = {}
    for f in filas:
        if f['channel_order_reference'] in ordenes:
            so_por_orden.setdefault(f['channel_order_reference'], {})[f['name']] = f
    for oid, o in ordenes.items():
        for n in o['so_mkp']:
            if n in por_nombre:
                so_por_orden.setdefault(oid, {})[n] = por_nombre[n]
    return {k: list(v.values()) for k, v in so_por_orden.items()}


def clasificar_miner(orden, sos):
    """(status, so_name, nota) con lo que se sabe sin tocar Odoo."""
    if not orden['cobrada']:
        return 'NO_VENTA', None, f"El marketplace no la cobro ({orden['estado_mkp']})"
    if orden['muestra']:
        return 'MUESTRA', (sos[0]['name'] if sos else None), 'Venta de $0: no es venta'
    vigentes = [s for s in sos if s['state'] in ('sale', 'done')]
    if len(vigentes) > 1:
        return 'SO_MULTIPLE', None, f"SO vigentes: {', '.join(s['name'] for s in vigentes)}"
    if vigentes:
        return 'PENDING', vigentes[0]['name'], None
    if sos:
        # Cancelada o en borrador: el loader decide (se factura o se confirma).
        return 'PENDING', sos[0]['name'], None
    return 'NO_SO_IN_ODOO', None, 'Sin SO en la replica de Odoo'


def run_miner(db_conn):
    cur = db_conn.cursor(dictionary=True)
    if ORDER_IDS:
        log.info(f"[MINER] Alcance de pruebas: {len(ORDER_IDS)} orden(es) de MKP_ORDER_IDS.")
    else:
        log.info(f"[MINER] Ventana {START_DATE} a {END_DATE} (exclusivo).")
    ordenes = FUENTE['leer'](cur, START_DATE, END_DATE, ORDER_IDS or None)
    sos = buscar_so_replica(cur, ordenes) if ordenes else {}
    log.info(f"[MINER] {len(ordenes)} orden(es) leidas de {FUENTE['etiqueta']}.")

    marcas_r = ','.join(f"'{s}'" for s in REFRESCABLES)
    marcas_a = ','.join(f"'{s}'" for s in REABRIBLES)
    # MySQL evalua las asignaciones de izquierda a derecha: status va al final
    # para que las condiciones de arriba lean el estado anterior.
    sql = f"""
        INSERT INTO {TBL_BILLING}
            (marketplace, mkp_order_id, odoo_so_name, total_amount, raw_json, status, error_log)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            odoo_so_name = IF(status IN ({marcas_r}), VALUES(odoo_so_name), odoo_so_name),
            total_amount = IF(status IN ({marcas_r}), VALUES(total_amount), total_amount),
            raw_json     = IF(status IN ({marcas_r}), VALUES(raw_json), raw_json),
            error_log    = IF(status IN ({marcas_a}), VALUES(error_log), error_log),
            status       = IF(status IN ({marcas_a}), VALUES(status), status)
    """
    conteo = {}
    w = db_conn.cursor()
    for oid, o in ordenes.items():
        status, so_name, nota = clasificar_miner(o, sos.get(oid, []))
        raw = {
            'marketplace': MARKETPLACE, 'fecha_venta': str(o['fecha']),
            'estado_mkp': o['estado_mkp'], 'cobrada': o['cobrada'],
            'muestra': o['muestra'], 'reembolso_total': o['reembolso_total'],
            'referencia': o['referencia'], 'referencias_alt': o['referencias_alt'],
            'so_replica': [f"{s['name']}:{s['state']}/{s['invoice_status']}/{s['team_name']}"
                           for s in sos.get(oid, [])],
            'detalle': o['detalle'],
        }
        w.execute(sql, (MARKETPLACE, oid, so_name, o['referencia'],
                        json.dumps(raw, ensure_ascii=False, default=str), status, nota))
        conteo[status] = conteo.get(status, 0) + 1
    db_conn.commit()
    w.close()
    cur.close()
    log.info(f"[MINER] Sembradas/refrescadas por estado: {conteo}")


# =============================================================================
# FASE B -- LOADER: tabla de control
# =============================================================================
def reset_stuck_processing(db_conn):
    cur = db_conn.cursor()
    cur.execute(f"""
        UPDATE {TBL_BILLING} SET status = 'ERROR',
               error_log = CONCAT('PROCESSING atorado; revisar en Odoo antes de reintentar. ',
                                  COALESCE(error_log, ''))
        WHERE marketplace = %s AND status = 'PROCESSING'
          AND processed_at < NOW() - INTERVAL %s HOUR""", (MARKETPLACE, STUCK_PROCESSING_HORAS))
    if cur.rowcount:
        log.warning(f"[LOADER] {cur.rowcount} registro(s) PROCESSING atorados pasan a ERROR.")
    db_conn.commit()
    cur.close()


def fetch_pendientes(db_conn):
    cur = db_conn.cursor(dictionary=True)
    sql = f"""
        SELECT id, mkp_order_id, odoo_so_name, total_amount, raw_json, status
        FROM {TBL_BILLING}
        WHERE marketplace = %s AND status IN ('PENDING', 'ERROR')
          AND retry_count_loader < 3"""
    params = [MARKETPLACE]
    if ORDER_IDS:
        sql += f" AND mkp_order_id IN ({','.join(['%s'] * len(ORDER_IDS))})"
        params += ORDER_IDS
    cur.execute(sql + " ORDER BY id ASC", tuple(params))
    filas = cur.fetchall()
    cur.close()
    return filas


def claim(db_conn, record_id):
    """Candado: True solo si esta corrida gano el registro."""
    cur = db_conn.cursor()
    cur.execute(f"""
        UPDATE {TBL_BILLING} SET status = 'PROCESSING', processed_at = NOW()
        WHERE id = %s AND status IN ('PENDING', 'ERROR')""", (record_id,))
    db_conn.commit()
    ganado = cur.rowcount > 0
    cur.close()
    return ganado


def update_control(db_conn, record_id, status, error_log=None, cfdi_uuid=None,
                   sumar_retry=False, facturas=None):
    cur = db_conn.cursor()
    sql_facturas, extra = '', []
    if facturas is not None:
        sql_facturas = (", raw_json = JSON_SET(COALESCE(raw_json, JSON_OBJECT()), "
                        "'$.facturas_odoo', CAST(%s AS JSON))")
        extra = [json.dumps(facturas, ensure_ascii=False, default=str)]
    cur.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = %s, error_log = %s, cfdi_uuid = COALESCE(%s, cfdi_uuid),
            retry_count_loader = retry_count_loader + %s, processed_at = NOW()
            {sql_facturas}
        WHERE id = %s""",
        tuple([status, error_log, cfdi_uuid, 1 if sumar_retry else 0] + extra + [record_id]))
    db_conn.commit()
    cur.close()


# =============================================================================
# FASE B -- LOADER: Odoo
# =============================================================================
def buscar_so(odoo, so_name):
    r = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', so_name)]],
                        {'fields': ['id', 'name', 'state', 'team_id', 'invoice_status',
                                    'invoice_ids', 'order_line', 'date_order',
                                    'effective_date'],
                         'limit': 1})
    return r[0] if r else None


def facturas_existentes(odoo, so_name, so):
    """Facturas vivas de la SO por tres caminos (igual que Amazon):
    invoice_origin, ref AUTOINV:<SO> y las ligadas a la SO (ve las globales).
    Las revertidas por completo con NC no cuentan."""
    campos = ['id', 'name', 'state', 'ref', 'amount_total', 'invoice_origin',
              'payment_state', 'l10n_mx_edi_cfdi_uuid']
    filas = odoo.execute_kw('account.move', 'search_read', [[
        '&', ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
        '|', ('invoice_origin', '=', so_name),
        ('ref', 'like', f'{IDEMPOTENCY_PREFIX}{so_name}%')]],
        {'fields': campos, 'order': 'id asc'})
    vistas = {f['id'] for f in filas}
    ligadas = [i for i in so.get('invoice_ids') or [] if i not in vistas]
    if ligadas:
        filas += odoo.execute_kw('account.move', 'search_read', [[
            ('id', 'in', ligadas), ('move_type', '=', 'out_invoice'),
            ('state', '!=', 'cancel')]], {'fields': campos, 'order': 'id asc'})
    return [f for f in filas if f.get('payment_state') != 'reversed']


def cxc_de_facturas(odoo, ids):
    """{move_id: codigo de la CxC}."""
    lineas = odoo.execute_kw('account.move.line', 'search_read', [[
        ('move_id', 'in', ids), ('account_type', '=', 'asset_receivable')]],
        {'fields': ['move_id', 'account_id']})
    return {l['move_id'][0]: l['account_id'][1].split(' ')[0] for l in lineas}


def confirmar_so(odoo, so):
    """Confirma una SO en borrador. None si quedo confirmada; si no, el motivo."""
    try:
        odoo.execute_kw('sale.order', 'action_confirm', [[so['id']]])
    except OdooWriteUncertain as e:
        log.warning(f"{so['name']}: confirmacion incierta, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        return f"Odoo no permitio confirmar la SO: {e.faultString[:300]}"
    estado = odoo.execute_kw('sale.order', 'read', [[so['id']]], {'fields': ['state']})[0]['state']
    if estado != 'sale':
        return f"La SO sigue en '{estado}' despues de confirmarla."
    so['state'] = estado
    return None


def buscar_por_ref(odoo, ref):
    return odoo.execute_kw('account.move', 'search_read', [[
        ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'), ('ref', '=', ref)]],
        {'fields': ['id', 'name'], 'order': 'id asc'})


def armar_lineas(odoo, so):
    """Lineas de factura sobre la cantidad PEDIDA (sin restriccion de OUT)."""
    lineas = odoo.execute_kw('sale.order.line', 'search_read', [[('id', 'in', so['order_line'])]],
                             {'fields': ['id', 'name', 'sequence', 'display_type', 'product_id',
                                         'product_uom', 'product_uom_qty', 'qty_invoiced',
                                         'qty_delivered', 'price_unit', 'discount', 'tax_id']})
    comandos = []
    for l in lineas:
        qty = l['product_uom_qty']
        if l['display_type'] or l['qty_invoiced'] >= qty:
            continue
        # C-ENVIO no genera movimiento de inventario: se marca entregado.
        pname = l['product_id'][1].upper() if l.get('product_id') else ''
        if 'C-ENVIO' in pname and l['qty_delivered'] < qty:
            try:
                odoo.execute_kw('sale.order.line', 'write', [[l['id']], {'qty_delivered': qty}])
            except Exception as e:
                log.warning(f"{so['name']}: no se pudo marcar C-ENVIO: {e}")
        comandos.append((0, 0, {
            'display_type': 'product',
            'sequence': int(l['sequence']) if l.get('sequence') else 10,
            'name': l['name'],
            'product_uom_id': l['product_uom'][0] if l.get('product_uom') else False,
            'product_id': l['product_id'][0] if l.get('product_id') else False,
            'quantity': qty - l['qty_invoiced'],
            'discount': l['discount'],
            'price_unit': l['price_unit'],
            'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])] if l.get('tax_id') else False,
            'sale_line_ids': [(4, l['id'])],
        }))
    return comandos


def total_estimado(comandos):
    """Total con IVA que producira Odoo (estimado; Odoo redondea por linea)."""
    base = sum(c[2]['quantity'] * c[2]['price_unit'] * (1 - (c[2]['discount'] or 0) / 100.0)
               for c in comandos)
    return round(base * 1.16, 2)


def es_kit(odoo, product_id, cache):
    if product_id not in cache:
        tmpl = odoo.execute_kw('product.product', 'read', [[product_id]],
                               {'fields': ['product_tmpl_id']})[0]['product_tmpl_id'][0]
        cache[product_id] = bool(odoo.execute_kw('mrp.bom', 'search_count', [[
            '|', ('product_id', '=', product_id),
            '&', ('product_id', '=', False), ('product_tmpl_id', '=', tmpl),
            ('type', '=', 'phantom')]]))
    return cache[product_id]


def religar_kits(odoo, inv_id, religar, so_name):
    """Liga a su linea de SO las lineas de kit de una factura ya publicada
    (en SO cancelada se crean sueltas para que tomen costo)."""
    lineas = odoo.execute_kw('account.move.line', 'search_read', [[
        ('move_id', '=', inv_id), ('display_type', '=', 'product'), ('sale_line_ids', '=', False)]],
        {'fields': ['id', 'product_id']})
    for product_id, sale_line_id in religar:
        linea = next((l for l in lineas if l['product_id'] and l['product_id'][0] == product_id), None)
        if not linea:
            continue
        lineas.remove(linea)
        try:
            odoo.execute_kw('account.move.line', 'write',
                            [[linea['id']], {'sale_line_ids': [(4, sale_line_id)]}])
        except Exception as e:
            log.warning(f"{so_name}: no se pudo religar la linea de kit {linea['id']}: {e}")


def timbrar(odoo, inv_id, etiqueta):
    """Timbra una factura publicada. Decide el UUID que queda, no la respuesta."""
    error = None
    try:
        ctx = {'active_model': 'account.move', 'active_ids': [inv_id]}
        wiz = odoo.execute_kw('account.move.send.wizard', 'create',
                              [{'is_download_only': False}], {'context': ctx})
        odoo.execute_kw('account.move.send.wizard', 'action_send_and_print', [[wiz]],
                        {'context': ctx})
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            error = str(e)
    uuid = odoo.execute_kw('account.move', 'read', [[inv_id]],
                           {'fields': ['l10n_mx_edi_cfdi_uuid']})[0]['l10n_mx_edi_cfdi_uuid']
    if not uuid:
        error = error or 'el timbrado termino sin UUID'
        log.warning(f"{etiqueta}: fallo el timbrado: {error}")
    return uuid or None, (None if uuid else error)


def conciliar_salida(odoo, so, inv_id, cuenta):
    """Concilia la 205.06.003 de la factura con la de la salida de almacen.
    Version corta de la de Amazon (aqui no hay facturas reemplazadas):
      CONCILIADA | SIN_SALIDA | PARCIAL | SIN_LINEAS | ERROR"""
    try:
        de_factura = odoo.execute_kw('account.move.line', 'search_read', [[
            ('move_id', '=', inv_id), ('account_id', '=', cuenta)]],
            {'fields': ['id', 'product_id', 'reconciled']})
        if not de_factura:
            return 'SIN_LINEAS'
        if all(l['reconciled'] for l in de_factura):
            return 'CONCILIADA'
        dominio = [('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
                   ('move_id.stock_move_id.sale_line_id.order_id', '=', so['id']),
                   ('balance', '>', 0), ('reconciled', '=', False)]
        salidas = odoo.execute_kw('account.move.line', 'search_read', [dominio],
                                  {'fields': ['id', 'product_id']})
        if not salidas:
            return 'SIN_SALIDA'
        for l in de_factura:
            if l['reconciled']:
                continue
            prod = l['product_id'][0] if l['product_id'] else False
            suyas = [s['id'] for s in salidas
                     if (s['product_id'][0] if s['product_id'] else False) == prod]
            if suyas:
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', [l['id']] + suyas)
        # Kits: la factura lleva el kit y la salida sus componentes. Se
        # concilian juntos solo si lo abierto de los dos lados es igual.
        restos_fac = odoo.execute_kw('account.move.line', 'search_read', [[
            ('move_id', '=', inv_id), ('account_id', '=', cuenta), ('reconciled', '=', False)]],
            {'fields': ['id', 'amount_residual']})
        restos_sal = odoo.execute_kw('account.move.line', 'search_read', [dominio],
                                     {'fields': ['id', 'amount_residual']})
        if restos_fac and restos_sal:
            neto = sum(a_float(r['amount_residual']) for r in restos_fac + restos_sal)
            if abs(neto) <= 0.01 * (len(restos_fac) + len(restos_sal)):
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile',
                                     [r['id'] for r in restos_fac + restos_sal])
        final = odoo.execute_kw('account.move.line', 'read', [[l['id'] for l in de_factura]],
                                {'fields': ['reconciled']})
        return 'CONCILIADA' if all(f['reconciled'] for f in final) else 'PARCIAL'
    except Exception as e:
        log.warning(f"{so['name']}: conciliacion 205.06.003 fallo: {e}")
        return 'ERROR'


def postear_chatter(odoo, inv_id, record, raw, inv, notas):
    """Log de conciliacion en el chatter, en texto plano (Odoo escapa el HTML
    que llega por XML-RPC). Acordado con Finanzas mientras no existan campos
    personalizados en account.move."""
    det = raw.get('detalle', {})
    lineas = [
        f"FACTURACION AUTOMATICA {FUENTE['etiqueta'].upper()}",
        f"order_id: {record['mkp_order_id']}",
        f"SO Odoo: {record['odoo_so_name']}",
        f"Fecha de la venta: {raw.get('fecha_venta')}",
        f"Estado en el marketplace: {raw.get('estado_mkp')}",
        f"Factura Odoo: base {a_float(inv.get('amount_untaxed')):,.2f} | IVA "
        f"{a_float(inv.get('amount_tax')):,.2f} | total {a_float(inv.get('amount_total')):,.2f}",
        f"Pagado por el cliente segun el marketplace: {a_float(raw.get('referencia')):,.2f}",
    ]
    if MARKETPLACE == 'TIKTOK':
        lineas.append(f"Promocion pagada por TikTok (platform_discount): "
                      f"{a_float(det.get('promo_plataforma')):,.2f} (se registra al aplicar el cobro)")
        if det.get('statement_id'):
            lineas.append(f"statement_id: {det['statement_id']}")
    if MARKETPLACE == 'COPPEL':
        lineas.append(f"order_line_id: {', '.join(det.get('order_line_ids') or [])}")
        lineas.append(f"debited_date: {det.get('debited_date') or 'n/d'}")
    lineas += notas
    lineas.append(f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}")
    try:
        odoo.execute_kw('account.move', 'message_post', [inv_id],
                        {'body': '\n'.join(lineas), 'message_type': 'comment'})
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            log.warning(f"{record['odoo_so_name']}: no se pudo escribir el chatter: {e}")


# =============================================================================
# FASE B -- LOADER: una orden
# =============================================================================
def procesar(odoo, db_conn, record, ctx, stats):
    so_name = record['odoo_so_name']
    oid = record['mkp_order_id']
    raw = json.loads(record['raw_json']) if record.get('raw_json') else {}

    def cerrar(status, nota=None, retry=False):
        update_control(db_conn, record['id'], status, error_log=nota, sumar_retry=retry)
        stats['estados'][status] = stats['estados'].get(status, 0) + 1
        stats['detalle'].setdefault(status, []).append(f"{oid} | {so_name or '-'} | {nota or ''}")

    if not so_name:
        return cerrar('NO_SO_IN_ODOO', 'Sin SO cruzada')
    so = buscar_so(odoo, so_name)
    if not so:
        return cerrar('NO_SO_IN_ODOO', f'SO {so_name} no existe en Odoo')

    equipo = so['team_id'][1] if so.get('team_id') else ''
    if equipo != FUENTE['equipo']:
        return cerrar('EQUIPO_DISTINTO', f"La SO es de '{equipo}', no de {FUENTE['equipo']}")

    # --- Re-validacion FAIL-CLOSED: ya hay factura viva? ---
    try:
        existentes = facturas_existentes(odoo, so_name, so)
    except Exception as e:
        return cerrar('ERROR', f'No se pudo re-validar duplicidad: {e}', retry=True)
    if existentes:
        nombres = ', '.join(f['name'] for f in existentes)
        nota = f"Ya tenia factura viva: {nombres}"
        if any(',' in (f.get('invoice_origin') or '') for f in existentes):
            nota += ' (factura global)'
        cuentas = cxc_de_facturas(odoo, [f['id'] for f in existentes])
        malas = [f"{f['name']}={cuentas.get(f['id'])}" for f in existentes
                 if cuentas.get(f['id']) and cuentas.get(f['id']) != FUENTE['cxc']]
        if malas:
            nota += f" | CXC_INCORRECTA: {', '.join(malas)} (debe ser {FUENTE['cxc']})"
            stats['cxc_incorrecta'].append(f"{so_name}: {', '.join(malas)}")
        if len(existentes) > 1:
            nota += ' | MAS_DE_UNA_FACTURA'
            stats['duplicadas'].append(f"{so_name}: {nombres}")
        return cerrar('ALREADY_ODOO_INVOICED', nota)

    # --- Reembolsada al 100% antes de facturar: no hay venta que soportar ---
    if raw.get('reembolso_total'):
        return cerrar('REEMBOLSADA_SIN_FACTURA',
                      f"Reembolso total antes de facturar ({raw.get('estado_mkp')}). "
                      f"Sin factura ni NC.")

    # --- Lista para facturar: con OUT o pasada la gracia ---
    fecha = fecha_factura(so)
    if not fecha and not so.get('effective_date'):
        venta = datetime.strptime(str(raw.get('fecha_venta'))[:19], '%Y-%m-%d %H:%M:%S')
        if (HOY_CORRIDA - venta).days < GRACE_DAYS:
            update_control(db_conn, record['id'], 'PENDING',
                           error_log=f"EN_GRACIA: sin salida de almacen y venta del {venta:%Y-%m-%d}")
            stats['en_gracia'] += 1
            return

    # --- Estado de la SO: toda venta cobrada se factura ---
    notas = []
    so_cancelada = so['state'] == 'cancel'
    if so_cancelada:
        notas.append('SO cancelada en Odoo: se factura porque el marketplace cobro la venta; '
                     'la NC sale por el flujo de devoluciones')
    if so['state'] in ('draft', 'sent'):
        motivo = confirmar_so(odoo, so)
        if motivo:
            return cerrar('SO_DRAFT', motivo, retry=True)
        notas.append('La SO estaba en borrador y se confirmo para facturarla')

    lineas = armar_lineas(odoo, so)
    if not lineas:
        return cerrar('ALREADY_ODOO_INVOICED', 'Sin lineas pendientes de facturar')

    # Kits de SO cancelada: sin la liga a la SO el costo sale en 0. Se crean
    # sueltas y se religan despues de publicar (mismo arreglo que Amazon).
    religar = []
    if so_cancelada:
        for cmd in lineas:
            if cmd[2].get('product_id') and es_kit(odoo, cmd[2]['product_id'], ctx['kits']):
                religar += [(cmd[2]['product_id'], l[1]) for l in cmd[2].pop('sale_line_ids')]

    # --- Importe: la SO contra lo que pago el cliente ---
    estimado = total_estimado(lineas)
    if estimado == 0:
        return cerrar('MUESTRA', 'La SO suma $0: no se factura')
    referencias = [a_float(raw.get('referencia'))] + [a_float(x) for x in raw.get('referencias_alt') or []]
    dif = min((estimado - r for r in referencias), key=abs)
    if abs(dif) > TOLERANCIA_TOTAL:
        return cerrar('TOTAL_DIFF', f"Odoo {estimado:,.2f} vs {MARKETPLACE} "
                                    f"{referencias[0]:,.2f} (dif {dif:+,.2f})")

    # --- Fecha: venta del mes anterior en cierre ---
    if fecha and ahora_cdmx() >= limite_72h():
        update_control(db_conn, record['id'], 'PENDING',
                       error_log=f"FUERA_72H: ya no se puede timbrar con fecha {fecha}")
        stats['fuera_72h'].append(so_name)
        return

    ref = f'{IDEMPOTENCY_PREFIX}{so_name}'
    vals = {
        'ref': ref,
        'move_type': 'out_invoice',
        'partner_id': PARTNER_ID_PUBLICO_GENERAL,
        'invoice_origin': so_name,
        'invoice_line_ids': lineas,
        'l10n_mx_edi_usage': 'S01',
        'l10n_mx_edi_payment_method_id': 3,
        'l10n_mx_edi_payment_policy': 'PUE',
        'team_id': so['team_id'][0],
        'invoice_payment_term_id': 1,
    }
    if fecha:
        vals['invoice_date'] = vals['date'] = fecha

    # --- CREATE ---
    try:
        inv_id = odoo.execute_kw('account.move', 'create', [vals])
    except OdooWriteUncertain as e:
        encontrada = buscar_por_ref(odoo, ref)
        if not encontrada:
            return cerrar('ERROR', f'WRITE_UNCERTAIN en create: {e}', retry=True)
        inv_id = encontrada[0]['id']
        log.warning(f"{so_name}: create incierto; se adopta {encontrada[0]['name']}.")
    except xmlrpc.client.Fault as e:
        return cerrar('ERROR', f'Odoo rechazo el create: {e.faultString[:300]}', retry=True)

    # --- CxC del marketplace ---
    try:
        mlines = odoo.execute_kw('account.move.line', 'search_read', [[('move_id', '=', inv_id)]],
                                 {'fields': ['id', 'account_type']})
        cxc = [(1, m['id'], {'account_id': ctx['cuenta_cxc']})
               for m in mlines if m['account_type'] == 'asset_receivable']
        if cxc:
            odoo.execute_kw('account.move', 'write', [[inv_id], {'line_ids': cxc}],
                            {'context': {'tracking_disable': True}})
    except OdooWriteUncertain as e:
        log.warning(f"{so_name}: write incierto de la CxC: {e}")

    # --- Candado de fecha antes de publicar ---
    if fecha:
        leida = odoo.execute_kw('account.move', 'read', [[inv_id]],
                                {'fields': ['invoice_date']})[0]['invoice_date']
        if str(leida) != fecha:
            odoo.execute_kw('account.move', 'unlink', [[inv_id]])
            return cerrar('ERROR', f"FECHA_INCORRECTA: traia {leida} y debe ser {fecha}. "
                                   f"Se borro el borrador.", retry=True)

    # --- POST ---
    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{so_name}: post incierto, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        return cerrar('ERROR', f'Fallo el post: {e.faultString[:300]}', retry=True)

    if religar:
        religar_kits(odoo, inv_id, religar, so_name)
    if fecha:
        try:
            odoo.execute_kw('account.move', 'write',
                            [[inv_id], {'l10n_mx_edi_post_time': f"{fecha} 23:59:59"}],
                            {'context': {'tracking_disable': True}})
        except Exception as e:
            log.warning(f"{so_name}: no se pudo fijar la hora del CFDI: {e}")

    # --- TIMBRADO: solo produccion ---
    timbre, error_timbre, uuid = 'APAGADO', None, None
    if DO_STAMPING:
        uuid, error_timbre = timbrar(odoo, inv_id, so_name)
        timbre = 'OK' if uuid else 'FALLIDO'

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]], {'fields': [
        'name', 'state', 'amount_untaxed', 'amount_tax', 'amount_total', 'invoice_date']})[0]
    if inv['state'] != 'posted':
        return cerrar('ERROR', f"{inv['name']} quedo en '{inv['state']}' despues de publicar",
                      retry=True)

    conc = conciliar_salida(odoo, so, inv_id, ctx['cuenta_205'])
    if so_cancelada and conc == 'SIN_SALIDA':
        conc = 'SO_CANCELADA'      # la 205.06.003 se concilia con la NC tipo A
    stats['conciliacion_205'][conc] = stats['conciliacion_205'].get(conc, 0) + 1
    notas.append(f"Mercancias facturadas no enviadas ({ACCOUNT_MERC_NO_ENVIADA}): {conc}")
    if timbre == 'FALLIDO':
        notas.insert(0, f"TIMBRADO_FALLIDO: {(error_timbre or '')[:300]}")
    postear_chatter(odoo, inv_id, record, raw, inv, notas)

    total = a_float(inv['amount_total'])
    stats['facturadas'].append({
        'order_id': oid, 'so': so_name, 'so_estado': so['state'],
        'con_out': bool(so.get('effective_date')), 'factura': inv['name'],
        'fecha': inv['invoice_date'], 'total_odoo': total,
        'referencia': referencias[0], 'dif': round(total - referencias[0], 2),
        'conciliacion_205': conc, 'timbre': timbre})
    update_control(db_conn, record['id'], 'ODOO_INVOICED', error_log=' | '.join(notas),
                   cfdi_uuid=uuid, facturas=[{'factura': inv['name'], 'id': inv_id,
                                              'fecha': inv['invoice_date'], 'total': total,
                                              'timbre': timbre, 'uuid': uuid}])
    stats['estados']['ODOO_INVOICED'] = stats['estados'].get('ODOO_INVOICED', 0) + 1
    log.info(f"{so_name}: {inv['name']} total {total:,.2f} (cliente {referencias[0]:,.2f}) "
             f"| 205.06.003 {conc} | timbre {timbre}")


def run_loader(db_conn):
    reset_stuck_processing(db_conn)
    records = fetch_pendientes(db_conn)
    if not records:
        log.info("[LOADER] Sin registros pendientes.")
        return None
    log.info(f"[LOADER] {len(records)} registro(s) por procesar.")

    odoo = OdooModelProxy(timeout=300)
    ctx = {'cuenta_cxc': get_account_id(odoo, FUENTE['cxc']),
           'cuenta_205': get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA),
           'kits': {}}
    stats = {'facturadas': [], 'estados': {}, 'detalle': {}, 'en_gracia': 0,
             'fuera_72h': [], 'cxc_incorrecta': [], 'duplicadas': [],
             'conciliacion_205': {}}

    for i, record in enumerate(records, 1):
        if MAX_FACTURAS and len(stats['facturadas']) >= MAX_FACTURAS:
            log.info(f"[LOADER] Tope de {MAX_FACTURAS} facturas alcanzado (MKP_MAX_FACTURAS).")
            break
        if not claim(db_conn, record['id']):
            continue
        try:
            procesar(odoo, db_conn, record, ctx, stats)
        except Exception as e:
            log.error(f"{record['odoo_so_name']}: error no controlado: {e}")
            update_control(db_conn, record['id'], 'ERROR', error_log=str(e)[:1000],
                           sumar_retry=True)
            stats['estados']['ERROR'] = stats['estados'].get('ERROR', 0) + 1
        if i % 25 == 0:
            log.info(f"  ...{i}/{len(records)}")
    return stats


# =============================================================================
# REPORTE
# =============================================================================
def reportar(stats):
    if not stats:
        return
    log.info('')
    log.info('=' * 100)
    log.info(f"FACTURACION {FUENTE['etiqueta'].upper()} -- RESUMEN ({ENTORNO})")
    log.info('=' * 100)
    f = stats['facturadas']
    if f:
        log.info(f"{'ORDEN':<22}{'SO':<13}{'ESTADO SO':<10}{'OUT':<5}{'FACTURA':<17}"
                 f"{'TOTAL':>11}{'CLIENTE':>11}{'DIF':>7}  205.06.003")
        for r in f:
            log.info(f"{r['order_id']:<22}{r['so']:<13}{r['so_estado']:<10}"
                     f"{'si' if r['con_out'] else 'no':<5}{r['factura']:<17}"
                     f"{r['total_odoo']:>11,.2f}{r['referencia']:>11,.2f}{r['dif']:>+7.2f}  "
                     f"{r['conciliacion_205']}")
    log.info(f"Estados: {stats['estados']} | en gracia: {stats['en_gracia']}")
    log.info(f"Conciliacion 205.06.003: {stats['conciliacion_205']}")
    for estado in ('NO_SO_IN_ODOO', 'TOTAL_DIFF', 'EQUIPO_DISTINTO', 'SO_DRAFT', 'ERROR',
                   'REEMBOLSADA_SIN_FACTURA', 'ALREADY_ODOO_INVOICED'):
        for linea in stats['detalle'].get(estado, [])[:10]:
            log.info(f"  {estado}: {linea}")
    alertas = []
    if stats['cxc_incorrecta']:
        alertas.append(f"{len(stats['cxc_incorrecta'])} factura(s) previas en otra CxC")
    if stats['duplicadas']:
        alertas.append(f"{len(stats['duplicadas'])} SO con mas de una factura viva")
    for estado in ('NO_SO_IN_ODOO', 'TOTAL_DIFF', 'ERROR'):
        if stats['estados'].get(estado):
            alertas.append(f"{stats['estados'][estado]} en {estado}")
    if stats['fuera_72h']:
        alertas.append(f"{len(stats['fuera_72h'])} venta(s) del mes anterior fuera de las 72 h")
    if alertas:
        notify_slack(f"Facturacion {FUENTE['etiqueta']}: {len(f)} factura(s). "
                     f"Revisar: {'; '.join(alertas)}.")


def main():
    validar_entorno()
    banner(f"FACTURACION {FUENTE['etiqueta'].upper()} -- 01_mkp_billing")
    db_conn = get_db_connection()
    try:
        if RUN_MINER:
            run_miner(db_conn)
        if RUN_LOADER:
            reportar(run_loader(db_conn))
    finally:
        db_conn.close()


if __name__ == '__main__':
    main()
