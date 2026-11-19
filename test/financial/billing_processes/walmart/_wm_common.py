"""
_wm_common.py
-------------
Configuracion, conexiones y funciones de Odoo compartidas por los scripts de
facturacion de Walmart 3P (Team_Walmart).

Plantilla: amazon/_amz_common.py. Igual que alla, no es una capa de
abstraccion: es la configuracion. El cambio de PRUEBAS a PRODUCCION es UNA
variable (WM_ENTORNO) y conmuta a la vez:

    * las tablas        finance.mkp_billing_staging -> _prod
    * la base de Odoo   ODOO_TEST_URL -> produccion
    * el timbrado       apagado en pruebas
    * el candado        en 'staging' la URL tiene que ser de pruebas (y al reves)

Las funciones de Odoo de la segunda mitad (facturas existentes, timbrado,
conciliacion de la 205.06.003, cuadre contra el CFDI) son las de
amazon/01_amz_billing.py, copiadas sin cambios de logica. Cuando Walmart pase a
produccion conviene moverlas a un modulo comun de billing_processes.

Datos de Walmart (somos_reyes), medidos el 10/12-nov-2026:
    wm_financial_transactions_master (FTM)  una fila por LINEA de orden.
        type='Order' es la venta. Clave unica: so_prime_line_sub_line_no.
        order_date ya viene en hora de Mexico (Playbook v2.6, 3.3).
    wm_fenix_invoices_report (Fenix)        CFDI que emitio el autofacturador
        de Walmart con nuestro RFC. UN CFDI POR PIEZA: cada UUID vigente es
        una fila con cantidad=1 y una linea de 5 piezas trae 5 UUID.
        iderp = so_prime_line_sub_line_no de FTM.
"""

import os
import sys
import json
import base64
import logging
import urllib.request
import xmlrpc.client
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


# =============================================================================
# EL INTERRUPTOR
# =============================================================================

ENTORNO = os.getenv('WM_ENTORNO', 'staging').lower().strip()
if ENTORNO not in ('staging', 'prod'):
    raise Exception(f"WM_ENTORNO invalido: '{ENTORNO}'. Usar 'staging' o 'prod'.")

TBL_BILLING = f'finance.mkp_billing_{ENTORNO}'
MARKETPLACE = 'WALMART'

# Timbrado de las facturas que emite WB. En pruebas se deja apagado: publicar
# ya genera el asiento, que es lo que Finanzas revisa.
DO_STAMPING = (ENTORNO == 'prod')


# =============================================================================
# CONFIGURACION CONTABLE Y FISCAL
# =============================================================================

TAX_ID_MARKETPLACES = 38                    # IVA 16% trasladado
PARTNER_ID_PUBLICO_GENERAL = 13436          # XAXX010101000
ACCOUNT_WALMART_CXC = '105.01.007'
ACCOUNT_MERC_NO_ENVIADA = os.getenv('WM_ACCOUNT_MERC_NO_ENVIADA', '205.06.003').strip()
PAYMENT_METHOD_TRANSFERENCIA = 3            # l10n_mx_edi_payment_method_id (03)

TEAM_WALMART = 'Team_Walmart'               # nombre exacto: NO Team_Walmart_1P

# RFC con el que el autofacturador de Walmart emite (Fenix.rfc_emisor). Un XML
# con otro emisor no es nuestro y no se carga.
RFC_EMISOR = os.getenv('WM_RFC_EMISOR', 'SRE1410278MA').strip().upper()

# Prefijo de idempotencia COMPARTIDO con el 1a1 y con Amazon a proposito: cada
# proceso ve las facturas de los otros.
#   AUTOINV:<SO>          factura que emite WB (una por SO)
#   AUTOINV:<SO>:<UUID>   factura que replica un CFDI de Walmart (una por pieza)
IDEMPOTENCY_PREFIX = 'AUTOINV:'

# Contacto generico para clientes nuevos que llegan en el CFDI de Walmart.
# Odoo no deja crear un contacto sin telefono ni correo (lote del 27-oct de
# Amazon). Mismos valores que Amazon / Yuju.
CLIENTE_TELEFONO_GENERICO = os.getenv('WM_CLIENTE_TELEFONO', '11111')
CLIENTE_CORREO_GENERICO = os.getenv('WM_CLIENTE_CORREO', 'market@somos-reyes.com')

UTC_local = -6   # CDMX. Mexico no aplica horario de verano desde 2022.

# Un peso, igual que Mercado Libre y Amazon.
TOLERANCIA_TOTAL = float(os.getenv('WM_TOLERANCIA_TOTAL', '1.00'))
# Ajuste de centavos contra el CFDI de Walmart (solo facturas que NO timbramos).
TOLERANCIA_CUADRE_CFDI = 0.05


