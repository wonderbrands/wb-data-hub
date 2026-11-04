"""
_amz_common.py
--------------
Configuracion y conexiones compartidas de los procesos de Amazon.

POR QUE EXISTE ESTE MODULO
--------------------------
No es una capa de abstraccion: es un archivo de configuracion. Su unica razon
de ser es que el cambio de PRUEBAS a PRODUCCION sea UNA linea en UN lugar.

Cambiar ENTORNO de 'staging' a 'prod' conmuta las tres cosas a la vez:

    * las tablas          finance.mkp_billing_staging  -> _prod
                          finance.mkp_payments_staging -> _prod
    * la base de Odoo     somosreyes-test...  -> produccion
    * el candado          en 'staging' se verifica que la URL sea de pruebas

Si cada script llevara su propia copia, un despliegue a medias dejaria un
script escribiendo en staging y otro en prod. Con esto, o estan los tres en
pruebas o estan los tres en produccion.

Sigue el mismo patron de import que processes/.../shipping_labels/_shared/.
"""

import os
import sys
import json
import time as tm
import logging
import urllib.parse
import urllib.request
import xmlrpc.client
import mysql.connector
from dotenv import load_dotenv

# Busca el .env subiendo desde este archivo (en local, el de la raiz del
# repo). En Kestra no hay .env: mandan las variables de entorno del flujo.
load_dotenv()

log = logging.getLogger(__name__)


# =============================================================================
# EL INTERRUPTOR
# =============================================================================
# 'staging' -> tablas _staging + Odoo de pruebas
# 'prod'    -> tablas _prod    + Odoo de produccion
#
# Se puede sobreescribir por variable de entorno para que Kestra lo inyecte
# sin tocar el codigo.
# =============================================================================

ENTORNO = os.getenv('AMZ_ENTORNO', 'staging').lower().strip()

if ENTORNO not in ('staging', 'prod'):
    raise Exception(f"AMZ_ENTORNO invalido: '{ENTORNO}'. Usar 'staging' o 'prod'.")

TBL_BILLING = f'finance.mkp_billing_{ENTORNO}'
TBL_PAYMENTS = f'finance.mkp_payments_{ENTORNO}'

MARKETPLACE = 'AMAZON'


# =============================================================================
# CONFIGURACION CONTABLE
# =============================================================================

TAX_ID_MARKETPLACES        = 38          # IVA 16% trasladado
PARTNER_ID_PUBLICO_GENERAL = 13436       # XAXX010101000
ACCOUNT_AMAZON_CXC         = '105.01.006'
ACCOUNT_LIBERACIONES       = '102.02.008'  # Liberaciones Amazon Pend. de Procesar
ACCOUNT_DEV_TRANSITO       = '107.06.001'  # Devoluciones en Transito

# Transitoria del costo (contabilidad anglosajona): la factura la acredita y
# la salida de almacen (OUT) la debita. Odoo concilia las dos partidas solo
# cuando ninguna esta conciliada con otra cosa; al refacturar, la salida
# seguia conciliada con la factura cancelada (correo de Jero, 28-oct-2026:
# 44 de 48 facturas del lote). 01_amz_billing.py rehace esa conciliacion.
ACCOUNT_MERC_NO_ENVIADA = os.getenv('AMZ_ACCOUNT_MERC_NO_ENVIADA', '205.06.003').strip()

# Excedente de promociones financiadas por Amazon. Decision de Finanzas
# (correo de Jero, oct-2026, sobre el lote 4): la parte sin IVA es ingreso en
# 403.01.002 y el IVA va a 208.01.001. Ejemplo 701-0635257-8905020: Amazon
# libero 3,200.00 sobre una factura de 2,900.00 -> 258.62 + 41.38.
# Solo para promociones CONFIRMADAS como pagadas por Amazon, no para cualquier
# diferencia (ver clasificar_excedente en 03_loader_amz_payments.py).
ACCOUNT_CUPONES_AMAZON     = '403.01.002'  # CUPONES AMAZON
ACCOUNT_IVA_TRASLADADO     = '208.01.001'  # IVA TRASLADADO COBRADO

# Cuenta para saldar diferencias de 1 centavo entre lo liberado por Amazon y
# la factura (redondeo del IVA de Odoo frente al precio con IVA de Amazon).
# 704.23.001 OTROS PRODUCTOS: confirmada por Finanzas para produccion
# (correo de Jero, 28-oct-2026, segundo). 03_loader_amz_payments.py sigue abortando si
# llega vacia: en el lote 5 la variable no llego (sintaxis de bash en
# PowerShell) y dos facturas quedaron en pago parcial por 0.01.
ACCOUNT_REDONDEO = os.getenv('AMZ_ACCOUNT_REDONDEO', '704.23.001').strip()

