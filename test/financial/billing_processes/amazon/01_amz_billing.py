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
  ALREADY_ODOO_INVOICED  ya tenia factura viva en Odoo (incluye factura global)
  B2B_FACTURADA_PG       B2B con factura viva a publico general: anularla
                         antes de vincular el CFDI de Amazon. Se reevalua.
  SKIPPED_GRACE          dentro del periodo de gracia
  TOTAL_DIFF             el importe de Odoo no cuadra con el de Amazon. NO se
                         factura; sale en el reporte de Finanzas.
  B2B_SPLIT_MISMATCH     Amazon emitio varios CFDI y no se pudieron repartir
                         contra los envios / las lineas de la SO.
  B2B_NO_RECEPTOR        el CFDI de Amazon no trae RFC receptor utilizable, u
                         Odoo rechazo el alta del cliente. Se reevalua.
  SO_CANCELLED           ya no se produce: la SO cancelada se factura (correo
                         de Jero, 28-oct-2026). Filas viejas se reevaluan.
  SO_DRAFT               SO en borrador que Odoo no dejo confirmar. Se
                         reevalua; la que si se confirma, se factura.
  ENVIO_SIN_CONFIRMAR    el cliente pago mas que la SO y ninguna segunda
                         fuente confirma que sea envio. Se reevalua.
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
    ACCOUNT_MERC_NO_ENVIADA, IDEMPOTENCY_PREFIX, KESTRA_EXECUTION_ID,
    validar_entorno, banner, notify_slack, get_db_connection,
    OdooModelProxy, OdooWriteUncertain, get_account_id, a_float,
    ejecutar_sin_retorno, sp_api_token, get_order_items,
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
AMZ_START_DATE = os.getenv('AMZ_START_DATE', '2026-08-27')

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

_END_DATE_DEFECTO = '2026-10-04' if ENTORNO == 'staging' else ''
AMZ_END_DATE = os.getenv('AMZ_END_DATE', _END_DATE_DEFECTO).strip() or None

# =============================================================================
# ALCANCE POR SETTLEMENT -- PRIMERA CORRIDA EN PRODUCCION
# =============================================================================
# Finanzas (correo de Jero, 28-oct-2026) pide que la primera corrida real sea
# un settlement COMPLETO, para cuadrar la transitoria 102.02.008 contra lo
# liberado antes de soltar el resto. Con AMZ_SETTLEMENT_ID el miner toma
# solo las ordenes con Order Payment en ese settlement (TODAS sus piezas,
# aunque alguna haya caido en otro settlement: la factura es por orden) y el
# loader procesa solo esas. Las fechas AMZ_START_DATE / AMZ_END_DATE dejan de
# acotar: manda el settlement.
#
# AMZ_ORDER_IDS (lista separada por comas) agrega ordenes sueltas al alcance.
# Es para pruebas: meter en el lote casos que no estan en el settlement (una
# orden en factura global, las excepciones del lote anterior).
#
# Con alcance, las cuotas de pruebas no aplican: se procesa todo el alcance.
# =============================================================================
AMZ_SETTLEMENT_ID = os.getenv('AMZ_SETTLEMENT_ID', '').strip() or None
AMZ_ORDER_IDS = [o.strip() for o in os.getenv('AMZ_ORDER_IDS', '').split(',') if o.strip()]
CON_ALCANCE = bool(AMZ_SETTLEMENT_ID or AMZ_ORDER_IDS)

# Dias que se le dan al cliente para autofacturar con su RFC antes de que
# emitamos a publico en general.
GRACE_DAYS = int(os.getenv('AMZ_GRACE_DAYS', '5'))

# Margen hacia atras en la subquery de deduplicacion. Las re-publicaciones de
# Amazon ocurren dentro de ~15-45 dias; 90 da holgura y evita que el GROUP BY
# recorra todo el historico en cada corrida.
DEDUP_LOOKBACK_DAYS = 90

# Contacto generico para clientes B2B nuevos. Odoo (validacion de "Ventas al
# Mayoreo") no deja crear un contacto sin telefono ni correo, y el CFDI de
# Amazon Business no los trae: en el lote del 27-oct, 12 de 15 B2B quedaron sin
# factura por eso. Se usa lo mismo que los contactos genericos de Yuju
# (Mercado Libre Oficiales: 11111 / market@somos-reyes.com). El correo es un
# buzon interno a proposito: nada que Odoo mande al cliente sale de la empresa.
# Envio cobrado al cliente. El ingreso (401.01.002) lo pone la cuenta del
# producto: Finanzas se la asigno en produccion el 28-oct, segundo (antes caia en
# 401.01.001 en todos los canales).
PRODUCTO_ENVIO = 'C-ENVIO'

B2B_TELEFONO_GENERICO = os.getenv('AMZ_B2B_TELEFONO', '11111')
B2B_CORREO_GENERICO = os.getenv('AMZ_B2B_CORREO', 'market@somos-reyes.com')

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

QUOTA_B2B = None if ENTORNO == 'prod' or CON_ALCANCE else int(os.getenv('AMZ_TEST_QUOTA_B2B', '6'))
QUOTA_B2C = None if ENTORNO == 'prod' or CON_ALCANCE else int(os.getenv('AMZ_TEST_QUOTA_B2C', '30'))

# =============================================================================
# TOPE DE ARRANQUE EN PRODUCCION
# =============================================================================
# En produccion las cuotas son None: se factura todo lo elegible. Para las
# primeras corridas reales (y para el barrido historico desde el 17-abr-2026,
# ~1,088 SO) conviene avanzar por tramos y revisar entre uno y otro. Este tope
# corta el LOADER despues de N facturas creadas en total (B2B + B2C). Lo que
# queda pendiente sigue en la tabla de control y lo toma la corrida siguiente
# (el loader va en orden FIFO en produccion).
#
# Solo aplica con AMZ_ENTORNO='prod'; en pruebas mandan las cuotas por tipo.
# 0 o vacio = sin tope. Una vez estable, se quita la variable del flujo.
# =============================================================================
PROD_MAX_FACTURAS = (int(os.getenv('AMZ_PROD_MAX_FACTURAS', '0') or 0) or None
                     if ENTORNO == 'prod' else None)

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
#
# Excepciones que se resuelven solas en una corrida posterior y por eso se
# reevaluan (correo de Jero, 28-oct-2026: "las excepciones se resuelven, no
# se saltean"): SO_DRAFT (fallo la confirmacion), SO_CANCELLED (filas de
# antes de que se facturaran las SO canceladas), B2B_NO_RECEPTOR (Odoo
# rechazo el alta del cliente) y B2B_FACTURADA_PG (en cuanto se anule la
# factura a publico general, la orden se vincula al CFDI de Amazon).
REFRESCABLES = ('PENDING_B2B_LINK', 'PENDING_WB_INVOICE', 'SKIPPED_GRACE', 'ERROR',
                'SO_DRAFT', 'SO_CANCELLED', 'B2B_NO_RECEPTOR', 'B2B_FACTURADA_PG',
                'ENVIO_SIN_CONFIRMAR')

# Las que el miner regresa a pendiente al refrescarlas (todas menos ERROR, que
# se reintenta por retry_count_loader y lo revisa una persona).
REABRIBLES = tuple(s for s in REFRESCABLES if s != 'ERROR')

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
    MAX(d.tot_min)                                AS amz_order_total_min,
    MAX(d.tot_max)                                AS amz_order_total_max,
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
    -- Envio cobrado al cliente (neto de envio gratis), base e IVA. Yuju no
    -- pone la linea de envio en la SO: el loader la agrega a la factura.
    ROUND(SUM(IFNULL(b.shipping,0) + IFNULL(b.shipping_discount,0)), 2)
                                                  AS envio_base,
    ROUND(SUM(IFNULL(b.shipping_tax,0) + IFNULL(b.shipping_tax_discount,0)), 2)
                                                  AS envio_iva,
    -- Lo que Amazon deposito por la orden. Contra venta - comisiones - IVA
    -- retenido revela un envio acreditado fuera de los campos de envio.
    ROUND(SUM(IFNULL(b.item_amount,0)), 2)        AS deposito,
    -- amazon_fees YA incluye amazon_for_all_fee y shipping_hb: sumarlos
    -- aparte los contaba dos veces (24% de las piezas). Asi cfdi_total +
    -- fees_total = item_amount, el deposito.
    ROUND(SUM(IFNULL(b.amazon_fees,0) + IFNULL(b.marketplace_vat_principal,0)
            + IFNULL(b.marketplace_vat_shipping,0)), 2)        AS fees_total,

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

