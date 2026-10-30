"""
Reproceso controlado de guías con VoBo humano (Amazon, Coppel, TikTok).

Un operador captura en el Google Sheet de "control de reprocesamiento de
guías" (una pestaña por marketplace) las órdenes que aprobó. Cada script de
guías carga esa lista al inicio y, para esas órdenes, se SALTA las reglas de
negocio que las rechazaban (límite de costo, pricing, etc.). Las validaciones
duras (orden cancelada / sin bloquear en Odoo, PICK, cotización, API de guías)
se siguen aplicando tal cual las define cada script.

Este módulo sólo se ocupa del sheet de reproceso: leer las órdenes elegibles y
registrar el resultado (estado e intentos). La generación de la guía, la BD
(`tools.shipping_labels`) y los sheets de control originales los sigue
escribiendo cada script.

Uso típico (en cada script de guías):

    from _01_vobo_reprocess import VoboSheet, EST_OK, EST_FAIL, EST_BLOCKED

    VOBO = VoboSheet('Coppel')
    VOBO.load(client)                 # nunca lanza excepción
    if order_id in VOBO: ...          # la orden tiene VoBo vigente
    VOBO.annotate(order_id, odoo_id=so_name)   # opcional: ID Odoo / tienda
    VOBO.mark(order_id, EST_OK)       # se ignora si la orden no tiene VoBo

Columnas del sheet (por nombre de encabezado, en cualquier orden):
    Manuales:    Marketplace ID, VoBo, Motivo Original, Aprobador, (Tienda opcional)
    Automáticas: Odoo ID, Tienda (si está vacía), Estado, Intentos, Fecha Guia

Estados (columna `estado`):
    (vacío) / PENDIENTE   -> elegible
    REINTENTO_FALLIDO     -> elegible mientras `intentos` < MAX_REPROCESS_ATTEMPTS
    GUIA_GENERADA         -> terminal
    BLOQUEADA_VALIDACION  -> terminal; el operador la libera borrando el estado
    AGOTADA               -> terminal; se alcanzó el máximo de intentos

Variables de entorno:
    SPREADSHEET_REPROCESS_ID  ID del sheet. Si no está definida el reproceso
                              queda desactivado (el flujo se comporta como siempre).
    MAX_REPROCESS_ATTEMPTS    Máximo de intentos fallidos (default 3).
    VOBO_DRY_RUN              true: sólo lista las órdenes elegibles; no aplica
                              VoBo ni escribe nada.
    GOOGLE_CREDS_JSON         Cuenta de servicio (JSON inline o ruta a archivo).
"""

import json
import logging
import os
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

EST_PENDING = 'PENDIENTE'
EST_OK = 'GUIA_GENERADA'
EST_FAIL = 'REINTENTO_FALLIDO'
EST_BLOCKED = 'BLOQUEADA_VALIDACION'
EST_EXHAUSTED = 'AGOTADA'

_ELIGIBLE_STATES = ('', EST_PENDING, EST_FAIL)

# Lista positiva a propósito: ante una celda ambigua NO se reprocesa.
_APPROVED_VALUES = {'SI', 'SÍ', 'S', 'OK', 'X', 'TRUE', '1', 'YES', 'Y', 'VOBO',
                    'VO.BO.', 'VO.BO', 'APROBADO', 'APROBADA'}

_REQUIRED_COLUMNS = ('marketplace_id', 'vobo', 'estado', 'intentos')


def _header(raw) -> str:
    """'Marketplace ID' -> marketplace_id, '# Intentos' -> intentos, 'Fecha Guía' -> fecha_guia."""
    text = unicodedata.normalize('NFKD', str(raw)).encode('ascii', 'ignore').decode()
    return text.replace('#', '').strip().lower().replace(' ', '_')


def _now_cdmx() -> str:
    # México ya no usa horario de verano: UTC-6 fijo.
    return datetime.now(timezone(timedelta(hours=-6))).strftime('%d/%m/%Y %H:%M:%S')


