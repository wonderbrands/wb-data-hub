"""
01_amz_billing.py
-----------------
FACTURACION AMAZON -- MINER + LOADER

Reescrito sobre la arquitectura de miner -> staging -> loader, la misma de
Mercado Libre (01_miner_ml_billing + 02_loader_ml_billing).


===============================================================================
POR QUE SE REESCRIBIO
===============================================================================
La version anterior arrancaba de Odoo (sale.order con team Amazon) y SALTABA
las ordenes B2B sin crear nada. Eso contradice el flujo de Finanzas, Paso 3:

    "B2B - Amazon ya facturo -> Jalar CFDI de Amazon a Odoo.
     Vincular a la SO correspondiente. Mismo mecanismo que ML."

Saltarlas deja la SO sin factura en Odoo: sin cuenta por cobrar y sin
reconocimiento de ingreso. Ahora las B2B SI se crean en Odoo, sellandoles el
UUID que emitio Amazon, tal como 02_loader_ml_billing.py hace con el CFDI de
Mercado Libre.

Tambien cambio la fuente de verdad: ahora es somos_reyes.amz_transactions.
Consecuencia conocida: las SOs de Odoo que nunca aparecen en las transacciones
de Amazon quedan fuera del alcance de este script. Ese hueco lo cubre
05_amz_orders_control.py, que las detecta y alerta.


===============================================================================
LAS TRES REGLAS QUE PIDIO FINANZAS
===============================================================================
  1. Distinguir B2B. Se detecta por la existencia real del CFDI en
     somos_reyes.amz_b2b_invoices, no por la marca IsBusinessOrder: preferimos
     verificar que el comprobante exista antes que asumir que deberia existir.

  2. Sin restriccion de OUT. Se factura sobre la cantidad PEDIDA, haya
     movimiento de salida o no. La politica de Odoo ya esta en "cantidades
     ordenadas" y la contrapartida cae en Mercancia Facturada No Enviada. Si
     operaciones deja ordenes sin surtir, esa cuenta se infla: es la senal que
     Finanzas quiere ver.

  3. Factura individual por operacion, y verificacion de que la SO no este ya
     facturada.


===============================================================================
BLINDAJE ANTI DOBLE FACTURACION
===============================================================================
El incidente del 26-jul-2026 (549 ordenes con doble CFDI por dos ejecuciones
traslapadas en Kestra) obliga a:

  1. EXCLUSION MUTUA EN KESTRA. concurrency: limit 1, behavior FAIL.
     Es la defensa principal y vive en el YAML, no aqui.
  2. CANDADO EN BD. El paso a PROCESSING es un UPDATE condicionado: solo una
     corrida lo gana.
  3. CLAVE COMPARTIDA. ref = 'AUTOINV:<SO>', el mismo prefijo del 1 a 1.
  4. RE-VALIDACION FAIL-CLOSED antes del create. Si la comprobacion falla, NO
     se crea.
  5. ESCRITURAS SIN REINTENTO CIEGO (OdooWriteUncertain).


===============================================================================
ESTADOS EN finance.mkp_billing_{staging|prod}
===============================================================================
  PENDING_B2B_LINK       Amazon ya facturo; falta vincular en Odoo
  PENDING_WB_INVOICE     nos toca emitir el CFDI
  PROCESSING             tomada por una corrida (candado)
  ODOO_B2B_LINKED        factura creada con el UUID de Amazon sellado
  ODOO_INVOICED          CFDI emitido por WB
  ALREADY_ODOO_INVOICED  ya tenia factura viva en Odoo
  SKIPPED_GRACE          dentro del periodo de gracia
  TOTAL_DIFF             el importe de Odoo no cuadra con el de Amazon. NO se
                         factura; sale en el reporte de Finanzas.
  B2B_SPLIT_MISMATCH     Amazon emitio varios CFDI y no se pudieron repartir
                         contra los envios / las lineas de la SO.
  B2B_NO_RECEPTOR        el CFDI de Amazon no trae RFC receptor utilizable
  SO_CANCELLED           SO cancelada. Requiere nota de credito Tipo A.
  SO_DRAFT               SO en borrador. Fuera de alcance.
  NO_SO_IN_ODOO          sin SO cruzada. Solo alerta: las crea Yuju.
  ERROR                  fallo recuperable
"""

import os
import sys
import json
import time as tm
import logging
from datetime import datetime, timedelta
import xmlrpc.client

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# Import del modulo de configuracion compartido (misma carpeta).
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, TBL_BILLING, MARKETPLACE,
    TAX_ID_MARKETPLACES, PARTNER_ID_PUBLICO_GENERAL, ACCOUNT_AMAZON_CXC,
    IDEMPOTENCY_PREFIX, KESTRA_EXECUTION_ID,
    validar_entorno, banner, notify_slack, get_db_connection,
    OdooModelProxy, OdooWriteUncertain, get_account_id, a_float,
)

# =============================================================================
# CONFIGURACION
# =============================================================================

RUN_MINER = True
RUN_LOADER = True

# Ventana de arranque. Blindaje contra facturacion masiva retroactiva: al
# quitar la restriccion de OUT, todo el rezago se vuelve elegible de golpe.
# >>> DEFINIR CON FINANZAS ANTES DE PASAR A PRODUCCION <<<
# Valor de PRUEBAS: inicio del settlement 27044880571 (depositado el 30-jul).
# Ver la nota de AMZ_END_DATE para por que se eligio esta ventana.
AMZ_START_DATE = os.getenv('AMZ_START_DATE', '2026-07-20')

# =============================================================================
# LIMITE SUPERIOR DE VENTANA -- HERRAMIENTA DE PRUEBAS
# =============================================================================
# Vacio en produccion: se factura hasta hoy.
#
# En pruebas hace falta porque la base de Odoo es un ESPEJO CONGELADO al
# 26-ago-2026. Cualquier orden cuya SO se creo despues no existe ahi, y el
# loader la reporta como NO_SO_IN_ODOO aunque en produccion si exista: en la
# corrida del 11-sep fueron 73 de 120. Acotando la ventana por arriba el lote
# de prueba cae dentro de lo que el espejo si conoce.
#
# Frontera EXCLUSIVA (<), igual que el resto del proyecto: con <= una orden
# justo en el limite se procesa en dos lotes.
#
# VENTANA DE PRUEBAS ELEGIDA: 2026-07-20 .. 2026-08-26
# ----------------------------------------------------
# No es arbitraria. Tiene que cumplir tres condiciones a la vez:
#
#   1. Que la SO exista en el espejo de Odoo, congelado al 26-ago-2026. De ahi
#      el limite superior: una orden posterior no tiene SO ahi y sale como
#      NO_SO_IN_ODOO aunque en produccion si exista (73 de 120 en la corrida
#      del 11-sep).
#   2. Que el settlement YA este depositado, o 02_miner_amz_payments.py no
#      encuentra nada que cobrar y no se puede probar el 03.
#   3. Que los settlements sean CONSECUTIVOS. Si la ventana parte por la mitad
#      una orden cuyas piezas cayeron en dos settlements, se factura solo una
#      parte y la orden acaba en TOTAL_DIFF por un descuadre que no existe.
#
# La ventana cubre cinco settlements consecutivos, todos ya depositados:
#
#   settlement     periodo            deposito     ordenes
#   27044880571    07-20 .. 07-28     30-jul        6,261
#   27172333181    07-28 .. 08-04     06-ago        3,961
#   27241792841    08-04 .. 08-11     13-ago        3,310
#   27298618171    08-11 .. 08-17     19-ago        3,025
#   27358358891    08-17 .. 08-25     27-ago        3,613
#
# Empieza justo en el arranque del primero para no cortar ninguna orden por el
# borde inferior. Ademas incluye el caso 702-2265086-8559438 del CLAUDE.md,
# repartido entre 27044880571 y 27172333181, que es el que permite ver el
# COBRO PARCIAL en 03_loader_amz_payments.py.
#
# El valor por defecto SOLO aplica en staging. En produccion el defecto es
# vacio: un tope superior heredado por descuido dejaria de facturar todo lo
# posterior a esa fecha SIN ERROR NI ALERTA, que es la peor clase de fallo.
# Sigue siendo sobreescribible por variable de entorno para un reproceso
# puntual, pero entonces hay que ponerlo a proposito.
# =============================================================================

_END_DATE_DEFECTO = '2026-08-26' if ENTORNO == 'staging' else ''
AMZ_END_DATE = os.getenv('AMZ_END_DATE', _END_DATE_DEFECTO).strip() or None

# Dias que se le dan al cliente para autofacturar con su RFC antes de que
# emitamos a publico en general.
GRACE_DAYS = int(os.getenv('AMZ_GRACE_DAYS', '5'))

# Margen hacia atras en la subquery de deduplicacion. Las re-publicaciones de
# Amazon ocurren dentro de ~15-45 dias; 90 da holgura y evita que el GROUP BY
# recorra todo el historico en cada corrida.
DEDUP_LOOKBACK_DAYS = 90

# Timbrado. En pruebas se deja apagado: publicar la factura ya genera el
# asiento, que es lo que Finanzas necesita revisar.
DO_STAMPING = (ENTORNO == 'prod')

# =============================================================================
# CUOTA DE PRUEBAS -- SE CUENTAN FACTURAS CREADAS, NO CANDIDATOS LEIDOS
# =============================================================================
# Un LIMIT N en la query limita candidatos. Y un candidato puede acabar en
# NO_SO_IN_ODOO, SO_CANCELLED, SO_DRAFT o ALREADY_ODOO_INVOICED sin producir
# ninguna factura: se pedian 20 y salian 6, sin forma de saber por que.
#
# Con cuotas, el loader sigue avanzando hasta juntar el numero pedido de
# facturas REALES de cada tipo, o hasta agotar candidatos (y entonces lo dice).
#
# Ademas la cuota es POR TIPO. El limite conjunto anterior no garantizaba
# ninguna B2B: como la query ordena por posted_date DESC y las B2B son
# minoria, una corrida de prueba podia terminar con 20 B2C y cero B2B, que es
# justo el caso que hay que probar.
#
# En produccion ambas son None: sin limite, se factura todo lo elegible.
# =============================================================================

QUOTA_B2B = None if ENTORNO == 'prod' else int(os.getenv('AMZ_TEST_QUOTA_B2B', '50'))
QUOTA_B2C = None if ENTORNO == 'prod' else int(os.getenv('AMZ_TEST_QUOTA_B2C', '50'))

# El miner tiene que sembrar MAS candidatos de los que el loader necesita:
# parte se caera en SO cancelada, sin SO o ya facturada.
#
# El 3x inicial era una suposicion y quedo corto. Medido en la corrida del
# 11-sep sobre la ventana 2026-07-20..2026-08-26: de 60 candidatos sembrados,
# 54 ya tenian factura viva en Odoo, 1 estaba cancelada y 1 ya facturada por
# otra via. Solo 4 produjeron factura: una merma del 93%.
#
# La causa no es un defecto: en una ventana pasada, facturacion_automatica_1a1
# ya recorrio esas ordenes en produccion y el espejo del 26-ago arrastra sus
# facturas. Lo que queda sin facturar son sobre todo las que el 1a1 salto por
# la restriccion de OUT, que es justamente la poblacion que este proceso viene
# a cubrir, y es minoritaria.
#
# 10x cuesta poco (la siembra midio ~0.22 s por candidato) y da margen real.
MINER_OVERFETCH = int(os.getenv('AMZ_MINER_OVERFETCH', '3'))

