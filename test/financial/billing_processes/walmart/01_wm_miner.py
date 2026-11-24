"""
01_wm_miner.py
--------------
Decide, orden por orden, QUIEN factura cada venta de Walmart 3P y la deja en
finance.mkp_billing_{entorno} con marketplace='WALMART'. Solo lee MySQL; no
toca Odoo.

Fuentes, ninguna sola (CLAUDE.md regla 6):
  * Que se vendio       FTM type='Order' (una fila por linea de orden).
  * Pidio factura?      API de Fenix (filas PRECFDI de 00b): la linea tiene
                        una peticion de factura RECIBIDA, CON_ERROR o NUEVA.
  * Con que CFDI        CFDI de Walmart del repositorio ya ligados a la linea
                        (CFDI:<UUID>, XML del SAT, 02) y, si WM_USAR_CRAWLER,
                        el crawler (wm_fenix_invoices_report). Un CFDI por
                        pieza (cantidad=1 por UUID).
  * Hay SO              odoo_new_sale_order_live por channel_order_reference
                        (= customer_order_id) y por FTM.so.

Clasificacion de cada LINEA (iderp = so_prime_line_sub_line_no):
  piezas con CFDI vigente de Walmart (CON_XML_PDF, tipo I) = q_wm
    q_wm == cantidad vendida                   -> WM   (la timbro Walmart)
    q_wm < cantidad y peticion RECIBIDA        -> FALTA_UUID (Walmart timbro;
                                                  falta ligar su CFDI)
    q_wm == 0 y peticion CON_ERROR/NUEVA/BLOQUEADA,
      o el crawler en ERROR, NUEVO, CANCELADO  -> REVISAR (el cliente pidio factura)
    q_wm == 0 y nadie dice nada                -> WB   (la timbramos nosotros)
    0 < q_wm < cantidad sin peticion           -> MIXTA
    q_wm > cantidad                            -> EXCEDENTE (Walmart emitio de mas)
  REEMPLAZADO y ERROR junto a un CFDI vigente se ignoran: en los datos los 51
  REEMPLAZADO tienen su sustituto vigente en la misma linea.

Clasificacion de la ORDEN:
  todas WM        -> una fila por UUID: <orden>:<UUID>  PENDING_WM_XML / _LINK
  WM + FALTA_UUID -> una fila: <orden>  PENDING_WM_UUID (02 baja y liga el XML)
  todas WB        -> una fila: <orden>  PENDING_WB_INVOICE
  si no           -> una fila: <orden> en espera (ORDEN_MIXTA, WM_CFDI_REVISAR...)
  Antes que todo, la SO: NO_SO, SO_DUPLICADA, SO_OTRO_EQUIPO, SO_CANCELADA.

Las filas en estado final (facturadas, TOTAL_DIFF...) no se tocan. Las que
siguen abiertas se reescriben en cada corrida; si la orden cambio de
clasificacion, las que sobran pasan a DESCARTADA.

Variables:
  WM_START_DATE  AAAA-MM-DD. Vacio = dia 1 del mes anterior (CDMX).
  WM_END_DATE    AAAA-MM-DD exclusivo. Vacio = sin tope.
  WM_DRY_RUN=1   no escribe: solo reporta lo que haria.
"""

import os
import sys
from collections import Counter, defaultdict
from datetime import timedelta

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, get_db_connection, notify_slack,
                        TBL_BILLING, MARKETPLACE, TEAM_WALMART, REFRESCABLES,
                        ST_PENDING_WB, ST_PENDING_WM_UUID, ST_PENDING_WM_XML, ST_PENDING_WM_LINK,
                        ST_NO_SO, ST_SO_DUPLICADA, ST_SO_OTRO_EQUIPO, ST_SO_CANCELADA,
                        ST_ORDEN_MIXTA, ST_WM_REVISAR, ST_WM_EXCEDENTE, ST_DESCARTADA,
                        ST_MANUAL_SERIE, SKUS_FACTURA_MANUAL, USAR_CRAWLER, PRE_RECIBIDO,
                        precfdi_por_linea, cfdis_ligados_por_linea,
                        inicio_de_mes, a_float, a_json, normalizar_uuid, lotes)

