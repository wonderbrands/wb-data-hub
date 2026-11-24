# ── Análisis de cobros de Mercado Libre (SOLO LECTURA) ─────────────────────────
# Responde a Finanzas (hoja de ruta 18-nov, punto 5) ANTES de aplicar pagos históricos:
#   A) Facturas de MeLi parcialmente pagadas (jun–hoy): por qué quedó saldo abierto.
#      Explica los ~$181k que Finanzas ve abiertos en la 105.01.004.
#   B) Corrida en seco del flujo de cobros (03): pagos aprobados en MeLi que todavía no
#      están registrados. Segundos pagos (caso INV/2026/162445) y órdenes sin ningún
#      cobro con pago aprobado hace más de SIN_COBRO_DIAS días.
# NO escribe nada: en Odoo solo usa search_read/read (lo garantiza odoo()); en MySQL solo SELECT.
# Lee Odoo PRODUCTIVO: lo corre Serch (desde esta máquina el clasificador bloquea esa lectura).
# Salida: 3 CSV en finanzas/ (detalle A, detalle B y resumen por categoría y mes).
import os
import sys
import csv
import time
import logging
import xmlrpc.client
from collections import defaultdict
from datetime import date, datetime, timedelta

import MySQLdb

DOTENV_PATHS = os.getenv("DOTENV_PATHS", ";".join([
    r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\.env",
    r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Claude_proyects\wb-data-hub\.env",
])).split(";")
try:
    from dotenv import load_dotenv
    for _path in DOTENV_PATHS:
        if os.path.exists(_path):
            load_dotenv(_path, override=False)
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
LOCAL_ODOO     = os.getenv("ODOO_TARGET", "prod")   # "prod" = Odoo productivo | "test" = ODOO_TEST_URL / ODOO_TEST_DB
DATE_FROM      = "2026-06-01"    # arranque del autofacturador de MeLi
SIN_COBRO_DIAS = 15              # B: orden sin ningún cobro con pago aprobado hace más de N días
# ══════════════════════════════════════════════════════════════

MARKETPLACE = "MERCADO_LIBRE"
SELLER_ID   = "25523702"
CXC_CODE    = "105.01.004"
TOLERANCE   = 1.00               # misma regla de ±$1.00 que TOTAL_DIFF
CHUNK       = 500
OUT_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas")
APPLIED     = ("ODOO_PAID", "INVOICE_SETTLED")      # estados del 04 que ya están en Odoo

if LOCAL_ODOO == "test":
    ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
    ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
else:
    ODOO_URL = (os.getenv("ODOO_URL") or os.getenv("odoo_urlV18") or "").strip()
    ODOO_DB  = (os.getenv("ODOO_DB") or os.getenv("odoo_dbV18") or "").strip()
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
READ_METHODS  = ("search_read", "read", "search", "search_count")


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = 120
        return conn


_common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", transport=TimeoutTransport())
UID = _common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not UID:
    raise SystemExit(f"Odoo rechazó la autenticación ({ODOO_URL}, {ODOO_DB})")
_models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", transport=TimeoutTransport(), allow_none=True)


def odoo(model, method, *args, **kwargs):
    """Solo lectura: cualquier otro método se rechaza aquí, antes de llegar a Odoo."""
    if method not in READ_METHODS:
        raise RuntimeError(f"analisis_cobros_ml es de solo lectura: '{method}' no permitido")
    for attempt in range(1, 4):
        try:
            return _models.execute_kw(ODOO_DB, UID, ODOO_PASSWORD, model, method, list(args), kwargs)
        except (xmlrpc.client.ProtocolError, OSError):
            if attempt == 3:
                raise
            time.sleep(3 * attempt)


