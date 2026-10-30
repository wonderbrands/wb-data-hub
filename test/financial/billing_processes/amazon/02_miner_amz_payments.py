"""

02_miner_amz_payments.py
------------------------
MINER DE COBROS AMAZON

Deja en finance.mkp_payments_{staging|prod} una fila por cada cobro a aplicar.
No toca Odoo: solo MySQL.


===============================================================================
MAPEO A LA TABLA DE MERCADO LIBRE
===============================================================================
finance.mkp_payments_* nacio para Mercado Libre. Asi se acomoda Amazon sin
alterar el esquema:

  marketplace     'AMAZON'
  mkp_order_id    order_id de Amazon
  payment_id      '<settlement_id>:<order_id>'   <<< CLAVE COMPUESTA
  amount          importe liberado en ese settlement (con IVA)
  date_released   deposit_date del settlement
  status          PENDING -> PROCESSING -> ODOO_PAID / NO_INVOICE / ERROR

La SO de Odoo NO se guarda aqui: la tabla no tiene columna odoo_so_name, pese
a lo que dice el CLAUDE.md. Vive solo en finance.mkp_billing_*, y el loader la
obtiene uniendo por (marketplace, mkp_order_id). Es exactamente el reparto que
usa Mercado Libre en produccion. Este miner si la calcula (columna
odoo_so_name de QUERY_COBROS) pero solo para reportar cuantas ordenes
liquidadas no tienen SO cruzada.


POR QUE payment_id ES COMPUESTO
-------------------------------
La tabla tiene UNIQUE KEY uk_payment (marketplace, payment_id), SIN
mkp_order_id.

En Mercado Libre eso basta porque payment_id es el id del pago de Mercado
Pago: uno por orden. En Amazon el identificador del deposito es el
settlement_id, y un settlement agrupa CIENTOS de ordenes. Guardar solo el
settlement_id haria que unicamente la PRIMERA orden de cada settlement
entrara; el resto choca con la unique key.

Concatenar settlement y orden hace que la clave sea exactamente la unidad de
cobro: una orden, en un settlement, una vez. Y de paso da la proteccion contra
el doble cobro sin tocar el esquema.

El separador es ':' porque el order_id de Amazon ya contiene guiones
(702-xxxxxxx-xxxxxxx), asi que partir por el primer ':' es inequivoco.
El loader recupera el settlement_id con split(':', 1)[0].


MARCA DE AGUA
-------------
No se usa finance.mkp_sync_watermarks: esa tabla nace con el proceso de notas
de credito de Mercado Libre y todavia no existe. Tampoco hace falta. El
MAX(date_released) ya registrado en la propia tabla de cobros ES la marca de
agua: menos estado que mantener y no se puede desincronizar.


El desglose (base, IVA, comisiones, piezas) no tiene columna aqui. Vive en el
raw_json de finance.mkp_billing_*, que el loader lee para el chatter.


===============================================================================
LA DEDUPLICACION ES EL CORAZON DE ESTE SCRIPT
===============================================================================
Una misma venta aparece varias veces en amz_transactions, por dos motivos:

  (a) CICLO DE VIDA DEL PAGO. La PK incluye transaction_status. Amazon publica
      la transaccion como DEFERRED (fondos retenidos) y despues como
      RELEASED / DEFERRED_RELEASED. Ambas filas conviven.

  (b) RE-PUBLICACION EN OTRO SETTLEMENT. Aun filtrando las DEFERRED, la misma
      pieza puede aparecer en dos settlements con deposit_date distinto.

Caso real -- orden 702-2265086-8559438 (2 piezas del SKU KHCESPIB), ya
filtrando DEFERRED, devuelve TRES filas:

  order_adjustment_item_id  settlement     status             posted  deposito
  ------------------------  -----------    -----------------  ------  --------
  165346845825161           27044880571    DEFERRED_RELEASED  07-24   07-30
  164910950935841           27044880571    DEFERRED_RELEASED  07-27   07-30
  165346845825161           27172333181    RELEASED           07-30   08-06
  ^^^^^^^^^^^^^^^ misma pieza que la primera fila, re-publicada

Solo hay DOS piezas reales. Lo confirma amz_b2b_invoices: Amazon emitio dos
CFDI de 1,379.00 (= 1,188.79 + 190.21), uno por pieza.

Sin deduplicar, la pieza 165346845825161 se cobraria dos veces. Amazon la
deposito una sola vez.

REGLA APLICADA: la fila autoritativa de cada PIEZA (shipment_id) es la de
posted_date mas reciente. Ese posted_date determina en que settlement (y por
lo tanto en que deposito) cae el cobro.

CORRECCION 25-sep-2026 -- la llave era order_adjustment_item_id y estaba mal.
Es la misma que 01_amz_billing.py abandono en su CORRECCION 3: dos envios de
una orden pueden compartir order_adjustment_item_id, y agrupar por esa columna
los colapsa en uno. 01 facturaba las dos piezas y 02 cobraba solo una. Casos
de la prueba masiva del 18-sep (query de revision: piezas por shipment_id = 2,
llaves order_adjustment_item_id = 1, un solo settlement):

  701-1847796-4357045  2 facturas B2B de 724.22   cobro sembrado 724.22
                       (settlement 27298618171)   -> la segunda quedo sin pagar
  702-0736782-6924263  1 factura de 4,498.00      cobro sembrado 2,249.00
                       (settlement 27358358891)   -> quedo pagada a medias

Las dos piezas estaban liberadas (RELEASED) en el mismo settlement: no era un
cobro parcial, era dinero depositado que no se aplicaba. Ahora 02 deduplica
exactamente igual que 01, por COALESCE(shipment_id, transaction_id). Las dos
queries tienen que usar la misma llave: si divergen, lo facturado y lo
cobrado dejan de ser comparables.

  >>> PENDIENTE DE VALIDAR CON FINANZAS <<<
  Contrastar un settlement completo contra el extracto bancario. Es la unica
  forma de confirmar que "gana el posted_date mas reciente" reproduce el
  importe realmente depositado.
  Solicitados: deposito del 30-jul (settlement 27044880571) y del 06-ago
  (settlement 27172333181). Al 10-sep-2026 sin respuesta.


===============================================================================
COBROS PARCIALES -- CORRIGE UN SUPUESTO DEL FLUJO DE FINANZAS
===============================================================================
El Paso 6 del flujo dice: "el cobro se aplica por el total de la factura,
saldando la CxC por completo". Eso supone que cada factura pertenece a un solo
settlement.

En el ejemplo de arriba las dos piezas de la misma orden caen en settlements
DISTINTOS: 1,379.00 el 30-jul y 1,379.00 el 06-ago. La factura de 2,758.00
recibe dos cobros y se salda con el segundo.

Odoo maneja pagos parciales sin problema. El supuesto del documento es el que
hay que corregir.
"""

