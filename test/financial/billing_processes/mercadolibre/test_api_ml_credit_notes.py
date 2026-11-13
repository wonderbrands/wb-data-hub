import requests
import MySQLdb
import os
from dotenv import load_dotenv

# ── Configuración de Entorno ──────────────────────────────────
load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')
ORDER_ID = "2000016709192140"

# ── Obtener Token de la Base de Datos ─────────────────────────
print("Conectando a la base de datos...")
try:
    db = MySQLdb.connect(
        host=os.getenv("DB_HOST"), 
        user=os.getenv("DB_USER"),
        passwd=os.getenv("DB_PASSWORD"), 
        db=os.getenv("DB_NAME"),
        charset='utf8mb4'
    )
    cursor = db.cursor()

    cursor.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = '25523702'")
    row = cursor.fetchone() # fetchone es suficiente para un solo token

    if not row:
        print("❌ No se encontró token para seller_id=25523702 en la BD.")
        cursor.close()
        db.close()
        exit()

    TOKEN = row[0]
    cursor.close()
    db.close()
    print("✅ Token extraído correctamente.")

except Exception as e:
    print(f"❌ Error conectando a la BD: {e}")
    exit()

# ── Endpoints y Peticiones ────────────────────────────────────
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
url_xml = f"https://api.mercadolibre.com/invoices/io/documents/stream/order/{ORDER_ID}/xml"
url_xml_nc = f"https://api.mercadolibre.com/invoices/io/documents/stream/order/{ORDER_ID}/xml?transaction_type=devolution"

# 1. Extraer y guardar XML
print(f"\nObteniendo XML para la orden {ORDER_ID}...")
res_xml = requests.get(url_xml, headers=HEADERS)

if res_xml.status_code == 200:
    with open(f"{ORDER_ID}.xml", "wb") as f:
        f.write(res_xml.content)
    print("✅ XML guardado correctamente.")
else:
    print(f"❌ Error XML ({res_xml.status_code}): {res_xml.text}")

# 2. Extraer y guardar xml de devolución
print(f"\nObteniendo xml de devolución para la orden {ORDER_ID}...")
res_xml_nc = requests.get(url_xml_nc, headers=HEADERS)

if res_xml.status_code == 200:
    with open(f"{ORDER_ID}_credit_note.xml", "wb") as f:
        f.write(res_xml_nc.content)
    print("✅ xml de devolución guardado correctamente.")
else:
    print(f"❌ Error xml ({res_xml_nc.status_code}): {res_xml_nc.text}")