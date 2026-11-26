# ── Traslado de CFDI de terceros a sus diarios (staging y productivo) ──────────────────────────────
# Regla de Finanzas: un CFDI de terceros nunca vive en un diario de Somos Reyes. Este script
# mueve facturas y NC de MeLi YA PUBLICADAS a los diarios de terceros con nombre = serie-folio:
#   factura (out_invoice, Serie MELI)     -> diario MELI   (ej. INV/2026/254566 -> MELI-188350)
#   NC      (out_refund,  Serie MELINC)   -> diario MLNC   (ej. RDEV/2026/01016 -> MELINC-11087)
#   factura (out_invoice, Serie MELIOF)   -> diario MLOF
#   NC      (out_refund,  Serie MELIOFNC) -> diario MLONC
# Procedimiento por documento (probado en staging el 17-nov y el 25-nov, 436 documentos):
#   borrador -> nombre '/' -> diario y serie-folio -> se restituye la posición fiscal -> publicar
#   -> volver a conciliar lo que estaba conciliado (cobro, NC, 205.06.003, 107.06.001)
#   -> COSTO ORIGINAL (25-nov, pedido de Finanzas): al pasar a borrador Odoo borra las líneas de costo
#      y al publicar las recalcula con el costo del día. Se copian ANTES del borrador, se vuelven a
#      poner tal cual y se publica con contexto move_reverse_cancel (Odoo no recalcula). El costo queda
#      idéntico al original; si algo no cuadra, el documento sale como REVISAR (no se ajusta en automático).
# Controles por lote (los que pidió Finanzas): totales por cuenta antes/después, conciliaciones,
# UUID, cobro, nombre = serie-folio y lista de lo que no cuadre.
# Funciona porque son CFDI de terceros que Odoo no timbró. Un CFDI timbrado por nosotros NUNCA
# se regresa a borrador: solo se toman documentos con el XML del tercero ('<UUID>.xml', como lo cargan
# 02 y 06) y con serie EXACTA de MeLi.
#
# MODO PRODUCTIVO (25-nov; Finanzas aprobó trasladar todo, oct–nov en segundo plano antes de la ventana):
#   LOCAL_ODOO="prod" apunta a odoo_urlV18. En seco (por defecto) no escribe nada. Para escribir:
#   DRY_RUN=false y CONFIRMAR_PROD="TRASLADAR". Candado cruzado: prod con URL de pruebas, o staging
#   con URL productiva, aborta. Pasos, comandos y ventanas: docs/TRASLADO_DIARIOS_TERCEROS_PROD.md
# Candados por documento: periodo cerrado (fecha <= candado de la compañía), nombre serie-folio ya
# ocupado en el diario destino, UUID repetido. Un diario de origen con hash inalterable aborta todo.
# Si un documento falla a la mitad, la línea se detiene y dice en qué paso quedó (puede quedar en borrador).
# HASTA="HH:MM" (hora CDMX): la línea no toma documentos nuevos a partir de esa hora (ventanas sin cobros).
# Para detener antes: Ctrl+C o crear el archivo finanzas/DETENER (detiene todas las líneas). La línea termina el
# documento en curso y se detiene. NUNCA matar el proceso (Stop-Process): puede dejar un documento en borrador.
# Log por documento para Finanzas: finanzas/Traslado_<modo>_<fecha>[_parte].csv, se escribe documento por
# documento (si la corrida se corta, lo hecho queda registrado) + un _resumen.csv al final.
import os
import sys
import csv
import time
import base64
import signal
import logging
import xmlrpc.client
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # consola de Windows (cp1252) y el '→' del log
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