def chunks(items, size=CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def month(value):
    return value.strftime("%Y-%m") if value else "sin fecha"


def money(value):
    return f"{value:.2f}"


# ── Datos de MySQL por orden ───────────────────────────────────
def load_order_data(cursor, order_ids):
    """Pagos de MeLi, cobros registrados por el 03/04 y NC descargadas por el 05."""
    pays, regs, ncs = defaultdict(list), defaultdict(list), defaultdict(list)
    for ch in chunks(order_ids):
        ph = ",".join(["%s"] * len(ch))
        cursor.execute(f"""SELECT order_id, payment_id, pay_status, txn_amt, refunded_amt, date_approved
                           FROM somos_reyes.ml_order_payments
                           WHERE seller_id = %s AND order_id IN ({ph})""", (SELLER_ID, *ch))
        for oid, pid, st, amt, ref, dap in cursor.fetchall():
            pays[str(oid)].append({"payment_id": str(pid), "status": st, "amount": float(amt or 0),
                                   "refunded": float(ref or 0), "date_approved": dap})
        cursor.execute(f"""SELECT mkp_order_id, payment_id, amount, status FROM finance.mkp_payments_prod
                           WHERE marketplace = %s AND mkp_order_id IN ({ph})""", (MARKETPLACE, *ch))
        for oid, pid, amt, st in cursor.fetchall():
            regs[str(oid)].append({"payment_id": str(pid), "amount": float(amt or 0), "status": st or ""})
        cursor.execute(f"""SELECT mkp_order_id, cfdi_uuid, total_amount, status FROM finance.mkp_credit_notes_prod
                           WHERE marketplace = %s AND mkp_order_id IN ({ph})""", (MARKETPLACE, *ch))
        for oid, uuid, amt, st in cursor.fetchall():
            ncs[str(oid)].append({"uuid": uuid, "amount": float(amt or 0), "status": st or ""})
    return pays, regs, ncs


def describe(regs, ncs):
    r = "; ".join(f"{x['payment_id']} {x['status']} ${x['amount']:,.2f}" for x in regs) or "-"
    n = "; ".join(f"{x['status']} ${x['amount']:,.2f}" for x in ncs) or "-"
    return r, n


def classify_partial(invoice, pays, regs, ncs):
    """Por qué una factura parcialmente pagada tiene saldo. La primera regla que aplica gana."""
    residual = invoice["amount_residual"]
    registered = {r["payment_id"] for r in regs}
    unregistered = [p for p in pays if p["status"] == "approved" and p["payment_id"] not in registered]
    unregistered_amt = sum(p["amount"] for p in unregistered)
    applied_amt = sum(r["amount"] for r in regs if r["status"].startswith(APPLIED))
    if residual <= TOLERANCE:
        return "1 CENTAVOS (saldo <= $1.00)"
    if unregistered and abs(unregistered_amt - residual) <= TOLERANCE:
        return "2 SEGUNDO PAGO SIN REGISTRAR (lo corrige el 03)"
    if any(not r["status"].startswith(APPLIED) for r in regs):
        return "3 PAGO REGISTRADO SIN APLICAR EN ODOO (error del 04)"
    if any(not n["status"].startswith("ODOO_CREDITED") for n in ncs):
        return "4 NC DE MELI PENDIENTE DE CARGAR (la cierra el 06)"
    if any(p["refunded"] > 0 for p in pays):
        return "5 REEMBOLSO PARCIAL SIN NC EN LA TABLA"
    if unregistered:
        return "6 PAGO SIN REGISTRAR POR OTRO MONTO"
    if applied_amt < invoice["amount_total"] - TOLERANCE:
        return "7 COBRO DE MELI MENOR A LA FACTURA"
    return "8 OTRO (revisar a mano)"


def invoices_by_so(so_names):
    """Factura publicada de cada SO (por invoice_origin). Si hay más de una se listan todas."""
    out = defaultdict(list)
    for ch in chunks(sorted(so_names)):
        for inv in odoo("account.move", "search_read",
                        [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("invoice_origin", "in", ch)],
                        fields=["name", "invoice_origin", "payment_state", "amount_total", "amount_residual"]):
            out[inv["invoice_origin"]].append(inv)
    return out


def main():
    today = date.today().isoformat()
    log.info(f"=== Análisis de cobros ML (solo lectura) | Odoo: {ODOO_URL} | desde {DATE_FROM} ===")
    db = MySQLdb.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"), passwd=os.getenv("DB_PASSWORD"),
                         db=os.getenv("DB_NAME"), charset="utf8mb4")
    cursor = db.cursor()
    summary = defaultdict(lambda: [0, 0.0])          # (sección, categoría, mes) -> [n, importe]

    # ── A) Facturas de MeLi parcialmente pagadas ──────────────────
    cxc = odoo("account.account", "search_read", [("code", "=", CXC_CODE)], fields=["id"])
    if not cxc:
        raise SystemExit(f"No existe la cuenta {CXC_CODE} en Odoo")
    domain = [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("payment_state", "=", "partial"),
              ("invoice_date", ">=", DATE_FROM), ("line_ids.account_id", "=", cxc[0]["id"])]
    partials, offset = [], 0
    while True:
        page = odoo("account.move", "search_read", domain, offset=offset, limit=2000, order="id asc",
                    fields=["name", "invoice_origin", "invoice_date", "amount_total", "amount_residual",
                            "journal_id", "l10n_mx_edi_cfdi_uuid"])
        partials += page
        offset += len(page)
        if len(page) < 2000:
            break
    log.info(f"[A] Facturas de MeLi parcialmente pagadas desde {DATE_FROM}: {len(partials)} | "
             f"saldo ${sum(i['amount_residual'] for i in partials):,.2f}")

    # SO -> orden de MeLi (mkp_billing_prod; si falta, channel_order_reference de la SO)
    so_names = {i["invoice_origin"] for i in partials if i["invoice_origin"]}
    so_to_order = {}
    for ch in chunks(so_names):
        cursor.execute(f"""SELECT odoo_so_name, mkp_order_id FROM finance.mkp_billing_prod
                           WHERE marketplace = %s AND odoo_so_name IN ({','.join(['%s'] * len(ch))})""",
                       (MARKETPLACE, *ch))
        so_to_order.update({so: str(oid) for so, oid in cursor.fetchall()})
    for ch in chunks(so_names - set(so_to_order)):
        for so in odoo("sale.order", "search_read", [("name", "in", ch)], fields=["name", "channel_order_reference"]):
            if so.get("channel_order_reference"):
                so_to_order[so["name"]] = str(so["channel_order_reference"])
    pays, regs, ncs = load_order_data(cursor, set(so_to_order.values()))

    rows_a = []
    for inv in partials:
        oid = so_to_order.get(inv["invoice_origin"])
        cat = classify_partial(inv, pays[oid], regs[oid], ncs[oid]) if oid else "9 SIN ORDEN DE MELI"
        inv_month = inv["invoice_date"][:7] if inv["invoice_date"] else "sin fecha"
        summary[("A parciales", cat, inv_month)][0] += 1
        summary[("A parciales", cat, inv_month)][1] += inv["amount_residual"]
        r_txt, n_txt = describe(regs[oid], ncs[oid]) if oid else ("-", "-")
        rows_a.append({"Categoría": cat, "Mes factura": inv_month, "Factura": inv["name"],
                       "Diario": inv["journal_id"][1] if inv["journal_id"] else "", "SO": inv["invoice_origin"],
                       "Orden": oid or "", "Total factura": money(inv["amount_total"]),
                       "Saldo abierto": money(inv["amount_residual"]),
                       "Pagos aprobados en MeLi": "; ".join(f"{p['payment_id']} ${p['amount']:,.2f}"
                                                            + (f" (reembolsado ${p['refunded']:,.2f})" if p['refunded'] else "")
                                                            for p in pays.get(oid, []) if p["status"] == "approved") or "-",
                       "Cobros registrados (03/04)": r_txt, "NC de MeLi (05/06)": n_txt,
                       "UUID factura": inv["l10n_mx_edi_cfdi_uuid"] or ""})

    # ── B) Corrida en seco del 03: pagos aprobados sin registrar ─────
    cursor.execute("""SELECT op.order_id, op.payment_id, op.txn_amt, op.refunded_amt, op.date_approved
                      FROM somos_reyes.ml_order_payments op
                      LEFT JOIN finance.mkp_payments_prod p ON p.marketplace = %s AND p.payment_id = op.payment_id
                      WHERE op.seller_id = %s AND op.date_created >= %s AND op.pay_status = 'approved' AND p.id IS NULL""",
                   (MARKETPLACE, SELLER_ID, DATE_FROM))
    pending = cursor.fetchall()
    invoiced = {}
    for ch in chunks({str(r[0]) for r in pending}, 1000):
        cursor.execute(f"""SELECT mkp_order_id, odoo_so_name FROM finance.mkp_billing_prod
                           WHERE marketplace = %s AND status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')
                             AND mkp_order_id IN ({','.join(['%s'] * len(ch))})""", (MARKETPLACE, *ch))
        invoiced.update({str(o): so for o, so in cursor.fetchall()})
    old_limit = datetime.now() - timedelta(days=SIN_COBRO_DIAS)
    b_pays, b_regs, b_ncs = load_order_data(cursor, set(invoiced))
    selected = []
    for oid, pid, amt, ref, dap in pending:
        oid = str(oid)
        if oid not in invoiced:
            continue
        if b_regs[oid]:
            cat = "B1 SEGUNDO PAGO (la orden ya tiene otro cobro registrado)"
        elif dap and dap < old_limit:
            cat = f"B2 ORDEN SIN NINGÚN COBRO (pago aprobado hace más de {SIN_COBRO_DIAS} días)"
        else:
            continue              # flujo normal: el 03 lo toma cuando MeLi libere el dinero
        selected.append((cat, oid, str(pid), float(amt or 0), float(ref or 0), dap))
    inv_map = invoices_by_so({invoiced[s[1]] for s in selected if invoiced[s[1]]})
    rows_b = []
    for cat, oid, pid, amt, ref, dap in selected:
        so = invoiced[oid] or ""
        invs = inv_map.get(so, [])
        summary[("B pagos por aplicar", cat, month(dap))][0] += 1
        summary[("B pagos por aplicar", cat, month(dap))][1] += amt
        r_txt, n_txt = describe(b_regs[oid], b_ncs[oid])
        rows_b.append({"Categoría": cat, "Mes aprobación": month(dap), "Orden": oid, "SO": so,
                       "Factura": "; ".join(i["name"] for i in invs) or "-",
                       "Estado de cobro factura": "; ".join(i["payment_state"] for i in invs) or "-",
                       "Saldo factura": "; ".join(money(i["amount_residual"]) for i in invs) or "-",
                       "Pago (payment_id)": pid, "Monto pago": money(amt), "Reembolsado": money(ref),
                       "Fecha aprobación": dap.strftime("%Y-%m-%d %H:%M") if dap else "",
                       "Cobros registrados (03/04)": r_txt, "NC de MeLi (05/06)": n_txt})
    db.close()

    # ── Salida ─────────────────────────────────────────────────────
    os.makedirs(OUT_DIR, exist_ok=True)
    files = {f"Cobros_parciales_ML_{today}.csv": rows_a, f"Pagos_por_aplicar_ML_{today}.csv": rows_b}
    for name, rows in files.items():
        with open(os.path.join(OUT_DIR, name), "w", newline="", encoding="utf-8-sig") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    with open(os.path.join(OUT_DIR, f"Resumen_cobros_ML_{today}.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Sección", "Categoría", "Mes", "Cantidad", "Importe"])
        for (sec, cat, mes), (n, amt) in sorted(summary.items()):
            w.writerow([sec, cat, mes, n, money(amt)])

    log.info("=== Resumen (cantidad | importe) ===")
    totals = defaultdict(lambda: [0, 0.0])
    for (sec, cat, mes), (n, amt) in summary.items():
        totals[(sec, cat)][0] += n
        totals[(sec, cat)][1] += amt
    for (sec, cat), (n, amt) in sorted(totals.items()):
        log.info(f"  [{sec}] {cat}: {n} | ${amt:,.2f}")
    log.info(f"Archivos en {OUT_DIR}: {', '.join(list(files) + [f'Resumen_cobros_ML_{today}.csv'])}")
    log.info("=== Fin: no se escribió nada en Odoo ni en MySQL ===")


if __name__ == "__main__":
    main()
