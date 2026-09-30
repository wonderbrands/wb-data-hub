-- =============================================================================
-- AMAZON - Reporte BI 3.0 (Repositorio de Pagos)  ::  VERSION CORREGIDA
-- =============================================================================
-- Basado en AMZ_facturacion.sql (query original).
-- Corrige tres defectos detectados al validar contra Reporte_3_0_BI.csv
-- (muestra de 100 filas, posted_date 2026-07-30).
--
-- ---------------------------------------------------------------------------
-- FIX 1 -- FAN-OUT POR amazon_orders   [CRITICO]
--   El JOIN a somos_reyes.amazon_orders entraba SIN deduplicar, a diferencia
--   de los otros tres subqueries. amazon_orders tiene N filas por orden, asi
--   que TODOS los SUM() se multiplicaban por N.
--   Efecto medido: 49 de 94 ventas de la muestra traian importes al doble
--   (product_charges, our_price_tax, quantity_shipped, comisiones...).
--   Ejemplo: orden 701-8131997-1237840, SKU RUMG30
--       reporte  -> product_charges 2782.82 / quantity_shipped 2
--       real     -> product_charges 1391.41 / quantity_shipped 1
--       (total_amount 1087.38 de la cabecera confirma el valor real,
--        porque no pasa por SUM)
--   Solucion: subquery con GROUP BY, igual que los joins c / e / f.
--
-- ---------------------------------------------------------------------------
-- FIX 2 -- DOBLE CONTEO DE DESCUENTOS
--   promo_rebates es el ROLL-UP y shipping_discount el DETALLE del mismo
--   descuento: en las 20 filas con promocion ambos traen identico importe.
--   Sumar los dos descuadra la factura. Lo mismo con our_price_tax_disc /
--   shipping_tax_discount.
--   Se conservan ambas columnas (Finanzas las usa) pero se agregan campos
--   calculados cfdi_* que aplican la formula correcta, para que nadie tenga
--   que decidir cual sumar.
--
-- ---------------------------------------------------------------------------
-- FIX 3 -- DUPLICIDAD POR transaction_status
--   La PK de amz_transactions incluye transaction_status, por lo que la misma
--   transaccion coexiste como DEFERRED y luego como RELEASED /
--   DEFERRED_RELEASED. La query original no filtraba por status y presentaba
--   la venta dos veces.
--   Muestra: 50 RELEASED / 34 DEFERRED / 16 DEFERRED_RELEASED.
--   Solucion: parametro p_solo_liberadas para excluir las DEFERRED puras.
--   El proceso de facturacion y el de cobros NO deben leer DEFERRED.
--
-- ---------------------------------------------------------------------------
-- OTROS CAMBIOS
--   * Rango de fechas parametrizado (antes hardcodeado 2026-01-01/2026-04-30).
--   * Se elimina el "DESC LIMIT 10" residual: el '#' comentaba el ';' y el
--     LIMIT si se estaba ejecutando.
--   * amz_b2b_invoices: se separa el CFDI (receipt_type='Invoice') del resto
--     de comprobantes. La PK incluye receipt_type/payment_type/uuid, asi que
--     una orden puede traer factura + complemento de pago y el GROUP_CONCAT
--     original los mezclaba (visto en 702-2265086-8559438: "1379.00 | 1379.00").
--
-- ---------------------------------------------------------------------------
-- FORMULA VALIDADA CONTRA LA MUESTRA (cuadra al centavo):
--     cfdi_base  = product_charges + shipping + shipping_discount
--                                  + our_price_discount
--     cfdi_iva   = our_price_tax + shipping_tax + shipping_tax_discount
--                                + our_price_tax_disc
--     cfdi_total = cfdi_base + cfdi_iva
--     neto       = cfdi_total + amazon_fees + marketplace_vat_principal
--                            + marketplace_vat_shipping + amazon_for_all_fee
--                            + shipping_hb
--                = total_amount   (cabecera de amz_transactions)
--
--   Verificacion orden 702-6941044-9293042 (envio gratis promocionado):
--     base = 1555.21 + 50.86 - 50.86              = 1555.21
--     iva  =  248.83 +  8.14 -  8.14              =  248.83   (16% exacto)
--     neto = 1804.04 - 444.45 - 124.42            = 1235.17 = total_amount
-- =============================================================================