# =============================================================================
# ESTADOS DE finance.mkp_billing_* (marketplace = 'WALMART')
# =============================================================================
# Filas:
#   mkp_order_id = <customer_order_id>          una por orden (WB o en espera)
#   mkp_order_id = <customer_order_id>:<UUID>   una por CFDI de Walmart (pieza)
#
# Pendientes que toma un loader
ST_PENDING_WB = 'PENDING_WB_INVOICE'     # la emite WB (04)
ST_PENDING_WM_XML = 'PENDING_WM_XML'     # CFDI de Walmart, falta su XML (02)
ST_PENDING_WM_LINK = 'PENDING_WM_LINK'   # CFDI de Walmart con XML: se replica (03)
ST_PROCESSING = 'PROCESSING'

# En espera de una persona o de un dato (el miner las reevalua)
ST_NO_SO = 'NO_SO'                        # Walmart vendio y no hay SO (Yuju la crea)
ST_SO_DUPLICADA = 'SO_DUPLICADA'          # mas de una SO viva para la orden
ST_SO_OTRO_EQUIPO = 'SO_OTRO_EQUIPO'      # la SO no es de Team_Walmart
ST_SO_CANCELADA = 'SO_CANCELADA'          # decision pendiente de Finanzas
ST_ORDEN_MIXTA = 'ORDEN_MIXTA'            # piezas con CFDI de Walmart y piezas sin
ST_WM_REVISAR = 'WM_CFDI_REVISAR'         # Fenix en ERROR/NUEVO/CANCELANDO/CANCELADO sin vigente
ST_WM_EXCEDENTE = 'WM_CFDI_EXCEDENTE'     # mas CFDI de Walmart que piezas vendidas
ST_DOBLE_CFDI = 'DOBLE_CFDI'              # CFDI de Walmart y otra factura viva en la SO
ST_RECLASIFICAR = 'RECLASIFICAR'          # 04 vio Fenix nuevo antes de timbrar
ST_MANUAL_SERIE = 'FACTURA_MANUAL_SERIE'  # bici electrica: factura CS/Finanzas con numero de serie
ST_BORRADOR_AJENO = 'BORRADOR_AJENO'      # la SO tiene un borrador de otro proceso (sin CFDI):
                                          # Finanzas lo cancela y la orden se reevalua

# Bicis electricas (correo de Jero, nov-2026): por disposicion legal se facturan
# con el numero de serie, que solo se conoce al abrir la caja. Walmart las
# bloqueo en su portal y las deriva a Somos Reyes; mientras no exista un
# mecanismo, las emiten a mano Customer Service y Finanzas. Si Walmart ya
# emitio su CFDI, se replica igual (03). Prefijos de SKU, separados por coma.
SKUS_FACTURA_MANUAL = tuple(s.strip().upper() for s in
                            os.getenv('WM_SKUS_FACTURA_MANUAL', 'EBIKE350,EBIKE500,EX-1').split(',')
                            if s.strip())

# Finales
ST_WB_INVOICED = 'WB_INVOICED'            # factura de WB publicada (y timbrada en prod)
ST_WM_LINKED = 'WM_LINKED'                # CFDI de Walmart replicado en Odoo
ST_ALREADY = 'ALREADY_ODOO_INVOICED'      # ya tenia factura (proceso manual, global...)
ST_DESCARTADA = 'DESCARTADA'              # la fila ya no aplica (cambio la clasificacion)
ST_TOTAL_DIFF = 'TOTAL_DIFF'              # no cuadra contra Walmart: revisa Finanzas
ST_XML_INVALIDO = 'XML_INVALIDO'          # el XML no es de esta fila o no es nuestro
ST_ERROR = 'ERROR'                        # se reintenta hasta 3 veces (retry_count_loader)

# Las que el miner puede reescribir en cada corrida. El resto queda cerrado.
REFRESCABLES = (ST_PENDING_WB, ST_PENDING_WM_XML, ST_PENDING_WM_LINK,
                ST_NO_SO, ST_SO_DUPLICADA, ST_SO_OTRO_EQUIPO, ST_SO_CANCELADA,
                ST_ORDEN_MIXTA, ST_WM_REVISAR, ST_WM_EXCEDENTE, ST_DOBLE_CFDI,
                ST_RECLASIFICAR, ST_MANUAL_SERIE, ST_BORRADOR_AJENO)

STUCK_PROCESSING_MIN_AGE_HOURS = 6


# =============================================================================
# ENTORNO DE ODOO Y MYSQL
# =============================================================================

if ENTORNO == 'staging':
    ODOO_URL = os.getenv('ODOO_TEST_URL')
    ODOO_DB = os.getenv('ODOO_TEST_DB')
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

