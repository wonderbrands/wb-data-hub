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

Dos asientos adicionales (revision del lote 4, oct-2026):

  Centavo de redondeo -- dentro del mismo pago, write-off nativo del wizard:
    105.01.006 contra AMZ_ACCOUNT_REDONDEO por +-0.01. La factura queda
    "Pagado" y el pago es exactamente lo liberado.

  Promocion pagada por Amazon -- asiento aparte, ref AUTOPROMO:<stl>:<orden>,
  solo con AMZ_PROMO_MODO=asentar y si clasificar_excedente() la confirma:
    102.02.008 Liberaciones Amazon      excedente
        403.01.002 Cupones Amazon                excedente / 1.16
        208.01.001 IVA trasladado cobrado        IVA


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
    ACCOUNT_CUPONES_AMAZON, ACCOUNT_IVA_TRASLADADO, ACCOUNT_REDONDEO,
    KESTRA_EXECUTION_ID, ODOO_URL,
    validar_entorno, banner, notify_slack, get_db_connection,
    OdooModelProxy, OdooWriteUncertain, a_float, get_account_id,
    parse_settlement_id, find_journal_by_account,
)

# =============================================================================
# CONFIGURACION
# =============================================================================

BATCH_LIMIT = int(os.getenv('AMZ_PAYMENTS_BATCH',
                            '200' if ENTORNO == 'prod' else '100'))
MAX_RETRIES = 3
TOLERANCE = 0.02        # centavos de tolerancia al comparar importes

# =============================================================================
# AJUSTE DE CENTAVO (Finanzas, revision del lote 4)
# =============================================================================
# Odoo calcula el total de la factura desde la SO (precio sin IVA x 1.16,
# redondeado), y Amazon cobra un precio con IVA incluido. Pueden diferir en un
# centavo, en los dos sentidos:
#
#   liberado < factura  (150731, 150721, 150696, 150695, 150693): el cobro
#       deja 0.01 de saldo y la factura queda "Pagado parcial" para siempre.
#   liberado > factura  (702-7982773-9610618: 1,399.00 sobre 1,398.99): el
#       cobro se aplicaba por la factura y el centavo desaparecia sin nota.
#
# Ahora el pago se registra por LO LIBERADO y Odoo salda la diferencia contra
# ACCOUNT_REDONDEO (write-off nativo del wizard). Asi la factura queda
# "Pagado" y 102.02.008 recibe exactamente lo que deposita Amazon, que es lo
# que hace falta para D9 (deposito = suma del diario por settlement).
#
# Un centavo POR FACTURA: una orden repartida en 2 facturas puede traer 0.02.
# Sin AMZ_ACCOUNT_REDONDEO la corrida aborta (ver preparar_contexto).
TOLERANCIA_REDONDEO = 0.01

# =============================================================================
# EXCEDENTE: PROMOCIONES PAGADAS POR AMAZON (D3)
# =============================================================================
#   off       no se clasifica (comportamiento del lote 4).
#   observar  se clasifica y se anota, sin asiento. Es el default: deja la
#             evidencia en la tabla para que Finanzas la revise sin impacto
#             contable.
#   asentar   las CONFIRMADAS generan el asiento 102.02.008 contra
#             403.01.002 + 208.01.001. En produccion, activar solo con el
#             visto bueno fiscal de Finanzas sobre el IVA.
PROMO_MODO = os.getenv('AMZ_PROMO_MODO', 'asentar').strip().lower()

# Clave de idempotencia del asiento de promocion: un asiento por settlement y
# orden. Prefijo distinto de AUTOINV: a proposito, no es una factura.
PROMO_PREFIX = 'AUTOPROMO:'

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
                    'payment_state', 'ref', 'l10n_mx_edi_cfdi_uuid'],
         'order': 'id asc'})


def quitar_neutralizadas(odoo, so_name, facturas):
    """Quita las facturas neutralizadas por una NC publicada de la misma SO y
    del mismo importe (refacturacion del portal a un cliente con RFC: factura
    a publico general + NC + factura al cliente). El cobro va a la vigente.
    Misma regla que netear_con_nc() en 01_amz_billing.py. Se queda con la mas
    reciente: la neutralizada es la mas vieja del mismo importe."""
    if len(facturas) < 2:
        return facturas
    so = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', so_name)]],
                         {'fields': ['invoice_ids'], 'limit': 1})
    ligadas = so[0]['invoice_ids'] if so else []
    ncs = odoo.execute_kw(
        'account.move', 'search_read',
        [[('id', 'in', ligadas), ('move_type', '=', 'out_refund'), ('state', '=', 'posted')]],
        {'fields': ['amount_total']})
    vigentes = list(facturas)
    for nc in ncs:
        par = next((f for f in vigentes
                    if abs(a_float(f['amount_total']) - a_float(nc['amount_total'])) <= 0.01), None)
        if par and len(vigentes) > 1:
            vigentes.remove(par)
    return vigentes


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


