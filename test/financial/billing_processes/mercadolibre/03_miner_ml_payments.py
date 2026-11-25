import requests
import mysql.connector
import os
import logging
import sys
from datetime import datetime, timedelta
import time
from ml_stores import get_store_config

# ── Logging setup ──────────────────────────────────────────────
# Solo StreamHandler: Kestra captura stdout/stderr como logs de la ejecución.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Parámetros por variables de entorno ────────────────────────
# Definidas en flows/wonderbrands/financial/billing_processes/mercadolibre/ml_payments_flow.yml
# y ml_oficiales_payments_flow.yml (cada flujo mapea su propio secreto a MERCADO_PAGO_TOKEN)
DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")
MP_TOKEN    = os.getenv("MERCADO_PAGO_TOKEN")

# Tienda de Mercado Libre (Kestra inyecta ML_SELLER_ID; el resto sale de ml_stores.py)
STORE               = get_store_config()
ML_SELLER_ID        = STORE['seller_id']
MKP_MARKETPLACE     = STORE['marketplace']
REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "10"))
# Pausa entre órdenes para respetar el rate limit de ML y MP
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.3"))
# MP devuelve la fecha en UTC-4; horas a sumar para llevarla a UTC 0
MP_UTC_OFFSET_HOURS = int(os.getenv("MP_UTC_OFFSET_HOURS", "4"))
# Corte del criterio (b) de abajo (otro pago aprobado sin registrar en una orden que ya tiene
# cobro): solo pagos creados desde esta fecha. Los anteriores (49 pagos, ~$63k al 18-nov) se
# aplican en una corrida dirigida con SECOND_PAYMENTS_SINCE=2026-06-01, con el VoBo de Finanzas.
SECOND_PAYMENTS_SINCE = os.getenv("SECOND_PAYMENTS_SINCE", "2026-11-18")
# Corrida en seco: consulta las APIs pero no registra nada en mkp_payments_prod.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"


def invoiced_pack_sibling(cursor, ml_headers, pack_id, order_id):
    """Orden facturada (ODOO_INVOICED / ALREADY_ODOO_INVOICED) del mismo paquete de MeLi, o None."""
    if not pack_id:
        return None
    r = requests.get(f"https://api.mercadolibre.com/packs/{pack_id}", headers=ml_headers, timeout=REQUEST_TIMEOUT)
    if r.status_code not in (200, 206):
        return None
    siblings = [str(x['id']) for x in r.json().get('orders', []) if str(x['id']) != str(order_id)]
    if not siblings:
        return None
    cursor.execute(f"""
        SELECT mkp_order_id FROM finance.mkp_billing_prod
        WHERE marketplace = %s AND status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')
          AND mkp_order_id IN ({','.join(['%s'] * len(siblings))})
        ORDER BY mkp_order_id LIMIT 1
    """, (MKP_MARKETPLACE, *siblings))
    row = cursor.fetchone()
    return row['mkp_order_id'] if row else None


def read_ml_token(db, cursor):
    """Token vigente de la tienda en somos_reyes.tokens (otro flujo lo renueva cada pocas horas).
    El commit abre una lectura nueva: sin él, la conexión sigue viendo el token del inicio."""
    db.commit()
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = %s", (ML_SELLER_ID,))
    row = cursor.fetchall()
    return str(row[0]['token']) if row else None


