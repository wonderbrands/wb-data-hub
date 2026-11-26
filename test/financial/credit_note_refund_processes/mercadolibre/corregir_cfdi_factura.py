# ── Casos puntuales de CFDI de facturas de MeLi en Odoo (SOLO STAGING por ahora) ──
# Dos modos (MODO):
#
# "nc_como_factura" (por defecto): el 01 guardó como FACTURA el XML de la NC (TipoDeComprobante 'E',
#   serie MELINC) porque no validaba el tipo (corregido en el 01). Son 10 órdenes canceladas (hoja de
#   ruta de Finanzas, punto 12). Deja la factura de Odoo con su CFDI de ingreso real; la NC se registra
#   después con el 06 (sale de NC_AS_INVOICE / UUID_MISMATCH).
#   Candado: solo corrige si el CFDI que hoy tiene la factura es tipo 'E'.
#   Por documento: borrador -> el XML anterior se renombra REEMPLAZADO_<uuid>.xml (se conserva) -> se
#   adjunta el XML de ingreso -> diario MELI con nombre serie-folio y el UUID nuevo -> se restituyen el
#   costo y el IVA ORIGINALES y se publica sin que Odoo recalcule (contexto move_reverse_cancel, como el
#   traslado; ya no hay ajustes MLNC-COSTO) -> volver a conciliar cobro / salida -> nota en el chatter.
#
# "refactura" (Finanzas 18-nov, filas 10 y 27): la NC de MeLi relaciona una primera factura (F1) que no
#   está en Odoo; la vigente en Odoo es otra. Esquema aprobado: la factura de Odoo se queda como la venta
#   real (401.01.001 + costo); la F1 entra al diario MELI con TODO su ingreso en 401.01.006 REFACTURACIONES,
#   sin costo y sin ligarse a la SO; después el 06 registra la NC contra la F1 como tipo R (401.01.006, sin
#   costo, conciliada con la F1). La F1 no la expone la API de MeLi: XML del SAT.
#   Las sustituciones (relación 04) NO van por aquí: las registra el 06 con la reconsulta (fila 7).
#
# Controles: asiento igual (nc_como_factura) o F1 = XML sin costo (refactura), conciliaciones y cobro.
# Entrada: CASES_FILE = CSV orden,uuid ; XML_DIR = carpeta con <uuid>.xml. DRY_RUN=True por defecto.
# CANDADO: solo corre contra ODOO_TEST_URL (staging).
import os
import sys
import csv
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
MODO       = os.getenv("MODO", "nc_como_factura").strip().lower()   # nc_como_factura | refactura
CASES_FILE = os.getenv("CASES_FILE", "")      # CSV: orden,uuid (uuid nuevo de la factura o de la F1)
XML_DIR    = os.getenv("XML_DIR", "")         # carpeta con <uuid>.xml
# ══════════════════════════════════════════════════════════════

NS = {'cfdi': 'http://www.sat.gob.mx/cfd/4', 'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'}
COST_ACCOUNTS = ("205.06.003", "107.06.001", "501.01.001")
TOTAL_TOLERANCE = 0.0101          # ±$0.01 heredado de origen: se cierra contra 704.23.001 en el cobro
ACCOUNT_REFACTURA = os.getenv("ACCOUNT_CODE_R", "401.01.006")
PARTNER_ID_PUBLICO_GENERAL = int(os.getenv("PARTNER_ID_PUBLICO_GENERAL", "13436"))
SERIE_JOURNAL = {"MELI": "MELI", "MELIOF": "MLOF"}   # serie EXACTA del CFDI de ingreso -> diario de terceros
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas")

if MODO not in ("nc_como_factura", "refactura"):
    raise SystemExit(f"MODO '{MODO}' inválido (nc_como_factura | refactura)")
os.makedirs(OUT_DIR, exist_ok=True)   # finanzas/ no está en el repo
ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
if ".dev.odoo.com" not in ODOO_URL and "test" not in ODOO_DB:
    raise SystemExit(f"CANDADO: este script solo corre en staging (ODOO_TEST_URL); recibido '{ODOO_URL}'")
if not CASES_FILE or not XML_DIR:
    raise SystemExit("Faltan CASES_FILE (CSV orden,uuid) y XML_DIR (carpeta con <uuid>.xml)")

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


COGS_FIELDS = ["account_id", "partner_id", "product_id", "name", "quantity", "price_unit", "debit", "credit"]