# Estados desde los que una fila de la tabla de control TODAVIA se puede
# reevaluar. El miner refresca estas; las demas (ya facturadas, canceladas,
# TOTAL_DIFF...) se dan por cerradas y una relectura de Amazon no las reabre.
REFRESCABLES = ('PENDING_B2B_LINK', 'PENDING_WB_INVOICE', 'SKIPPED_GRACE', 'ERROR')

# Centinela: armar_partes_split lo devuelve cuando la SO no tiene nada
# pendiente. No es un error de reparto, es una orden ya facturada.
SIN_LINEAS_PENDIENTES = 'SIN_LINEAS_PENDIENTES'

STUCK_PROCESSING_MIN_AGE_HOURS = 6
LOG_PROGRESS_EVERY = 25

# =============================================================================
# TOLERANCIA DE IMPORTES
# =============================================================================
# Se compara lo que Odoo va a facturar contra la referencia de Amazon (el CFDI
# de Amazon si es B2B, la suma de transacciones liberadas si es B2C) ANTES de
# crear la factura. Si no cuadran, NO se factura: se deja en TOTAL_DIFF para
# que Finanzas lo revise. Un CFDI mal emitido no se corrige con un UPDATE.
#
# Un peso, igual que 02_loader_ml_billing.py. Absorbe el centavo que produce
# el redondeo por linea de Odoo (una B2B de 2,110.00 en Amazon sale 2,110.01
# en Odoo) sin dejar pasar diferencias de precio reales, que en la muestra
# fueron de 99.99.
# =============================================================================

TOLERANCIA_TOTAL = float(os.getenv('AMZ_TOLERANCIA_TOTAL', '1.00'))


# =============================================================================
# FASE A -- MINER
# =============================================================================
# Una sola pasada. La clasificacion B2B / B2C se hace en Python: correr la
# query dos veces (una por caso) duplicaba el tiempo sin ganar nada.
#
# Aplica las tres correcciones validadas contra Reporte_3_0_BI.csv:
#   1. amazon_orders deduplicado -> sin el, todos los SUM() se multiplican
#      (49 de 94 ventas de la muestra salian al doble)
#   2. shipping_discount y NO promo_rebates -> son el mismo descuento
#   3. dedup por order_adjustment_item_id, gana el posted_date mas reciente
#      -> sin el, una pieza re-publicada en dos settlements se factura dos veces
# =============================================================================

QUERY_LOTE = """
SELECT
    a.order_id                                    AS mkp_order_id,
    c.so                                          AS odoo_so_name,
    MAX(a.settlement_id)                          AS settlement_id,
    MAX(e.deposit_date)                           AS deposit_date,
    MAX(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) AS posted_date,
    MAX(IFNULL(d.IsBusinessOrder, 0))             AS is_business_order,
    COUNT(*)                                      AS n_items,
    COUNT(DISTINCT COALESCE(a.shipment_id, a.transaction_id)) AS n_envios,
    GROUP_CONCAT(DISTINCT b.sku ORDER BY b.sku SEPARATOR ', ') AS skus,
    MAX(b.fulfillment_network)                    AS fulfillment_network,

    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)), 2)
                                                  AS cfdi_base,
    ROUND(SUM(IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                                  AS cfdi_iva,
    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)
            + IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                                  AS cfdi_total,
    ROUND(SUM(IFNULL(b.amazon_fees,0) + IFNULL(b.marketplace_vat_principal,0)
            + IFNULL(b.marketplace_vat_shipping,0) + IFNULL(b.amazon_for_all_fee,0)
            + IFNULL(b.shipping_hb,0)), 2)        AS fees_total,

    f.uuid       AS amz_cfdi_uuid,
    f.uuids      AS amz_cfdi_uuids,
    f.n_cfdi     AS amz_cfdi_count,
    f.total      AS amz_cfdi_total,
    f.subtotal   AS amz_cfdi_subtotal,
    f.discount   AS amz_cfdi_discount,
    f.taxes      AS amz_cfdi_taxes,
    f.receiver_rfc, f.receiver_name,
    f.receiver_cfdi_key, f.receiver_tax_regime, f.receiver_zip,
    f.forma_pago, f.metodo_pago,
    f.date       AS amz_cfdi_date,
    f.series     AS amz_cfdi_series,
    f.folios     AS amz_cfdi_folio

FROM somos_reyes.amz_transactions a

JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id

-- CORRECCION 3: fila autoritativa por ENVIO (no por order_adjustment_item_id)
--
-- Se cambio la llave de deduplicacion tras el caso 701-0599283-6185052 (2
-- piezas del SKU FLR30). Sus cuatro transacciones comparten el MISMO
-- order_adjustment_item_id (167963962689881), asi que agrupar por esa columna
-- colapsaba las dos piezas en una y facturaba la mitad:
--
--   transaccion  status             posted             settlement    shipment_id
--   mz81Ov0lMWn  DEFERRED_RELEASED  2026-08-25 22:31   27358358891   469360987092201
--   xPjHsYpafZi  DEFERRED_RELEASED  2026-08-25 22:37   27358358891   469360987091201
--   ye7glpXfLg4  RELEASED           2026-08-30 19:51   27410966541   469360987092201
--   whhBTmG_-m8  RELEASED           2026-08-30 15:47   27410966541   469360987091201
--
-- Son DOS envios, cada uno con su transaccion diferida y su re-publicacion.
-- Amazon emitio dos CFDI de 2,573.01 y Odoo tiene 2 unidades = 5,146.02.
-- Agrupando por shipment_id y quedandose con el posted_date mas reciente
-- sobreviven las dos filas correctas.
--
-- Amazon confirma el encadenamiento en related_identifiers: la fila RELEASED
-- lleva un DEFERRED_TRANSACTION_ID que apunta a la diferida que sustituye
-- (ye7glpXfLg4 -> mz81Ov0lMWn, whhBTmG_-m8 -> xPjHsYpafZi). Esa es la fuente
-- de verdad; aqui se usa shipment_id porque es una columna plana y llega al
-- mismo resultado en los tres casos verificados, sin depender de parsear JSON.
--
-- Verificado el 11-sep-2026 sobre la ventana desde 2026-08-01: CERO filas de
-- 'Order Payment' no diferidas sin shipment_id. El COALESCE con
-- transaction_id se deja como red: si algun dia llegara una sin envio, la
-- fila se conserva sola en vez de agruparse con otras que tampoco lo tienen.
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

-- CORRECCION 1: deduplicado
LEFT JOIN (
    SELECT amazonorderid, MAX(IsBusinessOrder) AS IsBusinessOrder
    FROM somos_reyes.amazon_orders
    GROUP BY amazonorderid
) d ON a.order_id = d.amazonorderid

LEFT JOIN (
    SELECT settlement_id, MAX(deposit_date) AS deposit_date
    FROM somos_reyes.amz_settlement_report
    GROUP BY settlement_id
) e ON a.settlement_id = e.settlement_id

-- CFDI de Amazon Business. Solo facturas vigentes, no complementos de pago:
-- la PK incluye receipt_type/payment_type/uuid, asi que una orden puede traer
-- factura y complemento a la vez.
--
-- ANTES: MIN(uuid) / MAX(total). Eso asumia UN CFDI por orden y era falso:
-- 701-0599283-6185052 tiene DOS (folios 975 y 976, 2,573.01 cada uno) porque
-- Amazon facturo envio por envio. MAX(total) devolvia 2,573.01 en vez de
-- 5,146.02. Ahora se cuenta (n_cfdi) y se SUMA, y el loader aparta las
-- ordenes con mas de un CFDI en vez de sellar uno solo y perder el otro.
--
-- Se arrastran ademas los datos fiscales del receptor: sin ellos la factura
-- B2B salia a publico en general, que es justo lo que Finanzas no quiere.
LEFT JOIN (
    SELECT order_id,
           COUNT(*)                            AS n_cfdi,
           ROUND(SUM(total), 2)                AS total,
           ROUND(SUM(subtotal), 2)             AS subtotal,
           -- subtotal viene BRUTO, antes del descuento. La base gravable es
           -- (subtotal - discount) y es la que cuadra contra nuestro
           -- cfdi_base. Verificado sobre 30 CFDI con descuento: en todos
           -- (subtotal - discount) * 0.16 == total_charged_taxes.
           ROUND(SUM(IFNULL(discount, 0)), 2)  AS discount,
           ROUND(SUM(total_charged_taxes), 2)  AS taxes,
           MIN(uuid)                           AS uuid,
           GROUP_CONCAT(uuid  ORDER BY date SEPARATOR ', ')   AS uuids,
           GROUP_CONCAT(invoice ORDER BY date SEPARATOR ', ') AS folios,
           MAX(receiver_rfc)                   AS receiver_rfc,
           MAX(receiver_name)                  AS receiver_name,
           MAX(receiver_cfdi_key)              AS receiver_cfdi_key,
           MAX(receiver_tax_regime)            AS receiver_tax_regime,
           MAX(receiver_tax_residence)         AS receiver_zip,
           MAX(payment_type)                   AS forma_pago,
           MAX(payment_method)                 AS metodo_pago,
           MIN(date)                           AS date,
           MAX(series)                         AS series
    FROM somos_reyes.amz_b2b_invoices
    WHERE receipt_type = 'I'
      AND (status IS NULL OR status <> 'CANCEL')
      AND (cancellation_date IS NULL OR cancellation_date = '')
    GROUP BY order_id
) f ON a.order_id = f.order_id

WHERE a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'
  AND DATE(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) >= %s
  {FILTRO_FIN}
  AND c.so IS NOT NULL

GROUP BY a.order_id, c.so, f.uuid, f.uuids, f.n_cfdi, f.total, f.subtotal,
         f.discount,
         f.taxes, f.receiver_rfc, f.receiver_name, f.receiver_cfdi_key,
         f.receiver_tax_regime, f.receiver_zip, f.forma_pago, f.metodo_pago,
         f.date, f.series, f.folios
HAVING cfdi_total > 0
ORDER BY posted_date DESC
"""


def construir_query_lote():
    """QUERY_LOTE lista para ejecutar, con sus parametros.

    Devuelve (sql, params) para que quien la use no tenga que saber cuantos
    placeholders lleva segun haya o no limite superior de ventana.
    """
    params = [AMZ_START_DATE, DEDUP_LOOKBACK_DAYS, AMZ_START_DATE]
    if AMZ_END_DATE:
        sql = QUERY_LOTE.replace(
            '{FILTRO_FIN}',
            'AND DATE(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) < %s')
        params.append(AMZ_END_DATE)
    else:
        sql = QUERY_LOTE.replace('{FILTRO_FIN}', '')
    return sql, tuple(params)


# =============================================================================
# DESGLOSE POR ENVIO -- SOLO PARA ORDENES CON VARIOS CFDI DE AMAZON
# =============================================================================
# Cuando Amazon emite N comprobantes para una orden, hay que crear N facturas
# en Odoo (una por CFDI), porque account.move guarda un solo
# l10n_mx_edi_cfdi_uuid.
#
# Para partir las lineas hace falta saber que se fue en cada envio. QUERY_LOTE
# ya agrega a nivel orden, asi que estas dos consultas bajan el detalle SOLO
# de las pocas ordenes afectadas: no se paga su costo en el lote completo.
# =============================================================================