-- CORRECCION 1: deduplicado.
-- Trae ademas lo que Amazon le COBRO al comprador (OrderTotalAmount), que es
-- la fuente independiente de la regla de promocion financiada por Amazon en
-- B2C (ver PROMOCIONES FINANCIADAS POR AMAZON). amazon_orders tiene N filas
-- por orden: se guardan el minimo y el maximo, y si no coinciden el loader
-- descarta el dato en vez de elegir uno. Se ignoran importes en cero o vacios
-- (filas de una orden todavia pendiente) y monedas distintas de MXN.
LEFT JOIN (
    SELECT amazonorderid, MAX(IsBusinessOrder) AS IsBusinessOrder,
           MIN(CASE WHEN OrderTotalCurrency = 'MXN' AND OrderTotalAmount > 0
                    THEN CAST(OrderTotalAmount AS DECIMAL(14,2)) END) AS tot_min,
           MAX(CASE WHEN OrderTotalCurrency = 'MXN' AND OrderTotalAmount > 0
                    THEN CAST(OrderTotalAmount AS DECIMAL(14,2)) END) AS tot_max
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


def filtro_alcance():
    """(sql, params) que limita las ordenes al settlement y/o la lista de
    AMZ_ORDER_IDS. Vacio si no hay alcance."""
    partes, params = [], []
    if AMZ_SETTLEMENT_ID:
        partes.append("a.order_id IN (SELECT s.order_id FROM somos_reyes.amz_transactions s"
                      " WHERE s.settlement_id = %s AND s.description = 'Order Payment'"
                      " AND s.transaction_status <> 'DEFERRED')")
        params.append(AMZ_SETTLEMENT_ID)
    if AMZ_ORDER_IDS:
        partes.append(f"a.order_id IN ({', '.join(['%s'] * len(AMZ_ORDER_IDS))})")
        params += AMZ_ORDER_IDS
    if not partes:
        return '', []
    return f"AND ({' OR '.join(partes)})", params


def inicio_del_alcance(db_conn):
    """Primer dia con Order Payment de las ordenes del alcance (fecha local).

    Con alcance no manda AMZ_START_DATE sino el settlement: hay que leer
    desde la primera pieza de sus ordenes, aunque haya caido en un settlement
    anterior, o la factura saldria corta.
    """
    filtro, params = filtro_alcance()
    cur = db_conn.cursor()
    cur.execute(f"""SELECT DATE(DATE_SUB(MIN(a.posted_date), INTERVAL 6 HOUR))
                    FROM somos_reyes.amz_transactions a
                    WHERE a.description = 'Order Payment'
                      AND a.transaction_status <> 'DEFERRED' {filtro}""", tuple(params))
    fila = cur.fetchone()
    cur.close()
    if not fila or not fila[0]:
        raise Exception(f"El alcance (settlement {AMZ_SETTLEMENT_ID}, ordenes "
                        f"{AMZ_ORDER_IDS}) no tiene Order Payment en Amazon.")
    return str(fila[0])


def construir_query_lote(desde=None):
    """QUERY_LOTE lista para ejecutar, con sus parametros.

    Devuelve (sql, params) para que quien la use no tenga que saber cuantos
    placeholders lleva segun haya o no limite superior de ventana.

    Con alcance (AMZ_SETTLEMENT_ID / AMZ_ORDER_IDS) la ventana empieza en
    'desde' (inicio_del_alcance) y no hay limite superior.
    """
    if CON_ALCANCE:
        inicio = desde or AMZ_START_DATE
        filtro, extra = filtro_alcance()
        sql = QUERY_LOTE.replace('{FILTRO_FIN}', filtro)
        return sql, tuple([inicio, DEDUP_LOOKBACK_DAYS, inicio] + extra)

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


def total_cobrado_cliente(row):
    """Lo que Amazon le cobro al comprador (amazon_orders.OrderTotalAmount).

    None si no hay dato o si las filas de amazon_orders no coinciden entre si:
    en ese caso no se elige ninguna y la regla de promocion no se aplica (la
    orden sigue el camino normal, que termina en TOTAL_DIFF si no cuadra).
    """
    lo, hi = row.get('amz_order_total_min'), row.get('amz_order_total_max')
    if lo is None or hi is None:
        return None
    if abs(a_float(lo) - a_float(hi)) > 0.005:
        return None
    return a_float(hi)


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
            'envio_base': a_float(row.get('envio_base')),
            'envio_iva': a_float(row.get('envio_iva')),
            'deposito': a_float(row.get('deposito')),
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
            # Cobrado al comprador segun amazon_orders. Lo usa la regla de
            # promocion financiada por Amazon (B2C). min/max se guardan para
            # auditar por que se descarto cuando no coinciden.
            'order_total_cliente': total_cobrado_cliente(row),
            'order_total_min': a_float(row.get('amz_order_total_min')) or None,
            'order_total_max': a_float(row.get('amz_order_total_max')) or None,
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


# Ordenes que leyo el miner cuando hay alcance (settlement / lista). El loader
# procesa solo estas: sin esto tomaria tambien pendientes viejos de la tabla
# y el settlement ya no se podria cuadrar por separado.
ORDENES_DEL_ALCANCE = set()


def run_miner(db_conn):
    lector = db_conn.cursor(dictionary=True)
    if CON_ALCANCE:
        desde = inicio_del_alcance(db_conn)
        log.info(f"[MINER] ALCANCE: settlement {AMZ_SETTLEMENT_ID or '-'} + "
                 f"{len(AMZ_ORDER_IDS)} orden(es) sueltas. Transacciones desde "
                 f"{desde} (dedup con {DEDUP_LOOKBACK_DAYS} dias de margen)...")
    else:
        desde = None
        log.info(f"[MINER] Leyendo transacciones de Amazon desde {AMZ_START_DATE}"
                 f"{' hasta ' + AMZ_END_DATE if AMZ_END_DATE else ''} "
                 f"(dedup con {DEDUP_LOOKBACK_DAYS} dias de margen)...")
    t0 = tm.time()
    sql, params = construir_query_lote(desde)
    lector.execute(sql, params)
    rows = lector.fetchall()
    lector.close()
    log.info(f"[MINER] {len(rows)} orden(es) leidas en "
             f"{round(tm.time() - t0, 1)}s")
    if CON_ALCANCE:
        ORDENES_DEL_ALCANCE.update(r['mkp_order_id'] for r in rows)

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
        # Las listas de estados son constantes del codigo, no datos de entrada.
        refrescables = ', '.join(f"'{s}'" for s in REFRESCABLES)
        reabribles = ', '.join(f"'{s}'" for s in REABRIBLES)
        w.execute(f"""
            INSERT INTO {TBL_BILLING}
                (marketplace, mkp_order_id, odoo_so_name, cfdi_uuid,
                 total_amount, xml_data, raw_json, status,
                 retry_count, retry_count_loader)
            VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, 0, 0)
            ON DUPLICATE KEY UPDATE
                odoo_so_name = IF(status IN ({refrescables}), VALUES(odoo_so_name), odoo_so_name),
                cfdi_uuid    = IF(status IN ({refrescables}), VALUES(cfdi_uuid),    cfdi_uuid),
                total_amount = IF(status IN ({refrescables}), VALUES(total_amount), total_amount),
                raw_json     = IF(status IN ({refrescables}), VALUES(raw_json),     raw_json),
                status       = IF(status IN ({reabribles}),   VALUES(status),       status)
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
        if CON_ALCANCE:
            rows = [r for r in rows if r['mkp_order_id'] in ORDENES_DEL_ALCANCE]
            log.info(f"[LOADER] Alcance: {len(rows)} pendiente(s) de "
                     f"{len(ORDENES_DEL_ALCANCE)} orden(es) leidas por el miner.")
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
                   sumar_retry=False, total_amount=None):
    """total_amount solo se pasa cuando lo facturado no es la referencia que
    sembro el miner (promocion financiada por Amazon): asi la tabla dice lo
    que se facturo, igual que en B2B, y error_log explica la diferencia."""
    cursor = db_conn.cursor()
    cursor.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = %s,
            cfdi_uuid = COALESCE(%s, cfdi_uuid),
            error_log = %s,
            retry_count_loader = retry_count_loader + %s,
            total_amount = COALESCE(%s, total_amount),
            processed_at = NOW()
        WHERE id = %s
    """, (status, cfdi_uuid, error_log, 1 if sumar_retry else 0,
          total_amount, record_id))
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
                                    'invoice_ids', 'order_line', 'partner_id',
                                    'date_order'],
                         'limit': 1})
    return r[0] if r else None


def facturas_existentes(odoo, so_name, so=None):
    """Facturas vivas de la SO, con su ref.

    Antes esto era un search_count. Ahora devuelve las filas porque una orden
    puede tener legitimamente VARIAS facturas (una por CFDI de Amazon) y hace
    falta saber cuales ya se crearon para reanudar sin duplicar.

    La ref sigue llevando el prefijo compartido 'AUTOINV:' a proposito: asi el
    1 a 1 generico y este script se siguen viendo mutuamente.

    Tres caminos (correo de Jero, 28-oct-2026: "SO dentro de una factura
    global anterior: no refacturar"):
      1. invoice_origin = SO       la factura 1 a 1
      2. ref AUTOINV:<SO>...       la clave de idempotencia
      3. sale.order.invoice_ids    lo ligado a las lineas de la SO. Es el unico
                                   que ve una factura GLOBAL: su invoice_origin
                                   lista cientos de SO separadas por coma.
    Se descartan las facturas ya revertidas por completo con NC
    (payment_state 'reversed'): no son una factura viva de la orden, y
    contarlas daria un falso "incidente fiscal" en la SO refacturada.
    """
    domain = ['&', ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
              '|', ('invoice_origin', '=', so_name),
              ('ref', 'like', f'{IDEMPOTENCY_PREFIX}{so_name}%')]
    campos = ['id', 'name', 'state', 'ref', 'amount_total', 'invoice_origin',
              'payment_state', 'l10n_mx_edi_cfdi_uuid']
    filas = odoo.execute_kw('account.move', 'search_read', [domain],
                            {'fields': campos, 'order': 'id asc'})
    vistas = {f['id'] for f in filas}
    ligadas = [i for i in (so or {}).get('invoice_ids') or [] if i not in vistas]
    if ligadas:
        filas += odoo.execute_kw(
            'account.move', 'search_read',
            [[('id', 'in', ligadas), ('move_type', '=', 'out_invoice'),
              ('state', '!=', 'cancel')]],
            {'fields': campos, 'order': 'id asc'})
    return [f for f in filas if f.get('payment_state') != 'reversed']


def netear_con_nc(odoo, so, existentes):
    """Quita de 'existentes' las facturas neutralizadas por una NC publicada
    de la misma SO y del mismo importe. Devuelve (netas, neutralizadas).

    Caso del lote 7 (44 de 46 alertas de "INCIDENTE FISCAL"): refacturacion
    del portal a un cliente con RFC = factura a publico general + NC "por
    refacturacion" + factura al cliente, las tres timbradas y sin conciliar
    entre si (por eso la primera no sale como 'reversed'). Hay UNA venta con
    UNA factura vigente, no un CFDI duplicado. Una NC en borrador no
    neutraliza nada: ese si es un doble real y se sigue alertando.
    """
    if len(existentes) < 2:
        return existentes, []
    ncs = odoo.execute_kw(
        'account.move', 'search_read',
        [['&', '&', ('move_type', '=', 'out_refund'), ('state', '=', 'posted'),
          '|', ('id', 'in', so.get('invoice_ids') or []),
          ('invoice_origin', '=', so['name'])]],
        {'fields': ['amount_total']})
    netas, neutralizadas = list(existentes), []
    for nc in ncs:
        # La que se neutraliza es la mas vieja del mismo importe: la de
        # publico general que se refacturo.
        par = next((f for f in netas
                    if abs(a_float(f['amount_total']) - a_float(nc['amount_total'])) <= 0.01), None)
        if par:
            netas.remove(par)
            neutralizadas.append(par)
    return netas, neutralizadas


