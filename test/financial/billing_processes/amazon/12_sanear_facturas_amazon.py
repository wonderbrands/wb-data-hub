"""
12_sanear_facturas_amazon.py
----------------------------
Saneamiento UNICO de las B2C de Amazon facturadas por 01 desde el arranque en
produccion (30-oct-2026) hasta la correccion del 2-nov-2026.

Dos casos (instruccion de Sergio, 2-nov-2026):

  A. SIN_TIMBRE   publicadas en Odoo y sin UUID. El PAC las rechazo
                  (CFDI40119, SO10362958): 01 editaba la linea de IVA un
                  centavo para igualar el OrderTotal y Odoo arma el XML con el
                  IVA recalculado por linea. Nunca existieron ante el SAT, asi
                  que NO se cancelan: se pasan a borrador, se quita el centavo
                  de la linea de IVA, se les pone la fecha del mes de la venta
                  y se vuelven a publicar y timbrar. El centavo lo absorbe 03
                  al aplicar el cobro (704.23.001).

  B. FECHA_MES    timbradas con fecha de noviembre siendo venta de octubre.
                  Se cancelan ante el SAT con motivo 02 ("Comprobantes
                  emitidos con errores sin relacion": button_request_cancel +
                  l10n_mx_edi_cfdi_try_sat) y, ya canceladas en Odoo, se
                  vuelven a crear con procesar() de 01 (mismo camino, mismos
                  blindajes) con fecha del ultimo dia de octubre.

En los dos casos lo que ya estaba conciliado con la factura (cobros de 03,
promociones, redondeos) se vuelve a conciliar contra la factura corregida, y
la 205.06.003 con conciliar_salida() de 01.

EL CASO B VA EN DOS PASADAS. El SAT no confirma la cancelacion al instante
(SO10364343: try_sat justo despues de solicitar devolvia cancel_requested).
Pasada 1: se solicitan todas las cancelaciones. Pasada 2: se consulta el SAT
por todas, en rondas con una espera entre ronda y ronda, y se refactura cada
una que Odoo ya tenga cancelada. Lo que quede sin confirmar se termina
volviendo a correr el script (la fila queda en SANEO_CANCELACION).

LIMITE DE 72 HORAS: el SAT no timbra un CFDI con fecha de mas de 72 horas
antes del timbrado. La fecha del 31-oct solo se puede timbrar hasta el 3-nov:
despues de eso el caso B no solicita cancelaciones. Una factura nueva que no
se timbre queda publicada sin UUID y la corrige el caso A en la siguiente
corrida. Primero --limite 1 para ver una completa.

ANTES DE APLICAR: el flujo de Kestra de Amazon apagado y sin ejecucion en
curso. Este script corre fuera de Kestra y la exclusion mutua no lo cubre.

Uso (AMZ_ENTORNO=prod y las credenciales de produccion en el entorno):
    python 12_sanear_facturas_amazon.py                      # en seco: lista y CSV
    python 12_sanear_facturas_amazon.py --aplicar --limite 1 # una factura
    python 12_sanear_facturas_amazon.py --aplicar            # todas
    python 12_sanear_facturas_amazon.py --aplicar --solo A   # solo un caso
"""

import os
import sys
import csv
import json
import logging
import time as tm
import importlib.util
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    TBL_BILLING, MARKETPLACE, IDEMPOTENCY_PREFIX, PARTNER_ID_PUBLICO_GENERAL,
    ACCOUNT_AMAZON_CXC, ACCOUNT_MERC_NO_ENVIADA,
    banner, get_db_connection, validar_entorno, OdooModelProxy, get_account_id,
    get_amazon_team_ids, ejecutar_sin_retorno, a_float,
)

APLICAR = '--aplicar' in sys.argv
LIMITE = int(sys.argv[sys.argv.index('--limite') + 1]) if '--limite' in sys.argv else None
SOLO = sys.argv[sys.argv.index('--solo') + 1].upper() if '--solo' in sys.argv else None

