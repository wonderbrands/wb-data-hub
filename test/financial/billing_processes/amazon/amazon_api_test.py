import requests
import mysql.connector
import sys
import os
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

from datetime import datetime, timedelta

# --- Configuración de Fechas e IDs ---
MARKETPLACE_ID = "A1AM78C64UM0Y8"
current_month_start = datetime.now().replace(day=1)

last_month = current_month_start - timedelta(days=1)

start_date = last_month.replace(day=1).strftime("%Y-%m-%d")
end_date = last_month.strftime("%Y-%m-%d")

try:
    db = mysql.connector.connect(
    host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"), database='somos_reyes'
        )
    cursor = db.cursor(dictionary=True)
except Exception as e:
    print(f"Error: {e}")



cursor = db.cursor()

cursor.execute("SET NAMES utf8mb4;")
cursor.execute("SET CHARACTER SET utf8mb4;")
cursor.execute("SET character_set_connection=utf8mb4;")

cursor.execute("SELECT access_token FROM amz_tokens")
access_token = cursor.fetchone()[0]

# Cerramos la conexión a la base de datos ya que no se harán inserts
cursor.close()
db.close()

# --- Configuración y Consulta a la API de Amazon ---
finances_url = "https://sellingpartnerapi-na.amazon.com/finances/2024-06-19/transactions"
headers = {
    "x-amz-access-token": access_token,
    "Content-Type": "application/json"
}
params = {
    "postedAfter": start_date + "T06:00:00Z",
    "postedBefore": end_date + "T05:59:59Z",
    "marketplaceId": MARKETPLACE_ID
}

all_transactions = []

# Bucle de paginación para obtener todas las transacciones
while True:
    r = requests.get(finances_url, headers=headers, params=params)
    if r.status_code != 200:
        raise Exception(f"Error API Amazon: {r.status_code} - {r.text}")
    
    payload = r.json().get('payload', {})
    all_transactions.extend(payload.get('transactions') or [])
    
    next_token = payload.get('nextToken')
    if not next_token:
        break
    params = {'nextToken': next_token}

print(f"Total transacciones: {len(all_transactions)}")