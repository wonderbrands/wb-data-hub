# ── Log de cobros de MeLi para Finanzas (SOLO LECTURA: MySQL + Odoo) ──────────
# Por cada pago de finance.mkp_payments_prod: orden, SO, factura, pago de Mercado Pago, monto,
# fecha de liberación, estado del flujo, nota (centavos, tope, carrito), y en Odoo el pago creado
# (memo 'MP <payment_id>'), su asiento y el monto. Lo piden en la hoja de ruta de Finanzas
# (24-nov, filas 3 y 4): los 45 históricos de B1 y lo desplegado el 23-nov.
#   LOCAL_MODO = "desde"  -> pagos registrados desde LOCAL_DESDE (created_at)
#   LOCAL_MODO = "ids"    -> los payment_id de LOCAL_IDS_CSV (columna "Pago (payment_id)"), p.ej. los B1
# Sale un CSV en finanzas/ y un resumen por estado en el log. No escribe nada.
import os
import csv
import sys
import xmlrpc.client
from datetime import datetime
import MySQLdb

try:
    from dotenv import load_dotenv
    load_dotenv(r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\.env", override=False)
except ImportError:
    pass

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
LOCAL_MODO     = "desde"                 # desde | ids
LOCAL_DESDE    = "2026-11-23"
LOCAL_IDS_CSV  = r"finanzas\Pagos_por_aplicar_ML_2026-11-19.csv"
LOCAL_IDS_CAT  = "B1"                    # solo filas cuya Categoría empieza con esto
LOCAL_ODOO     = "prod"                  # prod | test
# ══════════════════════════════════════════════════════════════
MODO, DESDE = os.getenv("MODO", LOCAL_MODO), os.getenv("DESDE", LOCAL_DESDE)
URL = (os.getenv("ODOO_TEST_URL") if LOCAL_ODOO == "test" else os.getenv("odoo_urlV18")).strip()
DB = (os.getenv("ODOO_TEST_DB") if LOCAL_ODOO == "test" else os.getenv("odoo_dbV18")).strip()
PWD = os.getenv("odoo_password_dataV18")
UID = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common").authenticate(DB, os.getenv("odoo_user_dataV18"), PWD, {})
M = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)


def sr(model, domain, fields, **kw):
    return M.execute_kw(DB, UID, PWD, model, "search_read", [domain], {"fields": fields, **kw})


db = MySQLdb.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"), passwd=os.getenv("DB_PASSWORD"),
                     db=os.getenv("DB_NAME"), charset="utf8mb4")
cur = db.cursor()
base = """SELECT p.payment_id, p.mkp_order_id, b.odoo_so_name, p.amount, p.date_released, p.status, p.error_log, p.created_at
          FROM finance.mkp_payments_prod p LEFT JOIN finance.mkp_billing_prod b ON b.marketplace = p.marketplace AND b.mkp_order_id = p.mkp_order_id
          WHERE p.marketplace = 'MERCADO_LIBRE' AND """
if MODO == "ids":
    here = os.path.dirname(os.path.abspath(__file__))
    rows_csv = list(csv.DictReader(open(os.path.join(here, LOCAL_IDS_CSV), encoding="utf-8-sig")))
    ids = [r["Pago (payment_id)"] for r in rows_csv if r["Categoría"].startswith(LOCAL_IDS_CAT)]
    cur.execute(base + f"p.payment_id IN ({','.join(['%s'] * len(ids))})", ids)
    found = cur.fetchall()
    missing = sorted(set(ids) - {str(r[0]) for r in found})
else:
    cur.execute(base + "p.created_at >= %s", (DESDE,))
    found, missing = cur.fetchall(), []
db.close()

memo_field = "memo" if "memo" in M.execute_kw(DB, UID, PWD, "account.payment", "fields_get", [], {"attributes": ["type"]}) else "communication"
out = []
for i in range(0, len(found), 200):
    chunk = found[i:i + 200]
    pays = {p[memo_field]: p for p in sr("account.payment", [(memo_field, "in", [f"MP {r[0]}" for r in chunk])],
                                         [memo_field, "name", "amount", "move_id", "reconciled_invoice_ids", "date"])}
    for pid, order, so, amount, rel, status, note, created in chunk:
        p = pays.get(f"MP {pid}")
        inv = sr("account.move", [("id", "in", p["reconciled_invoice_ids"])], ["name"]) if p and p["reconciled_invoice_ids"] else []
        out.append({"Pago MP": pid, "Orden": order, "SO": so or "", "Factura": ", ".join(x["name"] for x in inv),
                    "Monto MP": float(amount), "Liberado": rel, "Estado flujo": status, "Nota": note or "",
                    "Carrito": "Sí" if (note or "").startswith("Carrito:") else "",
                    "Pago Odoo": p["name"] if p else "", "Asiento": p["move_id"][1] if p and p["move_id"] else "",
                    "Monto aplicado": p["amount"] if p else "", "Fecha pago Odoo": p["date"] if p else "", "Registrado": created})
for pid in missing:
    out.append({"Pago MP": pid, "Estado flujo": "NO REGISTRADO TODAVÍA"})
stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas", f"Log_cobros_ML_{MODO}_{stamp}.csv")
os.makedirs(os.path.dirname(path), exist_ok=True)   # finanzas/ no está en el repo
cols = ["Pago MP", "Orden", "SO", "Factura", "Monto MP", "Liberado", "Estado flujo", "Nota", "Carrito", "Pago Odoo", "Asiento",
        "Monto aplicado", "Fecha pago Odoo", "Registrado"]
with open(path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    w.writerows(out)
summary = {}
for r in out:
    k = r["Estado flujo"]
    n, a = summary.get(k, (0, 0.0))
    summary[k] = (n + 1, a + float(r.get("Monto MP") or 0))
print(f"Pagos: {len(out)} | " + " | ".join(f"{k}: {n} (${a:,.2f})" for k, (n, a) in sorted(summary.items(), key=lambda x: -x[1][0])))
print(f"CSV: {path}")
sys.exit(0)