QUERY_ENVIOS = """
SELECT
    COALESCE(a.shipment_id, a.transaction_id)     AS pieza,
    b.sku                                         AS sku,
    MAX(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) AS posted_date,
    MAX(a.settlement_id)                          AS settlement_id,
    SUM(IFNULL(b.quantity_shipped, 0))            AS qty,

    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)), 2)
                                                  AS cfdi_base,
    ROUND(SUM(IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                                  AS cfdi_iva,
    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)
            + IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                                  AS cfdi_total

FROM somos_reyes.amz_transactions a
JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id

-- Misma deduplicacion que QUERY_LOTE: la fila autoritativa de cada envio.
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

WHERE a.order_id = %s
  AND a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'

GROUP BY COALESCE(a.shipment_id, a.transaction_id), b.sku
ORDER BY posted_date, pieza, sku
"""

QUERY_CFDIS_B2B = """
SELECT uuid, total, subtotal, IFNULL(discount, 0) AS discount,
       total_charged_taxes AS taxes, date, series,
       invoice AS folio, receiver_rfc, receiver_name, receiver_cfdi_key,
       receiver_tax_regime, receiver_tax_residence AS receiver_zip,
       payment_type AS forma_pago, payment_method AS metodo_pago
FROM somos_reyes.amz_b2b_invoices
WHERE order_id = %s
  AND receipt_type = 'I'
  AND (status IS NULL OR status <> 'CANCEL')
  AND (cancellation_date IS NULL OR cancellation_date = '')
ORDER BY date, invoice
"""


def leer_envios(cursor, order_id):
    """Envios de una orden, con sus SKU y cantidades."""
    cursor.execute(QUERY_ENVIOS,
                   (AMZ_START_DATE, DEDUP_LOOKBACK_DAYS, order_id))
    envios = {}
    for r in cursor.fetchall():
        e = envios.setdefault(r['pieza'], {
            'pieza': r['pieza'], 'settlement_id': r['settlement_id'],
            'posted_date': r['posted_date'], 'items': [],
            'base': 0.0, 'iva': 0.0, 'total': 0.0})
        e['items'].append({'sku': r['sku'], 'qty': int(a_float(r['qty']))})
        e['base'] += a_float(r['cfdi_base'])
        e['iva'] += a_float(r['cfdi_iva'])
        e['total'] += a_float(r['cfdi_total'])
    salida = sorted(envios.values(), key=lambda x: (str(x['posted_date']), x['pieza']))
    for e in salida:
        e['base'] = round(e['base'], 2)
        e['iva'] = round(e['iva'], 2)
        e['total'] = round(e['total'], 2)
        e['posted_date'] = str(e['posted_date']) if e['posted_date'] else None
    return salida


def emparejar_cfdi_con_envios(cursor, order_id):
    """Empareja cada CFDI de Amazon con el envio que factura.

    Amazon NO publica el vinculo entre comprobante y envio: la tabla
    amz_b2b_invoices solo trae order_id. Se empareja por importe, que es el
    unico dato comun entre ambos lados.

    Devuelve (parejas, motivo_de_fallo). Si no se puede emparejar con
    certeza devuelve ([], motivo) y la orden se aparta sin facturar: partir
    mal una orden en dos CFDI es un error fiscal, no un descuadre.
    """
    cursor.execute(QUERY_CFDIS_B2B, (order_id,))
    cfdis = cursor.fetchall()
    envios = leer_envios(cursor, order_id)

    if len(cfdis) != len(envios):
        return [], (f"{len(cfdis)} CFDI de Amazon contra {len(envios)} envio(s) "
                    f"deduplicado(s): no se puede repartir 1 a 1.")

    # Emparejamiento por importe. Ambas listas vienen ordenadas por fecha, asi
    # que cuando todos los importes son iguales (el caso frecuente: N piezas
    # del mismo SKU) el orden cronologico ya resuelve.
    libres = list(range(len(envios)))
    parejas = []
    for c in sorted(cfdis, key=lambda x: (str(x['date']), str(x['folio']))):
        total_cfdi = a_float(c['total'])
        elegido = None
        for i in libres:
            if abs(envios[i]['total'] - total_cfdi) <= TOLERANCIA_TOTAL:
                elegido = i
                break
        if elegido is None:
            disponibles = ', '.join(f"{envios[i]['total']:,.2f}" for i in libres)
            return [], (f"El CFDI {c['series']}-{c['folio']} por "
                        f"{total_cfdi:,.2f} no cuadra con ningun envio "
                        f"pendiente ({disponibles}).")
        libres.remove(elegido)
        parejas.append({'cfdi': c, 'envio': envios[elegido]})

    return parejas, None


def construir_raw_json(row, tipo, desglose=None):
    """Log de conciliacion de Finanzas.

    Vive aqui porque account.move todavia no tiene campos personalizados para
    settlement_id ni tipo. Cuando existan, se migra de este JSON.

    'desglose_b2b' solo viene cuando Amazon emitio varios CFDI para la orden:
    lleva una entrada por (CFDI, envio) y es lo que el loader usa para crear
    una factura de Odoo por cada comprobante.
    """
    return json.dumps({
        'desglose_b2b': desglose or None,
        'log_conciliacion': {
            'amazon_order_id': row['mkp_order_id'],
            'settlement_id': row['settlement_id'],
            'odoo_so': row['odoo_so_name'],
            'tipo': tipo,
            'cfdi_uuid': row['amz_cfdi_uuid'],
        },
        'importes': {
            'cfdi_base': a_float(row['cfdi_base']),
            'cfdi_iva': a_float(row['cfdi_iva']),
            'cfdi_total': a_float(row['cfdi_total']),
            'fees_total': a_float(row['fees_total']),
        },
        'amazon': {
            'deposit_date': str(row['deposit_date']) if row['deposit_date'] else None,
            'posted_date': str(row['posted_date']) if row['posted_date'] else None,
            'is_business_order': int(row['is_business_order'] or 0),
            'fulfillment_network': row['fulfillment_network'],
            'n_items': row['n_items'],
            'n_envios': row['n_envios'],
            'skus': row['skus'],
        },
        # Datos fiscales del CFDI que emitio Amazon. El loader los usa para
        # replicar el comprobante en Odoo (receptor, uso de CFDI, forma de
        # pago, fecha). Antes no se guardaban y la factura B2B acababa a
        # publico en general con la fecha de la corrida.
        'cfdi_amazon_business': ({
            'n_cfdi': int(row['amz_cfdi_count'] or 0),
            'uuid': row['amz_cfdi_uuid'],
            'uuids': row['amz_cfdi_uuids'],
            'total': a_float(row['amz_cfdi_total']),
            'subtotal': a_float(row['amz_cfdi_subtotal']),   # BRUTO
            'discount': a_float(row['amz_cfdi_discount']),
            'taxes': a_float(row['amz_cfdi_taxes']),
            'receiver_rfc': row['receiver_rfc'],
            'receiver_name': row['receiver_name'],
            'receiver_cfdi_key': row['receiver_cfdi_key'],      # uso de CFDI
            'receiver_tax_regime': row['receiver_tax_regime'],  # regimen fiscal
            'receiver_zip': row['receiver_zip'],                # domicilio fiscal
            'forma_pago': row['forma_pago'],                    # clave SAT (28, 03...)
            'metodo_pago': row['metodo_pago'],                  # PUE / PPD
            'date': str(row['amz_cfdi_date']) if row['amz_cfdi_date'] else None,
            'series': row['amz_cfdi_series'],
            'folio': row['amz_cfdi_folio'],
            'nota': ('Amazon no expone el XML del CFDI, solo metadata. '
                     'No se adjunta archivo a la factura.'),
        } if row['amz_cfdi_uuid'] else None),
        'entorno': ENTORNO,
        'generado_por': '01_amz_billing.py',
        'generado_en': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }, ensure_ascii=False, default=str)


def existe_en_staging(cursor, mkp_order_id):
    """Consulta previa solo para contabilizar cuantas ya estaban.

    La proteccion real la da UNIQUE KEY uk_mkp_order (marketplace,
    mkp_order_id) mas el ON DUPLICATE KEY del INSERT, que es atomico.
    """
    cursor.execute(f"""
        SELECT id, status FROM {TBL_BILLING}
        WHERE marketplace = %s AND mkp_order_id = %s
        LIMIT 1
    """, (MARKETPLACE, mkp_order_id))
    return cursor.fetchone()


