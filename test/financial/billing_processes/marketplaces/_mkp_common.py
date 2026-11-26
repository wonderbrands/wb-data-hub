"""
_mkp_common.py
--------------
Configuracion y conexiones compartidas del motor de facturacion de
marketplaces (TikTok, Coppel).

Es la misma idea que _amz_common.py: que el cambio de PRUEBAS a PRODUCCION
sea UNA variable en UN lugar. MKP_ENTORNO conmuta a la vez:

    * las tablas      finance.mkp_billing_staging  -> _prod
    * la base de Odoo ODOO_TEST_URL/ODOO_TEST_DB    -> produccion
    * el timbrado     apagado en staging
    * el candado      en 'staging' la URL tiene que ser de pruebas, y al reves

MKP elige el marketplace (TIKTOK | COPPEL). Lo que cambia entre uno y otro
(equipo, CxC, fuente de datos) vive en mkp_sources.py, igual que las tiendas
de Mercado Libre en ml_stores.py.

OdooModelProxy, OdooWriteUncertain y ejecutar_sin_retorno son copia literal
de _amz_common.py: son los blindajes del incidente del 26-jul-2026.
"""

import os
import sys
import json
import time as tm
import logging
import urllib.request
import xmlrpc.client
from datetime import datetime, timedelta, timezone
import mysql.connector
from dotenv import load_dotenv

# Busca el .env subiendo desde este archivo. En Kestra no hay .env: mandan
# las variables del flujo.
load_dotenv()

log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from mkp_sources import FUENTES


# =============================================================================
# EL INTERRUPTOR
# =============================================================================
ENTORNO = os.getenv('MKP_ENTORNO', 'staging').lower().strip()
if ENTORNO not in ('staging', 'prod'):
    raise Exception(f"MKP_ENTORNO invalido: '{ENTORNO}'. Usar 'staging' o 'prod'.")

TBL_BILLING = f'finance.mkp_billing_{ENTORNO}'
TBL_PAYMENTS = f'finance.mkp_payments_{ENTORNO}'

MKP = os.getenv('MKP', '').upper().strip()
if MKP not in FUENTES:
    raise Exception(f"MKP invalido: '{MKP}'. Opciones: {', '.join(FUENTES)}.")
FUENTE = FUENTES[MKP]
MARKETPLACE = FUENTE['marketplace']      # valor de la columna marketplace


# =============================================================================
# CONFIGURACION CONTABLE (comun a todos los marketplaces)
# =============================================================================
TAX_ID_MARKETPLACES = 38          # IVA 16% trasladado
PARTNER_ID_PUBLICO_GENERAL = 13436       # XAXX010101000

# Transitoria del costo: la factura la acredita y la salida de almacen (OUT)
# la debita. Igual que Amazon: se factura sin esperar el OUT.
ACCOUNT_MERC_NO_ENVIADA = os.getenv('MKP_ACCOUNT_MERC_NO_ENVIADA', '205.06.003').strip()

# Prefijo de idempotencia. DELIBERADAMENTE el mismo de facturacion_automatica_1a1.py
# y de 01_amz_billing.py: los procesos se ven entre si y ninguno factura lo
# que otro ya facturo.
IDEMPOTENCY_PREFIX = 'AUTOINV:'

UTC_local = -6   # CDMX. Mexico no aplica horario de verano desde 2022.


# =============================================================================
# ENTORNO DE ODOO
# =============================================================================
if ENTORNO == 'staging':
    ODOO_URL = (os.getenv('ODOO_TEST_URL') or '').strip()
    ODOO_DB = (os.getenv('ODOO_TEST_DB') or '').strip()
    ODOO_USER = os.getenv('ODOO_TEST_USER') or os.getenv('odoo_user_dataV18')
    ODOO_PWD = os.getenv('ODOO_TEST_PASSWORD') or os.getenv('odoo_password_dataV18')
else:
    ODOO_URL = (os.getenv('ODOO_URL') or os.getenv('odoo_urlV18') or '').strip()
    ODOO_DB = (os.getenv('ODOO_DB') or os.getenv('odoo_dbV18') or '').strip()
    ODOO_USER = os.getenv('ODOO_USER') or os.getenv('odoo_user_dataV18')
    ODOO_PWD = os.getenv('ODOO_PASSWORD') or os.getenv('odoo_password_dataV18')

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASSWORD', '')

SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK')
KESTRA_EXECUTION_ID = os.getenv('KESTRA_EXECUTION_ID', 'local')