# Tope de facturas creadas por corrida (03 + 04 por separado). En pruebas 20
# por defecto; en produccion sin tope salvo que Kestra lo mande.
_tope = os.getenv('WM_MAX_FACTURAS', '').strip()
MAX_FACTURAS = int(_tope) if _tope else (None if ENTORNO == 'prod' else 20)


def configurar_log():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s | %(levelname)s | %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])


def validar_entorno():
    """Candado. 'staging' exige URL de pruebas y 'prod' lo contrario."""
    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD]):
        raise Exception(f"Faltan credenciales de Odoo para WM_ENTORNO='{ENTORNO}'.")
    url = ODOO_URL.lower()
    parece_pruebas = ('test' in url) or ('.dev.odoo.com' in url)
    if ENTORNO == 'staging' and not parece_pruebas:
        raise Exception(f"ABORTADO: WM_ENTORNO='staging' pero la URL '{ODOO_URL}' "
                        f"no parece de pruebas. Revisar ODOO_TEST_URL.")
    if ENTORNO == 'prod' and parece_pruebas:
        raise Exception(f"ABORTADO: WM_ENTORNO='prod' pero la URL '{ODOO_URL}' "
                        f"parece de pruebas. Revisar ODOO_URL.")
    log.info(f"WM_ENTORNO={ENTORNO} | Odoo={ODOO_DB} | Tabla={TBL_BILLING}")


def banner(titulo):
    log.info('=' * 82)
    log.info(titulo)
    log.info(f"Entorno: {ENTORNO.upper()} | Odoo: {ODOO_DB} | Tabla: {TBL_BILLING}")
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


# =============================================================================
# FECHAS (CDMX desde UTC; Kestra corre en UTC)
# =============================================================================

def ahora_cdmx():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_local)


# El dia de la corrida se fija UNA vez al arrancar (incidente del 3-nov-2026 en
# Amazon: una corrida de cierre que paso la medianoche facturo 1,039 ventas de
# octubre con fecha de noviembre).
HOY_CORRIDA = ahora_cdmx()

# Dias del mes en que la venta del mes anterior sale con fecha del ultimo dia
# de ese mes. Walmart corre una vez al dia: dias 1 y 2. Nunca mas de 3 (72 h).
DIAS_CIERRE_MES = int(os.getenv('WM_DIAS_CIERRE_MES', '2'))
if not 0 <= DIAS_CIERRE_MES <= 3:
    raise Exception(f"WM_DIAS_CIERRE_MES={DIAS_CIERRE_MES}: debe estar entre 0 y 3 "
                    f"(el SAT no timbra con fecha de mas de 72 horas).")