SET @p_fecha_ini      = '2026-01-01';   -- inclusivo, hora CDMX
SET @p_fecha_fin      = '2026-04-30';   -- inclusivo, hora CDMX
SET @p_solo_liberadas = 1;              -- 1 = excluye DEFERRED puras (FIX 3)

SELECT
    a.settlement_id,
    a.transaction_id,
    a.order_id,
    c.so,
    a.shipment_id,
    IF(d.IsBusinessOrder = 1, 'True', 'False')            AS B2B,
    e.settlement_start_date,
    e.settlement_end_date,
    e.deposit_date,
    DATE_SUB(a.posted_date, INTERVAL 6 HOUR)              AS posted_date,
    a.transaction_type,
    a.transaction_status,
    a.description,

    JSON_UNQUOTE(JSON_EXTRACT(b.item_related_identifiers,
        '$[0].itemRelatedIdentifierName'))                AS item_identifier_name,
    JSON_UNQUOTE(JSON_EXTRACT(b.item_related_identifiers,
        '$[0].itemRelatedIdentifierValue'))               AS item_identifier_value,

    b.fulfillment_network,
    b.asin,
    b.sku,
    GROUP_CONCAT(DISTINCT b.items_description
        ORDER BY b.items_description SEPARATOR ' , ')     AS items_description,

    -- OJO: total_amount vive en la cabecera (amz_transactions). Se repite
    -- identico en cada fila de item de la misma transaccion. NO sumarlo
    -- entre filas: se contaria de mas en transacciones multi-SKU.
    a.total_amount,

    SUM(IFNULL(b.item_amount, 0))                         AS item_amount,
    b.item_currency_code,
    b.item_related_identifiers,
    SUM(IFNULL(b.quantity_shipped, 0))                    AS quantity_shipped,

    -- ---------------------------------------------------------------------
    -- CAMPOS CALCULADOS PARA FACTURACION  (FIX 2)
    -- Es lo unico que deben leer el script de CFDI y el de notas de credito.
    -- ---------------------------------------------------------------------
    IFNULL(SUM(b.product_charges), 0)
        + IFNULL(SUM(b.shipping), 0)
        + IFNULL(SUM(b.shipping_discount), 0)
        + IFNULL(SUM(b.our_price_discount), 0)            AS cfdi_base,

    IFNULL(SUM(b.our_price_tax), 0)
        + IFNULL(SUM(b.shipping_tax), 0)
        + IFNULL(SUM(b.shipping_tax_discount), 0)
        + IFNULL(SUM(b.our_price_tax_disc), 0)            AS cfdi_iva,

    IFNULL(SUM(b.product_charges), 0)
        + IFNULL(SUM(b.shipping), 0)
        + IFNULL(SUM(b.shipping_discount), 0)
        + IFNULL(SUM(b.our_price_discount), 0)
        + IFNULL(SUM(b.our_price_tax), 0)
        + IFNULL(SUM(b.shipping_tax), 0)
        + IFNULL(SUM(b.shipping_tax_discount), 0)
        + IFNULL(SUM(b.our_price_tax_disc), 0)            AS cfdi_total,

    -- Linea 401-001-002 "Ventas por Envio" del asiento contable.
    -- Neta de promocion: si Amazon regalo el flete, queda en 0.
    IFNULL(SUM(b.shipping), 0)
        + IFNULL(SUM(b.shipping_discount), 0)             AS cfdi_envio_base,

    -- Total de comisiones/fees. Con signo negativo, tal como llega de Amazon.
    -- Es el descuadre entre lo facturado y lo depositado; alimenta la
    -- conciliacion de 102.02.008 Liberaciones Amazon Pendientes de Procesar.
    IFNULL(SUM(b.amazon_fees), 0)
        + IFNULL(SUM(b.marketplace_vat_principal), 0)
        + IFNULL(SUM(b.marketplace_vat_shipping), 0)
        + IFNULL(SUM(b.amazon_for_all_fee), 0)
        + IFNULL(SUM(b.shipping_hb), 0)                   AS cfdi_fees_total,

    -- ---------------------------------------------------------------------
    -- COLUMNAS ORIGINALES (se conservan sin cambio para Finanzas)
    -- ---------------------------------------------------------------------
    IFNULL(SUM(b.promo_rebates), 0)             AS promo_rebates,
    IFNULL(SUM(b.our_price_discount), 0)        AS our_price_discount,
    IFNULL(SUM(b.tax), 0)                       AS tax,
    IFNULL(SUM(b.our_price_tax), 0)             AS our_price_tax,
    IFNULL(SUM(b.our_price_tax_disc), 0)        AS our_price_tax_disc,
    IFNULL(SUM(b.product_charges), 0)           AS product_charges,
    IFNULL(SUM(b.our_price_principal), 0)       AS our_price_principal,
    IFNULL(SUM(b.amazon_fees), 0)               AS amazon_fees,
    IFNULL(SUM(b.commission), 0)                AS commission,
    IFNULL(SUM(b.commission_base), 0)           AS commission_base,
    IFNULL(SUM(b.commission_tax), 0)            AS commission_tax,
    IFNULL(SUM(b.refund_commission), 0)         AS refund_commission,
    IFNULL(SUM(b.shipping), 0)                  AS shipping,
    IFNULL(SUM(b.shipping_principal), 0)        AS shipping_principal,
    IFNULL(SUM(b.shipping_discount), 0)         AS shipping_discount,
    IFNULL(SUM(b.shipping_tax), 0)              AS shipping_tax,
    IFNULL(SUM(b.shipping_tax_discount), 0)     AS shipping_tax_discount,
    IFNULL(SUM(b.fba_fee), 0)                   AS fba_fee,
    IFNULL(SUM(b.fba_fee_base), 0)              AS fba_fee_base,
    IFNULL(SUM(b.fba_fee_tax), 0)               AS fba_fee_tax,
    IFNULL(SUM(b.refund_commission_base), 0)    AS refund_commission_base,
    IFNULL(SUM(b.refund_commission_tax), 0)     AS refund_commission_tax,
    IFNULL(SUM(b.marketplace_vat_principal), 0) AS marketplace_vat_principal,
    IFNULL(SUM(b.marketplace_vat_shipping), 0)  AS marketplace_vat_shipping,
    IFNULL(SUM(b.shipping_hb), 0)               AS shipping_hb,
    IFNULL(SUM(b.shipping_hb_base), 0)          AS shipping_hb_base,
    IFNULL(SUM(b.shipping_hb_tax), 0)           AS shipping_hb_tax,
    IFNULL(SUM(b.amazon_for_all_fee), 0)        AS amazon_for_all_fee,
    IFNULL(SUM(b.amazon_for_all_fee_base), 0)   AS amazon_for_all_fee_base,
    IFNULL(SUM(b.amazon_for_all_fee_tax), 0)    AS amazon_for_all_fee_tax,

    -- ---------------------------------------------------------------------
    -- CFDI EMITIDO POR AMAZON BUSINESS  (FIX: solo receipt_type='Invoice')
    -- ---------------------------------------------------------------------
    f.amazon_business_invoice_date,
    f.amazon_business_invoice_total,
    f.amazon_business_invoice_id,
    IF(f.order_id IS NOT NULL, 1, 0)            AS ya_facturada_por_amazon