def run_miner(db_conn):
    lector = db_conn.cursor(dictionary=True)
    log.info(f"[MINER] Leyendo transacciones de Amazon desde {AMZ_START_DATE}"
             f"{' hasta ' + AMZ_END_DATE if AMZ_END_DATE else ''} "
             f"(dedup con {DEDUP_LOOKBACK_DAYS} dias de margen)...")
    t0 = tm.time()
    sql, params = construir_query_lote()
    lector.execute(sql, params)
    rows = lector.fetchall()
    lector.close()
    log.info(f"[MINER] {len(rows)} orden(es) leidas en "
             f"{round(tm.time() - t0, 1)}s")

    if not rows:
        return {'b2b': 0, 'b2c': 0, 'ya_existian': 0, 'gracia': 0,
                'multi_cfdi': [], 'split_fallido': [], 'refrescadas': 0}

    hoy = datetime.now()
    res = {'b2b': 0, 'b2c': 0, 'ya_existian': 0, 'gracia': 0,
           'multi_cfdi': [], 'split_fallido': [], 'refrescadas': 0}
    w = db_conn.cursor()
    # Cursor aparte, con diccionario, para las consultas de desglose de las
    # ordenes con varios CFDI. Se abre siempre aunque no haya ninguna: cuesta
    # nada y evita abrir y cerrar dentro del bucle.
    w_dict = db_conn.cursor(dictionary=True)

    # Topes de siembra. None en produccion = sin tope.
    tope_b2b = QUOTA_B2B * MINER_OVERFETCH if QUOTA_B2B else None
    tope_b2c = QUOTA_B2C * MINER_OVERFETCH if QUOTA_B2C else None

    for row in rows:
        if (tope_b2b is not None and tope_b2c is not None
                and res['b2b'] >= tope_b2b and res['b2c'] >= tope_b2c):
            log.warning(f"[MINER] Topes de siembra alcanzados "
                        f"(B2B {res['b2b']}/{tope_b2b}, B2C {res['b2c']}/{tope_b2c}).")
            break

        # El tipo se calcula ANTES de tocar la BD: si el tope de ESE tipo ya
        # esta lleno, se salta la fila y se sigue buscando del otro tipo. Con
        # un tope conjunto no habria forma de completar las B2B.
        es_b2b = bool(row['amz_cfdi_uuid'])
        if es_b2b and tope_b2b is not None and res['b2b'] >= tope_b2b:
            continue
        if not es_b2b and tope_b2c is not None and res['b2c'] >= tope_b2c:
            continue

        # =====================================================================
        # FILAS QUE YA EXISTEN: SE REFRESCAN, NO SE SALTAN
        # =====================================================================
        # Antes cualquier fila existente hacia 'continue'. Eso congelaba su
        # clasificacion para siempre y rompia dos cosas:
        #
        #   1. SKIPPED_GRACE nunca expiraba. Una orden metida en gracia jamas
        #      se volvia a evaluar, asi que pasaban los 5 dias y nadie la
        #      facturaba. Al 11-sep habia 205 filas asi.
        #   2. Una orden que se registro como B2C y despues recibe el CFDI de
        #      Amazon (el cliente autofactura dentro de la gracia) se quedaba
        #      como PENDING_WB_INVOICE y se habria emitido a publico general
        #      contradiciendo el comprobante de Amazon.
        #
        # Ahora se deja correr el INSERT ... ON DUPLICATE KEY, cuyo UPDATE ya
        # esta escrito para tocar solo filas que siguen pendientes: si la
        # orden ya se facturo, una relectura de Amazon NO la reabre.
        # =====================================================================
        ya = existe_en_staging(w, row['mkp_order_id'])
        if ya and ya[1] not in REFRESCABLES:
            res['ya_existian'] += 1
            continue
        es_nueva = ya is None

        tipo = 'B2B' if es_b2b else 'B2C_PUBLICO_GENERAL'
        status = 'PENDING_B2B_LINK' if es_b2b else 'PENDING_WB_INVOICE'

        # Referencia de importe. En B2B manda el CFDI que YA emitio Amazon, no
        # nuestra suma de transacciones: el comprobante fiscal existe y su
        # total es el dato duro. En B2C mandamos nosotros, asi que la
        # referencia es la suma de las transacciones liberadas.
        n_cfdi = int(row['amz_cfdi_count'] or 0)
        desglose = None
        if es_b2b:
            importe_ref = a_float(row['amz_cfdi_total'])
            # Cruce independiente sobre la BASE, contra una fuente distinta:
            # el CFDI que emitio Amazon (subtotal bruto - descuento) frente a
            # nuestro calculo sobre amz_transaction_items. Verificado sobre 30
            # CFDI con descuento. Solo AVISA, no bloquea: el anclaje que
            # decide sigue siendo el total, y el comportamiento de discount en
            # los casos raros (varios CFDI por orden, reembolsos parciales) no
            # esta perfilado. Sirve para detectar un descuento que no viaje en
            # shipping_discount / our_price_discount, caso en que el total
            # puede cuadrar por compensacion y la base no.
            base_amz = (a_float(row['amz_cfdi_subtotal'])
                        - a_float(row['amz_cfdi_discount']))
            base_wb = a_float(row['cfdi_base'])
            if base_amz > 0 and abs(base_amz - base_wb) > TOLERANCIA_TOTAL:
                log.warning(f"{row['mkp_order_id']}: base Amazon "
                            f"{base_amz:,.2f} vs base calculada "
                            f"{base_wb:,.2f}. El total sigue cuadrando; "
                            f"revisar descuentos.")
            # Varios CFDI de Amazon para una orden: account.move guarda un
            # solo l10n_mx_edi_cfdi_uuid, asi que se crea UNA FACTURA POR
            # CFDI. Aqui se resuelve el reparto (que envio factura cada
            # comprobante) y se guarda en el raw_json; el loader solo lo
            # ejecuta. Si el reparto no es inequivoco, la orden se aparta.
            if n_cfdi > 1:
                desglose, motivo = emparejar_cfdi_con_envios(
                    w_dict, row['mkp_order_id'])
                if not desglose:
                    status = 'B2B_SPLIT_MISMATCH'
                    res['split_fallido'].append(
                        f"{row['mkp_order_id']} ({row['odoo_so_name']}): {motivo}")
                else:
                    res['multi_cfdi'].append(
                        f"{row['mkp_order_id']} ({row['odoo_so_name']}): "
                        f"{n_cfdi} CFDI folios {row['amz_cfdi_folio']} -> "
                        f"{len(desglose)} facturas de Odoo por "
                        f"{importe_ref:,.2f}")
        else:
            importe_ref = a_float(row['cfdi_total'])

        # Periodo de gracia: solo aplica a B2C. Si Amazon ya emitio el CFDI no
        # hay nada que esperar.
        if not es_b2b and row['posted_date']:
            posted = row['posted_date']
            if isinstance(posted, str):
                posted = datetime.strptime(posted[:19], '%Y-%m-%d %H:%M:%S')
            if (hoy - posted).days < GRACE_DAYS:
                status = 'SKIPPED_GRACE'
                res['gracia'] += 1

        # ON DUPLICATE KEY sobre uk_mkp_order (marketplace, mkp_order_id).
        # El UPDATE es defensivo: solo refresca filas que siguen pendientes.
        # Si la orden ya se facturo, una re-lectura de Amazon NO la reabre.
        w.execute(f"""
            INSERT INTO {TBL_BILLING}
                (marketplace, mkp_order_id, odoo_so_name, cfdi_uuid,
                 total_amount, xml_data, raw_json, status,
                 retry_count, retry_count_loader)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, 0, 0)
            ON DUPLICATE KEY UPDATE
                odoo_so_name = IF(status IN ('PENDING_B2B_LINK','PENDING_WB_INVOICE','SKIPPED_GRACE','ERROR'), VALUES(odoo_so_name), odoo_so_name),
                cfdi_uuid    = IF(status IN ('PENDING_B2B_LINK','PENDING_WB_INVOICE','SKIPPED_GRACE','ERROR'), VALUES(cfdi_uuid),    cfdi_uuid),
                total_amount = IF(status IN ('PENDING_B2B_LINK','PENDING_WB_INVOICE','SKIPPED_GRACE','ERROR'), VALUES(total_amount), total_amount),
                raw_json     = IF(status IN ('PENDING_B2B_LINK','PENDING_WB_INVOICE','SKIPPED_GRACE','ERROR'), VALUES(raw_json),     raw_json),
                status       = IF(status IN ('PENDING_B2B_LINK','PENDING_WB_INVOICE','SKIPPED_GRACE'),          VALUES(status),       status)
        """, (MARKETPLACE, row['mkp_order_id'], row['odoo_so_name'],
              row['amz_cfdi_uuid'], importe_ref,
              construir_raw_json(row, tipo, desglose), status))
        db_conn.commit()

        if not es_nueva:
            # Refrescada, no sembrada: no consume cupo de siembra. El loader
            # la tomara igual, porque busca por status y no por id.
            res['refrescadas'] += 1
            continue
        if status in ('SKIPPED_GRACE', 'B2B_SPLIT_MISMATCH'):
            continue
        if es_b2b:
            res['b2b'] += 1
        else:
            res['b2c'] += 1

    w.close()
    w_dict.close()
    log.info(f"[MINER] Nuevas -> B2B: {res['b2b']} | B2C: {res['b2c']} | "
             f"en gracia: {res['gracia']} | refrescadas: {res['refrescadas']} | "
             f"ya cerradas: {res['ya_existian']}")

    if res['multi_cfdi']:
        log.info(f"[MINER] {len(res['multi_cfdi'])} orden(es) con varios CFDI "
                 f"de Amazon. Se facturaran partidas, una por comprobante:")
        for m in res['multi_cfdi'][:10]:
            log.info(f"  {m}")

    if res['split_fallido']:
        log.warning(f"[MINER] {len(res['split_fallido'])} orden(es) con varios "
                    f"CFDI que NO se pudieron repartir. Quedan en "
                    f"B2B_SPLIT_MISMATCH sin facturar:")
        for m in res['split_fallido'][:10]:
            log.warning(f"  {m}")
        notify_slack(
            f":warning: *{len(res['split_fallido'])} orden(es) de Amazon con "
            f"varios CFDI que no cuadran con sus envios*. No se facturaron: "
            f"repartir mal una orden entre dos comprobantes es un error "
            f"fiscal. Requieren revision manual.")
    return res


# =============================================================================
# FASE B -- LOADER
# =============================================================================

def reset_stuck_processing(db_conn):
    """Devuelve a pendiente lo que quedo en PROCESSING porque una corrida
    murio. Solo lo suficientemente viejo: un registro reciente puede
    pertenecer a una corrida viva.

    El estado original se recupera de cfdi_uuid: si lo tiene, era B2B.
    """
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = IF(cfdi_uuid IS NULL, 'PENDING_WB_INVOICE', 'PENDING_B2B_LINK')
        WHERE marketplace = %s
          AND status = 'PROCESSING'
          AND processed_at IS NOT NULL
          AND processed_at < (NOW() - INTERVAL %s HOUR)
    """, (MARKETPLACE, STUCK_PROCESSING_MIN_AGE_HOURS))
    db_conn.commit()
    if cursor.rowcount:
        log.warning(f"Liberados {cursor.rowcount} registros atorados en PROCESSING.")
    cursor.close()


def _fetch_por_status(cursor, status, limite, orden):
    sql = f"""
        SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
               raw_json, status, retry_count_loader
        FROM {TBL_BILLING}
        WHERE marketplace = %s
          AND status = %s
          AND retry_count_loader < 3
        ORDER BY id {orden}
    """
    params = [MARKETPLACE, status]
    if limite:
        sql += " LIMIT %s"
        params.append(limite)
    cursor.execute(sql, tuple(params))
    return cursor.fetchall()


def fetch_pendientes(db_conn):
    """Candidatos para el loader.

    EN PRUEBAS SE TRAE POR TIPO, Y ES LO QUE ARREGLA LA CUOTA.
    ---------------------------------------------------------
    Con una sola consulta 'ORDER BY id LIMIT 120' el lote se llenaba con los
    120 registros MAS ANTIGUOS de la tabla, que son rezago de corridas
    previas: ordenes sin SO, ya facturadas, canceladas. Las 30 B2B que el
    miner acababa de sembrar tienen los id mas altos y nunca entraban al lote.
    Resultado de la corrida del 11-sep: B2B 0/10 con 17 elegibles disponibles.

    Consultando por tipo, el lote siempre lleva candidatos de los dos. Y en
    pruebas se ordena por id DESCENDENTE para tomar lo recien sembrado, que es
    lo que se quiere probar; en produccion se mantiene el orden ascendente
    (FIFO: primero lo mas viejo).
    """
    cursor = db_conn.cursor(dictionary=True)

    if QUOTA_B2B is None:
        rows = (_fetch_por_status(cursor, 'PENDING_B2B_LINK', None, 'ASC')
                + _fetch_por_status(cursor, 'PENDING_WB_INVOICE', None, 'ASC'))
        cursor.close()
        return rows

    margen = MINER_OVERFETCH * 2
    rows = (_fetch_por_status(cursor, 'PENDING_B2B_LINK',
                              QUOTA_B2B * margen, 'DESC')
            + _fetch_por_status(cursor, 'PENDING_WB_INVOICE',
                                QUOTA_B2C * margen, 'DESC'))
    cursor.close()
    log.info(f"[LOADER] Candidatos por tipo -> "
             f"B2B: {sum(1 for r in rows if r['status'] == 'PENDING_B2B_LINK')} | "
             f"B2C: {sum(1 for r in rows if r['status'] == 'PENDING_WB_INVOICE')}")
    return rows


def claim(db_conn, record_id):
    """Candado. True solo si esta corrida gano el registro.

    Se sella processed_at porque es el reloj que usa reset_stuck_processing
    para decidir la antiguedad. Sin esto quedaria en NULL y la siguiente
    corrida liberaria un registro que todavia se esta procesando.
    """
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = 'PROCESSING', processed_at = NOW()
        WHERE id = %s AND status IN ('PENDING_B2B_LINK', 'PENDING_WB_INVOICE')
    """, (record_id,))
    db_conn.commit()
    ganado = cursor.rowcount > 0
    cursor.close()
    return ganado