def es_global(factura):
    """La factura ampara varias SO (factura global a publico en general)."""
    return ',' in (factura.get('invoice_origin') or '')


def confirmar_so(odoo, so):
    """Confirma una SO en borrador. Devuelve None si quedo confirmada, o el
    motivo por el que no.

    Se relee el estado despues: action_confirm puede no fallar y aun asi no
    confirmar (una validacion que regresa un asistente en vez de error).
    """
    try:
        odoo.execute_kw('sale.order', 'action_confirm', [[so['id']]])
    except OdooWriteUncertain as e:
        log.warning(f"{so['name']}: confirmacion incierta, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        return f"Odoo no permitio confirmar la SO: {e.faultString[:300]}"
    estado = odoo.execute_kw('sale.order', 'read', [[so['id']]],
                             {'fields': ['state']})[0]['state']
    if estado != 'sale':
        return f"La SO sigue en '{estado}' despues de confirmarla."
    so['state'] = estado
    log.info(f"{so['name']}: SO en borrador confirmada (Amazon ya cobro la venta).")
    return None


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


# Ref de la factura aparte de envio: AUTOINV:<SO>:ENVIO. Lleva el prefijo
# compartido, asi el 1a1 y este script la ven y no la duplican.
SUFIJO_ENVIO = ':ENVIO'


def facturar_envio_aparte(odoo, db_conn, record, so, vivas, de_envio,
                          account_cxc, catalogos, stats):
    """Factura solo por el envio cuando la orden ya tiene factura sin envio.

    Caso de Jero (28-oct, segundo): 702-0447823-8163428, factura del 1a1 por 1,600.00
    y el cliente pago 1,659.00. Con la factura de 59.00 por el envio la SO
    suma lo que pago el cliente y la promocion de 199.00 se puede asentar
    (03 compara la SUMA de las facturas de la SO contra OrderTotal).

    Mismo receptor y uso de CFDI que la factura que complementa. Idempotente
    por ref. Devuelve la nota para la tabla de control, o None.
    """
    so_name = so['name']
    if de_envio:
        return f"Envio ya facturado aparte: {de_envio[0]['name']}"

    ids = [v['id'] for v in vivas]
    ya_facturado = round(sum(a_float(v['amount_total']) for v in vivas), 2)
    # Primero con los datos de Amazon (sin llamar a Odoo): la gran mayoria de
    # las ordenes ya facturadas no tienen envio pendiente.
    envio = envio_por_facturar(db_conn, record, ya_facturado, catalogos, False)
    if not envio:
        return None
    if odoo.execute_kw('account.move.line', 'search_count',
                       [[('move_id', 'in', ids),
                         ('product_id', '=', catalogos.get('producto_envio'))]]):
        return None
    if envio.get('error'):
        stats['envio_sin_confirmar'].append(f"{so_name}: {envio['error']}")
        return f"ENVIO_SIN_CONFIRMAR: {envio['error']}"

    ref = f"{IDEMPOTENCY_PREFIX}{so_name}{SUFIJO_ENVIO}"
    ya = buscar_por_ref(odoo, ref)
    if ya:
        return f"Envio ya facturado aparte: {ya[0]['name']}"

    base = odoo.execute_kw('account.move', 'read', [[ids[0]]],
                           {'fields': ['partner_id', 'l10n_mx_edi_usage',
                                       'l10n_mx_edi_payment_method_id']})[0]
    vals = {
        'ref': ref,
        'move_type': 'out_invoice',
        'partner_id': base['partner_id'][0] if base['partner_id'] else PARTNER_ID_PUBLICO_GENERAL,
        'invoice_origin': so_name,
        'invoice_line_ids': [comando_envio(envio['base'], catalogos)],
        'l10n_mx_edi_usage': base.get('l10n_mx_edi_usage') or 'S01',
        'l10n_mx_edi_payment_method_id': (base['l10n_mx_edi_payment_method_id'][0]
                                          if base.get('l10n_mx_edi_payment_method_id') else 3),
        'l10n_mx_edi_payment_policy': 'PUE',
        'team_id': so['team_id'][0],
        'invoice_payment_term_id': 1,
    }
    try:
        inv_id = odoo.execute_kw('account.move', 'create', [vals])
    except OdooWriteUncertain:
        ya = buscar_por_ref(odoo, ref)
        if not ya:
            return "ENVIO: create incierto; revisar a mano"
        inv_id = ya[0]['id']
    except xmlrpc.client.Fault as e:
        return f"ENVIO: Odoo rechazo la factura de envio: {e.faultString[:200]}"

    cxc = odoo.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('account_type', '=', 'asset_receivable')]],
                          {'fields': ['id']})
    if cxc:
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'line_ids': [(1, l['id'], {'account_id': account_cxc}) for l in cxc]}],
                        {'context': {'tracking_disable': True}})
    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{so_name}: post incierto de la factura de envio: {e}")
    except xmlrpc.client.Fault as e:
        return f"ENVIO: fallo el post de la factura de envio: {e.faultString[:200]}"
    if DO_STAMPING:
        try:
            ctx = {'active_model': 'account.move', 'active_ids': [inv_id]}
            wiz = odoo.execute_kw('account.move.send.wizard', 'create',
                                  [{'is_download_only': False}], {'context': ctx})
            odoo.execute_kw('account.move.send.wizard', 'action_send_and_print',
                            [[wiz]], {'context': ctx})
        except Exception as e:
            log.warning(f"{so_name}: fallo el timbrado de la factura de envio: {e}")

    nombre = odoo.execute_kw('account.move', 'read', [[inv_id]], {'fields': ['name']})[0]['name']
    try:
        odoo.execute_kw('account.move', 'message_post', [inv_id], {
            'body': '\n'.join([
                "FACTURA DE ENVIO (AMAZON)",
                f"Amazon order_id: {record['mkp_order_id']}",
                f"SO Odoo: {so_name}",
                f"Complementa a: {', '.join(v['name'] for v in vivas)} "
                f"({ya_facturado:,.2f}), que no incluia el envio.",
                f"Envio cobrado al cliente: {envio['total']:,.2f} "
                f"(base {envio['base']:,.2f} + IVA {envio['iva']:,.2f}; fuente: {envio['fuente']}).",
                f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}",
            ]),
            'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo postear el chatter en {inv_id}: {e}")
    stats['envio_aparte'].append(f"{so_name} {nombre} {envio['total']:,.2f} ({envio['fuente']})")
    log.info(f"{so_name}: factura de envio aparte {nombre} por {envio['total']:,.2f} "
             f"(complementa {', '.join(v['name'] for v in vivas)}; fuente {envio['fuente']}).")
    return f"ENVIO_FACTURADO_APARTE {nombre} {envio['total']:.2f}"


def religar_kits(odoo, inv_id, religar, so_name):
    """Liga las lineas de kit de una factura YA PUBLICADA a su linea de SO.

    Probado en el espejo el 29-oct (INV/2026/150950 / SO10298832): el asiento
    no cambia (el costo ya se calculo al publicar) y la SO pasa a ver la
    factura (invoice_count, qty_invoiced). Si falla, la factura queda
    correcta pero suelta: solo se avisa.
    """
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', inv_id), ('display_type', '=', 'product'),
                               ('sale_line_ids', '=', False)]],
                             {'fields': ['id', 'product_id']})
    for product_id, sale_line_id in religar:
        linea = next((l for l in lineas
                      if l['product_id'] and l['product_id'][0] == product_id), None)
        if not linea:
            continue
        lineas.remove(linea)
        try:
            odoo.execute_kw('account.move.line', 'write',
                            [[linea['id']], {'sale_line_ids': [(4, sale_line_id)]}])
        except Exception as e:
            log.warning(f"{so_name}: no se pudo ligar a la SO la linea de kit "
                        f"{linea['id']}: {e}")


def es_kit(odoo, product_id, catalogos):
    """True si el producto tiene lista de materiales tipo kit (phantom)."""
    cache = catalogos.setdefault('kits', {})
    if product_id not in cache:
        tmpl = odoo.execute_kw('product.product', 'read', [[product_id]],
                               {'fields': ['product_tmpl_id']})[0]['product_tmpl_id'][0]
        cache[product_id] = bool(odoo.execute_kw(
            'mrp.bom', 'search_count',
            [['|', ('product_id', '=', product_id),
              '&', ('product_id', '=', False), ('product_tmpl_id', '=', tmpl),
              ('type', '=', 'phantom')]]))
    return cache[product_id]


def pagado_por_cliente(record):
    """OrderTotal de Amazon guardado por el miner, o None."""
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        return None
    valor = raw.get('amazon', {}).get('order_total_cliente')
    return a_float(valor) if valor else None


def comando_envio(base, catalogos):
    """Linea C-ENVIO de factura. El ingreso lo pone la cuenta del producto
    (401.01.002: Finanzas se la asigno en produccion el 28-oct, segundo)."""
    return (0, 0, {
        'display_type': 'product',
        'sequence': 999,
        'name': 'Costo de envio cobrado por Amazon',
        'product_id': catalogos['producto_envio'],
        'quantity': 1.0,
        'discount': 0.0,
        'price_unit': base,
        'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])],
    })


# IVA que Amazon retiene sobre el envio que acredita: la mitad del IVA. Un
# envio de 59.00 (IVA 8.14) entra al deposito por 59.00 - 4.07 = 54.93.
FACTOR_DEPOSITO_ENVIO = 1 - 0.08 / 1.16


