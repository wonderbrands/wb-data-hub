# ── Corrección del CFDI de una factura de MeLi cargada con la NC (SOLO STAGING por ahora) ──
# Caso: el 01 guardó como FACTURA el XML de la NC (TipoDeComprobante 'E', serie MELINC) porque no
# validaba el tipo (corregido en la rama fix/ml-billing-payments-validaciones). Son 10 órdenes
# canceladas (hoja de ruta de Finanzas, punto 12). Este script deja la factura de Odoo con su
# CFDI de ingreso real; la NC se registra después con el 06 (sale de UUID_MISMATCH).
# Las 16 sustituciones a RFC NO van por aquí: Finanzas pidió conservar todo el historial (18-nov).
# Por eso hay un candado: solo corrige si el CFDI que hoy tiene la factura es tipo 'E'.
#
# Por documento (probado en staging el 17-nov):
#   borrador -> el XML anterior se renombra REEMPLAZADO_<uuid>.xml (se conserva) -> se adjunta el
#   XML de ingreso -> diario MELI con nombre serie-folio y el UUID nuevo -> publicar -> volver a
#   conciliar cobro / salida -> si Odoo recalculó el costo, asiento MISC 'MLNC-COSTO:<id>' que lo
#   regresa al de la factura original -> nota en el chatter.
# Controles: asiento igual (con el ajuste), conciliaciones y cobro iguales, UUID nuevo; resumen de
# MLNC-COSTO (cuántos y por cuánto) al final.
#
# Entrada: CASES_FILE = CSV con columnas orden,uuid_nuevo; XML_DIR = carpeta con <uuid_nuevo>.xml
# (la API de MeLi los entrega con transaction_type=sale). DRY_RUN=True por defecto: solo valida.
# CANDADO: solo corre contra ODOO_TEST_URL (staging).
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
DRY_RUN    = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "si", "sí", "yes")
CASES_FILE = os.getenv("CASES_FILE", "")      # CSV: orden,uuid_nuevo
XML_DIR    = os.getenv("XML_DIR", "")         # carpeta con <uuid_nuevo>.xml
# ══════════════════════════════════════════════════════════════

COST_ACCOUNTS = ("205.06.003", "107.06.001", "501.01.001")
TOTAL_TOLERANCE = 0.0101          # ±$0.01 heredado de origen: se cierra contra 704.23.001 en el cobro
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas")

ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
if ".dev.odoo.com" not in ODOO_URL and "test" not in ODOO_DB:
    raise SystemExit(f"CANDADO: este script solo corre en staging (ODOO_TEST_URL); recibido '{ODOO_URL}'")
if not CASES_FILE or not XML_DIR:
    raise SystemExit("Faltan CASES_FILE (CSV orden,uuid_nuevo) y XML_DIR (carpeta con <uuid_nuevo>.xml)")

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
    mv = sr("account.move", [("id", "=", mid)], ["name", "journal_id", "payment_state", "amount_total",
                                                 "l10n_mx_edi_cfdi_uuid", "date"])[0]
    bal, rec = {}, {}
    for line in sr("account.move.line", [("move_id", "=", mid)], ["account_id", "debit", "credit",
                                                                   "matched_debit_ids", "matched_credit_ids"]):
        code = line["account_id"][1].split(" ")[0]
        bal[code] = round(bal.get(code, 0) + line["debit"] - line["credit"], 2)
        rec[code] = rec.get(code, 0) + len(line["matched_debit_ids"]) + len(line["matched_credit_ids"])
    return mv, {k: v for k, v in bal.items() if abs(v) > 0.004}, rec


def counterparts(mid):
    out = {}
    for line in sr("account.move.line", [("move_id", "=", mid)], ["account_id", "matched_debit_ids", "matched_credit_ids"]):
        parts = sr("account.partial.reconcile", [("id", "in", line["matched_debit_ids"] + line["matched_credit_ids"])],
                   ["debit_move_id", "credit_move_id"])
        other = {p[k][0] for p in parts for k in ("debit_move_id", "credit_move_id")} - {line["id"]}
        if other:
            out.setdefault(line["account_id"][0], set()).update(other)
    return out