# ══ PRUEBA LOCAL: editar aquí ═════════════════════════════════
LOCAL_ODOO     = os.getenv("LOCAL_ODOO", "staging").strip().lower()   # 'staging' | 'prod'
CONFIRMAR_PROD = os.getenv("CONFIRMAR_PROD", "").strip()              # escribir en prod exige "TRASLADAR"
DRY_RUN        = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "si", "sí", "yes")
# Lote: por ids explícitos (coma) o por filtro de diario de origen + fechas + tope
MOVE_IDS       = os.getenv("MOVE_IDS", "")
FROM_JOURNALS  = os.getenv("FROM_JOURNALS", "INV,DEV,BON")      # códigos de los diarios de Somos Reyes
DATE_FROM      = os.getenv("DATE_FROM", "2026-11-01")
DATE_TO        = os.getenv("DATE_TO", "2026-11-30")
BATCH_LIMIT    = int(os.getenv("BATCH_LIMIT", "50"))            # documentos a trasladar; 0 = sin tope
# Varias líneas en paralelo: PART="i/N" toma solo los documentos con id % N == i (lotes disjuntos)
PART           = os.getenv("PART", "")
HASTA          = os.getenv("HASTA", "").strip()                 # "HH:MM" CDMX; vacío = sin hora de corte
# ══════════════════════════════════════════════════════════════

# Serie EXACTA del CFDI -> (tipo de documento, diario de terceros). Exacta a propósito: "MELIOF" (Oficiales)
# empieza con "MELI" y con un prefijo se iría al diario de la tienda principal.
SERIE_JOURNAL = {"MELI": ("out_invoice", "MELI"), "MELINC": ("out_refund", "MLNC"),
                 "MELIOF": ("out_invoice", "MLOF"), "MELIOFNC": ("out_refund", "MLONC")}
MOVE_TYPES = ["out_invoice", "out_refund"]   # tipos de documento que se trasladan
ACCOUNTS = ["401.01.001", "402.01.001", "402.01.002", "402.01.003", "208.01.001", "105.01.004", "105.01.005",
            "501.01.001", "205.06.003", "107.06.001"]
COST_ACCOUNTS = ("205.06.003", "107.06.001", "501.01.001")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finanzas")
CFDI_NS = "{http://www.sat.gob.mx/cfd/4}"
STOP_FILE = os.path.join(OUT_DIR, "DETENER")

os.makedirs(OUT_DIR, exist_ok=True)   # finanzas/ no está en el repo
if LOCAL_ODOO == "prod":
    ODOO_URL = (os.getenv("odoo_urlV18") or "").strip()
    ODOO_DB  = (os.getenv("odoo_dbV18") or "").strip()
elif LOCAL_ODOO == "staging":
    ODOO_URL = (os.getenv("ODOO_TEST_URL") or os.getenv("odoo_url_testV18") or "").strip()
    ODOO_DB  = (os.getenv("ODOO_TEST_DB") or os.getenv("odoo_db_testV18") or "").strip()
else:
    raise SystemExit(f"LOCAL_ODOO debe ser 'staging' o 'prod'; recibido '{LOCAL_ODOO}'")
ODOO_USER     = os.getenv("ODOO_USER") or os.getenv("odoo_user_dataV18")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or os.getenv("odoo_password_dataV18")
_parece_pruebas = ".dev.odoo.com" in ODOO_URL or "test" in ODOO_DB.lower()
if not ODOO_URL or not ODOO_DB:
    raise SystemExit(f"Falta la URL o la base de Odoo para LOCAL_ODOO='{LOCAL_ODOO}'")
if LOCAL_ODOO == "staging" and not _parece_pruebas:
    raise SystemExit(f"CANDADO: LOCAL_ODOO=staging pero la URL no parece de pruebas: '{ODOO_URL}'")
if LOCAL_ODOO == "prod" and _parece_pruebas:
    raise SystemExit(f"CANDADO: LOCAL_ODOO=prod pero la URL parece de pruebas: '{ODOO_URL}' / '{ODOO_DB}'")
if LOCAL_ODOO == "prod" and not DRY_RUN and CONFIRMAR_PROD != "TRASLADAR":
    raise SystemExit("CANDADO: para escribir en PRODUCTIVO hace falta CONFIRMAR_PROD=\"TRASLADAR\"")

_common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
UID = _common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
if not UID:
    raise SystemExit(f"Odoo rechazó la autenticación ({ODOO_URL}, {ODOO_DB})")
_models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

STOP = {"pedido": False}


def _pedir_alto(signum, frame):
    STOP["pedido"] = True
    log.warning("Ctrl+C: termino el documento en curso y me detengo.")


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


def read_chunks(model, ids, fields, size=2000):
    out = []
    for i in range(0, len(ids), size):
        out += odoo(model, "read", ids[i:i + size], fields=fields)
    return out