DRY_RUN = os.getenv('WM_DRY_RUN', '0').strip() in ('1', 'true', 'TRUE')

WM_START_DATE = os.getenv('WM_START_DATE', '').strip()
if not WM_START_DATE:
    WM_START_DATE = f"{inicio_de_mes() - timedelta(days=1):%Y-%m}-01"
WM_END_DATE = os.getenv('WM_END_DATE', '').strip() or None

ESTADO_VIGENTE = 'CON_XML_PDF'


# =============================================================================
# LECTURAS
# =============================================================================

def leer_ftm(cur):
    sql = """
        SELECT customer_order_id, purchase_order_id, so_prime_line_sub_line_no AS iderp,
               so, sku, quantity, price_without_tax_amount, tax_amount, price_amount,
               fulfillment_type, order_status, order_date
        FROM somos_reyes.wm_financial_transactions_master
        WHERE type = 'Order' AND order_date >= %s
    """
    params = [WM_START_DATE]
    if WM_END_DATE:
        sql += " AND order_date < %s"
        params.append(WM_END_DATE)
    cur.execute(sql, tuple(params))
    ordenes = defaultdict(list)
    for r in cur.fetchall():
        ordenes[r['customer_order_id']].append(r)
    return ordenes


def leer_fenix(cur, pedidos):
    """CFDI de Walmart por iderp: los del repositorio ya ligados (XML del SAT)
    mas, si WM_USAR_CRAWLER, los del crawler. Sin duplicar UUID. Solo
    comprobantes de ingreso (las NC de Walmart, tipo E, van al flujo de
    devoluciones)."""
    por_linea = defaultdict(list)
    for iderp, filas in cfdis_ligados_por_linea(cur).items():
        por_linea[iderp].extend(filas)
    if not USAR_CRAWLER:
        return por_linea
    vistos = {f['uuid'] for fs in por_linea.values() for f in fs}
    for chunk in lotes(pedidos, 500):
        cur.execute(f"""
            SELECT UPPER(uuid) AS uuid, tipo_documento, pedido, iderp, cantidad, subtotal,
                   descuento, total_factura, estado_cfdi, rfc_receptor, fecha_creacion
            FROM somos_reyes.wm_fenix_invoices_report
            WHERE pedido IN ({','.join(['%s'] * len(chunk))}) AND tipo_documento = 'I'
        """, tuple(chunk))
        for r in cur.fetchall():
            if r['uuid'] and normalizar_uuid(r['uuid']) in vistos:
                continue
            r['origen'] = 'crawler'
            por_linea[r['iderp']].append(r)
    return por_linea


def leer_sos(cur, pedidos, so_names):
    """SO de la replica por referencia del marketplace y por nombre."""
    por_ref, por_nombre = defaultdict(list), {}
    for chunk in lotes(pedidos, 500):
        cur.execute(f"""
            SELECT name, state, team_name, invoice_status, channel_order_reference
            FROM somos_reyes.odoo_new_sale_order_live
            WHERE channel_order_reference IN ({','.join(['%s'] * len(chunk))})
        """, tuple(chunk))
        for r in cur.fetchall():
            por_ref[r['channel_order_reference']].append(r)
            por_nombre[r['name']] = r
    faltan = [n for n in so_names if n and n not in por_nombre]
    for chunk in lotes(faltan, 500):
        cur.execute(f"""
            SELECT name, state, team_name, invoice_status, channel_order_reference
            FROM somos_reyes.odoo_new_sale_order_live
            WHERE name IN ({','.join(['%s'] * len(chunk))})
        """, tuple(chunk))
        for r in cur.fetchall():
            por_nombre[r['name']] = r
    return por_ref, por_nombre


def leer_filas_existentes(cur):
    cur.execute(f"""SELECT id, mkp_order_id, status FROM {TBL_BILLING}
                    WHERE marketplace = %s""", (MARKETPLACE,))
    por_orden = defaultdict(list)
    for r in cur.fetchall():
        por_orden[r['mkp_order_id'].split(':', 1)[0]].append(r)
    return por_orden