def envio_por_facturar(db_conn, record, ya_facturado, catalogos, tiene_envio):
    """Envio que el cliente pago y que falta facturar.

    Devuelve None (no hay), {'base', 'iva', 'total', 'fuente'} o
    {'error': motivo} cuando hay una diferencia que nadie confirma.

    ya_facturado: lo que ya va en la factura (o la suma de las facturas
    vivas) SIN envio. tiene_envio: ya lleva una linea C-ENVIO.

    Fuentes, en orden (correo de Jero del 28-oct, segundo):
      1. settlement: los campos de envio de las transacciones.
      2. Si no vienen ahi, OrderTotal - ya_facturado es envio SOLO si una
         segunda fuente lo confirma:
         a. deposito: Amazon deposito de mas, neto del IVA que retiene
            (701-4769487-1597824: 54.93 = 59.00 - 4.07);
         b. API de ordenes: ShippingPrice + ShippingTax de getOrderItems.
         Si ninguna confirma, excepcion: no cualquier diferencia se factura
         como envio.
    """
    if tiene_envio or not catalogos.get('producto_envio'):
        return None
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        raw = {}
    imp = raw.get('importes', {})
    cliente = raw.get('amazon', {}).get('order_total_cliente')

    base = round(a_float(imp.get('envio_base')), 2)
    if base > 0.005:
        iva = round(a_float(imp.get('envio_iva')), 2)
        total = round(base + iva, 2)
        # Si ya se facturo lo que pago el cliente, el envio ya va incluido.
        if cliente is not None and abs(a_float(cliente) - ya_facturado) <= 0.05:
            return None
        return {'base': base, 'iva': iva, 'total': total, 'fuente': 'settlement'}

    if cliente is None:
        return None
    # Si las transacciones ya suman lo que pago el cliente, la diferencia no
    # es envio escondido: es la SO la que no cuadra (TOTAL_DIFF, como antes).
    if abs(a_float(imp.get('cfdi_total')) - a_float(cliente)) <= 0.05:
        return None
    dif = round(a_float(cliente) - ya_facturado, 2)
    if dif <= 0.05:
        return None

    def envio_de(total, fuente):
        # El importe es el de la fuente que confirma (59.00), no la resta
        # contra la factura (58.99 si la factura salio un centavo arriba).
        total = round(total, 2)
        base_e = round(total / 1.16, 2)
        return {'base': base_e, 'iva': round(total - base_e, 2), 'total': total,
                'fuente': fuente}

    exc = round(a_float(imp.get('deposito')) - a_float(imp.get('cfdi_total'))
                - a_float(imp.get('fees_total')), 2)
    if exc > 0.005 and abs(round(exc / FACTOR_DEPOSITO_ENVIO, 2) - dif) <= 0.05:
        return envio_de(exc / FACTOR_DEPOSITO_ENVIO, 'deposito')

    try:
        api = 0.0
        for it in get_order_items(sp_api_token(db_conn), record['mkp_order_id']):
            for k in ('ShippingPrice', 'ShippingTax'):
                api += a_float((it.get(k) or {}).get('Amount'))
            api -= a_float((it.get('ShippingDiscount') or {}).get('Amount'))
        if api > 0.005 and abs(round(api, 2) - dif) <= 0.05:
            return envio_de(api, 'api')
    except Exception as e:
        log.warning(f"{record['mkp_order_id']}: no se pudo consultar la API de ordenes: {e}")

    return {'error': (f"El cliente pago {a_float(cliente):,.2f} (OrderTotal) y la SO "
                      f"suma {ya_facturado:,.2f}: {dif:,.2f} de diferencia que ni el "
                      f"settlement, ni el deposito, ni la API confirman como envio.")}


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
    envio = odoo.execute_kw('product.product', 'search',
                            [[('default_code', '=', PRODUCTO_ENVIO)]], {'limit': 1})
    if not envio:
        log.warning(f"No existe el producto {PRODUCTO_ENVIO}: las facturas con "
                    f"envio cobrado y SO sin envio saldran en TOTAL_DIFF.")
    return {'formas_pago': metodos, 'mx_country_id': mx[0] if mx else False,
            'cuenta_merc_no_enviada': get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA),
            'producto_envio': envio[0] if envio else None}


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
        # Genericos: ver B2B_TELEFONO_GENERICO. Solo al crear; un cliente que
        # ya existe conserva sus datos.
        'phone': B2B_TELEFONO_GENERICO,
        'email': B2B_CORREO_GENERICO,
        'comment': ("Creado desde el CFDI de Amazon Business. Telefono y correo "
                    "genericos: el CFDI no trae datos de contacto."),
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