import os
import re
import sys
import time as tm
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, TBL_PAYMENTS, TBL_BILLING, MARKETPLACE,
    banner, notify_slack, get_db_connection, a_float,
    build_payment_id,
)

# =============================================================================
# CONFIGURACION
# =============================================================================

# Arranque cuando la tabla de cobros esta vacia. Filtra por DEPOSIT_DATE, no
# por posted_date: tiene que ser anterior al primer deposito de la ventana de
# facturacion. Para la ventana de pruebas (01_amz_billing.py: 2026-07-20 ..
# 2026-08-26) el primer deposito es el del settlement 27044880571, el
# 30-jul-2026, asi que 2026-07-20 lo cubre con holgura.
#
# En cuanto hay cobros registrados esta constante deja de usarse: la marca de
# agua sale de MAX(date_released) de la propia tabla.
WATERMARK_INICIAL = os.getenv('AMZ_PAYMENTS_DESDE', '2026-07-20 00:00:00')

# =============================================================================
# ALCANCE: TODOS LOS COBROS, O SOLO LOS DE ORDENES YA FACTURADAS
# =============================================================================
# En PRUEBAS -> True. Sin esto, un lote de prueba de 20 facturas es
# inservible: la query lee por deposit_date, no por orden, asi que sembraria
# cientos de cobros de todos los settlements de la ventana y el LIMIT del
# loader (ordenado por date_released) tomaria los mas antiguos, que casi nunca
# son los del lote de prueba. Resultado: 20 filas en NO_INVOICE y cero cobros
# aplicados. La prueba no probaria nada.
#
# En PRODUCCION -> False, a proposito, y es una diferencia deliberada con
# Mercado Libre. Alla el miner recorre orden por orden las ya facturadas
# (03_miner_ml_payments.py) porque cada pago de Mercado Pago es independiente.
# En Amazon la unidad de deposito es el SETTLEMENT: un solo movimiento
# bancario que agrupa cientos de ordenes. El resumen "POR SETTLEMENT" de este
# script es el documento con el que Finanzas cuadra 102.02.008 contra el
# extracto, y solo cuadra si el settlement esta COMPLETO. Filtrando a las
# facturadas, ese total sale corto por construccion y la conciliacion deja de
# servir. Ademas, una orden liquidada por Amazon y sin factura en Odoo es
# justo la fuga que Finanzas necesita ver, no una fila que conviene esconder.
#
# Contrapartida asumida: en produccion habra filas que terminen en NO_INVOICE.
# Por eso 03_loader_amz_payments.py las REINTENTA en vez de darlas por
# cerradas (ver NO_INVOICE_RETRY_HOURS alli): una orden puede cobrarse antes
# de que exista su factura, y ese cobro no se puede perder.
# =============================================================================