# =============================================================================
# CLASIFICACION
# =============================================================================

def resolver_so(pedido, lineas, por_ref, por_nombre):
    """Devuelve (so, estado_espera). so es un dict de la replica o un dict
    minimo con el nombre de FTM cuando la replica aun no la trae (el loader la
    valida en Odoo)."""
    candidatas = {r['name']: r for r in por_ref.get(pedido, [])}
    for l in lineas:
        if l['so'] and l['so'] in por_nombre:
            candidatas[l['so']] = por_nombre[l['so']]
    if not candidatas:
        nombre_ftm = next((l['so'] for l in lineas if l['so']), None)
        if nombre_ftm:
            return {'name': nombre_ftm, 'state': None, 'team_name': None}, None
        return None, ST_NO_SO

    vivas = [s for s in candidatas.values() if s['state'] != 'cancel']
    if len(vivas) > 1:
        return {'name': ','.join(sorted(s['name'] for s in vivas)), 'state': None,
                'team_name': None}, ST_SO_DUPLICADA
    if not vivas:
        so = sorted(candidatas.values(), key=lambda s: s['name'])[-1]
        return so, ST_SO_CANCELADA
    so = vivas[0]
    if so['team_name'] != TEAM_WALMART:
        return so, ST_SO_OTRO_EQUIPO
    return so, None


def clasificar_linea(linea, filas_fenix, peticiones):
    """peticiones = estados de la API de Fenix para la linea (RECIBIDO, CON_ERROR...)."""
    vigentes = [f for f in filas_fenix if f['estado_cfdi'] == ESTADO_VIGENTE and f['uuid']]
    q_wm = sum(int(f['cantidad'] or 0) for f in vigentes)
    qty = int(linea['quantity'] or 0)
    otros = sorted({f['estado_cfdi'] for f in filas_fenix if f['estado_cfdi'] != ESTADO_VIGENTE}
                   | {f"API:{p}" for p in peticiones if p != PRE_RECIBIDO})
    if q_wm > qty:
        return 'EXCEDENTE', vigentes
    if q_wm == qty and qty > 0:
        return 'WM', vigentes
    if PRE_RECIBIDO in peticiones:
        return 'FALTA_UUID', vigentes
    if q_wm == 0:
        return ('REVISAR', otros) if otros else ('WB', [])
    return 'MIXTA', vigentes


def linea_para_json(l):
    return {k: l[k] for k in ('iderp', 'sku', 'quantity', 'price_without_tax_amount',
                              'tax_amount', 'price_amount', 'fulfillment_type',
                              'order_status', 'order_date', 'purchase_order_id')}