def validar_entorno():
    """Candado: staging solo contra un Odoo de pruebas; prod nunca contra uno."""
    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD]):
        raise Exception(f"Faltan credenciales de Odoo para ENTORNO='{ENTORNO}'.")
    url = ODOO_URL.lower()
    parece_pruebas = ('test' in url) or ('.dev.odoo.com' in url)
    if ENTORNO == 'staging' and not parece_pruebas:
        raise Exception(f"ABORTADO: ENTORNO='staging' pero la URL '{ODOO_URL}' no "
                        f"parece de pruebas. Revisar ODOO_TEST_URL.")
    if ENTORNO == 'prod' and parece_pruebas:
        raise Exception(f"ABORTADO: ENTORNO='prod' pero la URL '{ODOO_URL}' parece "
                        f"de pruebas. Revisar ODOO_URL.")


def banner(titulo):
    log.info('=' * 82)
    log.info(titulo)
    log.info(f"Marketplace: {MARKETPLACE} | Entorno: {ENTORNO.upper()} | Odoo: {ODOO_DB}")
    log.info(f"Tabla: {TBL_BILLING}")
    log.info('=' * 82)


def notify_slack(text):
    prefijo = '' if ENTORNO == 'prod' else '[PRUEBAS] '
    if not SLACK_WEBHOOK:
        log.warning(f"[SLACK no configurado] {prefijo}{text}")
        return
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK,
            data=json.dumps({'text': prefijo + text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.error(f"No se pudo notificar a Slack: {e}")


def get_db_connection():
    return mysql.connector.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                                   database='finance')


def ahora_cdmx():
    """Hora de CDMX sin depender de la zona del servidor (Kestra corre en UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_local)


def a_float(v):
    """Decimal / str / None -> float."""
    if v is None or v is False:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# =============================================================================
# ODOO (copia de _amz_common.py)
# =============================================================================

class OdooWriteUncertain(Exception):
    """Una ESCRITURA en Odoo fallo por red: se desconoce si hubo commit. Nunca
    se reintenta a ciegas; se reconcilia leyendo."""


class TimeoutTransport(xmlrpc.client.SafeTransport):
    """Fuerza un timeout en XML-RPC para evitar procesos colgados."""

    def __init__(self, timeout=300, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class OdooModelProxy:
    """Llamadas a Odoo con timeout, renovacion de sesion y reintento SOLO de
    lecturas. Reintentar una escritura tras un 502 puede generar una segunda
    factura (incidente del 26-jul-2026)."""

    READ_METHODS = ('search', 'search_read', 'read', 'search_count',
                    'read_group', 'fields_get')

    def __init__(self, timeout=300):
        self.timeout = timeout
        self._connect()

    def _connect(self):
        self.common = xmlrpc.client.ServerProxy(
            f'{ODOO_URL}/xmlrpc/2/common', transport=TimeoutTransport(self.timeout))
        self.uid = self.common.authenticate(ODOO_DB, ODOO_USER, ODOO_PWD, {})
        if not self.uid:
            raise Exception(f"No se pudo autenticar contra Odoo ({ODOO_DB}).")
        self.models = xmlrpc.client.ServerProxy(
            f'{ODOO_URL}/xmlrpc/2/object', transport=TimeoutTransport(self.timeout))

    def reauthenticate(self):
        log.info("Renovando sesion con Odoo...")
        self._connect()

    def execute_kw(self, model, method, args, kwargs=None, max_retries=3, delay=2):
        is_read = method in self.READ_METHODS
        for attempt in range(1, max_retries + 1):
            try:
                if kwargs is not None:
                    return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PWD,
                                                  model, method, args, kwargs)
                return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PWD,
                                              model, method, args)
            except xmlrpc.client.Fault as e:
                raise e          # error de negocio: Odoo hizo rollback
            except (xmlrpc.client.ProtocolError, TimeoutError, OSError) as e:
                if not is_read:
                    raise OdooWriteUncertain(f"{model}.{method}: {e}")
                log.warning(f"Red en lectura [{model}.{method}] {attempt}/{max_retries}: {e}")
                if attempt == max_retries:
                    raise e
                tm.sleep(delay * attempt)
                try:
                    self.reauthenticate()
                except Exception as auth_e:
                    log.warning(f"Fallo al reautenticar: {auth_e}")
            except Exception as e:
                if not is_read:
                    raise OdooWriteUncertain(f"{model}.{method}: {e}")
                if attempt == max_retries:
                    raise e
                tm.sleep(delay * attempt)


def get_account_id(odoo, code):
    acc = odoo.execute_kw('account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"No existe la cuenta contable {code} en {ODOO_DB}.")
    return acc[0]


def ejecutar_sin_retorno(odoo, model, method, ids):
    """reconcile / remove_move_reconcile hacen su trabajo y fallan al responder
    ("cannot marshal None"). Ese Fault se toma como exito; quien llama
    confirma leyendo."""
    try:
        odoo.execute_kw(model, method, [ids])
    except xmlrpc.client.Fault as e:
        if 'cannot marshal None' not in (e.faultString or ''):
            raise
    return True