SOLO_FACTURADAS = os.getenv(
    'AMZ_PAYMENTS_SOLO_FACTURADAS',
    'true' if ENTORNO == 'staging' else 'false').lower().strip() in ('1', 'true', 'si')

# Estados de mkp_billing_* que cuentan como "tiene factura viva en Odoo".
# Incluye ALREADY_ODOO_INVOICED igual que 03_miner_ml_payments.py: una orden
# facturada por otra via sigue necesitando que se le aplique el cobro.
#
# B2B_FACTURADA_PG tambien: la orden tiene una factura viva a publico general
# (pendiente de anular para vincular el CFDI de Amazon) y el dinero ya llego.
STATUS_FACTURADAS = ('ODOO_INVOICED', 'ODOO_B2B_LINKED', 'ALREADY_ODOO_INVOICED',
                     'B2B_FACTURADA_PG')

# Primera corrida en produccion: un settlement completo (correo de Jero,
# 28-oct-2026). Con AMZ_SETTLEMENT_ID se leen solo los cobros de ese
# settlement y la marca de agua no aplica. Debe ir con el mismo valor que en
# 01_amz_billing.py.
AMZ_SETTLEMENT_ID = os.getenv('AMZ_SETTLEMENT_ID', '').strip() or None

# Ordenes sueltas de pruebas (mismas que en 01): sus cobros entran aunque
# esten en otro settlement, siempre que ese settlement ya este depositado.
AMZ_ORDER_IDS = [o.strip() for o in os.getenv('AMZ_ORDER_IDS', '').split(',') if o.strip()]

# Con alcance, se leen depositos desde SETTLEMENT_MARGEN_DIAS antes del
# deposito del settlement, para alcanzar los cobros de las ordenes sueltas.
SETTLEMENT_MARGEN_DIAS = 60

# Margen hacia atras sobre la marca de agua: Amazon puede re-publicar una
# transaccion despues de que la procesamos.
LOOKBACK_DAYS = 15

# Margen de la subquery de deduplicacion. Las re-publicaciones ocurren dentro
# de ~15-45 dias; 90 da holgura y evita recorrer todo el historico.
DEDUP_LOOKBACK_DAYS = 90