def cogs_lines(mid):
    """Líneas de costo tal como están publicadas, para restituirlas sin que Odoo las recalcule."""
    out = []
    for ln in sr("account.move.line", [("move_id", "=", mid), ("display_type", "=", "cogs")], COGS_FIELDS):
        vals = {k: (ln[k][0] if isinstance(ln[k], list) else ln[k]) for k in COGS_FIELDS}
        vals["display_type"] = "cogs"
        out.append(vals)
    return out


def set_tax(mid, target):
    """IVA del documento = target, con el mismo mecanismo que la pantalla (tax_totals), como el 06."""
    mv = sr("account.move", [("id", "=", mid)], ["tax_totals", "amount_tax"])[0]
    if target is None or abs(mv["amount_tax"] - target) < 0.005:
        return False
    totals = mv["tax_totals"]
    groups = [g for st in totals.get("subtotals", []) for g in st.get("tax_groups", [])] or \
             [g for v in (totals.get("groups_by_subtotal") or {}).values() for g in v]
    if len(groups) != 1:
        raise RuntimeError(f"{len(groups)} grupos de impuestos: no se puede fijar el IVA")
    key = "tax_amount_currency" if "tax_amount_currency" in groups[0] else "tax_group_amount"
    groups[0][key] = target
    odoo("account.move", "write", [mid], {"tax_totals": totals})
    return True


def read_xml(uuid):
    path = os.path.join(XML_DIR, f"{uuid}.xml")
    if not os.path.exists(path):
        return None, None
    xml_b = open(path, "rb").read()
    return xml_b, ET.fromstring(xml_b)


def lock_date():
    c = sr("res.company", [], ["fiscalyear_lock_date", "tax_lock_date", "sale_lock_date"])[0]
    return max([d for d in (c["fiscalyear_lock_date"], c["tax_lock_date"], c["sale_lock_date"]) if d] or [""])


# ── MODO nc_como_factura ─────────────────────────────────────────────────────

def check_nc_as_invoice(oid, new_uuid):
    """Valida sin escribir. Devuelve (datos, None) o (None, motivo)."""
    so = sr("sale.order", [("channel_order_reference", "=", oid)], ["name"])
    if len(so) != 1:
        return None, f"{len(so)} SO con la orden {oid}"
    inv = sr("account.move", [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("invoice_origin", "=", so[0]["name"])],
             ["id", "name", "l10n_mx_edi_cfdi_uuid", "amount_total", "amount_tax"])
    if len(inv) != 1:
        return None, f"{len(inv)} facturas publicadas de {so[0]['name']}"
    inv = inv[0]
    old_uuid = (inv["l10n_mx_edi_cfdi_uuid"] or "").upper()
    old_att = sr("ir.attachment", [("res_model", "=", "account.move"), ("res_id", "=", inv["id"]),
                                   ("name", "=ilike", f"{old_uuid}.xml")], ["id", "name", "datas"])
    if not old_att:
        return None, f"{inv['name']} no tiene el XML de su UUID {old_uuid}"
    if ET.fromstring(base64.b64decode(old_att[0]["datas"])).get("TipoDeComprobante") != "E":
        return None, f"{inv['name']}: su CFDI actual no es una NC (tipo E); este modo no aplica"
    xml_b, root = read_xml(new_uuid)
    if xml_b is None:
        return None, f"falta {new_uuid}.xml en {XML_DIR}"
    if root.get("TipoDeComprobante") != "I":
        return None, f"el XML nuevo {new_uuid} no es factura (tipo {root.get('TipoDeComprobante')})"
    if odoo("account.move", "search_count", [("l10n_mx_edi_cfdi_uuid", "=ilike", new_uuid)]):
        return None, f"el UUID nuevo {new_uuid} ya está en Odoo"
    if abs(float(root.get("Total")) - inv["amount_total"]) > TOTAL_TOLERANCE:
        return None, f"total distinto: XML {root.get('Total')} vs Odoo {inv['amount_total']}"
    return {"inv": inv, "old_uuid": old_uuid, "old_att": old_att[0], "xml_b": xml_b,
            "serie_folio": f"{root.get('Serie')}-{root.get('Folio')}"}, None


