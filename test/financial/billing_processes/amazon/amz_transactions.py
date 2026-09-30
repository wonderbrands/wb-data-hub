import requests
import MySQLdb
import sys
sys.path.append("/var/lib/jenkins/m1/")
import set666 as creds
import json
from collections import defaultdict
from datetime import datetime, timedelta
import calendar

MARKETPLACE_ID = "A1AM78C64UM0Y8"

# --- Calcular dinámicamente: mes anterior y mes actual ---
today = datetime.now()
current_month = (today.year, today.month)

first_day_current = today.replace(day=1)
last_month_date = first_day_current - timedelta(days=1)
previous_month = (last_month_date.year, last_month_date.month)

months_to_process = [previous_month, current_month]

# Margen de seguridad para no pedirle a Amazon una fecha futura o "casi ahora"
now_utc = datetime.utcnow()
safety_margin = now_utc - timedelta(minutes=5)

db = MySQLdb.connect(creds.wbh, creds.wbu, creds.wbp, 'somos_reyes', local_infile=True)
cursor = db.cursor()

db.set_character_set("utf8mb4")
cursor.execute("SET NAMES utf8mb4;")
cursor.execute("SET CHARACTER SET utf8mb4;")
cursor.execute("SET character_set_connection=utf8mb4;")

cursor.execute("SELECT access_token FROM amz_tokens")
access_token = cursor.fetchone()[0]

finances_url = "https://sellingpartnerapi-na.amazon.com/finances/2024-06-19/transactions"
headers = {
    "x-amz-access-token": access_token,
    "Content-Type": "application/json"
}

def get_breakdown(breakdowns, breakdown_type):
    for b in breakdowns or []:
        if b.get('breakdownType') == breakdown_type:
            return b.get('breakdownAmount', {}).get('currencyAmount')
    return None

def get_nested_breakdown(breakdowns, parent_type, child_type):
    for b in breakdowns or []:
        if b.get('breakdownType') == parent_type:
            return get_breakdown(b.get('breakdowns') or [], child_type)
        result = get_nested_breakdown(b.get('breakdowns') or [], parent_type, child_type)
        if result is not None:
            return result
    return None

insert_txn = """INSERT INTO somos_reyes.amz_transactions ( selling_partner_metadata, transaction_type, transaction_id, transaction_status, related_identifiers, total_amount, currency_code, description, posted_date, marketplace_details, contexts, order_id, settlement_id, shipment_id, refunded_expenses, refunded_sales, items ) 
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE selling_partner_metadata = VALUES(selling_partner_metadata), transaction_type         = VALUES(transaction_type), transaction_status       = VALUES(transaction_status), related_identifiers      = VALUES(related_identifiers), total_amount             = VALUES(total_amount), currency_code            = VALUES(currency_code), description              = VALUES(description), posted_date              = VALUES(posted_date), marketplace_details      = VALUES(marketplace_details), contexts                 = VALUES(contexts), order_id                 = VALUES(order_id), settlement_id            = VALUES(settlement_id), shipment_id              = VALUES(shipment_id), refunded_expenses        = VALUES(refunded_expenses), refunded_sales           = VALUES(refunded_sales), items                    = VALUES(items)"""