def postear_chatter(odoo, inv_id, record, tipo, uuid, parte=None, inv=None,
                    extra=None):
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
    ]
    # Factura y venta acreditada por separado. Antes solo salian las
    # transacciones rotuladas "Base/IVA/Total", y en una promocion pagada por
    # Amazon (702-6841264-9666630: 1,999.00 vs factura 1,399.28) parecian los
    # importes de la factura.
    if inv:
        lineas.append(f"Factura Odoo: base {a_float(inv.get('amount_untaxed')):,.2f}"
                      f" | IVA {a_float(inv.get('amount_tax')):,.2f}"
                      f" | total {a_float(inv.get('amount_total')):,.2f}")
    cliente = amz.get('order_total_cliente')
    if cliente is not None:
        lineas.append(f"Pagado por el comprador (Amazon OrderTotal): "
                      f"{a_float(cliente):,.2f}")
    lineas += [
        f"Venta acreditada por Amazon (transacciones): "
        f"base {imp.get('cfdi_base', 0):,.2f} | IVA {imp.get('cfdi_iva', 0):,.2f}"
        f" | total {imp.get('cfdi_total', 0):,.2f}",
        f"Comisiones e IVA retenido por Amazon: {imp.get('fees_total', 0):,.2f} "
        f"(desglose en el chatter del cobro)",
    ]
    promo = (parte or {}).get('promo')
    if not promo and cliente is not None \
            and a_float(imp.get('cfdi_total')) - a_float(cliente) > 0.01:
        lineas.append(f"Diferencia venta acreditada - comprador: "
                      f"{a_float(imp.get('cfdi_total')) - a_float(cliente):,.2f}"
                      f" (promocion pagada por Amazon; se registra al aplicar "
                      f"el cobro).")
    lineas += [
        f"Fecha de liberacion: {amz.get('deposit_date') or 'n/d'}",
        f"Red: {amz.get('fulfillment_network') or 'n/d'} | "
        f"Piezas: {amz.get('n_items') or 'n/d'} | "
        f"SKUs: {amz.get('skus') or 'n/d'}",
        f"Ejecucion Kestra: {KESTRA_EXECUTION_ID}",
    ]
    # En una orden repartida, cada factura lleva los datos de SU CFDI, no los
    # agregados de la orden.
    cfdi = (parte or {}).get('cfdi') or raw.get('cfdi_amazon_business') or {}

    # Con promocion de Amazon la venta acreditada (precio completo) NO es la
    # factura: se dice explicitamente.
    if promo:
        lineas += [
            "",
            "PROMOCION FINANCIADA POR AMAZON",
            f"Pagado por el cliente (Amazon OrderTotal): "
            f"{promo['cobrado_cliente']:,.2f}",
            f"Total segun transacciones de Amazon: "
            f"{promo['transacciones']:,.2f}",
            f"Excedente absorbido por Amazon: {promo['excedente']:,.2f}",
            "La factura va por lo que pago el cliente (monto de la SO). La "
            "venta acreditada de arriba son las transacciones, no la factura. "
            "El excedente no se aplica a la CxC: se registra al aplicar el "
            "cobro (403.01.002 + 208.01.001).",
        ]

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

    if extra:
        lineas += [""] + extra

    body = '\n'.join(lineas)
    try:
        odoo.execute_kw('account.move', 'message_post', [inv_id],
                        {'body': body, 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo postear el chatter en {inv_id}: {e}")


# =============================================================================
# PROMOCIONES FINANCIADAS POR AMAZON (B2C)
# =============================================================================
# Amazon a veces absorbe parte del precio: el cliente paga menos y Amazon le
# liquida al vendedor el precio completo. amz_transaction_items NO trae ese
# descuento (no lo paga el vendedor), asi que la suma de transacciones sale
# por el precio completo y no cuadra con la SO, que Yuju crea por lo que pago
# el cliente.
#
# En B2B ya se resolvia solo: la referencia es el CFDI de Amazon, que va por
# lo que pago el cliente. En B2C la referencia es la suma de transacciones y
# estas ordenes caian en TOTAL_DIFF. Casos de la prueba del 26-sep, todos con
# un cupon de $100:
#
#   orden                SO (Odoo)   transacciones   amazon_orders.OrderTotal
#   701-7403714-0375443    515.01       615.00              515.00
#   701-2640601-6960243  2,149.00     2,249.00            2,149.00
#   702-1844782-3257848  7,898.00     7,998.00            7,898.00
#   702-3221480-9299457    899.00       999.00              899.00
#
# Y el de control B2B: 701-2593380-9743412, SO 1,741.13, CFDI de Amazon
# 1,741.13, OrderTotal 1,741.13, transacciones 2,597.69.
#
# REGLA (acordada con Finanzas): facturar por la SO cuando coincide con lo que
# Amazon le cobro al cliente y es MENOR que las transacciones. Las tres
# condiciones, y no solo "la SO es menor":
#
#   1. Hay un OrderTotal inequivoco en amazon_orders (ver
#      total_cobrado_cliente). Es la fuente independiente: sin ella, un error
#      de la SO se facturaria sin que nadie lo vea.
#   2. La SO coincide con ese OrderTotal (+-TOLERANCIA_TOTAL).
#   3. La SO es MENOR que las transacciones. Si fuera mayor no es una
#      promocion de Amazon, es otra cosa, y sigue a TOTAL_DIFF.
#
# El excedente (transacciones - SO) no se factura ni se aplica a la CxC.
# 03_loader_amz_payments.py lo marca como EXCEDENTE_NO_APLICADO cuando llega
# el cobro. Su tratamiento contable y fiscal (cuenta, IVA, CFDI) esta
# pendiente de Finanzas.
# =============================================================================

def evaluar_promo_amazon(record, parte):
    """Detalle de la promocion si la orden cumple la regla; si no, None."""
    try:
        raw = json.loads(record['raw_json']) if record.get('raw_json') else {}
    except (ValueError, TypeError):
        return None
    cobrado = raw.get('amazon', {}).get('order_total_cliente')
    if not cobrado:
        return None

    estimado = parte['estimado']
    transacciones = parte['referencia']
    if abs(estimado - cobrado) > TOLERANCIA_TOTAL:
        return None                    # la SO no es lo que pago el cliente
    if estimado >= transacciones - TOLERANCIA_TOTAL:
        return None                    # no hay nada que Amazon haya absorbido
    return {'cobrado_cliente': cobrado,
            'transacciones': transacciones,
            'excedente': round(transacciones - estimado, 2)}


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

    # --- Estado de la SO: toda venta que Amazon cobro se factura ---
    # Regla de Finanzas (correo de Jero, 28-oct-2026, flujo v2 paso 2). Antes
    # las SO canceladas y en borrador se apartaban como excepcion.
    #
    # SO cancelada: se factura igual. La venta existio y Amazon la cobro; el
    # reembolso lleva su NC por el flujo de devoluciones, aunque venta y
    # reembolso caigan en el mismo settlement. Cada operacion queda con su
    # soporte fiscal. NO se reabre la SO (action_draft + confirmar crearia
    # salidas de almacen de algo que no se va a enviar): la factura se liga a
    # sus lineas tal como estan. En el lote 6 los 4 casos tenian venta y
    # reembolso total en el mismo settlement.
    so_cancelada = so['state'] == 'cancel'
    if so_cancelada:
        stats['canceladas'].append(so_name)
        log.info(f"{so_name}: SO cancelada en Odoo y cobrada por Amazon. Se "
                 f"factura; la NC sale por el flujo de devoluciones.")

    # SO en borrador o cotizacion enviada: se confirma y se factura. En el
    # lote 6 eran 5, todas FBA y sin salida. Confirmar no manda correo al
    # cliente (Odoo solo lo hace con send_email en el contexto) y genera la
    # salida de almacen que corresponde a una venta ya cobrada.
    if so['state'] in ('draft', 'sent'):
        motivo = confirmar_so(odoo, so)
        if motivo:
            update_staging(db_conn, record['id'], 'SO_DRAFT', error_log=motivo,
                           sumar_retry=True)
            stats['borrador'].append(f"{so_name}: {motivo}")
            return
        stats['confirmadas'].append(so_name)

    # --- BLINDAJE 4: re-validacion FAIL-CLOSED ---
    # Se vuelve a preguntar a Odoo justo antes de crear. Si la consulta falla,
    # NO se crea: es la leccion del 26-jul-2026.
    try:
        existentes = facturas_existentes(odoo, so_name, so)
    except Exception as e:
        update_staging(db_conn, record['id'], 'ERROR',
                       error_log=f'No se pudo re-validar duplicidad: {e}',
                       sumar_retry=True)
        return

    # La factura aparte de envio (AUTOINV:<SO>:ENVIO) no es "otra factura de
    # la venta": complementa a la principal. No cuenta para duplicados.
    de_envio = [e for e in existentes if (e.get('ref') or '').endswith(SUFIJO_ENVIO)]
    existentes = [e for e in existentes if e not in de_envio]

    # Cuantas facturas DEBE tener esta orden. Normalmente una; tantas como
    # CFDI emitio Amazon cuando la orden viene repartida.
    desglose = extraer_desglose_b2b(record)
    esperadas = len(desglose) if desglose else 1

    # --- Factura global anterior: la orden ya tiene soporte fiscal ---
    globales = [e for e in existentes if es_global(e)]
    if globales:
        nombres = ', '.join(e['name'] for e in globales)
        update_staging(db_conn, record['id'], 'ALREADY_ODOO_INVOICED',
                       error_log=f'En factura global {nombres}. No se refactura.')
        stats['ya_facturadas'] += 1
        stats['en_global'].append(f"{so_name} ({nombres})")
        return

    # --- B2B que ya tiene una factura nuestra a publico en general ---
    # Amazon emitio el CFDI al cliente y nosotros (el 1 a 1, antes de este
    # proceso) otro a publico general: dos CFDI de la misma venta. Vincular el
    # de Amazon encima seria una tercera factura. Hay que anular primero la de
    # publico general: cancelacion ante el SAT con motivo 02 (confirmado por
    # Finanzas el 28-oct, segundo). Mientras no se cancele, se aparta con estado propio;
    # el miner la reevalua en cada corrida y se vincula en cuanto la factura
    # anterior este cancelada o revertida.
    if es_b2b and existentes:
        uuid_amz = (record.get('cfdi_uuid') or '').upper()
        ajenas = [e for e in existentes
                  if (e.get('l10n_mx_edi_cfdi_uuid') or '').upper() != uuid_amz
                  and not (e.get('ref') or '').startswith(f"{IDEMPOTENCY_PREFIX}{so_name}:")]
        if ajenas:
            nombres = ', '.join(e['name'] for e in ajenas)
            update_staging(
                db_conn, record['id'], 'B2B_FACTURADA_PG',
                error_log=(f"Amazon ya emitio el CFDI B2B y la orden tiene "
                           f"factura viva {nombres}. Cancelarla ante el SAT "
                           f"(motivo 02) antes de vincular el CFDI de Amazon."))
            stats['b2b_facturada_pg'].append(f"{so_name} ({nombres})")
            return

    if len(existentes) >= esperadas:
        netas, neutralizadas = netear_con_nc(odoo, so, existentes)
        if len(netas) > esperadas:
            stats['duplicados'].append(so_name)
            log.error(f"{so_name}: {len(netas)} facturas vivas y se "
                      f"esperaban {esperadas}. INCIDENTE FISCAL.")
        nota = f'Ya tenia {len(netas)} factura(s) viva(s)'
        if neutralizadas:
            nota += (f" (refacturada: {', '.join(f['name'] for f in neutralizadas)} "
                     f"neutralizada con NC)")
        # Factura sin el envio que pago el cliente (las del 1a1): se emite una
        # factura aparte solo por el envio, con la misma SO.
        if not es_b2b and len(netas) == esperadas:
            nota_envio = facturar_envio_aparte(odoo, db_conn, record, so, netas,
                                               de_envio, account_cxc, catalogos, stats)
            if nota_envio:
                nota += f" | {nota_envio}"
        update_staging(db_conn, record['id'], 'ALREADY_ODOO_INVOICED', error_log=nota)
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
            stats['ya_facturadas'] += 1
            return
        partes = [{
            'ref': f'{IDEMPOTENCY_PREFIX}{so_name}',
            'cfdi': cfdi,
            'lineas': lineas,
            'referencia': a_float(record['total_amount']),
            'etiqueta': '',
        }]

    # --- SO cancelada con KITS: la factura tiene que llevar el costo ---
    # Para un kit Odoo calcula el costo con los movimientos de almacen de sus
    # componentes ligados a la linea de la SO. En una SO cancelada estan
    # cancelados: el costo da 0 y la factura sale SIN Dr 501.01.001 / Cr
    # 205.06.003 (7 de 26 en el lote 7). Sin la liga, Odoo toma el costo del
    # producto kit y el asiento sale completo. Despues de publicar se vuelve a
    # ligar a la SO (religar_kits): el asiento no cambia y la SO ve su factura
    # (hallazgo de Sergio, 29-oct: INV/2026/150950 no aparecia en SO10298832).
    #
    # Solo en SO cancelada. Una refactura de kit con su lista de materiales
    # bien SI lleva costo ligada (SO10243073, lote 6), y ligada el costo es el
    # de los componentes que salieron, que es el exacto. Las refacturas sin
    # costo del correo de Jero del 29-oct (TOLIXKIT#4-CLA1ROJ) son otra cosa:
    # su lista de materiales pide 0 sillas y el kit cuesta 0 en Odoo. Eso se
    # avisa despues de publicar (kit_sin_costo) y lo asienta el cierre mensual.
    for parte in partes:
        parte['religar'] = []
        if not so_cancelada:
            continue
        for cmd in parte['lineas']:
            if cmd[2].get('product_id') and cmd[2].get('sale_line_ids') \
                    and es_kit(odoo, cmd[2]['product_id'], catalogos):
                ligas = cmd[2].pop('sale_line_ids')
                parte['religar'] += [(cmd[2]['product_id'], l[1]) for l in ligas]

    # --- Validacion de importes ANTES de crear (patron de Mercado Libre) ---
    # Si lo que Odoo va a facturar no cuadra con la referencia de Amazon, no
    # se emite nada: queda en TOTAL_DIFF para el reporte de Finanzas. Un CFDI
    # equivocado no se corrige con un UPDATE.
    #
    # En una orden repartida se valida el conjunto: si una parte no cuadra, no
    # se factura ninguna.

    # --- Envio cobrado al cliente que la SO no trae (flujo v2, paso 2) ---
    # Yuju no pone la linea de envio en la SO. En el lote 7 las 4 TOTAL_DIFF
    # fueron eso: SO10276040 1,799.00 vs Amazon 1,858.00 = 59.00 de envio.
    # Si Amazon liquido envio y la factura no tiene la linea C-ENVIO, se
    # agrega a la FACTURA (401.01.002) por la base e IVA de las transacciones.
    # La SO no se toca: es de Yuju. Solo B2C de factura unica; en B2B manda
    # el CFDI de Amazon.
    if not es_b2b and len(partes) == 1:
        lineas0 = partes[0]['lineas']
        envio = envio_por_facturar(
            db_conn, record, total_estimado(lineas0), catalogos,
            any(c[2].get('product_id') == catalogos.get('producto_envio') for c in lineas0))
        if envio and envio.get('error'):
            update_staging(db_conn, record['id'], 'ENVIO_SIN_CONFIRMAR',
                           error_log=envio['error'])
            stats['envio_sin_confirmar'].append(f"{so_name}: {envio['error']}")
            log.warning(f"{so_name}: ENVIO_SIN_CONFIRMAR. {envio['error']}")
            return
        if envio:
            lineas0.append(comando_envio(envio['base'], catalogos))
            partes[0]['envio_agregado'] = envio['total']
            # Fuera de los campos del settlement el envio tampoco esta en la
            # referencia (transacciones): se suma para validar el total.
            if envio['fuente'] != 'settlement':
                partes[0]['referencia'] = round(partes[0]['referencia'] + envio['total'], 2)
            log.info(f"{so_name}: la SO no trae el envio que cobro Amazon; se "
                     f"agrega C-ENVIO {envio['total']:,.2f} (fuente: {envio['fuente']}).")

    malas = []
    for parte in partes:
        parte['estimado'] = total_estimado(parte['lineas'])
        dif_previa = parte['estimado'] - parte['referencia']
        if parte['referencia'] > 0 and abs(dif_previa) > TOLERANCIA_TOTAL:
            malas.append((parte, dif_previa))

    # Promocion financiada por Amazon (ver bloque arriba de esta funcion).
    # Solo B2C y solo factura unica: en B2B la referencia ya es el CFDI de
    # Amazon y las ordenes repartidas son siempre B2B.
    if malas and not es_b2b and len(partes) == 1:
        promo = evaluar_promo_amazon(record, partes[0])
        if promo:
            partes[0]['promo'] = promo
            # Desde aqui la referencia es lo que pago el cliente: contra eso
            # se valida el total que deja Odoo despues de crear.
            partes[0]['referencia'] = promo['cobrado_cliente']
            malas = []
            log.info(f"{so_name}: promocion financiada por Amazon. Se factura "
                     f"lo que pago el cliente {promo['cobrado_cliente']:,.2f} "
                     f"(transacciones {promo['transacciones']:,.2f}, "
                     f"excedente {promo['excedente']:,.2f}).")

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
            rfc = (cfdi.get('receiver_rfc') or '').strip().upper()
            if rfc and rfc != 'XAXX010101000':
                motivo = (f"Odoo no permitio crear el cliente {rfc} (ver el log "
                          f"de la corrida).")
            else:
                motivo = (f"CFDI de Amazon sin RFC receptor utilizable "
                          f"(receiver_rfc={cfdi.get('receiver_rfc')!r}).")
            update_staging(
                db_conn, record['id'], 'B2B_NO_RECEPTOR',
                error_log=(f"{motivo} No se factura a publico general para no "
                           f"contradecir el comprobante de Amazon."))
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

    promo = partes[0].get('promo') if len(partes) == 1 else None
    if promo:
        stats['promo_amazon'].append(
            f"{record['mkp_order_id']} | {so_name} | cliente "
            f"{promo['cobrado_cliente']:,.2f} | transacciones "
            f"{promo['transacciones']:,.2f} | excedente "
            f"{promo['excedente']:,.2f}")
    # error_log como bitacora de lo que paso con la orden; el reporte de
    # Finanzas lo lee (SO_CANCELADA alimenta despues el flujo de NC).
    notas = []
    if promo:
        notas.append(f"PROMO_AMAZON cliente {promo['cobrado_cliente']:.2f} "
                     f"transacciones {promo['transacciones']:.2f} "
                     f"excedente {promo['excedente']:.2f}")
    if so_cancelada:
        notas.append("SO_CANCELADA en Odoo: facturada porque Amazon cobro la "
                     "venta; la NC sale por el flujo de devoluciones")
    if so_name in stats['confirmadas']:
        notas.append("SO_CONFIRMADA: estaba en borrador")
    if len(partes) == 1 and partes[0].get('envio_agregado'):
        notas.append(f"ENVIO_AGREGADO C-ENVIO {partes[0]['envio_agregado']:.2f}")
    update_staging(
        db_conn, record['id'],
        'ODOO_B2B_LINKED' if es_b2b else 'ODOO_INVOICED',
        cfdi_uuid=record['cfdi_uuid'],
        error_log=' | '.join(notas) or None,
        total_amount=promo['cobrado_cliente'] if promo else None)


# =============================================================================
# B2B: CUADRE AL CENTAVO CON EL CFDI DE AMAZON
# =============================================================================
# La factura B2B en Odoo REPRESENTA el CFDI que timbro Amazon: lleva su UUID.
# Su total tiene que ser el del comprobante, al centavo. No lo era:
#
#                    base       IVA      total
#   CFDI Amazon      1,206.29   193.00   1,399.29   (702-6841264-9666630)
#   INV/2026/150708  1,206.28   193.00   1,399.28
#
# Odoo arma la factura desde la SO; Amazon parte del precio con IVA y reparte
# base e IVA a su manera. El centavo puede caer en la BASE (este caso) o en
# el IVA (base igual, Odoo redondea base x 16% distinto). Cuando se adjunte
# el XML (D10) diria 1,399.29 y Odoo 1,399.28.
#
# Arreglo en dos pasos, con la factura todavia en borrador:
#   1. Base (cuadrar_base_con_cfdi): se corrige el precio de UNA linea de
#      producto para que la base sea la del CFDI. Mueve el centavo en 401
#      (ventas): el CFDI de Amazon es la fuente de verdad. Va ANTES de poner
#      la CxC de Amazon, porque cambiar un precio recalcula las lineas.
#   2. IVA (cuadrar_con_cfdi): se corrige la LINEA DE IVA al importe del CFDI.
#      Odoo permite editar el impuesto de un borrador (es lo que hace el
#      widget de totales) y al hacerlo rebalancea solo la CxC.
#
# Solo se toca si es redondeo: diferencias de pocos centavos
# (TOLERANCIA_CUADRE_CFDI). Cada paso relee la factura y, si no quedo igual
# al CFDI, deshace su cambio. Si algo no cuadra o Odoo rechaza el cambio, la
# factura se crea igual que antes y queda el aviso en el log: no se bloquea
# la facturacion por esto.
# =============================================================================

TOLERANCIA_CUADRE_CFDI = 0.05


def cuadrar_base_con_cfdi(odoo, inv_id, cfdi, etiqueta):
    """Iguala la base de la factura a la del CFDI (subtotal - descuento)
    corrigiendo el precio de una linea. Devuelve el ajuste (0.0 si no hubo)."""
    if a_float(cfdi.get('total')) <= 0:
        return 0.0
    base_cfdi = round(a_float(cfdi.get('subtotal')) - a_float(cfdi.get('discount')), 2)
    base_odoo = round(a_float(odoo.execute_kw(
        'account.move', 'read', [[inv_id]],
        {'fields': ['amount_untaxed']})[0]['amount_untaxed']), 2)
    dif = round(base_cfdi - base_odoo, 2)
    if dif == 0:
        return 0.0
    if abs(dif) > TOLERANCIA_CUADRE_CFDI:
        log.warning(f"{etiqueta}: base Odoo {base_odoo:,.2f} vs CFDI "
                    f"{base_cfdi:,.2f}. No es redondeo: no se ajusta.")
        return 0.0

    # Linea sin descuento; de preferencia de 1 pieza (el centavo cae exacto
    # en el precio) y la de mayor importe (el producto, no el envio).
    lineas = odoo.execute_kw(
        'account.move.line', 'search_read',
        [[('move_id', '=', inv_id), ('display_type', '=', 'product')]],
        {'fields': ['id', 'quantity', 'price_unit', 'discount',
                    'price_subtotal']})
    candidatas = [l for l in lineas
                  if not l['discount'] and a_float(l['quantity']) > 0]
    if not candidatas:
        log.warning(f"{etiqueta}: sin linea donde ajustar la base. No se ajusta.")
        return 0.0
    linea = max(candidatas, key=lambda l: (a_float(l['quantity']) == 1,
                                           a_float(l['price_subtotal'])))
    precio_previo = linea['price_unit']
    nuevo = round((a_float(linea['price_subtotal']) + dif)
                  / a_float(linea['quantity']), 6)

    def escribir_precio(precio):
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'invoice_line_ids': [(1, linea['id'], {
                            'price_unit': precio})]}],
                        {'context': {'tracking_disable': True}})

    try:
        escribir_precio(nuevo)
    except Exception as e:
        log.warning(f"{etiqueta}: Odoo rechazo el ajuste de la base: {e}")
        return 0.0

    final = round(a_float(odoo.execute_kw(
        'account.move', 'read', [[inv_id]],
        {'fields': ['amount_untaxed']})[0]['amount_untaxed']), 2)
    if final != base_cfdi:
        log.warning(f"{etiqueta}: tras el ajuste la base es {final:,.2f}, el "
                    f"CFDI {base_cfdi:,.2f}. Se deshace el cambio.")
        try:
            escribir_precio(precio_previo)
        except Exception as e:
            log.error(f"{etiqueta}: no se pudo deshacer el precio de la linea "
                      f"{linea['id']} ({precio_previo}). Revisar a mano: {e}")
        return 0.0
    log.info(f"{etiqueta}: base ajustada {dif:+.2f} para igualar el CFDI de "
             f"Amazon ({base_cfdi:,.2f}).")
    return dif


