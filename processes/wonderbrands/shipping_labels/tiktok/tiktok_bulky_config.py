"""
Configuración unificada para la automatización de guías Bulky de TikTok Shop.

Todo lo que sea credencial, ID de documento o regla de negocio parametrizable
vive aquí y se lee de variables de entorno (.env local / secrets de Kestra).
El script principal (`tiktok_bulky_fulfillment.py`) sólo importa de este módulo,
nunca lee `os.getenv()` por su cuenta.

Convención de nombres de variables: se reutiliza la misma que ya usan los
scripts de Amazon, Coppel y Shopify del repo (DB_HOST, AUTH_USER, odoo_urlV18,
GOOGLE_CREDS_JSON, etc.) para no fragmentar el .env.
"""

import json
import logging
import os
import sys
from pathlib import Path

import dotenv

# ==========================================================================
# CARGA DEL .env
# ==========================================================================
# En local basta con tener el .env junto al script (o exportar ENV_PATH).
# En Kestra las variables ya vienen inyectadas y load_dotenv simplemente no
# encuentra archivo, lo cual no es un error.
ENV_PATH = os.getenv('ENV_PATH') or str(Path(__file__).resolve().parent / '.env')
dotenv.load_dotenv(dotenv_path=ENV_PATH, override=False)
dotenv.load_dotenv(override=False)  # fallback: .env en el cwd


# ==========================================================================
# LOGGING
# ==========================================================================
LOG_LEVEL = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)


def configure_logging():
    logging.basicConfig(
        level=LOG_LEVEL,
        format='%(asctime)s - %(levelname)s - %(message)s',
    )
    return logging.getLogger()


def get_summary_logger():
    """
    Logger de resumen operativo, independiente de LOG_LEVEL.

    En Kestra el detalle técnico se silencia con LOG_LEVEL=ERROR, pero el
    resumen (qué órdenes se procesaron, cuáles quedaron manuales y por qué)
    debe verse SIEMPRE en el build. Por eso este logger tiene su propio
    handler y no propaga al root.
    """
    summary = logging.getLogger('tiktok_bulky.resumen')
    if not summary.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
        summary.addHandler(handler)
        summary.setLevel(logging.INFO)
        summary.propagate = False
    return summary


# ==========================================================================
# INTERRUPTORES DE EJECUCIÓN
# ==========================================================================
def _flag(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).strip().lower() in ('1', 'true', 'yes', 'y')


# DRY_RUN: cotiza y valida, pero NO genera guías ni escribe en TikTok/Odoo/BD.
# Sí escribe en Google Sheets para poder revisar el resultado esperado.
DRY_RUN = _flag('TIKTOK_BULKY_DRY_RUN', 'false')

# Apunta la API interna de guías al entorno de pruebas (prefijo qa_).
IS_TEST = 'qa_' if _flag('TEST_API_LABELS_SR', 'false') else ''

# Límite de órdenes por corrida (0 = sin límite). Útil para el primer piloto.
MAX_ORDERS = int(os.getenv('TIKTOK_BULKY_MAX_ORDERS', '0'))

# Ventana de búsqueda de órdenes en TikTok.
LOOKBACK_HOURS = int(os.getenv('TIKTOK_BULKY_LOOKBACK_HOURS', '72'))
PAGE_SIZE = int(os.getenv('TIKTOK_BULKY_PAGE_SIZE', '50'))

# Pausa entre órdenes para no saturar GSheets / API interna.
SLEEP_BETWEEN_ORDERS = float(os.getenv('SLEEP_BETWEEN_ORDERS', '3'))


# ==========================================================================
# REGLAS DE NEGOCIO
# ==========================================================================
# La guía NO se genera si el costo supera este % del valor de la orden.
LIMIT_RATIO_PERCENTAGE = float(os.getenv('LIMIT_RATIO_PERCENTAGE', '0.21'))

# Clave SAT genérica de mercancía para el complemento carta porte.
SAT_BIENES_TRANSP = os.getenv('SAT_BIENES_TRANSP', '50161815')

MARKETPLACE_NAME = 'TikTok'