def inicio_de_mes(hoy=None):
    return (hoy or HOY_CORRIDA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def en_cierre_de_mes(hoy=None):
    return (hoy or HOY_CORRIDA).day <= DIAS_CIERRE_MES


def limite_72h(hoy=None):
    """Ultimo momento para timbrar con fecha del ultimo dia del mes anterior
    (23:59:59 + 72 h), con 20 min de margen."""
    return inicio_de_mes(hoy) + timedelta(days=3) - timedelta(minutes=20)


def a_datetime(valor):
    if isinstance(valor, datetime):
        return valor
    return datetime.strptime(str(valor)[:19], '%Y-%m-%d %H:%M:%S')


# =============================================================================
# UTILIDADES
# =============================================================================

def a_float(v):
    if v is None or v is False:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalizar_uuid(valor):
    """Mayusculas y sin el sufijo del nombre de archivo. Las facturas del proceso
    manual (invoice_creation_qtyV18) guardaron el UUID tal como venia en el
    nombre del XML: '78EB8339-...-1B273172F15C@3000000000XX0' (SO10187391)."""
    if not valor or valor is False:
        return None
    return str(valor).strip().split('@', 1)[0].upper()


def a_json(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def lotes(seq, tam):
    for i in range(0, len(seq), tam):
        yield seq[i:i + tam]


def sku_de_producto(product_field):
    """'[KH3PLGBL] Nombre' -> 'KH3PLGBL'. product_id de XML-RPC es [id, nombre]."""
    if not product_field:
        return None
    nombre = product_field[1] if isinstance(product_field, (list, tuple)) else str(product_field)
    if nombre.startswith('[') and ']' in nombre:
        return nombre[1:nombre.index(']')].strip().upper()
    return None


# =============================================================================
# CFDI (XML del SAT)
# =============================================================================
# Solo lectura con la libreria estandar. ElementTree no resuelve entidades
# externas, asi que un XML malicioso no puede leer archivos locales.

NS_CFDI = {'cfdi': 'http://www.sat.gob.mx/cfd/4',
           'tfd': 'http://www.sat.gob.mx/TimbreFiscalDigital'}


def leer_cfdi(xml_bytes):
    """Extrae del XML lo que el loader necesita. Lanza si no es un CFDI 4.0."""
    root = ET.fromstring(xml_bytes)
    if not root.tag.endswith('}Comprobante'):
        raise ValueError('no es un cfdi:Comprobante')
    ns = {'cfdi': root.tag[1:root.tag.index('}')], 'tfd': NS_CFDI['tfd']}

    emisor = root.find('cfdi:Emisor', ns)
    receptor = root.find('cfdi:Receptor', ns)
    timbre = root.find('.//tfd:TimbreFiscalDigital', ns)
    if emisor is None or receptor is None or timbre is None:
        raise ValueError('faltan Emisor, Receptor o TimbreFiscalDigital')

    conceptos = []
    for c in root.findall('cfdi:Conceptos/cfdi:Concepto', ns):
        conceptos.append({
            'cantidad': a_float(c.get('Cantidad')),
            'valor_unitario': a_float(c.get('ValorUnitario')),
            'importe': a_float(c.get('Importe')),
            'descuento': a_float(c.get('Descuento')),
            'no_identificacion': (c.get('NoIdentificacion') or '').strip(),
            'clave_prod_serv': c.get('ClaveProdServ'),
            'descripcion': c.get('Descripcion'),
        })

    impuestos = root.find('cfdi:Impuestos', ns)
    return {
        'uuid': normalizar_uuid(timbre.get('UUID')),
        'fecha': root.get('Fecha'),
        'serie': root.get('Serie'),
        'folio': root.get('Folio'),
        'tipo': root.get('TipoDeComprobante'),
        'subtotal': a_float(root.get('SubTotal')),
        'descuento': a_float(root.get('Descuento')),
        'total': a_float(root.get('Total')),
        'iva': a_float(impuestos.get('TotalImpuestosTrasladados')) if impuestos is not None else 0.0,
        'forma_pago': root.get('FormaPago'),
        'metodo_pago': root.get('MetodoPago'),
        'emisor_rfc': (emisor.get('Rfc') or '').strip().upper(),
        'receptor_rfc': (receptor.get('Rfc') or '').strip().upper(),
        'receptor_nombre': receptor.get('Nombre'),
        'receptor_uso': receptor.get('UsoCFDI'),
        'receptor_regimen': receptor.get('RegimenFiscalReceptor'),
        'receptor_cp': receptor.get('DomicilioFiscalReceptor'),
        'conceptos': conceptos,
    }


def leer_cfdi_b64(xml_b64):
    return leer_cfdi(base64.b64decode(xml_b64))


# =============================================================================
# ODOO
# =============================================================================

class OdooWriteUncertain(Exception):
    """Una ESCRITURA fallo por red: no se sabe si Odoo hizo commit. Nunca se
    reintenta a ciegas; se reconcilia leyendo."""


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=300, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class OdooModelProxy:
    """Llamadas a Odoo con timeout y reintento SOLO de lecturas (incidente del
    26-jul-2026: reintentar una escritura tras un 502 duplica facturas)."""

    READ_METHODS = ('search', 'search_read', 'read', 'search_count',
                    'read_group', 'fields_get')

    def __init__(self, timeout=300):
        self.timeout = timeout
        self._connect()

    def _connect(self):
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common',
                                           transport=TimeoutTransport(self.timeout))
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PWD, {})
        if not self.uid:
            raise Exception(f"No se pudo autenticar contra Odoo ({ODOO_DB}).")
        self.models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object',
                                                transport=TimeoutTransport(self.timeout))

    def execute_kw(self, model, method, args, kwargs=None, max_retries=3, delay=2):
        is_read = method in self.READ_METHODS
        for attempt in range(1, max_retries + 1):
            try:
                if kwargs is not None:
                    return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PWD,
                                                  model, method, args, kwargs)
                return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PWD,
                                              model, method, args)
            except xmlrpc.client.Fault:
                raise
            except Exception as e:
                if not is_read:
                    raise OdooWriteUncertain(f"{model}.{method}: {e}")
                log.warning(f"Red en lectura [{model}.{method}] {attempt}/{max_retries}: {e}")
                if attempt == max_retries:
                    raise
                import time
                time.sleep(delay * attempt)
                try:
                    self._connect()
                except Exception as auth_e:
                    log.warning(f"Fallo al reautenticar: {auth_e}")


def get_account_id(odoo, code):
    acc = odoo.execute_kw('account.account', 'search', [[('code', '=', code)]], {'limit': 1})
    if not acc:
        raise Exception(f"No existe la cuenta contable {code} en {ODOO_DB}.")
    return acc[0]


