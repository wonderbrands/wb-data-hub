"""Solo lectura: corre el clasificar_excedente nuevo de 03 sobre los cobros de
la tabla que ya tienen excedente. No escribe en MySQL ni en Odoo."""
import re
import sys
import importlib.util

sys.stdout.reconfigure(encoding='utf-8')
DIR = r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Claude_proyects\wb-data-hub\test\financial\billing_processes\amazon'
sys.path.insert(0, DIR)
spec = importlib.util.spec_from_file_location('loader03', DIR + r'\03_loader_amz_payments.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

db = m.get_db_connection()
cur = db.cursor(dictionary=True)
cur.execute(f"""
    SELECT p.id, p.mkp_order_id, p.payment_id, p.amount, p.error_log,
           b.odoo_so_name, b.raw_json
    FROM {m.TBL_PAYMENTS} p
    LEFT JOIN {m.TBL_BILLING} b
           ON b.marketplace = p.marketplace AND b.mkp_order_id = p.mkp_order_id
    WHERE p.marketplace = %s AND p.error_log LIKE '%%EXCEDENTE%%'
    ORDER BY p.id""", (m.MARKETPLACE,))
filas = cur.fetchall()
cur.close()
print(f"{len(filas)} cobros con excedente en {m.TBL_PAYMENTS}\n")

odoo = m.OdooModelProxy(timeout=120)
for r in filas:
    r['settlement_id'] = m.parse_settlement_id(r['payment_id'])
    mt = re.search(r'sin_aplicar ([\d\.]+)', r['error_log'] or '')
    mp = re.search(r'promo ([\d\.]+)', r['error_log'] or '')
    excedente = float(mt.group(1)) if mt else (float(mp.group(1)) if mp else None)
    if excedente is None or not r['odoo_so_name']:
        print(f"{r['mkp_order_id']}: sin excedente legible o sin SO -> se omite")
        continue
    facturas = m.buscar_factura(odoo, r['odoo_so_name'])
    tol = round(m.TOLERANCIA_REDONDEO * max(len(facturas), 1), 2)
    c = m.clasificar_excedente(db, r, facturas, excedente, tol)
    print(f"{r['mkp_order_id']} {r['odoo_so_name']} stl {r['settlement_id']} "
          f"exc {excedente:.2f} -> {c['clase']}: {c['motivo']}")
    print(f"    nota actual: {r['error_log']}")
db.close()
