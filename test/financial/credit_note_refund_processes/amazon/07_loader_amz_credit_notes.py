"""
07_loader_amz_credit_notes.py
-----------------------------
LOADER DE NOTAS DE CREDITO AMAZON (fase 4 de notas de credito)

Toma las devoluciones que 06_classifier_amz_refunds.py dejo en CLASSIFIED y
crea en Odoo la nota de credito (account.move out_refund) contra la factura de
la venta. No aplica el cobro: eso es de 08.

ENTRADA
  finance.mkp_refunds_{staging|prod}   filas en CLASSIFIED (tipo, canal, SO y,
                                       en B2B, el CFDI de egreso de Amazon)
  somos_reyes.amz_b2b_invoices         datos del CFDI de egreso (B2B)
  Odoo                                 la SO, sus facturas y sus NC

SALIDA
  Odoo: una NC publicada por devolucion, con ref AUTONC:<orden>:<refund_id>,
  reversed_entry_id = factura y l10n_mx_edi_cfdi_origin = '<rel>|<UUID factura>'.
  MySQL: odoo_invoice_*, odoo_refund_*, cfdi_uuid y status.
  lote_nc_<fecha>.csv en esta carpeta, con lo creado en la corrida.

ESTADOS
  PROCESSING           tomada por esta corrida
  ODOO_NC_POSTED       NC B2C publicada (y timbrada, en produccion)
  ODOO_NC_B2B_LINKED   NC B2B publicada con el UUID del egreso de Amazon; no
                       se timbra
  NO_INVOICE           la SO no tiene factura publicada en Odoo. 06 la reevalua
  NO_SO                la SO no existe en Odoo. 06 la reevalua
  MANUAL_REVIEW        no se emite sola (factura global, reembolso mayor que la
                       factura, factura ya acreditada, lineas que no cuadran)
  ERROR                fallo de Odoo. Para reintentar: regresarla a CLASSIFIED
                       (la revalidacion por ref impide duplicar)

COMO SE ARMA LA NC  >>> SUPUESTOS, PENDIENTES DE CONFIRMAR CON FINANZAS <<<
  Mismo esquema que las NC de Mercado Libre que ya estan en Odoo:
  A  diario DEV, ingreso 402.01.003. El costo regresa a la 205.06.003 y se
     concilia con el de la factura (la venta nunca salio del almacen).
  B  diario DEV, ingreso 402.01.001. El costo se reclasifica de la 205.06.003
     a la 107.06.001 (devoluciones en transito), al 100 %.
  C  diario BON, producto BON_SRV, ingreso 402.01.002. Sin costo.
  IVA: el del impuesto 38 (208.01.001); nunca se edita.
  Lineas A/B: las de la factura si el reembolso es por su total; si es menor,
  las del SKU devuelto (mas el envio si hace falta para cuadrar). Sin
  sale_line_ids: ligarlas reabre la SO para facturar.
  Fecha: B2C la del dia (el SAT no deja timbrar con mas de 72 h); B2B la del
  CFDI de egreso de Amazon.

BLINDAJES (CLAUDE.md seccion 6)
  Revalidacion por ref justo antes del create, fail-closed: si la consulta
  falla, no se crea. Las escrituras no se reintentan: un create incierto se
  resuelve buscando la NC por su ref. Kestra: concurrency 1, sin retry.

VARIABLES
  AMZ_REFUNDS_DRY_RUN   true = lee Odoo y dice que haria; no escribe nada
  AMZ_NC_QUOTA_TIPO     en pruebas, NC por combinacion tipo/canal (default 3)
  AMZ_NC_ORDER_IDS      limita la corrida a esas ordenes (separadas por coma)
"""

import os
import sys
import csv
import json
import logging
import xmlrpc.client
from datetime import datetime, timedelta, timezone

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
    ENTORNO, MARKETPLACE, TAX_ID_MARKETPLACES, ACCOUNT_AMAZON_CXC,
    ACCOUNT_DEV_TRANSITO, ACCOUNT_MERC_NO_ENVIADA, IDEMPOTENCY_PREFIX, UTC_local,
    validar_entorno, banner, notify_slack, get_db_connection, a_float,
    OdooModelProxy, OdooWriteUncertain, get_account_id, ejecutar_sin_retorno,
)

TBL_REFUNDS = f'finance.mkp_refunds_{ENTORNO}'
NC_PREFIX = 'AUTONC:'

