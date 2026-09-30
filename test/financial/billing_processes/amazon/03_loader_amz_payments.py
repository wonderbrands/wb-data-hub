"""
03_loader_amz_payments.py
-------------------------
APLICACION DE COBROS AMAZON EN ODOO

Toma las filas PENDING de finance.mkp_payments_{staging|prod} (las deja
02_miner_amz_payments.py) y registra un pago individual por factura.

Asiento que genera Odoo al aplicar el cobro:

    Cuenta                                              Debe        Haber
    ------------------------------------------------  ----------  ----------
    102.02.008 Liberaciones Amazon Pend. de Procesar   Venta+IVA
    105.01.006 Amazon (CxC)                                        Venta+IVA

La transitoria se usa en lugar de bancos para poder conciliar cada liberacion
contra el extracto bancario antes de dar el proceso por cerrado. Esa cuenta es
la que tiene configurada el diario AMZ_JOURNAL_CODE, asi que el asiento sale
solo: aqui no se arman apuntes a mano.

El descuadre contra el deposito real son las comisiones de Amazon.


===============================================================================
COBROS PARCIALES
===============================================================================
El Paso 6 del flujo de Finanzas dice que el cobro salda la CxC por completo.
Los datos muestran que no siempre: las piezas de una misma orden pueden
liberarse en settlements distintos.

Caso real -- orden 702-2265086-8559438 (2 piezas):
    pieza 164910950935841 -> settlement 27044880571 (deposito 30-jul)
    pieza 165346845825161 -> settlement 27172333181 (deposito 06-ago)

Dos cobros de 1,379.00 sobre la misma factura, con seis dias de diferencia.
La CxC se salda con el segundo.

El script aplica el importe liberado en cada settlement y NUNCA mas que el
saldo pendiente de la factura. Odoo concilia parcialmente sin problema.


===============================================================================
BLINDAJE ANTI DOBLE COBRO
===============================================================================
  1. Clave (marketplace, mkp_order_id, payment_id) donde payment_id es el
     settlement_id. Verificar que exista como UNIQUE KEY en la tabla.
  2. Candado: el paso a PROCESSING es un UPDATE condicionado.
  3. El miner no reabre filas en ODOO_PAID aunque Amazon re-publique.
  4. Se verifica amount_residual ANTES de pagar. Si ya esta en cero, se marca
     ALREADY_PAID y no se toca.
  5. Las escrituras no se reintentan a ciegas (OdooWriteUncertain). Un cobro
     incierto se aparta para revision manual, nunca se repite.
  6. En Kestra: concurrency limit 1, behavior FAIL.
"""

import os
import sys
import json
import time as tm
import logging
from datetime import datetime
import xmlrpc.client

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, TBL_PAYMENTS, TBL_BILLING, MARKETPLACE,
    ACCOUNT_LIBERACIONES, AMZ_JOURNAL_CODE, PAYMENT_METHOD_TRANSFERENCIA,
    KESTRA_EXECUTION_ID,
    validar_entorno, banner, notify_slack, get_db_connection,
    OdooModelProxy, OdooWriteUncertain, a_float,
    parse_settlement_id, find_journal_by_account,
)

# =============================================================================
# CONFIGURACION
# =============================================================================

BATCH_LIMIT = 200 if ENTORNO == 'prod' else 100
MAX_RETRIES = 3
TOLERANCE = 0.02        # centavos de tolerancia al comparar importes

# =============================================================================
# NO_INVOICE NO ES UN ESTADO FINAL
# =============================================================================
# 02_miner_amz_payments.py mina en produccion TODOS los cobros liberados, no
# solo los de ordenes ya facturadas (ver el comentario de SOLO_FACTURADAS
# alli). Eso implica que un cobro puede llegar antes que su factura: Yuju crea
# la SO tarde, la SO estaba en borrador, o la orden estaba en periodo de
# gracia cuando Amazon ya la habia liquidado.
#
# Si NO_INVOICE fuera terminal, ese cobro no se aplicaria NUNCA, ni cuando la
# factura apareciera al dia siguiente. Se perderia en silencio y la CxC de
# Amazon quedaria inflada sin que nada lo reportara.
#
# Por eso se reintenta, con dos limites para que no crezca sin control:
#   * no antes de N horas desde el ultimo intento (no reintentar en la misma
#     tanda del dia)
#   * no despues de N dias desde el deposito: pasado ese plazo la factura ya
#     no va a aparecer sola y lo que hace falta es intervencion manual, no un
#     reintento eterno.
# La tabla mkp_payments_* no tiene columna retry_count, asi que el limite es
# temporal y no por numero de intentos. Es suficiente y no toca el esquema.
# =============================================================================