# =============================================================================
# QUERY DE COBROS
# =============================================================================
# NOTA: somos_reyes.amazon_orders NO se usa aqui. En la query original del
# reporte entraba sin deduplicar y multiplicaba todos los SUM() por su numero
# de filas por orden. Para cobros no aporta nada.
# =============================================================================

QUERY_COBROS = """
SELECT
    a.settlement_id,
    a.order_id                                   AS mkp_order_id,
    c.so                                         AS odoo_so_name,
    e.deposit_date,
    MAX(a.posted_date)                           AS posted_date,
    COUNT(*)                                     AS n_items,

    -- Importe del cobro a aplicar en Odoo (factura con IVA).
    -- Se usa shipping_discount (el detalle) y NO promo_rebates (el roll-up):
    -- ambos traen el mismo importe y sumarlos descuadra la factura.
    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)
            + IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                                 AS cfdi_total,

    -- Comisiones (negativas). Diferencia entre lo facturado y lo depositado;
    -- explica el saldo de 102.02.008 contra el extracto bancario.
    -- amazon_fees YA incluye amazon_for_all_fee y shipping_hb: sumarlos
    -- aparte los contaba dos veces. cfdi_total + fees_total = amount_deposited.
    ROUND(SUM(IFNULL(b.amazon_fees,0) + IFNULL(b.marketplace_vat_principal,0)
            + IFNULL(b.marketplace_vat_shipping,0)), 2)       AS fees_total,

    ROUND(SUM(IFNULL(b.item_amount,0)), 2)       AS amount_deposited

FROM somos_reyes.amz_transactions a

JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id

-- Fila autoritativa por PIEZA (ver encabezado del archivo). Misma llave que
-- 01_amz_billing.py (CORRECCION 3): COALESCE(shipment_id, transaction_id).
-- NO usar order_adjustment_item_id: dos envios pueden compartirlo y se
-- colapsan en uno.
JOIN (
    SELECT COALESCE(a2.shipment_id, a2.transaction_id) AS pieza,
           MAX(a2.posted_date) AS max_posted
    FROM somos_reyes.amz_transactions a2
    WHERE a2.transaction_status <> 'DEFERRED'
      AND a2.description = 'Order Payment'
      AND a2.posted_date >= DATE_SUB(%s, INTERVAL %s DAY)
    GROUP BY COALESCE(a2.shipment_id, a2.transaction_id)
) dedup
    ON dedup.pieza = COALESCE(a.shipment_id, a.transaction_id)
   AND dedup.max_posted = a.posted_date

LEFT JOIN (
    SELECT marketplace_order_id, MAX(order_id) AS so
    FROM somos_reyes.odoo_master_txns_c
    GROUP BY marketplace_order_id
) c ON a.order_id = c.marketplace_order_id

JOIN (
    SELECT settlement_id, MAX(deposit_date) AS deposit_date
    FROM somos_reyes.amz_settlement_report
    GROUP BY settlement_id
) e ON a.settlement_id = e.settlement_id

{JOIN_FACTURADAS}

WHERE a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'
  AND e.deposit_date >= %s
  AND e.deposit_date <= NOW()
  {FILTRO_SETTLEMENT}

GROUP BY a.settlement_id, a.order_id, c.so, e.deposit_date
HAVING cfdi_total > 0
ORDER BY e.deposit_date, a.settlement_id, a.order_id
"""