def get_team_walmart(odoo):
    """Id de Team_Walmart por nombre EXACTO (un ilike traeria Team_Walmart_1P)."""
    teams = odoo.execute_kw('crm.team', 'search_read', [[('name', '=', TEAM_WALMART)]],
                            {'fields': ['id', 'name']})
    if len(teams) != 1:
        raise Exception(f"Se esperaba un equipo '{TEAM_WALMART}' en {ODOO_DB}; hay {len(teams)}.")
    return teams[0]['id']


def ejecutar_sin_retorno(odoo, model, method, ids):
    """reconcile / remove_move_reconcile hacen el trabajo y fallan al responder
    ("cannot marshal None"). Ese Fault se toma como exito; quien llama confirma
    leyendo."""
    try:
        odoo.execute_kw(model, method, [ids])
    except xmlrpc.client.Fault as e:
        if 'cannot marshal None' not in (e.faultString or ''):
            raise
    return True


def cargar_catalogos(odoo):
    formas = odoo.execute_kw('l10n_mx_edi.payment.method', 'search_read', [[]],
                             {'fields': ['id', 'code']})
    mx = odoo.execute_kw('res.country', 'search', [[('code', '=', 'MX')]], {'limit': 1})
    return {
        'formas_pago': {f['code']: f['id'] for f in formas},
        'mx_country_id': mx[0] if mx else False,
        'cuenta_cxc': get_account_id(odoo, ACCOUNT_WALMART_CXC),
        'cuenta_merc_no_enviada': get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA),
        'team_walmart': get_team_walmart(odoo),
    }


def buscar_so(odoo, so_name):
    r = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', so_name)]],
                        {'fields': ['id', 'name', 'state', 'team_id', 'invoice_status',
                                    'invoice_ids', 'order_line', 'partner_id', 'date_order'],
                         'limit': 1})
    return r[0] if r else None