def filas_deseadas(pedido, lineas, fenix, peticiones, por_ref, por_nombre):
    """Lista de filas (dict) que la tabla de control debe tener para la orden."""
    so, espera = resolver_so(pedido, lineas, por_ref, por_nombre)
    so_name = so['name'] if so else None
    total = round(sum(a_float(l['price_amount']) for l in lineas), 2)
    base_json = {
        'customer_order_id': pedido,
        'purchase_order_id': lineas[0]['purchase_order_id'],
        'order_date': lineas[0]['order_date'],
        'so': so,
        'lineas': [linea_para_json(l) for l in lineas],
    }

    def fila_orden(status, motivo=None, tipo='WB'):
        return {'mkp_order_id': pedido, 'odoo_so_name': so_name, 'cfdi_uuid': None,
                'total_amount': total, 'status': status, 'error_log': motivo,
                'raw_json': dict(base_json, tipo=tipo,
                                 fenix={l['iderp']: fenix.get(l['iderp'], []) for l in lineas},
                                 peticiones_fenix={l['iderp']: sorted(peticiones.get(l['iderp'], set()))
                                                   for l in lineas})}

    if espera:
        return [fila_orden(espera, f"SO: {espera}", tipo='ESPERA')]

    clases = {}
    for l in lineas:
        clases[l['iderp']] = clasificar_linea(l, fenix.get(l['iderp'], []), peticiones.get(l['iderp'], set()))
    tipos = {c[0] for c in clases.values()}

    if 'FALTA_UUID' in tipos and tipos <= {'WM', 'FALTA_UUID'}:
        faltan = [k for k, v in clases.items() if v[0] == 'FALTA_UUID']
        return [fila_orden(ST_PENDING_WM_UUID, f"Walmart facturo; falta ligar el CFDI de: {', '.join(faltan)}"[:900],
                           tipo='WM')]

    if tipos == {'WB'}:
        manuales = sorted({l['sku'] for l in lineas
                           if (l['sku'] or '').upper().startswith(SKUS_FACTURA_MANUAL)})
        if manuales:
            return [fila_orden(ST_MANUAL_SERIE, f"Bici electrica ({', '.join(manuales)}): la factura "
                                                f"CS/Finanzas con numero de serie", tipo='ESPERA')]
        return [fila_orden(ST_PENDING_WB)]

    if tipos == {'WM'}:
        filas = []
        for l in lineas:
            precio_pieza = a_float(l['price_amount']) / max(int(l['quantity'] or 1), 1)
            for f in clases[l['iderp']][1]:
                uuid = normalizar_uuid(f['uuid'])
                filas.append({
                    'mkp_order_id': f"{pedido}:{uuid}", 'odoo_so_name': so_name,
                    'cfdi_uuid': uuid, 'total_amount': round(a_float(f['total_factura']), 2),
                    'status': ST_PENDING_WM_XML, 'error_log': None,
                    'raw_json': dict(base_json, tipo='WM', linea=linea_para_json(l), fenix=f,
                                     dif_vs_walmart=round(a_float(f['total_factura'])
                                                          - precio_pieza * int(f['cantidad'] or 1), 2)),
                })
        return filas

    if 'EXCEDENTE' in tipos:
        status = ST_WM_EXCEDENTE
    elif 'REVISAR' in tipos:
        status = ST_WM_REVISAR
    else:
        status = ST_ORDEN_MIXTA
    detalle = '; '.join(f"{k}={v[0]}" + (f"({','.join(v[1])})" if v[0] == 'REVISAR' else '')
                        for k, v in clases.items())
    if 'FALTA_UUID' in tipos and status == ST_ORDEN_MIXTA:
        detalle = 'Hay lineas facturadas por Walmart (falta ligar su CFDI) y lineas sin peticion: ' + detalle
    return [fila_orden(status, detalle[:900], tipo='ESPERA')]


# =============================================================================
# ESCRITURA
# =============================================================================

def sql_upsert():
    r = ', '.join(f"'{s}'" for s in REFRESCABLES)
    cond = f"status IN ({r})"
    return f"""
        INSERT INTO {TBL_BILLING}
            (marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, raw_json,
             status, error_log, retry_count, retry_count_loader)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
        ON DUPLICATE KEY UPDATE
            odoo_so_name = IF({cond}, VALUES(odoo_so_name), odoo_so_name),
            cfdi_uuid    = IF({cond}, VALUES(cfdi_uuid), cfdi_uuid),
            total_amount = IF({cond}, VALUES(total_amount), total_amount),
            raw_json     = IF({cond}, VALUES(raw_json), raw_json),
            error_log    = IF({cond}, VALUES(error_log), error_log),
            status       = IF({cond},
                              IF(VALUES(status) = '{ST_PENDING_WM_XML}' AND xml_data IS NOT NULL,
                                 '{ST_PENDING_WM_LINK}', VALUES(status)),
                              status)
    """
    # status va AL FINAL: MySQL evalua las asignaciones en orden y las de
    # arriba tienen que ver el status anterior.


