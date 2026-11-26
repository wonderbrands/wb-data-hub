"""
04_wm_billing_push.py
---------------------
Emite (y en produccion timbra) la factura de las ventas de Walmart que el
cliente NO facturo con Walmart: una factura por SO, a publico en general
(XAXX010101000), CxC 105.01.007, sin restriccion de OUT.

Toma las filas PENDING_WB_INVOICE. Candados, en orden:

  A. FENIX AL DIA (fail-closed). Si la ultima corrida completa del ETL de la
     API de Fenix (00b, marca ETL:ULTIMA_CORRIDA) tiene mas de
     WM_FENIX_MAX_HORAS, no se emite NADA: no sabriamos si el cliente pidio
     factura a Walmart y saldria un doble CFDI. Ya no depende del crawler.
  B. ULTIMA MIRADA A FENIX. Justo antes de emitir se buscan, por las ordenes
     del lote, peticiones de factura de la API (filas PRECFDI / BLOQUEADO de
     cualquier estado) y filas del crawler. Si aparece cualquiera, la orden
     pasa a RECLASIFICAR y no se emite. Si la consulta falla, se aborta.
  B2. CFDI DE WALMART SIN LIGAR. La API de Fenix no lista todas las peticiones
     (el pedido 600000121084541 tiene CFDI vigente y no sale en la API). Si en
     el repositorio hay un CFDI de Walmart SIN_LIGAR timbrado entre la venta y
     WM_B2_DIAS dias despues, con total = precio de una linea x piezas del CFDI
     (+-0.02) y el mismo UPC que la linea, la orden pasa a RECLASIFICAR: puede
     ser suyo. Se resuelve cuando ese CFDI se liga (por folio, crawler o
     Finanzas). El UPC de la linea sale de wm_order_line (API de Walmart), que
     coincide con el NoIdentificacion del CFDI (232 de 232 en octubre).
  C. GRACIA. El cliente tiene 30 dias para pedir factura a Walmart. Se espera
     WM_GRACE_DAYS desde order_date: 7 dias (Finanzas lo dejo a criterio de
     Data el 25-nov). En los datos el 94% de las peticiones llega en 3 dias, el
     99.8% en 5 y el 100% en 10 (434 peticiones de ventas hasta el 9-oct).
     Mas gracia casi no evita dobles CFDI y amontona el cierre de mes, donde
     todo lo pendiente se timbra en los dias 1-2. En el cierre de mes (dias 1..WM_DIAS_CIERRE_MES) la venta del mes
     anterior se emite sin gracia con fecha del ultimo dia de ese mes.
  D. 72 HORAS. Con fecha del mes anterior, no se crea despues de
     limite_72h(): el SAT ya no la timbraria.
  E. REVALIDACION FAIL-CLOSED justo antes del create: cualquier factura viva
     de la SO (1a1, proceso manual, global, CFDI de Walmart) -> no se crea
     (ALREADY_ODOO_INVOICED). Si la consulta falla, no se crea.
  F. TOTAL: lo que Odoo va a facturar contra lo que vendio Walmart (+-$1).

Idempotencia: ref = AUTOINV:<SO>. Un create con respuesta perdida se busca por
esa ref y se adopta; nunca se reintenta.

Variables: WM_ENTORNO, WM_MAX_FACTURAS, WM_GRACE_DAYS, WM_FENIX_MAX_HORAS,
WM_DIAS_CIERRE_MES.
"""

import os
import sys
import json
import xmlrpc.client
from collections import Counter
from datetime import timedelta

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, validar_entorno, notify_slack,
                        get_db_connection, OdooModelProxy, OdooWriteUncertain,
                        cargar_catalogos, buscar_so, facturas_existentes, resumen_factura,
                        confirmar_so, buscar_por_ref, leer_lineas_so, comando_linea,
                        total_estimado, fijar_cxc, timbrar, conciliar_salida, postear_chatter,
                        reset_stuck_processing, claim, update_fila, frescura_etl_fenix, MKP_FENIX,
                        CFDI_SIN_LIGAR, upc_por_linea,
                        ahora_cdmx, inicio_de_mes, en_cierre_de_mes, limite_72h, a_datetime,
                        a_float, lotes, HOY_CORRIDA, DO_STAMPING,
                        TBL_BILLING, MARKETPLACE, IDEMPOTENCY_PREFIX,
                        PARTNER_ID_PUBLICO_GENERAL, PAYMENT_METHOD_TRANSFERENCIA,
                        TOLERANCIA_TOTAL, MAX_FACTURAS, ACCOUNT_MERC_NO_ENVIADA,
                        ST_PENDING_WB, ST_WB_INVOICED, ST_ALREADY, ST_NO_SO,
                        ST_SO_CANCELADA, ST_SO_OTRO_EQUIPO, ST_TOTAL_DIFF,
                        ST_RECLASIFICAR, ST_ERROR, ST_BORRADOR_AJENO)

