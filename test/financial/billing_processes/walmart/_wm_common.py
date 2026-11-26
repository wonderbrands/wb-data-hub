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
        La llena el crawler de Data, a mano: es OPCIONAL (WM_USAR_CRAWLER).

Fuente propia (23-nov-2026), en la misma tabla de control con
marketplace = 'WALMART_FENIX' (no pisa la tabla del crawler):
    PRECFDI:<cfdiid>  peticion de factura del cliente (API de Fenix, 00b)
    CFDI:<UUID>       CFDI que timbro Fenix, con su XML (descarga del SAT, 02)
La API dice QUE linea pidio factura (cfdiid, invoiceno: los mismos que el
crawler llama identificador e invoice) pero NO da el UUID; el SAT da el XML.
Ligar un CFDI con su peticion sin el crawler: camino rapido, folio con el
pedido y tramos de folio (ver ligar_cfdis). Lo que no se liga sin duda queda
SIN_LIGAR.
    wm_order_line (API de Walmart, al dia)   UPC de cada linea: es el
        NoIdentificacion del CFDI de Walmart.
"""

import os
import sys
import json
import base64
import bisect
import logging
import urllib.request
import xmlrpc.client
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
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

# IVA 16% con exigibilidad AL COBRO (correo de Finanzas, 25-nov-2026, A1): al
# facturar va a 209.01.001 IVA trasladado no cobrado y Odoo lo pasa a 208.01.001
# al conciliar el cobro. En Odoo es el impuesto 14 ("16%"). El 38 ("IVA(16%)
# Marketplaces", que usan ML y Amazon) es exigible al facturar y va directo a la
# 208. cargar_catalogos() valida que el impuesto sea al cobro y con la 209.
TAX_ID_MARKETPLACES = int(os.getenv('WM_TAX_ID', '14'))
ACCOUNT_IVA_NO_COBRADO = '209.01.001'
PARTNER_ID_PUBLICO_GENERAL = 13436          # XAXX010101000
ACCOUNT_WALMART_CXC = '105.01.007'
# Diario de los CFDI del autofacturador de Walmart (Finanzas, C3/D5): entran con
# su serie y folio (FI-4252), no consumen la secuencia INV. Las de publico
# general (04) siguen en el diario de facturas normal (INV).
JOURNAL_WALMART_CODE = os.getenv('WM_JOURNAL_CODE', 'WALM').strip()
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
ST_PENDING_WM_UUID = 'PENDING_WM_UUID'   # Walmart facturo (API de Fenix) y falta ligar su CFDI (02)
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

# Bicis electricas. Finanzas (25-nov-2026, A3): sin trato especial; si la orden
# sigue pendiente pasado el plazo de gracia, 04 la factura a publico general
# como cualquier otra. Por eso la lista viene VACIA. Se conserva la variable por
# si algun SKU necesitara volver a factura manual (prefijos separados por coma).
SKUS_FACTURA_MANUAL = tuple(s.strip().upper() for s in
                            os.getenv('WM_SKUS_FACTURA_MANUAL', '').split(',')
                            if s.strip())
ST_LINEA_REVISAR = 'WM_LINEA_REVISAR'     # el CFDI no corresponde a la linea de la SO
                                          # (UPC distinto o la linea ya la facturo otra
                                          # factura): no se crea nada cruzado
ST_ADOPTADA_REVISAR = 'WM_ADOPTADA_REVISAR'  # ya existia una factura con el UUID pero con
                                             # otro total, cliente, CxC o diario (proceso
                                             # manual): la corrige el traslado de 2026

# Finales
ST_WB_INVOICED = 'WB_INVOICED'            # factura de WB publicada (y timbrada en prod)
ST_WM_LINKED = 'WM_LINKED'                # CFDI de Walmart replicado en Odoo
ST_ALREADY = 'ALREADY_ODOO_INVOICED'      # ya tenia factura (proceso manual, global...)
ST_DESCARTADA = 'DESCARTADA'              # la fila ya no aplica (cambio la clasificacion)
ST_TOTAL_DIFF = 'TOTAL_DIFF'              # no cuadra contra Walmart: revisa Finanzas
ST_XML_INVALIDO = 'XML_INVALIDO'          # el XML no es de esta fila o no es nuestro
ST_WM_NO_VIGENTE = 'WM_CFDI_NO_VIGENTE'   # el SAT dice que el CFDI de Walmart ya no esta vigente
ST_ERROR = 'ERROR'                        # se reintenta hasta 3 veces (retry_count_loader)

# Las que el miner puede reescribir en cada corrida. El resto queda cerrado.
REFRESCABLES = (ST_PENDING_WB, ST_PENDING_WM_UUID, ST_PENDING_WM_XML, ST_PENDING_WM_LINK,
                ST_NO_SO, ST_SO_DUPLICADA, ST_SO_OTRO_EQUIPO, ST_SO_CANCELADA,
                ST_ORDEN_MIXTA, ST_WM_REVISAR, ST_WM_EXCEDENTE, ST_DOBLE_CFDI,
                ST_RECLASIFICAR, ST_MANUAL_SERIE, ST_BORRADOR_AJENO,
                ST_LINEA_REVISAR, ST_ADOPTADA_REVISAR)


# =============================================================================
# FUENTE PROPIA DE FENIX (marketplace = 'WALMART_FENIX')
# =============================================================================

MKP_FENIX = 'WALMART_FENIX'
CLAVE_ETL = 'ETL:ULTIMA_CORRIDA'   # marca de la ultima corrida completa de 00b

# Filas PRECFDI:<cfdiid> (peticiones de factura). Un pre-CFDI puede salir en
# dos listas; gana la de mayor prioridad.
PRE_RECIBIDO, PRE_CON_ERROR, PRE_NUEVO, PRE_BLOQUEADO = 'RECIBIDO', 'CON_ERROR', 'NUEVO', 'BLOQUEADO'
PRIORIDAD_PRE = {PRE_RECIBIDO: 3, PRE_CON_ERROR: 2, PRE_NUEVO: 1, PRE_BLOQUEADO: 0}

# Filas CFDI:<UUID> (CFDI timbrados por Fenix, con XML del SAT)
CFDI_SIN_LIGAR, CFDI_LIGADO = 'SIN_LIGAR', 'LIGADO'

# Como se reconoce un CFDI timbrado por Fenix dentro de todo lo que emite la
# empresa (medido en la descarga del SAT del 30-31 oct: 13,306 CFDI, solo 7
# con esta combinacion; los nuestros son CVD110412TF6 'INV/2026/', Mercado
# Libre TLE011122SC2 y Amazon EME000602QR9). El certificado es uno propio de
# la empresa para Walmart (OU=Walmart).
PAC_FENIX = os.getenv('WM_PAC_FENIX', 'ICA050422AA6').strip().upper()
SERIE_FENIX = os.getenv('WM_SERIE_FENIX', 'FI').strip().upper()

# El crawler de Data (wm_fenix_invoices_report) es opcional: si esta al dia,
# da el UUID de cada linea; si no, se usa la fuente propia.
USAR_CRAWLER = os.getenv('WM_USAR_CRAWLER', '1').strip() in ('1', 'true', 'TRUE')

# Ligado CFDI -> peticion sin el crawler (ver ligar_cfdis). Validado contra el
# crawler con los 232 CFDI de octubre-2026: 203 ligados, 0 errores.
#  - Camino rapido: peticion entre VENTANA_MIN antes del timbrado y el
#    timbrado, precio exacto, mismo UPC y una sola orden posible (110).
#  - Folio con el pedido: algunos CFDI traen el pedido al inicio del Folio (3).
#  - Tramo de folios: el Folio de la serie FI crece en el mismo orden que el
#    cfdiid de la peticion (0 inversiones en octubre). Los CFDI sin ligar entre
#    dos ya ligados solo pueden ser de las peticiones con cfdiid entre las de
#    esos dos; se emparejan en orden y solo si hay UNA manera de hacerlo (90).
VENTANA_LIGADO_MIN = int(os.getenv('WM_VENTANA_LIGADO_MIN', '30'))
GRUPO_CERRADO = os.getenv('WM_GRUPO_CERRADO', 'orden').strip().lower()   # 'orden' | 'cliente' | '0'
TOLERANCIA_PRECIO_LIGADO = 0.02

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
        'pac': (timbre.get('RfcProvCertif') or '').strip().upper(),
        'fecha_timbrado': timbre.get('FechaTimbrado'),
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


def es_cfdi_walmart(cfdi):
    """CFDI de ingreso que timbro Fenix (el autofacturador de Walmart) a nuestro nombre."""
    return (cfdi['emisor_rfc'] == RFC_EMISOR and cfdi['tipo'] == 'I'
            and cfdi.get('pac') == PAC_FENIX and (cfdi.get('serie') or '').upper() == SERIE_FENIX)


def cantidad_cfdi(cfdi):
    return sum(c['cantidad'] for c in cfdi['conceptos']) or 1.0


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
    validar_impuesto(odoo)
    return {
        'formas_pago': {f['code']: f['id'] for f in formas},
        'mx_country_id': mx[0] if mx else False,
        'cuenta_cxc': get_account_id(odoo, ACCOUNT_WALMART_CXC),
        'cuenta_merc_no_enviada': get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA),
        'team_walmart': get_team_walmart(odoo),
        'diario_walmart': get_journal_id(odoo, JOURNAL_WALMART_CODE),
    }


def validar_impuesto(odoo):
    """El IVA tiene que ser al cobro y pasar por la 209.01.001 (A1). Si alguien
    cambia el impuesto en Odoo, no se factura con el equivocado."""
    t = odoo.execute_kw('account.tax', 'read', [[TAX_ID_MARKETPLACES]],
                        {'fields': ['name', 'amount', 'type_tax_use', 'tax_exigibility',
                                    'cash_basis_transition_account_id']})
    if not t:
        raise Exception(f"No existe el impuesto {TAX_ID_MARKETPLACES} (WM_TAX_ID).")
    t = t[0]
    transitoria = (t['cash_basis_transition_account_id'] or [None, ''])[1] or ''
    if (t['type_tax_use'] != 'sale' or a_float(t['amount']) != 16 or t['tax_exigibility'] != 'on_payment'
            or not transitoria.startswith(ACCOUNT_IVA_NO_COBRADO)):
        raise Exception(f"El impuesto {TAX_ID_MARKETPLACES} ({t['name']}) no es IVA 16% de venta al cobro "
                        f"con transitoria {ACCOUNT_IVA_NO_COBRADO} (exigibilidad {t['tax_exigibility']}, "
                        f"transitoria {transitoria or 'ninguna'}). Revisar WM_TAX_ID.")


def get_journal_id(odoo, code):
    j = odoo.execute_kw('account.journal', 'search_read', [[('code', '=', code), ('type', '=', 'sale')]],
                        {'fields': ['id'], 'limit': 1})
    if not j:
        raise Exception(f"No existe el diario de ventas {code} en {ODOO_DB} (WM_JOURNAL_CODE).")
    return j[0]['id']


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


# =============================================================================
# FUENTE PROPIA DE FENIX: lecturas, repositorio de CFDI y ligado
# =============================================================================

def frescura_etl_fenix(cursor):
    """Horas desde la ultima corrida COMPLETA del ETL de la API de Fenix (00b)."""
    cursor.execute(f"""SELECT TIMESTAMPDIFF(MINUTE, processed_at, NOW()) FROM {TBL_BILLING}
                       WHERE marketplace = %s AND mkp_order_id = %s""", (MKP_FENIX, CLAVE_ETL))
    fila = cursor.fetchone()
    if not fila:
        return None
    valor = fila[0] if not isinstance(fila, dict) else next(iter(fila.values()))
    return None if valor is None else round(valor / 60.0, 1)


def _raw(valor):
    return json.loads(valor) if isinstance(valor, (str, bytes)) else (valor or {})


def precfdi_por_linea(cursor):
    """{iderp: {estados}} de las peticiones de factura vistas en la API de Fenix."""
    cursor.execute(f"""SELECT status, raw_json FROM {TBL_BILLING}
                       WHERE marketplace = %s AND (mkp_order_id LIKE 'PRECFDI:%%'
                                                   OR mkp_order_id LIKE 'BLOQUEADO:%%')""", (MKP_FENIX,))
    por_linea = {}
    for fila in cursor.fetchall():
        status, raw = (fila['status'], fila['raw_json']) if isinstance(fila, dict) else fila
        iderp = _raw(raw).get('iderp')
        if iderp:
            por_linea.setdefault(iderp, set()).add(status)
    return por_linea


def cfdis_ligados_por_linea(cursor):
    """{iderp: [fila]} de los CFDI de Walmart del repositorio ya ligados a una linea.
    La fila tiene la forma de una de wm_fenix_invoices_report, para que el miner
    las trate igual."""
    cursor.execute(f"""SELECT cfdi_uuid, total_amount, raw_json FROM {TBL_BILLING}
                       WHERE marketplace = %s AND mkp_order_id LIKE 'CFDI:%%' AND status = %s""",
                   (MKP_FENIX, CFDI_LIGADO))
    por_linea = {}
    for fila in cursor.fetchall():
        uuid, total, raw = ((fila['cfdi_uuid'], fila['total_amount'], fila['raw_json'])
                            if isinstance(fila, dict) else fila)
        raw = _raw(raw)
        if not raw.get('iderp'):
            continue
        por_linea.setdefault(raw['iderp'], []).append({
            'uuid': normalizar_uuid(uuid), 'tipo_documento': 'I', 'pedido': raw.get('orden'),
            'iderp': raw['iderp'], 'cantidad': int(round(a_float(raw.get('cantidad')) or 1)),
            'subtotal': None, 'descuento': None, 'total_factura': a_float(total),
            'estado_cfdi': 'CON_XML_PDF', 'rfc_receptor': raw.get('receptor_rfc'),
            'fecha_creacion': (raw.get('fecha') or '').replace('T', ' '),
            'origen': f"SAT/{raw.get('metodo')}"})
    return por_linea


def resumen_cfdi(cfdi):
    return {'fecha': cfdi['fecha'], 'total': cfdi['total'], 'cantidad': cantidad_cfdi(cfdi),
            'receptor_rfc': cfdi['receptor_rfc'], 'receptor_nombre': cfdi['receptor_nombre'],
            'serie': cfdi['serie'], 'folio': cfdi['folio'],
            'upc': cfdi['conceptos'][0]['no_identificacion'] if cfdi['conceptos'] else None}


def guardar_cfdis_walmart(db, cfdis, dry_run=False):
    """Repositorio de CFDI de Walmart: inserta una fila CFDI:<UUID> por cada
    (xml_bytes, cfdi) que no exista. Una fila ya ligada no se toca."""
    if dry_run or not cfdis:
        return
    cur = db.cursor()
    for bloque in lotes(cfdis, 200):
        cur.executemany(f"""
            INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, cfdi_uuid, total_amount, xml_data,
                                       raw_json, status, retry_count, retry_count_loader, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0, NOW())
            ON DUPLICATE KEY UPDATE xml_data = COALESCE(xml_data, VALUES(xml_data))
        """, [(MKP_FENIX, f"CFDI:{c['uuid']}", c['uuid'], round(c['total'], 2),
               base64.b64encode(x).decode('ascii'), a_json(resumen_cfdi(c)), CFDI_SIN_LIGAR)
              for x, c in bloque])
        db.commit()
    cur.close()


def llenar_xml_desde_repositorio(db, dry_run=False):
    """Las filas <orden>:<UUID> en PENDING_WM_XML cuyo UUID ya esta en el
    repositorio toman de ahi su XML y pasan a PENDING_WM_LINK. La llaman 01
    (al crear las filas de lo recien ligado, para que 03 las replique en la
    misma corrida) y 02. Devuelve cuantas filas se llenaron."""
    if dry_run:
        return 0
    cur = db.cursor()
    cur.execute(f"""
        UPDATE {TBL_BILLING} b
        JOIN {TBL_BILLING} r ON r.marketplace = %s AND r.mkp_order_id = CONCAT('CFDI:', b.cfdi_uuid)
        SET b.xml_data = r.xml_data, b.status = %s, b.processed_at = NOW(), b.error_log = NULL
        WHERE b.marketplace = %s AND b.status = %s AND r.xml_data IS NOT NULL
    """, (MKP_FENIX, ST_PENDING_WM_LINK, MARKETPLACE, ST_PENDING_WM_XML))
    n = cur.rowcount
    db.commit()
    cur.close()
    return n


def _lineas_ftm(cursor, iderps):
    """{iderp: (precio_unitario, cantidad)} de FTM."""
    res = {}
    for chunk in lotes(sorted(iderps), 500):
        cursor.execute(f"""
            SELECT so_prime_line_sub_line_no, price_amount, quantity
            FROM somos_reyes.wm_financial_transactions_master
            WHERE type = 'Order' AND so_prime_line_sub_line_no IN ({','.join(['%s'] * len(chunk))})
        """, tuple(chunk))
        for k, precio, qty in cursor.fetchall():
            q = max(int(qty or 1), 1)
            res[k] = (a_float(precio) / q, q)
    return res


def upc_por_linea(cursor, iderps):
    """{iderp: UPC sin ceros a la izquierda} de wm_order_line (API de Walmart).
    Es el NoIdentificacion del CFDI de Walmart (232 de 232 en octubre-2026)."""
    res = {}
    for chunk in lotes(sorted(i for i in iderps if i), 500):
        cursor.execute(f"""
            SELECT so_prime_line_sub_line_no, upc FROM somos_reyes.wm_order_line
            WHERE so_prime_line_sub_line_no IN ({','.join(['%s'] * len(chunk))}) AND upc IS NOT NULL AND upc <> ''
        """, tuple(chunk))
        for k, upc in cursor.fetchall():
            res[k] = str(upc).strip().lstrip('0')
    return res


def _folio_secuencial(raw):
    """Folio numerico de la serie de Fenix, o None (otra serie o folio con el pedido)."""
    folio = str(raw.get('folio') or '').strip()
    if str(raw.get('serie') or '').strip().upper() != SERIE_FENIX or not folio.isdigit() or len(folio) > 9:
        return None
    return int(folio)


def _emparejar_en_orden(cfdis, peticiones, cuadra):
    """Empareja cada CFDI (en orden de folio) con una peticion (en orden de
    cfdiid) sin cruzarse. Devuelve la lista de pares solo si hay UNA manera de
    hacerlo en la que todos los pares cuadren; si hay cero o varias, None.

    formas[i][j] = cuantas maneras hay de emparejar cfdis[i:] con
    peticiones[j:] (con tope 2: solo importa si es una o mas de una)."""
    n, m = len(cfdis), len(peticiones)
    if n == 0 or m < n:
        return None
    ok = [[cuadra(peticiones[j], cfdis[i]) for j in range(m)] for i in range(n)]
    formas = [[0] * (m + 1) for _ in range(n + 1)]
    formas[n] = [1] * (m + 1)
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            formas[i][j] = min(2, formas[i][j + 1] + (formas[i + 1][j + 1] if ok[i][j] else 0))
    if formas[0][0] != 1:
        return None
    pares, i = [], 0
    for j in range(m):
        if i < n and ok[i][j] and formas[i + 1][j + 1] == 1 and formas[i][j + 1] == 0:
            pares.append((cfdis[i], peticiones[j]))
            i += 1
    return pares if i == n else None


def _grupos_cerrados(cfdis, pres, cuadra, ftm, usos):
    """Pares (cfdi, peticion) de los grupos cerrados. Se arma un grafo: cada
    CFDI sin ligar se une con cada peticion libre que le cuadra (precio, UPC,
    piezas, peticion antes del timbrado). En cada componente conexo:
      - tantos CFDI como peticiones;
      - todos los CFDI con el mismo receptor (RFC), UPC, total y piezas;
      - las piezas caben en sus lineas;
      - se emparejan en orden (folio del CFDI con cfdiid de la peticion) y en
        cada par la peticion es anterior al timbrado.
    Entonces todas esas peticiones son de ese cliente y cada orden recibe sus
    piezas (mismo cliente, producto y precio). El orden por folio ademas pone
    cada UUID en su orden: emparejar por fecha cruzaba 8 de 24 entre ordenes
    del mismo cliente, lo que despues confunde la NC de una devolucion."""
    padre = {}

    def raiz(x):
        padre.setdefault(x, x)
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    por_id = {p['cfdiid']: p for p in pres}
    for c in cfdis:
        raiz(('c', c['uuid']))
        for p in pres:
            if p['t'] is not None and cuadra(p, c):
                padre[raiz(('c', c['uuid']))] = raiz(('p', p['cfdiid']))
    grupos = defaultdict(lambda: ([], []))
    for c in cfdis:
        grupos[raiz(('c', c['uuid']))][0].append(c)
    for nodo in list(padre):
        if nodo[0] == 'p':
            grupos[raiz(nodo)][1].append(por_id[nodo[1]])
    pares = []
    for cs, ps in grupos.values():
        if not cs or len(cs) != len(ps):
            continue
        if GRUPO_CERRADO == 'orden' and len({p['orden'] for p in ps}) != 1:
            continue
        firma = {(c['receptor'], c['upc'], round(c['total'], 2), c['cant']) for c in cs}
        if len(firma) != 1 or not cs[0]['receptor']:
            continue
        piezas = Counter()
        for p in ps:
            piezas[p['iderp']] += cs[0]['cant']
        if any(usos[k] + n > ftm[k][1] for k, n in piezas.items()):
            continue
        # En orden: el Folio de la serie FI crece igual que el cfdiid de la
        # peticion (0 inversiones en octubre). Si algun par queda con la
        # peticion despues del timbrado, el grupo no se liga.
        folios = [_folio_de(c) for c in cs]
        if None in folios or len(set(folios)) != len(folios):
            continue
        grupo = list(zip(sorted(cs, key=_folio_de), sorted(ps, key=lambda p: p['cfdiid'])))
        if all(p['t'] <= c['fecha'] for c, p in grupo):
            pares.extend(grupo)
    return pares


def _folio_de(c):
    f = c.get('folio') or ''
    return int(f) if f.isdigit() and len(f) <= 9 else None


def ligar_cfdis(db, dry_run=False):
    """Liga cada CFDI:<UUID> SIN_LIGAR con su linea de Walmart (iderp) y su
    peticion (cfdiid). Una linea no recibe mas CFDI que piezas vendidas y una
    peticion no recibe mas de un CFDI. Un par "cuadra" si precio unitario de
    FTM x cantidad del XML = total (+-0.02), el UPC del XML es el de la linea
    (wm_order_line) y la peticion es anterior al timbrado.

    1. Crawler (si WM_USAR_CRAWLER y trae el UUID): liga exacta.
    2. Folio que empieza con el numero de pedido: una sola orden posible.
    3. Camino rapido: peticion RECIBIDO entre 0 y VENTANA_LIGADO_MIN minutos
       antes de la Fecha del XML y una sola orden posible.
    4. Tramo de folios: los CFDI sin ligar entre dos CFDI ligados (por folio)
       se emparejan, en orden, con las peticiones libres cuyo cfdiid esta entre
       los de esos dos; solo si hay una sola manera. Se repite mientras ligue.
    5. Grupo cerrado (ver _grupos_cerrados): N CFDI iguales del mismo cliente
       que solo pueden ser de N peticiones, y esas peticiones de ningun otro
       CFDI. WM_GRUPO_CERRADO: 'orden' (por defecto) solo si las N peticiones
       son de la misma orden; 'cliente' tambien entre ordenes del mismo
       cliente; '0' apagado.
    Lo demas queda SIN_LIGAR (no se adivina: un CFDI pegado a la orden
    equivocada no se corrige despues). Devuelve un Counter."""
    stats = Counter()
    cur = db.cursor()
    cur.execute(f"""SELECT id, cfdi_uuid, raw_json, status FROM {TBL_BILLING}
                    WHERE marketplace = %s AND mkp_order_id LIKE 'CFDI:%%'""", (MKP_FENIX,))
    filas, usos, usados = {}, Counter(), set()
    for rid, uuid, raw, status in cur.fetchall():
        raw = _raw(raw)
        uuid = normalizar_uuid(uuid)
        filas[uuid] = {'id': rid, 'raw': raw, 'status': status}
        if status == CFDI_LIGADO and raw.get('iderp'):
            usos[raw['iderp']] += int(round(a_float(raw.get('cantidad')) or 1))
            if raw.get('cfdiid'):
                usados.add(int(raw['cfdiid']))
    sueltos = [u for u, f in filas.items() if f['status'] == CFDI_SIN_LIGAR]
    if not sueltos:
        cur.close()
        return stats

    ligas = {}

    def ligar(uuid, iderp, orden, cfdiid, metodo):
        ligas[uuid] = {'iderp': iderp, 'orden': orden, 'cfdiid': cfdiid, 'metodo': metodo}
        usos[iderp] += int(round(a_float(filas[uuid]['raw'].get('cantidad')) or 1))
        if cfdiid:
            usados.add(int(cfdiid))

    if USAR_CRAWLER:
        for chunk in lotes(sueltos, 500):
            cur.execute(f"""SELECT UPPER(uuid), iderp, pedido, identificador FROM somos_reyes.wm_fenix_invoices_report
                            WHERE tipo_documento = 'I' AND UPPER(uuid) IN ({','.join(['%s'] * len(chunk))})""",
                        tuple(chunk))
            for u, iderp, pedido, ident in cur.fetchall():
                ligar(normalizar_uuid(u), iderp, str(pedido), int(ident) if ident else None, 'crawler')

    cur.execute(f"""SELECT raw_json FROM {TBL_BILLING}
                    WHERE marketplace = %s AND mkp_order_id LIKE 'PRECFDI:%%' AND status = %s""",
                (MKP_FENIX, PRE_RECIBIDO))
    pres = []
    for (raw,) in cur.fetchall():
        raw = _raw(raw)
        if raw.get('cfdiid') and raw.get('iderp'):
            pres.append({'cfdiid': int(raw['cfdiid']), 'iderp': raw['iderp'], 'orden': str(raw.get('orderno')),
                         't': a_datetime(raw['peticion_cdmx']) if raw.get('peticion_cdmx') else None})
    pres.sort(key=lambda p: p['cfdiid'])
    # Anclas ya ligadas en corridas anteriores, por cfdiid (para los tramos)
    for f in filas.values():
        if f['status'] == CFDI_LIGADO and f['raw'].get('cfdiid'):
            f['cfdiid'] = int(f['raw']['cfdiid'])
    ftm = _lineas_ftm(cur, {p['iderp'] for p in pres})
    upcs = upc_por_linea(cur, {p['iderp'] for p in pres})

    datos = {}
    for u in sueltos:
        raw = filas[u]['raw']
        try:
            fecha = datetime.fromisoformat(str(raw['fecha'])[:19])
        except (KeyError, ValueError):
            stats['sin_ligar_sin_fecha'] += 1
            continue
        datos[u] = {'uuid': u, 'fecha': fecha, 'cant': a_float(raw.get('cantidad')) or 1.0,
                    'total': a_float(raw.get('total')), 'upc': str(raw.get('upc') or '').strip().lstrip('0'),
                    'folio': str(raw.get('folio') or '').strip(),
                    'receptor': str(raw.get('receptor_rfc') or '').strip().upper()}

    def cuadra(p, c):
        k = p['iderp']
        return (p['cfdiid'] not in usados and k in ftm
                and (p['t'] is None or p['t'] <= c['fecha'])
                and abs(ftm[k][0] * c['cant'] - c['total']) <= TOLERANCIA_PRECIO_LIGADO
                and usos[k] + c['cant'] <= ftm[k][1]
                and (not upcs.get(k) or not c['upc'] or upcs[k] == c['upc']))

    def unica_orden(c, cand, metodo, elegir):
        if len({p['orden'] for p in cand}) == 1:
            p = elegir(cand)
            ligar(c['uuid'], p['iderp'], p['orden'], p['cfdiid'], metodo)

    # 2. Folio con el pedido al inicio
    for c in datos.values():
        if c['uuid'] not in ligas and len(c['folio']) >= 15:
            unica_orden(c, [p for p in pres if c['folio'].startswith(p['orden']) and cuadra(p, c)],
                        'folio_pedido', lambda cand: min(cand, key=lambda q: q['cfdiid']))
    # 3. Camino rapido
    ventana = timedelta(minutes=VENTANA_LIGADO_MIN)
    for c in sorted(datos.values(), key=lambda d: d['fecha']):
        if c['uuid'] not in ligas:
            unica_orden(c, [p for p in pres if p['t'] and timedelta(0) <= c['fecha'] - p['t'] <= ventana
                            and cuadra(p, c)],
                        'api_ventana', lambda cand: max(cand, key=lambda q: q['t']))
    # 4. Tramos de folio entre anclas
    ids = [p['cfdiid'] for p in pres]
    cambio = True
    while cambio:
        cambio = False
        secuencia = []   # (folio, uuid, cfdiid del ancla o None)
        for u, f in filas.items():
            folio = _folio_secuencial(f['raw'])
            if folio is None:
                continue
            if u in ligas:
                if ligas[u]['cfdiid']:
                    secuencia.append((folio, u, ligas[u]['cfdiid']))
            elif f['status'] == CFDI_LIGADO:
                if f.get('cfdiid'):
                    secuencia.append((folio, u, f['cfdiid']))
            elif u in datos:
                secuencia.append((folio, u, None))
        secuencia.sort()
        anclas = [i for i, s in enumerate(secuencia) if s[2]]
        for i1, i2 in zip(anclas, anclas[1:]):
            tramo = [datos[s[1]] for s in secuencia[i1 + 1:i2] if s[1] not in ligas]
            lo, hi = secuencia[i1][2], secuencia[i2][2]
            if not tramo or hi <= lo:
                continue
            libres = [p for p in pres[bisect.bisect_right(ids, lo):bisect.bisect_left(ids, hi)]
                      if p['cfdiid'] not in usados]
            pares = _emparejar_en_orden(tramo, libres, cuadra)
            if not pares:
                continue
            # cuadra() se evaluo par por par: confirmar que juntos no rebasan las piezas de una linea
            piezas = Counter()
            for c, p in pares:
                piezas[p['iderp']] += c['cant']
            if any(usos[k] + n > ftm[k][1] for k, n in piezas.items()):
                continue
            for c, p in pares:
                ligar(c['uuid'], p['iderp'], p['orden'], p['cfdiid'], 'folio_tramo')
            cambio = True

    # 5. Grupo cerrado
    for c, p in [] if GRUPO_CERRADO not in ('orden', 'cliente') else _grupos_cerrados([c for c in datos.values() if c['uuid'] not in ligas], pres, cuadra, ftm, usos):
        ligar(c['uuid'], p['iderp'], p['orden'], p['cfdiid'], 'grupo_cerrado')

    for u in sueltos:
        if u not in ligas:
            stats['sin_ligar'] += 1
    for u, l in ligas.items():
        stats[f"ligado_{l['metodo']}"] += 1
        if dry_run:
            continue
        f = filas[u]
        f['raw'].update(l, ligado_en=f"{ahora_cdmx():%Y-%m-%d %H:%M:%S}")
        cur.execute(f"""UPDATE {TBL_BILLING} SET status = %s, raw_json = %s, processed_at = NOW()
                        WHERE id = %s AND status = %s""", (CFDI_LIGADO, a_json(f['raw']), f['id'], CFDI_SIN_LIGAR))
    if not dry_run:
        db.commit()
    cur.close()
    return stats