def main():
    banner(f"01 - MINER WALMART | ventana {WM_START_DATE} -> {WM_END_DATE or 'hoy'}"
           f"{' | DRY RUN' if DRY_RUN else ''}")

    db = get_db_connection()
    cur = db.cursor(dictionary=True)

    ordenes = leer_ftm(cur)
    pedidos = list(ordenes.keys())
    log.info(f"FTM: {sum(len(v) for v in ordenes.values())} lineas en {len(pedidos)} ordenes.")
    if not pedidos:
        log.info('Sin ordenes en la ventana.')
        return

    fenix = leer_fenix(cur, pedidos)
    peticiones = precfdi_por_linea(cur)
    so_names = sorted({l['so'] for v in ordenes.values() for l in v if l['so']})
    por_ref, por_nombre = leer_sos(cur, pedidos, so_names)
    existentes = leer_filas_existentes(cur)
    origenes = Counter(f.get('origen') for v in fenix.values() for f in v)
    log.info(f"CFDI de Walmart por linea: {dict(origenes)} (crawler {'SI' if USAR_CRAWLER else 'NO'}) | "
             f"lineas con peticion en la API de Fenix: {len(peticiones)} | "
             f"SO en replica: {len(por_nombre)} | filas de control previas: "
             f"{sum(len(v) for v in existentes.values())}")
    if not peticiones:
        log.warning('No hay filas de la API de Fenix (00b no ha corrido): la clasificacion depende solo del '
                    'crawler. 04 no emitira sin la marca del ETL.')

    deseadas = []
    for pedido in pedidos:
        deseadas.extend(filas_deseadas(pedido, ordenes[pedido], fenix, peticiones, por_ref, por_nombre))

    # Filas abiertas de estas ordenes que ya no corresponden.
    claves = {f['mkp_order_id'] for f in deseadas}
    descartar = [r for p in pedidos for r in existentes.get(p, [])
                 if r['mkp_order_id'] not in claves and r['status'] in REFRESCABLES]
    cerradas_conflicto = [r for p in pedidos for r in existentes.get(p, [])
                          if r['mkp_order_id'] not in claves and r['status'] not in REFRESCABLES
                          and r['status'] != ST_DESCARTADA]

    resumen = Counter(f['status'] for f in deseadas)
    for status, n in sorted(resumen.items()):
        log.info(f"  {status:<22} {n:>6}")
    log.info(f"  {'(a DESCARTADA)':<22} {len(descartar):>6}")
    if cerradas_conflicto:
        log.warning(f"{len(cerradas_conflicto)} fila(s) cerradas ya no coinciden con la "
                    f"clasificacion actual (p. ej. factura de WB y ahora CFDI de Walmart). "
                    f"03 las detecta como DOBLE_CFDI. Ej.: "
                    f"{[r['mkp_order_id'] for r in cerradas_conflicto[:5]]}")

    if DRY_RUN:
        log.info('DRY RUN: no se escribio nada.')
        return

    wcur = db.cursor()
    sql = sql_upsert()
    # Por bloques de 200 con commit: una corrida cortada a la mitad deja lo ya
    # escrito (cada fila es idempotente por la clave unica).
    for bloque in lotes(deseadas, 200):
        wcur.executemany(sql, [(MARKETPLACE, f['mkp_order_id'], f['odoo_so_name'], f['cfdi_uuid'],
                                f['total_amount'], a_json(f['raw_json']), f['status'], f['error_log'])
                               for f in bloque])
        db.commit()
    for r in descartar:
        wcur.execute(f"""UPDATE {TBL_BILLING} SET status = %s, processed_at = NOW(),
                         error_log = 'La orden cambio de clasificacion en el miner.'
                         WHERE id = %s AND status = %s""", (ST_DESCARTADA, r['id'], r['status']))
    db.commit()
    wcur.close()
    cur.close()
    db.close()
    log.info(f"Escritas {len(deseadas)} fila(s); {len(descartar)} descartada(s).")

    normales = (ST_PENDING_WB, ST_PENDING_WM_UUID, ST_PENDING_WM_XML, ST_PENDING_WM_LINK)
    espera = sum(n for s, n in resumen.items() if s not in normales)
    if espera:
        notify_slack(f"Walmart miner: {espera} orden(es) en espera de revision "
                     f"({', '.join(f'{s}={n}' for s, n in resumen.items() if s not in normales)}).")


if __name__ == '__main__':
    configurar_log()
    main()