def cuadrar_con_cfdi(odoo, inv_id, cfdi, etiqueta):
    """Iguala el IVA (y por tanto el total) de la factura al CFDI de Amazon.
    Devuelve el ajuste aplicado al IVA (0.0 si no hubo)."""
    total_cfdi = round(a_float(cfdi.get('total')), 2)
    iva_cfdi = round(a_float(cfdi.get('taxes')), 2)
    base_cfdi = round(a_float(cfdi.get('subtotal')) - a_float(cfdi.get('discount')), 2)
    if total_cfdi <= 0 or iva_cfdi <= 0:
        return 0.0

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['amount_untaxed', 'amount_tax',
                                      'amount_total']})[0]
    dif_total = round(total_cfdi - a_float(inv['amount_total']), 2)
    if dif_total == 0:
        return 0.0

    base_odoo = round(a_float(inv['amount_untaxed']), 2)
    dif_iva = round(iva_cfdi - a_float(inv['amount_tax']), 2)
    if abs(dif_total) > TOLERANCIA_CUADRE_CFDI or base_odoo != base_cfdi \
            or dif_iva != dif_total:
        log.warning(f"{etiqueta}: total Odoo {inv['amount_total']:,.2f} vs CFDI "
                    f"{total_cfdi:,.2f} (base Odoo {base_odoo:,.2f} / CFDI "
                    f"{base_cfdi:,.2f}). No es redondeo del IVA: no se ajusta.")
        return 0.0

    lineas_iva = odoo.execute_kw(
        'account.move.line', 'search_read',
        [[('move_id', '=', inv_id), ('display_type', '=', 'tax')]],
        {'fields': ['id', 'balance']})
    if len(lineas_iva) != 1:
        log.warning(f"{etiqueta}: {len(lineas_iva)} lineas de impuesto; se "
                    f"esperaba 1. No se ajusta el centavo.")
        return 0.0

    # En una factura de cliente el IVA es un credito: balance negativo.
    linea = lineas_iva[0]
    nuevo = round(a_float(linea['balance']) - dif_iva, 2)
    try:
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'line_ids': [(1, linea['id'], {
                            'amount_currency': nuevo, 'balance': nuevo})]}],
                        {'context': {'tracking_disable': True}})
    except Exception as e:
        log.warning(f"{etiqueta}: Odoo rechazo el ajuste del IVA: {e}")
        return 0.0

    final = odoo.execute_kw('account.move', 'read', [[inv_id]],
                            {'fields': ['amount_total']})[0]['amount_total']
    if round(a_float(final), 2) != total_cfdi:
        log.warning(f"{etiqueta}: tras el ajuste el total es {final:,.2f}, el "
                    f"CFDI {total_cfdi:,.2f}. Revisar a mano.")
        return 0.0
    log.info(f"{etiqueta}: IVA ajustado {dif_iva:+.2f} para igualar el CFDI "
             f"de Amazon ({total_cfdi:,.2f}).")
    return dif_iva


