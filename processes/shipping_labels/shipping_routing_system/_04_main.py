import _03_shipping_logic_parallel
import logging
from datetime import datetime
import time as tm
import gspread
import os

def set_logging_info():
    credentials_json = '/var/lib/jenkins/m1/credenciales_reportes.json'
    #credentials_json = r'C:\Users\Sergio Gil Guerrero\PycharmProjects\Herramientas propias\shipping_routing_system_test_POO-V3\google_cred.json'

    # Zona horaria
    UTC_LOCAL = -6
    LOG_FILENAME = datetime.now().strftime("%Y-%m-%d -- %H-%M-%S") + ".log"

    # Configuración básica
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),  # Enviar logs a la consola
            logging.FileHandler(LOG_FILENAME)  # Guardar logs en un archivo
        ]
    )
    # Ajustar zona horaria en los logs
    #logging.Formatter.converter = lambda *args: tm.localtime(tm.time() + UTC_LOCAL * 3600)

    return LOG_FILENAME, credentials_json

# Función para subir logs a Google Sheets
def insert_log_in_sheets(log_file, file_id, credentials_json):
    """
    Sube un archivo de log a una hoja de Google Sheets.
    """
    print("Subiendo log a Google Sheets...")
    gc = gspread.service_account(filename=credentials_json)
    sh = gc.open_by_key(file_id)
    try:
        worksheet = sh.worksheet("log")  # Intenta abrir la hoja "log"
    except gspread.exceptions.WorksheetNotFound:
        print("La hoja 'log' no existe. Creando una nueva hoja...")
        worksheet = sh.add_worksheet(title="log", rows="1000", cols="1")  # Crear la hoja si no existe

    # Lee el archivo de logs .log
    with open(log_file, 'r') as file:
        lines = file.readlines()
        lines.reverse()  # Invierte el orden de las líneas para que las más recientes aparezcan al principio

    # Obtén los datos actuales de la hoja de Google Sheets
    current_data = worksheet.get_all_values()
    # Prepara los nuevos datos para actualizar la hoja
    updated_data = [[line.strip()] for line in lines] + current_data
    # Borra el contenido actual de la hoja de Google Sheets
    #worksheet.clear()
    # Actualiza la hoja de Google Sheets con los nuevos datos
    worksheet.update('A1', updated_data)
    print("Log actualizado en Google Sheets.")

def delete_log_file(file_path):
    """
    Elimina el archivo de log local.
    """
    try:
        os.remove(file_path)
        print(f"Archivo de log eliminado: {file_path}")
    except FileNotFoundError:
        print(f"Archivo no encontrado: {file_path}")


if __name__ == "__main__":

    # Iniciar manejador de loggers. Tambien devuelve el path del file y las credenciales de google.
    LOG_FILE, credentials_json = set_logging_info()
    logging.info('\n-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*-*\n')

    # ///////////////////////////////////////////////

    """ 
    Heurística basada en el modelo de Amdahl.
    
    Optimal Threads = Número de Núcleos × (1 + R)
    
    Donde R es la relación entre el tiempo de espera de I/O y el tiempo de computación. 
    
    """


    # ///////////////////////////////////////////////

    # ///////////////////////////////////////////////////////////////////////////////////////////////////////
    # ///////////////////////////////////////////////////////////////////////////////////////////////////////
    start = tm.time()

    # Proceso de Shipping Routing System
    order_processor = _03_shipping_logic_parallel.OrderProcessor(carrier_apis_test=False, odoo_test=False, top_number=100)
    # --------------------------------------------------------------------------------------------
    order_processor.process_orders(num_hours=10) # num_days , num_hours , max_workers=N
    """
    num_hours -> Numero de horas para buscar ordenes nuevas en Odoo
    max_workers -> Numero de hilos, si no pasamos nada, por default es None. 
    
    process_orders -> get_orders_list_info -> get_date_range (for Odoo) -> get_all_marketplace_orders -> build_odoo_domain:
    so_domain = [
            ('date_order', '>=', start_date),
            ('date_order', '<=', end_date),
            ('wb_srs_flag', '=', False),
            ('data_tracking_readwrite', 'not ilike', 'TURBO'),
        ]
    """
    # --------------------------------------------------------------------------------------------

    end = tm.time()
    logging.info(f"Tiempo transcurrido de la ejecución TOTAL: {round(end - start, 2)} [sec]")
    # ///////////////////////////////////////////////////////////////////////////////////////////////////////
    # ///////////////////////////////////////////////////////////////////////////////////////////////////////


    # Subir log al final
    GOOGLE_SHEETS_FILE_ID = "1zcbvVNzVuW2SgHlF3d64tN602g5G4G1Pu72_b0zXa1A"
    try:
        insert_log_in_sheets(LOG_FILE, GOOGLE_SHEETS_FILE_ID, credentials_json)
    finally:
        logging.shutdown()
        delete_log_file(LOG_FILE)

