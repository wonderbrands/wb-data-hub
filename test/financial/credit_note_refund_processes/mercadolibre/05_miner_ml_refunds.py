"""
05_miner_ml_refunds.py — FASE 1: DETECCIÓN DE EVENTOS

Puebla finance.mkp_refunds_staging con los eventos de Mercado Libre que PUEDEN
derivar en una Nota de Crédito. NO descarga el CFDI todavía (eso es la fase 2).

Tres barridos:
  1. Cancelaciones  -> SQL puro contra somos_reyes.ml_order_update. Sin API.
  2. Reclamos       -> /post-purchase/v1/claims/search, watermark + orden desc.
  3. Refunds de MP  -> enriquecimiento sobre candidatos ya conocidos.

Idempotencia: columna generada event_key + UNIQUE KEY. Cada barrido puede
correr N veces sobre la misma ventana sin duplicar.

Convenciones heredadas de 01_miner_ml_billing.py:
  - Solo StreamHandler (Kestra captura stdout).
  - Sin logs por registro: un único resumen agregado al final.
  - Token expirado (401) aborta con SystemExit(1) para que Kestra reintente.
"""

import os
import sys
import time
import json
import logging
import requests
import mysql.connector
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv


load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── Parámetros por variables de entorno ────────────────────────
DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")

ML_SELLER_ID     = os.getenv("ML_SELLER_ID", "25523702")
ML_BILLING_START = os.getenv("ML_BILLING_START", "2026-06-01 15:50:55")
MP_TOKEN         = os.getenv("mercado_pago_token")

REQUEST_TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "15"))
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS", "0.1"))
CLAIMS_PAGE_SIZE    = int(os.getenv("CLAIMS_PAGE_SIZE", "50"))
CLAIMS_MAX_PAGES    = int(os.getenv("CLAIMS_MAX_PAGES", "60"))
# Solape hacia atrás sobre el watermark: last_updated de ML no es estrictamente
# monótono respecto del indexado. Reprocesar es barato; perder un evento no.
WATERMARK_OVERLAP_MIN = int(os.getenv("WATERMARK_OVERLAP_MIN", "360"))
# Primera corrida: cuántos días hacia atrás mirar si no hay watermark.
COLD_START_DAYS   = int(os.getenv("COLD_START_DAYS", "30"))
ENABLE_MP_ENRICH  = os.getenv("ENABLE_MP_ENRICH", "true").lower() == "true"

INVOICED_STATES = ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')


class TokenExpired(Exception):
    """El token de ML murió a media corrida."""