def update_staging(db_conn, record_id, status, cfdi_uuid=None, error_log=None,
                   sumar_retry=False):
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = %s,
            cfdi_uuid = COALESCE(%s, cfdi_uuid),
            error_log = %s,
            retry_count_loader = retry_count_loader + %s,
            processed_at = NOW()
        WHERE id = %s
    """, (status, cfdi_uuid, error_log, 1 if sumar_retry else 0, record_id))
    db_conn.commit()
    cursor.close()


def detectar_b2b_tardio(db_conn, records):
    """Ultima red antes de emitir a publico general.

    Un registro marcado PENDING_WB_INVOICE dice "nos toca emitir el CFDI".
    Si Amazon YA emitio uno para esa orden, emitir el nuestro a publico
    general genera un segundo comprobante del mismo hecho: doble ingreso y
    doble CxC.

    Puede pasar por dos caminos: el cliente autofacturo despues de que el
    miner clasifico la orden, o la fila quedo mal clasificada por una
    intervencion manual sobre la tabla de control.

    Las afectadas se devuelven a PENDING_B2B_LINK con su UUID y NO se
    facturan en esta corrida: el miner las refrescara con los datos fiscales
    completos y el loader las tomara bien en la siguiente.

    Una sola consulta para todo el lote.
    """
    candidatos = [r for r in records if r['status'] == 'PENDING_WB_INVOICE']
    if not candidatos:
        return set()

    ids = [r['mkp_order_id'] for r in candidatos]
    marcas = ', '.join(['%s'] * len(ids))
    cursor = db_conn.cursor()
    cursor.execute(f"""
        SELECT order_id, MIN(uuid)
        FROM somos_reyes.amz_b2b_invoices
        WHERE order_id IN ({marcas})
          AND receipt_type = 'I'
          AND (status IS NULL OR status <> 'CANCEL')
          AND (cancellation_date IS NULL OR cancellation_date = '')
        GROUP BY order_id
    """, tuple(ids))
    con_cfdi = dict(cursor.fetchall())

    if con_cfdi:
        for r in candidatos:
            uuid = con_cfdi.get(r['mkp_order_id'])
            if not uuid:
                continue
            cursor.execute(f"""
                UPDATE {TBL_BILLING}
                SET status = 'PENDING_B2B_LINK', cfdi_uuid = %s,
                    error_log = %s, processed_at = NOW()
                WHERE id = %s AND status = 'PENDING_WB_INVOICE'
            """, (uuid,
                  'Amazon ya emitio CFDI para esta orden: reclasificada a B2B '
                  'antes de facturar a publico general.', r['id']))
        db_conn.commit()
        log.warning(f"[LOADER] {len(con_cfdi)} orden(es) marcadas como B2C "
                    f"tienen CFDI vivo de Amazon. Reclasificadas a "
                    f"PENDING_B2B_LINK; no se facturan en esta corrida.")
    cursor.close()
    return set(con_cfdi.keys())


def buscar_so(odoo, so_name):
    r = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', so_name)]],
                        {'fields': ['id', 'name', 'state', 'team_id',
                                    'invoice_status', 'invoice_count',
                                    'order_line', 'partner_id', 'date_order'],
                         'limit': 1})
    return r[0] if r else None


def facturas_existentes(odoo, so_name):
    """Facturas vivas de la SO, con su ref.

    Antes esto era un search_count. Ahora devuelve las filas porque una orden
    puede tener legitimamente VARIAS facturas (una por CFDI de Amazon) y hace
    falta saber cuales ya se crearon para reanudar sin duplicar.

    La ref sigue llevando el prefijo compartido 'AUTOINV:' a proposito: asi el
    1 a 1 generico y este script se siguen viendo mutuamente.
    """
    domain = ['&', ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
              '|', ('invoice_origin', '=', so_name),
              ('ref', 'like', f'{IDEMPOTENCY_PREFIX}{so_name}%')]
    return odoo.execute_kw('account.move', 'search_read', [domain],
                           {'fields': ['id', 'name', 'state', 'ref',
                                       'amount_total', 'l10n_mx_edi_cfdi_uuid'],
                            'order': 'id asc'})


def buscar_por_ref(odoo, ref):
    """Recupera una factura huerfana cuando se pierde la respuesta del create.

    Se busca por la ref EXACTA de la parte, no por la SO: en una orden
    repartida entre varios CFDI, buscar por SO devolveria las hermanas y se
    adoptaria la equivocada.
    """
    domain = [('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
              ('ref', '=', ref)]
    return odoo.execute_kw('account.move', 'search_read', [domain],
                           {'fields': ['id', 'name', 'state', 'ref',
                                       'l10n_mx_edi_cfdi_uuid'],
                            'order': 'id asc'})


def leer_lineas_so(odoo, so):
    """Lineas de la SO pendientes de facturar, en crudo.

    REGLA 2 DE FINANZAS: sin restriccion de OUT. No se aborta cuando
    qty_delivered < qty_ordered.

    Se separo de armar_lineas() porque el reparto por envio (varios CFDI de
    Amazon para una orden) necesita las lineas crudas para tomar de cada una
    solo la cantidad que se fue en ese envio.
    """
    lines = odoo.execute_kw('sale.order.line', 'search_read',
                            [[('id', 'in', so['order_line'])]],
                            {'fields': ['id', 'name', 'sequence', 'display_type',
                                        'product_id', 'product_uom',
                                        'product_uom_qty', 'qty_invoiced',
                                        'qty_delivered', 'price_unit',
                                        'discount', 'tax_id']})
    salida = []
    for line in lines:
        qty = line['product_uom_qty']
        if line['qty_invoiced'] >= qty:
            continue

        # C-ENVIO no genera movimiento de inventario: se marca entregado para
        # que Odoo no lo deje pendiente en la SO.
        pname = line['product_id'][1].upper() if line.get('product_id') else ''
        if 'C-ENVIO' in pname and line['qty_delivered'] < qty:
            try:
                odoo.execute_kw('sale.order.line', 'write',
                                [[line['id']], {'qty_delivered': qty}])
            except Exception as e:
                log.warning(f"{so['name']}: no se pudo marcar C-ENVIO: {e}")

        salida.append(line)
    return salida


def comando_linea(line, qty):
    """Una linea de sale.order.line -> comando de account.move.line."""
    return (0, 0, {
        'display_type': line.get('display_type') or 'product',
        'sequence': int(line['sequence']) if line.get('sequence') else 10,
        'name': line['name'],
        'product_uom_id': line['product_uom'][0] if line.get('product_uom') else False,
        'product_id': line['product_id'][0] if line.get('product_id') else False,
        'quantity': qty,
        'discount': line['discount'],
        'price_unit': line['price_unit'],
        'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])] if line.get('tax_id') else False,
        'sale_line_ids': [(4, line['id'])],
    })


def armar_lineas(odoo, so):
    """Lineas del CFDI sobre la cantidad PEDIDA (caso normal, una factura)."""
    return [comando_linea(l, l['product_uom_qty']) for l in leer_lineas_so(odoo, so)]


def total_estimado(comandos):
    """Total con IVA que producira Odoo, calculado ANTES de crear la factura.

    Mismo metodo que 02_loader_ml_billing.py: cantidad x precio x descuento,
    por 1.16. Es una estimacion (Odoo redondea por linea), de ahi la
    tolerancia de un peso.
    """
    base = sum(c[2]['quantity'] * c[2]['price_unit']
               * (1 - (c[2]['discount'] or 0) / 100.0) for c in comandos)
    return round(base * 1.16, 2)


# =============================================================================
# DATOS FISCALES DEL CFDI DE AMAZON BUSINESS
# =============================================================================
# Una factura B2B en Odoo tiene que ser el reflejo del CFDI que Amazon ya
# emitio: mismo receptor, mismo uso de CFDI, misma forma de pago, misma fecha.
# Antes se creaba con PARTNER_ID_PUBLICO_GENERAL, uso 'S01' (sin efectos
# fiscales), forma de pago 3 y la fecha de la corrida: un comprobante distinto
# del que el cliente ya tiene en su contabilidad.
#
# Todos los datos salen de somos_reyes.amz_b2b_invoices y viajan en el
# raw_json que escribio el miner, asi que el loader no vuelve a MySQL.
#
# El mecanismo de partner es el mismo de 02_loader_ml_billing.py: buscar por
# vat y crear el que falte con los datos del comprobante.
# =============================================================================

def cargar_catalogos(odoo):
    """Cataloga una sola vez lo que despues se consulta por factura."""
    pm = odoo.execute_kw('l10n_mx_edi.payment.method', 'search_read', [[]],
                         {'fields': ['id', 'code']})
    metodos = {p['code']: p['id'] for p in pm}

    mx = odoo.execute_kw('res.country', 'search', [[('code', '=', 'MX')]],
                         {'limit': 1})
    log.info(f"Catalogo de formas de pago SAT: {len(metodos)} claves.")
    return {'formas_pago': metodos, 'mx_country_id': mx[0] if mx else False}


def extraer_desglose_b2b(record):
    """Lista [(CFDI, envio)] cuando la orden va repartida. [] si no."""
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        return []
    return raw.get('desglose_b2b') or []


def armar_partes_split(odoo, so, desglose):
    """Una factura de Odoo por cada CFDI de Amazon.

    Devuelve (partes, motivo_de_fallo). Cualquier desajuste entre lo que
    Amazon envio y lo que la SO tiene pendiente aborta el reparto COMPLETO:
    emitir una de las dos partes dejaria la orden facturada a medias, con un
    CFDI de Amazon sin contrapartida en Odoo.

    El reparto es por SKU y cantidad. El precio unitario sale siempre de la
    SO; de Amazon solo se toma CUANTAS piezas van en cada envio.
    """
    so_name = so['name']
    lineas_so = leer_lineas_so(odoo, so)
    if not lineas_so:
        # No es un fallo de reparto: la SO ya esta facturada por completo. Se
        # devuelve un motivo reconocible para que procesar() la clasifique
        # como ALREADY_ODOO_INVOICED y no como B2B_SPLIT_MISMATCH, que
        # mandaria a Finanzas a revisar un problema inexistente.
        return [], SIN_LINEAS_PENDIENTES

    # SKU de cada linea. default_code es el campo que cruza con el sku de
    # Amazon; el nombre mostrado no sirve porque incluye la descripcion.
    ids_prod = [l['product_id'][0] for l in lineas_so if l.get('product_id')]
    codigos = {}
    if ids_prod:
        prods = odoo.execute_kw('product.product', 'read', [ids_prod],
                                {'fields': ['id', 'default_code']})
        codigos = {p['id']: (p.get('default_code') or '').strip().upper()
                   for p in prods}

    por_sku = {}
    for l in lineas_so:
        sku = codigos.get(l['product_id'][0], '') if l.get('product_id') else ''
        por_sku.setdefault(sku, []).append(l)

    # Lo que Amazon dice que se envio, contra lo que la SO tiene pendiente.
    pedido_amz = {}
    for par in desglose:
        for it in par['envio']['items']:
            sku = (it['sku'] or '').strip().upper()
            pedido_amz[sku] = pedido_amz.get(sku, 0) + int(it['qty'])

    pendiente_so = {}
    for sku, lineas in por_sku.items():
        pendiente_so[sku] = sum(l['product_uom_qty'] for l in lineas)

    faltantes = [s for s in pedido_amz if s not in por_sku]
    if faltantes:
        return [], (f"SKU {', '.join(faltantes)} de los envios de Amazon no "
                    f"existe en las lineas pendientes de {so_name}.")

    sobrantes = [s for s in pendiente_so if s not in pedido_amz]
    if sobrantes:
        return [], (f"La SO {so_name} tiene lineas pendientes que ningun envio "
                    f"de Amazon cubre (SKU {', '.join(sobrantes) or 'sin codigo'}): "
                    f"no hay forma de asignarlas a un CFDI.")

    for sku, qty in pedido_amz.items():
        if abs(pendiente_so.get(sku, 0) - qty) > 0.001:
            return [], (f"SKU {sku}: Amazon envio {qty} pieza(s) y la SO tiene "
                        f"{pendiente_so.get(sku, 0)} pendiente(s).")

    # Reparto. Se consume la cantidad de cada linea conforme se van asignando
    # los envios, por si un SKU ocupa mas de una linea de la SO.
    restante = {l['id']: l['product_uom_qty'] for l in lineas_so}
    partes = []
    for i, par in enumerate(desglose, 1):
        comandos = []
        for it in par['envio']['items']:
            sku = (it['sku'] or '').strip().upper()
            por_asignar = int(it['qty'])
            for l in por_sku[sku]:
                if por_asignar <= 0:
                    break
                disponible = restante[l['id']]
                if disponible <= 0:
                    continue
                toma = float(min(disponible, por_asignar))
                restante[l['id']] = disponible - toma
                por_asignar -= toma
                comandos.append(comando_linea(l, toma))
            if por_asignar > 0:
                return [], (f"No alcanzaron las lineas de {so_name} para "
                            f"asignar {por_asignar} pieza(s) de {sku}.")

        c = par['cfdi']
        partes.append({
            'ref': f"{IDEMPOTENCY_PREFIX}{so_name}:{c['uuid']}",
            'cfdi': c,
            'lineas': comandos,
            'referencia': a_float(c['total']),
            'etiqueta': f" [{i}/{len(desglose)} {c.get('series')}-{c.get('folio')}]",
            'envio': par['envio'],
        })

    return partes, None


def extraer_cfdi_amazon(record):
    """Bloque 'cfdi_amazon_business' del raw_json. {} si no hay."""
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        return {}
    return raw.get('cfdi_amazon_business') or {}


def resolver_partner_b2b(odoo, cfdi, so, catalogos, cache):
    """Cliente de la factura B2B, por RFC del receptor del CFDI de Amazon.

    Orden: cache -> busqueda por vat -> creacion. Devuelve None si no hay RFC:
    el llamador decide (una B2B sin RFC no se factura, se aparta).

    Crear el partner es deliberado y sigue a Mercado Libre: el CFDI ya existe
    y el receptor es un dato del SAT, no una suposicion nuestra.
    """
    rfc = (cfdi.get('receiver_rfc') or '').strip().upper()
    if not rfc or rfc == 'XAXX010101000':
        return None

    if rfc in cache:
        return cache[rfc]

    encontrados = odoo.execute_kw('res.partner', 'search_read',
                                  [[('vat', '=', rfc)]],
                                  {'fields': ['id'], 'limit': 1})
    if encontrados:
        cache[rfc] = encontrados[0]['id']
        return cache[rfc]

    # Si la SO ya trae al cliente correcto, se reutiliza en vez de duplicarlo.
    if so.get('partner_id'):
        p = odoo.execute_kw('res.partner', 'read', [[so['partner_id'][0]]],
                            {'fields': ['id', 'vat']})
        if p and (p[0].get('vat') or '').strip().upper() == rfc:
            cache[rfc] = p[0]['id']
            return cache[rfc]

    vals = {
        'name': cfdi.get('receiver_name') or f'Cliente {rfc}',
        'vat': rfc,
        'zip': cfdi.get('receiver_zip') or False,
        'l10n_mx_edi_fiscal_regime': cfdi.get('receiver_tax_regime') or False,
        'country_id': catalogos['mx_country_id'],
        'company_type': 'person' if len(rfc) == 13 else 'company',
    }
    try:
        nuevo = odoo.execute_kw('res.partner', 'create', [vals],
                                {'context': {'tracking_disable': True}})
        cache[rfc] = nuevo
        log.info(f"Cliente creado desde el CFDI de Amazon: {rfc} "
                 f"({vals['name']}) -> id={nuevo}")
        return nuevo
    except Exception as e:
        log.error(f"No se pudo crear el cliente {rfc}: {e}")
        return None


def postear_chatter(odoo, inv_id, record, tipo, uuid, parte=None):
    """Log de conciliacion en el chatter.

    Acordado con Finanzas como solucion temporal mientras no existan los
    campos personalizados en account.move. No es filtrable ni exportable,
    pero queda auditable.

    TEXTO PLANO A PROPOSITO. La primera version mandaba HTML (<b>, <br/>) y
    Odoo lo mostro literal, con las etiquetas a la vista. Desde Odoo 17
    message_post renderiza el body solo si llega como markupsafe.Markup; un
    str normal pasa por plaintext2html(), que escapa < y > y convierte los
    saltos de linea en <br>. Por XML-RPC todo viaja como str, asi que no hay
    manera de mandar Markup. Mandando texto plano no hay nada que escapar y
    los saltos los pone Odoo. Ningun script de produccion postea HTML aqui:
    facturacion_automatica_1a1.py manda una linea de texto plano y el loader
    de ML ni siquiera usa el chatter, escribe en narration.

    Sin alinear etiquetas en columnas con espacios: HTML colapsa los espacios
    multiples y quedaria desalineado igual.
    """
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        raw = {}
    imp = raw.get('importes', {})
    amz = raw.get('amazon', {})
    lc = raw.get('log_conciliacion', {})

    lineas = [
        "FACTURACION AUTOMATICA AMAZON",
        f"Amazon order_id: {record['mkp_order_id']}",
        f"settlement_id: {lc.get('settlement_id') or 'n/d'}",
        f"SO Odoo: {record['odoo_so_name']}",
        f"Tipo: {tipo}",
        f"CFDI UUID: {uuid or 'pendiente'}",
        "",
        f"Base: {imp.get('cfdi_base', 0):,.2f}",
        f"IVA: {imp.get('cfdi_iva', 0):,.2f}",
        f"Total: {imp.get('cfdi_total', 0):,.2f}",
        f"Comisiones Amazon: {imp.get('fees_total', 0):,.2f}",
        f"Fecha de liberacion: {amz.get('deposit_date') or 'n/d'}",
        f"Red: {amz.get('fulfillment_network') or 'n/d'} | "
        f"Piezas: {amz.get('n_items') or 'n/d'} | "
        f"SKUs: {amz.get('skus') or 'n/d'}",
        f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}",
    ]
    # En una orden repartida, cada factura lleva los datos de SU CFDI, no los
    # agregados de la orden.
    cfdi = (parte or {}).get('cfdi') or raw.get('cfdi_amazon_business') or {}

    envio = (parte or {}).get('envio')
    if envio:
        piezas = ', '.join(f"{it['sku']} x{it['qty']}" for it in envio['items'])
        lineas += [
            "",
            "PARTE DE UNA ORDEN REPARTIDA",
            f"Envio Amazon (shipment_id): {envio['pieza']}",
            f"Settlement: {envio.get('settlement_id') or 'n/d'}",
            f"Piezas de este envio: {piezas}",
            f"Importe del envio: {a_float(envio.get('total')):,.2f}",
        ]

    if tipo == 'B2B' and cfdi:
        # El subtotal de amz_b2b_invoices es BRUTO. Se desglosa el descuento
        # para que la base gravable del CFDI de Amazon se lea junto a la
        # nuestra y no parezca un descuadre: lo que cuadra es
        # (subtotal - descuento), no el subtotal a secas.
        sub = a_float(cfdi.get('subtotal'))
        desc = a_float(cfdi.get('discount'))
        lineas += [
            "",
            "CFDI EMITIDO POR AMAZON BUSINESS",
            f"Serie-folio: {cfdi.get('series') or 'n/d'}-{cfdi.get('folio') or 'n/d'}",
            f"Receptor: {cfdi.get('receiver_rfc') or 'n/d'} "
            f"({cfdi.get('receiver_name') or 'n/d'})",
            f"Uso CFDI: {cfdi.get('receiver_cfdi_key') or 'n/d'} | "
            f"Regimen: {cfdi.get('receiver_tax_regime') or 'n/d'} | "
            f"CP: {cfdi.get('receiver_zip') or 'n/d'}",
            f"Forma de pago: {cfdi.get('forma_pago') or 'n/d'} | "
            f"Metodo: {cfdi.get('metodo_pago') or 'n/d'}",
            f"Fecha de emision: {cfdi.get('date') or 'n/d'}",
            f"Subtotal: {sub:,.2f}",
            f"Descuento: -{desc:,.2f}",
            f"Base gravable: {sub - desc:,.2f}",
            f"IVA: {a_float(cfdi.get('taxes')):,.2f}",
            f"Total: {a_float(cfdi.get('total')):,.2f}",
            "No se adjunta XML: Amazon solo expone metadata del comprobante.",
        ]
    elif tipo == 'B2B':
        lineas += [
            "",
            "CFDI emitido por Amazon Business. No se adjunta XML: Amazon solo "
            "expone metadata del comprobante.",
        ]

    body = '\n'.join(lineas)
    try:
        odoo.execute_kw('account.move', 'message_post', [inv_id],
                        {'body': body, 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo postear el chatter en {inv_id}: {e}")


def procesar(odoo, db_conn, record, account_cxc, stats, catalogos, partner_cache):
    so_name = record['odoo_so_name']
    es_b2b = record['status'] == 'PENDING_B2B_LINK'
    tipo = 'B2B' if es_b2b else 'B2C_PUBLICO_GENERAL'

    if not so_name:
        update_staging(db_conn, record['id'], 'NO_SO_IN_ODOO',
                       error_log='Sin SO cruzada en odoo_master_txns_c')
        stats['sin_so'].append(record['mkp_order_id'])
        return

    so = buscar_so(odoo, so_name)
    if not so:
        update_staging(db_conn, record['id'], 'NO_SO_IN_ODOO',
                       error_log=f'SO {so_name} no existe en Odoo')
        stats['sin_so'].append(so_name)
        return

    # --- Guard de estado de la SO ---
    # Una SO cancelada NO se factura. Si Amazon registro Order Payment y la
    # orden esta cancelada, lo correcto es factura + nota de credito Tipo A,
    # no facturar a ciegas.
    if so['state'] == 'cancel':
        update_staging(db_conn, record['id'], 'SO_CANCELLED',
                       error_log='SO cancelada. Requiere nota de credito Tipo A.')
        stats['canceladas'].append(so_name)
        return

    if so['state'] in ('draft', 'sent'):
        update_staging(db_conn, record['id'], 'SO_DRAFT',
                       error_log=f"SO en estado '{so['state']}'")
        stats['borrador'].append(so_name)
        return

    # --- BLINDAJE 4: re-validacion FAIL-CLOSED ---
    # Se vuelve a preguntar a Odoo justo antes de crear. Si la consulta falla,
    # NO se crea: es la leccion del 26-jul-2026.
    try:
        existentes = facturas_existentes(odoo, so_name)
    except Exception as e:
        update_staging(db_conn, record['id'], 'ERROR',
                       error_log=f'No se pudo re-validar duplicidad: {e}',
                       sumar_retry=True)
        return

    # Cuantas facturas DEBE tener esta orden. Normalmente una; tantas como
    # CFDI emitio Amazon cuando la orden viene repartida.
    desglose = extraer_desglose_b2b(record)
    esperadas = len(desglose) if desglose else 1

    if len(existentes) >= esperadas:
        if len(existentes) > esperadas:
            stats['duplicados'].append(so_name)
            log.error(f"{so_name}: {len(existentes)} facturas vivas y se "
                      f"esperaban {esperadas}. INCIDENTE FISCAL.")
        update_staging(db_conn, record['id'], 'ALREADY_ODOO_INVOICED',
                       error_log=f'Ya tenia {len(existentes)} factura(s) viva(s)')
        stats['ya_facturadas'] += 1
        return

    # --- Armado de las partes a facturar ---
    cfdi = extraer_cfdi_amazon(record) if es_b2b else {}

    if desglose:
        partes, motivo = armar_partes_split(odoo, so, desglose)
        if motivo == SIN_LINEAS_PENDIENTES:
            update_staging(db_conn, record['id'], 'ALREADY_ODOO_INVOICED',
                           error_log='Sin lineas pendientes de facturar')
            stats['ya_facturadas'] += 1
            return
        if motivo:
            # Fail-closed: o se crean TODAS las partes o ninguna. Dejar una
            # orden facturada a medias entre dos CFDI es peor que no tocarla.
            update_staging(db_conn, record['id'], 'B2B_SPLIT_MISMATCH',
                           error_log=motivo)
            stats['split_fallido'].append(f"{so_name}: {motivo}")
            return
    else:
        lineas = armar_lineas(odoo, so)
        if not lineas:
            update_staging(db_conn, record['id'], 'ALREADY_ODOO_INVOICED',
                           error_log='Sin lineas pendientes de facturar')
            return
        partes = [{
            'ref': f'{IDEMPOTENCY_PREFIX}{so_name}',
            'cfdi': cfdi,
            'lineas': lineas,
            'referencia': a_float(record['total_amount']),
            'etiqueta': '',
        }]

    # --- Validacion de importes ANTES de crear (patron de Mercado Libre) ---
    # Si lo que Odoo va a facturar no cuadra con la referencia de Amazon, no
    # se emite nada: queda en TOTAL_DIFF para el reporte de Finanzas. Un CFDI
    # equivocado no se corrige con un UPDATE.
    #
    # En una orden repartida se valida el conjunto: si una parte no cuadra, no
    # se factura ninguna.
    malas = []
    for parte in partes:
        parte['estimado'] = total_estimado(parte['lineas'])
        dif_previa = parte['estimado'] - parte['referencia']
        if parte['referencia'] > 0 and abs(dif_previa) > TOLERANCIA_TOTAL:
            malas.append((parte, dif_previa))

    if malas:
        detalle = '; '.join(
            f"{p['etiqueta'] or 'factura'}: Odoo {p['estimado']:,.2f} vs "
            f"Amazon {p['referencia']:,.2f} (dif {d:+,.2f})" for p, d in malas)
        update_staging(db_conn, record['id'], 'TOTAL_DIFF', error_log=detalle)
        stats['total_diff'].append(f"{so_name} [{tipo}] {detalle}")
        log.warning(f"{so_name}: TOTAL_DIFF, no se factura. {detalle}")
        return

    # --- B2B: cada factura replica el CFDI que Amazon ya emitio ---
    partner_b2b = None
    if es_b2b:
        partner_b2b = resolver_partner_b2b(odoo, cfdi, so, catalogos,
                                           partner_cache)
        if not partner_b2b:
            update_staging(
                db_conn, record['id'], 'B2B_NO_RECEPTOR',
                error_log=(f"CFDI de Amazon sin RFC receptor utilizable "
                           f"(receiver_rfc={cfdi.get('receiver_rfc')!r}). No se "
                           f"factura a publico general para no contradecir el "
                           f"comprobante de Amazon."))
            stats['b2b_sin_receptor'].append(so_name)
            return

    refs_ya_creadas = {e.get('ref') for e in existentes if e.get('ref')}

    for parte in partes:
        if parte['ref'] in refs_ya_creadas:
            # Reanudacion: una corrida anterior murio a la mitad del reparto.
            log.info(f"{so_name}{parte['etiqueta']}: ya existia "
                     f"(ref={parte['ref']}). Se omite.")
            continue
        creada = crear_parte(odoo, db_conn, record, so, parte, account_cxc,
                             stats, catalogos, es_b2b, tipo, partner_b2b)
        if creada is None:
            return   # el error ya quedo registrado en la tabla de control

    update_staging(db_conn, record['id'],
                   'ODOO_B2B_LINKED' if es_b2b else 'ODOO_INVOICED',
                   cfdi_uuid=record['cfdi_uuid'])


def crear_parte(odoo, db_conn, record, so, parte, account_cxc, stats,
                catalogos, es_b2b, tipo, partner_b2b):
    """Crea, sella y publica UNA factura. Devuelve el dict de resultado o None.

    Es el cuerpo que antes vivia dentro de procesar(). Se extrajo para que el
    reparto por CFDI (una factura por comprobante de Amazon) recorra
    exactamente el mismo camino, blindajes incluidos.
    """
    so_name = so['name']
    cfdi = parte['cfdi'] or {}

    invoice_vals = {
        'ref': parte['ref'],
        'move_type': 'out_invoice',
        'partner_id': PARTNER_ID_PUBLICO_GENERAL,
        'invoice_origin': so_name,
        'invoice_line_ids': parte['lineas'],
        'l10n_mx_edi_usage': 'S01',
        'l10n_mx_edi_payment_method_id': 3,
        'l10n_mx_edi_payment_policy': 'PUE',
        'team_id': so['team_id'][0],
        'invoice_payment_term_id': 1,   # pago inmediato -> PUE, no PPD
    }

    # Los valores de arriba son los de publico en general y solo aplican a
    # B2C. En B2B se sustituyen por los del comprobante real: receptor, uso de
    # CFDI, forma de pago y fecha de emision.
    if es_b2b:
        invoice_vals['partner_id'] = partner_b2b

        # Uso de CFDI del receptor (G01 adquisicion, G03 gastos...). Sin esto
        # la factura salia S01 "sin efectos fiscales", que es lo contrario de
        # lo que necesita un cliente que factura con su RFC.
        if cfdi.get('receiver_cfdi_key'):
            invoice_vals['l10n_mx_edi_usage'] = cfdi['receiver_cfdi_key']

        # Forma de pago: clave del SAT tal como la timbro Amazon (28 tarjeta
        # de debito, 03 transferencia...). Se resuelve contra el catalogo de
        # Odoo; si la clave no existiera se deja la que ya venia.
        forma = str(cfdi.get('forma_pago') or '').strip()
        if forma and forma in catalogos['formas_pago']:
            invoice_vals['l10n_mx_edi_payment_method_id'] = \
                catalogos['formas_pago'][forma]
        elif forma:
            log.warning(f"{so_name}: forma de pago '{forma}' del CFDI de "
                        f"Amazon no existe en el catalogo de Odoo.")

        if cfdi.get('metodo_pago'):
            invoice_vals['l10n_mx_edi_payment_policy'] = cfdi['metodo_pago']

        # Fecha del comprobante de Amazon, no la de la corrida.
        fecha_cfdi = str(cfdi.get('date') or '')[:10]
        if len(fecha_cfdi) == 10:
            invoice_vals['invoice_date'] = fecha_cfdi
            invoice_vals['date'] = fecha_cfdi
            invoice_vals['invoice_date_due'] = fecha_cfdi

        log.info(f"{so_name}{parte['etiqueta']}: B2B -> receptor "
                 f"{cfdi.get('receiver_rfc')} ({cfdi.get('receiver_name')}) | "
                 f"uso {invoice_vals['l10n_mx_edi_usage']} | "
                 f"forma {forma or 'n/d'} | fecha {fecha_cfdi or 'n/d'} | "
                 f"folio {cfdi.get('folio')}")

    # --- CREATE ---
    try:
        inv_id = odoo.execute_kw('account.move', 'create', [invoice_vals])
    except OdooWriteUncertain as e:
        # BLINDAJE 5: puede existir. Se busca POR SU REF en vez de reintentar.
        log.warning(f"{so_name}: create incierto. Buscando huerfana... ({e})")
        found = buscar_por_ref(odoo, parte['ref'])
        if found:
            inv_id = found[0]['id']
            stats['adoptadas'] += 1
            log.info(f"{so_name}: adoptada {found[0]['name']}")
        else:
            update_staging(db_conn, record['id'], 'ERROR',
                           error_log=f'WRITE_UNCERTAIN en create: {e}',
                           sumar_retry=True)
            stats['inciertos'].append(so_name)
            return None
    except xmlrpc.client.Fault as e:
        update_staging(db_conn, record['id'], 'ERROR',
                       error_log=f'Odoo rechazo el create: {e}', sumar_retry=True)
        return None

    # --- CxC de Amazon + UUID de Amazon en el caso B2B ---
    # Mismo mecanismo que 02_loader_ml_billing.py, salvo el adjunto: Amazon no
    # expone el XML, asi que no se puede crear ir.attachment ni
    # account.edi.document. Solo se sella el UUID.
    update_vals = {}
    move_lines = odoo.execute_kw('account.move.line', 'search_read',
                                 [[('move_id', '=', inv_id)]],
                                 {'fields': ['id', 'account_type']})
    cxc = [(1, m['id'], {'account_id': account_cxc})
           for m in move_lines if m['account_type'] == 'asset_receivable']
    if cxc:
        update_vals['line_ids'] = cxc

    uuid_amazon = cfdi.get('uuid') if es_b2b else None
    if uuid_amazon:
        update_vals['l10n_mx_edi_cfdi_uuid'] = uuid_amazon
        # La narracion lleva serie-folio ademas del UUID: es lo que Finanzas
        # busca cuando cruza contra el portal de Amazon Business.
        update_vals['narration'] = (
            f"CFDI Amazon Business {cfdi.get('series') or ''}-"
            f"{cfdi.get('folio') or ''} | UUID {uuid_amazon}").strip(' |-')

    if update_vals:
        try:
            odoo.execute_kw('account.move', 'write', [[inv_id], update_vals],
                            {'context': {'tracking_disable': True}})
        except OdooWriteUncertain as e:
            log.warning(f"{so_name}: write incierto de CxC/UUID: {e}")

    # --- POST: genera el asiento contable ---
    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{so_name}: post incierto, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        update_staging(db_conn, record['id'], 'ERROR',
                       error_log=f'Fallo el post: {e}', sumar_retry=True)
        return None

    # --- TIMBRADO: solo B2C. En B2B el comprobante ya existe. ---
    if DO_STAMPING and not es_b2b:
        try:
            ctx = {'active_model': 'account.move', 'active_ids': [inv_id]}
            wiz = odoo.execute_kw('account.move.send.wizard', 'create',
                                  [{'is_download_only': False}], {'context': ctx})
            odoo.execute_kw('account.move.send.wizard', 'action_send_and_print',
                            [[wiz]], {'context': ctx})
        except Exception as e:
            log.warning(f"{so_name}: fallo el timbrado: {e}")

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['name', 'state', 'amount_total',
                                      'l10n_mx_edi_cfdi_uuid']})
    inv = inv[0] if inv else {}
    uuid_final = inv.get('l10n_mx_edi_cfdi_uuid') or uuid_amazon or None
    if uuid_final is False:
        uuid_final = None

    postear_chatter(odoo, inv_id, record, tipo, uuid_final, parte)

    total_odoo = a_float(inv.get('amount_total'))
    total_amz = a_float(parte['referencia'])
    dif = total_odoo - total_amz

    resultado = {
        'tipo': tipo, 'so': so_name + parte['etiqueta'],
        'order_id': record['mkp_order_id'],
        'factura': inv.get('name'), 'total_odoo': total_odoo,
        'total_amazon': total_amz, 'diferencia': dif,
        'marca': 'OK' if abs(dif) <= TOLERANCIA_TOTAL else 'DIFERENCIA',
        'uuid': uuid_final,
    }
    stats['facturadas'].append(resultado)

    log.info(f"{so_name}{parte['etiqueta']}: {inv.get('name')} [{tipo}] "
             f"Odoo={total_odoo:,.2f} Amazon={total_amz:,.2f} dif={dif:+,.2f}")
    return resultado


def run_loader(db_conn):
    reset_stuck_processing(db_conn)
    records = fetch_pendientes(db_conn)
    if not records:
        log.info("[LOADER] Sin registros pendientes.")
        return None

    log.info(f"[LOADER] {len(records)} registro(s) por procesar.")

    # Red de seguridad antes de tocar Odoo: ninguna orden con CFDI vivo de
    # Amazon puede facturarse a publico general.
    reclasificadas = detectar_b2b_tardio(db_conn, records)
    if reclasificadas:
        records = [r for r in records if r['mkp_order_id'] not in reclasificadas]
        stats_reclasificadas = len(reclasificadas)
    else:
        stats_reclasificadas = 0
    odoo = OdooModelProxy(timeout=300)
    account_cxc = get_account_id(odoo, ACCOUNT_AMAZON_CXC)
    catalogos = cargar_catalogos(odoo)
    partner_cache = {}

    stats = {'facturadas': [], 'ya_facturadas': 0, 'adoptadas': 0,
             'sin_so': [], 'canceladas': [], 'borrador': [],
             'duplicados': [], 'inciertos': [], 'b2b_sin_receptor': [],
             'total_diff': [], 'split_fallido': [],
             'reclasificadas_b2b': stats_reclasificadas,
             'cuota': {'B2B': 0, 'B2C_PUBLICO_GENERAL': 0}}

    hechas = stats['cuota']

    for idx, record in enumerate(records):
        # El tipo se conoce por el status pendiente, ANTES de reclamar el
        # registro. Importa: reclamar lo pone en PROCESSING, y no tiene
        # sentido marcar como en proceso algo que no se va a tocar.
        tipo = ('B2B' if record['status'] == 'PENDING_B2B_LINK'
                else 'B2C_PUBLICO_GENERAL')
        cupo = QUOTA_B2B if tipo == 'B2B' else QUOTA_B2C

        if cupo is not None and hechas[tipo] >= cupo:
            continue
        if (QUOTA_B2B is not None
                and hechas['B2B'] >= QUOTA_B2B
                and hechas['B2C_PUBLICO_GENERAL'] >= QUOTA_B2C):
            log.info(f"[LOADER] Cuota de pruebas completa: "
                     f"B2B {hechas['B2B']}/{QUOTA_B2B}, "
                     f"B2C {hechas['B2C_PUBLICO_GENERAL']}/{QUOTA_B2C}.")
            break

        if not claim(db_conn, record['id']):
            continue

        # Marca de agua de la lista: solo cuenta si procesar() agrego una
        # factura REAL. Los desenlaces sin factura (SO cancelada, sin SO, ya
        # facturada) no consumen cuota; el loader sigue buscando.
        antes = len(stats['facturadas'])
        try:
            procesar(odoo, db_conn, record, account_cxc, stats,
                     catalogos, partner_cache)
        except Exception as e:
            log.error(f"{record['odoo_so_name']}: error no controlado: {e}")
            update_staging(db_conn, record['id'], 'ERROR',
                           error_log=str(e), sumar_retry=True)

        if len(stats['facturadas']) > antes:
            hechas[stats['facturadas'][-1]['tipo']] += 1

        if (idx + 1) % LOG_PROGRESS_EVERY == 0:
            log.info(f"  ...{idx + 1}/{len(records)}")

    return stats


# =============================================================================
# REPORTE
# =============================================================================

def reportar(stats):
    if not stats:
        return
    f = stats['facturadas']
    b2b = [r for r in f if r['tipo'] == 'B2B']
    b2c = [r for r in f if r['tipo'] != 'B2B']
    difs = [r for r in f if r['marca'] == 'DIFERENCIA']

    log.info('')
    log.info('=' * 96)
    log.info('FACTURACION AMAZON -- RESUMEN')
    log.info('=' * 96)
    if f:
        log.info(f"{'TIPO':<22}{'SO':<14}{'FACTURA':<16}"
                 f"{'TOTAL ODOO':>13}{'TOTAL AMZ':>13}{'DIF':>10}  ESTADO")
        log.info('-' * 96)
        for r in f:
            log.info(f"{r['tipo']:<22}{r['so']:<14}{str(r['factura']):<16}"
                     f"{r['total_odoo']:>13,.2f}{r['total_amazon']:>13,.2f}"
                     f"{r['diferencia']:>+10,.2f}  {r['marca']}")
        log.info('-' * 96)

    log.info(f"  B2B vinculadas (CFDI de Amazon) : {len(b2b)}")
    log.info(f"  B2C emitidas por WB             : {len(b2c)}")
    log.info(f"  Con diferencia > $1.00          : {len(difs)}")
    log.info(f"  Ya estaban facturadas           : {stats['ya_facturadas']}")
    log.info(f"  SO canceladas (requieren NC)    : {len(stats['canceladas'])}")
    log.info(f"  SO en borrador                  : {len(stats['borrador'])}")
    log.info(f"  Sin SO en Odoo                  : {len(stats['sin_so'])}")
    log.info(f"  B2B sin receptor en el CFDI     : {len(stats['b2b_sin_receptor'])}")
    log.info(f"  TOTAL_DIFF (no facturadas)      : {len(stats['total_diff'])}")
    log.info(f"  Reparto B2B fallido             : {len(stats['split_fallido'])}")
    log.info(f"  Reclasificadas a B2B (tardias)  : {stats['reclasificadas_b2b']}")
    log.info(f"  Adoptadas tras create incierto  : {stats['adoptadas']}")
    log.info(f"  Escrituras inciertas            : {len(stats['inciertos'])}")
    log.info('=' * 96)

    # Cuota de pruebas: decir si se lleno y, si no, por que.
    if QUOTA_B2B is not None:
        h = stats['cuota']
        log.info(f"  CUOTA -> B2B {h['B2B']}/{QUOTA_B2B} | "
                 f"B2C {h['B2C_PUBLICO_GENERAL']}/{QUOTA_B2C}")
        if h['B2B'] < QUOTA_B2B or h['B2C_PUBLICO_GENERAL'] < QUOTA_B2C:
            # Se nombra la causa dominante en vez de listar posibles remedios:
            # casi siempre es merma (candidatos que ya tenian factura), no
            # falta de candidatos.
            descartes = (stats['ya_facturadas'] + len(stats['canceladas'])
                         + len(stats['borrador']) + len(stats['sin_so'])
                         + len(stats['total_diff']))
            hechas_total = h['B2B'] + h['B2C_PUBLICO_GENERAL']
            log.warning(
                f"Cuota NO alcanzada. De los candidatos sembrados, "
                f"{descartes} se descartaron ({stats['ya_facturadas']} ya "
                f"facturadas, {len(stats['canceladas'])} canceladas, "
                f"{len(stats['sin_so'])} sin SO, {len(stats['borrador'])} en "
                f"borrador, {len(stats['total_diff'])} con descuadre) y solo "
                f"{hechas_total} produjeron factura.")
            if stats['ya_facturadas'] > hechas_total:
                log.warning(
                    f"La causa dominante es que ya estaban facturadas: en una "
                    f"ventana pasada el 1a1 generico ya las recorrio. Subir "
                    f"AMZ_MINER_OVERFETCH (hoy {MINER_OVERFETCH}) o correr "
                    f"00_conteo_lote_test.py, que ya descuenta las que tienen "
                    f"factura viva en Odoo.")

    if difs:
        log.info('')
        log.info('REVISAR -- diferencia Amazon vs Odoo:')
        for r in difs:
            log.info(f"  {r['so']:<14} order={r['order_id']} "
                     f"dif={r['diferencia']:+,.2f}")
        log.info('  Diferencia residual DESPUES de crear: solo deberia salir '
                 'por redondeo de Odoo. Si es mayor, revisar la tolerancia.')

    if stats['total_diff']:
        log.info('')
        log.info(f"TOTAL_DIFF -- {len(stats['total_diff'])} orden(es) NO "
                 f"facturadas por descuadre contra Amazon:")
        for d in stats['total_diff'][:20]:
            log.info(f"  {d}")
        log.info('  Quedan en la tabla de control para el reporte de Finanzas. '
                 'Causa habitual: el precio de la SO difiere del liquidado por '
                 'Amazon, o la SO no incluye un flete que Amazon si cobro.')

    if stats['split_fallido']:
        log.info('')
        log.warning(f"REPARTO B2B FALLIDO -- {len(stats['split_fallido'])} "
                    f"orden(es) con varios CFDI que no se pudieron partir:")
        for d in stats['split_fallido'][:20]:
            log.warning(f"  {d}")

    if stats['duplicados']:
        u = sorted(set(stats['duplicados']))
        notify_slack(
            f":rotating_light: *{len(u)} orden(es) de Amazon con CFDI "
            f"DUPLICADO* (`{KESTRA_EXECUTION_ID}`).\nRequieren cancelacion "
            f"fiscal manual; el script las aparto.\n{', '.join(u[:20])}")

    if stats['inciertos']:
        notify_slack(
            f":rotating_light: *{len(stats['inciertos'])} factura(s) de Amazon "
            f"en estado INCIERTO* (`{KESTRA_EXECUTION_ID}`).\nNo se "
            f"reintentaron para no duplicar. Verificar en Odoo antes de "
            f"reprocesar.\n{', '.join(stats['inciertos'][:15])}")

    if stats['canceladas']:
        notify_slack(
            f":warning: *{len(stats['canceladas'])} orden(es) de Amazon "
            f"CANCELADAS* que Amazon si liquido. Quedan sin facturar y "
            f"requieren nota de credito Tipo A.\n"
            f"{', '.join(stats['canceladas'][:15])}")

    if stats['sin_so']:
        notify_slack(
            f":mag: *{len(stats['sin_so'])} orden(es) de Amazon sin SO en "
            f"Odoo*. Corresponde a Yuju crearlas.\n"
            f"{', '.join(stats['sin_so'][:15])}")


def main():
    validar_entorno()

    # Un tope superior en produccion deja de facturar todo lo posterior sin
    # que nada falle. Si alguien lo pone, que quede en el log.
    if ENTORNO == 'prod' and AMZ_END_DATE:
        log.warning(f"AMZ_END_DATE={AMZ_END_DATE} en PRODUCCION: no se "
                    f"facturara nada posterior a esa fecha. Verificar que sea "
                    f"intencional (reproceso acotado) y no un descuido.")
        notify_slack(f":warning: Facturacion Amazon corriendo con tope "
                     f"AMZ_END_DATE={AMZ_END_DATE}. Nada posterior a esa fecha "
                     f"se facturara en esta corrida.")

    db_conn = get_db_connection()

    if RUN_MINER:
        log.info('--- FASE A: MINER ---')
        run_miner(db_conn)

    stats = None
    if RUN_LOADER:
        log.info('')
        log.info('--- FASE B: LOADER ---')
        stats = run_loader(db_conn)

    db_conn.close()
    reportar(stats)


if __name__ == '__main__':
    banner('FACTURACION AMAZON (MINER + LOADER)')
    _cuota = ('sin limite' if QUOTA_B2B is None
              else f"B2B={QUOTA_B2B} B2C={QUOTA_B2C} (facturas creadas)")
    log.info(f"Timbrado: {'ACTIVADO' if DO_STAMPING else 'DESACTIVADO'} | "
             f"Cuota: {_cuota} | "
             f"Gracia: {GRACE_DAYS} dias | Ventana: {AMZ_START_DATE} .. "
             f"{AMZ_END_DATE or 'hoy'}")
    t0 = tm.time()
    try:
        main()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico en facturacion Amazon: {e}')
        notify_slack(f":x: Fallo critico en facturacion Amazon "
                     f"(`{KESTRA_EXECUTION_ID}`): {e}")
        sys.exit(1)