GRACE_DAYS = int(os.getenv('WM_GRACE_DAYS', '7'))
FENIX_MAX_HORAS = float(os.getenv('WM_FENIX_MAX_HORAS', '48'))
B2_DIAS = int(os.getenv('WM_B2_DIAS', '7'))   # 0 = candado B2 apagado


def fetch_pendientes(db):
    cur = db.cursor(dictionary=True)
    cur.execute(f"""
        SELECT id, mkp_order_id, odoo_so_name, total_amount, raw_json
        FROM {TBL_BILLING}
        WHERE marketplace = %s AND status = %s AND retry_count_loader < 3
        ORDER BY id ASC
    """, (MARKETPLACE, ST_PENDING_WB))
    filas = cur.fetchall()
    cur.close()
    for f in filas:
        f['raw'] = json.loads(f['raw_json']) if isinstance(f['raw_json'], str) else (f['raw_json'] or {})
    return filas


def fecha_y_turno(fila):
    """Decide si a la fila ya le toca y con que fecha.
    Devuelve (le_toca, fecha_factura o None, motivo)."""
    venta = a_datetime(fila['raw']['order_date'])       # ya en hora de Mexico
    mes_anterior = venta < inicio_de_mes()
    if mes_anterior and en_cierre_de_mes():
        fecha = (inicio_de_mes() - timedelta(days=1)).strftime('%Y-%m-%d')
        if ahora_cdmx() >= limite_72h():
            return False, fecha, 'FUERA_72H'
        return True, fecha, 'CIERRE_MES'
    if venta + timedelta(days=GRACE_DAYS) > HOY_CORRIDA:
        return False, None, 'GRACIA'
    return True, None, ('MES_ANTERIOR_FUERA_DE_CIERRE' if mes_anterior else 'NORMAL')


def ordenes_con_fenix(db, pedidos):
    """Ultima mirada a Fenix: ordenes con peticion de factura en la API (00b) o
    con fila en el crawler. Lanza si falla (fail-closed)."""
    con_fila = set()
    buscados = set(pedidos)
    cur = db.cursor()
    cur.execute(f"""SELECT JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.orderno')) FROM {TBL_BILLING}
                    WHERE marketplace = %s AND (mkp_order_id LIKE 'PRECFDI:%%' OR mkp_order_id LIKE 'BLOQUEADO:%%')""",
                (MKP_FENIX,))
    con_fila.update(r[0] for r in cur.fetchall() if r[0] in buscados)
    for chunk in lotes(pedidos, 500):
        cur.execute(f"""SELECT DISTINCT pedido FROM somos_reyes.wm_fenix_invoices_report
                        WHERE tipo_documento = 'I' AND pedido IN ({','.join(['%s'] * len(chunk))})""",
                    tuple(chunk))
        con_fila.update(r[0] for r in cur.fetchall())
    cur.close()
    return con_fila


def cfdi_sin_ligar_posible(db, filas):
    """{id de fila: uuid} de las ordenes del lote para las que hay un CFDI de
    Walmart SIN_LIGAR que podria ser suyo (B2). Lanza si falla (fail-closed)."""
    if B2_DIAS <= 0 or not filas:
        return {}
    cur = db.cursor()
    cur.execute(f"""SELECT cfdi_uuid, raw_json FROM {TBL_BILLING}
                    WHERE marketplace = %s AND mkp_order_id LIKE 'CFDI:%%' AND status = %s""",
                (MKP_FENIX, CFDI_SIN_LIGAR))
    sueltos = []
    for uuid, raw in cur.fetchall():
        raw = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
        if raw.get('fecha'):
            sueltos.append((uuid, a_datetime(str(raw['fecha']).replace('T', ' ')),
                            a_float(raw.get('total')), a_float(raw.get('cantidad')) or 1.0,
                            str(raw.get('upc') or '').lstrip('0')))
    # UPC de cada linea (API de Walmart): es el NoIdentificacion del CFDI.
    upcs = upc_por_linea(cur, {l.get('iderp') for f in filas for l in f['raw'].get('lineas', [])}) if sueltos else {}
    cur.close()
    posibles = {}
    for f in filas:
        venta = a_datetime(f['raw']['order_date'])
        for l in f['raw'].get('lineas', []):
            unit = a_float(l.get('price_amount')) / max(int(l.get('quantity') or 1), 1)
            upc_linea = upcs.get(l.get('iderp'))
            for uuid, fecha, total, cant, upc in sueltos:
                if (venta <= fecha <= venta + timedelta(days=B2_DIAS)
                        and abs(unit * cant - total) <= 0.02
                        and (not upc_linea or not upc or upc == upc_linea)):
                    posibles[f['id']] = uuid
                    break
            if f['id'] in posibles:
                break
    return posibles