def _flag(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).strip().lower() in ('1', 'true', 'yes', 'y')


def authenticate_google_sheets():
    """Acepta el JSON de la cuenta de servicio inline (Kestra) o una ruta local."""
    try:
        raw = (os.getenv('GOOGLE_CREDS_JSON') or '').strip()
        if not raw:
            logger.error("VoBo: GOOGLE_CREDS_JSON no está definida.")
            return None
        if raw.startswith('{'):
            creds = Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(raw, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"VoBo: error de autenticación en Google Sheets: {e}")
        return None


class VoboSheet:
    """Lee las órdenes con VoBo de una pestaña y registra su resultado."""

    def __init__(self, marketplace: str):
        self.marketplace = marketplace
        self.spreadsheet_id = os.getenv('SPREADSHEET_REPROCESS_ID')
        self.max_attempts = int(os.getenv('MAX_REPROCESS_ATTEMPTS', '3'))
        self.dry_run = _flag('VOBO_DRY_RUN')
        self.orders = {}  # marketplace_id -> {'row', 'intentos', 'aprobador', 'tienda'}
        self._ws = None
        self._cols = {}
        self._marked = set()

    # ------------------------------------------------------------------
    def __contains__(self, order_id) -> bool:
        return str(order_id).strip() in self.orders

    def ids(self) -> list:
        return list(self.orders)

    def annotate(self, order_id, odoo_id=None, tienda=None):
        """
        Guarda datos que el script ya conoce de la orden (SO de Odoo, tienda) para
        escribirlos junto con el resultado. Se ignora si la orden no tiene VoBo.
        """
        entry = self.orders.get(str(order_id).strip())
        if not entry:
            return
        if odoo_id and odoo_id != 'N/A':
            entry['odoo_id'] = str(odoo_id)
        if tienda:
            entry['tienda_auto'] = str(tienda)

    def note(self, order_id) -> str:
        """Texto corto para dejar constancia del VoBo en los sheets originales."""
        entry = self.orders.get(str(order_id).strip())
        if not entry:
            return ''
        approver = f" ({entry['aprobador']})" if entry.get('aprobador') else ''
        return f" Reproceso por VoBo{approver}."

    # ------------------------------------------------------------------
    def load(self, client=None):
        """
        Carga las órdenes elegibles. Nunca lanza excepción: cualquier fallo deja
        la lista vacía y el flujo original corre sin cambios.
        """
        self.orders, self._ws, self._marked = {}, None, set()
        if not self.spreadsheet_id:
            logger.info("VoBo: SPREADSHEET_REPROCESS_ID no definido. Reproceso desactivado.")
            return self
        try:
            client = client or authenticate_google_sheets()
            if client is None:
                return self
            ws = client.open_by_key(self.spreadsheet_id).worksheet(self.marketplace)
            values = ws.get_all_values()
            if not values:
                return self

            # 'Marketplace ID' -> marketplace_id, '# Intentos' -> intentos
            headers = [_header(h) for h in values[0]]
            missing = [c for c in _REQUIRED_COLUMNS if c not in headers]
            if missing:
                logger.error(
                    f"VoBo: la pestaña '{self.marketplace}' no tiene las columnas "
                    f"{missing}. Reproceso desactivado."
                )
                return self
            cols = {name: idx for idx, name in enumerate(headers)}

            def cell(row, name):
                idx = cols.get(name)
                return row[idx].strip() if idx is not None and idx < len(row) else ''

            eligible = {}
            for row_number, row in enumerate(values[1:], start=2):
                order_id = cell(row, 'marketplace_id').lstrip("'").strip()
                if not order_id or cell(row, 'vobo').upper() not in _APPROVED_VALUES:
                    continue
                if cell(row, 'estado').upper() not in _ELIGIBLE_STATES:
                    continue
                try:
                    attempts = int(cell(row, 'intentos') or 0)
                except ValueError:
                    attempts = 0
                if attempts >= self.max_attempts:
                    continue
                eligible[order_id] = {
                    'row': row_number, 'intentos': attempts,
                    'aprobador': cell(row, 'aprobador'), 'tienda': cell(row, 'tienda'),
                }

            # En dry-run va como warning: es el único resultado visible con LOG_LEVEL=WARNING.
            (logger.warning if self.dry_run else logger.info)(
                f"VoBo [{self.marketplace}]: {len(eligible)} orden(es) elegible(s) para "
                f"reproceso: {', '.join(eligible) or 'ninguna'}."
            )
            if self.dry_run:
                logger.warning("VoBo: VOBO_DRY_RUN activo. No se aplica VoBo ni se escribe nada.")
                return self

            self._ws, self._cols, self.orders = ws, cols, eligible
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"VoBo: no existe la pestaña '{self.marketplace}'. Reproceso desactivado.")
        except Exception as e:
            logger.error(f"VoBo: no se pudo leer el sheet de reproceso: {e}")
        return self

    # ------------------------------------------------------------------
    def mark(self, order_id, estado: str):
        """
        Registra el resultado de la orden en el sheet de reproceso.

        - Se ignora si la orden no tiene VoBo vigente.
        - Sólo cuenta el primer resultado de la orden en la corrida (los scripts
          pueden pasar varias veces por la misma orden, p. ej. una fila por guía).
        - BLOQUEADA_VALIDACION no suma intento; GUIA_GENERADA y REINTENTO_FALLIDO sí.
        - REINTENTO_FALLIDO pasa a AGOTADA al llegar al máximo de intentos.

        Devuelve el estado finalmente escrito (o None si no se registró).
        """
        order_id = str(order_id).strip()
        entry = self.orders.get(order_id)
        if not entry or order_id in self._marked:
            return None
        self._marked.add(order_id)

        attempts = entry['intentos'] + (0 if estado == EST_BLOCKED else 1)
        if estado == EST_FAIL and attempts >= self.max_attempts:
            estado = EST_EXHAUSTED

        try:
            self._write(entry, order_id, estado, attempts)
            logger.info(f"VoBo [{self.marketplace}]: {order_id} -> {estado} (intentos={attempts})")
        except Exception as e:
            logger.error(f"VoBo [{self.marketplace}]: no se pudo registrar {order_id} -> {estado}: {e}")
        return estado

    def _write(self, entry: dict, order_id: str, estado: str, attempts: int):
        row = entry['row']
        cells = [('estado', estado), ('intentos', attempts)]
        if entry.get('odoo_id'):
            cells.append(('odoo_id', entry['odoo_id']))
        if entry.get('tienda_auto') and not entry.get('tienda'):
            cells.append(('tienda', entry['tienda_auto']))
        if estado == EST_OK:
            cells.append(('fecha_guia', _now_cdmx()))
        # Las columnas automáticas son opcionales: si no existen en la pestaña, se omiten.
        data = [{'range': gspread.utils.rowcol_to_a1(row, self._cols[name] + 1), 'values': [[value]]}
                for name, value in cells if name in self._cols]
        for attempt in (1, 2):
            try:
                id_col = self._cols['marketplace_id'] + 1
                # Si alguien insertó/borró filas durante la corrida, no se escribe a ciegas.
                if (self._ws.cell(row, id_col).value or '').strip().lstrip("'") != order_id:
                    raise ValueError(f"la fila {row} ya no corresponde a la orden {order_id}")
                self._ws.batch_update(data, value_input_option='USER_ENTERED')
                return
            except gspread.exceptions.APIError as api_err:
                if attempt == 2:
                    raise
                logger.warning(f"VoBo: error de API ({api_err}). Reintentando en 2s...")
                time.sleep(2)