def deadline():
    """Hora de corte HASTA (CDMX = UTC-6 fijo) como datetime UTC; si ya pasó hoy, es la de mañana."""
    if not HASTA:
        return None
    h, m = (int(x) for x in HASTA.split(":"))
    now_mx = datetime.now(timezone.utc) - timedelta(hours=6)
    cut = now_mx.replace(hour=h, minute=m, second=0, microsecond=0)
    if cut <= now_mx:
        cut += timedelta(days=1)
    return cut + timedelta(hours=6)


def snapshot(mid):
    mv = sr("account.move", [("id", "=", mid)], ["name", "journal_id", "state", "payment_state", "l10n_mx_edi_cfdi_uuid",
                                                 "date", "move_type", "fiscal_position_id", "amount_tax"])[0]
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


def restore_tax(mid, target):
    """En borrador Odoo recalcula el IVA por línea. Se regresa al IVA objetivo con el mismo mecanismo que la
    pantalla (tax_totals), como el 06 al registrar la NC. Devuelve True si tuvo que ajustar."""
    mv = sr("account.move", [("id", "=", mid)], ["tax_totals", "amount_tax"])[0]
    if target is None or abs(mv["amount_tax"] - target) < 0.005:
        return False
    totals = mv["tax_totals"]
    groups = [g for st in totals.get("subtotals", []) for g in st.get("tax_groups", [])] or \
             [g for v in (totals.get("groups_by_subtotal") or {}).values() for g in v]
    if len(groups) != 1:
        raise RuntimeError(f"{len(groups)} grupos de impuestos: no se puede restituir el IVA")
    key = "tax_amount_currency" if "tax_amount_currency" in groups[0] else "tax_group_amount"
    groups[0][key] = target
    odoo("account.move", "write", [mid], {"tax_totals": totals})
    return True


def third_party_attachments(moves):
    """{move_id: id del adjunto} de los documentos con el XML de un tercero ('<UUID>.xml', como lo cargan
    02 y 06). Los CFDI que timbró Odoo tienen otro nombre de adjunto y quedan fuera desde aquí."""
    by_id = {m["id"]: m for m in moves}
    ids, out = list(by_id), {}
    for i in range(0, len(ids), 1000):
        for a in sr("ir.attachment", [("res_model", "=", "account.move"), ("res_id", "in", ids[i:i + 1000]),
                                      ("name", "=ilike", "%.xml")], ["res_id", "name"]):
            uuid = (by_id[a["res_id"]]["l10n_mx_edi_cfdi_uuid"] or "").lower()
            if uuid and a["name"].lower() == f"{uuid}.xml":
                out[a["res_id"]] = a["id"]
    return out


def read_cfdi(att_ids):
    """{id del adjunto: (serie, folio, IVA del XML)}. XML ilegible -> ('', '', None)."""
    out = {}
    for i in range(0, len(att_ids), 100):
        for a in odoo("ir.attachment", "read", att_ids[i:i + 100], fields=["datas"]):
            try:
                root = ET.fromstring(base64.b64decode(a["datas"] or b""))
                imp = root.find(f"{CFDI_NS}Impuestos")
                tax = imp.get("TotalImpuestosTrasladados") if imp is not None else None
                out[a["id"]] = ((root.get("Serie") or "").strip(), (root.get("Folio") or "").strip(),
                                float(tax) if tax else None)
            except (ET.ParseError, ValueError, TypeError):
                out[a["id"]] = ("", "", None)
    return out


def lock_date(journal_ids):
    """Fecha de candado más alta de la(s) compañía(s) de los diarios de origen."""
    companies = {j["company_id"][0] for j in odoo("account.journal", "read", journal_ids, fields=["company_id"])}
    fields = ["fiscalyear_lock_date", "tax_lock_date", "sale_lock_date", "hard_lock_date"]
    dates = [str(c[k]) for c in odoo("res.company", "read", list(companies), fields=fields) for k in fields if c.get(k)]
    return max(dates) if dates else ""