# =============================================================================
# REPARTO POR ENVIO (ordenes con varias facturas)
# =============================================================================
# Cambio del 2-oct-2026. Una orden B2B con N CFDI de Amazon se factura en N
# facturas, una por envio. Amazon libera por envio, y cada envio puede caer en
# un settlement distinto (caso visto en la prueba sobre el espejo del 29-sep:
# 2 SKU, 2 facturas, 2 settlements).
#
# Antes el cobro se llenaba factura por factura en el orden de emision, sin
# mirar que envio pagaba el settlement. Con facturas de importe distinto eso
# dejaba pagada a medias la factura equivocada hasta que llegaba el segundo
# deposito: con A (SKU1, 1,000) y B (SKU2, 500), si el primer settlement paga
# el SKU2, los 500 caian en A. Al final todo cuadraba, pero el estado
# intermedio (y un cierre de mes entre los dos depositos) mostraba A parcial y
# B sin pagar, cuando el dinero era de B.
#
# Ahora: se leen de amz_transactions los envios que ESTE settlement libero para
# la orden (misma regla de deduplicacion que 01/02: gana el posted_date mas
# reciente de cada pieza) y el dinero se aplica solo a las facturas de esos
# envios. El vinculo factura -> envio sale del desglose que dejo 01 en el
# raw_json: cada parte guarda su CFDI (uuid) y su envio (pieza). La factura
# lleva ese uuid en l10n_mx_edi_cfdi_uuid y en la ref (AUTOINV:<SO>:<uuid>).
#
# Si el vinculo no se puede establecer con certeza (falta el uuid en alguna
# factura, o no se encuentran los envios), se vuelve al orden de emision y se
# anota REPARTO_SIN_ENVIO: el total sigue cuadrando, solo se pierde la
# atribucion exacta, y queda visible para revisar.
# =============================================================================

QUERY_PIEZAS_ORDEN = """
SELECT COALESCE(shipment_id, transaction_id) AS pieza,
       settlement_id, posted_date
FROM somos_reyes.amz_transactions
WHERE order_id = %s
  AND transaction_status <> 'DEFERRED'
  AND description = 'Order Payment'
"""


def piezas_del_settlement(db_conn, order_id, settlement_id):
    """Envios de la orden cuyo cobro cae en ESTE settlement.

    Por cada pieza gana la fila de posted_date mas reciente, igual que en
    01_amz_billing.py y 02_miner_amz_payments.py.
    """
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_PIEZAS_ORDEN, (order_id,))
    ultima = {}
    for r in cursor.fetchall():
        p = str(r['pieza'])
        if p not in ultima or r['posted_date'] > ultima[p]['posted_date']:
            ultima[p] = r
    cursor.close()
    return {p for p, r in ultima.items()
            if str(r['settlement_id']) == str(settlement_id)}


# =============================================================================
# DESGLOSE DEL COBRO PARA EL CHATTER
# =============================================================================
# Lo que 02 guarda como 'amount' es la VENTA ACREDITADA (precio de lista con
# IVA), no el deposito. Caso 702-6841264-9666630 (revision del 27-oct):
#
#   venta acreditada   1,999.00   product_charges + our_price_tax (+ envio)
#   comisiones          -476.17   amazon_fees (comision + FBA)
#   marketplace_vat      -96.50   50% del IVA del CFDI al comprador (193.00)
#   deposito neto      1,426.33   = item_amount = total_amount de cabecera
#
# Antes el chatter decia "Liberado por Amazon: 1,999.00" y "Comisiones:
# -572.67" (comisiones + marketplace_vat juntos), y no cuadraba contra Seller
# Central. Solo informativo: si la consulta falla, el cobro sigue igual.
# =============================================================================

QUERY_DESGLOSE_COBRO = """
SELECT COALESCE(a.shipment_id, a.transaction_id) AS pieza,
       a.settlement_id, a.posted_date,
       SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
         + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)) AS base,
       SUM(IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
         + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)) AS iva,
       -- amazon_fees YA incluye amazon_for_all_fee y shipping_hb: sumarlos
       -- de nuevo duplicaba el cargo (701-5211463-2777026: 1,535.77 =
       -- comision 1,279.80 + amazon_for_all 255.97). Con solo amazon_fees
       -- cuadra item_amount en 99.7% de las piezas desde ago-2026.
       SUM(IFNULL(b.amazon_fees,0)) AS comisiones,
       SUM(IFNULL(b.marketplace_vat_principal,0)
         + IFNULL(b.marketplace_vat_shipping,0)) AS marketplace_vat,
       SUM(IFNULL(b.item_amount,0)) AS neto
FROM somos_reyes.amz_transactions a
JOIN somos_reyes.amz_transaction_items b ON a.transaction_id = b.transaction_id
WHERE a.order_id = %s
  AND a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'
GROUP BY a.transaction_id, a.transaction_status, a.shipment_id,
         a.settlement_id, a.posted_date
"""


def desglose_del_cobro(db_conn, order_id, settlement_id):
    """Importes de las piezas que ESTE settlement libero para la orden (gana el
    posted_date mas reciente de cada pieza). None si no se pudo calcular."""
    try:
        cursor = db_conn.cursor(dictionary=True)
        cursor.execute(QUERY_DESGLOSE_COBRO, (order_id,))
        ultima = {}
        for r in cursor.fetchall():
            p = str(r['pieza'])
            if p not in ultima or r['posted_date'] > ultima[p]['posted_date']:
                ultima[p] = r
        cursor.close()
    except Exception as e:
        log.warning(f"{order_id}: sin desglose del cobro para el chatter: {e}")
        return None

    piezas = [r for r in ultima.values()
              if str(r['settlement_id']) == str(settlement_id)]
    if not piezas:
        return None
    campos = ('base', 'iva', 'comisiones', 'marketplace_vat', 'neto')
    return {k: round(sum(a_float(r[k]) for r in piezas), 2) for k in campos}


def partir_iva(monto):
    """(base, iva) de un importe con IVA 16% incluido."""
    base = round(monto / 1.16, 2)
    return base, round(monto - base, 2)