def procesar(odoo, db, fila, fecha, catalogos, stats):
    rid = fila['id']
    raw = fila['raw']
    so_name = fila['odoo_so_name']
    pedido = fila['mkp_order_id']

    so = buscar_so(odoo, so_name) if so_name else None
    if not so:
        update_fila(db, rid, ST_NO_SO, f"SO {so_name} no existe en Odoo")
        return 'no_so'
    if so['state'] == 'cancel':
        update_fila(db, rid, ST_SO_CANCELADA, 'SO cancelada en Odoo (decision pendiente de Finanzas)')
        return 'so_cancelada'
    if not so['team_id'] or so['team_id'][0] != catalogos['team_walmart']:
        update_fila(db, rid, ST_SO_OTRO_EQUIPO, f"equipo {so['team_id']}")
        return 'otro_equipo'
    confirmada_aqui = False
    if so['state'] in ('draft', 'sent'):
        motivo = confirmar_so(odoo, so)
        if motivo:
            update_fila(db, rid, ST_ERROR, motivo, sumar_retry=True)
            return 'error'
        stats['so_confirmadas'] += 1
        confirmada_aqui = True

    lineas = leer_lineas_so(odoo, so)
    comandos = [comando_linea(l, l['product_uom_qty'] - l['qty_invoiced']) for l in lineas]

    # --- E. REVALIDACION FAIL-CLOSED ---
    ref = f"{IDEMPOTENCY_PREFIX}{so['name']}"
    try:
        vivas = facturas_existentes(odoo, so['name'], so)
    except Exception as e:
        update_fila(db, rid, ST_ERROR, f"Revalidacion fallida, no se crea: {e}", sumar_retry=True)
        return 'error'
    propias_borrador = [f for f in vivas if f.get('ref') == ref and f['state'] == 'draft']
    otras = [f for f in vivas if f not in propias_borrador]
    if otras and all(f['state'] == 'draft' for f in otras):
        update_fila(db, rid, ST_BORRADOR_AJENO,
                    'La SO tiene un borrador sin CFDI de otro proceso: cancelarlo',
                    facturas=[resumen_factura(f) for f in otras])
        return 'borrador_ajeno'
    if otras:
        update_fila(db, rid, ST_ALREADY, 'La SO ya tiene factura viva',
                    facturas=[resumen_factura(f) for f in vivas])
        return 'ya_facturada'
    if not comandos and not propias_borrador:
        update_fila(db, rid, ST_ALREADY, 'La SO no tiene lineas pendientes de facturar')
        return 'ya_facturada'

    if propias_borrador:
        inv_id = propias_borrador[0]['id']
        stats['retomadas'] += 1
    else:
        # --- F. TOTAL ---
        estimado = total_estimado(comandos)
        if abs(estimado - a_float(fila['total_amount'])) > TOLERANCIA_TOTAL:
            update_fila(db, rid, ST_TOTAL_DIFF,
                        f"Odoo estimado {estimado:,.2f} vs Walmart {a_float(fila['total_amount']):,.2f}")
            return 'total_diff'

        vals = {
            'ref': ref,
            'move_type': 'out_invoice',
            'partner_id': PARTNER_ID_PUBLICO_GENERAL,
            'invoice_origin': so['name'],
            'invoice_line_ids': comandos,
            'team_id': catalogos['team_walmart'],
            'l10n_mx_edi_usage': 'S01',
            'l10n_mx_edi_payment_method_id': PAYMENT_METHOD_TRANSFERENCIA,
            'l10n_mx_edi_payment_policy': 'PUE',
            'invoice_payment_term_id': 1,
        }
        if fecha:
            vals.update({'invoice_date': fecha, 'date': fecha})
        try:
            inv_id = odoo.execute_kw('account.move', 'create', [vals])
        except OdooWriteUncertain as e:
            encontradas = buscar_por_ref(odoo, ref)
            if not encontradas:
                update_fila(db, rid, ST_ERROR, f"WRITE_UNCERTAIN en create y sin factura: {e}",
                            sumar_retry=True)
                return 'error'
            inv_id = encontradas[0]['id']
            stats['adoptadas_incierto'] += 1
        except xmlrpc.client.Fault as e:
            update_fila(db, rid, ST_ERROR, f"Odoo rechazo el create: {e.faultString[:500]}",
                        sumar_retry=True)
            return 'error'

    try:
        fijar_cxc(odoo, inv_id, catalogos['cuenta_cxc'])
    except OdooWriteUncertain as e:
        log.warning(f"{so['name']}: write incierto de CxC: {e}")

    # Candado de fecha (incidente del 3-nov-2026 en Amazon).
    if fecha:
        leida = odoo.execute_kw('account.move', 'read', [[inv_id]],
                                {'fields': ['invoice_date']})[0]['invoice_date']
        if str(leida) != fecha:
            odoo.execute_kw('account.move', 'unlink', [[inv_id]])
            update_fila(db, rid, ST_ERROR, f"FECHA_INCORRECTA: traia {leida}, debe ser {fecha}. "
                                           f"Se borro el borrador.", sumar_retry=True)
            return 'error'

    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{so['name']}: post incierto, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        update_fila(db, rid, ST_ERROR, f"Fallo el post: {e.faultString[:500]}", sumar_retry=True)
        return 'error'

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['name', 'state', 'invoice_date']})[0]
    if inv['state'] != 'posted':
        update_fila(db, rid, ST_ERROR, f"La factura {inv_id} quedo en {inv['state']}", sumar_retry=True)
        return 'error'

    fecha_ok = (not fecha) or str(inv['invoice_date']) == fecha
    if fecha and fecha_ok:
        # Odoo arma la hora del CFDI con la de publicacion; con fecha del mes
        # anterior se lleva a 23:59:59 de ese dia para no adelantar las 72 h.
        try:
            odoo.execute_kw('account.move', 'write',
                            [[inv_id], {'l10n_mx_edi_post_time': f"{fecha} 23:59:59"}],
                            {'context': {'tracking_disable': True}})
        except Exception as e:
            log.warning(f"{so['name']}: no se pudo fijar la hora del CFDI: {e}")

    timbre, error_timbre, uuid_wb = 'APAGADO', None, None
    if DO_STAMPING and fecha_ok:
        uuid_wb, error_timbre = timbrar(odoo, inv_id, so['name'])
        timbre = 'OK' if uuid_wb else 'FALLIDO'
    elif DO_STAMPING:
        timbre, error_timbre = 'FALLIDO', f"FECHA_INCORRECTA: debe ser {fecha}; no se timbro"

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['name', 'state', 'ref', 'amount_total', 'invoice_date',
                                      'l10n_mx_edi_cfdi_uuid']})[0]
    conc = conciliar_salida(odoo, so, inv_id, catalogos['cuenta_merc_no_enviada'])
    stats[f"205_{conc['estado']}"] += 1
    postear_chatter(odoo, inv_id, [
        'Factura 1 a 1 a publico en general creada por Data via API: Facturacion Walmart.',
        f"customer_order_id {pedido} | purchase_order_id {raw.get('purchase_order_id')} | "
        f"fulfillment {','.join(sorted({l.get('fulfillment_type') or '' for l in raw.get('lineas', [])}))}",
        f"Mercancias facturadas - no enviadas ({ACCOUNT_MERC_NO_ENVIADA}): {conc['estado']} {conc['detalle']}",
    ] + (['SO en borrador confirmada para facturar (Walmart ya vendio).']
         if confirmada_aqui else []))

    dif = round(a_float(inv['amount_total']) - a_float(fila['total_amount']), 2)
    update_fila(db, rid, ST_WB_INVOICED,
                f"TIMBRADO_FALLIDO: {error_timbre[:400]}" if timbre == 'FALLIDO' else None,
                cfdi_uuid=uuid_wb, facturas=[resumen_factura(dict(inv, id=inv_id))],
                extra_json={'timbre': timbre, 'fecha': inv['invoice_date'],
                            'total_odoo': a_float(inv['amount_total']),
                            'total_walmart': a_float(fila['total_amount']), 'diferencia': dif,
                            'conciliacion_205': conc['estado']})
    if timbre == 'FALLIDO':
        stats['timbrado_fallido'] += 1
    log.info(f"{so['name']}: {inv['name']} [{timbre}] Odoo {a_float(inv['amount_total']):,.2f} "
             f"Walmart {a_float(fila['total_amount']):,.2f} dif {dif:+.2f}")
    return 'creada'