# Primera corrida de 01 en produccion: 30-oct-2026 18:41 UTC. Antes de eso las
# B2C de Amazon las hacia el 1a1 y no se tocan.
DESDE = os.getenv('AMZ_SANEO_DESDE', '2026-10-30 00:00:00')
MOTIVO_SAT = '02'
# Estado de la tabla de control mientras el SAT confirma la cancelacion. Ni el
# miner ni el loader de 01 lo tocan (no es refrescable ni pendiente).
EN_CANCELACION = 'SANEO_CANCELACION'
# Consultas al SAT de la pasada 2 del caso B y espera entre una y otra.
RONDAS_SAT = int(os.getenv('AMZ_SANEO_RONDAS_SAT', '4'))
ESPERA_SAT = int(os.getenv('AMZ_SANEO_ESPERA_SAT', '30'))
CSV_SALIDA = os.path.join(_dir, f"saneamiento_amazon_{datetime.now():%Y%m%d_%H%M}.csv")


def cargar_01():
    spec = importlib.util.spec_from_file_location(
        'amz_billing_mod', os.path.join(_dir, '01_amz_billing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# LECTURAS
# =============================================================================

def campos_mx(odoo):
    """Campos del CFDI que existen en esta base (se leen solo los que hay)."""
    f = odoo.execute_kw('account.move', 'fields_get', [], {'attributes': ['type']})
    return [c for c in ('l10n_mx_edi_cfdi_uuid', 'l10n_mx_edi_cfdi_state',
                        'l10n_mx_edi_cfdi_sat_state') if c in f]


def facturas_del_periodo(odoo, teams, extra):
    domain = [('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
              ('ref', '=like', f'{IDEMPOTENCY_PREFIX}%'),
              ('partner_id', '=', PARTNER_ID_PUBLICO_GENERAL),
              ('team_id', 'in', teams), ('create_date', '>=', DESDE)]
    return odoo.execute_kw('account.move', 'search_read', [domain],
                           {'fields': ['id', 'name', 'ref', 'invoice_origin',
                                       'invoice_date', 'amount_tax',
                                       'amount_total'] + extra,
                            'order': 'id asc'})


def iva_esperado(odoo, inv_id):
    """(IVA de las lineas, IVA de la linea de impuesto, ids de lineas de IVA).

    price_total - price_subtotal es el IVA que Odoo calcula por linea y es lo
    que viaja al XML. Si la linea de impuesto no suma eso, fue editada a mano.
    """
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', inv_id), ('display_type', '=', 'product')]],
                             {'fields': ['price_subtotal', 'price_total']})
    iva = odoo.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('display_type', '=', 'tax')]],
                          {'fields': ['id', 'balance']})
    esperado = round(sum(a_float(l['price_total']) - a_float(l['price_subtotal'])
                         for l in lineas), 2)
    actual = round(-sum(a_float(l['balance']) for l in iva), 2)
    return esperado, actual, iva


def contrapartidas(odoo, inv_id):
    """Partidas conciliadas con la CxC de la factura (cobros, promociones,
    redondeos), para volver a conciliarlas con la factura corregida."""
    cxc = odoo.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('account_type', '=', 'asset_receivable')]],
                          {'fields': ['id', 'matched_debit_ids', 'matched_credit_ids']})
    propias = {l['id'] for l in cxc}
    parciales = [p for l in cxc for p in l['matched_debit_ids'] + l['matched_credit_ids']]
    if not parciales:
        return []
    filas = odoo.execute_kw('account.partial.reconcile', 'read', [parciales],
                            {'fields': ['debit_move_id', 'credit_move_id']})
    ids = set()
    for p in filas:
        for lado in (p['debit_move_id'], p['credit_move_id']):
            if lado and lado[0] not in propias:
                ids.add(lado[0])
    return sorted(ids)