FROM somos_reyes.amz_transactions a

LEFT JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id

-- SO de Odoo
LEFT JOIN (
    SELECT marketplace_order_id, MAX(order_id) AS so
    FROM somos_reyes.odoo_master_txns_c
    GROUP BY marketplace_order_id
) c
    ON a.order_id = c.marketplace_order_id

-- FIX 1: deduplicado. Solo se necesita el flag B2B de esta tabla.
LEFT JOIN (
    SELECT amazonorderid, MAX(IsBusinessOrder) AS IsBusinessOrder
    FROM somos_reyes.amazon_orders
    GROUP BY amazonorderid
) d
    ON a.order_id = d.amazonorderid

-- Fechas del settlement. deposit_date es el trigger de aplicacion de cobros.
LEFT JOIN (
    SELECT settlement_id,
           MAX(settlement_start_date) AS settlement_start_date,
           MAX(settlement_end_date)   AS settlement_end_date,
           MAX(deposit_date)          AS deposit_date
    FROM somos_reyes.amz_settlement_report
    GROUP BY settlement_id
) e
    ON a.settlement_id = e.settlement_id

-- CFDI de Amazon Business, solo facturas vigentes.
LEFT JOIN (
    SELECT
        order_id,
        GROUP_CONCAT(date  ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_date,
        GROUP_CONCAT(total ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_total,
        GROUP_CONCAT(uuid  ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_id
    FROM somos_reyes.amz_b2b_invoices
    WHERE receipt_type = 'Invoice'
      AND (status IS NULL OR status <> 'Cancelado')
      AND (cancellation_date IS NULL OR cancellation_date = '')
    GROUP BY order_id
) f
    ON a.order_id = f.order_id

WHERE DATE(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) >= @p_fecha_ini
  AND DATE(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) <= @p_fecha_fin
  -- FIX 3: las DEFERRED reaparecen luego como RELEASED / DEFERRED_RELEASED.
  AND (@p_solo_liberadas = 0 OR a.transaction_status <> 'DEFERRED')

GROUP BY
    a.settlement_id,
    a.transaction_id,
    a.order_id,
    -- Granularidad dual (se respeta la del original):
    --   Order Payment -> una fila por ITEM  (habilita NC parciales por SKU)
    --   Refund / Adjustment -> una fila por TRANSACCION (sin desglose SKU)
    CASE
        WHEN a.description = 'Order Payment'
        THEN b.transaction_item_id
        ELSE NULL
    END

ORDER BY posted_date DESC;



# ======================================================================
# somos_reyes-amz_b2b_invoices--701-0599283-6185052

SELECT * FROM somos_reyes.amz_transactions LIMIT 1;

SELECT * FROM somos_reyes.amz_b2b_invoices
WHERE order_id in ('701-2593380-9743412');#LIMIT 1;

SELECT * FROM somos_reyes.amz_settlement_report order by updated_at DESC LIMIT 10;

# somos_reyes-amz_transactions--701-0599283-6185052

# ODOO_PAID = 97; sum = $204,585.63
SELECT status, COUNT(*), SUM(amount)
FROM finance.mkp_payments_staging WHERE marketplace='AMAZON' GROUP BY status;

SELECT * FROM somos_reyes.amz_transactions
WHERE order_id in ('701-2593380-9743412'); 

# somos_reyes-amz_transaction_items--701-0599283-6185052
SELECT * FROM somos_reyes.amz_transaction_items
WHERE order_id in ('701-2593380-9743412');   

SELECT count(*) as TOTAL, marketplace FROM finance.mkp_billing_staging group by marketplace;

SELECT * FROM finance.mkp_billing_staging WHERE marketplace='Amazon';

SELECT s.mkp_order_id, s.odoo_so_name, s.created_at, f.uuid
FROM finance.mkp_billing_staging s
JOIN somos_reyes.amz_b2b_invoices f ON f.order_id=s.mkp_order_id
AND f.receipt_type='I'
AND (f.status IS NULL OR f.status<>'CANCEL')
WHERE s.marketplace='AMAZON' AND s.status='ODOO_INVOICED';

SELECT COUNT(*) b2b_en_ventana, SUM(s.id IS NOT NULL) ya_en_control
FROM (SELECT DISTINCT f.order_id FROM somos_reyes.amz_b2b_invoices f
JOIN somos_reyes.amz_transactions t ON t.order_id=f.order_id
WHERE f.receipt_type='I'
AND (f.status IS NULL OR f.status<>'CANCEL') AND t.description='Order Payment' AND t.transaction_status<>'DEFERRED'
AND DATE(DATE_SUB(t.posted_date, INTERVAL 6 HOUR)) BETWEEN '2026-07-20' AND '2026-08-26') o
LEFT JOIN finance.mkp_billing_staging s ON s.marketplace='AMAZON' AND s.mkp_order_id=o.order_id;



# ===================================================================
SELECT marketplace, mkp_order_id, odoo_so_name, total_amount, status, error_log, created_at, processed_at
FROM finance.mkp_billing_staging WHERE created_at > '2026-09-18' order by id DESC LIMIT 500;

UPDATE finance.mkp_billing_staging SET status='PENDING_WB_INVOICE', cfdi_uuid=NULL, error_log=NULL, retry_count_loader=0, raw_json=NULL WHERE marketplace='AMAZON';

# ========================== PAGOS ==========================

SELECT * FROM finance.mkp_payments_staging 
order by created_at DESC LIMIT 2;

