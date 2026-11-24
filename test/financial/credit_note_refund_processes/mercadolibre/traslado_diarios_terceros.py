# ── Traslado de CFDI de terceros a sus diarios (SOLO STAGING por ahora) ─────────
# Regla de Finanzas: un CFDI de terceros nunca vive en un diario de Somos Reyes. Este script
# mueve facturas y NC de MeLi YA PUBLICADAS a los diarios de terceros con nombre = serie-folio:
#   factura (out_invoice, Serie MELI*)  -> diario MELI   (ej. INV/2026/254566 -> MELI-188350)
#   NC      (out_refund,  Serie MELINC) -> diario MLNC   (ej. RDEV/2026/01016 -> MELINC-11087)
# Procedimiento por documento (probado en staging el 17-nov, 23 documentos):
#   borrador -> nombre '/' -> diario y serie-folio -> se restituye la posición fiscal -> publicar
#   -> volver a conciliar lo que estaba conciliado (cobro, NC, 205.06.003, 107.06.001)
#   -> si Odoo recalculó el costo (orden sin salida: toma el del día), un asiento MISC con ref
#      'MLNC-COSTO:<id>' lo regresa al costo original. Así los totales por cuenta no cambian.
# Controles por lote (los que pidió Finanzas): totales por cuenta antes/después, conciliaciones,
# UUID, cobro, nombre = serie-folio, lista de lo que no cuadre y cuántos MLNC-COSTO y por cuánto.
# Funciona porque son CFDI de terceros que Odoo no timbró. Un CFDI timbrado por nosotros NUNCA
# se regresa a borrador: el filtro de serie lo deja fuera.
#
# CANDADO: solo corre contra ODOO_TEST_URL (staging). Para el histórico de productivo se quita
# a propósito, con la ventana de Finanzas (jun–sep con la reapertura).
# DRY_RUN=True (por defecto): solo lista qué movería y con qué nombre. No escribe nada.
import os
import sys
import csv
import json
import time
import base64
import logging
import xmlrpc.client
import xml.etree.ElementTree as ET
from datetime import datetime

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
DRY_RUN        = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "si", "sí", "yes")
# Lote: por ids explícitos (coma) o por filtro de diario de origen + fechas + tope
MOVE_IDS       = os.getenv("MOVE_IDS", "")
FROM_JOURNALS  = os.getenv("FROM_JOURNALS", "INV,DEV,BON")      # códigos de los diarios de Somos Reyes
DATE_FROM      = os.getenv("DATE_FROM", "2026-11-01")
DATE_TO        = os.getenv("DATE_TO", "2026-11-30")
BATCH_LIMIT    = int(os.getenv("BATCH_LIMIT", "50"))
# ══════════════════════════════════════════════════════════════

DEST = {"out_invoice": ("MELI", ("MELI",)), "out_refund": ("MLNC", ("MELINC",))}   # diario, series permitidas
ACCOUNTS = ["401.01.001", "402.01.001", "402.01.002", "402.01.003", "208.01.001", "105.01.004",
            "501.01.001", "205.06.003", "107.06.001"]
COST_ACCOUNTS = ("205.06.003", "107.06.001", "501.01.001")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas")

ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
if ".dev.odoo.com" not in ODOO_URL and "test" not in ODOO_DB:
    raise SystemExit(f"CANDADO: este script solo corre en staging (ODOO_TEST_URL); recibido '{ODOO_URL}'")

_common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
UID = _common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not UID:
    raise SystemExit(f"Odoo rechazó la autenticación ({ODOO_URL}, {ODOO_DB})")
_models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)


def odoo(model, method, *args, **kwargs):
    """Solo se reintentan lecturas. 'cannot marshal None' = el método se ejecutó y no devuelve valor."""
    read = method in ("search_read", "read", "search", "search_count")
    for attempt in range(1, 4):
        try:
            return _models.execute_kw(ODOO_DB, UID, ODOO_PASSWORD, model, method, list(args), kwargs)
        except xmlrpc.client.Fault as e:
            if "cannot marshal None" in str(e):
                return None
            raise
        except (xmlrpc.client.ProtocolError, OSError):
            if not read or attempt == 3:
                raise
            time.sleep(3 * attempt)


def sr(model, domain, fields, **kw):
    return odoo(model, "search_read", domain, fields=fields, **kw)


def snapshot(mid):
    mv = sr("account.move", [("id", "=", mid)], ["name", "journal_id", "payment_state", "l10n_mx_edi_cfdi_uuid",
                                                 "date", "move_type", "fiscal_position_id"])[0]
    bal, rec = {}, {}
    for line in sr("account.move.line", [("move_id", "=", mid)], ["account_id", "debit", "credit",
                                                                   "matched_debit_ids", "matched_credit_ids"]):
        code = line["account_id"][1].split(" ")[0]
        bal[code] = round(bal.get(code, 0) + line["debit"] - line["credit"], 2)
        rec[code] = rec.get(code, 0) + len(line["matched_debit_ids"]) + len(line["matched_credit_ids"])
    return mv, {k: v for k, v in bal.items() if abs(v) > 0.004}, rec