NO_INVOICE_RETRY_HOURS = int(os.getenv('AMZ_NO_INVOICE_RETRY_HOURS', '6'))
NO_INVOICE_MAX_AGE_DAYS = int(os.getenv('AMZ_NO_INVOICE_MAX_AGE_DAYS', '60'))

ESTADOS_REPROCESABLES = ('PENDING', 'ERROR', 'NO_INVOICE')


# =============================================================================
# VALIDACION DE CONFIGURACION CONTABLE
# =============================================================================

def resolve_journal(odoo):
    """Localiza el diario de los cobros de Amazon.

    Estrategia por defecto: buscarlo POR SU CUENTA (102.02.008 LIBERACIONES
    AMAZON PENDIENTES DE PROCESAR). El diario existe en Odoo pero no tiene un
    codigo corto evidente, y adivinar el codigo es fragil; la cuenta si es un
    dato firme, validado por Finanzas.

    Si AMZ_JOURNAL_CODE viene definido (por ejemplo porque hay dos diarios
    apuntando a la misma cuenta y hubo que desempatar), se usa ese y se
    verifica que la cuenta coincida.
    """
    if not AMZ_JOURNAL_CODE:
        return find_journal_by_account(odoo, ACCOUNT_LIBERACIONES)

    journals = odoo.execute_kw('account.journal', 'search_read',
                               [[('code', '=', AMZ_JOURNAL_CODE)]],
                               {'fields': ['id', 'name', 'type',
                                           'default_account_id'], 'limit': 1})
    if not journals:
        raise Exception(
            f"No existe el diario con codigo '{AMZ_JOURNAL_CODE}'. Dejar "
            f"AMZ_JOURNAL_CODE vacio para que se localice por la cuenta "
            f"{ACCOUNT_LIBERACIONES}.")

    journal = journals[0]
    cuenta = journal.get('default_account_id')
    if not cuenta:
        raise Exception(f"El diario {journal['name']} no tiene cuenta por defecto.")

    acc = odoo.execute_kw('account.account', 'read', [[cuenta[0]]],
                          {'fields': ['code', 'name']})
    code = acc[0]['code'] if acc else '?'

    if code != ACCOUNT_LIBERACIONES:
        raise Exception(
            f"El diario '{journal['name']}' ({AMZ_JOURNAL_CODE}) apunta a la "
            f"cuenta {code}, no a {ACCOUNT_LIBERACIONES}. Los cobros de Amazon "
            f"irian a la cuenta equivocada. Se aborta.")

    log.info(f"Diario validado por codigo: {journal['name']} "
             f"(id={journal['id']}) -> {code} {acc[0]['name']}")
    return journal['id']


# =============================================================================
# TABLA DE CONTROL
# =============================================================================