def facturas_por_envio(abiertas, desglose, piezas):
    """Separa las facturas abiertas segun el envio que facturan.

    Devuelve (del_settlement, sin_vinculo):
      del_settlement  facturas cuyo envio libero este settlement
      sin_vinculo     facturas que no se pudieron ligar a un envio
    """
    pieza_de_uuid = {}
    for e in desglose:
        uuid = str((e.get('cfdi') or {}).get('uuid') or '').upper()
        pieza = (e.get('envio') or {}).get('pieza')
        if uuid and pieza is not None:
            pieza_de_uuid[uuid] = str(pieza)

    del_settlement, sin_vinculo = [], []
    for inv in abiertas:
        uuid = str(inv.get('l10n_mx_edi_cfdi_uuid') or '').upper()
        if not uuid and inv.get('ref'):
            uuid = str(inv['ref']).rsplit(':', 1)[-1].upper()
        pieza = pieza_de_uuid.get(uuid)
        if pieza is None:
            sin_vinculo.append(inv)
        elif pieza in piezas:
            del_settlement.append(inv)
    return del_settlement, sin_vinculo


def fecha_odoo(fecha):
    return (fecha.strftime('%Y-%m-%d') if isinstance(fecha, datetime)
            else str(fecha)[:10])


def aplicar_pago(odoo, journal_id, invoice, importe, fecha, settlement_id,
                 order_id, writeoff_account_id=None):
    """Registra el pago con el wizard nativo.

    Se usa account.payment.register (y no un account.move a mano) porque es lo
    que concilia las lineas de forma nativa y mantiene correcto el
    payment_state de la factura. Mismo mecanismo que 04_loader_ml_payments.py.

    Con writeoff_account_id el wizard salda la diferencia entre el importe y
    el saldo contra esa cuenta (payment_difference_handling='reconcile'). Solo
    se pasa para el centavo de redondeo; funciona en los dos sentidos (pago
    menor o mayor que el saldo).
    """
    ctx = {'active_model': 'account.move', 'active_ids': [invoice['id']]}
    vals = {
        'journal_id': journal_id,
        'amount': round(float(importe), 2),
        'payment_date': fecha_odoo(fecha),
        'l10n_mx_edi_payment_method_id': PAYMENT_METHOD_TRANSFERENCIA,
        'communication': f"AMZ {settlement_id} / {order_id}",
    }
    if writeoff_account_id:
        vals.update({
            'payment_difference_handling': 'reconcile',
            'writeoff_account_id': writeoff_account_id,
            'writeoff_label': f"Redondeo Amazon {settlement_id} / {order_id}",
        })

    wiz = odoo.execute_kw('account.payment.register', 'create', [vals],
                          {'context': ctx})
    odoo.execute_kw('account.payment.register', 'action_create_payments',
                    [[wiz]], {'context': ctx})


