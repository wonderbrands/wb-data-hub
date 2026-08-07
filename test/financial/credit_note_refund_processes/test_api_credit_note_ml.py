#!/usr/bin/env python3
"""
test_ml_billing_endpoints.py

Script simple de testeo de endpoints de facturación de Mercado Libre.
Solo reutiliza la lógica de conexión (token ML desde la DB) del script
original 01_miner_ml_billing.py. No inserta ni actualiza nada en la DB.

Configurá ORDER_ID / INVOICE_ID abajo y correlo con: python test_ml_billing_endpoints.py
"""

import os
import requests
import MySQLdb
from datetime import datetime
from dotenv import load_dotenv


load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# ── EDITAR ACÁ ──────────────────────────────────────────────────
ORDER_ID   = "2000016949680478"   # usado para /json y /xml?transaction_type=sale_return
INVOICE_ID = None          # usado para /invoice/{id}/xml (dejar None si no aplica)

# ── Config DB / ML (mismas env vars que el script original) ────
DB_HOST     = os.getenv("DB_HOST")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME     = os.getenv("DB_NAME")
ML_SELLER_ID    = os.getenv("ML_SELLER_ID", "25523702")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))

BASE_URL = "https://api.mercadolibre.com"


def get_ml_token():
    db = MySQLdb.connect(
        host=DB_HOST, user=DB_USER,
        passwd=DB_PASSWORD, db=DB_NAME,
        local_infile=True, charset='utf8mb4'
    )
    cursor = db.cursor()
    cursor.execute(
        "SELECT token FROM somos_reyes.tokens WHERE seller_id = %s",
        (ML_SELLER_ID,)
    )
    row = cursor.fetchall()
    cursor.close()
    db.close()
    return str(row[0][0])


def show(label, resp, save_as):
    print(f"\n=== {label} ===")
    print(f"URL: {resp.url}")
    print(f"Status: {resp.status_code}")
    print(f"Body (primeros 800 chars):\n{resp.text[:800]}")
    if resp.status_code == 200:
        with open(save_as, 'wb') as f:
            f.write(resp.content)
        print(f"[OK] Guardado en: {save_as}")


headers = {'Authorization': f'Bearer {get_ml_token()}'}

if ORDER_ID:
    
    r0 = requests.get(f"{BASE_URL}/orders/{ORDER_ID}",
                       headers=headers, timeout=REQUEST_TIMEOUT)
    show("Discovery ORDER data", r0, f"order_{ORDER_ID}_data.json")
    
    
    
    # 1) Discovery JSON
    r1 = requests.get(f"{BASE_URL}/invoices/io/documents/stream/order/{ORDER_ID}/json",
                       headers=headers, timeout=REQUEST_TIMEOUT)
    show("Discovery JSON (order)", r1, f"order_{ORDER_ID}_discovery.json")

    # 2) XML sale_return
    r2 = requests.get(f"{BASE_URL}/invoices/io/documents/stream/order/{ORDER_ID}/xml",
                       headers=headers,
                       timeout=REQUEST_TIMEOUT)
    show("XML sale_return (order)", r2, f"order_{ORDER_ID}_sale_return.xml")

if INVOICE_ID:
    # 3) XML por invoice_id
    r3 = requests.get(f"{BASE_URL}/invoices/io/documents/stream/invoice/{INVOICE_ID}/xml",
                       headers=headers, timeout=REQUEST_TIMEOUT)
    show("XML por invoice_id", r3, f"invoice_{INVOICE_ID}.xml")