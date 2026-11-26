# ── Registro dirigido de los cobros históricos B1 (segundo pago) — VoBo de Finanzas del 24-nov ──
# Los segundos pagos aprobados antes del 18-nov no los mina el 03 (corte SECOND_PAYMENTS_SINCE). Una corrida
# del 03 con el corte en junio revisaría ~11 mil órdenes; este script registra SOLO los pagos de la lista B1
# (Pagos_por_aplicar_ML_2026-11-19.csv, categoría "B1 SEGUNDO PAGO"), igual que lo haría el 03:
#   - consulta el pago en Mercado Pago: debe seguir 'approved' y con el dinero liberado;
#   - lo inserta en finance.mkp_payments_prod como PENDING a nombre de su orden (INSERT IGNORE: idempotente).
# Después el 04 los aplica con sus guardas (tope al saldo, centavos $0.15, facturas ya pagadas, periodo cerrado):
#   ONLY_PAYMENT_IDS=<los registrados> python 04_loader_ml_payments.py
# Los de jul–sep quedan PENDING (periodo cerrado) y entran solos con la reapertura.
# LOCAL_DRY_RUN = True: solo consulta y lista.
import os
import csv
import time
from datetime import datetime, timedelta
import requests
import MySQLdb

try:
    from dotenv import load_dotenv
    load_dotenv(r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\.env", override=False)
except ImportError:
    pass

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
LOCAL_DRY_RUN  = True
LOCAL_CSV      = r"finanzas\Pagos_por_aplicar_ML_2026-11-19.csv"
LOCAL_CATEGORY = "B1"
# ══════════════════════════════════════════════════════════════
DRY_RUN = os.getenv("DRY_RUN", str(LOCAL_DRY_RUN)).strip().lower() == "true"
MP_UTC_OFFSET_HOURS = 4
NOTE = "B1 histórico (segundo pago; VoBo Finanzas 24-nov)"

here = os.path.dirname(os.path.abspath(__file__))
rows = [r for r in csv.DictReader(open(os.path.join(here, LOCAL_CSV), encoding="utf-8-sig"))
        if r["Categoría"].startswith(LOCAL_CATEGORY)]
db = MySQLdb.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"), passwd=os.getenv("DB_PASSWORD"),
                     db=os.getenv("DB_NAME"), charset="utf8mb4")
cur = db.cursor()
headers = {"Authorization": f"Bearer {os.getenv('MERCADO_PAGO_TOKEN')}"}
inserted, out = [], []
for r in rows:
    pid, order = r["Pago (payment_id)"], r["Orden"]
    resp = requests.get(f"https://api.mercadopago.com/v1/payments/{pid}", headers=headers, timeout=15)
    time.sleep(0.2)
    if resp.status_code != 200:
        out.append((pid, order, f"MP HTTP {resp.status_code}: no se registra"))
        continue
    mp = resp.json()
    released = mp.get("date_released") or mp.get("money_release_date")
    if mp.get("status") != "approved":
        out.append((pid, order, f"MP status '{mp.get('status')}' ({mp.get('status_detail')}): no se registra"))
        continue
    rel = datetime.strptime(released[:19], "%Y-%m-%dT%H:%M:%S") if released else None
    if not rel or rel + timedelta(hours=MP_UTC_OFFSET_HOURS) > datetime.utcnow():
        out.append((pid, order, "Dinero aún no liberado: no se registra"))
        continue
    amount = mp["transaction_amount"]
    if DRY_RUN:
        out.append((pid, order, f"[SECO] se registraría ${amount:,.2f} liberado {rel:%Y-%m-%d}"))
        continue
    cur.execute("""INSERT IGNORE INTO finance.mkp_payments_prod
                   (marketplace, mkp_order_id, payment_id, amount, date_released, status, error_log)
                   VALUES ('MERCADO_LIBRE', %s, %s, %s, %s, 'PENDING', %s)""", (order, pid, amount, rel, NOTE))
    db.commit()
    if cur.rowcount:
        inserted.append(pid)
    out.append((pid, order, f"registrado ${amount:,.2f} liberado {rel:%Y-%m-%d}" if cur.rowcount else "ya estaba registrado"))
db.close()
for o in out:
    print(*o, sep=" | ")
print(f"B1: {len(rows)} | registrados ahora: {len(inserted)}")
if inserted:
    print("ONLY_PAYMENT_IDS=" + ",".join(inserted))