def main():
    banner(f"04 - FACTURACION WB A PUBLICO GENERAL (PUSH) | gracia {GRACE_DAYS} d | "
           f"cierre de mes: {'SI' if en_cierre_de_mes() else 'no'} | timbrado: "
           f"{'SI' if DO_STAMPING else 'APAGADO'}")
    validar_entorno()
    db = get_db_connection()

    # --- A. Fenix al dia (ETL de la API, 00b) ---
    cur = db.cursor()
    horas = frescura_etl_fenix(cur)
    cur.close()
    if horas is None or horas > FENIX_MAX_HORAS:
        msg = (f"Walmart 04: NO se emitio ninguna factura. La ultima corrida buena del ETL de la API de "
               f"Fenix fue hace {horas} h (maximo {FENIX_MAX_HORAS} h): no sabemos si el cliente pidio "
               f"factura a Walmart.")
        log.error(msg)
        notify_slack(msg)
        db.close()
        return

    reset_stuck_processing(db)
    filas = fetch_pendientes(db)
    stats = Counter()
    turno = []
    for f in filas:
        le_toca, fecha, motivo = fecha_y_turno(f)
        stats[f"turno_{motivo}"] += 1
        if le_toca:
            turno.append((f, fecha))
    log.info(f"Pendientes: {len(filas)} | les toca hoy: {len(turno)} | tope: {MAX_FACTURAS or 'sin tope'}")
    if not turno:
        return

    # --- B. Ultima mirada a Fenix (fail-closed: si falla, la excepcion aborta) ---
    con_fenix = ordenes_con_fenix(db, [f['mkp_order_id'] for f, _ in turno])
    for f, _ in turno:
        if f['mkp_order_id'] in con_fenix:
            update_fila(db, f['id'], ST_RECLASIFICAR,
                        'Fenix tiene fila para la orden: el cliente pidio factura a Walmart.')
            stats['reclasificadas'] += 1
    turno = [(f, fecha) for f, fecha in turno if f['mkp_order_id'] not in con_fenix]

    # --- B2. CFDI de Walmart sin ligar que podria ser de la orden ---
    posibles = cfdi_sin_ligar_posible(db, [f for f, _ in turno])
    for f, _ in turno:
        if f['id'] in posibles:
            update_fila(db, f['id'], ST_RECLASIFICAR,
                        f"Hay un CFDI de Walmart sin ligar ({posibles[f['id']]}) con el precio de esta orden: "
                        f"puede ser suyo. Se reevalua cuando se ligue.")
            stats['cfdi_sin_ligar_posible'] += 1
    turno = [(f, fecha) for f, fecha in turno if f['id'] not in posibles]

    odoo = OdooModelProxy()
    catalogos = cargar_catalogos(odoo)
    creadas = 0
    for f, fecha in turno:
        if MAX_FACTURAS and creadas >= MAX_FACTURAS:
            log.info(f"Tope de {MAX_FACTURAS} facturas alcanzado.")
            break
        if fecha and ahora_cdmx() >= limite_72h():
            log.error('Se alcanzo el limite de 72 h del cierre de mes: se detiene la corrida.')
            break
        if not claim(db, f['id'], ST_PENDING_WB):
            continue
        try:
            resultado = procesar(odoo, db, f, fecha, catalogos, stats)
        except Exception as e:
            log.exception(f"{f['mkp_order_id']}: error inesperado")
            update_fila(db, f['id'], ST_ERROR, f"Inesperado: {str(e)[:500]}", sumar_retry=True)
            resultado = 'error'
        stats[resultado] += 1
        if resultado == 'creada':
            creadas += 1
    db.close()

    for k, v in sorted(stats.items()):
        log.info(f"  {k:<32} {v:>6}")
    if stats['error'] or stats['timbrado_fallido'] or stats['turno_MES_ANTERIOR_FUERA_DE_CIERRE']:
        notify_slack(f"Walmart 04 (push): creadas={stats['creada']} errores={stats['error']} "
                     f"timbrado_fallido={stats['timbrado_fallido']} "
                     f"mes_anterior_fuera_de_cierre={stats['turno_MES_ANTERIOR_FUERA_DE_CIERRE']}")


if __name__ == '__main__':
    configurar_log()
    main()