def construir_query():
    """Arma la query final segun el alcance.

    El fragmento va SIN placeholders %s para no alterar el orden de los tres
    parametros posicionales que ya tiene QUERY_COBROS. La lista de estados se
    interpola porque es una constante del codigo, no un dato de entrada.
    """
    # Settlement y ordenes vienen de la configuracion; se validan contra su
    # formato (digitos / 999-9999999-9999999) antes de interpolarlos.
    filtro = ''
    if AMZ_SETTLEMENT_ID:
        if not AMZ_SETTLEMENT_ID.isdigit():
            raise Exception(f"AMZ_SETTLEMENT_ID invalido: '{AMZ_SETTLEMENT_ID}'")
        partes = [f"a.settlement_id = '{AMZ_SETTLEMENT_ID}'"]
        if AMZ_ORDER_IDS:
            malas = [o for o in AMZ_ORDER_IDS if not re.fullmatch(r'\d{3}-\d{7}-\d{7}', o)]
            if malas:
                raise Exception(f"AMZ_ORDER_IDS invalidos: {malas}")
            partes.append(f"a.order_id IN ({', '.join(repr(o) for o in AMZ_ORDER_IDS)})")
        filtro = f"AND ({' OR '.join(partes)})"
    sql = QUERY_COBROS.replace('{FILTRO_SETTLEMENT}', filtro)

    if not SOLO_FACTURADAS:
        return sql.replace('{JOIN_FACTURADAS}', '')

    estados = ', '.join(f"'{s}'" for s in STATUS_FACTURADAS)
    join = (f"-- ALCANCE ACOTADO: solo ordenes ya facturadas en Odoo\n"
            f"JOIN {TBL_BILLING} fac\n"
            f"    ON fac.marketplace = '{MARKETPLACE}'\n"
            f"   AND fac.mkp_order_id = a.order_id\n"
            f"   AND fac.status IN ({estados})")
    return sql.replace('{JOIN_FACTURADAS}', join)


# =============================================================================
# MARCA DE AGUA -- derivada de la propia tabla de cobros
# =============================================================================