def fetch_pendientes(db_conn):
    """Cobros pendientes, con la SO y el raw_json de la tabla de facturacion.

    DOS COSAS VIENEN DE mkp_billing_*, NO DE mkp_payments_*:

      odoo_so_name  la tabla de cobros no tiene esa columna, pese a lo que
                    dice el CLAUDE.md. Es el mismo reparto que usa Mercado
                    Libre en produccion (04_loader_ml_payments.py la trae con
                    un JOIN).
      raw_json      el desglose (base, IVA, comisiones, piezas) que se pinta
                    en el chatter.

    El JOIN incluye marketplace ademas de mkp_order_id. El de Mercado Libre
    une solo por mkp_order_id, lo que era inocuo cuando la tabla era suya pero
    hoy es compartida: dos marketplaces con el mismo identificador de orden se
    cruzarian.
    """
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(f"""
        SELECT p.id, p.mkp_order_id, p.payment_id,
               p.amount, p.date_released, p.status,
               b.odoo_so_name, b.raw_json
        FROM {TBL_PAYMENTS} p
        LEFT JOIN {TBL_BILLING} b
               ON b.marketplace = p.marketplace
              AND b.mkp_order_id = p.mkp_order_id
        WHERE p.marketplace = %s
          AND (
                p.status IN ('PENDING', 'ERROR')
             OR (p.status = 'NO_INVOICE'
                 AND p.processed_at < (NOW() - INTERVAL %s HOUR)
                 AND p.date_released >= (NOW() - INTERVAL %s DAY))
              )
        -- Los reintentos de NO_INVOICE van al final: un rezago viejo no debe
        -- desplazar del lote a los cobros nuevos.
        ORDER BY (p.status = 'NO_INVOICE'), p.date_released, p.payment_id
        LIMIT %s
    """, (MARKETPLACE, NO_INVOICE_RETRY_HOURS, NO_INVOICE_MAX_AGE_DAYS,
          BATCH_LIMIT))
    rows = cursor.fetchall()
    cursor.close()

    # payment_id es '<settlement_id>:<order_id>' (ver _amz_common).
    for r in rows:
        r['settlement_id'] = parse_settlement_id(r['payment_id'])
    return rows


def claim(db_conn, record_id):
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_PAYMENTS}
        SET status = 'PROCESSING'
        WHERE id = %s AND status IN {ESTADOS_REPROCESABLES}
    """, (record_id,))
    db_conn.commit()
    ganado = cursor.rowcount > 0
    cursor.close()
    return ganado


def update_payment(db_conn, record_id, status, error_log=None):
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_PAYMENTS}
        SET status = %s,
            error_log = %s,
            processed_at = NOW()
        WHERE id = %s
    """, (status, error_log, record_id))
    db_conn.commit()
    cursor.close()


# =============================================================================
# APLICACION DEL COBRO
# =============================================================================

def buscar_factura(odoo, so_name):
    """Factura publicada de la SO. Se exige state='posted': una factura en
    borrador no tiene apuntes que conciliar."""
    return odoo.execute_kw(
        'account.move', 'search_read',
        [[('invoice_origin', '=', so_name),
          ('move_type', '=', 'out_invoice'),
          ('state', '=', 'posted')]],
        {'fields': ['id', 'name', 'amount_total', 'amount_residual',
                    'payment_state'], 'order': 'id asc'})


def esperadas_por_desglose(record):
    """Reparto declarado por 01_amz_billing.py en el raw_json.

    Lista vacia = orden normal, una sola factura. Con mas de un elemento, la
    orden se facturo partida (un CFDI de Amazon por factura de Odoo) y el
    cobro puede repartirse entre ellas.
    """
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        return []
    return raw.get('desglose_b2b') or []


def aplicar_pago(odoo, journal_id, invoice, importe, fecha, settlement_id,
                 order_id):
    """Registra el pago con el wizard nativo.

    Se usa account.payment.register (y no un account.move a mano) porque es lo
    que concilia las lineas de forma nativa y mantiene correcto el
    payment_state de la factura. Mismo mecanismo que 04_loader_ml_payments.py.
    """
    f = (fecha.strftime('%Y-%m-%d') if isinstance(fecha, datetime)
         else str(fecha)[:10])

    ctx = {'active_model': 'account.move', 'active_ids': [invoice['id']]}
    vals = {
        'journal_id': journal_id,
        'amount': round(float(importe), 2),
        'payment_date': f,
        'l10n_mx_edi_payment_method_id': PAYMENT_METHOD_TRANSFERENCIA,
        'communication': f"AMZ {settlement_id} / {order_id}",
    }

    wiz = odoo.execute_kw('account.payment.register', 'create', [vals],
                          {'context': ctx})
    odoo.execute_kw('account.payment.register', 'action_create_payments',
                    [[wiz]], {'context': ctx})