def counterparts(mid):
    """Líneas de OTROS asientos conciliadas con este documento, por cuenta."""
    out = {}
    for line in sr("account.move.line", [("move_id", "=", mid)], ["account_id", "matched_debit_ids", "matched_credit_ids"]):
        parts = sr("account.partial.reconcile", [("id", "in", line["matched_debit_ids"] + line["matched_credit_ids"])],
                   ["debit_move_id", "credit_move_id"])
        other = {p[k][0] for p in parts for k in ("debit_move_id", "credit_move_id")} - {line["id"]}
        if other:
            out.setdefault(line["account_id"][0], set()).update(other)
    return out


def serie_folio(mid, uuid):
    att = sr("ir.attachment", [("res_model", "=", "account.move"), ("res_id", "=", mid), ("name", "=ilike", f"{uuid}.xml")],
             ["datas"])
    if not att:
        return None, None
    root = ET.fromstring(base64.b64decode(att[0]["datas"]))
    return root.get("Serie") or "", root.get("Folio") or ""


def select_batch(journals):
    if MOVE_IDS:
        return [int(x) for x in MOVE_IDS.split(",") if x.strip()]
    domain = [("journal_id", "in", list(journals.values())), ("move_type", "in", list(DEST)), ("state", "=", "posted"),
              ("date", ">=", DATE_FROM), ("date", "<=", DATE_TO), ("l10n_mx_edi_cfdi_uuid", "!=", False)]
    return [m["id"] for m in sr("account.move", domain, ["id"], limit=BATCH_LIMIT, order="date asc, id asc")]


def cost_adjustment(mid, date_, new_name, adj, misc_journal):
    """Asiento MISC que regresa el costo recalculado al de la factura original (idempotente por ref)."""
    ref = f"MLNC-COSTO:{mid}"
    found = sr("account.move", [("ref", "=", ref), ("state", "=", "posted")], ["name"])
    if found:
        return found[0]["name"]
    acc = {a["code"]: a["id"] for a in sr("account.account", [("code", "in", list(adj))], ["code"])}
    aid = odoo("account.move", "create", {"move_type": "entry", "journal_id": misc_journal, "date": date_, "ref": ref,
                                          "line_ids": [(0, 0, {"account_id": acc[k], "debit": max(-v, 0), "credit": max(v, 0),
                                                               "name": f"Ajuste costo a la factura original {new_name}"})
                                                       for k, v in adj.items()]})
    odoo("account.move", "action_post", [aid])
    return sr("account.move", [("id", "=", aid)], ["name"])[0]["name"]


