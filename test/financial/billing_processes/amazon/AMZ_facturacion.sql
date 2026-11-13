# =============== AMAZON ====================
# query reporte de Yanina para fianzas, llamado BI 3.0 (Reositorio de pagos):

SELECT 
		a.settlement_id,
		a.transaction_id,
		a.order_id,
		so,
		shipment_id,
        IF(IsBusinessOrder = 1, 'True', 'False') 'B2B',
        settlement_start_date, 
        settlement_end_date, 
        deposit_date,
		DATE_SUB(posted_date, INTERVAL 6 HOUR) AS posted_date,
		transaction_type,
		transaction_status,
		description,
		JSON_UNQUOTE(JSON_EXTRACT(item_related_identifiers, '$[0].itemRelatedIdentifierName')) AS item_identifier_name,
		JSON_UNQUOTE(JSON_EXTRACT(item_related_identifiers, '$[0].itemRelatedIdentifierValue')) AS item_identifier_value,
		fulfillment_network,
		asin,
		sku,
		GROUP_CONCAT(DISTINCT items_description ORDER BY items_description SEPARATOR ' , ' ) AS items_description,
		total_amount,
		SUM(IFNULL(item_amount,0)) AS item_amount,
		item_currency_code,
		item_related_identifiers,
		SUM(IFNULL(quantity_shipped,0)) AS quantity_shipped,
		IFNULL(SUM(promo_rebates),0) AS promo_rebates,
		IFNULL(SUM(our_price_discount),0) AS our_price_discount,
		IFNULL(SUM(tax),0) AS tax,
		IFNULL(SUM(our_price_tax),0) AS our_price_tax,
		IFNULL(SUM(our_price_tax_disc),0) AS our_price_tax_disc,
		IFNULL(SUM(product_charges),0) AS product_charges,
		IFNULL(SUM(our_price_principal),0) AS our_price_principal,
		IFNULL(SUM(amazon_fees),0) AS amazon_fees,
		IFNULL(SUM(commission),0) AS commission,
		IFNULL(SUM(commission_base),0) AS commission_base,
		IFNULL(SUM(commission_tax),0) AS commission_tax,
		IFNULL(SUM(refund_commission),0) AS refund_commission,
		IFNULL(SUM(shipping),0) AS shipping,
		IFNULL(SUM(shipping_principal),0) AS shipping_principal,
		IFNULL(SUM(shipping_discount),0) AS shipping_discount,
		IFNULL(SUM(shipping_tax),0) AS shipping_tax,
		IFNULL(SUM(shipping_tax_discount),0) AS shipping_tax_discount,
		IFNULL(SUM(fba_fee),0) AS fba_fee,
		IFNULL(SUM(fba_fee_base),0) AS fba_fee_base,
		IFNULL(SUM(fba_fee_tax),0) AS fba_fee_tax,
		IFNULL(SUM(refund_commission_base),0) AS refund_commission_base,
		IFNULL(SUM(refund_commission_tax),0) AS refund_commission_tax,
		IFNULL(SUM(marketplace_vat_principal),0) AS marketplace_vat_principal,
		IFNULL(SUM(marketplace_vat_shipping),0) AS marketplace_vat_shipping,
		IFNULL(SUM(shipping_hb),0) AS shipping_hb,
		IFNULL(SUM(shipping_hb_base),0) AS shipping_hb_base,
		IFNULL(SUM(shipping_hb_tax),0) AS shipping_hb_tax,
		IFNULL(SUM(amazon_for_all_fee),0) AS amazon_for_all_fee,
		IFNULL(SUM(amazon_for_all_fee_base),0) AS amazon_for_all_fee_base,
		IFNULL(SUM(amazon_for_all_fee_tax),0) AS amazon_for_all_fee_tax,
		amazon_business_invoice_date, 
        amazon_business_invoice_total, 
        amazon_business_invoice_id
	FROM somos_reyes.amz_transactions a
	LEFT JOIN somos_reyes.amz_transaction_items b 
		ON a.transaction_id = b.transaction_id
	LEFT JOIN (
		SELECT marketplace_order_id, order_id so
		FROM somos_reyes.odoo_master_txns_c
		GROUP BY marketplace_order_id
	) c 
		ON a.order_id = c.marketplace_order_id
    LEFT JOIN somos_reyes.amazon_orders d
    ON a.order_id = d.amazonorderid
	LEFT JOIN (
	SELECT 
		settlement_id, 
        MAX(settlement_start_date) settlement_start_date, 
        MAX(settlement_end_date) settlement_end_date, 
        MAX(deposit_date) deposit_date
    FROM somos_reyes.amz_settlement_report
    GROUP BY settlement_id
	) e
	ON a.settlement_id = e.settlement_id
    LEFT JOIN (
		SELECT 
		order_id,
		GROUP_CONCAT(date ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_date,
		GROUP_CONCAT(total ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_total,
		GROUP_CONCAT(uuid ORDER BY date ASC, uuid ASC SEPARATOR ' | ') AS amazon_business_invoice_id
		FROM somos_reyes.amz_b2b_invoices
		GROUP BY order_id ) f
    ON a.order_id = f.order_id
	WHERE DATE(DATE_SUB(posted_date, INTERVAL 6 HOUR)) >= '2026-01-01'
	  AND DATE(DATE_SUB(posted_date, INTERVAL 6 HOUR)) <= '2026-04-30'
    GROUP BY
        settlement_id,
        a.transaction_id,
        a.order_id,
        CASE
            WHEN description = 'Order Payment'
            THEN transaction_item_id
            ELSE NULL
        END
    ORDER BY posted_date  # ;
    DESC LIMIT 10;