# =============================================================================
# CONCILIACION DE LA 205.06.003 (MERCANCIAS FACTURADAS - NO ENVIADAS)
# =============================================================================
# Con contabilidad anglosajona la factura acredita el costo en la 205.06.003 y
# la salida de almacen (OUT) lo debita. Odoo concilia las dos partidas solo:
# al publicar la factura si la salida ya existe, y al validar la salida si la
# factura ya existe. Pero solo toma partidas que NO esten conciliadas.
#
# Al refacturar, la salida sigue conciliada con la factura anterior y Odoo no
# la toca: la factura nueva queda abierta. Hallazgo de Jero en el lote 6 (44
# de 48 facturas; ej. SO10307197: VALIN/2026/09/46644 conciliado con
# INV/2026/145489 cancelada, INV/2026/150842 abierta). En pruebas la causa
# fue 06_reset_lote_test.py, que cancelaba con un write directo y no
# desconciliaba la 205.06.003. En produccion pasara cada vez que una factura
# se anule para refacturar (los B2B que hoy estan a publico general).
#
# Lo que pide Finanzas, y lo que hace conciliar_salida() despues de publicar:
#   (i)   rompe la conciliacion de la salida con una factura CANCELADA o
#         revertida por completo con NC;
#   (ii)  si la factura anterior se revirtio con NC, concilia su 205.06.003
#         con la de la NC (la CxC ya la concilia Odoo al revertir);
#   (iii) concilia la factura vigente con la salida, producto por producto.
#
# Una conciliacion con una factura VIVA no se toca nunca.
#
# reconcile y remove_move_reconcile no devuelven nada y por XML-RPC fallan al
# responder despues de haber hecho el trabajo (ver ejecutar_sin_retorno): el
# resultado se confirma releyendo.
#
# Si el costo promedio cambio entre la factura y la salida, los importes no
# son iguales y la conciliacion queda parcial: el estado sale PARCIAL con el
# residuo, para que Finanzas lo vea.
# =============================================================================