def fix_nc_as_invoice(oid, new_uuid, data, journals):
    mid = data["inv"]["id"]
    before = snapshot(mid)
    cps = counterparts(mid)
    orig_cogs = cogs_lines(mid)
    orig_tax = data["inv"]["amount_tax"]
    odoo("account.move", "button_draft", [mid])
    odoo("ir.attachment", "write", [data["old_att"]["id"]], {"name": f"REEMPLAZADO_{data['old_att']['name']}"})
    odoo("ir.attachment", "create", {"name": f"{new_uuid}.xml", "datas": base64.b64encode(data["xml_b"]).decode(),
                                     "res_model": "account.move", "res_id": mid})
    odoo("account.move", "write", [mid], {"name": "/"})
    odoo("account.move", "write", [mid], {"journal_id": journals["MELI"], "name": data["serie_folio"],
                                          "l10n_mx_edi_cfdi_uuid": new_uuid, "narration": new_uuid})
    # Costo e IVA originales: se vuelven a poner tal cual y se publica sin recalcular (como el traslado)
    stale = odoo("account.move.line", "search", [("move_id", "=", mid), ("display_type", "=", "cogs")])
    if stale:
        odoo("account.move", "write", [mid], {"line_ids": [(2, x) for x in stale]})
    set_tax(mid, orig_tax)
    if orig_cogs:
        odoo("account.move", "write", [mid], {"line_ids": [(0, 0, v) for v in orig_cogs]})
    odoo("account.move", "action_post", [mid], context={"move_reverse_cancel": True})
    for acc, lines in cps.items():
        mine = odoo("account.move.line", "search", [("move_id", "=", mid), ("account_id", "=", acc), ("reconciled", "=", False)])
        others = odoo("account.move.line", "search", [("id", "in", list(lines)), ("reconciled", "=", False)])
        if mine and others:
            odoo("account.move.line", "reconcile", mine + others)
    odoo("account.move", "message_post", [mid],
         body=(f"CFDI corregido (NC cargada como factura): {data['old_uuid']} -> {new_uuid} ({data['serie_folio']}). "
               f"Nombre anterior {before[0]['name']}. El XML anterior se conserva como REEMPLAZADO_. Costo original restituido."),
         message_type="comment", subtype_xmlid="mail.mt_note")
    after = snapshot(mid)
    ok = (after[1] == before[1] and after[2] == before[2] and after[0]["payment_state"] == before[0]["payment_state"]
          and (after[0]["l10n_mx_edi_cfdi_uuid"] or "").upper() == new_uuid)
    cost = "; ".join(f"{k} {before[1].get(k, 0):,.2f}→{after[1].get(k, 0):,.2f}" for k in COST_ACCOUNTS
                     if before[1].get(k, 0) or after[1].get(k, 0)) or "sin costo"
    return {"Orden": oid, "Nombre anterior": before[0]["name"], "Nombre nuevo": after[0]["name"],
            "UUID anterior": data["old_uuid"], "UUID nuevo": new_uuid,
            "Cobro antes→después": f"{before[0]['payment_state']}→{after[0]['payment_state']}",
            "Conciliaciones antes→después": f"{sum(before[2].values())}→{sum(after[2].values())}",
            "Costo antes→después": cost, "Resultado": "OK" if ok else "REVISAR"}


# ── MODO refactura ───────────────────────────────────────────────────────────

