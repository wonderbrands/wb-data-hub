import os
import requests
import MySQLdb
import zipfile
import io
from dotenv import load_dotenv

# Cargar variables de entorno locales
load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

def get_db_connection():
    return MySQLdb.connect(
        host=os.getenv("DB_HOST"), 
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), 
        database=os.getenv("DB_NAME"),
        local_infile=True, 
        charset='utf8mb4'
    )

def get_ml_token(db):
    cursor = db.cursor()
    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = '25523702'")
    return str(cursor.fetchone()[0])

def get_test_shipping_id(db):
    """
    Obtiene un ml_shipping_id de una orden QUE YA FUE PROCESADA E IMPRESA
    para demostrar que ML permite volver a descargar la guía.
    """
    cursor = db.cursor()
    cursor.execute("""
        SELECT ml_shipping_id, marketplace_reference 
        FROM tools.ml_api_etl_orders 
        WHERE print_status = 'PRINTED' AND ml_shipping_id IS NOT NULL 
        LIMIT 1
    """)
    row = cursor.fetchone()
    return row[0], row[1] if row else (None, None)

def test_multiple_label_downloads():
    db = get_db_connection()
    access_token = get_ml_token(db)
    shipping_id, mkp_ref = get_test_shipping_id(db)
    db.close()

    if not shipping_id:
        print("❌ No se encontró ninguna orden con estado 'PRINTED' en la BD para hacer la prueba.")
        return

    print(f"📦 Iniciando prueba de estrés de descarga para Orden: {mkp_ref} | Shipping ID: {shipping_id}")
    url = f'https://api.mercadolibre.com/shipment_labels?shipment_ids={shipping_id}&response_type=zpl2&access_token={access_token}'

    # Realizar 3 peticiones consecutivas al mismo endpoint
    for intento in range(1, 4):
        print(f"\n--- INTENTO DE DESCARGA #{intento} ---")
        response = requests.get(url)
        print(f"Código de Estado HTTP: {response.status_code}")

        if response.status_code == 200:
            try:
                # Validar que el binario es un ZIP válido y extraer el ZPL en memoria
                zip_file = zipfile.ZipFile(io.BytesIO(response.content))
                txt_filename = next((name for name in zip_file.namelist() if name.endswith('.txt')), None)
                
                if txt_filename:
                    zpl_bytes = zip_file.read(txt_filename)
                    print(f"✅ Éxito: Archivo '{txt_filename}' extraído correctamente.")
                    print(f"📊 Tamaño del ZPL en memoria: {len(zpl_bytes)} bytes.")
                    print(f"🖨️ Muestra del contenido ZPL: {zpl_bytes[:60].decode('utf-8')}...")
                else:
                    print("⚠️ El ZIP se descargó pero no contiene un archivo .txt.")
            except Exception as e:
                print(f"❌ Error al descomprimir el ZIP recibido: {str(e)}")
        else:
            print(f"❌ Falló la petición a ML: {response.text}")

if __name__ == "__main__":
    test_multiple_label_downloads()