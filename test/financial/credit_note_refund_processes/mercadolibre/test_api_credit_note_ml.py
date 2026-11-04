#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ml_order_traceability.py
=========================================================================
Traza el ciclo de vida completo de una orden de Mercado Libre:

    orden  ->  factura (CFDI/documento fiscal)  ->  cancelación
           ->  claim / devolución  ->  pago  ->  refund  ->  NOTA DE CRÉDITO

Reutiliza la lógica de conexión del script original (token ML desde la DB).
NO escribe nada en la base de datos: solo lee el token y consulta la API.

Uso:
    1) Editá el bloque "EDITAR ACÁ" (ORDER_ID, INVOICE_ID, OUT_DIR, ACCESS_TOKEN).
    2) Corré: python ml_order_traceability.py
       (o pegá el archivo en un notebook: los pasos son llamadas a funciones)

Salidas:
    <out>/<order_id>/*.json | *.xml   -> payloads crudos de cada endpoint
    <out>/<order_id>/_resumen.json    -> resumen estructurado de la trazabilidad
=========================================================================
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # MySQLdb es opcional: si definís ACCESS_TOKEN no hace falta
    import MySQLdb
except ImportError:  # pragma: no cover
    MySQLdb = None

from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────

# Ruta del .env: configurable por variable de entorno para no hardcodear
# la ruta absoluta de una máquina en particular.
load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

BASE_URL = "https://api.mercadolibre.com"

# ══════════════════════════════════════════════════════════════════════
#  ── EDITAR ACÁ ──  Todo lo que necesitás tocar para correr el script
# ══════════════════════════════════════════════════════════════════════
ORDER_ID = "2000017527328146"   # Orden a rastrear (obligatorio)
INVOICE_ID = None               # invoice_id conocido, o None
OUT_DIR = "./ml_traza"          # Carpeta donde se guardan los payloads
ACCESS_TOKEN = None             # Token ML directo; None => se busca en la DB
# ══════════════════════════════════════════════════════════════════════

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
TOKENS_TABLE = os.getenv("ML_TOKENS_TABLE", "somos_reyes.tokens")

ML_SELLER_ID = os.getenv("ML_SELLER_ID", "25523702")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "20"))

# Los documentos fiscales de ML se distinguen por transaction_type.
# "sale" = factura de venta; "sale_return" = NOTA DE CRÉDITO por devolución/cancelación.
CREDIT_NOTE_TYPES = {"sale_return", "credit_note", "return"}
SALE_TYPES = {"sale", "invoice"}


# ─────────────────────────────────────────────────────────────────────────
# Infraestructura: token, sesión HTTP, logging y persistencia de payloads
# ─────────────────────────────────────────────────────────────────────────

def get_ml_token(seller_id: str = ML_SELLER_ID) -> str:
    """Obtiene el access_token vigente desde la tabla de tokens (misma
    lógica que 01_miner_ml_billing.py). Lanza RuntimeError si no hay fila."""
    if MySQLdb is None:
        raise RuntimeError(
            "MySQLdb no está instalado. Instalalo o pasá el token con "
            "definí ACCESS_TOKEN arriba."
        )
    db = MySQLdb.connect(
        host=DB_HOST, user=DB_USER, passwd=DB_PASSWORD, db=DB_NAME,
        local_infile=True, charset="utf8mb4",
    )
    try:
        cursor = db.cursor()
        cursor.execute(
            f"SELECT token FROM {TOKENS_TABLE} WHERE seller_id = %s", (seller_id,)
        )
        row = cursor.fetchone()
        cursor.close()
    finally:
        db.close()

    if not row or not row[0]:
        raise RuntimeError(f"No hay token en {TOKENS_TABLE} para seller_id={seller_id}")
    return str(row[0])


def build_session(token: str) -> requests.Session:
    """Sesión con reintentos para 429/5xx y backoff exponencial."""
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    retry = Retry(
        total=3, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def log(msg: str, level: str = "INFO") -> None:
    print(f"[{datetime.now():%H:%M:%S}] [{level}] {msg}")


@dataclass
class ApiResult:
    """Resultado normalizado de una llamada. Nunca lanza excepción hacia
    arriba: un 404 es información válida (ej.: 'no hay nota de crédito')."""
    label: str
    url: str
    status: int = 0
    ok: bool = False
    data: Any = None          # dict/list si es JSON
    text: str = ""            # cuerpo crudo (XML u otros)
    error: Optional[str] = None
    saved_as: Optional[str] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "label": self.label, "url": self.url, "status": self.status,
            "ok": self.ok, "error": self.error, "saved_as": self.saved_as,
        }


class MLClient:
    """Wrapper fino sobre la API de ML/MP con guardado automático de payloads."""

    def __init__(self, session: requests.Session, out_dir: Path):
        self.session = session
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.calls: List[ApiResult] = []

    def get(
        self,
        path: str,
        label: str,
        params: Optional[Dict[str, Any]] = None,
        save_as: Optional[str] = None,
        expect: str = "json",
        quiet_404: bool = True,
    ) -> ApiResult:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        res = ApiResult(label=label, url=url)
        try:
            resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            res.url = resp.url
            res.status = resp.status_code
            res.ok = resp.status_code == 200
            res.text = resp.text or ""

            if expect == "json":
                try:
                    res.data = resp.json()
                except ValueError:
                    res.data = None
                    if res.ok:
                        res.error = "Respuesta 200 sin JSON válido"
        except requests.RequestException as exc:
            res.error = f"{type(exc).__name__}: {exc}"

        # Logging: los 404 esperados (recurso inexistente) no son errores duros.
        if res.ok:
            log(f"OK   {label} ({res.status})")
        elif res.status == 404 and quiet_404:
            log(f"--   {label}: no existe / no aplica (404)")
        elif res.status in (401, 403):
            log(f"AUTH {label}: sin permiso o token inválido ({res.status})", "WARN")
        else:
            log(f"FAIL {label}: {res.status} {res.error or res.text[:180]}", "WARN")

        # Persistimos el payload solo si vino contenido útil.
        if save_as and (res.ok or res.text):
            dest = self.out_dir / save_as
            dest.write_text(res.text, encoding="utf-8", errors="replace")
            res.saved_as = str(dest)

        self.calls.append(res)
        return res


# ─────────────────────────────────────────────────────────────────────────
# Helpers de extracción
# ─────────────────────────────────────────────────────────────────────────

def dig(obj: Any, *keys, default=None):
    """Acceso anidado tolerante: dig(d, 'cancel_detail', 'requested_by')."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def is_credit_note(doc: Dict[str, Any]) -> bool:
    """Heurística para reconocer una nota de crédito dentro del discovery
    de documentos fiscales (el naming varía según país/versión del recurso)."""
    candidates = [
        str(doc.get(k, "")).lower()
        for k in ("transaction_type", "type", "document_type", "invoice_type", "subtype")
    ]
    return any(c in CREDIT_NOTE_TYPES for c in candidates)


def flatten_documents(payload: Any) -> List[Dict[str, Any]]:
    """El discovery devuelve a veces un dict con 'documents'/'results',
    a veces una lista plana. Normalizamos a lista de dicts."""
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        for key in ("documents", "results", "invoices", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [d for d in val if isinstance(d, dict)]
        # Documento único
        if any(k in payload for k in ("transaction_type", "document_type", "id")):
            return [payload]
    return []


# ─────────────────────────────────────────────────────────────────────────
# Pasos de la trazabilidad
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Trace:
    order_id: str
    order: Dict[str, Any] = field(default_factory=dict)
    cancellation: Dict[str, Any] = field(default_factory=dict)
    invoices: List[Dict[str, Any]] = field(default_factory=list)
    credit_notes: List[Dict[str, Any]] = field(default_factory=list)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    returns: List[Dict[str, Any]] = field(default_factory=list)
    payments: List[Dict[str, Any]] = field(default_factory=list)
    refunds: List[Dict[str, Any]] = field(default_factory=list)
    cfdi: Dict[str, Any] = field(default_factory=dict)          # CFDI parseado
    sat: Dict[str, Any] = field(default_factory=dict)           # estatus SAT
    billing_credit_notes: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# --- 1) Orden general + motivo de cancelación ---------------------------

def step_order(cli: MLClient, tr: Trace) -> None:
    log("── 1) Datos generales de la orden ──")
    r = cli.get(f"/orders/{tr.order_id}", "Orden", save_as=f"01_order_{tr.order_id}.json")
    if not r.ok or not isinstance(r.data, dict):
        tr.notes.append("No se pudo leer la orden: el resto de la traza puede quedar incompleta.")
        return

    o = r.data
    tr.order = {
        "id": o.get("id"),
        "status": o.get("status"),
        "status_detail": o.get("status_detail"),
        "date_created": o.get("date_created"),
        "date_closed": o.get("date_closed"),
        "last_updated": o.get("last_updated"),
        "total_amount": o.get("total_amount"),
        "paid_amount": o.get("paid_amount"),
        "currency_id": o.get("currency_id"),
        "buyer_id": dig(o, "buyer", "id"),
        "seller_id": dig(o, "seller", "id"),
        "pack_id": o.get("pack_id"),
        "shipping_id": dig(o, "shipping", "id"),
        "items": [
            {
                "sku": dig(it, "item", "seller_sku"),
                "title": dig(it, "item", "title"),
                "quantity": it.get("quantity"),
                "unit_price": it.get("unit_price"),
            }
            for it in (o.get("order_items") or [])
        ],
    }

    # cancel_detail solo existe cuando la cancelación fue pre-despacho.
    cd = o.get("cancel_detail") or {}
    tr.cancellation = {
        "is_cancelled": str(o.get("status", "")).lower() == "cancelled",
        "requested_by": cd.get("requested_by"),
        "code": cd.get("code"),
        "description": cd.get("description"),
        "date": cd.get("date"),
        "application_id": cd.get("application_id"),
        "group": cd.get("group"),
    }

    if tr.cancellation["is_cancelled"]:
        log(f"     Orden CANCELADA · requested_by={cd.get('requested_by')} code={cd.get('code')}")
    else:
        log(f"     Orden en estado '{o.get('status')}' (no figura como cancelled)", "WARN")
        tr.notes.append(
            "La orden no está en status 'cancelled'. Si esperabas nota de crédito, "
            "verificá si la cancelación fue parcial o post-despacho (devolución)."
        )

    # IDs de pago: fuente para los pasos de MP.
    tr.payments = [
        {
            "id": p.get("id"),
            "status": p.get("status"),
            "status_detail": p.get("status_detail"),
            "transaction_amount": p.get("transaction_amount"),
            # ESTE campo faltaba en la v1 y por eso el veredicto decía
            # "sin refund" cuando el reembolso sí estaba hecho.
            "transaction_amount_refunded": p.get("transaction_amount_refunded"),
            "total_paid_amount": p.get("total_paid_amount"),
            "date_approved": p.get("date_approved"),
            "date_last_modified": p.get("date_last_modified"),
            "payment_method_id": p.get("payment_method_id"),
        }
        for p in (o.get("payments") or [])
    ]


# --- 2) Documentos fiscales: factura y NOTA DE CRÉDITO ------------------

def step_fiscal_documents(cli: MLClient, tr: Trace, invoice_id: Optional[str]) -> None:
    log("── 2) Documentos fiscales (factura y nota de crédito) ──")
    oid = tr.order_id

    # 2.a Discovery: lista los documentos emitidos para la orden.
    disc = cli.get(
        f"/invoices/io/documents/stream/order/{oid}/json",
        "Discovery documentos fiscales",
        save_as=f"02_discovery_{oid}.json",
    )
    docs = flatten_documents(disc.data) if disc.ok else []

    for d in docs:
        entry = {
            "id": d.get("id") or d.get("invoice_id") or d.get("document_id"),
            "transaction_type": d.get("transaction_type") or d.get("type"),
            "status": d.get("status"),
            "date_created": d.get("date_created") or d.get("date"),
            "cfdi_uuid": d.get("cfdi_uuid") or d.get("uuid") or d.get("external_id"),
            "amount": d.get("total_amount") or d.get("amount"),
            "raw": d,
        }
        (tr.credit_notes if is_credit_note(d) else tr.invoices).append(entry)

    if not docs and disc.ok:
        tr.notes.append("El discovery respondió 200 pero sin documentos listados.")

    # 2.b Fallbacks de facturación (varían por sitio; se intentan en orden).
    if not tr.invoices:
        for path, label, fname in (
            (f"/orders/{oid}/billing_info", "Billing info de la orden", f"02b_billing_info_{oid}.json"),
            (f"/users/{ML_SELLER_ID}/invoices/orders/{oid}", "Factura por orden (legacy)", f"02c_invoice_legacy_{oid}.json"),
        ):
            r = cli.get(path, label, save_as=fname)
            if r.ok and r.data:
                tr.invoices.extend(flatten_documents(r.data) or [{"raw": r.data}])
                break

    # 2.c XML de la factura de venta.
    cli.get(
        f"/invoices/io/documents/stream/order/{oid}/xml",
        "XML factura (sale)",
        params={"transaction_type": "sale"},
        save_as=f"03_factura_sale_{oid}.xml",
        expect="xml",
    )

    # 2.d XML de la NOTA DE CRÉDITO. Este es el objetivo principal:
    #     un 404 aquí significa "todavía no se emitió".
    cn = cli.get(
        f"/invoices/io/documents/stream/order/{oid}/xml",
        "XML NOTA DE CRÉDITO (sale_return)",
        params={"transaction_type": "sale_return"},
        save_as=f"04_nota_credito_{oid}.xml",
        expect="xml",
    )
    if cn.ok and cn.text.strip():
        log("     >>> NOTA DE CRÉDITO ENCONTRADA (XML descargado)")
        if not tr.credit_notes:
            tr.credit_notes.append({
                "id": None, "transaction_type": "sale_return",
                "source": "stream/order/xml", "saved_as": cn.saved_as,
            })
    else:
        tr.notes.append(
            "No se obtuvo XML de nota de crédito por orden. Puede que aún no esté "
            "emitida (ML tarda hasta 24-72h post-cancelación) o que se emita por invoice_id."
        )

    # 2.e Si conocemos un invoice_id explícito, lo consultamos directo.
    ids_to_try = {invoice_id} if invoice_id else set()
    ids_to_try |= {str(d["id"]) for d in tr.invoices + tr.credit_notes if d.get("id")}
    for iid in filter(None, ids_to_try):
        cli.get(
            f"/invoices/io/documents/stream/invoice/{iid}/xml",
            f"XML por invoice_id {iid}",
            save_as=f"05_invoice_{iid}.xml",
            expect="xml",
        )


# --- 3) Reclamos y devoluciones ----------------------------------------

def step_claims(cli: MLClient, tr: Trace) -> None:
    log("── 3) Reclamos (claims) y devoluciones ──")
    oid = tr.order_id

    # La búsqueda de claims acepta order_id; probamos también por pack_id,
    # porque en carritos el claim se abre a nivel pack.
    search_params: List[Dict[str, Any]] = [{"order_id": oid}]
    pack_id = tr.order.get("pack_id")
    if pack_id and str(pack_id) != str(oid):
        search_params.append({"resource_id": pack_id})

    claim_ids: List[str] = []
    for i, params in enumerate(search_params):
        r = cli.get(
            "/post-purchase/v1/claims/search",
            f"Búsqueda de claims {params}",
            params=params,
            save_as=f"06_claims_search_{i}_{oid}.json",
        )
        for c in (dig(r.data, "data", default=[]) or dig(r.data, "results", default=[]) or []):
            if isinstance(c, dict) and c.get("id"):
                claim_ids.append(str(c["id"]))

    claim_ids = list(dict.fromkeys(claim_ids))  # dedup preservando orden
    if not claim_ids:
        log("     Sin claims asociados: la cancelación probablemente fue pre-despacho.")
        tr.notes.append("No se encontraron claims. Consistente con cancelación antes del envío.")
        return

    for cid in claim_ids:
        detail = cli.get(f"/post-purchase/v1/claims/{cid}", f"Claim {cid}",
                         save_as=f"07_claim_{cid}.json")
        extra = cli.get(f"/post-purchase/v1/claims/{cid}/detail", f"Claim {cid} · detalle",
                        save_as=f"08_claim_{cid}_detail.json")
        d = detail.data if isinstance(detail.data, dict) else {}
        tr.claims.append({
            "id": cid,
            "type": d.get("type"),
            "stage": d.get("stage"),
            "status": d.get("status"),
            "reason_id": d.get("reason_id"),
            "resolution": d.get("resolution"),
            "date_created": d.get("date_created"),
            "players": d.get("players"),
            "detail_extra": extra.data if extra.ok else None,
        })

        # Devolución física asociada al claim (v2).
        rt = cli.get(f"/post-purchase/v2/claims/{cid}/returns", f"Returns del claim {cid}",
                     save_as=f"09_claim_{cid}_returns.json")
        for ret in flatten_documents(rt.data) if rt.ok else []:
            tr.returns.append({
                "claim_id": cid,
                "id": ret.get("id"),
                "type": ret.get("type"),
                "subtype": ret.get("subtype"),
                "status": ret.get("status"),
                "status_money": ret.get("status_money"),
                "shipping": ret.get("shipping"),
                "date_created": ret.get("date_created"),
            })


# --- 4) Pagos y reembolsos (Mercado Pago) ------------------------------

def step_payments(cli: MLClient, tr: Trace) -> None:
    log("── 4) Pagos y reembolsos en Mercado Pago ──")
    if not tr.payments:
        log("     La orden no trae payments; no hay dinero que rastrear.", "WARN")
        tr.notes.append("La orden no expone payments (¿orden sin pago acreditado?).")
        return

    for p in tr.payments:
        pid = p.get("id")
        if not pid:
            continue

        r = cli.get(f"/v1/payments/{pid}", f"Pago {pid}", save_as=f"10_payment_{pid}.json")
        if r.ok and isinstance(r.data, dict):
            d = r.data
            p.update({
                "status": d.get("status"),
                "status_detail": d.get("status_detail"),
                "transaction_amount": d.get("transaction_amount"),
                "transaction_amount_refunded": d.get("transaction_amount_refunded"),
                "date_last_updated": d.get("date_last_updated"),
                "payment_method_id": d.get("payment_method_id"),
                "refunded_totalmente": (
                    d.get("transaction_amount_refunded") == d.get("transaction_amount")
                    and (d.get("transaction_amount_refunded") or 0) > 0
                ),
            })

        # Lista explícita de refunds: es el respaldo "de dinero" de la nota de crédito.
        rr = cli.get(f"/v1/payments/{pid}/refunds", f"Refunds del pago {pid}",
                     save_as=f"11_refunds_{pid}.json")
        for ref in (rr.data if isinstance(rr.data, list) else flatten_documents(rr.data)):
            tr.refunds.append({
                "payment_id": pid,
                "id": ref.get("id"),
                "amount": ref.get("amount"),
                "status": ref.get("status"),
                "date_created": ref.get("date_created"),
                "reason": ref.get("reason"),
                "refund_mode": ref.get("refund_mode"),
            })

    total_ref = sum(float(r.get("amount") or 0) for r in tr.refunds)
    if tr.refunds:
        log(f"     {len(tr.refunds)} refund(s) · total reembolsado: {total_ref:,.2f}")
    else:
        log("     Sin refunds registrados.")
        tr.notes.append("No hay refunds en MP: el dinero puede no haberse devuelto todavía.")


# ─────────────────────────────────────────────────────────────────────────
# PASOS v2

import re
import xml.etree.ElementTree as ET

# Todos los transaction_type conocidos. Los de venta y los de nota de crédito.
# OJO: la lista varía por site; en MLM algunos devuelven 404 y es normal.
TX_VENTA = ["sale", "disposal_sale"]
TX_NOTA_CREDITO = ["sale_return", "devolution", "disposal_sale_return"]

SAT_WSDL = "https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc"


# --- 2') Documentos fiscales, probando TODOS los transaction_type ---------

def step_documentos_fiscales_v2(cli, tr, invoice_id=None):
    """Recorre todos los transaction_type conocidos en JSON y XML.
    El JSON trae fiscal_data.transaction_type, que es la fuente de verdad
    sobre qué tipo de documento se emitió realmente para esta orden."""
    log("── 2) Documentos fiscales (todos los transaction_type) ──")
    oid = tr.order_id

    for tx in TX_VENTA + TX_NOTA_CREDITO:
        es_nc = tx in TX_NOTA_CREDITO
        etiqueta = "NOTA DE CRÉDITO" if es_nc else "FACTURA"

        # JSON primero: trae metadata (invoice_id, fiscal_data, document_type).
        rj = cli.get(
            f"/invoices/io/documents/stream/order/{oid}/json",
            f"{etiqueta} JSON · transaction_type={tx}",
            params={"transaction_type": tx},
            save_as=f"20_json_{tx}_{oid}.json",
        )

        if rj.ok and isinstance(rj.data, dict):
            doc = {
                "transaction_type": tx,
                "invoice_id": rj.data.get("id") or dig(rj.data, "invoice", "id"),
                "document_type": dig(rj.data, "fiscal_data", "document_type")
                                 or rj.data.get("document_type"),
                "fiscal_data": rj.data.get("fiscal_data"),
                "status": rj.data.get("status"),
                "raw": rj.data,
            }
            (tr.credit_notes if es_nc else tr.invoices).append(doc)
            if es_nc:
                log(f"     >>> NOTA DE CRÉDITO encontrada con transaction_type={tx}")

        # XML: el documento fiscal propiamente dicho.
        rx = cli.get(
            f"/invoices/io/documents/stream/order/{oid}/xml",
            f"{etiqueta} XML · transaction_type={tx}",
            params={"transaction_type": tx},
            save_as=f"21_xml_{tx}_{oid}.xml",
            expect="xml",
        )
        # Filtro anti-falso-positivo: el servicio devuelve JSON de error con
        # status 200/404 y el script viejo lo guardaba como si fuera un XML.
        if rx.ok and not rx.text.lstrip().startswith("<"):
            log(f"     (transaction_type={tx} devolvió un error, no un XML)", "WARN")

    # Consulta directa por invoice_id si lo conocemos.
    ids = {invoice_id} if invoice_id else set()
    ids |= {str(d.get("invoice_id")) for d in tr.invoices + tr.credit_notes if d.get("invoice_id")}
    for iid in filter(lambda v: v and v != "None", ids):
        cli.get(f"/invoices/io/documents/stream/invoice/{iid}/json",
                f"Invoice {iid} JSON", save_as=f"22_invoice_{iid}.json")
        cli.get(f"/invoices/io/documents/stream/invoice/{iid}/xml",
                f"Invoice {iid} XML", save_as=f"22_invoice_{iid}.xml", expect="xml")

    if not tr.credit_notes:
        tr.notes.append(
            "Ningún transaction_type de nota de crédito devolvió documento. "
            "En ventas a público en general (RFC XAXX010101000 / UsoCFDI S01) "
            "esto es lo esperado: la anulación se hace CANCELANDO el CFDI, "
            "no emitiendo un CFDI de Egreso. Ver el paso del SAT."
        )


# --- 2b) Parseo del CFDI + consulta de estatus en el SAT ------------------

CFDI_NS = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}


def parsear_cfdi(ruta):
    """Extrae los datos necesarios para consultar el estatus en el SAT."""
    root = ET.parse(ruta).getroot()
    emisor = root.find("cfdi:Emisor", CFDI_NS)
    receptor = root.find("cfdi:Receptor", CFDI_NS)
    tfd = root.find(".//tfd:TimbreFiscalDigital", CFDI_NS)
    if tfd is None:
        raise ValueError("El XML no tiene TimbreFiscalDigital (no está timbrado)")

    sello = root.get("Sello") or ""
    return {
        "uuid": tfd.get("UUID"),
        "rfc_emisor": emisor.get("Rfc"),
        "rfc_receptor": receptor.get("Rfc"),
        "uso_cfdi": receptor.get("UsoCFDI"),
        "total": root.get("Total"),
        "tipo_comprobante": root.get("TipoDeComprobante"),
        "serie": root.get("Serie"),
        "folio": root.get("Folio"),
        "fecha": root.get("Fecha"),
        "fecha_timbrado": tfd.get("FechaTimbrado"),
        "sello_ultimos_8": sello[-8:],  # requerido por la consulta del SAT
    }


def consultar_sat(cfdi, timeout=30):
    """Consulta el ConsultaCFDIService del SAT (SOAP) y devuelve el estado.

    Este es el ÚNICO dato que resuelve la duda de fondo: si el CFDI figura
    como 'Cancelado' ante el SAT, la venta ya quedó anulada fiscalmente y
    NO debe existir ninguna nota de crédito."""
    expresion = (
        f"?re={cfdi['rfc_emisor']}"
        f"&rr={cfdi['rfc_receptor']}"
        f"&tt={cfdi['total']}"
        f"&id={cfdi['uuid']}"
        f"&fe={cfdi['sello_ultimos_8']}"
    )
    envelope = f"""<soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:tem="http://tempuri.org/">
      <soapenv:Header/>
      <soapenv:Body>
        <tem:Consulta>
          <tem:expresionImpresa><![CDATA[{expresion}]]></tem:expresionImpresa>
        </tem:Consulta>
      </soapenv:Body>
    </soapenv:Envelope>"""

    resp = requests.post(
        SAT_WSDL,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://tempuri.org/IConsultaCFDIService/Consulta",
        },
        timeout=timeout,
    )

    def tag(nombre):
        m = re.search(rf"<a:{nombre}>(.*?)</a:{nombre}>", resp.text, re.S)
        return m.group(1).strip() if m else None

    return {
        "http_status": resp.status_code,
        "codigo_estatus": tag("CodigoEstatus"),
        "estado": tag("Estado"),                        # Vigente | Cancelado | No Encontrado
        "es_cancelable": tag("EsCancelable"),
        "estatus_cancelacion": tag("EstatusCancelacion"),
        "validacion_efos": tag("ValidacionEFOS"),
        "expresion_consultada": expresion,
    }


def step_sat(cli, tr):
    """Parsea el CFDI de venta descargado y consulta su estatus en el SAT."""
    log("── 2b) Estatus del CFDI ante el SAT ──")

    # Buscamos cualquier XML guardado que sea realmente un CFDI.
    candidatos = sorted(cli.out_dir.glob("*.xml"))
    cfdi = None
    for ruta in candidatos:
        try:
            if ruta.read_text(encoding="utf-8", errors="replace").lstrip().startswith("<"):
                cfdi = parsear_cfdi(ruta)
                cfdi["archivo"] = str(ruta)
                break
        except Exception:
            continue

    if not cfdi:
        log("     No hay ningún CFDI parseable en la carpeta.", "WARN")
        tr.notes.append("No se pudo parsear ningún CFDI para consultar al SAT.")
        return

    tr.cfdi = cfdi
    log(f"     CFDI {cfdi['serie']}-{cfdi['folio']} · UUID {cfdi['uuid']} "
        f"· tipo {cfdi['tipo_comprobante']} · receptor {cfdi['rfc_receptor']}")

    try:
        sat = consultar_sat(cfdi)
        tr.sat = sat
        log(f"     SAT dice: Estado={sat['estado']} · "
            f"EstatusCancelacion={sat['estatus_cancelacion']}")

        if (sat.get("estado") or "").lower() == "cancelado":
            tr.notes.append(
                f"CFDI {cfdi['uuid']} CANCELADO ante el SAT. La venta ya está "
                "anulada fiscalmente; no corresponde buscar nota de crédito."
            )
        elif (sat.get("estado") or "").lower() == "vigente":
            tr.notes.append(
                f"CFDI {cfdi['uuid']} sigue VIGENTE ante el SAT pese a la "
                "cancelación de la orden. Esto es lo que hay que reclamarle a ML: "
                "o cancelan el CFDI, o emiten el egreso."
            )
    except Exception as exc:
        log(f"     No se pudo consultar el SAT: {exc}", "WARN")
        tr.notes.append(f"Consulta al SAT falló: {exc}")


# --- 5) Nota de crédito de ML al VENDEDOR (reversión de comisiones) -------

def step_billing_notas_credito(cli, tr, meses_atras=4):
    """Busca la nota de crédito que Mercado Libre emite al vendedor cuando
    anula los cargos de una venta cancelada después del cierre del período.

    Es un documento DISTINTO del CFDI al comprador: acá ML te devuelve la
    comisión de venta y los cargos de envío de la orden anulada."""
    log("── 5) Notas de crédito de ML al vendedor (billing) ──")

    per = cli.get("/billing/integration/periods",
                  "Períodos de facturación ML",
                  params={"group": "ML", "document_type": "CREDIT_NOTE", "limit": meses_atras},
                  save_as="30_billing_periods.json")

    periodos = dig(per.data, "results", default=[]) or []
    if not periodos:
        tr.notes.append("No se listaron períodos de facturación (¿scope de billing en la app?).")
        return

    for p in periodos:
        exp = p.get("expiration_date")
        if not exp:
            continue

        # Documentos (facturas y notas de crédito) del período.
        cli.get(f"/billing/integration/periods/{exp}/documents",
                f"Documentos período {exp}",
                params={"group": "ML", "document_type": "CREDIT_NOTE"},
                save_as=f"31_billing_docs_{exp}.json")

        # Renglones filtrados por ESTA orden: acá aparece la reversión.
        det = cli.get(f"/billing/integration/periods/{exp}/group/ML/details",
                      f"Detalle CREDIT_NOTE período {exp} · orden {tr.order_id}",
                      params={"document_type": "CREDIT_NOTE",
                              "order_ids": tr.order_id, "limit": 100},
                      save_as=f"32_billing_details_{exp}.json")

        for r in (dig(det.data, "results", default=[]) or []):
            tr.billing_credit_notes.append({
                "periodo": exp,
                "document_id": dig(r, "document_info", "document_id"),
                "legal_document_number": dig(r, "charge_info", "legal_document_number"),
                "detalle": dig(r, "charge_info", "transaction_detail"),
                "monto": dig(r, "charge_info", "detail_amount"),
                "detail_type": dig(r, "charge_info", "detail_type"),
                "detail_sub_type": dig(r, "charge_info", "detail_sub_type"),
                "status": dig(r, "charge_info", "status"),
                "moneda": dig(r, "currency_info", "currency_id"),
            })

    if tr.billing_credit_notes:
        total = sum(float(n.get("monto") or 0) for n in tr.billing_credit_notes)
        log(f"     >>> {len(tr.billing_credit_notes)} renglón(es) de nota de "
            f"crédito de ML · total bonificado {total:,.2f}")
    else:
        log("     Sin renglones de nota de crédito de ML para esta orden.")
        tr.notes.append(
            "No aparecen bonificaciones de ML para esta orden. Si la cancelación "
            "cayó dentro del mismo período de la venta, la reversión sale como "
            "bonificación en la factura (BILL), no como CREDIT_NOTE: "
            "reintentá con document_type=BILL y detail_type=bonus."
        )


# --- 4') Pagos: usar lo que ya trae la orden ------------------------------

def step_pagos_v2(cli, tr):
    """El endpoint /v1/payments/{id} es de Mercado Pago y NO responde con un
    token de aplicación de Mercado Libre (404 'resource not found').
    La orden ya trae los campos de reembolso: los usamos como fuente primaria
    y sólo intentamos MP como complemento opcional."""
    log("── 4) Pagos y reembolsos ──")
    for p in tr.payments:
        pid = p.get("id")
        monto = float(p.get("transaction_amount") or 0)
        reint = float(p.get("transaction_amount_refunded") or 0)
        p["reembolso_total"] = reint > 0 and abs(reint - monto) < 0.01
        p["reembolso_parcial"] = 0 < reint < monto

        log(f"     Pago {pid}: status={p.get('status')}/{p.get('status_detail')} "
            f"· monto {monto:,.2f} · reembolsado {reint:,.2f}")

        if str(p.get("status_detail")) == "bpp_refunded":
            tr.notes.append(
                f"Pago {pid} reembolsado vía Buyer Protection Program (bpp_refunded). "
                "ML cubrió al comprador; verificá en billing si el cargo se te "
                "debitó o se te bonificó."
            )

        # Complemento opcional: sólo funciona con credenciales de Mercado Pago.
        cli.get(f"/v1/payments/{pid}", f"MP · pago {pid}",
                save_as=f"40_mp_payment_{pid}.json")
        cli.get(f"/v1/payments/{pid}/refunds", f"MP · refunds {pid}",
                save_as=f"41_mp_refunds_{pid}.json")


# ─────────────────────────────────────────────────────────────────────────
# Reporte final
# ─────────────────────────────────────────────────────────────────────────

def build_report(cli: MLClient, tr: Trace) -> Dict[str, Any]:
    # El reembolso se lee de la ORDEN (que siempre lo trae), no de /v1/payments.
    total_refunded = sum(
        float(p.get("transaction_amount_refunded") or 0) for p in tr.payments
    ) or sum(float(r.get("amount") or 0) for r in tr.refunds)
    order_total = float(tr.order.get("total_amount") or 0)

    estado_sat = (tr.sat.get("estado") or "").lower()
    publico_general = tr.cfdi.get("rfc_receptor") == "XAXX010101000"

    # Árbol de decisión ordenado de la conclusión más fuerte a la más débil.
    if tr.credit_notes:
        veredicto = "NOTA DE CRÉDITO AL COMPRADOR EMITIDA (CFDI de Egreso)"
    elif estado_sat == "cancelado":
        veredicto = ("CFDI CANCELADO ANTE EL SAT · la venta está anulada "
                     "fiscalmente y NO corresponde nota de crédito")
    elif estado_sat == "vigente" and publico_general:
        veredicto = ("CFDI VIGENTE con receptor público en general · ML debe "
                     "CANCELAR el CFDI (no emite egreso en este escenario) — "
                     "abrir reclamo con ML si no lo cancela")
    elif estado_sat == "vigente":
        veredicto = "CFDI VIGENTE pese a la cancelación · falta egreso o cancelación"
    elif total_refunded >= order_total > 0:
        veredicto = "REEMBOLSO TOTAL CONFIRMADO · estatus fiscal sin verificar en el SAT"
    elif tr.cancellation.get("is_cancelled"):
        veredicto = "ORDEN CANCELADA · sin documento fiscal de reversa localizado"
    else:
        veredicto = "SIN EVIDENCIA DE CANCELACIÓN FISCAL"

    return {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "order_id": tr.order_id,
        "veredicto": veredicto,
        "orden": tr.order,
        "cancelacion": tr.cancellation,
        "cfdi": tr.cfdi,
        "estatus_sat": tr.sat,
        "facturas": tr.invoices,
        "notas_de_credito_comprador": tr.credit_notes,
        "notas_de_credito_ml_al_vendedor": tr.billing_credit_notes,
        "claims": tr.claims,
        "devoluciones": tr.returns,
        "pagos": tr.payments,
        "refunds": tr.refunds,
        "totales": {
            "total_orden": order_total,
            "total_reembolsado": total_refunded,
            "diferencia": round(order_total - total_refunded, 2),
        },
        "observaciones": tr.notes,
        "llamadas": [c.summary() for c in cli.calls],
    }


def print_report(rep: Dict[str, Any]) -> None:
    line = "═" * 72
    print(f"\n{line}\nRESUMEN · Orden {rep['order_id']}\n{line}")
    o, c = rep["orden"], rep["cancelacion"]
    print(f"Estado.............: {o.get('status')} / {o.get('status_detail')}")
    print(f"Total..............: {o.get('total_amount')} {o.get('currency_id')}")
    print(f"Cancelada..........: {c.get('is_cancelled')} "
          f"(por {c.get('requested_by')} · code {c.get('code')})")
    cf, sat = rep.get("cfdi") or {}, rep.get("estatus_sat") or {}
    if cf:
        print(f"CFDI...............: {cf.get('serie')}-{cf.get('folio')} "
              f"tipo {cf.get('tipo_comprobante')} · receptor {cf.get('rfc_receptor')} "
              f"({cf.get('uso_cfdi')})")
        print(f"UUID...............: {cf.get('uuid')}")
    if sat:
        print(f"Estatus SAT........: {sat.get('estado')} · "
              f"cancelación: {sat.get('estatus_cancelacion')}")
    print(f"Facturas...........: {len(rep['facturas'])}")
    print(f"NC al comprador....: {len(rep['notas_de_credito_comprador'])}")
    print(f"NC de ML a vos.....: {len(rep['notas_de_credito_ml_al_vendedor'])}")
    print(f"Claims.............: {len(rep['claims'])}")
    print(f"Devoluciones.......: {len(rep['devoluciones'])}")
    print(f"Refunds............: {len(rep['refunds'])} "
          f"(total {rep['totales']['total_reembolsado']:,.2f})")
    print(f"\n>> VEREDICTO: {rep['veredicto']}")
    if rep["observaciones"]:
        print("\nObservaciones:")
        for n in rep["observaciones"]:
            print(f"  · {n}")
    print(line)


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────

def resolve_token() -> str:
    """Token desde (1) la constante ACCESS_TOKEN, (2) la env var
    ML_ACCESS_TOKEN, o (3) la DB. En ese orden."""
    if ACCESS_TOKEN:
        return ACCESS_TOKEN.strip()
    if os.getenv("ML_ACCESS_TOKEN"):
        return os.environ["ML_ACCESS_TOKEN"].strip()
    return get_ml_token(ML_SELLER_ID)


def safe(fn, *args) -> None:
    """Corre un paso aislado: si revienta, lo anota y sigue con el siguiente."""
    nombre = fn.__name__
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 — queremos continuar la traza
        log(f"Paso '{nombre}' abortado: {type(exc).__name__}: {exc}", "ERROR")
        for a in args:
            if isinstance(a, Trace):
                a.notes.append(f"Paso '{nombre}' falló: {exc}")


def guardar_reporte(cli: MLClient, tr: Trace) -> Dict[str, Any]:
    """Construye el resumen, lo escribe en disco y lo imprime."""
    rep = build_report(cli, tr)
    destino = cli.out_dir / "_resumen.json"
    destino.write_text(
        json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print_report(rep)
    log(f"Resumen escrito en {destino}")
    return rep


# ─────────────────────────────────────────────────────────────────────────
# EJECUCIÓN
# Se corre con:  python ml_order_traceability.py
# Comentá cualquier línea de abajo para saltear ese paso.
# ─────────────────────────────────────────────────────────────────────────

# Setup
token = resolve_token()
cli = MLClient(build_session(token), Path(OUT_DIR) / str(ORDER_ID))
tr = Trace(order_id=str(ORDER_ID))
log(f"Iniciando trazabilidad de la orden {tr.order_id} → {cli.out_dir}")

# Pasos
safe(step_order, cli, tr)                             # 1) orden + motivo de cancelación
safe(step_documentos_fiscales_v2, cli, tr, INVOICE_ID)# 2) TODOS los transaction_type
safe(step_sat, cli, tr)                               # 2b) estatus del CFDI en el SAT
safe(step_claims, cli, tr)                            # 3) claims y devoluciones
safe(step_pagos_v2, cli, tr)                          # 4) pagos y reembolsos
safe(step_billing_notas_credito, cli, tr)             # 5) NC de ML al vendedor

# Reporte
reporte = guardar_reporte(cli, tr)