def check_refactura(oid, f1_uuid, lock):
    """Valida sin escribir. Devuelve (datos, None) o (None, motivo)."""
    so = sr("sale.order", [("channel_order_reference", "=", oid)], ["name", "team_id"])
    if len(so) != 1:
        return None, f"{len(so)} SO con la orden {oid}"
    invs = sr("account.move", [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("invoice_origin", "=", so[0]["name"])],
              ["id", "name", "l10n_mx_edi_cfdi_uuid", "amount_total", "team_id"])
    if len(invs) != 1:
        return None, f"{len(invs)} facturas publicadas de {so[0]['name']} (se espera solo la vigente)"
    inv = invs[0]
    xml_b, root = read_xml(f1_uuid)
    if xml_b is None:
        return None, f"falta {f1_uuid}.xml en {XML_DIR} (XML del SAT de la F1)"
    tfd = root.find("cfdi:Complemento/tfd:TimbreFiscalDigital", NS)
    if root.get("TipoDeComprobante") != "I" or tfd is None or (tfd.get("UUID") or "").upper() != f1_uuid:
        return None, f"el XML {f1_uuid} no es una factura con ese UUID"
    serie = root.get("Serie") or ""
    if serie not in SERIE_JOURNAL:
        return None, f"serie '{serie}' no es de MeLi"
    if odoo("account.move", "search_count", [("l10n_mx_edi_cfdi_uuid", "=ilike", f1_uuid)]):
        return None, f"la F1 {f1_uuid} ya está en Odoo"
    date = (root.get("Fecha") or "")[:10]
    if lock and date <= lock:
        return None, f"F1 del {date} en periodo cerrado (bloqueo {lock})"
    concepts = root.findall("cfdi:Conceptos/cfdi:Concepto", NS)
    src = sr("account.move.line", [("move_id", "=", inv["id"]), ("display_type", "=", "product")], ["product_id", "tax_ids"])
    if len(concepts) != len(src):
        return None, f"la F1 tiene {len(concepts)} conceptos y {inv['name']} {len(src)} líneas: emparejar a mano"
    imp = root.find("cfdi:Impuestos", NS)
    rec = root.find("cfdi:Receptor", NS)
    return {"inv": inv, "so": so[0], "xml_b": xml_b, "serie": serie, "folio": root.get("Folio"), "date": date,
            "total": float(root.get("Total")), "tax": float(imp.get("TotalImpuestosTrasladados")) if imp is not None else 0.0,
            "rfc": rec.get("Rfc"), "usage": rec.get("UsoCFDI"),
            "lines": [{"product_id": ln["product_id"][0] if ln["product_id"] else False, "tax_ids": [(6, 0, ln["tax_ids"])],
                       "name": f"REFACTURA F1 · {c.get('Descripcion') or ''}"[:250], "quantity": 1,
                       "price_unit": round(float(c.get("Importe")) - float(c.get("Descuento") or 0), 2)}
                      for ln, c in zip(src, concepts)]}, None


def load_refactura(oid, f1_uuid, data, ids):
    inv = data["inv"]
    if data["rfc"] != "XAXX010101000":
        p = sr("res.partner", [("vat", "=", data["rfc"])], ["id"], limit=1)
        if not p:
            raise RuntimeError(f"no existe el cliente {data['rfc']}; darlo de alta antes")
        partner = p[0]["id"]
    else:
        partner = PARTNER_ID_PUBLICO_GENERAL
    name = f"{data['serie']}-{data['folio']}"
    vals = {"move_type": "out_invoice", "journal_id": ids["journal"][data["serie"]], "name": name, "partner_id": partner,
            "team_id": inv["team_id"][0] if inv["team_id"] else False, "invoice_origin": data["so"]["name"],
            "invoice_date": data["date"], "date": data["date"], "invoice_date_due": data["date"], "invoice_payment_term_id": False,
            "payment_reference": f"F1 de refacturación · venta vigente {inv['name']} (UUID {inv['l10n_mx_edi_cfdi_uuid']})",
            # Todo el ingreso a 401.01.006 y SIN sale_line_ids: la SO no se factura dos veces
            "invoice_line_ids": [(0, 0, {**ln, "account_id": ids["acc_r"]}) for ln in data["lines"]]}
    if data["usage"] in ids["usages"]:
        vals["l10n_mx_edi_usage"] = data["usage"]
    mid = odoo("account.move", "create", vals)
    set_tax(mid, data["tax"])
    cxc = sr("account.move.line", [("move_id", "=", inv["id"]), ("account_type", "=", "asset_receivable")], ["account_id"])
    rec_lines = sr("account.move.line", [("move_id", "=", mid), ("account_type", "=", "asset_receivable")], ["id"])
    # El UUID va después del create (en el create Odoo lo recalcula y lo deja vacío; igual que el 02)
    odoo("account.move", "write", [mid], {"l10n_mx_edi_cfdi_uuid": f1_uuid, "narration": f1_uuid,
                                          "line_ids": [(1, ln["id"], {"account_id": cxc[0]["account_id"][0]}) for ln in rec_lines]})
    draft = sr("account.move", [("id", "=", mid)], ["amount_total"])[0]
    if abs(draft["amount_total"] - data["total"]) > 0.005:
        odoo("account.move", "unlink", [mid])
        raise RuntimeError(f"la F1 suma ${draft['amount_total']:,.2f} y el XML ${data['total']:,.2f} (borrador eliminado)")
    att = odoo("ir.attachment", "create", {"name": f"{f1_uuid}.xml", "datas": base64.b64encode(data["xml_b"]).decode(),
                                           "res_model": "account.move", "res_id": mid})
    try:   # aviso EDI 'sent', como el 02 (no bloqueante)
        odoo("account.edi.document", "create", {"move_id": mid, "edi_format_id": 2, "attachment_id": att, "state": "sent"})
    except Exception:
        pass
    odoo("account.move", "action_post", [mid], context={"move_reverse_cancel": True})   # sin costo
    body = (f"F1 de refacturación {name} (UUID {f1_uuid}): la NC de MeLi la relaciona. Ingreso en {ACCOUNT_REFACTURA} "
            f"sin costo (Finanzas 18-nov); la venta vigente es {inv['name']}.")
    for res_id in (mid, inv["id"]):
        odoo("account.move", "message_post", [res_id], body=body, message_type="comment", subtype_xmlid="mail.mt_note")
    after = snapshot(mid)
    ok = (after[0]["l10n_mx_edi_cfdi_uuid"] or "").upper() == f1_uuid and not any(after[1].get(k) for k in COST_ACCOUNTS) \
        and abs(after[1].get(ACCOUNT_REFACTURA, 0) + (data["total"] - data["tax"])) < 0.01
    return {"Orden": oid, "Nombre anterior": inv["name"] + " (vigente, sin cambio)", "Nombre nuevo": after[0]["name"],
            "UUID anterior": inv["l10n_mx_edi_cfdi_uuid"], "UUID nuevo": f1_uuid,
            "Cobro antes→después": f"-→{after[0]['payment_state']}", "Conciliaciones antes→después": "-",
            "Costo antes→después": f"sin costo; {ACCOUNT_REFACTURA} {after[1].get(ACCOUNT_REFACTURA, 0):,.2f}",
            "Resultado": "OK" if ok else "REVISAR"}