# ==========================================================================
# TIENDAS BULKY
# ==========================================================================
# `slug` es el prefijo de las variables de entorno de cada tienda:
#     TIKTOK_<SLUG>_APP_KEY / _APP_SECRET / _SHOP_CIPHER / _SELLER_NAME
# `seller_name` debe coincidir EXACTAMENTE con la columna seller_name de
# somos_reyes.tiktok_shop_tokens (de ahí sale el access/refresh token).
# `enabled` se apaga desde el .env con TIKTOK_<SLUG>_ENABLED=false.
_SHOP_DEFINITIONS = [
    {'slug': 'NEON', 'seller_name': 'Neon', 'default_enabled': 'false'},          # próximamente
    {'slug': 'KH', 'seller_name': 'KingsHouse', 'default_enabled': 'true'},
    {'slug': 'CDH', 'seller_name': 'ColorDreams Home', 'default_enabled': 'true'},
]


def get_active_shops() -> list:
    """
    Devuelve la lista de tiendas habilitadas y con credenciales completas.

    Una tienda sin app_key/app_secret/shop_cipher se omite con warning en vez
    de tumbar la corrida: así Neon puede quedar declarada desde hoy y
    encenderse sólo agregando sus variables al .env.
    """
    logger = logging.getLogger()
    shops = []
    for definition in _SHOP_DEFINITIONS:
        slug = definition['slug']
        if not _flag(f'TIKTOK_{slug}_ENABLED', definition['default_enabled']):
            logger.info(f"Tienda {definition['seller_name']} deshabilitada por configuración. Omitida.")
            continue

        shop = {
            'slug': slug,
            'seller_name': os.getenv(f'TIKTOK_{slug}_SELLER_NAME', definition['seller_name']),
            'app_key': os.getenv(f'TIKTOK_{slug}_APP_KEY'),
            'app_secret': os.getenv(f'TIKTOK_{slug}_APP_SECRET'),
            'shop_cipher': os.getenv(f'TIKTOK_{slug}_SHOP_CIPHER'),
        }
        missing = [k for k in ('app_key', 'app_secret', 'shop_cipher') if not shop[k]]
        if missing:
            logger.warning(
                f"Tienda {shop['seller_name']} habilitada pero sin credenciales "
                f"({', '.join(f'TIKTOK_{slug}_{m.upper()}' for m in missing)}). Omitida."
            )
            continue
        shops.append(shop)
    return shops


# ==========================================================================
# API DE TIKTOK SHOP
# ==========================================================================
TIKTOK_BASE_URL = os.getenv('TIKTOK_BASE_URL', 'https://open-api.tiktokglobalshop.com')
TIKTOK_AUTH_URL = os.getenv('TIKTOK_AUTH_URL', 'https://auth.tiktok-shops.com')
TIKTOK_TIMEOUT = (10, 45)

# Tabla donde viven los tokens por tienda (ya la usa el fulfillment actual).
TIKTOK_TOKENS_TABLE = os.getenv('TIKTOK_TOKENS_TABLE', 'somos_reyes.tiktok_shop_tokens')
TOKEN_REFRESH_MARGIN_SECONDS = int(os.getenv('TOKEN_REFRESH_MARGIN_SECONDS', '3600'))

# Sólo se procesan órdenes cuyo shipping_type esté en esta lista: son las que
# TikTok NO surte con guía propia y por tanto requieren guía manual (Bulky).
MERCHANT_SHIPPING_TYPES = ('SELLER',)

# Método de entrega al reportar el paquete. Para envíos del vendedor TikTok
# normalmente acepta DROP_OFF (nosotros entregamos a la paquetería).
# Vacío = no se envía el campo (algunos shops lo rechazan en self-shipment).
TIKTOK_HANDOVER_METHOD = os.getenv('TIKTOK_HANDOVER_METHOD', 'DROP_OFF').strip()

