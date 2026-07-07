import requests
import mysql.connector
import os
import logging
from datetime import datetime, timezone, timedelta
import time
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

#load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

def extract_ml_payments():
    # ── 1. Conexión a BD ─────────────────────────────────────────
    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error BD: {e}")
        return

    # ── 2. Obtener Tokens (ML y MP) ──────────────────────────────
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = '25523702'")
    ml_token = str(cursor.fetchall()[0]['token'])
    
    # Tomamos el token de Mercado Pago de tu .env
    mp_token = os.getenv("mercado_pago_token")
    if not mp_token:
        log.error("Falta mercado_pago_token en el archivo .env")
        return

    ml_headers = {'Authorization': f'Bearer {ml_token}'}
    mp_headers = {'Authorization': f'Bearer {mp_token}'}

    # ── 3. Buscar órdenes facturadas sin pago registrado ─────────
    # Buscamos en Staging las facturas exitosas que aún no tienen registro en pagos
    cursor.execute("""
        SELECT b.mkp_order_id 
        FROM finance.mkp_billing_prod b
        LEFT JOIN finance.mkp_payments_prod p ON b.mkp_order_id = p.mkp_order_id
        WHERE b.status in ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED') AND p.mkp_order_id IS NULL;
    """)
    orders = cursor.fetchall()
    
    log.info(f"Ordenes pendientes: {len(orders)}")

    if not orders:
        log.info("No hay órdenes facturadas pendientes de revisar cobro.")
        cursor.close()
        db.close()
        return

    for o in orders:
        order_id = o['mkp_order_id']
        
        try:
            # PASO A: Obtener IDs de pagos desde Mercado Libre
            ml_url = f"https://api.mercadolibre.com/orders/{order_id}"
            r_ml = requests.get(ml_url, headers=ml_headers, timeout=10)
            
            if r_ml.status_code != 200:
                log.warning(f"Error consultando orden {order_id} en ML.")
                continue
                
            data_ml = r_ml.json()
            payments = data_ml.get('payments', [])
            
            for pay in payments:
                if pay.get('status') == 'approved':
                    payment_id = pay['id']
                    
                    # PASO B: Consultar fecha de liberación en Mercado Pago
                    mp_url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
                    r_mp = requests.get(mp_url, headers=mp_headers, timeout=10)
                    
                    if r_mp.status_code != 200:
                        log.warning(f"Error consultando pago {payment_id} en MP.")
                        continue
                    
                    data_mp = r_mp.json()
                    date_released_str = data_mp.get('date_released') or data_mp.get('money_release_date')
                    
                    if not date_released_str:
                        print(date_released_str)
                        continue
                        
                    # Parsear la fecha de Mercado Pago (ej. "2026-06-01T15:50:58.000-04:00")
                    # Limpiamos hasta los segundos para poder comparar en Python
                    raw_date = date_released_str[:19] 
                    #print(f'date_released_str: {date_released_str} // raw_date: {raw_date}')
                    date_rel_local = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S")
                    date_rel_utc = date_rel_local + timedelta(hours=4) # Convertimos el UTC-4 a UTC 0

                    date_rel = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%S")
                    
                    # PASO C: Validar si el dinero ya está liberado hoy
                    if date_rel_utc <= datetime.utcnow():
                        cursor.execute("""
                            INSERT IGNORE INTO finance.mkp_payments_prod 
                            (marketplace, mkp_order_id, payment_id, amount, date_released, status)
                            VALUES ('MERCADO_LIBRE', %s, %s, %s, %s, 'PENDING')
                        """, (order_id, payment_id, data_mp['transaction_amount'], date_rel))
                        db.commit()
                        
                        log.info(f"Pago liberado encontrado: {payment_id} (Orden {order_id})")
                    else:
                        log.info(f"Pago {payment_id} aún no liberado. Fecha programada: {date_rel}")
                        
        except Exception as e:
            log.error(f"Error procesando pagos de orden {order_id}: {e}")
            
        time.sleep(0.3) # Respetar Rate Limit de ambas APIs

    cursor.close()
    db.close()

if __name__ == "__main__":
    log.info("=== Iniciando Extracción de Pagos ===")
    extract_ml_payments()
    log.info("=== Finalizado ===")