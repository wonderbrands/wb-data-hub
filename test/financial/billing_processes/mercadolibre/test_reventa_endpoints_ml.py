import os, requests, MySQLdb
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')
OID, SELLER = '2000018220646686', '25523702'

db = MySQLdb.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                     passwd=os.getenv("DB_PASSWORD"), db=os.getenv("DB_NAME"), charset='utf8mb4')
cur = db.cursor()
cur.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = %s", (SELLER,))
H = {'Authorization': f'Bearer {cur.fetchone()[0]}'}
db.close()

API = "https://api.mercadolibre.com"
urls = [
    f"{API}/invoices/io/documents/stream/order/{OID}/xml?transaction_type=resale",
    f"{API}/invoices/io/documents/stream/order/{OID}/xml?transaction_type=meli_resale",
    f"{API}/invoices/io/documents/stream/resale/{OID}/xml",
    f"{API}/invoices/io/documents/stream/resale/order/{OID}/xml",
    f"{API}/invoices-download/resale/{OID}/xml?source=ml",
    f"{API}/emisor/omni/api/invoices-download/resale/{OID}/xml?source=ml",
    f"https://www.mercadolibre.com.mx/emisor/omni/api/invoices-download/resale/{OID}/xml?source=ml",
]
for u in urls:
    r = requests.get(u, headers=H, timeout=20, allow_redirects=False)
    ok = '<cfdi:Comprobante' in r.text
    print(f"[{r.status_code}] {'XML OK ' if ok else ''}{u.replace(API, '')}\n   {r.text[:150]!r}")