# Cómo se reporta nuestra guía a TikTok. Son dos endpoints con propósitos
# DISTINTOS, no dos alternativas equivalentes:
#   'ship'          -> POST /fulfillment/202309/packages/{id}/ship
#                      Despacha el paquete. Es el camino normal.
#   'shipping_info' -> POST /fulfillment/202309/orders/{order_id}/shipping_info/update
#                      CORRIGE la guía de una orden YA despachada (scope
#                      seller.logistics). Sirve para reparar, no para enviar.
#   'auto'          -> despacha con 'ship' y, si falla porque el paquete ya
#                      estaba despachado, corrige con 'shipping_info' (default)
TIKTOK_SHIP_STRATEGY = os.getenv('TIKTOK_SHIP_STRATEGY', 'auto').strip().lower()

# Catálogo de paqueterías (Get Shipping Providers 202309). La ruta EXIGE el
# delivery_option_id; no existe una variante sin él (por eso
# /logistics/202309/shipping_providers responde 404 / 36009009):
#   GET /logistics/202309/delivery_options/{id}/shipping_providers
#       ?warehouse_region=MX&buyer_region=MX
TIKTOK_SHIPPING_PROVIDERS_PATH = (
    '/logistics/202309/delivery_options/{delivery_option_id}/shipping_providers'
)

# Regiones que acompañan la consulta del catálogo.
TIKTOK_WAREHOUSE_REGION = os.getenv('TIKTOK_WAREHOUSE_REGION', 'MX').strip()

# delivery_option_id fijo, por si el descubrimiento automático falla.
TIKTOK_DELIVERY_OPTION_ID = os.getenv('TIKTOK_DELIVERY_OPTION_ID', '').strip()

# Mapa carrier -> shipping_provider_id de TikTok.
#
# IMPORTANTE: estos IDs son el catálogo devuelto para KingsHouse
# (delivery_option_id 7360882931864209158). TikTok los entrega por delivery
# option, así que otra tienda PODRÍA devolver IDs distintos. Por eso el script
# consulta el catálogo real primero y sólo cae a este mapa si la consulta
# falla: reportar un provider_id equivocado le mostraría al comprador una
# paquetería que no es.
#
# Se puede extender/sobrescribir desde el .env con un JSON:
#   TIKTOK_SHIPPING_PROVIDER_IDS={"FEDEX":"7046330616433870593"}
TIKTOK_SHIPPING_PROVIDER_IDS = {
    'DHL Express': '7043781643047274241',
    'DHL': '7043781643047274241',
    'FedEx': '7046330616433870593',
    'FEDEX': '7046330616433870593',
    'Amazon Logistics + MCF': '7046331959399679746',
    'UPS': '7345645330412603141',
    '99 Minutos': '7360935685572118278',
    'Estafeta': '7360938269065873158',
    'ESTAFETA': '7360938269065873158',
    'Mexico Post': '7360940494693795590',
    'bigsmart MX': '7360943477548730118',
    'Paquetexpress': '7360943911512409861',
    'PAQUETEXPRESS': '7360943911512409861',
    'PAQUETEEXPRESS': '7360943911512409861',
    'Redpack': '7361711809394706181',
    'iMile MX': '7361793703653934864',
    'J&T MX': '7361795014805948161',
    'J&TExpress': '7361795014805948161',
    'Ivoy': '7429631802710460166',
}

try:
    TIKTOK_SHIPPING_PROVIDER_IDS.update(
        json.loads(os.getenv('TIKTOK_SHIPPING_PROVIDER_IDS', '{}'))
    )
except json.JSONDecodeError:
    logging.getLogger().warning(
        "TIKTOK_SHIPPING_PROVIDER_IDS no es un JSON válido; se ignora."
    )


# Equivalencias entre el nombre que usa NUESTRA API de guías y el que usa el
# catálogo de TikTok. Sin esto, 'PAQUETEEXPRESS' (dos E) nunca empata con
# 'Paquetexpress' (una E) y la orden se iría a manual sin razón real.
# Llave y valores se comparan normalizados (sin espacios, símbolos ni mayúsculas).
CARRIER_NAME_ALIASES = {
    'paqueteexpress': ['paquetexpress'],
    'paquetexpress': ['paqueteexpress'],
    'jtexpress': ['jtmx', 'jt'],
    'jt': ['jtmx', 'jtexpress'],
    'imile': ['imilemx'],
    'bigsmart': ['bigsmartmx'],
    'correosdemexico': ['mexicopost'],
    'mexicopost': ['correosdemexico'],
    '99minutos': ['99minutos'],
    'amazon': ['amazonlogisticsmcf'],
}