def sellar_settlement(odoo, invoice, record, importe_aplicado, saldo_previo,
                      excedente, n_facturas):
    """Log de conciliacion: sella settlement_id y deposito en la factura.

    settlement_id no se conoce al facturar (el settlement cierra ~15 dias
    despues), por eso se escribe aqui y no en 01_amz_billing.py.

    TEXTO PLANO: el HTML se mostraba literal en el chatter (ver
    postear_chatter en 01_amz_billing.py). Por XML-RPC el body llega como str
    y message_post lo escapa.

    Se distinguen tres importes porque la prueba del 18-sep los confundio:
      - Liberado por Amazon: lo que trae el settlement para la orden (lo que
        guarda mkp_payments_*.amount).
      - Aplicado a esta factura: lo que realmente se concilio (nunca mas que
        el saldo). Es el importe del pago en Odoo.
      - Excedente: liberado que no se aplico a ninguna factura (promocion
        financiada por Amazon). Finanzas lo necesita para cuadrar 102.02.008.
    """
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        raw = {}
    imp = raw.get('importes', {})

    saldo_nuevo = saldo_previo - importe_aplicado
    lineas = [
        "COBRO AMAZON APLICADO",
        f"settlement_id: {record['settlement_id']}",
        f"Amazon order_id: {record['mkp_order_id']}",
        f"Fecha de liberacion: {record['date_released']}",
        "",
        f"Liberado por Amazon para la orden: {a_float(record['amount']):,.2f}",
        f"Aplicado a esta factura: {importe_aplicado:,.2f}",
        f"Saldo antes: {saldo_previo:,.2f}",
        f"Saldo despues: {saldo_nuevo:,.2f}",
        f"Total de la factura (Odoo): {a_float(invoice.get('amount_total')):,.2f}",
    ]
    if n_facturas > 1:
        lineas.append(f"Orden facturada en {n_facturas} facturas (un CFDI de "
                      f"Amazon por factura); el cobro se reparte entre ellas.")
    if saldo_nuevo > TOLERANCE:
        lineas.append("Cobro PARCIAL: Amazon libera por pieza. El saldo se "
                      "cierra con la liberacion del siguiente settlement.")
    if excedente > TOLERANCE:
        lineas.append(f"EXCEDENTE NO APLICADO: {excedente:,.2f}. Amazon libero "
                      f"mas que el saldo de la factura, tipicamente por una "
                      f"promocion financiada por Amazon (no viene en "
                      f"amz_transaction_items). Pendiente de tratamiento "
                      f"contable de Finanzas.")
    if imp:
        # Son los importes calculados sobre amz_transaction_items. En una
        # promocion financiada por Amazon NO coinciden con la factura (que va
        # por lo que pago el cliente), por eso no se rotulan "Total factura".
        lineas += [
            "",
            "Segun transacciones de Amazon:",
            f"Base: {imp.get('cfdi_base', 0):,.2f} | "
            f"IVA: {imp.get('cfdi_iva', 0):,.2f} | "
            f"Total: {imp.get('cfdi_total', 0):,.2f}",
            f"Comisiones Amazon: {imp.get('fees_total', 0):,.2f}",
        ]
    lineas.append(f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}")
    body = '\n'.join(lineas)

    try:
        odoo.execute_kw('account.move', 'message_post', [invoice['id']],
                        {'body': body, 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo sellar el settlement en {invoice['id']}: {e}")


def procesar(odoo, db_conn, journal_id, record, stats):
    order_id = record['mkp_order_id']
    so_name = record['odoo_so_name']
    settlement_id = record['settlement_id']
    importe = a_float(record['amount'])

    if not so_name:
        # Sin fila en la tabla de facturacion, o con la SO vacia. En
        # produccion es esperable: 02 mina todos los cobros liberados, tambien
        # los de ordenes que todavia no se han facturado. NO_INVOICE se
        # reintenta, asi que el cobro no se pierde cuando la factura aparezca.
        update_payment(db_conn, record['id'], 'NO_INVOICE',
                       error_log='Sin SO en la tabla de facturacion para esta orden')
        stats['sin_so'].append(order_id)
        return

    facturas = buscar_factura(odoo, so_name)

    if not facturas:
        update_payment(db_conn, record['id'], 'NO_INVOICE',
                       error_log=f'Sin factura publicada para {so_name}')
        stats['sin_factura'].append(so_name)
        return

    # =========================================================================
    # VARIAS FACTURAS PARA UNA MISMA SO
    # =========================================================================
    # Por defecto es un incidente fiscal y no se cobra a ciegas. La excepcion
    # es el reparto legitimo: cuando Amazon emite N CFDI para una orden,
    # 01_amz_billing.py crea N facturas y lo deja anotado en el raw_json
    # ('desglose_b2b'). Ese es el unico caso en que se permite repartir el
    # cobro; se distingue leyendo la tabla de facturacion, no adivinando.
    # =========================================================================
    reparto_legitimo = len(esperadas_por_desglose(record)) > 1

    if len(facturas) > 1 and not reparto_legitimo:
        nombres = ', '.join(i['name'] for i in facturas)
        log.error(f"{so_name}: {len(facturas)} facturas publicadas ({nombres}) "
                  f"sin reparto declarado en la tabla de facturacion.")
        update_payment(db_conn, record['id'], 'ERROR',
                       error_log=f'Multiples facturas: {nombres}')
        stats['multi_factura'].append(so_name)
        return

    # --- BLINDAJE 4: saldo real al momento de cobrar ---
    abiertas = [i for i in facturas if a_float(i.get('amount_residual')) > TOLERANCE]
    saldo_total = sum(a_float(i.get('amount_residual')) for i in abiertas)

    if saldo_total <= TOLERANCE:
        nombres = ', '.join(i['name'] for i in facturas)
        log.info(f"{so_name}: {nombres} ya saldada(s). Se omite.")
        update_payment(db_conn, record['id'], 'ALREADY_PAID',
                       error_log='amount_residual = 0 al momento de cobrar')
        stats['ya_pagadas'] += 1
        return

    # --- Nunca mas que el saldo pendiente ---
    # Aplicar de mas dejaria un saldo a favor del cliente que el cliente nunca
    # pago: en la prueba del 18-sep los 4 casos fueron promociones financiadas
    # por Amazon (701-2593380-9743412: cliente pago 1,741.13, Amazon libero
    # 2,597.69). La factura va por lo que pago el cliente, que es lo que Yuju
    # exporta a la SO; el resto es dinero de Amazon, no de la CxC. Se aplica el
    # saldo y el excedente se deja registrado, no se pierde de vista.
    aplicado_total = min(importe, saldo_total)
    # round(): 10316.92 - 10316.90 da 0.0200000004 en punto flotante y un
    # redondeo de centavos acababa reportado como promocion.
    excedente = round(importe - aplicado_total, 2)
    if excedente > TOLERANCE:
        log.warning(f"{so_name}: Amazon libero {importe:,.2f} pero el saldo es "
                    f"{saldo_total:,.2f}. Se aplica {aplicado_total:,.2f}; "
                    f"excedente {excedente:,.2f} sin aplicar.")
        stats['ajustados'].append(
            f"{settlement_id} | {order_id} | {so_name} | liberado "
            f"{importe:,.2f} | aplicado {aplicado_total:,.2f} | excedente "
            f"{excedente:,.2f}")
    else:
        excedente = 0.0

    # El settlement libera por pieza, no por factura. Cuando la orden esta
    # repartida, el importe se va llenando factura por factura en el orden en
    # que se emitieron: es la aplicacion de caja ordinaria y queda auditable
    # en el chatter de cada una.
    por_aplicar = aplicado_total
    aplicadas = []
    for invoice in abiertas:
        if por_aplicar <= TOLERANCE:
            break
        saldo_inv = a_float(invoice.get('amount_residual'))
        monto = min(por_aplicar, saldo_inv)

        try:
            aplicar_pago(odoo, journal_id, invoice, monto,
                         record['date_released'], settlement_id, order_id)
        except OdooWriteUncertain as e:
            # BLINDAJE 5: no se reintenta, podria duplicar el pago.
            log.error(f"{so_name}: cobro INCIERTO sobre {invoice['name']}. "
                      f"Requiere revision manual. {e}")
            update_payment(db_conn, record['id'], 'ERROR',
                           error_log=f"WRITE_UNCERTAIN sobre {invoice['name']}: {e}")
            stats['inciertos'].append(f"{so_name} / {settlement_id}")
            return
        except xmlrpc.client.Fault as e:
            log.error(f"{so_name}: Odoo rechazo el cobro sobre "
                      f"{invoice['name']}: {e}")
            update_payment(db_conn, record['id'], 'ERROR',
                           error_log=f"ODOO_FAULT sobre {invoice['name']}: {e}")
            return

        sellar_settlement(odoo, invoice, record, monto, saldo_inv,
                          excedente, len(facturas))
        por_aplicar -= monto
        aplicadas.append((invoice, monto, saldo_inv))

    # amount guarda lo LIBERADO por Amazon (es lo que cuadra contra el
    # settlement y el banco). Cuando lo aplicado difiere, o se repartio entre
    # varias facturas, se deja escrito en error_log: sin esto la tabla no se
    # puede cruzar contra los pagos de Odoo. Es exactamente lo que confundio a
    # Finanzas el 18-sep (settlement 27241792841: "4 montos del CSV que no
    # estan en Odoo" = 3 excedentes + 1 orden repartida en 2 facturas).
    # Se usa error_log porque es la unica columna libre de la tabla compartida
    # con ML; agregar una columna seria un cambio de esquema.
    notas = []
    if excedente > TOLERANCE:
        notas.append(f"EXCEDENTE_NO_APLICADO liberado {importe:.2f} "
                     f"aplicado {aplicado_total:.2f} "
                     f"sin_aplicar {excedente:.2f}")
    if len(aplicadas) > 1:
        notas.append('REPARTIDO ' + ' + '.join(
            f"{inv['name']} {monto:.2f}" for inv, monto, _ in aplicadas))
    update_payment(db_conn, record['id'], 'ODOO_PAID',
                   error_log=(' | '.join(notas) or None))

    s = stats['por_settlement'].setdefault(
        str(settlement_id), {'n': 0, 'liberado': 0.0, 'aplicado': 0.0})
    s['n'] += 1
    s['liberado'] += importe
    s['aplicado'] += aplicado_total

    resto = saldo_total - aplicado_total
    parcial = resto > TOLERANCE
    stats['aplicados'] += 1
    stats['monto'] += aplicado_total
    if parcial:
        stats['parciales'].append(
            f"{so_name} (aplicado {aplicado_total:,.2f}, resta {resto:,.2f})")

    detalle = ' + '.join(f"{inv['name']} {monto:,.2f}"
                         for inv, monto, _ in aplicadas)
    log.info(f"{so_name}: {aplicado_total:,.2f} aplicado a {detalle} "
             f"(settlement {settlement_id})"
             + ('  [PARCIAL]' if parcial else '  [SALDADA]'))


# =============================================================================
# MAIN
# =============================================================================

def run():
    validar_entorno()
    db_conn = get_db_connection()
    records = fetch_pendientes(db_conn)

    if not records:
        log.info("No hay cobros pendientes.")
        db_conn.close()
        return

    reintentos = sum(1 for r in records if r['status'] == 'NO_INVOICE')
    log.info(f"{len(records)} cobro(s) pendientes "
             f"({reintentos} son reintentos de NO_INVOICE).")

    odoo = OdooModelProxy(timeout=300)
    journal_id = resolve_journal(odoo)

    stats = {'aplicados': 0, 'ya_pagadas': 0, 'monto': 0.0,
             'sin_so': [], 'sin_factura': [], 'multi_factura': [],
             'inciertos': [], 'ajustados': [], 'parciales': [],
             'por_settlement': {}}

    for record in records:
        if not claim(db_conn, record['id']):
            continue
        try:
            procesar(odoo, db_conn, journal_id, record, stats)
        except Exception as e:
            log.error(f"{record['mkp_order_id']}: error no controlado: {e}")
            update_payment(db_conn, record['id'], 'ERROR', error_log=str(e))

    db_conn.close()
    reportar(stats)


def reportar(stats):
    log.info('')
    log.info('=' * 82)
    log.info('APLICACION DE COBROS AMAZON -- RESUMEN')
    log.info('=' * 82)
    log.info(f"  Cobros aplicados        : {stats['aplicados']}")
    log.info(f"  Monto total aplicado    : {stats['monto']:,.2f}")
    log.info(f"  Cobros parciales        : {len(stats['parciales'])}")
    log.info(f"  Facturas ya saldadas    : {stats['ya_pagadas']}")
    log.info(f"  Sin SO en Odoo          : {len(stats['sin_so'])}")
    log.info(f"  Sin factura publicada   : {len(stats['sin_factura'])}")
    log.info(f"  Con multiples facturas  : {len(stats['multi_factura'])}")
    log.info(f"  Cobros inciertos        : {len(stats['inciertos'])}")
    log.info(f"  Excedentes no aplicados : {len(stats['ajustados'])}")
    log.info('=' * 82)

    # Cuadre por settlement: es el documento con el que Finanzas concilia
    # 102.02.008 contra el deposito. Liberado = lo que trae Amazon; aplicado =
    # lo que quedo como pago en Odoo. La diferencia es el excedente.
    # Ojo: cubre solo ESTA corrida. Con BATCH_LIMIT un settlement puede
    # repartirse en varias corridas; el cuadre completo sale de la tabla.
    if stats['por_settlement']:
        log.info('')
        log.info('POR SETTLEMENT (esta corrida):')
        log.info(f"  {'settlement':<14}{'cobros':>7}{'liberado':>16}"
                 f"{'aplicado':>16}{'excedente':>14}")
        for sid in sorted(stats['por_settlement']):
            s = stats['por_settlement'][sid]
            log.info(f"  {sid:<14}{s['n']:>7}{s['liberado']:>16,.2f}"
                     f"{s['aplicado']:>16,.2f}"
                     f"{s['liberado'] - s['aplicado']:>14,.2f}")

    if stats['ajustados']:
        log.info('')
        log.info('EXCEDENTES NO APLICADOS (Amazon libero mas que el saldo; '
                 'tipicamente promocion financiada por Amazon):')
        for a in stats['ajustados']:
            log.info(f"  {a}")
        notify_slack(
            f":moneybag: *{len(stats['ajustados'])} cobro(s) de Amazon con "
            f"excedente no aplicado* (`{KESTRA_EXECUTION_ID}`). Amazon libero "
            f"mas que el saldo de la factura. Ese importe entra al banco pero "
            f"no a 102.02.008: requiere tratamiento contable.\n"
            + '\n'.join(stats['ajustados'][:15]))

    if stats['parciales']:
        log.info('')
        log.info('COBROS PARCIALES (esperado: Amazon libera por pieza):')
        for p in stats['parciales'][:10]:
            log.info(f"  {p}")

    if stats['inciertos']:
        notify_slack(
            f":rotating_light: *{len(stats['inciertos'])} cobro(s) de Amazon "
            f"INCIERTOS* (`{KESTRA_EXECUTION_ID}`).\nNo se reintentaron para "
            f"no duplicar el pago. Verificar en Odoo si el pago existe antes "
            f"de reprocesar.\n{', '.join(stats['inciertos'][:15])}")

    if stats['multi_factura']:
        notify_slack(
            f":rotating_light: *{len(stats['multi_factura'])} orden(es) de "
            f"Amazon con MULTIPLES facturas publicadas*. No se les aplico "
            f"cobro.\n{', '.join(stats['multi_factura'][:15])}")

    if stats['sin_so'] or stats['sin_factura']:
        notify_slack(
            f":mag: Cobros Amazon sin destino (`{KESTRA_EXECUTION_ID}`): "
            f"{len(stats['sin_so'])} sin SO, "
            f"{len(stats['sin_factura'])} sin factura publicada. "
            f"Quedan en NO_INVOICE y se reintentan cada "
            f"{NO_INVOICE_RETRY_HOURS}h hasta {NO_INVOICE_MAX_AGE_DAYS} dias "
            f"despues del deposito; pasado ese plazo requieren revision "
            f"manual.")


if __name__ == '__main__':
    banner('LOADER DE COBROS -- AMAZON')
    t0 = tm.time()
    try:
        run()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico en el loader de cobros: {e}')
        notify_slack(f":x: Fallo critico en cobros Amazon "
                     f"(`{KESTRA_EXECUTION_ID}`): {e}")
        sys.exit(1)