def facturas_existentes(odoo, so_name, so=None):
    """Facturas vivas de la SO (de amazon/01): por invoice_origin, por la ref
    AUTOINV:<SO>... y por sale.order.invoice_ids, que es el unico camino que ve
    una factura GLOBAL. Buscar tambien por invoice_origin es lo que ve las
    facturas del proceso manual, que se creaban con ref=''. Se descartan las
    revertidas por completo con NC."""
    domain = ['&', ('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
              '|', ('invoice_origin', '=', so_name),
              ('ref', 'like', f'{IDEMPOTENCY_PREFIX}{so_name}%')]
    campos = ['id', 'name', 'state', 'ref', 'amount_total', 'invoice_origin',
              'payment_state', 'l10n_mx_edi_cfdi_uuid', 'partner_id']
    filas = odoo.execute_kw('account.move', 'search_read', [domain],
                            {'fields': campos, 'order': 'id asc'})
    vistas = {f['id'] for f in filas}
    ligadas = [i for i in (so or {}).get('invoice_ids') or [] if i not in vistas]
    if ligadas:
        filas += odoo.execute_kw('account.move', 'search_read',
                                 [[('id', 'in', ligadas), ('move_type', '=', 'out_invoice'),
                                   ('state', '!=', 'cancel')]],
                                 {'fields': campos, 'order': 'id asc'})
    return [f for f in filas if f.get('payment_state') != 'reversed']


def resumen_factura(f):
    return {'id': f['id'], 'name': f.get('name'), 'ref': f.get('ref'),
            'state': f.get('state'), 'uuid': normalizar_uuid(f.get('l10n_mx_edi_cfdi_uuid')),
            'total': a_float(f.get('amount_total'))}


def confirmar_so(odoo, so):
    """Confirma una SO en borrador. None si quedo confirmada; si no, el motivo."""
    try:
        odoo.execute_kw('sale.order', 'action_confirm', [[so['id']]])
    except OdooWriteUncertain as e:
        log.warning(f"{so['name']}: confirmacion incierta, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        return f"Odoo no permitio confirmar la SO: {e.faultString[:300]}"
    estado = odoo.execute_kw('sale.order', 'read', [[so['id']]],
                             {'fields': ['state']})[0]['state']
    if estado != 'sale':
        return f"La SO sigue en '{estado}' despues de confirmarla."
    so['state'] = estado
    return None


def buscar_por_ref(odoo, ref):
    """Recupera la factura cuando se pierde la respuesta del create."""
    return odoo.execute_kw('account.move', 'search_read',
                           [[('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
                             ('ref', '=', ref)]],
                           {'fields': ['id', 'name', 'state', 'ref', 'l10n_mx_edi_cfdi_uuid'],
                            'order': 'id asc'})


def leer_lineas_so(odoo, so):
    """Lineas de la SO pendientes de facturar. Sin restriccion de OUT."""
    lines = odoo.execute_kw('sale.order.line', 'search_read',
                            [[('id', 'in', so['order_line'])]],
                            {'fields': ['id', 'name', 'sequence', 'display_type', 'product_id',
                                        'product_uom', 'product_uom_qty', 'qty_invoiced',
                                        'qty_delivered', 'price_unit', 'discount', 'tax_id']})
    salida = []
    for line in lines:
        if line.get('display_type') in ('line_section', 'line_note'):
            continue
        qty = line['product_uom_qty']
        if line['qty_invoiced'] >= qty:
            continue
        # C-ENVIO no genera movimiento de inventario: se marca entregado para
        # que Odoo no lo deje pendiente en la SO.
        pname = line['product_id'][1].upper() if line.get('product_id') else ''
        if 'C-ENVIO' in pname and line['qty_delivered'] < qty:
            try:
                odoo.execute_kw('sale.order.line', 'write', [[line['id']], {'qty_delivered': qty}])
            except Exception as e:
                log.warning(f"{so['name']}: no se pudo marcar C-ENVIO entregado: {e}")
        salida.append(line)
    return salida


def comando_linea(line, qty, price_unit=None, discount=None):
    return (0, 0, {
        'display_type': 'product',
        'sequence': int(line['sequence']) if line.get('sequence') else 10,
        'name': line['name'],
        'product_uom_id': line['product_uom'][0] if line.get('product_uom') else False,
        'product_id': line['product_id'][0] if line.get('product_id') else False,
        'quantity': qty,
        'discount': line['discount'] if discount is None else discount,
        'price_unit': line['price_unit'] if price_unit is None else price_unit,
        'tax_ids': [(6, 0, [TAX_ID_MARKETPLACES])],
        'sale_line_ids': [(4, line['id'])],
    })


def total_estimado(comandos):
    """Total con IVA que Odoo va a calcular, redondeando por linea."""
    total = 0.0
    for _, _, v in comandos:
        base = round(v['quantity'] * v['price_unit'] * (1 - (v['discount'] or 0) / 100.0), 2)
        total += base + round(base * 0.16, 2)
    return round(total, 2)


def fijar_cxc(odoo, inv_id, cuenta_cxc, extra_vals=None):
    """Lleva la CxC a la 105.01.007 (si no, queda en la del partner) y escribe
    en la misma llamada lo que venga en extra_vals (UUID, narration)."""
    vals = dict(extra_vals or {})
    lineas = odoo.execute_kw('account.move.line', 'search_read', [[('move_id', '=', inv_id)]],
                             {'fields': ['id', 'account_type', 'account_id']})
    cxc = [(1, l['id'], {'account_id': cuenta_cxc}) for l in lineas
           if l['account_type'] == 'asset_receivable' and l['account_id'][0] != cuenta_cxc]
    if cxc:
        vals['line_ids'] = cxc
    if vals:
        odoo.execute_kw('account.move', 'write', [[inv_id], vals],
                        {'context': {'tracking_disable': True}})


def timbrar(odoo, inv_id, etiqueta):
    """Timbra una factura publicada. Devuelve (uuid, error). Decide el UUID que
    queda en la factura, no si la llamada fallo."""
    error = None
    try:
        ctx = {'active_model': 'account.move', 'active_ids': [inv_id]}
        wiz = odoo.execute_kw('account.move.send.wizard', 'create',
                              [{'is_download_only': False}], {'context': ctx})
        odoo.execute_kw('account.move.send.wizard', 'action_send_and_print', [[wiz]],
                        {'context': ctx})
    except Exception as e:
        if 'cannot marshal None' not in str(e):
            error = str(e)
    uuid = odoo.execute_kw('account.move', 'read', [[inv_id]],
                           {'fields': ['l10n_mx_edi_cfdi_uuid']})[0]['l10n_mx_edi_cfdi_uuid']
    if not uuid:
        error = error or 'el timbrado termino sin UUID'
        log.warning(f"{etiqueta}: fallo el timbrado: {error}")
    return (uuid or None), (None if uuid else error)


def cuadrar_base_con_cfdi(odoo, inv_id, cfdi, etiqueta):
    """Iguala la base de la factura a la del CFDI corrigiendo el precio de una
    linea (solo diferencias de redondeo). Devuelve el ajuste."""
    if cfdi['total'] <= 0:
        return 0.0
    base_cfdi = round(cfdi['subtotal'] - cfdi['descuento'], 2)
    base_odoo = round(a_float(odoo.execute_kw('account.move', 'read', [[inv_id]],
                                              {'fields': ['amount_untaxed']})[0]['amount_untaxed']), 2)
    dif = round(base_cfdi - base_odoo, 2)
    if dif == 0:
        return 0.0
    if abs(dif) > TOLERANCIA_CUADRE_CFDI:
        log.warning(f"{etiqueta}: base Odoo {base_odoo:,.2f} vs CFDI {base_cfdi:,.2f}. "
                    f"No es redondeo: no se ajusta.")
        return 0.0
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', inv_id), ('display_type', '=', 'product')]],
                             {'fields': ['id', 'quantity', 'price_unit', 'discount', 'price_subtotal']})
    candidatas = [l for l in lineas if not l['discount'] and a_float(l['quantity']) > 0]
    if not candidatas:
        return 0.0
    linea = max(candidatas, key=lambda l: a_float(l['price_subtotal']))
    nuevo = round((a_float(linea['price_subtotal']) + dif) / a_float(linea['quantity']), 6)
    try:
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'invoice_line_ids': [(1, linea['id'], {'price_unit': nuevo})]}],
                        {'context': {'tracking_disable': True}})
    except Exception as e:
        log.warning(f"{etiqueta}: Odoo rechazo el ajuste de la base: {e}")
        return 0.0
    return dif