def reconciliar(odoo, inv_id, otras, etiqueta):
    if not otras:
        return 'sin contrapartidas'
    cxc = odoo.execute_kw('account.move.line', 'search',
                          [[('move_id', '=', inv_id), ('account_type', '=', 'asset_receivable')]])
    libres = odoo.execute_kw('account.move.line', 'search',
                             [[('id', 'in', otras), ('reconciled', '=', False)]])
    if not libres:
        return 'contrapartidas ya conciliadas con otra cosa: revisar'
    try:
        ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', cxc + libres)
    except Exception as e:
        log.warning(f"{etiqueta}: no se pudieron reconciliar {libres}: {e}")
        return f'RECONCILIAR A MANO lineas {libres}: {str(e)[:150]}'
    estado = odoo.execute_kw('account.move', 'read', [[inv_id]],
                             {'fields': ['payment_state', 'amount_residual']})[0]
    return f"reconciliado {len(libres)} partida(s) -> {estado['payment_state']} residuo {estado['amount_residual']:.2f}"


def fila_control(db, so_name):
    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                           raw_json, status, retry_count_loader, error_log
                    FROM {TBL_BILLING}
                    WHERE marketplace = %s AND odoo_so_name = %s
                    ORDER BY id DESC LIMIT 1""", (MARKETPLACE, so_name))
    fila = cur.fetchone()
    cur.close()
    return fila


def anotar(db, fila_id, nota, uuid=None, facturas=None):
    """Antepone la nota del saneamiento en error_log; con uuid lo guarda."""
    cur = db.cursor()
    sql_f, extra = '', []
    if facturas is not None:
        sql_f = (", raw_json = JSON_SET(COALESCE(raw_json, JSON_OBJECT()), "
                 "'$.facturas_odoo', CAST(%s AS JSON))")
        extra = [json.dumps(facturas, ensure_ascii=False, default=str)]
    cur.execute(f"""UPDATE {TBL_BILLING}
                    SET error_log = CONCAT_WS(' | ', %s, error_log),
                        cfdi_uuid = COALESCE(%s, cfdi_uuid) {sql_f}
                    WHERE id = %s""", tuple([nota, uuid] + extra + [fila_id]))
    db.commit()
    cur.close()


# =============================================================================
# CASO A -- PUBLICADA SIN TIMBRE
# =============================================================================

def sanear_sin_timbre(odoo, m, db, f, so, cuenta_cxc, cuenta_205):
    etiqueta = f"{f['invoice_origin']} {f['name']}"
    otras = contrapartidas(odoo, f['id'])

    ejecutar_sin_retorno(odoo, 'account.move', 'button_draft', [f['id']])
    if odoo.execute_kw('account.move', 'read', [[f['id']]], {'fields': ['state']})[0]['state'] != 'draft':
        return 'ERROR: Odoo no la paso a borrador'

    # Fecha primero: cambiarla puede recalcular lineas, y el IVA se corrige despues.
    objetivo = m.fecha_factura(so)
    if objetivo and str(f['invoice_date']) != objetivo:
        odoo.execute_kw('account.move', 'write',
                        [[f['id']], {'invoice_date': objetivo, 'date': objetivo}])

    esperado, actual, iva = iva_esperado(odoo, f['id'])
    if esperado != actual:
        if len(iva) != 1:
            return f'ERROR: {len(iva)} lineas de IVA; se esperaba 1. Queda en borrador.'
        odoo.execute_kw('account.move', 'write',
                        [[f['id']], {'line_ids': [(1, iva[0]['id'], {
                            'amount_currency': -esperado, 'balance': -esperado})]}],
                        {'context': {'tracking_disable': True}})
        # Editar el IVA rebalancea la CxC: que siga en la cuenta de Amazon.
        fuera = odoo.execute_kw('account.move.line', 'search_read',
                                [[('move_id', '=', f['id']), ('account_type', '=', 'asset_receivable'),
                                  ('account_id', '!=', cuenta_cxc)]], {'fields': ['id']})
        if fuera:
            odoo.execute_kw('account.move', 'write',
                            [[f['id']], {'line_ids': [(1, l['id'], {'account_id': cuenta_cxc})
                                                      for l in fuera]}],
                            {'context': {'tracking_disable': True}})
    esperado, actual, _ = iva_esperado(odoo, f['id'])
    if esperado != actual:
        return f'ERROR: el IVA quedo en {actual:.2f} y debe ser {esperado:.2f}. Queda en borrador.'

    odoo.execute_kw('account.move', 'action_post', [[f['id']]])
    uuid, error = m.timbrar(odoo, f['id'], etiqueta)
    nueva = odoo.execute_kw('account.move', 'read', [[f['id']]],
                            {'fields': ['invoice_date', 'amount_tax', 'amount_total']})[0]
    conc = reconciliar(odoo, f['id'], otras, etiqueta)
    m.conciliar_salida(odoo, so, f['id'], cuenta_205)

    fila = fila_control(db, f['invoice_origin'])
    if fila:
        principal = f['ref'] == f"{IDEMPOTENCY_PREFIX}{f['invoice_origin']}"
        nota = (f"SANEADA {datetime.now():%Y-%m-%d} {f['name']}: "
                f"{'timbrada' if uuid else 'TIMBRADO_FALLIDO ' + (error or '')[:200]}, "
                f"IVA {actual:.2f}, fecha {nueva['invoice_date']}")
        anotar(db, fila['id'], nota, uuid=uuid if principal else None,
               facturas=[{'factura': f['name'], 'uuid': uuid, 'fecha': nueva['invoice_date'],
                          'timbre': 'OK' if uuid else 'FALLIDO',
                          'total': nueva['amount_total']}] if principal else None)
    if not uuid:
        return f"TIMBRADO_FALLIDO: {(error or '')[:200]} | {conc}"
    return (f"OK timbrada {uuid} | fecha {nueva['invoice_date']} | IVA "
            f"{nueva['amount_tax']:.2f} total {nueva['amount_total']:.2f} | {conc}")


# =============================================================================
# CASO B -- TIMBRADA CON FECHA DEL MES EQUIVOCADO
# =============================================================================

def solicitar_cancelacion(odoo, inv_id):
    """button_request_cancel abre el asistente de cancelacion del CFDI. Se crea
    con su mismo contexto, motivo 02, y se confirma. Si el asistente no tiene
    el campo o el motivo esperado, falla antes de tocar nada."""
    try:
        accion = odoo.execute_kw('account.move', 'button_request_cancel', [[inv_id]])
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            raise
        accion = None
    if not isinstance(accion, dict) or not accion.get('res_model'):
        # Sin asistente Odoo ya mando la solicitud por su cuenta (sin elegir
        # motivo). No se revierte nada: lo confirma l10n_mx_edi_cfdi_try_sat.
        log.warning(f"button_request_cancel de {inv_id} no abrio asistente ({accion!r}); "
                    f"revisar en Odoo el motivo de la cancelacion.")
        return
    modelo, ctx = accion['res_model'], accion.get('context') or {}
    campos = odoo.execute_kw(modelo, 'fields_get', [], {'attributes': ['type', 'selection']})
    if 'cancellation_reason' not in campos:
        raise Exception(f"{modelo} no tiene cancellation_reason: {sorted(campos)}")
    claves = [s[0] for s in campos['cancellation_reason'].get('selection') or []]
    if MOTIVO_SAT not in claves:
        raise Exception(f"Motivo {MOTIVO_SAT} no esta en {claves}")
    wiz = odoo.execute_kw(modelo, 'create', [{'cancellation_reason': MOTIVO_SAT}],
                          {'context': ctx})
    for metodo in ('action_cancel_invoice', 'action_cancel'):
        try:
            odoo.execute_kw(modelo, metodo, [[wiz]], {'context': ctx})
            return
        except Exception as e:
            if 'cannot marshal None' in str(e):
                return
            if 'has no attribute' in str(e) or 'does not exist' in str(e):
                continue
            raise
    raise Exception(f"{modelo}: no se encontro el metodo para confirmar la cancelacion")


def estado_cancelacion(odoo, inv_id, extra):
    ejecutar_sin_retorno(odoo, 'account.move', 'l10n_mx_edi_cfdi_try_sat', [inv_id])
    return odoo.execute_kw('account.move', 'read', [[inv_id]],
                           {'fields': ['state', 'name'] + extra})[0]


def cancelar(odoo, db, f, fila, extra):
    """Paso 1 del caso B. Deja la fila en EN_CANCELACION con lo necesario para
    reanudar (la factura vieja y sus contrapartidas) en error_log."""
    otras = contrapartidas(odoo, f['id'])
    pendiente = {'factura': f['name'], 'inv_id': f['id'], 'ref': f['ref'],
                 'contrapartidas': otras, 'status_anterior': fila['status']}
    cur = db.cursor()
    cur.execute(f"""UPDATE {TBL_BILLING} SET status = %s, error_log = %s
                    WHERE id = %s AND status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')""",
                (EN_CANCELACION, 'SANEO ' + json.dumps(pendiente), fila['id']))
    db.commit()
    ganada = cur.rowcount > 0
    cur.close()
    if not ganada:
        return None, f"la fila esta en {fila['status']}; no se toca"
    try:
        solicitar_cancelacion(odoo, f['id'])
    except Exception as e:
        # Nada se cancelo: la fila vuelve a su estado.
        cur = db.cursor()
        cur.execute(f"UPDATE {TBL_BILLING} SET status = %s, error_log = %s WHERE id = %s",
                    (fila['status'], fila['error_log'], fila['id']))
        db.commit()
        cur.close()
        return None, f"ERROR al solicitar la cancelacion: {str(e)[:300]}"
    return pendiente, 'cancelacion solicitada (motivo 02)'