def main():
    cases = [(r[0].strip(), r[1].strip().upper()) for r in csv.reader(open(CASES_FILE, encoding="utf-8-sig"))
             if r and r[0].strip().isdigit()]
    journals = {c: odoo("account.journal", "search", [("code", "=", c)])[0] for c in ("MELI",)}
    ids, lock = {}, lock_date()
    if MODO == "refactura":
        acc = odoo("account.account", "search", [("code", "=", ACCOUNT_REFACTURA)], limit=1)
        if not acc:
            raise SystemExit(f"No existe la cuenta {ACCOUNT_REFACTURA}")
        fields = odoo("account.move", "fields_get", [], attributes=["selection"])
        ids = {"acc_r": acc[0], "usages": {k for k, _ in fields["l10n_mx_edi_usage"].get("selection", [])},
               "journal": {s: (odoo("account.journal", "search", [("code", "=", j)], limit=1) or [False])[0]
                           for s, j in SERIE_JOURNAL.items()}}
    log.info(f"=== Casos de CFDI | modo {MODO} | {'EN SECO' if DRY_RUN else 'REAL'} | {ODOO_URL} | casos: {len(cases)} ===")
    rows = []
    for oid, uuid in cases:
        data, reason = check_nc_as_invoice(oid, uuid) if MODO == "nc_como_factura" else check_refactura(oid, uuid, lock)
        if reason:
            rows.append({"Orden": oid, "Resultado": f"NO SE APLICA: {reason}"})
        elif DRY_RUN:
            rows.append({"Orden": oid, "Nombre anterior": data["inv"]["name"], "UUID nuevo": uuid,
                         "Nombre nuevo": data.get("serie_folio") or f"{data['serie']}-{data['folio']}", "Resultado": "EN SECO: OK"})
        else:
            try:
                rows.append(fix_nc_as_invoice(oid, uuid, data, journals) if MODO == "nc_como_factura"
                            else load_refactura(oid, uuid, data, ids))
            except Exception as e:
                rows.append({"Orden": oid, "Resultado": f"ERROR: {str(e)[:300]}"})
        cost = rows[-1].get('Costo antes→después', '').replace('→', ' -> ')   # la consola de Windows no imprime la flecha
        log.info(f"{oid}: {rows[-1]['Resultado']} {rows[-1].get('Nombre nuevo', '')} {cost}")
    fields = ["Orden", "Nombre anterior", "Nombre nuevo", "UUID anterior", "UUID nuevo", "Cobro antes→después",
              "Conciliaciones antes→después", "Costo antes→después", "Resultado"]
    out = os.path.join(OUT_DIR, f"Casos_CFDI_{MODO}_{'seco_' if DRY_RUN else ''}{datetime.now():%Y-%m-%d_%H%M}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log.info(f"=== {len(rows)} casos | OK: {sum(1 for r in rows if r['Resultado'] in ('OK', 'EN SECO: OK'))} | archivo: {out} ===")


if __name__ == "__main__":
    main()