def main():
    journals = {c: odoo("account.journal", "search", [("code", "=", c)])[0] for c in FROM_JOURNALS.split(",")}
    dest_ids = {c: odoo("account.journal", "search", [("code", "=", c)])[0] for c in ("MELI", "MLNC", "MISC")}
    ids = select_batch(journals)
    log.info(f"=== Traslado a diarios de terceros | {'EN SECO' if DRY_RUN else 'REAL'} | {ODOO_URL} | lote: {len(ids)} ===")
    tot_before = {a: 0.0 for a in ACCOUNTS}
    tot_after = {a: 0.0 for a in ACCOUNTS}
    rows, problems, cost_adjs = [], [], []
    for mid in ids:
        before = snapshot(mid)
        mv = before[0]
        uuid = mv["l10n_mx_edi_cfdi_uuid"]
        dest_code, series = DEST[mv["move_type"]]
        serie, folio = serie_folio(mid, uuid)
        new_name = f"{serie}-{folio}"
        if serie is None or not any(serie.upper().startswith(s) for s in series):
            problems.append((mv["name"], f"Serie '{serie}' no es de MeLi o falta el XML: no se mueve"))
            log.warning(f"{mv['name']}: serie '{serie}' no es de MeLi o falta el XML -> se omite")
            continue
        if DRY_RUN:
            rows.append({"Nombre anterior": mv["name"], "Nombre nuevo": new_name, "Diario nuevo": dest_code, "UUID": uuid,
                         "Fecha": mv["date"], "Cobro antes→después": mv["payment_state"], "Conciliaciones antes→después": "",
                         "Ajuste de costo": "", "Resultado": "EN SECO"})
            continue
        cps = counterparts(mid)
        fp = mv["fiscal_position_id"]
        odoo("account.move", "button_draft", [mid])
        odoo("account.move", "write", [mid], {"name": "/"})
        odoo("account.move", "write", [mid], {"journal_id": dest_ids[dest_code], "name": new_name})
        # Cambiar de diario borra la posición fiscal: sin ella la NC tipo B mandaría el costo a 205.06.003
        odoo("account.move", "write", [mid], {"fiscal_position_id": fp[0] if fp else False})
        odoo("account.move", "action_post", [mid])
        for acc, lines in cps.items():
            mine = odoo("account.move.line", "search", [("move_id", "=", mid), ("account_id", "=", acc), ("reconciled", "=", False)])
            others = odoo("account.move.line", "search", [("id", "in", list(lines)), ("reconciled", "=", False)])
            if mine and others:
                odoo("account.move.line", "reconcile", mine + others)
        after = snapshot(mid)
        adj = {k: round(after[1].get(k, 0) - before[1].get(k, 0), 2) for k in COST_ACCOUNTS}
        adj = {k: v for k, v in adj.items() if abs(v) > 0.004}
        adj_name = cost_adjustment(mid, after[0]["date"], new_name, adj, dest_ids["MISC"]) if adj else ""
        if adj:
            cost_adjs.append((adj_name, mv["name"], adj))
        final = {k: round(after[1].get(k, 0) - adj.get(k, 0), 2) for k in set(after[1]) | set(adj)}
        final = {k: v for k, v in final.items() if abs(v) > 0.004}
        for a in ACCOUNTS:
            tot_before[a] += before[1].get(a, 0)
            tot_after[a] += final.get(a, 0)
        checks = {"mismo UUID": after[0]["l10n_mx_edi_cfdi_uuid"] == uuid, "asiento igual": final == before[1],
                  "conciliaciones iguales": after[2] == before[2], "cobro igual": after[0]["payment_state"] == mv["payment_state"],
                  "nombre = serie-folio": after[0]["name"] == new_name}
        bad = [k for k, v in checks.items() if not v]
        if bad:
            problems.append((mv["name"], ", ".join(bad)))
        rows.append({"Nombre anterior": mv["name"], "Nombre nuevo": after[0]["name"], "Diario nuevo": after[0]["journal_id"][1],
                     "UUID": uuid, "Fecha": after[0]["date"],
                     "Cobro antes→después": f"{mv['payment_state']}→{after[0]['payment_state']}",
                     "Conciliaciones antes→después": f"{sum(before[2].values())}→{sum(after[2].values())}",
                     "Ajuste de costo": f"{adj_name} {adj}" if adj_name else "-",
                     "Resultado": "OK" if not bad else "REVISAR: " + ", ".join(bad)})
        log.info(f"{mv['name']} -> {after[0]['name']} | {rows[-1]['Resultado']} | ajuste: {rows[-1]['Ajuste de costo']}")

    # ── Controles del lote ──
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = os.path.join(OUT_DIR, f"Traslado_lote_{'seco_' if DRY_RUN else ''}{stamp}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if not DRY_RUN:
            w.writerow(["Totales por cuenta del lote", f"{len(rows)} documentos", "", ""])
            w.writerow(["Cuenta", "Antes", "Después", "Diferencia"])
            for a in ACCOUNTS:
                w.writerow([a, f"{tot_before[a]:.2f}", f"{tot_after[a]:.2f}", f"{tot_after[a] - tot_before[a]:.2f}"])
            w.writerow([])
            w.writerow(["Ajustes MLNC-COSTO", len(cost_adjs)] + [f"{k} {sum(a[2].get(k, 0) for a in cost_adjs):.2f}"
                                                                   for k in COST_ACCOUNTS])
            for name, doc, adj in cost_adjs:
                w.writerow(["", name, doc, json.dumps(adj)])
            w.writerow([])
        if rows:
            w.writerow(list(rows[0]))
            for r in rows:
                w.writerow(list(r.values()))
        w.writerow([])
        w.writerow(["No se movió / no cuadra", "Motivo"])
        for p in problems:
            w.writerow(list(p))
    if not DRY_RUN:
        log.info("=== Totales por cuenta (antes | después | diferencia) ===")
        for a in ACCOUNTS:
            log.info(f"  {a}: {tot_before[a]:,.2f} | {tot_after[a]:,.2f} | {tot_after[a] - tot_before[a]:,.2f}")
        log.info(f"=== Ajustes MLNC-COSTO: {len(cost_adjs)} | " +
                 " | ".join(f"{k} {sum(a[2].get(k, 0) for a in cost_adjs):,.2f}" for k in COST_ACCOUNTS) + " ===")
    log.info(f"=== Documentos: {len(rows)} | no se movieron o no cuadran: {len(problems)} | archivo: {out} ===")


if __name__ == "__main__":
    main()