insert_item = """INSERT INTO somos_reyes.amz_transaction_items ( transaction_id, order_id, transaction_item_id, order_adjustment_item_id, items_description, item_amount, item_currency_code, item_related_identifiers, asin, sku, quantity_shipped, fulfillment_network, promo_rebates, our_price_discount, shipping_discount, product_charges, our_price_principal, shipping, shipping_principal, tax, our_price_tax, our_price_tax_disc, shipping_tax, shipping_tax_discount, amazon_fees, fba_fee, fba_fee_base, fba_fee_tax, commission, commission_base, commission_tax, refund_commission, refund_commission_base, refund_commission_tax, marketplace_vat_principal, marketplace_vat_shipping, shipping_hb, shipping_hb_base, shipping_hb_tax, amazon_for_all_fee, amazon_for_all_fee_base, amazon_for_all_fee_tax) 
VALUES ( %s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s,%s)
ON DUPLICATE KEY UPDATE order_id = VALUES(order_id), order_adjustment_item_id = VALUES(order_adjustment_item_id), items_description = VALUES(items_description), item_amount = VALUES(item_amount), item_currency_code = VALUES(item_currency_code), item_related_identifiers = VALUES(item_related_identifiers), asin = VALUES(asin), sku = VALUES(sku), quantity_shipped = VALUES(quantity_shipped), fulfillment_network = VALUES(fulfillment_network), promo_rebates = VALUES(promo_rebates), our_price_discount = VALUES(our_price_discount), shipping_discount = VALUES(shipping_discount), product_charges = VALUES(product_charges), our_price_principal = VALUES(our_price_principal), shipping = VALUES(shipping), shipping_principal = VALUES(shipping_principal), tax = VALUES(tax), our_price_tax = VALUES(our_price_tax), our_price_tax_disc = VALUES(our_price_tax_disc), shipping_tax = VALUES(shipping_tax), shipping_tax_discount = VALUES(shipping_tax_discount), amazon_fees = VALUES(amazon_fees), fba_fee = VALUES(fba_fee), fba_fee_base = VALUES(fba_fee_base), fba_fee_tax = VALUES(fba_fee_tax), commission = VALUES(commission), commission_base = VALUES(commission_base), commission_tax = VALUES(commission_tax), refund_commission = VALUES(refund_commission), refund_commission_base = VALUES(refund_commission_base), refund_commission_tax = VALUES(refund_commission_tax), marketplace_vat_principal = VALUES(marketplace_vat_principal), marketplace_vat_shipping = VALUES(marketplace_vat_shipping), shipping_hb = VALUES(shipping_hb), shipping_hb_base = VALUES(shipping_hb_base), shipping_hb_tax = VALUES(shipping_hb_tax), amazon_for_all_fee = VALUES(amazon_for_all_fee), amazon_for_all_fee_base = VALUES(amazon_for_all_fee_base), amazon_for_all_fee_tax = VALUES(amazon_for_all_fee_tax)"""

item_counters = defaultdict(int)
batch_size = 1000