def check_case(oid, new_uuid):
    """Valida sin escribir. Devuelve (datos, None) o (None, motivo)."""
    so = sr("sale.order", [("channel_order_reference", "=", oid)], ["name"])
    if len(so) != 1:
        return None, f"{len(so)} SO con la orden {oid}"
    inv = sr("account.move", [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("invoice_origin", "=", so[0]["name"])],
             ["id", "name", "l10n_mx_edi_cfdi_uuid", "amount_total"])
    if len(inv) != 1:
        return None, f"{len(inv)} facturas publicadas de {so[0]['name']}"
    inv = inv[0]
    old_uuid = (inv["l10n_mx_edi_cfdi_uuid"] or "").upper()
    old_att = sr("ir.attachment", [("res_model", "=", "account.move"), ("res_id", "=", inv["id"]),
                                   ("name", "=ilike", f"{old_uuid}.xml")], ["id", "name", "datas"])
    if not old_att:
        return None, f"{inv['name']} no tiene el XML de su UUID {old_uuid}"
    if ET.fromstring(base64.b64decode(old_att[0]["datas"])).get("TipoDeComprobante") != "E":
        return None, f"{inv['name']}: su CFDI actual no es una NC (tipo E); este script no aplica"
    path = os.path.join(XML_DIR, f"{new_uuid}.xml")
    if not os.path.exists(path):
        return None, f"falta {path}"
    xml_b = open(path, "rb").read()
    root = ET.fromstring(xml_b)
    if root.get("TipoDeComprobante") != "I":
        return None, f"el XML nuevo {new_uuid} no es factura (tipo {root.get('TipoDeComprobante')})"
    if odoo("account.move", "search_count", [("l10n_mx_edi_cfdi_uuid", "=ilike", new_uuid)]):
        return None, f"el UUID nuevo {new_uuid} ya está en Odoo"
    if abs(float(root.get("Total")) - inv["amount_total"]) > TOTAL_TOLERANCE:
        return None, f"total distinto: XML {root.get('Total')} vs Odoo {inv['amount_total']}"
    return {"inv": inv, "old_uuid": old_uuid, "old_att": old_att[0], "xml_b": xml_b,
            "serie_folio": f"{root.get('Serie')}-{root.get('Folio')}"}, None


def fix_case(oid, new_uuid, data, journals):
    mid = data["inv"]["id"]
    before = snapshot(mid)
    cps = counterparts(mid)
    odoo("account.move", "button_draft", [mid])
    odoo("ir.attachment", "write", [data["old_att"]["id"]], {"name": f"REEMPLAZADO_{data['old_att']['name']}"})
    odoo("ir.attachment", "create", {"name": f"{new_uuid}.xml", "datas": base64.b64encode(data["xml_b"]).decode(),
                                     "res_model": "account.move", "res_id": mid})
    odoo("account.move", "write", [mid], {"name": "/"})
    odoo("account.move", "write", [mid], {"journal_id": journals["MELI"], "name": data["serie_folio"],
                                          "l10n_mx_edi_cfdi_uuid": new_uuid, "narration": new_uuid})
    odoo("account.move", "action_post", [mid])
    for acc, lines in cps.items():
        mine = odoo("account.move.line", "search", [("move_id", "=", mid), ("account_id", "=", acc), ("reconciled", "=", False)])
        others = odoo("account.move.line", "search", [("id", "in", list(lines)), ("reconciled", "=", False)])
        if mine and others:
            odoo("account.move.line", "reconcile", mine + others)
    odoo("account.move", "message_post", [mid],
         body=(f"CFDI corregido (NC cargada como factura): {data['old_uuid']} -> {new_uuid} ({data['serie_folio']}). "
               f"Nombre anterior {before[0]['name']}. El XML anterior se conserva como REEMPLAZADO_."),
         message_type="comment", subtype_xmlid="mail.mt_note")
    after = snapshot(mid)
    adj = {k: round(after[1].get(k, 0) - before[1].get(k, 0), 2) for k in COST_ACCOUNTS}
    adj = {k: v for k, v in adj.items() if abs(v) > 0.004}
    adj_name = ""
    if adj:
        ref = f"MLNC-COSTO:{mid}"
        found = sr("account.move", [("ref", "=", ref), ("state", "=", "posted")], ["name"])
        if found:
            adj_name = found[0]["name"]
        else:
            acc = {a["code"]: a["id"] for a in sr("account.account", [("code", "in", list(adj))], ["code"])}
            aid = odoo("account.move", "create", {"move_type": "entry", "journal_id": journals["MISC"], "date": after[0]["date"],
                                                  "ref": ref, "line_ids": [(0, 0, {"account_id": acc[k], "debit": max(-v, 0),
                                                                                   "credit": max(v, 0),
                                                                                   "name": f"Ajuste costo a la factura original {data['serie_folio']}"})
                                                                           for k, v in adj.items()]})
            odoo("account.move", "action_post", [aid])
            adj_name = sr("account.move", [("id", "=", aid)], ["name"])[0]["name"]
    final = {k: round(after[1].get(k, 0) - adj.get(k, 0), 2) for k in set(after[1]) | set(adj)}
    final = {k: v for k, v in final.items() if abs(v) > 0.004}
    ok = (final == before[1] and after[2] == before[2] and after[0]["payment_state"] == before[0]["payment_state"]
          and (after[0]["l10n_mx_edi_cfdi_uuid"] or "").upper() == new_uuid)
    return {"Orden": oid, "Nombre anterior": before[0]["name"], "Nombre nuevo": after[0]["name"],
            "UUID anterior (NC)": data["old_uuid"], "UUID nuevo (factura)": new_uuid,
            "Cobro antes→después": f"{before[0]['payment_state']}→{after[0]['payment_state']}",
            "Conciliaciones antes→después": f"{sum(before[2].values())}→{sum(after[2].values())}",
            "Ajuste de costo": f"{adj_name} {adj}" if adj_name else "-", "Resultado": "OK" if ok else "REVISAR"}, (adj_name, adj)