def candidates(source):
    """Documentos publicados en los diarios de origen con UUID, en orden de fecha. Con MOVE_IDS, esos ids."""
    if MOVE_IDS:
        ids = [int(x) for x in MOVE_IDS.split(",") if x.strip()]
    else:
        domain = [("journal_id", "in", list(source.values())), ("move_type", "in", MOVE_TYPES), ("state", "=", "posted"),
                  ("date", ">=", DATE_FROM), ("date", "<=", DATE_TO), ("l10n_mx_edi_cfdi_uuid", "!=", False)]
        ids = odoo("account.move", "search", domain, order="date asc, id asc")
    pos = {x: k for k, x in enumerate(ids)}
    moves = read_chunks("account.move", ids, ["name", "date", "move_type", "journal_id", "state", "l10n_mx_edi_cfdi_uuid"])
    return sorted(moves, key=lambda m: pos[m["id"]])


COGS_FIELDS = ["account_id", "partner_id", "product_id", "name", "quantity", "price_unit", "debit", "credit"]


def cogs_lines(mid):
    """Líneas de costo (display_type 'cogs') tal como están publicadas, para restituirlas sin recalcular."""
    out = []
    for ln in sr("account.move.line", [("move_id", "=", mid), ("display_type", "=", "cogs")], COGS_FIELDS):
        vals = {k: (ln[k][0] if isinstance(ln[k], list) else ln[k]) for k in COGS_FIELDS}
        vals["display_type"] = "cogs"
        out.append(vals)
    return out


def trasladar(mid, mv, cps, dest_id, new_name, target_tax, orig_cogs, paso):
    """Los pasos que escriben. 'paso' (lista de un elemento) dice dónde quedó si algo falla."""
    paso[0] = "regresar a borrador"
    odoo("account.move", "button_draft", [mid])
    paso[0] = "nombre '/'"
    odoo("account.move", "write", [mid], {"name": "/"})
    paso[0] = "cambiar diario y nombre"
    odoo("account.move", "write", [mid], {"journal_id": dest_id, "name": new_name})
    # Cambiar de diario borra la posición fiscal: sin ella la NC tipo B mandaría el costo a 205.06.003
    paso[0] = "restituir posición fiscal"
    fp = mv["fiscal_position_id"]
    odoo("account.move", "write", [mid], {"fiscal_position_id": fp[0] if fp else False})
    # Costo original: se vuelve a poner tal cual y se publica sin que Odoo lo recalcule
    paso[0] = "restituir costo"
    stale = odoo("account.move.line", "search", [("move_id", "=", mid), ("display_type", "=", "cogs")])
    if stale:   # por si la versión de Odoo no los borró al pasar a borrador
        odoo("account.move", "write", [mid], {"line_ids": [(2, x) for x in stale]})
    paso[0] = "restituir IVA"
    restore_tax(mid, target_tax)
    if orig_cogs:
        paso[0] = "restituir costo"
        odoo("account.move", "write", [mid], {"line_ids": [(0, 0, v) for v in orig_cogs]})
    paso[0] = "publicar"
    odoo("account.move", "action_post", [mid], context={"move_reverse_cancel": True})
    paso[0] = "volver a conciliar"
    for acc, lines in cps.items():
        mine = odoo("account.move.line", "search", [("move_id", "=", mid), ("account_id", "=", acc), ("reconciled", "=", False)])
        others = odoo("account.move.line", "search", [("id", "in", list(lines)), ("reconciled", "=", False)])
        if mine and others:
            odoo("account.move.line", "reconcile", mine + others)


COLS = ["Nombre anterior", "Nombre nuevo", "Diario anterior", "Diario nuevo", "Tipo", "UUID", "Fecha",
        "Cobro antes→después", "Conciliaciones antes→después", "Costo antes→después", "Resultado"]