PAYMENT_METHOD_TRANSFERENCIA = 3           # l10n_mx_edi_payment_method_id

# Diario de Odoo para los cobros de Amazon.
# Se deja VACIO a proposito: el diario existe en Odoo pero no tiene un codigo
# corto evidente, asi que el loader lo localiza por su cuenta por defecto
# (ACCOUNT_LIBERACIONES) con find_journal_by_account(). Solo hace falta llenar
# esta variable si en algun momento hay dos diarios apuntando a la misma
# cuenta y hay que desempatar.
AMZ_JOURNAL_CODE = os.getenv('AMZ_JOURNAL_CODE', '').strip()

# Equipo de ventas de Amazon en Odoo. Se resuelve por coincidencia de nombre
# para no hardcodear ids que cambian entre bases.
TEAM_AMAZON_KEYWORD = 'Amazon'

# Prefijo de idempotencia. DELIBERADAMENTE el mismo que
# facturacion_automatica_1a1.py: durante la transicion los dos procesos se ven
# mutuamente y ninguno puede facturar una orden que el otro ya facturo.
IDEMPOTENCY_PREFIX = 'AUTOINV:'

UTC_local = -6   # CDMX. Mexico no aplica horario de verano desde 2022.


# =============================================================================
# ENTORNO DE ODOO
# =============================================================================

if ENTORNO == 'staging':
    ODOO_URL = os.getenv('ODOO_TEST_URL', 'https://somosreyes-test-05-10-2026-39264934.dev.odoo.com')
    ODOO_DB = os.getenv('ODOO_TEST_DB', 'somosreyes-test-05-10-2026-39264934')
    ODOO_USER = os.getenv('ODOO_TEST_USER') or os.getenv('odoo_user_dataV18')
    ODOO_PWD = os.getenv('ODOO_TEST_PASSWORD') or os.getenv('odoo_password_dataV18')
else:
    ODOO_URL = os.getenv('ODOO_URL') or os.getenv('odoo_urlV18')
    ODOO_DB = os.getenv('ODOO_DB') or os.getenv('odoo_dbV18')
    ODOO_USER = os.getenv('ODOO_USER') or os.getenv('odoo_user_dataV18')
    ODOO_PWD = os.getenv('ODOO_PASSWORD') or os.getenv('odoo_password_dataV18')

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASSWORD', '')

SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK')
KESTRA_EXECUTION_ID = os.getenv('KESTRA_EXECUTION_ID', 'local')


def validar_entorno():
    """Candado. Si ENTORNO es 'staging', la URL de Odoo TIENE que ser de
    pruebas. Evita que una variable de entorno mal puesta escriba facturas
    reales creyendo estar en pruebas."""
    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD]):
        raise Exception(f"Faltan credenciales de Odoo para ENTORNO='{ENTORNO}'.")

    url = ODOO_URL.lower()
    parece_pruebas = ('test' in url) or ('.dev.odoo.com' in url)

    if ENTORNO == 'staging' and not parece_pruebas:
        raise Exception(
            f"ABORTADO: ENTORNO='staging' pero la URL '{ODOO_URL}' no parece "
            f"de pruebas. Revisar ODOO_TEST_URL.")

    if ENTORNO == 'prod' and parece_pruebas:
        raise Exception(
            f"ABORTADO: ENTORNO='prod' pero la URL '{ODOO_URL}' parece de "
            f"pruebas. Revisar ODOO_URL.")

    log.info(f"ENTORNO={ENTORNO} | Odoo={ODOO_DB}")
    log.info(f"Tablas: {TBL_BILLING} / {TBL_PAYMENTS}")


def banner(titulo):
    log.info('=' * 82)
    log.info(titulo)
    log.info(f"Entorno: {ENTORNO.upper()} | Odoo: {ODOO_DB}")
    log.info(f"Tablas:  {TBL_BILLING} | {TBL_PAYMENTS}")
    log.info('=' * 82)


# =============================================================================
# ALERTAS
# =============================================================================

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


# =============================================================================
# MYSQL
# =============================================================================

def get_db_connection():
    return mysql.connector.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database='finance')