def cuadrar_iva_con_cfdi(odoo, inv_id, cfdi, etiqueta):
    """Iguala el IVA (y el total) al CFDI. SOLO en facturas que no timbramos: en
    una que timbramos editar el IVA hace que el PAC la rechace (CFDI40119)."""
    total_cfdi = round(cfdi['total'], 2)
    iva_cfdi = round(cfdi['iva'], 2)
    if total_cfdi <= 0 or iva_cfdi <= 0:
        return 0.0
    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['amount_untaxed', 'amount_tax', 'amount_total']})[0]
    dif_total = round(total_cfdi - a_float(inv['amount_total']), 2)
    if dif_total == 0:
        return 0.0
    dif_iva = round(iva_cfdi - a_float(inv['amount_tax']), 2)
    if abs(dif_total) > TOLERANCIA_CUADRE_CFDI or dif_iva != dif_total:
        log.warning(f"{etiqueta}: total Odoo {a_float(inv['amount_total']):,.2f} vs CFDI "
                    f"{total_cfdi:,.2f}. No es redondeo del IVA: no se ajusta.")
        return 0.0
    lineas_iva = odoo.execute_kw('account.move.line', 'search_read',
                                 [[('move_id', '=', inv_id), ('display_type', '=', 'tax')]],
                                 {'fields': ['id', 'balance']})
    if len(lineas_iva) != 1:
        return 0.0
    nuevo = round(a_float(lineas_iva[0]['balance']) - dif_iva, 2)
    try:
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'line_ids': [(1, lineas_iva[0]['id'],
                                                  {'amount_currency': nuevo, 'balance': nuevo})]}],
                        {'context': {'tracking_disable': True}})
    except Exception as e:
        log.warning(f"{etiqueta}: Odoo rechazo el ajuste del IVA: {e}")
        return 0.0
    return dif_iva


def conciliar_salida(odoo, so, inv_id, cuenta):
    """Concilia la 205.06.003 de la factura con la salida de almacen de la SO
    (version reducida de amazon/01: sin el paso de facturas canceladas, que en
    Walmart todavia no se refacturan). Devuelve {'estado', 'detalle'}.
      CONCILIADA  |  SIN_SALIDA (Odoo concilia al validar el OUT)  |  PARCIAL
      SIN_LINEAS  |  ERROR
    Ojo WFS (55% de las lineas): hay que validar como registra Odoo esa salida.
    """
    campos = ['id', 'product_id', 'balance', 'amount_residual', 'reconciled']
    try:
        de_factura = odoo.execute_kw('account.move.line', 'search_read',
                                     [[('move_id', '=', inv_id), ('account_id', '=', cuenta)]],
                                     {'fields': campos})
        if not de_factura:
            return {'estado': 'SIN_LINEAS', 'detalle': ''}
        if all(l['reconciled'] for l in de_factura):
            return {'estado': 'CONCILIADA', 'detalle': 'conciliada por Odoo al publicar'}
        dominio_salida = [('account_id', '=', cuenta), ('parent_state', '=', 'posted'),
                          ('move_id.stock_move_id.sale_line_id.order_id', '=', so['id']),
                          ('balance', '>', 0), ('reconciled', '=', False)]
        salidas = odoo.execute_kw('account.move.line', 'search_read', [dominio_salida],
                                  {'fields': campos})
        if not salidas:
            return {'estado': 'SIN_SALIDA', 'detalle': 'sin salida de almacen; Odoo concilia al validarla'}
        for l in de_factura:
            if l['reconciled']:
                continue
            prod = l['product_id'][0] if l['product_id'] else False
            suyas = [s['id'] for s in salidas
                     if (s['product_id'][0] if s['product_id'] else False) == prod]
            if suyas:
                ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', [l['id']] + suyas)
        final = odoo.execute_kw('account.move.line', 'read', [[l['id'] for l in de_factura]],
                                {'fields': ['reconciled', 'amount_residual']})
        if all(f['reconciled'] for f in final):
            return {'estado': 'CONCILIADA', 'detalle': ''}
        residuo = round(sum(a_float(f['amount_residual']) for f in final), 2)
        return {'estado': 'PARCIAL', 'detalle': f"residuo {residuo:,.2f}"}
    except Exception as e:
        return {'estado': 'ERROR', 'detalle': str(e)[:300]}