# ==========================================================================
# ESTADOS DE MÉXICO (nombre -> código de 3 letras)
# ==========================================================================
# TikTok devuelve el estado con su nombre completo ("Ciudad de México"), pero
# las paqueteras exigen el código de 3 letras.
#
# NOTA: este mapeo idealmente vive en la API interna de cotización/guías, para
# que Amazon, Coppel, TikTok y lo que venga después manden el nombre y la API
# resuelva el código en un solo lugar. Mientras eso no exista, se traduce aquí.
# Cuando la API lo absorba, basta poner MAP_STATE_TO_CODE=false y se enviará el
# nombre tal como lo da TikTok.
MAP_STATE_TO_CODE = _flag('MAP_STATE_TO_CODE', 'true')

MX_STATE_CODES = {
    'aguascalientes': 'AGU',
    'baja california': 'BCN',
    'baja california sur': 'BCS',
    'campeche': 'CAM',
    'chiapas': 'CHP',
    'chihuahua': 'CHH',
    'ciudad de mexico': 'CMX',
    'cdmx': 'CMX',
    'distrito federal': 'CMX',
    'mexico city': 'CMX',
    'coahuila': 'COA',
    'coahuila de zaragoza': 'COA',
    'colima': 'COL',
    'durango': 'DUR',
    'guanajuato': 'GUA',
    'guerrero': 'GRO',
    'hidalgo': 'HID',
    'jalisco': 'JAL',
    'estado de mexico': 'MEX',
    'mexico': 'MEX',
    'michoacan': 'MIC',
    'michoacan de ocampo': 'MIC',
    'morelos': 'MOR',
    'nayarit': 'NAY',
    'nuevo leon': 'NLE',
    'oaxaca': 'OAX',
    'puebla': 'PUE',
    'queretaro': 'QUE',
    'quintana roo': 'ROO',
    'san luis potosi': 'SLP',
    'sinaloa': 'SIN',
    'sonora': 'SON',
    'tabasco': 'TAB',
    'tamaulipas': 'TAM',
    'tlaxcala': 'TLA',
    'veracruz': 'VER',
    'veracruz de ignacio de la llave': 'VER',
    'yucatan': 'YUC',
    'zacatecas': 'ZAC',
}


# ==========================================================================
# API INTERNA DE COTIZACIÓN Y GUÍAS (Shipping Routing System)
# ==========================================================================
API_URL_LIVE_RATES = f"https://wonder-site.duckdns.org/{IS_TEST}live-rates"
API_URL_GENERATE_LABEL = f"https://wonder-site.duckdns.org/{IS_TEST}generate-label"
API_AUTH = (os.getenv('AUTH_USER'), os.getenv('AUTH_PASS'))
API_TIMEOUT_RATES = 30
API_TIMEOUT_LABEL = 45

LABELARY_URL = 'http://api.labelary.com/v1/printers/8dpmm/labels/4x6/'


# ==========================================================================
# REMITENTE (SHIPPER)
# ==========================================================================
ORIGIN_ZIP = os.getenv('ORIGIN_ZIP', '54010')

SHIPPER_DATA = {
    "name": "Equipo Somos Reyes",
    "company": "SOMOS REYES",
    "email": "info@somos-reyes.com",
    "phone": "5568309828",
    "street1": "BENITO JUAREZ 11/B6",
    "street2": "SAN PEDRO BARRIENTOS",
    "city": "Tlalnepantla de Baz",
    "state": "MEX",
    "country": "MX",
    "zip": ORIGIN_ZIP,
}


# ==========================================================================
# ODOO 18
# ==========================================================================
ODOO_URL = os.getenv('odoo_urlV18')
ODOO_DB = os.getenv('odoo_dbV18')
ODOO_USER = os.getenv('odoo_user_dataV18')
ODOO_PASSWORD = os.getenv('odoo_password_dataV18')