def get_watermark(db_conn):
    """MAX(date_released) ya registrado. Si la tabla esta vacia, arranca desde
    WATERMARK_INICIAL."""
    cursor = db_conn.cursor()
    try:
        cursor.execute(f"""
            SELECT MAX(date_released) FROM {TBL_PAYMENTS}
            WHERE marketplace = %s
        """, (MARKETPLACE,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    except Exception as e:
        log.warning(f"No se pudo leer la marca de agua ({e}). "
                    f"Se usa {WATERMARK_INICIAL}.")
    finally:
        cursor.close()
    return WATERMARK_INICIAL


def aplicar_lookback(watermark):
    if isinstance(watermark, datetime):
        dt = watermark
    else:
        dt = datetime.strptime(str(watermark)[:19], '%Y-%m-%d %H:%M:%S')
    return (dt - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# CARGA
# =============================================================================

def contar_settlements_previos(cursor, order_id, payment_id):
    """Cobros de la misma orden en OTROS settlements.

    No es un error: es el caso de cobro parcial. Se registra para poder
    reportarlo, porque contradice el supuesto del flujo de Finanzas.
    """
    cursor.execute(f"""
        SELECT payment_id, amount, status FROM {TBL_PAYMENTS}
        WHERE marketplace = %s AND mkp_order_id = %s AND payment_id <> %s
    """, (MARKETPLACE, order_id, payment_id))
    return cursor.fetchall()


def upsert_cobros(db_conn, rows):
    """Inserta o refresca los cobros.

    Con UNIQUE KEY uk_payment (marketplace, payment_id) se puede usar
    ON DUPLICATE KEY UPDATE, que es atomico.

    El UPDATE solo toca filas que siguen en PENDING o ERROR: si el cobro ya se
    aplico en Odoo (ODOO_PAID), una re-lectura de Amazon NO debe reabrirlo. Esa
    condicion es la defensa contra el doble cobro.
    """
    cursor = db_conn.cursor()
    res = {'insertados': 0, 'refrescados': 0, 'protegidos': 0,
           'parciales': [], 'sin_so': [], 'envio_oculto': []}

    for r in rows:
        order_id = r['mkp_order_id']
        settlement_id = str(r['settlement_id'])
        payment_id = build_payment_id(settlement_id, order_id)
        importe = a_float(r['cfdi_total'])

        # Envio acreditado FUERA de los campos de envio (correo de Jero del
        # 28-oct, segundo, 701-4769487-1597824): Amazon deposito 221.32 contra 166.39
        # de venta - comisiones - IVA retenido. La diferencia, 54.93, es un
        # envio de 59.00 menos los 4.07 de IVA que Amazon retiene sobre el.
        # El cobro es lo que Amazon acredito, asi que se suma el envio. 3
        # ordenes asi entre sep y oct.
        excedente = round(a_float(r['amount_deposited']) - importe
                          - a_float(r['fees_total']), 2)
        if excedente > 0.005:
            oculto = round(excedente / (1 - 0.08 / 1.16), 2)
            importe = round(importe + oculto, 2)
            res['envio_oculto'].append(f"{order_id} / {settlement_id}: envio {oculto:,.2f}")

        if not r['odoo_so_name']:
            res['sin_so'].append(order_id)

        previos = contar_settlements_previos(cursor, order_id, payment_id)
        if previos:
            res['parciales'].append(
                f"{order_id}: {settlement_id} + "
                f"{', '.join(str(p[0]).split(':', 1)[0] for p in previos)}")

        # NOTA DE ESQUEMA: finance.mkp_payments_* NO tiene columna
        # odoo_so_name. La SO vive solo en finance.mkp_billing_*, y el loader
        # la obtiene uniendo por (marketplace, mkp_order_id). Es como lo hace
        # Mercado Libre en produccion: 03_miner_ml_payments.py inserta sin esa
        # columna y 04_loader_ml_payments.py la trae con un JOIN.
        cursor.execute(f"""
            INSERT INTO {TBL_PAYMENTS}
                (marketplace, mkp_order_id, payment_id,
                 amount, date_released, status)
            VALUES (%s, %s, %s, %s, %s, 'PENDING')
            ON DUPLICATE KEY UPDATE
                amount        = IF(status IN ('PENDING','ERROR'), VALUES(amount),        amount),
                date_released = IF(status IN ('PENDING','ERROR'), VALUES(date_released), date_released)
        """, (MARKETPLACE, order_id, payment_id, importe, r['deposit_date']))
        db_conn.commit()

        # rowcount: 1 = insert, 2 = update efectivo, 0 = sin cambios
        if cursor.rowcount == 1:
            res['insertados'] += 1
        elif cursor.rowcount == 2:
            res['refrescados'] += 1
        else:
            res['protegidos'] += 1

    cursor.close()
    return res


# =============================================================================
# MAIN
# =============================================================================

def run():
    db_conn = get_db_connection()

    if AMZ_SETTLEMENT_ID:
        cur = db_conn.cursor()
        cur.execute("""SELECT MAX(deposit_date) FROM somos_reyes.amz_settlement_report
                       WHERE settlement_id = %s""", (AMZ_SETTLEMENT_ID,))
        fila = cur.fetchone()
        cur.close()
        if not fila or not fila[0]:
            raise Exception(f"El settlement {AMZ_SETTLEMENT_ID} no tiene fecha de "
                            f"deposito: todavia no se libera.")
        deposito = fila[0]
        desde = ((deposito - timedelta(days=SETTLEMENT_MARGEN_DIAS)) if AMZ_ORDER_IDS
                 else deposito).strftime('%Y-%m-%d %H:%M:%S')
        log.info(f"ALCANCE: settlement {AMZ_SETTLEMENT_ID} (depositado {deposito})"
                 f"{f' + {len(AMZ_ORDER_IDS)} orden(es) sueltas' if AMZ_ORDER_IDS else ''}. "
                 f"Depositos desde {desde}. La marca de agua no aplica.")
    else:
        watermark = get_watermark(db_conn)
        desde = aplicar_lookback(watermark)
        log.info(f"Marca de agua: {watermark} | leyendo desde {desde} "
                 f"(lookback {LOOKBACK_DAYS} dias)")

    if SOLO_FACTURADAS:
        log.warning(f"ALCANCE ACOTADO: solo ordenes con factura en Odoo "
                    f"({', '.join(STATUS_FACTURADAS)}). Los totales POR "
                    f"SETTLEMENT de abajo NO cuadran contra el extracto "
                    f"bancario: son parciales por diseno.")
    else:
        log.info("ALCANCE COMPLETO: todos los cobros liberados de la ventana.")

    t0 = tm.time()
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(construir_query(), (desde, DEDUP_LOOKBACK_DAYS, desde))
    rows = cursor.fetchall()
    cursor.close()
    log.info(f"{len(rows)} fila(s) leidas en {round(tm.time() - t0, 1)}s")

    if not rows:
        log.info("No hay settlements liberados nuevos.")
        db_conn.close()
        return

    settlements = {r['settlement_id'] for r in rows}
    total_cobro = sum(a_float(r['cfdi_total']) for r in rows)
    total_fees = sum(a_float(r['fees_total']) for r in rows)
    total_neto = sum(a_float(r['amount_deposited']) for r in rows)

    log.info(f"Settlements              : {len(settlements)}")
    log.info(f"Ordenes a cobrar         : {len(rows)}")
    log.info(f"Total a aplicar (c/IVA)  : {total_cobro:,.2f}")
    log.info(f"Comisiones Amazon        : {total_fees:,.2f}")
    log.info(f"Neto depositado          : {total_neto:,.2f}")

    res = upsert_cobros(db_conn, rows)

    log.info('')
    log.info(f"Insertados   : {res['insertados']}")
    log.info(f"Refrescados  : {res['refrescados']}")
    if res['envio_oculto']:
        log.info(f"Con envio fuera de los campos de envio (sumado al cobro): "
                 f"{len(res['envio_oculto'])}")
        for e in res['envio_oculto']:
            log.info(f"  {e}")
    log.info(f"Protegidos   : {res['protegidos']}  (ya cobrados en Odoo)")

    if res['sin_so']:
        log.warning(f"{len(res['sin_so'])} orden(es) sin SO en Odoo. Se "
                    f"registran para que el loader las reporte; NO se crean.")

    if res['parciales']:
        log.info('')
        log.warning(f"{len(res['parciales'])} orden(es) con cobro repartido en "
                    f"varios settlements:")
        for p in res['parciales'][:10]:
            log.info(f"  {p}")
        log.info("  Esperado: Amazon libera por pieza. La factura se salda con "
                 "el ultimo cobro.")

    # Resumen por settlement: es lo que Finanzas cruza contra el banco.
    log.info('')
    log.info('POR SETTLEMENT (para conciliar contra el extracto bancario):')
    por_settlement = {}
    for r in rows:
        s = str(r['settlement_id'])
        acc = por_settlement.setdefault(
            s, {'dep': r['deposit_date'], 'n': 0, 'cobro': 0.0, 'neto': 0.0})
        acc['n'] += 1
        acc['cobro'] += a_float(r['cfdi_total'])
        acc['neto'] += a_float(r['amount_deposited'])
    for s, v in sorted(por_settlement.items(), key=lambda x: str(x[1]['dep'])):
        log.info(f"  {s:<14} deposito={str(v['dep'])[:10]}  "
                 f"ordenes={v['n']:>4}  facturado={v['cobro']:>14,.2f}  "
                 f"neto={v['neto']:>14,.2f}")

    db_conn.close()

    if res['parciales'] and ENTORNO == 'prod':
        notify_slack(
            f":information_source: {len(res['parciales'])} orden(es) de Amazon "
            f"con cobro repartido en varios settlements. Comportamiento "
            f"esperado: la CxC se salda con el ultimo cobro.")


if __name__ == '__main__':
    banner('MINER DE COBROS -- AMAZON')
    t0 = tm.time()
    try:
        run()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico en el miner de cobros: {e}')
        sys.exit(1)