def main():
    source = {}
    for c in FROM_JOURNALS.split(","):
        found = odoo("account.journal", "search", [("code", "=", c.strip())])
        if not found:
            raise SystemExit(f"No existe el diario de origen {c}")
        source[c.strip()] = found[0]
    source_code = {v: k for k, v in source.items()}
    dest_ids = {}
    for c in sorted({j for _, j in SERIE_JOURNAL.values()}):
        found = odoo("account.journal", "search", [("code", "=", c), ("type", "=", "sale")])
        if found:
            dest_ids[c] = found[0]
    hashed = [j["code"] for j in odoo("account.journal", "read", list(source.values()), fields=["code", "restrict_mode_hash_table"])
              if j.get("restrict_mode_hash_table")]
    if hashed:
        raise SystemExit(f"Diarios de origen con hash inalterable {hashed}: sus documentos no se pueden regresar a borrador")
    lock = lock_date(list(source.values()))
    cut = deadline()

    moves = candidates(source)
    # UUID repetido: se cuenta sobre todo el universo, antes de partirlo en líneas
    uuid_count = {}
    for m in moves:
        u = (m["l10n_mx_edi_cfdi_uuid"] or "").lower()
        uuid_count[u] = uuid_count.get(u, 0) + 1
    if PART:
        i, n = (int(x) for x in PART.split("/"))
        moves = [m for m in moves if m["id"] % n == i]
    atts = third_party_attachments(moves)
    att_order = [atts[m["id"]] for m in moves if m["id"] in atts]
    att_pos = {a: k for k, a in enumerate(att_order)}
    # Ocupados en destino: nombre por diario y UUID (un CFDI ya registrado en el diario de terceros)
    taken = {(m["journal_id"][0], m["name"]) for m in sr("account.move", [("journal_id", "in", list(dest_ids.values()))],
                                                         ["journal_id", "name"])}
    dest_uuids = {(m["l10n_mx_edi_cfdi_uuid"] or "").lower() for m in sr(
        "account.move", [("journal_id", "in", list(dest_ids.values())), ("state", "!=", "cancel"),
                         ("l10n_mx_edi_cfdi_uuid", "!=", False)], ["l10n_mx_edi_cfdi_uuid"])}

    modo = "EN SECO" if DRY_RUN else "REAL"
    log.info(f"=== Traslado a diarios de terceros | {LOCAL_ODOO.upper()} | {modo} | {ODOO_URL} ({ODOO_DB}) ===")
    log.info(f"Origen {list(source)} | destino {dest_ids} | {DATE_FROM}..{DATE_TO} | parte {PART or 'todo'} | "
             f"candado {lock or 'sin candado'} | hasta {HASTA or 'sin hora de corte'} | tope {BATCH_LIMIT or 'sin tope'}")
    log.info(f"Publicados con UUID: {len(moves)} | con XML de tercero: {len(atts)} | "
             f"sin XML de tercero (CFDI de Somos Reyes u otro): {len(moves) - len(atts)}")
    if not DRY_RUN:
        signal.signal(signal.SIGINT, _pedir_alto)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base = f"Traslado_{LOCAL_ODOO}_{'seco_' if DRY_RUN else ''}{stamp}{'_parte' + PART.replace('/', 'de') if PART else ''}"
    out = os.path.join(OUT_DIR, base + ".csv")
    fh = open(out, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(fh)
    w.writerow(COLS)

    tot_before = {a: 0.0 for a in ACCOUNTS}
    tot_after = {a: 0.0 for a in ACCOUNTS}
    summary, problems, cost_diffs = {}, [], []
    moved = 0
    stop_reason = ""

    def emit(row, mes, origen, destino, cat=None):
        w.writerow([row.get(c, "") for c in COLS])
        fh.flush()
        key = (mes, origen, destino, cat or row["Resultado"].split(":")[0])
        summary[key] = summary.get(key, 0) + 1

    cfdi = {}   # XML del tercero, se lee por bloques de 100 al llegar a ellos
    for m in moves:
        mid = m["id"]
        if mid not in atts:
            continue
        if BATCH_LIMIT and moved >= BATCH_LIMIT:
            stop_reason = f"tope de {BATCH_LIMIT} documentos"
            break
        if STOP["pedido"] or os.path.exists(STOP_FILE):
            stop_reason = "Ctrl+C" if STOP["pedido"] else "archivo DETENER"
            break
        if cut and datetime.now(timezone.utc) >= cut:
            stop_reason = f"hora de corte {HASTA} CDMX"
            break
        if atts[mid] not in cfdi:
            k = att_pos[atts[mid]]
            cfdi.update(read_cfdi(att_order[k:k + 100]))
        serie, folio, xml_iva = cfdi[atts[mid]]
        uuid = m["l10n_mx_edi_cfdi_uuid"]
        new_name = f"{serie}-{folio}"
        kind, dest_code = SERIE_JOURNAL.get(serie.upper(), (None, None))
        origen = source_code.get(m["journal_id"][0], m["journal_id"][1])
        mes = str(m["date"])[:7]
        row = {"Nombre anterior": m["name"], "Nombre nuevo": new_name, "Diario anterior": origen,
               "Diario nuevo": dest_code or "", "Tipo": m["move_type"], "UUID": uuid, "Fecha": m["date"]}
        cat, motivo = "", ""
        if kind is None:
            cat, motivo = "OMITIDO serie no MeLi", f"serie '{serie}' no es de MeLi (o el XML no tiene serie)"
        elif kind != m["move_type"]:
            cat, motivo = "OMITIDO serie/tipo", f"serie '{serie}' no corresponde a un {m['move_type']}"
        elif dest_code not in dest_ids:
            cat, motivo = "OMITIDO sin diario", f"no existe el diario {dest_code}"
        elif not folio:
            cat, motivo = "OMITIDO sin folio", "el XML no tiene folio"
        elif m["state"] != "posted" or m["journal_id"][0] not in source_code:
            cat, motivo = "OMITIDO no publicado", f"no está publicado en {list(source)} ({m['state']}, {m['journal_id'][1]})"
        elif lock and str(m["date"]) <= lock:
            cat, motivo = "OMITIDO PERIODO_CERRADO", f"fecha {m['date']} <= candado {lock}"
        elif uuid_count[uuid.lower()] > 1:
            cat, motivo = "OMITIDO UUID repetido", "UUID repetido en otro documento del origen (doble registro)"
        elif uuid.lower() in dest_uuids:
            cat, motivo = "OMITIDO UUID ya en destino", "el UUID ya está registrado en un diario de terceros"
        elif (dest_ids[dest_code], new_name) in taken:
            cat, motivo = "OMITIDO NOMBRE_OCUPADO", f"{new_name} ya existe en {dest_code}"
        if motivo:
            row["Resultado"] = f"{cat}: {motivo}"
            problems.append((m["name"], row["Resultado"]))
            emit(row, mes, origen, dest_code or "-", cat)
            continue
        if DRY_RUN:
            taken.add((dest_ids[dest_code], new_name))
            row["Resultado"] = "SE TRASLADARÍA"
            emit(row, mes, origen, dest_code)
            moved += 1
            continue

        paso = ["leer"]
        try:
            before = snapshot(mid)
            mv = before[0]
            if mv["state"] != "posted" or mv["journal_id"][0] not in source_code:
                row["Resultado"] = f"OMITIDO: ya no está publicado en el origen ({mv['state']}, {mv['journal_id'][1]})"
                emit(row, mes, origen, dest_code)
                continue
            cps = counterparts(mid)
            orig_cogs = cogs_lines(mid)
            # NC: su IVA es el del XML (lo fija el 06). Factura: el que tenía antes del traslado.
            target_tax = xml_iva if mv["move_type"] == "out_refund" else mv["amount_tax"]
            trasladar(mid, mv, cps, dest_ids[dest_code], new_name, target_tax, orig_cogs, paso)
            paso[0] = "verificar"
            after = snapshot(mid)
        except Exception as e:   # noqa: BLE001 — se reporta el paso y la línea se detiene
            row["Resultado"] = f"FALLO en '{paso[0]}': {str(e)[:300]}"
            emit(row, mes, origen, dest_code)
            problems.append((m["name"], row["Resultado"]))
            log.error(f"{m['name']}: {row['Resultado']}. SE DETIENE ESTA LÍNEA. Revisar el documento en Odoo "
                      f"(id {mid}): puede haber quedado en borrador, con nombre '/' o sin conciliar.")
            stop_reason = f"FALLO en {m['name']}"
            break
        taken.add((dest_ids[dest_code], new_name))
        moved += 1
        cost_diff = {k: round(after[1].get(k, 0) - before[1].get(k, 0), 2) for k in COST_ACCOUNTS}
        cost_diff = {k: v for k, v in cost_diff.items() if abs(v) > 0.004}
        if cost_diff:
            cost_diffs.append((mv["name"], new_name, cost_diff))
        for a in ACCOUNTS:
            tot_before[a] += before[1].get(a, 0)
            tot_after[a] += after[1].get(a, 0)
        checks = {"estado publicado": after[0]["state"] == "posted", "mismo UUID": after[0]["l10n_mx_edi_cfdi_uuid"] == uuid,
                  "asiento igual": after[1] == before[1], "costo idéntico": not cost_diff,
                  "conciliaciones iguales": after[2] == before[2], "cobro igual": after[0]["payment_state"] == mv["payment_state"],
                  "nombre = serie-folio": after[0]["name"] == new_name,
                  "diario destino": after[0]["journal_id"][0] == dest_ids[dest_code]}
        bad = [k for k, v in checks.items() if not v]
        if bad:
            problems.append((mv["name"], ", ".join(bad)))
        row.update({"Nombre nuevo": after[0]["name"], "Diario nuevo": after[0]["journal_id"][1], "Fecha": after[0]["date"],
                    "Cobro antes→después": f"{mv['payment_state']}→{after[0]['payment_state']}",
                    "Conciliaciones antes→después": f"{sum(before[2].values())}→{sum(after[2].values())}",
                    "Costo antes→después": "; ".join(f"{k} {before[1].get(k, 0):,.2f}→{after[1].get(k, 0):,.2f}"
                                                     for k in COST_ACCOUNTS if before[1].get(k, 0) or after[1].get(k, 0))
                                           or "sin costo",
                    "Resultado": "OK" if not bad else "REVISAR: " + ", ".join(bad)})
        emit(row, mes, origen, dest_code)
        log.info(f"{mv['name']} -> {after[0]['name']} | {row['Resultado']} | costo: {row['Costo antes→después']}")
    fh.close()

    # ── Resumen del lote ──
    res = os.path.join(OUT_DIR, base + "_resumen.csv")
    with open(res, "w", newline="", encoding="utf-8-sig") as f:
        rw = csv.writer(f)
        rw.writerow([f"Traslado {LOCAL_ODOO} {modo}", ODOO_DB, f"{DATE_FROM}..{DATE_TO}", f"parte {PART or 'todo'}",
                     f"se detuvo por: {stop_reason or 'terminó la lista'}"])
        rw.writerow([])
        rw.writerow(["Mes", "Diario anterior", "Diario nuevo", "Resultado", "Documentos"])
        for k in sorted(summary):
            rw.writerow(list(k) + [summary[k]])
        if not DRY_RUN:
            rw.writerow([])
            rw.writerow(["Totales por cuenta de lo trasladado", "Antes", "Después", "Diferencia"])
            for a in ACCOUNTS:
                rw.writerow([a, f"{tot_before[a]:.2f}", f"{tot_after[a]:.2f}", f"{tot_after[a] - tot_before[a]:.2f}"])
            rw.writerow([])
            rw.writerow(["Costo distinto (REVISAR)", len(cost_diffs)])
            for old, new, diff in cost_diffs:
                rw.writerow([old, new] + [f"{k} {v:+.2f}" for k, v in diff.items()])
        rw.writerow([])
        rw.writerow(["No se movió / no cuadra", "Motivo"])
        for p in problems:
            rw.writerow(list(p))

    log.info("=== Conteo por mes y diario (mes | origen -> destino | resultado: documentos) ===")
    for k in sorted(summary):
        log.info(f"  {k[0]} | {k[1]} -> {k[2]} | {k[3]}: {summary[k]}")
    if not DRY_RUN:
        log.info("=== Totales por cuenta (antes | después | diferencia) ===")
        for a in ACCOUNTS:
            log.info(f"  {a}: {tot_before[a]:,.2f} | {tot_after[a]:,.2f} | {tot_after[a] - tot_before[a]:,.2f}")
        log.info(f"=== Costo distinto (REVISAR): {len(cost_diffs)} ===")
    log.info(f"=== {'Se trasladarían' if DRY_RUN else 'Trasladados'}: {moved} | omitidos o no cuadran: {len(problems)} | "
             f"se detuvo por: {stop_reason or 'terminó la lista'} ===")
    log.info(f"=== Log por documento: {out} | resumen: {res} ===")
    return 1 if stop_reason.startswith("FALLO") else 0


if __name__ == "__main__":
    sys.exit(main())