def main():
    cases = [(r[0].strip(), r[1].strip().upper()) for r in csv.reader(open(CASES_FILE, encoding="utf-8-sig"))
             if r and r[0].strip().isdigit()]
    journals = {c: odoo("account.journal", "search", [("code", "=", c)])[0] for c in ("MELI", "MISC")}
    log.info(f"=== Corrección de CFDI | {'EN SECO' if DRY_RUN else 'REAL'} | {ODOO_URL} | casos: {len(cases)} ===")
    rows, adjs = [], []
    for oid, new_uuid in cases:
        data, reason = check_case(oid, new_uuid)
        if reason:
            rows.append({"Orden": oid, "Resultado": f"NO SE CORRIGE: {reason}"})
        elif DRY_RUN:
            rows.append({"Orden": oid, "Nombre anterior": data["inv"]["name"], "Nombre nuevo": data["serie_folio"],
                         "UUID anterior (NC)": data["old_uuid"], "UUID nuevo (factura)": new_uuid, "Resultado": "EN SECO: OK"})
        else:
            row, adj = fix_case(oid, new_uuid, data, journals)
            rows.append(row)
            if adj[0]:
                adjs.append(adj)
        log.info(f"{oid}: {rows[-1]['Resultado']} {rows[-1].get('Ajuste de costo', '')}")
    fields = ["Orden", "Nombre anterior", "Nombre nuevo", "UUID anterior (NC)", "UUID nuevo (factura)",
              "Cobro antes→después", "Conciliaciones antes→después", "Ajuste de costo", "Resultado"]
    out = os.path.join(OUT_DIR, f"Correccion_CFDI_{'seco_' if DRY_RUN else ''}{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
        f.write(f"\nAjustes MLNC-COSTO,{len(adjs)}," + ",".join(
            f"{k} {sum(a[1].get(k, 0) for a in adjs):.2f}" for k in COST_ACCOUNTS) + "\n")
        for name, adj in adjs:
            f.write(f",{name},{json.dumps(adj)}\n")
    log.info(f"=== Ajustes MLNC-COSTO: {len(adjs)} | " +
             " | ".join(f"{k} {sum(a[1].get(k, 0) for a in adjs):,.2f}" for k in COST_ACCOUNTS) + f" | archivo: {out} ===")


if __name__ == "__main__":
    main()