DRY_RUN = os.getenv('AMZ_REFUNDS_DRY_RUN', 'false').lower().strip() in ('1', 'true', 'si')
DO_STAMPING = (ENTORNO == 'prod')
QUOTA_TIPO = None if ENTORNO == 'prod' else int(os.getenv('AMZ_NC_QUOTA_TIPO', '3'))
ORDER_IDS = [o.strip() for o in os.getenv('AMZ_NC_ORDER_IDS', '').split(',') if o.strip()]

DIARIO = {'A': 'DEV', 'B': 'DEV', 'C': 'BON'}
CUENTA_INGRESO = {'A': '402.01.003', 'B': '402.01.001', 'C': '402.01.002'}
PRODUCTO_BONIFICACION = 'BON_SRV'
PRODUCTO_ENVIO = 'C-ENVIO'

# El centavo entre el precio con IVA de Amazon y el IVA por linea de Odoo se
# salda en el cobro (704.23.001), igual que en las facturas. En B2B la NC debe
# dar el total del CFDI de Amazon.
TOLERANCIA_TOTAL = 1.00
TOLERANCIA_B2B = 0.05
STUCK_PROCESSING_MIN_AGE_HOURS = 6


def ahora_cdmx():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_local)


# =============================================================================
# MYSQL
# =============================================================================

def leer(db_conn, sql, params=()):
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    cursor.close()
    return rows


def reset_stuck_processing(db_conn):
    """PROCESSING viejo (corrida que murio) -> CLASSIFIED. Es seguro: antes de
    crear se busca la NC por su ref."""
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_REFUNDS} SET status = 'CLASSIFIED'
        WHERE marketplace = %s AND status = 'PROCESSING'
          AND processed_at < (NOW() - INTERVAL %s HOUR)
    """, (MARKETPLACE, STUCK_PROCESSING_MIN_AGE_HOURS))
    db_conn.commit()
    if cursor.rowcount:
        log.warning(f"Liberadas {cursor.rowcount} devoluciones atoradas en PROCESSING.")
    cursor.close()


def fetch_clasificadas(db_conn):
    rows = leer(db_conn, f"""
        SELECT id, mkp_order_id, mp_refund_id, ml_transaction_type, refund_type,
               refund_amount, odoo_so_name, cfdi_uuid AS egreso_uuid, cfdi_date, raw_json
        FROM {TBL_REFUNDS}
        WHERE marketplace = %s AND status = 'CLASSIFIED'
        ORDER BY ml_money_taken_date, id""", (MARKETPLACE,))
    for r in rows:
        r['raw'] = json.loads(r['raw_json']) if r['raw_json'] else {}
        r['canal'] = (r['raw'].get('clasificacion') or {}).get('canal') or 'B2C'
        r['total'] = a_float(r['refund_amount'])
        r['ref'] = f"{NC_PREFIX}{r['mkp_order_id']}:{r['mp_refund_id']}"
    if ORDER_IDS:
        rows = [r for r in rows if r['mkp_order_id'] in ORDER_IDS]
    elif QUOTA_TIPO is not None:
        # En pruebas: unas cuantas de cada combinacion, para que el lote que
        # revisa Finanzas traiga todos los casos.
        tomadas, lote = {}, []
        for r in rows:
            clave = (r['refund_type'], r['canal'])
            if tomadas.get(clave, 0) < QUOTA_TIPO:
                tomadas[clave] = tomadas.get(clave, 0) + 1
                lote.append(r)
        rows = lote
    return rows


def claim(db_conn, record_id):
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_REFUNDS} SET status = 'PROCESSING', processed_at = NOW()
        WHERE id = %s AND status = 'CLASSIFIED'""", (record_id,))
    db_conn.commit()
    ganado = cursor.rowcount > 0
    cursor.close()
    return ganado