def conciliar_salida(odoo, so, inv_id, cuenta):
    """Rehace la conciliacion de la 205.06.003 de una factura publicada.

    Devuelve {'estado', 'detalle'}:
      CONCILIADA   todas las partidas de la factura conciliadas con su salida
      SIN_SALIDA   la orden todavia no sale del almacen (OUT posterior: Odoo
                   concilia al validar la salida)
      PARCIAL      quedo un residuo (salida parcial o costo distinto)
      SIN_LINEAS   la factura no tiene 205.06.003 (solo servicio o envio)
      ERROR        no se pudo; la factura queda como esta
    """
    campos = ['id', 'move_id', 'product_id', 'balance', 'amount_residual',
              'reconciled', 'matched_debit_ids', 'matched_credit_ids']
    try:
        de_factura = odoo.execute_kw(
            'account.move.line', 'search_read',
            [[('move_id', '=', inv_id), ('account_id', '=', cuenta)]],
            {'fields': campos})
        if de_factura and all(l['reconciled'] for l in de_factura):
            return {'estado': 'CONCILIADA', 'detalle': 'conciliada por Odoo al publicar'}

        dominio_salida = [('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
                          ('move_id.stock_move_id.sale_line_id.order_id', '=', so['id']),
                          ('balance', '>', 0)]
        salidas = odoo.execute_kw('account.move.line', 'search_read',
                                  [dominio_salida], {'fields': campos})
        if not salidas:
            if not de_factura:
                return {'estado': 'SIN_LINEAS', 'detalle': ''}
            return {'estado': 'SIN_SALIDA',
                    'detalle': 'sin salida de almacen; Odoo concilia al validarla'}
        # Aunque la factura vigente no traiga costo (kit refacturado antes de
        # la correccion del 29-oct), la salida NO puede quedarse conciliada
        # con la factura cancelada: asi el cierre mensual la ve abierta y le
        # asienta el costo (SO10232549, SO10308861, SO10309861).

        # (i) Conciliaciones de la salida con facturas canceladas o revertidas.
        rotas, revertidas, nombres = [], set(), []
        for l in salidas:
            parciales = l['matched_debit_ids'] + l['matched_credit_ids']
            if not parciales:
                continue
            pr = odoo.execute_kw('account.partial.reconcile', 'read', [parciales],
                                 {'fields': ['debit_move_id', 'credit_move_id']})
            otras = {p[k][0] for p in pr for k in ('debit_move_id', 'credit_move_id')
                     if p[k] and p[k][0] != l['id']}
            movs = odoo.execute_kw('account.move.line', 'read', [list(otras)],
                                   {'fields': ['move_id']})
            facturas = odoo.execute_kw(
                'account.move', 'read', [list({m['move_id'][0] for m in movs})],
                {'fields': ['name', 'state', 'payment_state']})
            viejas = [f for f in facturas
                      if f['state'] == 'cancel' or f['payment_state'] == 'reversed']
            if viejas:
                rotas.append(l['id'])
                revertidas.update(f['id'] for f in viejas if f['state'] != 'cancel')
                nombres += [f['name'] for f in viejas]
        if rotas:
            ejecutar_sin_retorno(odoo, 'account.move.line', 'remove_move_reconcile', rotas)

        # (ii) Factura anterior revertida con NC: su 205.06.003 contra la de la NC.
        for mid in revertidas:
            ncs = odoo.execute_kw('account.move', 'search',
                                  [[('reversed_entry_id', '=', mid), ('state', '=', 'posted')]])
            par = odoo.execute_kw('account.move.line', 'search_read',
                                  [[('move_id', 'in', [mid] + ncs), ('account_id', '=', cuenta),
                                    ('reconciled', '=', False)]], {'fields': ['id', 'balance']})
            if any(p['balance'] > 0 for p in par) and any(p['balance'] < 0 for p in par):
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile',
                                     [p['id'] for p in par])

        if not de_factura:
            return {'estado': 'SIN_LINEAS',
                    'detalle': (f"rota con {', '.join(sorted(set(nombres)))}; la salida "
                                f"queda abierta para el cierre mensual") if nombres else ''}

        # (iii) Factura vigente contra la salida, por producto.
        salidas = odoo.execute_kw('account.move.line', 'search_read',
                                  [dominio_salida + [('reconciled', '=', False)]],
                                  {'fields': campos})
        for l in de_factura:
            if l['reconciled']:
                continue
            prod = l['product_id'][0] if l['product_id'] else False
            suyas = [s['id'] for s in salidas
                     if (s['product_id'][0] if s['product_id'] else False) == prod]
            if suyas:
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile',
                                     [l['id']] + suyas)

        # KITS: la factura lleva el kit y la salida sus componentes (otro
        # producto), asi que el paso por producto no los empareja. Ej.
        # SO10243073: factura 1,737.39 contra dos salidas de 872.21 + 865.18.
        # Si lo que queda abierto de la factura es IGUAL a lo que queda de
        # las salidas de la SO, se concilian juntas. Si no es igual no se
        # fuerza: puede ser de otra factura de la misma orden (reparto B2B).
        restos_fac = odoo.execute_kw('account.move.line', 'search_read',
                                     [[('move_id', '=', inv_id), ('account_id', '=', cuenta),
                                       ('reconciled', '=', False)]],
                                     {'fields': ['id', 'amount_residual']})
        restos_sal = odoo.execute_kw('account.move.line', 'search_read',
                                     [dominio_salida + [('reconciled', '=', False)]],
                                     {'fields': ['id', 'amount_residual']})
        if restos_fac and restos_sal:
            neto = sum(a_float(r['amount_residual']) for r in restos_fac + restos_sal)
            if abs(neto) <= 0.01 * (len(restos_fac) + len(restos_sal)):
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile',
                                     [r['id'] for r in restos_fac + restos_sal])

        final = odoo.execute_kw('account.move.line', 'read',
                                [[l['id'] for l in de_factura]],
                                {'fields': ['reconciled', 'amount_residual']})
        detalle = f"rota con {', '.join(sorted(set(nombres)))}" if nombres else ''
        if all(f['reconciled'] for f in final):
            return {'estado': 'CONCILIADA', 'detalle': detalle}
        residuo = round(sum(a_float(f['amount_residual']) for f in final), 2)
        return {'estado': 'PARCIAL',
                'detalle': f"residuo {residuo:,.2f} {detalle}".strip()}
    except Exception as e:
        return {'estado': 'ERROR', 'detalle': str(e)[:300]}


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

    # --- B2B, paso 1: la base de Odoo tiene que ser la del CFDI ---
    # Antes de la CxC: cambiar un precio recalcula las lineas de la factura.
    ajuste_base = 0.0
    if es_b2b and cfdi:
        ajuste_base = cuadrar_base_con_cfdi(odoo, inv_id, cfdi,
                                            so_name + parte['etiqueta'])

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

    # --- B2B, paso 2: el IVA (y el total) tiene que ser el del CFDI ---
    ajuste_cfdi = 0.0
    if es_b2b and cfdi:
        ajuste_cfdi = cuadrar_con_cfdi(odoo, inv_id, cfdi,
                                       so_name + parte['etiqueta'])

    # --- B2C: el total tiene que ser lo que pago el cliente ---
    # 701-4769487-1597824 salio por 399.01 y el cliente pago 399.00 (correo
    # de Jero del 28-oct, segundo): Odoo redondea el IVA por linea (343.97 x 16% =
    # 55.04) y Amazon cobro 55.03. Mismo ajuste del IVA que en B2B, contra el
    # OrderTotal, solo si es redondeo (TOLERANCIA_CUADRE_CFDI).
    if not es_b2b:
        objetivo = pagado_por_cliente(record)
        if objetivo:
            leida = odoo.execute_kw('account.move', 'read', [[inv_id]],
                                    {'fields': ['amount_untaxed', 'amount_total']})[0]
            dif = round(objetivo - a_float(leida['amount_total']), 2)
            if dif and abs(dif) <= TOLERANCIA_CUADRE_CFDI:
                base = round(a_float(leida['amount_untaxed']), 2)
                ajuste_cfdi = cuadrar_con_cfdi(
                    odoo, inv_id, {'total': objetivo, 'taxes': round(objetivo - base, 2),
                                   'subtotal': base, 'discount': 0},
                    so_name + parte['etiqueta'])
                if ajuste_cfdi:
                    stats['centavo_b2c'] += 1
    if ajuste_cfdi and cxc:
        # Editar el IVA rebalancea la CxC: se confirma que siga en la cuenta
        # de Amazon y no en la del partner.
        cuentas = odoo.execute_kw(
            'account.move.line', 'search_read',
            [[('move_id', '=', inv_id), ('account_type', '=', 'asset_receivable')]],
            {'fields': ['id', 'account_id']})
        fuera = [(1, l['id'], {'account_id': account_cxc}) for l in cuentas
                 if l['account_id'] and l['account_id'][0] != account_cxc]
        if fuera:
            log.warning(f"{so_name}{parte['etiqueta']}: la CxC cambio de cuenta "
                        f"al ajustar el IVA; se regresa a la de Amazon.")
            odoo.execute_kw('account.move', 'write', [[inv_id], {'line_ids': fuera}],
                            {'context': {'tracking_disable': True}})

    # --- POST: genera el asiento contable ---
    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{so_name}: post incierto, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        update_staging(db_conn, record['id'], 'ERROR',
                       error_log=f'Fallo el post: {e}', sumar_retry=True)
        return None

    # --- KITS: de vuelta la liga a la SO, ya con el costo calculado ---
    if parte.get('religar'):
        religar_kits(odoo, inv_id, parte['religar'], so_name)

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
                          {'fields': ['name', 'state', 'amount_untaxed',
                                      'amount_tax', 'amount_total',
                                      'l10n_mx_edi_cfdi_uuid']})
    inv = inv[0] if inv else {}
    uuid_final = inv.get('l10n_mx_edi_cfdi_uuid') or uuid_amazon or None
    if uuid_final is False:
        uuid_final = None

    # --- 205.06.003: la factura vigente queda conciliada con la salida ---
    conc = conciliar_salida(odoo, so, inv_id, catalogos['cuenta_merc_no_enviada'])
    # Factura con kit y SIN costo: lista de materiales en 0 (TOLIXKIT). Se
    # avisa hoy; el cierre mensual le asienta el costo de lo que salio.
    if conc['estado'] == 'SIN_LINEAS':
        kits = [c[2]['product_id'] for c in parte['lineas']
                if c[2].get('product_id') and es_kit(odoo, c[2]['product_id'], catalogos)]
        if kits:
            stats['kit_sin_costo'].append(so_name)
            log.warning(f"{so_name}{parte['etiqueta']}: factura de kit SIN costo "
                        f"(revisar la lista de materiales; costo del kit en 0). "
                        f"El cierre mensual de la 205.06.003 le asienta el costo.")
    # SO cancelada: no va a haber salida. La 205.06.003 de esta factura se
    # concilia con la NC (tipo A), no con un OUT (correo de Jero, 28-oct, segundo).
    if so['state'] == 'cancel' and conc['estado'] == 'SIN_SALIDA':
        conc = {'estado': 'SO_CANCELADA',
                'detalle': 'sin salida; la 205.06.003 se concilia con la NC (tipo A)'}
    stats['conciliacion_205'][conc['estado']] = \
        stats['conciliacion_205'].get(conc['estado'], 0) + 1
    if conc['estado'] in ('PARCIAL', 'ERROR'):
        log.warning(f"{so_name}{parte['etiqueta']}: 205.06.003 {conc['estado']} "
                    f"{conc['detalle']}")

    extra = [f"Mercancias facturadas - no enviadas ({ACCOUNT_MERC_NO_ENVIADA}): "
             f"{conc['estado']}{' - ' + conc['detalle'] if conc['detalle'] else ''}"]
    if parte.get('envio_agregado'):
        extra.append(f"Envio cobrado al cliente que la SO no traia: se agrego "
                     f"C-ENVIO por {parte['envio_agregado']:,.2f} (base e IVA de "
                     f"las transacciones de Amazon).")
        stats['envio_agregado'].append(so_name)
    if so['state'] == 'cancel':
        extra.append("SO CANCELADA en Odoo: se factura porque Amazon cobro la "
                     "venta. La nota de credito sale por el flujo de "
                     "devoluciones.")
    if so_name in stats['confirmadas']:
        extra.append("La SO estaba en borrador y se confirmo para facturarla "
                     "(Amazon ya cobro la venta).")
    postear_chatter(odoo, inv_id, record, tipo, uuid_final, parte, inv, extra)

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
        'ajuste_base': ajuste_base,
        'ajuste_cfdi': ajuste_cfdi,
        'conciliacion_205': conc['estado'],
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
             'total_diff': [], 'split_fallido': [], 'promo_amazon': [],
             'confirmadas': [], 'en_global': [], 'b2b_facturada_pg': [],
             'envio_agregado': [], 'envio_sin_confirmar': [], 'envio_aparte': [],
             'kit_sin_costo': [],
             'centavo_b2c': 0,
             'conciliacion_205': {},
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
        # Las facturas de envio aparte tambien cuentan: son CFDI timbrados.
        if (PROD_MAX_FACTURAS is not None
                and hechas['B2B'] + hechas['B2C_PUBLICO_GENERAL']
                + len(stats['envio_aparte']) >= PROD_MAX_FACTURAS):
            log.info(f"[LOADER] Tope de arranque alcanzado: "
                     f"{PROD_MAX_FACTURAS} facturas (AMZ_PROD_MAX_FACTURAS). "
                     f"Lo pendiente queda para la siguiente corrida.")
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
    log.info(f"     dentro de una factura global : {len(stats['en_global'])}")
    log.info(f"  SO canceladas (facturadas, NC)  : {len(stats['canceladas'])}")
    log.info(f"  SO en borrador confirmadas      : {len(stats['confirmadas'])}")
    log.info(f"  SO en borrador sin confirmar    : {len(stats['borrador'])}")
    log.info(f"  B2B con factura a publico gral. : {len(stats['b2b_facturada_pg'])}")
    log.info(f"  Con envio agregado (C-ENVIO)    : {len(stats['envio_agregado'])}")
    log.info(f"  Factura de envio aparte         : {len(stats['envio_aparte'])}")
    log.info(f"  Envio sin confirmar (excepcion) : {len(stats['envio_sin_confirmar'])}")
    log.info(f"  B2C con centavo ajustado        : {stats['centavo_b2c']}")
    log.info(f"  205.06.003 de las facturas      : "
             f"{', '.join(f'{k} {v}' for k, v in sorted(stats['conciliacion_205'].items())) or '-'}")
    log.info(f"  Sin SO en Odoo                  : {len(stats['sin_so'])}")
    log.info(f"  B2B sin receptor en el CFDI     : {len(stats['b2b_sin_receptor'])}")
    log.info(f"  B2C con promocion de Amazon     : {len(stats['promo_amazon'])}")
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

    if stats['promo_amazon']:
        log.info('')
        log.info(f"PROMOCION FINANCIADA POR AMAZON -- {len(stats['promo_amazon'])} "
                 f"B2C facturada(s) por lo que pago el cliente:")
        for p in stats['promo_amazon'][:20]:
            log.info(f"  {p}")
        log.info('  El excedente no se factura ni se aplica a la CxC; 03 lo '
                 'marca al llegar el cobro. Tratamiento contable pendiente.')

    for clave, titulo in (('b2b_facturada_pg', 'B2B CON FACTURA A PUBLICO GENERAL '
                                               '(anular antes de vincular el CFDI de Amazon)'),
                          ('borrador', 'SO EN BORRADOR QUE NO SE PUDIERON CONFIRMAR'),
                          ('envio_aparte', 'FACTURAS DE ENVIO APARTE'),
                          ('kit_sin_costo', 'FACTURAS DE KIT SIN COSTO (lista de materiales)'),
                          ('envio_sin_confirmar', 'ENVIO SIN CONFIRMAR (no se facturo)'),
                          ('en_global', 'ORDENES EN FACTURA GLOBAL (no se refacturan)')):
        if stats[clave]:
            log.info('')
            log.info(f"{titulo} -- {len(stats[clave])}:")
            for d in stats[clave][:20]:
                log.info(f"  {d}")

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
            f"CANCELADAS en Odoo* que Amazon si liquido. Se facturaron; "
            f"llevan NC por el flujo de devoluciones.\n"
            f"{', '.join(stats['canceladas'][:15])}")

    if stats['b2b_facturada_pg']:
        notify_slack(
            f":warning: *{len(stats['b2b_facturada_pg'])} orden(es) B2B de "
            f"Amazon con factura a publico general*. No se vinculo el CFDI de "
            f"Amazon: primero hay que anular la factura anterior.\n"
            f"{', '.join(stats['b2b_facturada_pg'][:15])}")

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
    if QUOTA_B2B is not None:
        _cuota = f"B2B={QUOTA_B2B} B2C={QUOTA_B2C} (facturas creadas)"
    elif PROD_MAX_FACTURAS:
        _cuota = f"sin limite por tipo; tope de arranque {PROD_MAX_FACTURAS} facturas"
    else:
        _cuota = 'sin limite'
    if CON_ALCANCE:
        _ventana = (f"ALCANCE settlement {AMZ_SETTLEMENT_ID or '-'} + "
                    f"{len(AMZ_ORDER_IDS)} orden(es) sueltas")
    else:
        _ventana = f"Ventana: {AMZ_START_DATE} .. {AMZ_END_DATE or 'hoy'}"
    log.info(f"Timbrado: {'ACTIVADO' if DO_STAMPING else 'DESACTIVADO'} | "
             f"Cuota: {_cuota} | Gracia: {GRACE_DAYS} dias | {_ventana}")
    t0 = tm.time()
    try:
        main()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico en facturacion Amazon: {e}')
        notify_slack(f":x: Fallo critico en facturacion Amazon "
                     f"(`{KESTRA_EXECUTION_ID}`): {e}")
        sys.exit(1)