def postear_chatter(odoo, inv_id, lineas_texto):
    body = '<br/>'.join(lineas_texto)
    try:
        odoo.execute_kw('account.move', 'message_post', [inv_id],
                        {'body': body, 'message_type': 'comment'})
    except Exception as e:
        log.warning(f"No se pudo escribir el chatter de la factura {inv_id}: {e}")


# =============================================================================
# TABLA DE CONTROL
# =============================================================================

def reset_stuck_processing(db_conn):
    """Regresa a su pendiente lo que quedo en PROCESSING por una corrida muerta.
    El pendiente original se deduce de la clave: con ':' es un CFDI de Walmart."""
    cur = db_conn.cursor()
    cur.execute(f"""
        UPDATE {TBL_BILLING}
        SET status = IF(LOCATE(':', mkp_order_id) > 0,
                        IF(xml_data IS NULL, %s, %s), %s)
        WHERE marketplace = %s AND status = %s
          AND processed_at < (NOW() - INTERVAL %s HOUR)
    """, (ST_PENDING_WM_XML, ST_PENDING_WM_LINK, ST_PENDING_WB, MARKETPLACE, ST_PROCESSING,
          STUCK_PROCESSING_MIN_AGE_HOURS))
    db_conn.commit()
    if cur.rowcount:
        log.warning(f"Liberados {cur.rowcount} registros atorados en PROCESSING.")
    cur.close()


def claim(db_conn, record_id, desde_status):
    """Candado por fila: True solo si esta corrida gano el registro."""
    cur = db_conn.cursor()
    cur.execute(f"""UPDATE {TBL_BILLING} SET status = %s, processed_at = NOW()
                    WHERE id = %s AND status = %s""", (ST_PROCESSING, record_id, desde_status))
    db_conn.commit()
    ganado = cur.rowcount > 0
    cur.close()
    return ganado


def update_fila(db_conn, record_id, status, error_log=None, cfdi_uuid=None,
                sumar_retry=False, facturas=None, extra_json=None):
    """Cierra una fila. facturas -> raw_json.facturas_odoo; extra_json se
    mezcla en raw_json.resultado."""
    sets, params = ['status = %s', 'error_log = %s', 'processed_at = NOW()',
                    'retry_count_loader = retry_count_loader + %s',
                    'cfdi_uuid = COALESCE(%s, cfdi_uuid)'], \
        [status, error_log, 1 if sumar_retry else 0, cfdi_uuid]
    if facturas is not None:
        sets.append("raw_json = JSON_SET(COALESCE(raw_json, JSON_OBJECT()), "
                    "'$.facturas_odoo', CAST(%s AS JSON))")
        params.append(a_json(facturas))
    if extra_json is not None:
        sets.append("raw_json = JSON_SET(COALESCE(raw_json, JSON_OBJECT()), "
                    "'$.resultado', CAST(%s AS JSON))")
        params.append(a_json(extra_json))
    cur = db_conn.cursor()
    cur.execute(f"UPDATE {TBL_BILLING} SET {', '.join(sets)} WHERE id = %s",
                tuple(params + [record_id]))
    db_conn.commit()
    cur.close()


def frescura_fenix(cursor):
    """Horas desde la ultima carga de Fenix (hora del servidor MySQL)."""
    cursor.execute("""SELECT TIMESTAMPDIFF(MINUTE, GREATEST(MAX(inserted_at), MAX(updated_at)), NOW())
                      FROM somos_reyes.wm_fenix_invoices_report""")
    fila = cursor.fetchone()
    return None if not fila or fila[0] is None else round(fila[0] / 60.0, 1)


def frescura_ftm(cursor):
    cursor.execute("""SELECT TIMESTAMPDIFF(MINUTE, MAX(updated_at), NOW())
                      FROM somos_reyes.wm_financial_transactions_master""")
    fila = cursor.fetchone()
    return None if not fila or fila[0] is None else round(fila[0] / 60.0, 1)