# =============================================================================
# SP-API (SOLO LECTURA)
# =============================================================================
# El access_token lo renueva el ETL de Amazon y lo deja en amz_tokens; aqui
# solo se lee. Mismo patron que amz_transactions.py, con urllib en vez de
# requests para no sumar dependencias a este modulo.

SP_API_ENDPOINT = 'https://sellingpartnerapi-na.amazon.com'


def sp_api_token(db_conn):
    cursor = db_conn.cursor()
    cursor.execute("SELECT access_token FROM somos_reyes.amz_tokens LIMIT 1")
    fila = cursor.fetchone()
    cursor.close()
    if not fila or not fila[0]:
        raise Exception("amz_tokens no tiene access_token.")
    return fila[0]


def sp_api_get(token, path, timeout=30):
    """GET a SP-API. Devuelve el JSON decodificado; lanza en cualquier error."""
    req = urllib.request.Request(
        SP_API_ENDPOINT + path,
        headers={'x-amz-access-token': token, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def get_order_items(token, order_id):
    """Lineas de la orden (Orders API v0 getOrderItems), con paginacion.

    De aqui sale PromotionDiscount, el unico dato por linea que dice cuanto
    descuento de promocion recibio el cliente. Ni amazon_orders ni
    amz_transaction_items lo traen.
    """
    items, token_pag = [], None
    while True:
        path = f"/orders/v0/orders/{order_id}/orderItems"
        if token_pag:
            path += '?NextToken=' + urllib.parse.quote(token_pag)
        payload = sp_api_get(token, path).get('payload') or {}
        items += payload.get('OrderItems') or []
        token_pag = payload.get('NextToken')
        if not token_pag:
            return items
        tm.sleep(2.1)   # getOrderItems: 0.5 req/s


# =============================================================================
# ODOO
# =============================================================================

class OdooWriteUncertain(Exception):
    """Una ESCRITURA en Odoo fallo por red: se desconoce si hubo commit.

    Distingue "Odoo rechazo la operacion" (xmlrpc Fault: hubo rollback, no hay
    factura) de "no se sabe si Odoo la aplico" (502 / timeout: puede haberla).
    La primera se da por fallida; la segunda JAMAS se reintenta a ciegas, se
    reconcilia leyendo.
    """


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
    """Llamadas a Odoo con timeout, renovacion de sesion TLS y reintento
    SOLO de lecturas.

    Reintentar una escritura tras un 502 puede generar una segunda factura:
    es la causa raiz del incidente del 26-jul-2026.
    """

    READ_METHODS = ('search', 'search_read', 'read', 'search_count',
                    'read_group', 'fields_get')

    def __init__(self, timeout=300):
        self.timeout = timeout
        self._connect()

    def _connect(self):
        self.common = xmlrpc.client.ServerProxy(
            f'{ODOO_URL}/xmlrpc/2/common',
            transport=TimeoutTransport(self.timeout))
        self.uid = self.common.authenticate(ODOO_DB, ODOO_USER, ODOO_PWD, {})
        if not self.uid:
            raise Exception(f"No se pudo autenticar contra Odoo ({ODOO_DB}).")
        self.models = xmlrpc.client.ServerProxy(
            f'{ODOO_URL}/xmlrpc/2/object',
            transport=TimeoutTransport(self.timeout))

    def reauthenticate(self):
        log.info("Renovando sesion TLS con Odoo...")
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
                # Error de negocio: Odoo hizo rollback. No se reintenta.
                raise e

            except (xmlrpc.client.ProtocolError, TimeoutError, OSError) as e:
                if not is_read:
                    raise OdooWriteUncertain(f"{model}.{method}: {e}")
                log.warning(f"Red en lectura [{model}.{method}] "
                            f"{attempt}/{max_retries}: {e}")
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
    acc = odoo.execute_kw('account.account', 'search',
                          [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"No existe la cuenta contable {code} en {ODOO_DB}.")
    return acc[0]


def ejecutar_sin_retorno(odoo, model, method, ids):
    """Llama un metodo de Odoo que no devuelve nada (reconcile,
    remove_move_reconcile...).

    Por XML-RPC esos metodos YA HICIERON SU TRABAJO y luego fallan al armar la
    respuesta: "cannot marshal None unless allow_none is enabled". Odoo hace
    commit antes de serializar. Verificado el 28-oct-2026 en el espejo: la
    conciliacion de SO10307197 quedo hecha aunque la llamada devolvio Fault.
    Ese Fault concreto se toma como exito; cualquier otro sube. Quien llama
    debe confirmar el resultado leyendo.
    """
    try:
        odoo.execute_kw(model, method, [ids])
    except xmlrpc.client.Fault as e:
        if 'cannot marshal None' not in (e.faultString or ''):
            raise
    return True


def get_amazon_team_ids(odoo):
    teams = odoo.execute_kw('crm.team', 'search_read',
                            [[('name', 'ilike', TEAM_AMAZON_KEYWORD)]],
                            {'fields': ['id', 'name']})
    if not teams:
        raise Exception(f"No hay equipos de venta con "
                        f"'{TEAM_AMAZON_KEYWORD}' en {ODOO_DB}.")
    for t in teams:
        log.info(f"  Equipo Amazon: {t['name']} (id={t['id']})")
    return [t['id'] for t in teams]


# =============================================================================
# UTILIDADES
# =============================================================================

def a_float(v):
    """Decimal / str / None -> float. Evita repetir el try en cada script."""
    from decimal import Decimal
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalizar_uuid(valor):
    if not valor or valor is False:
        return None
    return str(valor).strip().upper()


# =============================================================================
# CLAVE DE COBRO
# =============================================================================
# finance.mkp_payments_* tiene UNIQUE KEY uk_payment (marketplace, payment_id),
# SIN mkp_order_id.
#
# En Mercado Libre eso basta: payment_id es el id del pago de Mercado Pago, uno
# por orden. En Amazon el identificador del deposito es el settlement_id, y un
# settlement agrupa CIENTOS de ordenes: guardar solo el settlement haria que
# unicamente la primera orden entrara y el resto chocara con la unique key.
#
# La solucion es meter las dos partes en payment_id. Asi la clave es
# exactamente la unidad de cobro (una orden, en un settlement, una vez) y de
# paso queda la proteccion contra el doble cobro sin tocar el esquema.
#
# El separador es ':' porque el order_id de Amazon ya contiene guiones
# (702-xxxxxxx-xxxxxxx): partir por el primer ':' es inequivoco.
# =============================================================================

PAYMENT_ID_SEP = ':'


def build_payment_id(settlement_id, order_id):
    return f"{settlement_id}{PAYMENT_ID_SEP}{order_id}"


def parse_settlement_id(payment_id):
    """Recupera el settlement_id de un payment_id compuesto.

    Tolera valores sin separador (por si alguna fila quedo del formato
    anterior): en ese caso devuelve el valor completo.
    """
    if not payment_id:
        return None
    return str(payment_id).split(PAYMENT_ID_SEP, 1)[0]


def find_journal_by_account(odoo, account_code):
    """Localiza el diario cuya cuenta por defecto es account_code.

    Se busca POR CUENTA y no por codigo de diario a proposito: el diario de
    Amazon existe en Odoo pero no tiene un codigo corto evidente, y adivinarlo
    es fragil. La cuenta si es un dato firme y validado por Finanzas
    (102.02.008 LIBERACIONES AMAZON PENDIENTES DE PROCESAR).

    Si el diario apuntara a otra cuenta, los cobros de Amazon acabarian en
    bancos y el descuadre no se descubriria hasta la conciliacion. Por eso
    esta funcion falla en vez de adivinar.
    """
    acc = odoo.execute_kw('account.account', 'search_read',
                          [[('code', '=', account_code)]],
                          {'fields': ['id', 'code', 'name'], 'limit': 1})
    if not acc:
        raise Exception(f"No existe la cuenta {account_code} en {ODOO_DB}.")

    journals = odoo.execute_kw(
        'account.journal', 'search_read',
        [[('default_account_id', '=', acc[0]['id'])]],
        {'fields': ['id', 'code', 'name', 'type']})

    if not journals:
        raise Exception(
            f"Ningun diario de Odoo tiene como cuenta por defecto "
            f"{account_code} ({acc[0]['name']}). Sin ese diario los cobros de "
            f"Amazon no pueden registrarse en la transitoria. "
            f"Pedir a Finanzas que lo configure.")

    if len(journals) > 1:
        nombres = ', '.join(f"{j['name']} ({j['code']})" for j in journals)
        raise Exception(
            f"Hay {len(journals)} diarios apuntando a {account_code}: "
            f"{nombres}. Ambiguo: definir con Finanzas cual usar y fijarlo en "
            f"AMZ_JOURNAL_CODE.")

    j = journals[0]
    log.info(f"Diario localizado por cuenta: {j['name']} "
             f"(code={j['code']}, id={j['id']}, type={j['type']}) "
             f"-> {acc[0]['code']} {acc[0]['name']}")
    return j['id']
