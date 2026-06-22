import os
import sys
import MySQLdb
import csv
import requests
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

def generate_and_send_report():
    # 1. Recibir fechas y canal de Kestra/Slack
    fecha_inicio = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    fecha_fin = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    slack_channel = os.getenv("SLACK_CHANNEL_ID")

    if not fecha_inicio or not fecha_fin:
        log.error("Faltan las fechas. Uso esperado: YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)

    try:
        # Calcular el límite superior (Le sumamos 1 día a la fecha fin para abarcar hasta las 23:59:59 CDMX)
        dfin = datetime.strptime(fecha_fin, '%Y-%m-%d')
        dfin_plus1 = dfin + timedelta(days=1)
        fecha_fin_plus1 = dfin_plus1.strftime('%Y-%m-%d')

        limite_inferior = f"{fecha_inicio} 06:00:00"
        limite_superior = f"{fecha_fin_plus1} 05:59:59"
        nombre_reporte = f"reporte_guias_{fecha_inicio}_al_{fecha_fin}.csv"
    except ValueError:
        log.error("Formato de fecha incorrecto. Debe ser YYYY-MM-DD.")
        sys.exit(1)

    log.info(f"Generando reporte desde {limite_inferior} UTC hasta {limite_superior} UTC")

    # 2. Conexión y Query
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
        mensaje_vacio = f"No hubo guías procesadas en el rango del {fecha_inicio} al {fecha_fin}."
        log.info(mensaje_vacio)
        _enviar_mensaje_slack(mensaje_vacio, slack_channel)
        return

    # 3. Crear CSV
    with open(nombre_reporte, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    # 4. Enviar Archivo a Slack (Usando la API Moderna v2)
    slack_token = os.getenv("SLACK_BOT_TOKEN") 
    slack_channel = os.getenv("SLACK_CHANNEL_ID")

    if slack_token and slack_channel:
        try:
            # PASO 1: Pedir URL de subida segura a Slack
            file_size = os.path.getsize(nombre_reporte)
            headers = {"Authorization": f"Bearer {slack_token}"}
            params = {"filename": nombre_reporte, "length": file_size}
            
            get_url = "https://slack.com/api/files.getUploadURLExternal"
            resp_url = requests.get(get_url, headers=headers, params=params).json()
            
            if not resp_url.get("ok"):
                log.error(f"Error al pedir URL a Slack: {resp_url}")
                sys.exit(1)
                
            upload_url = resp_url["upload_url"]
            file_id = resp_url["file_id"]
            
            # PASO 2: Subir el archivo físico a la URL segura
            with open(nombre_reporte, 'rb') as f:
                upload_response = requests.post(upload_url, data=f)
                
            if upload_response.status_code != 200:
                log.error("Error al subir el archivo físico a Slack.")
                sys.exit(1)
                
            # PASO 3: Publicar el archivo en el canal de Slack
            complete_url = "https://slack.com/api/files.completeUploadExternal"
            complete_data = {
                "channel_id": slack_channel,
                "initial_comment": f" *Reporte a guías MeercadoLibre:* Detalle de guías del *{fecha_inicio} al {fecha_fin}* ({len(rows)} registros).",
                "files": [{"id": file_id, "title": nombre_reporte}]
            }
            
            resp_complete = requests.post(complete_url, headers=headers, json=complete_data).json()
            
            if resp_complete.get("ok"):
                log.info("¡Reporte enviado a Slack exitosamente con la nueva API!")
            else:
                log.error(f"Slack rechazó publicar el archivo: {resp_complete}")
                sys.exit(1)
                
        except Exception as e:
            log.error(f"Error en la conexión con Slack API: {e}")
            sys.exit(1)
    else:
        log.error("Faltan las credenciales SLACK_BOT_TOKEN o SLACK_CHANNEL_ID en el entorno.")
        sys.exit(1)

def _enviar_mensaje_slack(texto, canal):
    token = os.getenv("SLACK_BOT_TOKEN")
    if token and canal:
        requests.post("https://slack.com/api/chat.postMessage", 
                      headers={"Authorization": f"Bearer {token}"}, 
                      json={"channel": canal, "text": texto})

if __name__ == "__main__":
    generate_and_send_report()