# --- Loop: procesa cada mes de forma independiente ---
for year, month in months_to_process:

    start_dt = datetime(year, month, 1, 6, 0, 0)  # 06:00 UTC = 00:00 hora México
    last_day = calendar.monthrange(year, month)[1]
    natural_end_dt = datetime(year, month, last_day, 6, 0, 0) + timedelta(days=1) - timedelta(seconds=1)  # cierre natural del mes en UTC

    # Si el cierre natural del mes cae en el futuro (mes en curso), usar "ahora" con margen de seguridad
    end_dt = min(natural_end_dt, safety_margin)

    if end_dt <= start_dt:
        print(f"=== Saltando {year}-{month:02d}: aún no hay rango válido (inicio de mes) ===\n")
        continue

    print(f"=== Procesando {year}-{month:02d}: {start_dt} a {end_dt} ===")

    params = {
        "postedAfter": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "postedBefore": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "marketplaceId": MARKETPLACE_ID
    }

    all_transactions = []
    while True:
        r = requests.get(finances_url, headers=headers, params=params)
        if r.status_code != 200:
            raise Exception(f"Error API Amazon: {r.status_code} - {r.text}")
        payload = r.json().get('payload', {})
        all_transactions.extend(payload.get('transactions') or [])
        next_token = payload.get('nextToken')
        if not next_token:
            break
        params = {'nextToken': next_token}

    print(f"Total transacciones {year}-{month:02d}: {len(all_transactions)}")

    all_txn_data  = []
    all_item_data = []

    for x in all_transactions:
        related = x.get('relatedIdentifiers') or []
        order_id      = next((r.get('relatedIdentifierValue') for r in related if r.get('relatedIdentifierName') == 'ORDER_ID'),      None)
        settlement_id = next((r.get('relatedIdentifierValue') for r in related if r.get('relatedIdentifierName') == 'SETTLEMENT_ID'), None)
        shipment_id   = next((r.get('relatedIdentifierValue') for r in related if r.get('relatedIdentifierName') == 'SHIPMENT_ID'),   None)

        txn_data = (
            json.dumps(x.get('sellingPartnerMetadata')),
            x.get('transactionType'),
            x.get('transactionId'),
            x.get('transactionStatus'),
            json.dumps(related),
            x.get('totalAmount', {}).get('currencyAmount'),
            x.get('totalAmount', {}).get('currencyCode'),
            x.get('description'),
            x.get('postedDate'),
            json.dumps(x.get('marketplaceDetails')),
            json.dumps(x.get('contexts')),
            order_id,
            settlement_id,
            shipment_id,
            x.get('refundedExpenses', {}).get('currencyAmount') if x.get('refundedExpenses') else None,
            x.get('refundedSales', {}).get('currencyAmount')    if x.get('refundedSales')    else None,
            json.dumps(x.get('items') or []),
        )
        all_txn_data.append(txn_data)

        for i in x.get('items') or []:
            contexts = i.get('contexts') or []
            item_bd  = i.get('breakdowns') or []
            txn_id   = x.get('transactionId')

            item_counters[txn_id] += 1
            transaction_item_id = f"{txn_id}-{item_counters[txn_id]}"

            item_id = next(
                (
                    r.get('itemRelatedIdentifierValue')
                    for r in (i.get('relatedIdentifiers') or [])
                    if r.get('itemRelatedIdentifierName') == 'ORDER_ADJUSTMENT_ITEM_ID'
                ),
                None
            ) or ''

            item_data = (
                txn_id,
                order_id,
                transaction_item_id,
                item_id,
                i.get('description'),
                i.get('totalAmount', {}).get('currencyAmount'),
                i.get('totalAmount', {}).get('currencyCode'),
                json.dumps(i.get('relatedIdentifiers')),
                next((c.get('asin')               for c in contexts), None) or '',
                next((c.get('sku')                for c in contexts), None) or '',
                next((c.get('quantityShipped')    for c in contexts), None),
                next((c.get('fulfillmentNetwork') for c in contexts), None),
                get_breakdown(item_bd, 'PromoRebates'),
                get_breakdown(item_bd, 'OurPriceDiscount'),
                get_nested_breakdown(item_bd, 'PromoRebates', 'ShippingDiscount'),
                get_breakdown(item_bd, 'ProductCharges'),
                get_nested_breakdown(item_bd, 'ProductCharges', 'OurPricePrincipal'),
                get_breakdown(item_bd, 'Shipping'),
                get_nested_breakdown(item_bd, 'Shipping', 'ShippingPrincipal'),
                get_breakdown(item_bd, 'Tax'),
                get_nested_breakdown(item_bd, 'Tax', 'OurPriceTax'),
                get_nested_breakdown(item_bd, 'Tax', 'OurPriceTaxDiscount'),
                get_nested_breakdown(item_bd, 'Tax', 'ShippingTax'),
                get_nested_breakdown(item_bd, 'Tax', 'ShippingTaxDiscount'),
                get_breakdown(item_bd, 'AmazonFees'),
                get_nested_breakdown(item_bd, 'AmazonFees', 'FBAPerUnitFulfillmentFee'),
                get_nested_breakdown(item_bd, 'FBAPerUnitFulfillmentFee', 'Base'),
                get_nested_breakdown(item_bd, 'FBAPerUnitFulfillmentFee', 'Tax'),
                get_nested_breakdown(item_bd, 'AmazonFees', 'Commission'),
                get_nested_breakdown(item_bd, 'Commission', 'Base'),
                get_nested_breakdown(item_bd, 'Commission', 'Tax'),
                get_breakdown(item_bd, 'RefundCommission'),
                get_nested_breakdown(item_bd, 'RefundCommission', 'Base'),
                get_nested_breakdown(item_bd, 'RefundCommission', 'Tax'),
                get_nested_breakdown(item_bd, 'Tax', 'MarketplaceFacilitatorVAT-Principal'),
                get_nested_breakdown(item_bd, 'Tax', 'MarketplaceFacilitatorVAT-Shipping'),
                get_nested_breakdown(item_bd, 'AmazonFees', 'ShippingHB'),
                get_nested_breakdown(item_bd, 'ShippingHB', 'Base'),
                get_nested_breakdown(item_bd, 'ShippingHB', 'Tax'),
                get_nested_breakdown(item_bd, 'AmazonFees', 'AmazonForAllFee'),
                get_nested_breakdown(item_bd, 'AmazonForAllFee', 'Base'),
                get_nested_breakdown(item_bd, 'AmazonForAllFee', 'Tax'),
            )
            all_item_data.append(item_data)

    for i in range(0, len(all_txn_data), batch_size):
        try:
            cursor.executemany(insert_txn, all_txn_data[i:i+batch_size])
            db.commit()
        except Exception as e:
            print(f"Error insert txn batch {i} ({year}-{month:02d}): {e}")

    for i in range(0, len(all_item_data), batch_size):
        try:
            cursor.executemany(insert_item, all_item_data[i:i+batch_size])
            db.commit()
        except Exception as e:
            print(f"Error insert item batch {i} ({year}-{month:02d}): {e}")

    print(f"=== {year}-{month:02d} completado ===\n")

cursor.close()
db.close()