def actualizar(db_conn, record_id, status, error_log=None, sumar_retry=False,
               factura=None, nc=None, cfdi_uuid=None):
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_REFUNDS}
        SET status = %s, error_log = %s, processed_at = NOW(),
            retry_count_loader = COALESCE(retry_count_loader, 0) + %s,
            odoo_invoice_id = COALESCE(%s, odoo_invoice_id),
            odoo_invoice_name = COALESCE(%s, odoo_invoice_name),
            odoo_refund_id = COALESCE(%s, odoo_refund_id),
            odoo_refund_name = COALESCE(%s, odoo_refund_name),
            cfdi_uuid = COALESCE(%s, cfdi_uuid)
        WHERE id = %s
    """, (status, error_log, 1 if sumar_retry else 0,
          (factura or {}).get('id'), (factura or {}).get('name'),
          (nc or {}).get('id'), (nc or {}).get('name'), cfdi_uuid, record_id))
    db_conn.commit()
    cursor.close()


def leer_egreso(db_conn, uuid):
    filas = leer(db_conn, """
        SELECT uuid, series, invoice AS folio, total, date, status, cancellation_date,
               receiver_cfdi_key, related_cfdi_relation_type AS relacion
        FROM somos_reyes.amz_b2b_invoices
        WHERE uuid = %s AND receipt_type = 'E'""", (uuid,))
    return filas[0] if filas else None


# =============================================================================
# ODOO: LECTURAS
# =============================================================================

def cargar_catalogos(odoo):
    cat = {'cuenta': {c: get_account_id(odoo, c) for c in
                      set(CUENTA_INGRESO.values()) | {ACCOUNT_AMAZON_CXC, ACCOUNT_DEV_TRANSITO,
                                                      ACCOUNT_MERC_NO_ENVIADA}},
           'diario': {}}
    for codigo in set(DIARIO.values()):
        j = odoo.execute_kw('account.journal', 'search',
                            [[('code', '=', codigo), ('type', '=', 'sale')]], {'limit': 1})
        if not j:
            raise Exception(f"No existe el diario de ventas '{codigo}' en Odoo.")
        cat['diario'][codigo] = j[0]
    p = odoo.execute_kw('product.product', 'search_read',
                        [[('default_code', '=', PRODUCTO_BONIFICACION)]],
                        {'fields': ['id', 'uom_id', 'type'], 'limit': 1})
    if not p or p[0]['type'] != 'service':
        raise Exception(f"No existe el producto de servicio {PRODUCTO_BONIFICACION} en Odoo.")
    cat['bonificacion'] = p[0]
    return cat


def buscar_nc_por_ref(odoo, ref):
    return odoo.execute_kw('account.move', 'search_read',
                           [[('move_type', '=', 'out_refund'), ('state', '!=', 'cancel'),
                             ('ref', '=', ref)]],
                           {'fields': ['id', 'name', 'state', 'amount_total',
                                       'l10n_mx_edi_cfdi_uuid'], 'order': 'id asc'})


def facturas_y_nc(odoo, so):
    """Facturas publicadas de la SO y sus NC vivas. Mismos tres caminos que
    01 (invoice_origin, ref AUTOINV: y sale.order.invoice_ids)."""
    campos = ['id', 'name', 'ref', 'invoice_origin', 'amount_total', 'partner_id',
              'l10n_mx_edi_cfdi_uuid', 'l10n_mx_edi_payment_method_id', 'reversed_entry_id',
              'move_type', 'state']
    ligadas = so.get('invoice_ids') or []
    facturas = odoo.execute_kw('account.move', 'search_read', [[
        '&', '&', ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
        '|', '|', ('invoice_origin', '=', so['name']),
        ('ref', 'like', f"{IDEMPOTENCY_PREFIX}{so['name']}%"), ('id', 'in', ligadas)]],
        {'fields': campos, 'order': 'id asc'})
    ncs = odoo.execute_kw('account.move', 'search_read', [[
        '&', '&', ('move_type', '=', 'out_refund'), ('state', '!=', 'cancel'),
        '|', '|', ('reversed_entry_id', 'in', [f['id'] for f in facturas]),
        ('invoice_origin', 'ilike', so['name']), ('id', 'in', ligadas)]],
        {'fields': campos, 'order': 'id asc'})
    return facturas, ncs


def elegir_factura(r, facturas, ncs):
    """Devuelve (factura, None) o (None, (status, motivo)).

    Una orden puede tener varias facturas (una por CFDI de Amazon, o la de
    publico general refacturada al cliente). Se toma la que todavia tiene
    saldo sin acreditar para este reembolso; de preferencia, la del mismo total.
    """
    if not facturas:
        return None, ('NO_INVOICE', 'la SO no tiene factura publicada en Odoo')
    propias = [f for f in facturas if ',' not in (f['invoice_origin'] or '')]
    if not propias:
        return None, ('MANUAL_REVIEW', f"FACTURA_GLOBAL: {facturas[0]['name']} ampara varias SO")

    acreditado = {f['id']: 0.0 for f in propias}
    for nc in ncs:
        origen = nc['reversed_entry_id'][0] if nc['reversed_entry_id'] else None
        if origen in acreditado:
            acreditado[origen] += a_float(nc['amount_total'])
        elif nc['state'] == 'posted':
            # NC sin liga (refacturacion del portal): neutraliza a la factura
            # mas vieja del mismo importe, como netear_con_nc() de 01.
            par = next((f for f in propias if acreditado[f['id']] == 0.0
                        and abs(a_float(f['amount_total']) - a_float(nc['amount_total'])) <= 0.01), None)
            if par:
                acreditado[par['id']] = a_float(par['amount_total'])

    con_saldo = [f for f in propias
                 if a_float(f['amount_total']) - acreditado[f['id']] >= r['total'] - TOLERANCIA_TOTAL]
    if not con_saldo:
        detalle = ', '.join(f"{f['name']} {a_float(f['amount_total']):,.2f} "
                            f"(acreditado {acreditado[f['id']]:,.2f})" for f in propias)
        return None, ('MANUAL_REVIEW', f"SIN_SALDO_EN_FACTURA: reembolso {r['total']:,.2f} "
                                       f"mayor que lo que queda por acreditar: {detalle}")
    exacta = [f for f in con_saldo if abs(a_float(f['amount_total']) - r['total']) <= TOLERANCIA_TOTAL]
    return (exacta or con_saldo)[0], None


def armar_lineas(odoo, r, factura, cat):
    """Devuelve (comandos, total_estimado) o (None, motivo)."""
    tipo = r['refund_type']
    cuenta = cat['cuenta'][CUENTA_INGRESO[tipo]]
    impuesto = [(6, 0, [TAX_ID_MARKETPLACES])]

    if tipo == 'C':
        base = round(r['total'] / 1.16, 2)
        return [(0, 0, {'product_id': cat['bonificacion']['id'],
                        'product_uom_id': cat['bonificacion']['uom_id'][0],
                        'name': f"{PRODUCTO_BONIFICACION}-{r['mkp_order_id']}",
                        'quantity': 1, 'price_unit': base,
                        'tax_ids': impuesto, 'account_id': cuenta})], round(base * 1.16, 2)

    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', factura['id']), ('display_type', '=', 'product')]],
                             {'fields': ['product_id', 'name', 'quantity', 'price_unit', 'discount',
                                         'product_uom_id', 'price_total']})
    if abs(a_float(factura['amount_total']) - r['total']) <= TOLERANCIA_TOTAL:
        elegidas = [(l, l['quantity']) for l in lineas]
    else:
        # Reembolso menor que la factura: solo el SKU devuelto.
        productos = odoo.execute_kw('product.product', 'read',
                                    [[l['product_id'][0] for l in lineas if l['product_id']]],
                                    {'fields': ['default_code']})
        sku = {p['id']: (p['default_code'] or '').strip().upper() for p in productos}
        pedido = {}
        for p in r['raw'].get('piezas') or []:
            clave = (p.get('sku') or '').strip().upper()
            pedido[clave] = pedido.get(clave, 0) + (p.get('cantidad') or 1)
        elegidas, envio = [], []
        for l in lineas:
            codigo = sku.get(l['product_id'][0]) if l['product_id'] else None
            if codigo in pedido and pedido[codigo] > 0:
                qty = min(pedido[codigo], l['quantity'])
                pedido[codigo] -= qty
                elegidas.append((l, qty))
            elif codigo == PRODUCTO_ENVIO:
                envio.append((l, l['quantity']))

        def total_de(sel):
            return sum(a_float(l['price_total']) * q / l['quantity'] for l, q in sel if l['quantity'])

        if not elegidas:
            return None, (f"LINEAS_NO_CUADRAN: la factura {factura['name']} no tiene el SKU "
                          f"devuelto ({', '.join(pedido) or 'sin SKU'})")
        if abs(total_de(elegidas) - r['total']) > TOLERANCIA_TOTAL:
            if envio and abs(total_de(elegidas + envio) - r['total']) <= TOLERANCIA_TOTAL:
                elegidas += envio
            else:
                return None, (f"LINEAS_NO_CUADRAN: las lineas del SKU dan "
                              f"{total_de(elegidas):,.2f} y el reembolso es {r['total']:,.2f}")

    comandos = [(0, 0, {'product_id': l['product_id'][0] if l['product_id'] else False,
                        'product_uom_id': l['product_uom_id'][0] if l['product_uom_id'] else False,
                        'name': l['name'], 'quantity': q, 'price_unit': l['price_unit'],
                        'discount': l['discount'], 'tax_ids': impuesto, 'account_id': cuenta})
                for l, q in elegidas]
    return comandos, round(sum(a_float(l['price_total']) * q / l['quantity']
                               for l, q in elegidas if l['quantity']), 2)


# =============================================================================
# ODOO: ESCRITURAS
# =============================================================================

def ajustar_costo(odoo, tipo, nc_id, factura_id, cat):
    """Deja el costo de la NC donde pide Finanzas. Devuelve el texto del estado."""
    if tipo == 'C':
        return 'SIN_COSTO (bonificacion)'
    cuenta_205 = cat['cuenta'][ACCOUNT_MERC_NO_ENVIADA]
    try:
        de_nc = odoo.execute_kw('account.move.line', 'search_read',
                                [[('move_id', '=', nc_id), ('account_id', '=', cuenta_205)]],
                                {'fields': ['id', 'reconciled']})
        if not de_nc:
            return 'SIN_COSTO (la NC no genero costo)'
        ids = [l['id'] for l in de_nc]

        if tipo == 'B':
            try:
                odoo.execute_kw('account.move.line', 'write',
                                [ids, {'account_id': cat['cuenta'][ACCOUNT_DEV_TRANSITO]}])
            except OdooWriteUncertain as e:
                log.warning(f"Reclasificacion de costo incierta, se verifica leyendo: {e}")
            quedan = odoo.execute_kw('account.move.line', 'search_count',
                                     [[('id', 'in', ids), ('account_id', '=', cuenta_205)]])
            return (f"RECLASIFICADO a {ACCOUNT_DEV_TRANSITO}" if not quedan
                    else f"PENDIENTE: el costo sigue en {ACCOUNT_MERC_NO_ENVIADA}")

        # Tipo A: no hubo salida, la 205.06.003 de la factura sigue abierta.
        # Con reversed_entry_id Odoo suele conciliarlas al publicar la NC.
        if all(l['reconciled'] for l in de_nc):
            return 'CONCILIADO con la factura (por Odoo al publicar)'
        de_factura = odoo.execute_kw('account.move.line', 'search',
                                     [[('move_id', '=', factura_id), ('account_id', '=', cuenta_205),
                                       ('reconciled', '=', False)]])
        if not de_factura:
            return (f"ABIERTO: la {ACCOUNT_MERC_NO_ENVIADA} de la factura ya estaba "
                    f"conciliada (hubo salida de almacen)")
        ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', ids + de_factura)
        final = odoo.execute_kw('account.move.line', 'read', [ids], {'fields': ['reconciled']})
        return ('CONCILIADO con la factura' if all(l['reconciled'] for l in final)
                else 'PARCIAL: quedo residuo contra la factura')
    except Exception as e:
        return f"ERROR: {str(e)[:200]}"


def timbrar(odoo, move_id, etiqueta):
    """Igual que en 01: decide el UUID que queda en el documento, no la llamada."""
    error = None
    try:
        ctx = {'active_model': 'account.move', 'active_ids': [move_id]}
        wiz = odoo.execute_kw('account.move.send.wizard', 'create',
                              [{'is_download_only': False}], {'context': ctx})
        odoo.execute_kw('account.move.send.wizard', 'action_send_and_print',
                        [[wiz]], {'context': ctx})
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            error = str(e)
    uuid = odoo.execute_kw('account.move', 'read', [[move_id]],
                           {'fields': ['l10n_mx_edi_cfdi_uuid']})[0]['l10n_mx_edi_cfdi_uuid']
    if not uuid:
        error = error or 'el timbrado termino sin UUID'
        log.warning(f"{etiqueta}: fallo el timbrado: {error}")
    return uuid or None, (None if uuid else error)


def postear_chatter(odoo, nc_id, r, factura, egreso, costo):
    imp = r['raw'].get('importes') or {}
    lib = r['raw'].get('liberacion') or {}
    lineas = [
        "Nota de credito por devolucion de Amazon (07_loader_amz_credit_notes.py)",
        f"Orden: {r['mkp_order_id']}",
        f"Refund ID: {r['raw'].get('refund_id') or r['mp_refund_id']}",
        f"Tipo {r['refund_type']} | {r['canal']} | {r['ml_transaction_type']}",
        f"Reembolso al cliente: {r['total']:,.2f} (base {a_float(imp.get('base')):,.2f}, "
        f"IVA {a_float(imp.get('iva')):,.2f})",
        f"Settlement: {lib.get('settlement_id') or 'aun retenido por Amazon'}",
        f"Factura de origen: {factura['name']} | UUID {factura['l10n_mx_edi_cfdi_uuid'] or 'n/d'}",
        f"Costo: {costo}",
    ]
    if egreso:
        lineas.append(f"CFDI de egreso de Amazon Business {egreso['series']}-{egreso['folio']} | "
                      f"UUID {egreso['uuid']}. No se adjunta XML ni se timbra.")
    try:
        odoo.execute_kw('account.move', 'message_post', [nc_id],
                        {'body': '\n'.join(lineas), 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo postear el chatter en la NC {nc_id}: {e}")


def procesar(odoo, db_conn, r, cat, resultados):
    """Una devolucion -> una NC. Devuelve el estado final."""
    etiqueta = f"{r['mkp_order_id']} [{r['refund_type']}/{r['canal']}]"
    es_b2b = r['canal'] == 'B2B'

    def cerrar(status, motivo=None, **kw):
        if not DRY_RUN:
            actualizar(db_conn, r['id'], status, error_log=motivo, **kw)
        nivel = log.info if status.startswith('ODOO_NC') else log.warning
        nivel(f"{etiqueta}: {status}{' - ' + motivo if motivo else ''}")
        return status

    so = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', r['odoo_so_name'])]],
                         {'fields': ['id', 'name', 'state', 'team_id', 'invoice_ids'], 'limit': 1})
    if not so:
        return cerrar('NO_SO', f"la SO {r['odoo_so_name']} no existe en Odoo")
    so = so[0]

    facturas, ncs = facturas_y_nc(odoo, so)
    factura, problema = elegir_factura(r, facturas, ncs)
    if problema:
        return cerrar(*problema)

    egreso = None
    if es_b2b:
        egreso = leer_egreso(db_conn, r['egreso_uuid'])
        if not egreso or egreso['cancellation_date'] or egreso['status'] != 'AUTHORIZED':
            return cerrar('MANUAL_REVIEW', f"EGRESO_NO_VIGENTE: CFDI {r['egreso_uuid']} "
                                           f"cancelado o inexistente en amz_b2b_invoices")
    elif DO_STAMPING and not factura['l10n_mx_edi_cfdi_uuid']:
        return cerrar('MANUAL_REVIEW', f"FACTURA_SIN_UUID: {factura['name']} no esta timbrada; "
                                       f"la NC no tendria CFDI que relacionar")

    comandos, estimado = armar_lineas(odoo, r, factura, cat)
    if comandos is None:
        return cerrar('MANUAL_REVIEW', estimado, factura=factura)

    fecha = str(egreso['date'])[:10] if es_b2b else ahora_cdmx().strftime('%Y-%m-%d')
    relacion = (egreso['relacion'] or '03') if es_b2b else '01'
    vals = {
        'ref': r['ref'],
        'move_type': 'out_refund',
        'journal_id': cat['diario'][DIARIO[r['refund_type']]],
        'partner_id': factura['partner_id'][0],
        'invoice_origin': so['name'],
        'reversed_entry_id': factura['id'],
        'invoice_date': fecha,
        'date': fecha,
        'invoice_line_ids': comandos,
        'team_id': so['team_id'][0] if so['team_id'] else False,
        'l10n_mx_edi_usage': (egreso['receiver_cfdi_key'] or 'G02') if es_b2b else 'S01',
        'l10n_mx_edi_payment_policy': 'PUE',
    }
    if factura['l10n_mx_edi_payment_method_id']:
        vals['l10n_mx_edi_payment_method_id'] = factura['l10n_mx_edi_payment_method_id'][0]
    if factura['l10n_mx_edi_cfdi_uuid']:
        vals['l10n_mx_edi_cfdi_origin'] = f"{relacion}|{factura['l10n_mx_edi_cfdi_uuid']}"

    # --- REVALIDACION FAIL-CLOSED: si esta lectura falla, no se crea ---
    try:
        previas = buscar_nc_por_ref(odoo, r['ref'])
    except Exception as e:
        return cerrar('CLASSIFIED', f"no se pudo revalidar en Odoo, no se crea: {e}")
    if previas and previas[0]['state'] != 'posted':
        return cerrar('ERROR', f"NC_EN_BORRADOR: {previas[0]['name'] or previas[0]['id']} ya "
                               f"existe con la ref {r['ref']}; publicarla o borrarla a mano",
                      factura=factura, nc=previas[0])

    if DRY_RUN:
        accion = f"ya existe {previas[0]['name']}" if previas else 'se crearia'
        log.info(f"{etiqueta}: {accion} | factura {factura['name']} "
                 f"{a_float(factura['amount_total']):,.2f} | NC {estimado:,.2f} en "
                 f"{DIARIO[r['refund_type']]} con {len(comandos)} linea(s), fecha {fecha}")
        return 'ADOPTARIA' if previas else 'CREARIA'

    adoptada = bool(previas)
    if adoptada:
        nc_id = previas[0]['id']
        log.info(f"{etiqueta}: ya existia {previas[0]['name']}; se adopta.")
    else:
        try:
            nc_id = odoo.execute_kw('account.move', 'create', [vals])
        except OdooWriteUncertain as e:
            log.warning(f"{etiqueta}: create incierto, se busca por ref ({e})")
            encontrada = buscar_nc_por_ref(odoo, r['ref'])
            if not encontrada:
                return cerrar('ERROR', f"WRITE_UNCERTAIN en create: {e}", sumar_retry=True)
            nc_id = encontrada[0]['id']
        except xmlrpc.client.Fault as e:
            return cerrar('ERROR', f"Odoo rechazo el create: {e.faultString[:400]}", sumar_retry=True)

        # La CxC de la NC va en la misma cuenta que la de la factura: solo asi
        # se pueden conciliar entre si.
        cxc_factura = odoo.execute_kw(
            'account.move.line', 'search_read',
            [[('move_id', '=', factura['id']), ('account_type', '=', 'asset_receivable')]],
            {'fields': ['account_id'], 'limit': 1})
        cuenta_cxc = cxc_factura[0]['account_id'][0] if cxc_factura else cat['cuenta'][ACCOUNT_AMAZON_CXC]
        if cuenta_cxc != cat['cuenta'][ACCOUNT_AMAZON_CXC]:
            log.warning(f"{etiqueta}: la factura {factura['name']} no esta en la "
                        f"{ACCOUNT_AMAZON_CXC} (CXC_INCORRECTA); la NC usa su misma cuenta.")
        cxc_nc = odoo.execute_kw(
            'account.move.line', 'search',
            [[('move_id', '=', nc_id), ('account_type', '=', 'asset_receivable')]])
        cambios = {'line_ids': [(1, i, {'account_id': cuenta_cxc}) for i in cxc_nc]}
        if es_b2b:
            cambios['l10n_mx_edi_cfdi_uuid'] = egreso['uuid']
            cambios['narration'] = (f"CFDI de egreso Amazon Business {egreso['series']}-"
                                    f"{egreso['folio']} | UUID {egreso['uuid']}")
        try:
            odoo.execute_kw('account.move', 'write', [[nc_id], cambios],
                            {'context': {'tracking_disable': True}})
        except OdooWriteUncertain as e:
            log.warning(f"{etiqueta}: write incierto de CxC/UUID: {e}")

        # El borrador tiene que dar el importe esperado antes de publicarse.
        borrador = odoo.execute_kw('account.move', 'read', [[nc_id]],
                                   {'fields': ['amount_total']})[0]
        esperado = a_float(egreso['total']) if es_b2b else r['total']
        tolerancia = TOLERANCIA_B2B if es_b2b else TOLERANCIA_TOTAL
        if abs(a_float(borrador['amount_total']) - esperado) > tolerancia:
            odoo.execute_kw('account.move', 'unlink', [[nc_id]])
            return cerrar('MANUAL_REVIEW',
                          f"TOTAL_DIFF: la NC daba {a_float(borrador['amount_total']):,.2f} y "
                          f"debe ser {esperado:,.2f}. Se borro el borrador.", factura=factura)

        try:
            odoo.execute_kw('account.move', 'action_post', [[nc_id]])
        except OdooWriteUncertain as e:
            log.warning(f"{etiqueta}: post incierto, se verifica leyendo: {e}")
        except xmlrpc.client.Fault as e:
            return cerrar('ERROR', f"Fallo el post de la NC {nc_id} (queda en borrador): "
                                   f"{e.faultString[:400]}", sumar_retry=True,
                          factura=factura, nc={'id': nc_id, 'name': None})

    nc = odoo.execute_kw('account.move', 'read', [[nc_id]],
                         {'fields': ['id', 'name', 'state', 'amount_total', 'invoice_date',
                                     'l10n_mx_edi_cfdi_uuid']})[0]
    if nc['state'] != 'posted':
        return cerrar('ERROR', f"la NC {nc_id} no quedo publicada (estado {nc['state']})",
                      sumar_retry=True, factura=factura, nc=nc)

    costo = ajustar_costo(odoo, r['refund_type'], nc_id, factura['id'], cat)

    timbre, error_timbre = ('AMAZON' if es_b2b else 'APAGADO'), None
    if DO_STAMPING and not es_b2b and not nc['l10n_mx_edi_cfdi_uuid']:
        uuid, error_timbre = timbrar(odoo, nc_id, etiqueta)
        nc['l10n_mx_edi_cfdi_uuid'] = uuid
        timbre = 'OK' if uuid else 'FALLIDO'

    if not adoptada:
        postear_chatter(odoo, nc_id, r, factura, egreso, costo)

    resultados.append({
        'order_id': r['mkp_order_id'], 'refund_id': r['mp_refund_id'],
        'tipo': r['refund_type'], 'canal': r['canal'], 'so': so['name'], 'so_estado': so['state'],
        'factura': factura['name'], 'factura_total': a_float(factura['amount_total']),
        'nc': nc['name'], 'nc_total': a_float(nc['amount_total']), 'reembolso_amazon': r['total'],
        'diferencia': round(a_float(nc['amount_total']) - r['total'], 2),
        'fecha': nc['invoice_date'], 'uuid': nc['l10n_mx_edi_cfdi_uuid'] or '',
        'timbre': timbre, 'costo': costo,
    })
    return cerrar('ODOO_NC_B2B_LINKED' if es_b2b else 'ODOO_NC_POSTED',
                  f"TIMBRADO_FALLIDO: {error_timbre}" if timbre == 'FALLIDO' else None,
                  factura=factura, nc=nc,
                  cfdi_uuid=None if es_b2b else (nc['l10n_mx_edi_cfdi_uuid'] or None))


# =============================================================================
# CORRIDA
# =============================================================================

def run():
    validar_entorno()
    db_conn = get_db_connection()
    if not DRY_RUN:
        reset_stuck_processing(db_conn)
    pendientes = fetch_clasificadas(db_conn)
    log.info(f"{len(pendientes)} devolucion(es) a procesar"
             f"{' | DRY_RUN: no se escribe' if DRY_RUN else ''}"
             f"{f' | cuota de pruebas: {QUOTA_TIPO} por tipo/canal' if QUOTA_TIPO and not ORDER_IDS else ''}"
             f" | timbrado: {'ENCENDIDO' if DO_STAMPING else 'APAGADO'}")
    if not pendientes:
        db_conn.close()
        return

    odoo = OdooModelProxy()
    cat = cargar_catalogos(odoo)
    conteo, resultados = {}, []
    for r in pendientes:
        if not DRY_RUN and not claim(db_conn, r['id']):
            continue
        try:
            estado = procesar(odoo, db_conn, r, cat, resultados)
        except OdooWriteUncertain as e:
            estado = 'ERROR'
            if not DRY_RUN:
                actualizar(db_conn, r['id'], 'ERROR', error_log=f"WRITE_UNCERTAIN: {e}", sumar_retry=True)
            log.error(f"{r['mkp_order_id']}: escritura incierta, revisar en Odoo la ref {r['ref']}: {e}")
        except Exception as e:
            estado = 'ERROR'
            if not DRY_RUN:
                actualizar(db_conn, r['id'], 'ERROR', error_log=str(e)[:1000], sumar_retry=True)
            log.error(f"{r['mkp_order_id']}: {e}")
        clave = (estado, r['refund_type'], r['canal'])
        acc = conteo.setdefault(clave, [0, 0.0])
        acc[0] += 1
        acc[1] += r['total']
    db_conn.close()

    log.info('')
    log.info(f"{'ESTADO':<20}{'TIPO':<6}{'CANAL':<7}{'N':>5}{'IMPORTE':>16}")
    for (estado, tipo, canal), (n, importe) in sorted(conteo.items()):
        log.info(f"{estado:<20}{tipo:<6}{canal:<7}{n:>5}{importe:>16,.2f}")

    if resultados:
        log.info('')
        for x in resultados:
            log.info(f"{x['nc']} [{x['tipo']}/{x['canal']}] {x['so']} {x['factura']} | "
                     f"NC {x['nc_total']:,.2f} Amazon {x['reembolso_amazon']:,.2f} "
                     f"dif {x['diferencia']:+,.2f} | costo: {x['costo']}")
        salida = os.path.join(_dir, f"lote_nc_{datetime.now():%Y%m%d_%H%M}.csv")
        with open(salida, 'w', newline='', encoding='utf-8-sig') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(resultados[0].keys()))
            writer.writeheader()
            writer.writerows(resultados)
        log.info(f"Detalle: {salida}")

    atender = sum(n for (estado, _, _), (n, _) in conteo.items()
                  if estado in ('ERROR', 'MANUAL_REVIEW'))
    fallidos = [x['nc'] for x in resultados if x['timbre'] == 'FALLIDO']
    if ENTORNO == 'prod' and not DRY_RUN and (atender or fallidos):
        notify_slack(f":warning: NC Amazon: {atender} devolucion(es) en ERROR / MANUAL_REVIEW"
                     f"{f' y {len(fallidos)} NC sin timbrar' if fallidos else ''} "
                     f"({TBL_REFUNDS}).")


if __name__ == '__main__':
    banner('LOADER DE NOTAS DE CREDITO -- AMAZON')
    try:
        run()
    except Exception as e:
        log.error(f'Fallo critico en el loader de notas de credito: {e}')
        sys.exit(1)