def extract_ml_payments():
    # ── Validación de configuración ────────────────────────────
    missing = [k for k, v in {
        "DB_HOST": DB_HOST, "DB_USER": DB_USER, "DB_PASSWORD": DB_PASSWORD,
        "DB_NAME": DB_NAME, "MERCADO_PAGO_TOKEN": MP_TOKEN
    }.items() if not v]
    if missing:
        log.error(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
        raise SystemExit(1)

    # ── 1. Conexión a BD ─────────────────────────────────────────
    try:
        db = mysql.connector.connect(
            host=DB_HOST, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error BD: {e}")
        raise SystemExit(1)

    # ── 2. Obtener Tokens (ML y MP) ──────────────────────────────
    ml_token = read_ml_token(db, cursor)
    if not ml_token:
        log.error(f"No se encontró token para seller_id={ML_SELLER_ID}")
        cursor.close()
        db.close()
        raise SystemExit(1)

    ml_headers = {'Authorization': f'Bearer {ml_token}'}
    mp_headers = {'Authorization': f'Bearer {MP_TOKEN}'}

    # ── 3. Buscar órdenes facturadas con cobro por registrar ─────
    # a) Facturadas sin ningún pago registrado (criterio original).
    cursor.execute("""
        SELECT b.mkp_order_id
        FROM finance.mkp_billing_prod b
        LEFT JOIN finance.mkp_payments_prod p
          ON p.marketplace = b.marketplace
         AND p.mkp_order_id = b.mkp_order_id
        WHERE b.marketplace = %s
          AND b.status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')
          AND p.mkp_order_id IS NULL;
    """, (MKP_MARKETPLACE,))
    order_ids = {o['mkp_order_id'] for o in cursor.fetchall()}
    without_payments = len(order_ids)

    # b) Órdenes con OTRO pago aprobado aún sin registrar. Una orden pagada con dos pagos que se
    #    liberan en corridas distintas quedaba fuera de (a) en cuanto entraba el primero, y el
    #    segundo nunca se aplicaba (caso INV/2026/162445). Se parte de ml_order_payments (ETL)
    #    filtrada por tienda y fecha, y se cruza por lotes contra la facturación (sin EXISTS
    #    correlacionados: no terminan sobre estas tablas).
    cursor.execute("""
        SELECT DISTINCT op.order_id
        FROM somos_reyes.ml_order_payments op
        LEFT JOIN finance.mkp_payments_prod p
          ON p.marketplace = %s AND p.payment_id = op.payment_id
        WHERE op.seller_id = %s
          AND op.date_created >= %s
          AND op.date_created >= %s
          AND op.pay_status = 'approved'
          AND p.id IS NULL
    """, (MKP_MARKETPLACE, ML_SELLER_ID, STORE['billing_start'], SECOND_PAYMENTS_SINCE))
    with_missing = [str(o['order_id']) for o in cursor.fetchall()]
    for i in range(0, len(with_missing), 1000):
        chunk = with_missing[i:i + 1000]
        cursor.execute(f"""
            SELECT mkp_order_id FROM finance.mkp_billing_prod
            WHERE marketplace = %s AND status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')
              AND mkp_order_id IN ({','.join(['%s'] * len(chunk))})
        """, (MKP_MARKETPLACE, *chunk))
        order_ids.update(o['mkp_order_id'] for o in cursor.fetchall())

    # c) Carritos (Finanzas, 21-nov): Yuju crea UNA SO por paquete (pack) de MeLi y solo una de sus
    #    órdenes queda facturada; las demás quedan en ORDER_NEVER_IN_ODOO y su pago nunca se aplicaba.
    #    Se busca la orden facturada del mismo paquete y el pago se registra a nombre de ella.
    pack_orders = set()
    for i in range(0, len(with_missing), 1000):
        chunk = with_missing[i:i + 1000]
        cursor.execute(f"""
            SELECT mkp_order_id FROM finance.mkp_billing_prod
            WHERE marketplace = %s AND status = 'ORDER_NEVER_IN_ODOO'
              AND mkp_order_id IN ({','.join(['%s'] * len(chunk))})
        """, (MKP_MARKETPLACE, *chunk))
        pack_orders.update(o['mkp_order_id'] for o in cursor.fetchall())

    # Los carritos van primero: son pocos y, si la corrida se corta (token, error), no se quedan sin revisar.
    orders = [{'mkp_order_id': o, 'pack': True} for o in sorted(pack_orders)]
    orders += [{'mkp_order_id': o, 'pack': False} for o in sorted(order_ids)]
    log.info(f"[{MKP_MARKETPLACE}] Órdenes pendientes de revisar cobro: {len(orders)} "
             f"(sin ningún cobro: {without_payments}; con otro pago por registrar desde "
             f"{SECOND_PAYMENTS_SINCE}: {len(order_ids) - without_payments}; "
             f"de carrito sin SO propia: {len(pack_orders)})")

    if not orders:
        cursor.close()
        db.close()
        return

    # ── Contadores (los detalles por pago NO se loguean: solo el resumen final) ──
    released     = 0   # Pagos liberados insertados como PENDING
    not_released = 0   # Pagos aprobados cuya fecha de liberación aún no llega (o sin fecha)
    ml_errors    = 0
    mp_errors    = 0
    other_errors = 0
    pack_matched = 0   # Órdenes de carrito cuyo pago se registró a nombre de la orden facturada del paquete
    pack_waiting = 0   # Órdenes de carrito sin orden facturada en su paquete (todavía)
    token_expired = False

    for o in orders:
        order_id = o['mkp_order_id']

        try:
            # PASO A: Obtener IDs de pagos desde Mercado Libre
            r_ml = requests.get(f"https://api.mercadolibre.com/orders/{order_id}",
                                headers=ml_headers, timeout=REQUEST_TIMEOUT)

            if r_ml.status_code == 401:
                # El token vence a media corrida: se relee el renovado y se reintenta una vez.
                ml_token = read_ml_token(db, cursor) or ml_token
                ml_headers = {'Authorization': f'Bearer {ml_token}'}
                r_ml = requests.get(f"https://api.mercadolibre.com/orders/{order_id}",
                                    headers=ml_headers, timeout=REQUEST_TIMEOUT)
                if r_ml.status_code == 401:
                    log.info("[TOKEN] Token de ML renovado desde somos_reyes.tokens: sigue en 401.")
                else:
                    log.info("[TOKEN] Token de ML renovado desde somos_reyes.tokens.")

            if r_ml.status_code == 401:
                token_expired = True
                log.error("Token de ML expirado (401). Abortando script.")
                break

            # 206 (Partial Content) también trae la orden con sus pagos: antes se descartaba
            # y esos pagos nunca se registraban (31 de 94 sin cobro al 23-nov).
            if r_ml.status_code not in (200, 206):
                ml_errors += 1
                continue

            order_json = r_ml.json()
            payments = order_json.get('payments', [])

            # Carrito: el pago se registra a nombre de la orden facturada del mismo paquete
            register_as = order_id
            if o['pack']:
                register_as = invoiced_pack_sibling(cursor, ml_headers, order_json.get('pack_id'), order_id)
                if not register_as:
                    pack_waiting += 1
                    continue
                pack_matched += 1

            for pay in payments:
                if pay.get('status') != 'approved':
                    continue
                payment_id = pay['id']

                # PASO B: Consultar fecha de liberación en Mercado Pago
                r_mp = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
                                    headers=mp_headers, timeout=REQUEST_TIMEOUT)

                if r_mp.status_code == 401:
                    token_expired = True
                    log.error("Token de Mercado Pago expirado (401). Abortando script.")
                    break

                if r_mp.status_code != 200:
                    mp_errors += 1
                    continue

                data_mp = r_mp.json()
                date_released_str = data_mp.get('date_released') or data_mp.get('money_release_date')

                if not date_released_str:
                    not_released += 1
                    continue

                # Parsear la fecha de Mercado Pago (ej. "2026-06-01T15:50:58.000-04:00")
                # Limpiamos hasta los segundos para poder comparar en Python
                date_rel = datetime.strptime(date_released_str[:19], "%Y-%m-%dT%H:%M:%S")
                date_rel_utc = date_rel + timedelta(hours=MP_UTC_OFFSET_HOURS)

                # PASO C: Validar si el dinero ya está liberado hoy
                if date_rel_utc <= datetime.utcnow() and DRY_RUN:
                    log.info(f"[SECO] pago {payment_id} orden {order_id} -> se registraría a nombre de "
                             f"{register_as} por ${data_mp['transaction_amount']:,.2f} (liberado {date_rel:%Y-%m-%d})")
                    released += 1
                elif date_rel_utc <= datetime.utcnow():
                    # Carrito: queda anotada la orden real del pago (el 04 conserva la marca al procesarlo)
                    pack_note = f"Carrito: pago de la orden {order_id}" if register_as != order_id else None
                    cursor.execute("""
                        INSERT IGNORE INTO finance.mkp_payments_prod
                        (marketplace, mkp_order_id, payment_id, amount, date_released, status, error_log)
                        VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
                    """, (MKP_MARKETPLACE, register_as, payment_id, data_mp['transaction_amount'], date_rel, pack_note))
                    db.commit()
                    released += cursor.rowcount
                else:
                    not_released += 1

            if token_expired:
                break

        except Exception:
            other_errors += 1

        time.sleep(SLEEP_BETWEEN_CALLS)  # Respetar Rate Limit de ambas APIs

    # ── Resumen único de la corrida ────────────────────────────
    total_errors = ml_errors + mp_errors + other_errors
    log.info(
        f"[{MKP_MARKETPLACE}] Resumen{' (EN SECO)' if DRY_RUN else ''} -> Órdenes revisadas: {len(orders)} | Pagos liberados: {released} | "
        f"Pagos no liberados: {not_released} | Carritos: {pack_matched} ligados, {pack_waiting} sin orden facturada | "
        f"Errores: {total_errors} (ml={ml_errors}, mp={mp_errors}, otros={other_errors})"
    )
    cursor.close()
    db.close()

    # Si un token murió a media corrida, la tarea debe fallar para que Kestra reintente/alerte
    if token_expired:
        raise SystemExit(1)


if __name__ == "__main__":
    extract_ml_payments()