# Campos custom donde viven la guía y el carrier en sale.order.
ODOO_TRACKING_FIELD = os.getenv('ODOO_TRACKING_FIELD', 'data_tracking_readwrite')
ODOO_CARRIER_FIELD = os.getenv('ODOO_CARRIER_FIELD', 'data_carrier_selection_relational')

# Campos por los que se busca la orden de TikTok en Odoo (en orden).
ODOO_ORDER_REFERENCE_FIELDS = ('channel_order_reference', 'channel_order_id')

# Mapa carrier -> carriers.list.id de Odoo (mismo que Amazon/Coppel).
ODOO_CARRIER_IDS = {
    'fedex': 1,
    'estafeta': 2,
    'dhl': 3,
    'paqueteexpress': 4,
    'segmail': 7,
}


# ==========================================================================
# BASE DE DATOS (tools.shipping_labels)
# ==========================================================================
DB_CONFIG = {
    'host': os.getenv('DB_HOST'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
    'database': os.getenv('DB_NAME'),
}


# ==========================================================================
# GOOGLE SHEETS
# ==========================================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Contenido JSON de la cuenta de servicio (igual que Coppel) o ruta al archivo.
GOOGLE_CREDS_JSON = os.getenv('GOOGLE_CREDS_JSON')

# Documento de reportería que ya se usa para TikTok.
SPREADSHEET_TIKTOK_ID = os.getenv(
    'SPREADSHEET_TIKTOK_ID', '1eMbXb_lb8WRYO1jr8jcBnfq495kaLc3Do9Wyf8BxTnM'
)

SHEET_SUCCESS = '[TikTok_Bulky] PROCESADAS'
SHEET_MANUAL = '[TikTok_Bulky] MANUALES'

# Encabezados EXACTOS de las pestañas ya existentes en el documento.
# El orden importa: es el orden en que se escriben las filas.
SHEET_SUCCESS_HEADERS = [
    'Time-stamp', 'Seller Name', 'Order Date', 'ID TikTok', 'ID Odoo', 'Status',
    'SKU(s)', 'Guías (tracking)', 'Carrier', 'Costo total guia(s)',
    'Total orden', 'Ratio',
]

SHEET_MANUAL_HEADERS = [
    'Time-stamp', 'Seller Name', 'Order date', 'ID TikTok', 'ID Odoo', 'Status',
    'Reason', 'Attemps', 'SKU(s)', 'Carrier', 'Total cost shipping',
    'Total order', 'Ratio',
]

# Columna (1-based) que identifica la orden en cada pestaña. Es la llave que
# usa el script para actualizar la fila existente en vez de duplicarla.
SHEET_KEY_COLUMN = SHEET_MANUAL_HEADERS.index('ID TikTok') + 1  # columna D

# Texto para las celdas que no aplican al motivo registrado.
NOT_APPLICABLE = 'No-Aplica'


# ==========================================================================
# MÓDULO COMPARTIDO tools.shipping_labels
# ==========================================================================
def _register_shared_module_path():
    """
    Agrega al sys.path la carpeta `_shared` que contiene
    `_00_shipping_labels_db.insert_shipping_label`, buscándola tanto desde
    test/ como desde processes/ (la ruta cambia según dónde se ejecute).
    """
    here = Path(__file__).resolve()
    override = os.getenv('SHIPPING_LABELS_SHARED_PATH')
    candidates = [Path(override)] if override else []
    candidates.append(here.parents[2] / '_shared')  # <marketplaces>/_shared
    # repo_root/processes/wonderbrands/shipping_labels/_shared, subiendo niveles
    relative = Path('processes') / 'wonderbrands' / 'shipping_labels' / '_shared'
    candidates.extend(parent / relative for parent in here.parents[3:7])

    for candidate in candidates:
        if candidate.is_dir() and (candidate / '_00_shipping_labels_db.py').is_file():
            if str(candidate) not in sys.path:
                sys.path.append(str(candidate))
            return str(candidate)
    return None


SHARED_MODULE_PATH = _register_shared_module_path()