def refacturar(odoo, m, db, fila_id, pendiente, contexto, extra):
    """Paso 2 del caso B: solo si Odoo ya tiene la factura cancelada."""
    estado = estado_cancelacion(odoo, pendiente['inv_id'], extra)
    if estado['state'] != 'cancel':
        return False, (f"pendiente del SAT: state={estado['state']} "
                       f"{ {k: estado.get(k) for k in extra} }")

    cur = db.cursor()
    cur.execute(f"UPDATE {TBL_BILLING} SET status = 'PROCESSING', processed_at = NOW() "
                f"WHERE id = %s AND status = %s", (fila_id, EN_CANCELACION))
    db.commit()
    ganada = cur.rowcount > 0
    cur.close()
    if not ganada:
        return False, 'otra ejecucion tomo la fila'

    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
                           raw_json, status, retry_count_loader
                    FROM {TBL_BILLING} WHERE id = %s""", (fila_id,))
    record = cur.fetchone()
    cur.close()
    record['status'] = 'PENDING_WB_INVOICE'

    # Si el cliente autofacturo mientras tanto, va a B2B y no se refactura.
    if m.detectar_b2b_tardio(db, [record]):
        return True, (f"reclasificada a B2B: 01 vincula el CFDI de Amazon; "
                      f"contrapartidas {pendiente['contrapartidas']} por conciliar a mano")

    stats = m.nuevos_stats()
    m.procesar(odoo, db, record, contexto['cxc'], stats, contexto['catalogos'], {})
    nuevas = m.buscar_por_ref(odoo, pendiente['ref'])
    if not nuevas:
        return True, (f"ERROR: procesar() no creo la factura nueva; ver la tabla "
                      f"de control. Contrapartidas {pendiente['contrapartidas']}")
    nueva = odoo.execute_kw('account.move', 'read', [[nuevas[0]['id']]],
                            {'fields': ['name', 'invoice_date', 'amount_total',
                                        'l10n_mx_edi_cfdi_uuid']})[0]
    conc = reconciliar(odoo, nueva['id'], pendiente['contrapartidas'], nueva['name'])
    timbrada = bool(nueva['l10n_mx_edi_cfdi_uuid'])
    fila = fila_control(db, record['odoo_so_name'])
    anotar(db, fila['id'], f"SANEADA {datetime.now():%Y-%m-%d}: {pendiente['factura']} "
                           f"cancelada motivo 02 -> {nueva['name']} del {nueva['invoice_date']}")
    resultado = (f"{pendiente['factura']} cancelada -> {nueva['name']} fecha "
                 f"{nueva['invoice_date']} total {nueva['amount_total']:.2f} "
                 f"{'timbrada' if timbrada else 'SIN TIMBRAR'} | {conc}")
    if not timbrada:
        resultado += ' -> queda como caso A: volver a correr el script'
    return True, resultado


def filas_en_cancelacion(db):
    cur = db.cursor(dictionary=True)
    cur.execute(f"SELECT id, odoo_so_name, error_log FROM {TBL_BILLING} "
                f"WHERE marketplace = %s AND status = %s ORDER BY id",
                (MARKETPLACE, EN_CANCELACION))
    filas = cur.fetchall()
    cur.close()
    return filas


# =============================================================================
# PRINCIPAL
# =============================================================================

def run():
    m = cargar_01()
    hoy = m.ahora_cdmx()
    # La ventana de cierre se abre hasta hoy para que fecha_factura() de el mes
    # de la venta. Tope de 3 dias por las 72 horas del SAT.
    if hoy.day > 3:
        log.warning(f"Hoy es dia {hoy.day}: pasaron las 72 horas para timbrar con "
                    f"fecha del mes anterior. El caso B no se aplica.")
    m.DIAS_CIERRE_MES = min(max(m.DIAS_CIERRE_MES, hoy.day), 3)

    db = get_db_connection()
    odoo = OdooModelProxy(timeout=300)
    extra = campos_mx(odoo)
    if 'l10n_mx_edi_cfdi_uuid' not in extra:
        raise Exception("Esta base no tiene l10n_mx_edi_cfdi_uuid: no es la de produccion MX.")
    teams = get_amazon_team_ids(odoo)
    contexto = {'cxc': get_account_id(odoo, ACCOUNT_AMAZON_CXC),
                'catalogos': m.cargar_catalogos(odoo)}
    cuenta_205 = get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA)

    facturas = facturas_del_periodo(odoo, teams, extra)
    nombres_so = sorted({f['invoice_origin'] for f in facturas if f['invoice_origin']})
    sos = {}
    for i in range(0, len(nombres_so), 200):
        for so in odoo.execute_kw('sale.order', 'search_read',
                                  [[('name', 'in', nombres_so[i:i + 200])]],
                                  {'fields': ['id', 'name', 'state', 'date_order',
                                              'team_id', 'order_line', 'invoice_ids']}):
            sos[so['name']] = so
    inicio = m.inicio_de_mes(hoy)

    casos = []
    for f in facturas:
        so = sos.get(f['invoice_origin'])
        if not so:
            continue
        venta = m.fecha_factura(so, hoy)   # None = venta del mes en curso
        if not f.get('l10n_mx_edi_cfdi_uuid'):
            casos.append(('A', f, so, venta))
        elif venta and str(f['invoice_date']) >= f"{inicio:%Y-%m-%d}":
            casos.append(('B', f, so, venta))
    log.info(f"{len(facturas)} B2C de Amazon publicadas desde {DESDE}: "
             f"{sum(1 for c in casos if c[0] == 'A')} sin timbre (A), "
             f"{sum(1 for c in casos if c[0] == 'B')} con fecha del mes equivocado (B).")

    en_curso = filas_en_cancelacion(db)
    if en_curso:
        log.info(f"{len(en_curso)} cancelacion(es) solicitadas en una corrida anterior.")

    salida = []
    hechos = 0

    # --- CASO A: factura por factura ---
    for caso, f, so, venta in casos:
        if caso != 'A' or SOLO == 'B':
            continue
        fila = [caso, f['invoice_origin'], f['name'], f['invoice_date'],
                venta or f['invoice_date'], f['amount_total']]
        if not APLICAR or (LIMITE and hechos >= LIMITE):
            salida.append(fila + ['en seco' if not APLICAR else 'fuera del limite'])
            continue
        hechos += 1
        try:
            res = sanear_sin_timbre(odoo, m, db, f, so, contexto['cxc'], cuenta_205)
        except Exception as e:
            res = f"ERROR: {str(e)[:400]}"
        log.info(f"A {f['invoice_origin']} {f['name']}: {res}")
        salida.append(fila + [res])

    # --- CASO B, PASADA 1: se solicitan TODAS las cancelaciones ---
    # El SAT no confirma al instante: l10n_mx_edi_cfdi_try_sat justo despues de
    # solicitar devuelve cancel_requested (SO10364343, 2-nov-2026; con "Actualizar
    # SAT" a mano unos segundos despues ya salio cancelada). Por eso aqui no se
    # consulta el SAT: el tiempo que tarda esta pasada es la espera de las primeras.
    filas_b = {}
    for caso, f, so, venta in casos:
        if caso != 'B' or SOLO == 'A':
            continue
        fila = [caso, f['invoice_origin'], f['name'], f['invoice_date'],
                venta or f['invoice_date'], f['amount_total']]
        filas_b[f['invoice_origin']] = fila
        if not APLICAR or (LIMITE and hechos >= LIMITE):
            fila.append('en seco' if not APLICAR else 'fuera del limite')
            continue
        if hoy.day > 3:
            fila.append('NO APLICADA: pasaron las 72 horas')
            continue
        hechos += 1
        ctl = fila_control(db, f['invoice_origin'])
        if not ctl:
            fila.append('sin fila en la tabla de control; no se toca')
            continue
        try:
            _, res = cancelar(odoo, db, f, ctl, extra)
        except Exception as e:
            res = f"ERROR: {str(e)[:400]}"
        log.info(f"B {f['invoice_origin']} {f['name']}: {res}")
        fila.append(res)

    # --- CASO B, PASADA 2: SAT y factura nueva para lo ya cancelado ---
    # Toma todo lo que esta en EN_CANCELACION, tambien lo de corridas anteriores.
    # Rondas con una espera ENTRE RONDAS, no por factura. Lo que siga sin
    # confirmar al final se termina volviendo a correr el script. Una factura
    # nueva que no se timbre queda como caso A para la siguiente corrida.
    if APLICAR and SOLO != 'A':
        for ronda in range(1, RONDAS_SAT + 1):
            cola = filas_en_cancelacion(db)
            if not cola:
                break
            if ronda > 1:
                log.info(f"{len(cola)} cancelacion(es) sin confirmar; ronda {ronda} "
                         f"en {ESPERA_SAT} s.")
                tm.sleep(ESPERA_SAT)
            for r in cola:
                pendiente = json.loads(r['error_log'][len('SANEO '):])
                try:
                    hecho, res = refacturar(odoo, m, db, r['id'], pendiente, contexto, extra)
                except Exception as e:
                    hecho, res = True, f"ERROR: {str(e)[:400]}"
                if hecho or ronda == RONDAS_SAT:
                    log.info(f"B {r['odoo_so_name']} {pendiente['factura']}: {res}")
                    fila = filas_b.setdefault(
                        r['odoo_so_name'], ['B', r['odoo_so_name'], pendiente['factura'],
                                            '', '', '', 'solicitada en una corrida anterior'])
                    if len(fila) == 7:
                        fila[-1] = f"{fila[-1]} | {res}"
                    else:
                        fila.append(res)
    elif en_curso:
        for r in en_curso:
            filas_b.setdefault(r['odoo_so_name'], ['B', r['odoo_so_name'], '', '', '', '',
                                                   'cancelacion solicitada (pendiente)'])
    salida += list(filas_b.values())

    with open(CSV_SALIDA, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['Caso', 'Orden de venta', 'Factura', 'Fecha actual',
                    'Fecha correcta', 'Total', 'Resultado'])
        w.writerows(salida)
    db.close()

    log.info('')
    log.info('=' * 82)
    log.info(f"SANEAMIENTO {'APLICADO' if APLICAR else '(EN SECO)'} -- {len(salida)} fila(s)")
    log.info("  A = publicada sin timbre (se corrige y timbra la misma factura)")
    log.info("  B = timbrada con fecha del mes equivocado (cancelacion 02 + factura nueva)")
    log.info(f"  CSV: {CSV_SALIDA}")
    log.info('=' * 82)


if __name__ == '__main__':
    banner('SANEAMIENTO B2C AMAZON (sin timbre / fecha del mes)')
    validar_entorno()
    run()
