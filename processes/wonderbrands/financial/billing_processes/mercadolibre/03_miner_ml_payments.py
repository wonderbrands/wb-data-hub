import requests
import mysql.connector
import os
import logging
import sys
from datetime import datetime, timedelta
import time

# ── Logging setup ──────────────────────────────────────────────
# Solo StreamHandler: Kestra captura stdout/stderr como logs de la ejecución.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Parámetros por variables de entorno ────────────────────────
# Definidas en flows/wonderbrands/financial/mercadolibre/ml_payments_flow.yml
DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")
MP_TOKEN    = os.getenv("MERCADO_PAGO_TOKEN")

ML_SELLER_ID        = os.getenv("ML_SELLER_ID", "25523702")
REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "10"))
# Pausa entre órdenes para respetar el rate limit de ML y MP
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.3"))
# MP devuelve la fecha en UTC-4; horas a sumar para llevarla a UTC 0
MP_UTC_OFFSET_HOURS = int(os.getenv("MP_UTC_OFFSET_HOURS", "4"))


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
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = %s", (ML_SELLER_ID,))
    row = cursor.fetchall()
    if not row:
        log.error(f"No se encontró token para seller_id={ML_SELLER_ID}")
        cursor.close()
        db.close()
        raise SystemExit(1)
    ml_token = str(row[0]['token'])

    ml_headers = {'Authorization': f'Bearer {ml_token}'}
    mp_headers = {'Authorization': f'Bearer {MP_TOKEN}'}

    # ── 3. Buscar órdenes facturadas sin pago registrado ─────────
    cursor.execute("""
        SELECT b.mkp_order_id
        FROM finance.mkp_billing_prod b
        LEFT JOIN finance.mkp_payments_prod p ON b.mkp_order_id = p.mkp_order_id
        WHERE b.status in ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED') AND p.mkp_order_id IS NULL;
    """)
    orders = cursor.fetchall()
    log.info(f"Órdenes pendientes de revisar cobro: {len(orders)}")

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
    token_expired = False

    for o in orders:
        order_id = o['mkp_order_id']

        try:
            # PASO A: Obtener IDs de pagos desde Mercado Libre
            r_ml = requests.get(f"https://api.mercadolibre.com/orders/{order_id}",
                                headers=ml_headers, timeout=REQUEST_TIMEOUT)

            if r_ml.status_code == 401:
                token_expired = True
                log.error("Token de ML expirado (401). Abortando script.")
                break

            if r_ml.status_code != 200:
                ml_errors += 1
                continue

            payments = r_ml.json().get('payments', [])

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
                if date_rel_utc <= datetime.utcnow():
                    cursor.execute("""
                        INSERT IGNORE INTO finance.mkp_payments_prod
                        (marketplace, mkp_order_id, payment_id, amount, date_released, status)
                        VALUES ('MERCADO_LIBRE', %s, %s, %s, %s, 'PENDING')
                    """, (order_id, payment_id, data_mp['transaction_amount'], date_rel))
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
        f"Resumen -> Órdenes revisadas: {len(orders)} | Pagos liberados: {released} | "
        f"Pagos no liberados: {not_released} | "
        f"Errores: {total_errors} (ml={ml_errors}, mp={mp_errors}, otros={other_errors})"
    )
    cursor.close()
    db.close()

    # Si un token murió a media corrida, la tarea debe fallar para que Kestra reintente/alerte
    if token_expired:
        raise SystemExit(1)


if __name__ == "__main__":
    extract_ml_payments()