# ── Utilidades ─────────────────────────────────────────────────
def to_utc(value, assume_offset_hours=-4):
    """
    Normaliza cualquier fecha de ML/MP a datetime naive en UTC.

    ML devuelve '2026-06-01T15:50:58.000-04:00'. Si por algún motivo llega sin
    offset, se asume UTC-4 (hora de los servidores de ML), no UTC: asumir UTC
    sobre un string naive de ML es exactamente el bug de las 4 horas que ya
    apareció en 03_miner_ml_payments.py.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=assume_offset_hours)))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def get_db():
    return mysql.connector.connect(
        host=DB_HOST, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME
    )


def validate_env():
    required = {"DB_HOST": DB_HOST, "DB_USER": DB_USER,
                "DB_PASSWORD": DB_PASSWORD, "DB_NAME": DB_NAME}
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error(f"Faltan variables de entorno obligatorias: {', '.join(missing)}")
        raise SystemExit(1)


def get_ml_token(cursor):
    cursor.execute(
        "SELECT token FROM somos_reyes.tokens WHERE seller_id = %s",
        (ML_SELLER_ID,)
    )
    row = cursor.fetchone()
    if not row:
        log.error(f"No se encontró token para seller_id={ML_SELLER_ID}")
        raise SystemExit(1)
    return str(row['token'])


def ml_get(url, headers, params=None):
    """GET con manejo uniforme de 401 y errores de red. Devuelve dict o None."""
    try:
        r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        return None
    if r.status_code == 401:
        raise TokenExpired()
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


# ── Watermarks ─────────────────────────────────────────────────
def ensure_watermark_table(cursor, db):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS finance.mkp_sync_watermarks (
            sweep_name VARCHAR(64) PRIMARY KEY,
            last_watermark_value DATETIME NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    db.commit()


def read_watermark(cursor, sweep_name):
    cursor.execute(
        "SELECT last_watermark_value FROM finance.mkp_sync_watermarks WHERE sweep_name = %s",
        (sweep_name,)
    )
    row = cursor.fetchone()
    if row and row['last_watermark_value']:
        return row['last_watermark_value'] - timedelta(minutes=WATERMARK_OVERLAP_MIN)
    return datetime.utcnow() - timedelta(days=COLD_START_DAYS)


def write_watermark(cursor, db, sweep_name, value):
    if not value:
        return
    cursor.execute("""
        INSERT INTO finance.mkp_sync_watermarks (sweep_name, last_watermark_value)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE last_watermark_value = GREATEST(
            COALESCE(last_watermark_value, '1970-01-01'), VALUES(last_watermark_value))
    """, (sweep_name, value))
    db.commit()


# ── Escritura idempotente ──────────────────────────────────────
def upsert_event(cursor, db, payload):
    """
    Inserta o refresca un evento. Depende de la columna generada event_key con
    UNIQUE KEY: sin ella, los NULL de ml_claim_id / mp_refund_id hacen que el
    índice único no colisione nunca y se duplique en cada corrida.

    Solo se refrescan campos volátiles (estado del reclamo, raw_json). Nunca se
    pisa nada que haya escrito el loader: cfdi_uuid, refund_type, odoo_*.
    """
    cursor.execute("""
        INSERT INTO finance.mkp_refunds_staging
            (marketplace, mkp_order_id, ml_claim_id, mp_payment_id, mp_refund_id,
             source_event, status, classification_reason, raw_json, cfdi_seq)
        VALUES
            ('MERCADO_LIBRE', %(order_id)s, %(claim_id)s, %(payment_id)s,
             %(refund_id)s, %(source_event)s, %(status)s, %(reason)s,
             %(raw_json)s, 1)
        ON DUPLICATE KEY UPDATE
            status = IF(status IN ('CLAIM_OPEN', 'WAITING_CFDI',
                                   'ORIGINAL_NOT_INVOICED'),
                        VALUES(status), status),
            classification_reason = VALUES(classification_reason),
            mp_payment_id = COALESCE(VALUES(mp_payment_id), mp_payment_id),
            mp_refund_id  = COALESCE(VALUES(mp_refund_id),  mp_refund_id),
            raw_json      = VALUES(raw_json)
    """, payload)
    db.commit()
    return cursor.rowcount  # 1 = insert, 2 = update, 0 = sin cambios


def resolve_scope(cursor, order_id):
    """
    ¿Este evento nos incumbe? Devuelve (status, motivo).

    Un refund sobre una orden que nunca facturamos no tiene nada que revertir,
    pero tampoco se puede descartar: puede que el loader de billing todavía no
    la haya procesado.
    """
    cursor.execute("""
        SELECT b.status AS billing_status, o.date_created
        FROM somos_reyes.ml_order_update o
        LEFT JOIN finance.mkp_billing_prod b ON o.order_id = b.mkp_order_id
        WHERE o.order_id = %s
    """, (order_id,))
    row = cursor.fetchone()

    if not row:
        return 'OUT_OF_SCOPE', 'Orden no existe en ml_order_update'
    if row['date_created'] and str(row['date_created']) < ML_BILLING_START:
        return 'OUT_OF_SCOPE', f"Orden anterior a {ML_BILLING_START}"
    if row['billing_status'] not in INVOICED_STATES:
        return 'ORIGINAL_NOT_INVOICED', f"Billing en estado {row['billing_status']}"
    return None, None


# ── Barrido 1: cancelaciones (sin API) ─────────────────────────
def sweep_cancellations(cursor, db, stats):
    """
    Tipo A puro. La orden se canceló y ya la habíamos facturado en Odoo.
    Todo el dato está en MySQL: cero llamadas HTTP, cero rate limit.
    """
    placeholders = ", ".join(["%s"] * len(INVOICED_STATES))
    cursor.execute(f"""
        SELECT o.order_id
        FROM somos_reyes.ml_order_update o
        JOIN finance.mkp_billing_prod b ON o.order_id = b.mkp_order_id
        WHERE o.date_created >= %s
          AND o.status = 'cancelled'
          AND b.status IN ({placeholders})
        ORDER BY o.date_closed ASC
    """, (ML_BILLING_START, *INVOICED_STATES))

    for row in cursor.fetchall():
        n = upsert_event(cursor, db, {
            'order_id': str(row['order_id']),
            'claim_id': None, 'payment_id': None, 'refund_id': None,
            'source_event': 'ORDER_CANCELLED',
            'status': 'WAITING_CFDI',
            'reason': 'Orden cancelada en ML con factura ya publicada en Odoo',
            'raw_json': None,
        })
        stats['cancel_new'] += 1 if n == 1 else 0
        stats['cancel_seen'] += 1


# ── Barrido 2: reclamos y disputas ─────────────────────────────
def sweep_claims(cursor, db, headers, stats):
    """
    Paginación descendente por last_updated con corte contra el watermark.
    No depende de que la API soporte filtro de rango de fechas: si tu site lo
    soporta, cambiar a filtro explícito reduce páginas, pero esto funciona igual.
    """
    watermark = read_watermark(cursor, 'ml_claims')
    max_seen = None
    url = "https://api.mercadolibre.com/post-purchase/v1/claims/search"

    for page in range(CLAIMS_MAX_PAGES):
        data = ml_get(url, headers, params={
            'players.user_id': ML_SELLER_ID,
            'players.role': 'respondent',
            'sort': 'last_updated:desc',
            'limit': CLAIMS_PAGE_SIZE,
            'offset': page * CLAIMS_PAGE_SIZE,
        })
        if data is None:
            stats['api_errors'] += 1
            break

        claims = data.get('data') or []
        if not claims:
            break

        reached_watermark = False
        for claim in claims:
            last_updated = to_utc(claim.get('last_updated'))
            if last_updated:
                max_seen = max(max_seen, last_updated) if max_seen else last_updated
                if last_updated < watermark:
                    reached_watermark = True
                    continue

            # Solo reclamos anclados a una orden. Los de tipo 'shipment' u otros
            # recursos no tienen contraparte fiscal directa.
            if claim.get('resource') != 'order':
                stats['claims_skipped'] += 1
                continue

            order_id = str(claim.get('resource_id') or '')
            if not order_id:
                stats['claims_skipped'] += 1
                continue

            scope_status, scope_reason = resolve_scope(cursor, order_id)
            if scope_status == 'OUT_OF_SCOPE':
                stats['claims_out_of_scope'] += 1
                continue

            is_dispute = (claim.get('stage') == 'dispute')
            if scope_status:
                status, reason = scope_status, scope_reason
            elif claim.get('status') == 'closed':
                status = 'WAITING_CFDI'
                reason = (f"Reclamo cerrado · stage={claim.get('stage')} · "
                          f"type={claim.get('type')} · reason={claim.get('reason_id')}")
            else:
                status = 'CLAIM_OPEN'
                reason = (f"Reclamo abierto · stage={claim.get('stage')} · "
                          f"type={claim.get('type')}")

            n = upsert_event(cursor, db, {
                'order_id': order_id,
                'claim_id': str(claim.get('id')),
                'payment_id': None, 'refund_id': None,
                'source_event': 'DISPUTE' if is_dispute else 'CLAIM',
                'status': status,
                'reason': reason[:255],
                'raw_json': json.dumps(claim, ensure_ascii=False),
            })
            stats['claims_new'] += 1 if n == 1 else 0
            stats['claims_seen'] += 1
            if status == 'WAITING_CFDI':
                stats['claims_resolved'] += 1

        time.sleep(SLEEP_BETWEEN_CALLS)
        if reached_watermark or len(claims) < CLAIMS_PAGE_SIZE:
            break

    write_watermark(cursor, db, 'ml_claims', max_seen)


# ── Barrido 3: enriquecimiento con el lado del dinero ──────────
def enrich_with_mp_refunds(cursor, db, ml_headers, stats):
    """
    Sobre los candidatos ya detectados, busca el refund real en Mercado Pago.

    No es un barrido de descubrimiento: consulta solo órdenes que ya están en la
    tabla esperando CFDI. Sirve para dos cosas — confirmar que hubo movimiento
    de dinero, y capturar ml_money_taken_date, que es la fecha con la que se
    debe reversar el pago en Odoo (no la fecha de la NC).
    """
    if not MP_TOKEN:
        log.warning("Sin mercado_pago_token: se omite el enriquecimiento de MP.")
        return

    mp_headers = {'Authorization': f'Bearer {MP_TOKEN}'}
    cursor.execute("""
        SELECT id, mkp_order_id
        FROM finance.mkp_refunds_staging
        WHERE status = 'WAITING_CFDI'
          AND mp_refund_id IS NULL
        ORDER BY created_at ASC
    """)
    candidates = cursor.fetchall()

    for cand in candidates:
        order = ml_get(
            f"https://api.mercadolibre.com/orders/{cand['mkp_order_id']}",
            ml_headers
        )
        if not order:
            stats['api_errors'] += 1
            continue

        for pay in order.get('payments', []):
            payment_id = pay.get('id')
            if not payment_id:
                continue

            refunds = ml_get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}/refunds",
                mp_headers
            )
            if not refunds:
                continue

            # El endpoint puede devolver lista o dict paginado según versión.
            items = refunds if isinstance(refunds, list) else refunds.get('results', [])
            for ref in items:
                taken = to_utc(ref.get('date_created'))
                cursor.execute("""
                    UPDATE finance.mkp_refunds_staging
                    SET mp_payment_id       = %s,
                        mp_refund_id        = %s,
                        ml_money_taken_date = %s
                    WHERE id = %s AND mp_refund_id IS NULL
                """, (str(payment_id), str(ref.get('id')), taken, cand['id']))
                db.commit()
                stats['mp_refunds_found'] += 1
                break

        time.sleep(SLEEP_BETWEEN_CALLS)


# ── Main ───────────────────────────────────────────────────────
def detect_refund_events():
    validate_env()
    db = get_db()
    cursor = db.cursor(dictionary=True)

    stats = {
        'cancel_seen': 0, 'cancel_new': 0,
        'claims_seen': 0, 'claims_new': 0, 'claims_resolved': 0,
        'claims_skipped': 0, 'claims_out_of_scope': 0,
        'mp_refunds_found': 0, 'api_errors': 0,
    }
    token_expired = False

    try:
        ensure_watermark_table(cursor, db)
        ml_token = get_ml_token(cursor)
        headers = {'Authorization': f'Bearer {ml_token}'}

        sweep_cancellations(cursor, db, stats)
        sweep_claims(cursor, db, headers, stats)
        if ENABLE_MP_ENRICH:
            enrich_with_mp_refunds(cursor, db, headers, stats)

    except TokenExpired:
        token_expired = True
        log.error("Token expirado (401). Abortando detección.")
    finally:
        log.info(
            f"Resumen deteccion -> "
            f"Cancelaciones: {stats['cancel_seen']} (nuevas {stats['cancel_new']}) | "
            f"Reclamos: {stats['claims_seen']} (nuevos {stats['claims_new']}, "
            f"resueltos {stats['claims_resolved']}) | "
            f"Fuera de alcance: {stats['claims_out_of_scope']} | "
            f"Sin orden: {stats['claims_skipped']} | "
            f"Refunds MP: {stats['mp_refunds_found']} | "
            f"Errores API: {stats['api_errors']}"
        )
        cursor.close()
        db.close()

    if token_expired:
        raise SystemExit(1)


if __name__ == "__main__":
    detect_refund_events()