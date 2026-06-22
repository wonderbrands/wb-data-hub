import os
import sys
import MySQLdb
import csv
import requests
from datetime import datetime, timedelta, timezone
import logging

# Configurar logs para Kestra (salida estándar para evitar falsos errores)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

def generate_and_send_report():
    # 1. Lógica Automática para "Ayer" (Calculado en CDMX)
    now_utc = datetime.now(timezone.utc)
    now_cdmx = now_utc - timedelta(hours=6)
    yesterday_cdmx = now_cdmx - timedelta(days=1)
    
    fecha_ayer = yesterday_cdmx.strftime('%Y-%m-%d')
    fecha_hoy = now_cdmx.strftime('%Y-%m-%d')
    
    limite_inferior = f"{fecha_ayer} 06:00:00"
    limite_superior = f"{fecha_hoy} 05:59:59"
    nombre_reporte = f"reporte_guias_{fecha_ayer}.csv"

    log.info(f"Generando reporte desde {limite_inferior} UTC hasta {limite_superior} UTC")

    # 2. Conexión a Base de Datos
    db = MySQLdb.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
        charset='utf8mb4'
    )
    cursor = db.cursor(MySQLdb.cursors.DictCursor)
    
    query = f"""
        SELECT 
            order_id AS orden_ml,
            marketplace_reference AS referencia_ml_paquete,
            odoo_so_name AS orden_odoo,
            print_status AS estado_de_inyeccion,
            odoo_carrier_ref AS paqueteria,
            error_message AS mensaje_de_error,
            updated_at - INTERVAL 6 HOUR AS hora_procesamiento_cdmx 
        FROM tools.ml_api_etl_orders
        WHERE updated_at >= '{limite_inferior}'
          AND updated_at <= '{limite_superior}'
          AND print_status IS NOT NULL
        ORDER BY updated_at DESC;
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    db.close()

    if not rows:
        log.info(f"No hubo guías procesadas para el día {fecha_ayer}. Fin del proceso.")
        return

    # 3. Crear el archivo CSV
    with open(nombre_reporte, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    
    log.info(f"CSV '{nombre_reporte}' generado con {len(rows)} registros.")

    # 4. Enviar a Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN") 
    slack_channel = os.getenv("SLACK_CHANNEL_ID")

    if slack_token and slack_channel:
        try:
            url = "https://slack.com/api/files.upload"
            headers = {"Authorization": f"Bearer {slack_token}"}
            data = {
                "channels": slack_channel,
                "initial_comment": f"📊 *Reporte Diario Kestra*\nSe procesaron *{len(rows)}* guías el día {fecha_ayer}. Adjunto el detalle:",
                "title": f"Reporte MercadoLibre {fecha_ayer}"
            }
            files = {'file': open(nombre_reporte, 'rb')}
            
            response = requests.post(url, headers=headers, data=data, files=files)
            if response.json().get("ok"):
                log.info("¡Reporte enviado a Slack exitosamente!")
            else:
                log.error(f"Slack rechazó el archivo. Detalles: {response.text}")
                sys.exit(1)
        except Exception as e:
            log.error(f"Error en la conexión con la API de Slack: {e}")
            sys.exit(1)
    else:
        log.error("Faltan las credenciales SLACK_BOT_TOKEN o SLACK_CHANNEL_ID en el entorno.")
        sys.exit(1)

if __name__ == "__main__":
    generate_and_send_report()