def sellar_settlement(odoo, invoice, record, importe_aplicado, saldo_previo,
                      excedente, n_facturas, redondeo=0.0, nota_excedente=None,
                      cobro=None):
    """Log de conciliacion: sella settlement_id y deposito en la factura.

    settlement_id no se conoce al facturar (el settlement cierra ~15 dias
    despues), por eso se escribe aqui y no en 01_amz_billing.py.

    TEXTO PLANO: el HTML se mostraba literal en el chatter (ver
    postear_chatter en 01_amz_billing.py). Por XML-RPC el body llega como str
    y message_post lo escapa.

    Se distinguen estos importes porque la prueba del 18-sep los confundio:
      - Venta acreditada: lo que Amazon reconoce de la orden en el settlement,
        ANTES de comisiones (lo que guarda mkp_payments_*.amount). No es el
        deposito.
      - Pago registrado en esta factura: lo que se concilio contra la CxC
        (nunca mas que el saldo, salvo el centavo de redondeo).
      - Excedente: venta acreditada que no va a ninguna factura (promocion
        pagada por Amazon). Finanzas lo necesita para cuadrar 102.02.008.
      - Deposito neto: venta acreditada - comisiones - marketplace_vat.
    """
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        raw = {}
    cliente = (raw.get('amazon') or {}).get('order_total_cliente')

    # Con redondeo el pago difiere del saldo en +-0.01 y Odoo salda el
    # centavo: el saldo despues es cero, no -0.01 ni 0.01.
    saldo_nuevo = round(saldo_previo - importe_aplicado + redondeo, 2)
    lineas = [
        "COBRO AMAZON APLICADO",
        f"settlement_id: {record['settlement_id']}",
        f"Amazon order_id: {record['mkp_order_id']}",
        f"Fecha de liberacion: {record['date_released']}",
        "",
        f"Venta acreditada por Amazon (antes de comisiones): "
        f"{a_float(record['amount']):,.2f}",
        f"Pago registrado en esta factura: {importe_aplicado:,.2f}",
        f"Saldo antes: {saldo_previo:,.2f}",
        f"Saldo despues: {saldo_nuevo:,.2f}",
        f"Total de la factura (Odoo): {a_float(invoice.get('amount_total')):,.2f}",
    ]
    if redondeo:
        lineas.append(f"Ajuste de redondeo: {redondeo:+,.2f} saldado contra la "
                      f"cuenta {ACCOUNT_REDONDEO}. Diferencia entre el precio "
                      f"con IVA de Amazon y el total calculado por Odoo.")
    if n_facturas > 1:
        lineas.append(f"Orden facturada en {n_facturas} facturas (un CFDI de "
                      f"Amazon por factura); el cobro se reparte entre ellas.")
    if saldo_nuevo > TOLERANCE:
        lineas.append("Cobro PARCIAL: Amazon libera por pieza. El saldo se "
                      "cierra con la liberacion del siguiente settlement.")
    if nota_excedente:
        lineas.append(nota_excedente)
    elif excedente > TOLERANCE:
        lineas.append(f"EXCEDENTE NO APLICADO: {excedente:,.2f}. Amazon acredito "
                      f"mas que el saldo de la factura. Sin clasificar: queda "
                      f"para revision de Finanzas.")

    # Mismos conceptos que Seller Central. En una promocion pagada por Amazon
    # la venta acreditada NO coincide con la factura (que va por lo que pago
    # el comprador), por eso no se rotula "Total factura".
    lineas += ["", "Desglose de Amazon para esta orden en el settlement:"]
    if cobro:
        lineas += [
            f"Venta acreditada: {cobro['base'] + cobro['iva']:,.2f} "
            f"(base {cobro['base']:,.2f} + IVA {cobro['iva']:,.2f})",
        ]
        if cliente is not None:
            lineas.append(f"Pagado por el comprador (OrderTotal): "
                          f"{a_float(cliente):,.2f}")
        lineas += [
            f"Comisiones Amazon: {cobro['comisiones']:,.2f}",
            f"IVA retenido por Amazon (marketplace_vat): "
            f"{cobro['marketplace_vat']:,.2f}",
            f"Deposito neto: {cobro['neto']:,.2f}",
        ]
    else:
        lineas.append("No disponible (sin transacciones de este settlement).")
    lineas.append(f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}")
    body = '\n'.join(lineas)

    try:
        odoo.execute_kw('account.move', 'message_post', [invoice['id']],
                        {'body': body, 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo sellar el settlement en {invoice['id']}: {e}")


# =============================================================================
# CLASIFICACION DEL EXCEDENTE (D3 / D6)
# =============================================================================
# Finanzas (Martin y Jero, revision del lote 4): el excedente va a 403.01.002
# CUPONES AMAZON solo si es una promocion PAGADA POR AMAZON, no cualquier
# diferencia positiva.
#
# Amazon NO marca esas promociones en ninguna API que usamos. Lo probo
# 08_probe_promo_amazon.py (26-oct-2026): en los 9 casos getOrderItems trae
# PromotionDiscount = 0 y ItemPrice + ItemTax = lo que pago el cliente; la API
# de pedidos ya entrega el precio neto de la promocion y no dice que la hubo.
#
# La regla sale del Playbook de Soporte de Ventas v2.6 de Finanzas (p. 8),
# validada contra settlement al centavo y en 7,535 de 7,564 SO de septiembre:
#
#   promocion = lista neta + envio - pagado por el cliente
#
#   lista neta  product_charges + our_price_tax + our_price_discount
#               + our_price_tax_disc   (ya descuenta NUESTRAS promociones)
#   envio       shipping + shipping_tax + shipping_discount
#               + shipping_tax_discount
#   pagado      amazon_orders.OrderTotalAmount
#
# lista neta + envio es exactamente cfdi_total de 01_amz_billing.py (lo deja en
# raw_json['importes']), y OrderTotal esta en raw_json['amazon']. No hace falta
# consultar ninguna API.
#
# Es un residuo, asi que se exige que TRES fuentes independientes coincidan:
#
#   1. Amazon reporta lo que pago el cliente (OrderTotal).
#   2. La factura de Odoo es igual a eso (+-1 centavo por factura). Es el
#      "Contra Odoo" del playbook: si no, la diferencia es otra cosa.
#   3. Excedente liberado = transacciones - OrderTotal (+-1 centavo por
#      factura), con TODAS las piezas de la orden liberadas en este settlement.
#      La promocion es de la orden completa y el excedente es por settlement:
#      en una orden repartida no hay forma segura de asignarla.
#
# Lo que no cumpla queda EXCEDENTE_SIN_CLASIFICAR con el motivo: sin asiento,
# visible en la tabla y en el expediente (07_amz_casos_diferencia.py).
# =============================================================================

def settlements_de_la_orden(db_conn, order_id):
    """Settlements donde se liberaron las piezas de la orden (gana el
    posted_date mas reciente de cada pieza, igual que en 01 y 02)."""
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_PIEZAS_ORDEN, (order_id,))
    ultima = {}
    for r in cursor.fetchall():
        p = str(r['pieza'])
        if p not in ultima or r['posted_date'] > ultima[p]['posted_date']:
            ultima[p] = r
    cursor.close()
    return {str(r['settlement_id']) for r in ultima.values()}


def clasificar_excedente(db_conn, record, facturas, excedente, tolerancia):
    """{'clase', 'promo', 'motivo'}. Nunca lanza: ante cualquier duda,
    SIN_CLASIFICAR (fail-closed: sin asiento)."""
    def sin(motivo):
        return {'clase': 'SIN_CLASIFICAR', 'promo': 0.0, 'motivo': motivo}

    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        raw = {}
    cliente = (raw.get('amazon') or {}).get('order_total_cliente')
    transacciones = (raw.get('importes') or {}).get('cfdi_total')

    # --- 1. Lo que pago el cliente ---
    if cliente is None or transacciones is None:
        return sin('sin OrderTotal o sin importes en la tabla de facturacion')
    cliente = round(a_float(cliente), 2)
    transacciones = round(a_float(transacciones), 2)

    # --- 2. Factura de Odoo = lo que pago el cliente ---
    total_facturas = round(sum(a_float(i.get('amount_total')) for i in facturas), 2)
    if round(abs(total_facturas - cliente), 2) > tolerancia:
        return sin(f"factura {total_facturas:.2f} no coincide con lo pagado "
                   f"por el cliente {cliente:.2f}")

    # --- 3. Excedente = promocion del playbook, orden completa ---
    # Playbook: si la formula da negativo, va en 0 y se informa.
    promo = round(transacciones - cliente, 2)
    if promo <= 0.005:
        return sin(f"transacciones {transacciones:.2f} no superan lo pagado "
                   f"por el cliente {cliente:.2f}")
    otros = (settlements_de_la_orden(db_conn, record['mkp_order_id'])
             - {str(record['settlement_id'])})
    if otros:
        return sin(f"orden repartida entre settlements ({', '.join(sorted(otros))})")
    # round(): 100.01 - 100.00 da 0.0100000000000051 y un cupon de $100 con
    # su centavo de redondeo (702-0367082-5204206) quedaba sin clasificar.
    if round(abs(excedente - promo), 2) > tolerancia:
        return sin(f"promocion {promo:.2f} no coincide con excedente "
                   f"{excedente:.2f}")
    return {'clase': 'PROMO_AMAZON', 'promo': promo,
            'motivo': f"promocion pagada por Amazon {promo:.2f} "
                      f"(transacciones {transacciones:.2f} - cliente {cliente:.2f})"}


def url_registro(move_id):
    """Liga directa a un account.move. El chatter va en texto plano, pero
    Odoo convierte en enlace cualquier URL del texto (confirmado en el espejo
    del 05-oct). ODOO_URL se conmuta con ENTORNO, igual que la base."""
    return f"{ODOO_URL.rstrip('/')}/odoo/account.move/{move_id}"


def ligar_promo_con_facturas(odoo, asiento, record, aplicadas):
    """Chatter del asiento AUTOPROMO con la liga a la(s) factura(s) donde se
    aplico el cobro: quien llega desde el libro mayor de 403.01.002 llega
    tambien a la factura. Solo informativo: si falla, se avisa y se sigue."""
    lineas = [
        "PROMOCION PAGADA POR AMAZON",
        f"Amazon order_id: {record['mkp_order_id']}",
        f"settlement_id: {record['settlement_id']}",
        "",
        "Cobro aplicado en:",
    ]
    lineas += [f"{inv['name']} ({monto:,.2f}): {url_registro(inv['id'])}"
               for inv, monto, _ in aplicadas]
    lineas.append(f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}")
    try:
        odoo.execute_kw('account.move', 'message_post', [asiento['id']],
                        {'body': '\n'.join(lineas), 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo ligar el asiento {asiento['name']} con sus "
                    f"facturas: {e}")


def registrar_promo(odoo, journal_id, cuentas, record, promo, so_name, facturas):
    """Asiento de la promocion pagada por Amazon. Devuelve {'id', 'name'}.

        102.02.008 Liberaciones Amazon     promo
            403.01.002 Cupones Amazon              promo / 1.16
            208.01.001 IVA trasladado cobrado      promo - base

    Idempotente por ref (AUTOPROMO:<settlement>:<orden>): si ya existe uno
    vivo, no se crea otro. Se crea ANTES de aplicar el pago: si el pago falla,
    la fila queda en ERROR, se reintenta, y el asiento ya existente se
    reconoce por su ref en vez de duplicarse.
    """
    ref = f"{PROMO_PREFIX}{record['settlement_id']}:{record['mkp_order_id']}"
    ya = odoo.execute_kw('account.move', 'search_read',
                         [[('ref', '=', ref), ('state', '!=', 'cancel')]],
                         {'fields': ['id', 'name'], 'limit': 1})
    if ya:
        return {'id': ya[0]['id'], 'name': ya[0]['name']}

    base, iva = partir_iva(promo)
    # La etiqueta lleva SO y factura ademas de orden y settlement: con ella
    # Finanzas concilia a mano la 102.02.008 desde el libro mayor, sin abrir
    # el chatter (correo de Jero, 28-oct-2026, punto 3).
    etiqueta = (f"Promocion pagada por Amazon {record['mkp_order_id']} | "
                f"settlement {record['settlement_id']} | {so_name} | "
                f"{', '.join(f['name'] for f in facturas)}")
    vals = {
        'move_type': 'entry',
        'journal_id': journal_id,
        'date': fecha_odoo(record['date_released']),
        'ref': ref,
        'line_ids': [
            (0, 0, {'account_id': cuentas['liberaciones'], 'name': etiqueta,
                    'debit': promo, 'credit': 0.0}),
            (0, 0, {'account_id': cuentas['cupones'], 'name': etiqueta,
                    'debit': 0.0, 'credit': base}),
            (0, 0, {'account_id': cuentas['iva'], 'name': etiqueta,
                    'debit': 0.0, 'credit': iva}),
        ],
    }
    move_id = odoo.execute_kw('account.move', 'create', [vals])
    odoo.execute_kw('account.move', 'action_post', [[move_id]])
    leido = odoo.execute_kw('account.move', 'read', [[move_id]],
                            {'fields': ['name']})
    return {'id': move_id, 'name': leido[0]['name'] if leido else str(move_id)}


def repartir_centavos(destino, dif):
    """Cierra todas las facturas destino repartiendo 'dif' de a centavo.

    Devuelve [(factura, monto_pago, redondeo)]. Nunca mas de 0.01 por factura:
    es lo que aprobo Finanzas.
    """
    n = int(round(abs(dif) * 100))
    signo = 1 if dif > 0 else -1
    montos = []
    for k, inv in enumerate(destino):
        ajuste = round(signo * 0.01, 2) if k < n else 0.0
        saldo = a_float(inv.get('amount_residual'))
        montos.append((inv, round(saldo + ajuste, 2), ajuste))
    return montos


def llenar_en_orden(destino, pago):
    """Cobro parcial: se llena factura por factura sin pasar de su saldo."""
    montos, resto = [], round(pago, 2)
    for inv in destino:
        if resto <= 0.005:
            break
        m = round(min(resto, a_float(inv.get('amount_residual'))), 2)
        montos.append((inv, m, 0.0))
        resto = round(resto - m, 2)
    return montos


def preparar_contexto(odoo):
    """Cuentas que dependen de la configuracion. FAIL-CLOSED: si falta una
    cuenta o no existe en Odoo, se aborta la corrida antes de tocar nada, en
    vez de seguir sin ajuste y descubrirlo en el cierre (lote 5: dos facturas
    en pago parcial por 0.01 porque la variable no llego)."""
    if not ACCOUNT_REDONDEO:
        raise Exception("AMZ_ACCOUNT_REDONDEO vacio: sin cuenta de redondeo las "
                        "facturas con 1 centavo de diferencia quedan en pago "
                        "parcial. Definirla antes de correr.")
    ctx = {'cuenta_redondeo': get_account_id(odoo, ACCOUNT_REDONDEO),
           'cuentas': None}
    log.info(f"Ajuste de centavo contra la cuenta {ACCOUNT_REDONDEO}.")

    if PROMO_MODO not in ('off', 'observar', 'asentar'):
        raise Exception(f"AMZ_PROMO_MODO='{PROMO_MODO}' invalido "
                        f"(off | observar | asentar).")
    if PROMO_MODO == 'asentar':
        ctx['cuentas'] = {
            'liberaciones': get_account_id(odoo, ACCOUNT_LIBERACIONES),
            'cupones': get_account_id(odoo, ACCOUNT_CUPONES_AMAZON),
            'iva': get_account_id(odoo, ACCOUNT_IVA_TRASLADADO),
        }
    log.info(f"Excedentes: modo '{PROMO_MODO}'")
    return ctx


def contar_pendientes(db_conn):
    cursor = db_conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {TBL_PAYMENTS} "
                   f"WHERE marketplace = %s AND status IN ('PENDING', 'ERROR')",
                   (MARKETPLACE,))
    n = cursor.fetchone()[0]
    cursor.close()
    return n


def procesar(odoo, db_conn, journal_id, record, stats, ctx):
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
    desglose = esperadas_por_desglose(record)
    reparto_legitimo = len(desglose) > 1

    # La factura aparte de envio (AUTOINV:<SO>:ENVIO, de 01) complementa a la
    # principal: el cobro se reparte entre las dos y la regla de promocion
    # compara la SUMA contra lo que pago el cliente (correo de Jero, 28-oct, segundo).
    de_envio = [f for f in facturas if (f.get('ref') or '').endswith(':ENVIO')]
    facturas = [f for f in facturas if f not in de_envio]

    if len(facturas) > 1 and not reparto_legitimo:
        facturas = quitar_neutralizadas(odoo, so_name, facturas)

    if len(facturas) > 1 and not reparto_legitimo:
        nombres = ', '.join(i['name'] for i in facturas)
        log.error(f"{so_name}: {len(facturas)} facturas publicadas ({nombres}) "
                  f"sin reparto declarado en la tabla de facturacion.")
        update_payment(db_conn, record['id'], 'ERROR',
                       error_log=f'Multiples facturas: {nombres}')
        stats['multi_factura'].append(so_name)
        return
    facturas = facturas + de_envio

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

    # --- Orden repartida: el cobro va a las facturas de SUS envios ---
    # Ver "REPARTO POR ENVIO". saldo_total sigue siendo el de toda la orden
    # (para saber si queda parcial); el tope de lo que se aplica es el saldo
    # de las facturas destino.
    destino = abiertas
    nota_reparto = None
    if reparto_legitimo:
        piezas = piezas_del_settlement(db_conn, order_id, settlement_id)
        por_envio, sin_vinculo = facturas_por_envio(abiertas, desglose, piezas)
        if piezas and not sin_vinculo:
            if not por_envio:
                log.info(f"{so_name}: las facturas de los envios del settlement "
                         f"{settlement_id} ya estan saldadas. Se omite.")
                update_payment(db_conn, record['id'], 'ALREADY_PAID',
                               error_log=('Las facturas de los envios de este '
                                          'settlement ya estaban saldadas'))
                stats['ya_pagadas'] += 1
                return
            destino = por_envio
            nota_reparto = 'POR_ENVIO ' + ' + '.join(i['name'] for i in destino)
        else:
            nota_reparto = 'REPARTO_SIN_ENVIO'
            log.warning(f"{so_name}: no se pudo ligar cada factura a su envio "
                        f"(envios del settlement: {len(piezas)}, facturas sin "
                        f"vinculo: {len(sin_vinculo)}). Se reparte en orden de "
                        f"emision.")
            stats['sin_envio'].append(f"{so_name} / {settlement_id}")
    saldo_destino = sum(a_float(i.get('amount_residual')) for i in destino)

    # =========================================================================
    # CUANTO VA A LAS FACTURAS
    # =========================================================================
    # bruto = liberado - saldo de las facturas destino.
    #   bruto  >  tolerancia  sobra dinero: promocion de Amazon u otra cosa
    #   |bruto| <= tolerancia redondeo: se salda el centavo (si hay cuenta)
    #   bruto  < -tolerancia  cobro parcial: Amazon libera por pieza
    #
    # Nunca se aplica a la factura mas que su saldo (salvo el centavo): aplicar
    # de mas dejaria un saldo a favor de un cliente que nunca pago eso. En la
    # prueba del 18-sep 701-2593380-9743412 tenia factura de 1,741.13 y Amazon
    # libero 2,597.69.
    # =========================================================================
    tol = round(TOLERANCIA_REDONDEO * len(destino), 2)
    bruto = round(importe - saldo_destino, 2)

    # --- 1. Excedente: se clasifica y, si se confirma, se asienta ---
    clasif = None
    promo = 0.0
    asiento_promo = None
    if bruto > tol and PROMO_MODO != 'off':
        clasif = clasificar_excedente(db_conn, record, facturas, bruto, tol)
        if clasif['clase'] == 'PROMO_AMAZON' and PROMO_MODO == 'asentar':
            try:
                asiento_promo = registrar_promo(odoo, journal_id,
                                                ctx['cuentas'], record,
                                                clasif['promo'], so_name,
                                                destino)
            except OdooWriteUncertain as e:
                log.error(f"{so_name}: asiento de promocion INCIERTO. {e}")
                update_payment(db_conn, record['id'], 'ERROR',
                               error_log=f"WRITE_UNCERTAIN asiento promo: {e}")
                stats['inciertos'].append(f"{so_name} / {settlement_id} (promo)")
                return
            except xmlrpc.client.Fault as e:
                log.error(f"{so_name}: Odoo rechazo el asiento de promocion: {e}")
                update_payment(db_conn, record['id'], 'ERROR',
                               error_log=f"ODOO_FAULT asiento promo: {e}")
                return
            promo = clasif['promo']

    # --- 2. Reparto entre las facturas ---
    pago = round(importe - promo, 2)
    dif = round(pago - saldo_destino, 2)
    excedente = 0.0
    if 0 < abs(dif) <= tol:
        montos = repartir_centavos(destino, dif)
    elif dif > tol:
        montos = llenar_en_orden(destino, saldo_destino)
        excedente = dif
    else:
        montos = llenar_en_orden(destino, pago)

    if excedente:
        log.warning(f"{so_name}: Amazon libero {importe:,.2f} pero el saldo es "
                    f"{saldo_destino:,.2f}. Excedente {excedente:,.2f} sin "
                    f"aplicar" + (f" ({clasif['motivo']})" if clasif else '')
                    + '.')
        stats['ajustados'].append(
            f"{settlement_id} | {order_id} | {so_name} | liberado "
            f"{importe:,.2f} | saldo {saldo_destino:,.2f} | excedente "
            f"{excedente:,.2f} | "
            + (f"{clasif['clase']}: {clasif['motivo']}" if clasif
               else 'sin clasificar (modo off)'))

    # Texto del excedente para el chatter.
    # La base es lo que Seller Central muestra como "Paid by Amazon" (sin
    # IVA): 702-6841264-9666630, 599.71 = 516.99 + 82.72.
    nota_exc = None
    if asiento_promo:
        base_p, iva_p = partir_iva(promo)
        nota_exc = (f"PROMOCION PAGADA POR AMAZON: {promo:,.2f} = base "
                    f"{base_p:,.2f} en {ACCOUNT_CUPONES_AMAZON} (Seller Central: "
                    f"Paid by Amazon) + IVA {iva_p:,.2f} en "
                    f"{ACCOUNT_IVA_TRASLADADO}. Asiento {asiento_promo['name']}.\n"
                    f"Ver asiento: {url_registro(asiento_promo['id'])}")
    elif clasif and clasif['clase'] == 'PROMO_AMAZON':
        base_p, iva_p = partir_iva(clasif['promo'])
        nota_exc = (f"EXCEDENTE NO APLICADO: {excedente:,.2f}. Detectado como "
                    f"promocion pagada por Amazon ({clasif['promo']:,.2f} = "
                    f"base {base_p:,.2f} + IVA {iva_p:,.2f}); sin asiento "
                    f"(modo {PROMO_MODO}).")
    elif clasif:
        nota_exc = (f"EXCEDENTE NO APLICADO: {excedente:,.2f}. Sin clasificar: "
                    f"{clasif['motivo']}. Queda para revision de Finanzas.")

    # --- 3. Aplicacion ---
    cobro = desglose_del_cobro(db_conn, order_id, settlement_id)
    aplicadas = []
    for invoice, monto, redondeo in montos:
        if monto <= 0.005:
            continue
        saldo_inv = a_float(invoice.get('amount_residual'))
        try:
            aplicar_pago(odoo, journal_id, invoice, monto,
                         record['date_released'], settlement_id, order_id,
                         writeoff_account_id=(ctx['cuenta_redondeo']
                                              if redondeo else None))
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
                          excedente, len(facturas), redondeo, nota_exc, cobro)
        aplicadas.append((invoice, monto, redondeo))

    aplicado_total = round(sum(m for _, m, _ in aplicadas), 2)
    redondeo_total = round(sum(r for _, _, r in aplicadas), 2)

    if asiento_promo and aplicadas:
        ligar_promo_con_facturas(odoo, asiento_promo, record, aplicadas)

    # =========================================================================
    # NOTAS EN LA TABLA
    # =========================================================================
    # amount guarda lo LIBERADO por Amazon (cuadra contra el settlement y el
    # banco). Lo demas va en error_log, la unica columna libre de la tabla
    # compartida con ML. Con esto se cumple, por fila:
    #
    #     liberado = aplicado + promo + sin_aplicar
    #
    # 'aplicado' es la suma de los pagos en Odoo (incluye el centavo de
    # redondeo). 07_amz_casos_diferencia.py lee 'aplicado' y 'sin_aplicar' de
    # estas notas: no cambiar el formato sin ajustar resumir_cobros() alla.
    # =========================================================================
    notas = []
    if asiento_promo:
        notas.append(f"EXCEDENTE_PROMO_ASENTADO liberado {importe:.2f} "
                     f"aplicado {aplicado_total:.2f} promo {promo:.2f} "
                     f"asiento {asiento_promo['name']}")
    elif excedente:
        notas.append(f"EXCEDENTE_NO_APLICADO liberado {importe:.2f} "
                     f"aplicado {aplicado_total:.2f} "
                     f"sin_aplicar {excedente:.2f}")
        if clasif and clasif['clase'] == 'PROMO_AMAZON':
            notas.append(f"PROMO_AMAZON_DETECTADA {clasif['promo']:.2f} "
                         f"(sin asiento, modo {PROMO_MODO})")
        elif clasif:
            notas.append(f"EXCEDENTE_SIN_CLASIFICAR {clasif['motivo']}")
    if redondeo_total:
        notas.append(f"REDONDEO {redondeo_total:+.2f}")
    if len(aplicadas) > 1:
        notas.append('REPARTIDO ' + ' + '.join(
            f"{inv['name']} {monto:.2f}" for inv, monto, _ in aplicadas))
    if nota_reparto:
        notas.append(nota_reparto)
    update_payment(db_conn, record['id'], 'ODOO_PAID',
                   error_log=(' | '.join(notas) or None))

    s = stats['por_settlement'].setdefault(
        str(settlement_id), {'n': 0, 'liberado': 0.0, 'aplicado': 0.0,
                             'promo': 0.0, 'sin_aplicar': 0.0,
                             'redondeo': 0.0})
    s['n'] += 1
    s['liberado'] += importe
    s['aplicado'] += aplicado_total
    s['promo'] += promo
    s['sin_aplicar'] += excedente
    s['redondeo'] += redondeo_total

    if asiento_promo:
        stats['promos'].append(f"{settlement_id} | {order_id} | {so_name} | "
                               f"{promo:,.2f} | {asiento_promo['name']}")
    if redondeo_total:
        stats['redondeos'].append(f"{so_name} {redondeo_total:+.2f}")

    resto = round(saldo_total - aplicado_total + redondeo_total, 2)
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
             + (f" redondeo {redondeo_total:+.2f}" if redondeo_total else '')
             + (f" promo {promo:,.2f}" if promo else '')
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
    ctx = preparar_contexto(odoo)

    stats = {'aplicados': 0, 'ya_pagadas': 0, 'monto': 0.0,
             'sin_so': [], 'sin_factura': [], 'multi_factura': [],
             'inciertos': [], 'ajustados': [], 'parciales': [],
             'por_settlement': {}, 'sin_envio': [],
             'promos': [], 'redondeos': []}

    for record in records:
        if not claim(db_conn, record['id']):
            continue
        try:
            procesar(odoo, db_conn, journal_id, record, stats, ctx)
        except Exception as e:
            log.error(f"{record['mkp_order_id']}: error no controlado: {e}")
            update_payment(db_conn, record['id'], 'ERROR', error_log=str(e))

    # Con BATCH_LIMIT una corrida puede no terminar la cola. En el lote del
    # 18-sep eso dejo cobros en PENDING sin ningun aviso y parecio un fallo
    # de 02. Ahora se dice cuantos quedan.
    stats['quedan'] = contar_pendientes(db_conn)
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
    log.info(f"  Promociones asentadas   : {len(stats['promos'])}")
    log.info(f"  Ajustes de centavo      : {len(stats['redondeos'])}")
    log.info(f"  Reparto sin envio       : {len(stats['sin_envio'])}")
    log.info(f"  QUEDAN EN COLA          : {stats.get('quedan', 0)}"
             + (f"  <- volver a correr (BATCH_LIMIT={BATCH_LIMIT})"
                if stats.get('quedan') else ''))
    log.info('=' * 82)

    # Cuadre por settlement: es el documento con el que Finanzas concilia
    # 102.02.008 contra el deposito (D9). Por settlement:
    #   liberado = aplicado + promo + sin_aplicar
    # 'aplicado' son los pagos en Odoo (incluye el centavo de redondeo);
    # 'promo' entra a 102.02.008 por el asiento AUTOPROMO; 'sin_aplicar' es
    # lo unico que NO entra a 102.02.008 y queda para revision.
    # Ojo: cubre solo ESTA corrida. Con BATCH_LIMIT un settlement puede
    # repartirse en varias corridas; el cuadre completo sale de la tabla.
    if stats['por_settlement']:
        log.info('')
        log.info('POR SETTLEMENT (esta corrida):')
        log.info(f"  {'settlement':<14}{'cobros':>7}{'liberado':>14}"
                 f"{'aplicado':>14}{'promo':>11}{'sin_aplicar':>13}"
                 f"{'redondeo':>10}")
        for sid in sorted(stats['por_settlement']):
            s = stats['por_settlement'][sid]
            log.info(f"  {sid:<14}{s['n']:>7}{s['liberado']:>14,.2f}"
                     f"{s['aplicado']:>14,.2f}{s['promo']:>11,.2f}"
                     f"{s['sin_aplicar']:>13,.2f}{s['redondeo']:>+10.2f}")

    if stats['promos']:
        log.info('')
        log.info('PROMOCIONES PAGADAS POR AMAZON ASENTADAS (403.01.002 + 208.01.001):')
        for p in stats['promos']:
            log.info(f"  {p}")

    if stats['redondeos']:
        log.info('')
        log.info(f"AJUSTES DE CENTAVO (contra {ACCOUNT_REDONDEO}): "
                 + ', '.join(stats['redondeos'][:20]))

    if stats['ajustados']:
        log.info('')
        log.info('EXCEDENTES NO APLICADOS (Amazon libero mas que el saldo):')
        for a in stats['ajustados']:
            log.info(f"  {a}")
        notify_slack(
            f":moneybag: *{len(stats['ajustados'])} cobro(s) de Amazon con "
            f"excedente no aplicado* (`{KESTRA_EXECUTION_ID}`). Amazon libero "
            f"mas que el saldo de la factura y no se confirmo como promocion "
            f"pagada por Amazon. Ese importe entra al banco pero no a "
            f"102.02.008: requiere revision.\n"
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
