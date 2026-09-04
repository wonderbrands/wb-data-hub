"""
=============================================================================
 TikTok Shop - Fulfillment de órdenes BULKY (guía generada por nosotros)
=============================================================================

Modalidad "Tiendas Bulky": TikTok NO entrega la guía; nosotros cotizamos y
generamos la guía con nuestras paqueteras en convenio a través de la API
interna (Shipping Routing System), la cargamos a Odoo y reportamos el tracking
de vuelta a TikTok para marcar la orden como enviada.


Flujo por orden:
    1. TikTok  -> descargar órdenes AWAITING_SHIPMENT con shipping_type=SELLER
    2. SRS     -> cotizar (/live-rates) y validar reglas de negocio
    3. SRS     -> generar guía(s) (/generate-label)
    4. Odoo    -> tracking, carrier, PDF adjunto y mensaje en el chatter
    5. TikTok  -> ship package con tracking + shipping_provider_id
    6. MySQL   -> INSERT en tools.shipping_labels
    7. GSheets -> Guias_automaticas_generadas / Guias_automaticas_manuales

Toda credencial y parámetro vive en `tiktok_bulky_config.py` (.env).
"""

import base64
import hashlib
import hmac
import io
import json
import time
import unicodedata
import xmlrpc.client
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import gspread
import mysql.connector
import requests
from google.oauth2.service_account import Credentials

import tiktok_bulky_config as cfg

logger = cfg.configure_logging()
# Resumen operativo: siempre visible, incluso con LOG_LEVEL=ERROR en Kestra.
resumen = cfg.get_summary_logger()

# `upsert_shipping_label` (más abajo) delega en el INSERT/UPDATE inteligente
# del módulo compartido para mantener UN registro por (orden, SKU).
from _00_shipping_labels_db import (
    insert_shipping_label,
    sku_shipping_cost_from_labels,
    sku_shipping_cost_from_rates,
)


# PyPDF2 sólo se usa para consolidar guías multicaja en un único PDF. Si no
# está instalado, cada guía se adjunta por separado en Odoo.
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
    logger.warning(
        "PyPDF2 no está instalado: las guías multicaja se adjuntarán "
        "como archivos separados en Odoo."
    )


CDMX_TZ = timezone(timedelta(hours=-6))

# Cachés de corrida: el catálogo de paqueterías y el delivery_option_id no
# cambian entre órdenes de la misma tienda/destino.
_SHOP_CACHE = {}
_PROVIDERS_CACHE = {}


# =============================================================================
# UTILIDADES
# =============================================================================
def now_cdmx_str() -> str:
    return datetime.now(CDMX_TZ).strftime('%Y-%m-%d %H:%M:%S')


def epoch_to_cdmx_str(value) -> str:
    """Convierte un timestamp de TikTok (segundos, UTC) a texto CDMX."""
    try:
        return datetime.fromtimestamp(int(value), CDMX_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OSError):
        return ''


def clean_phone(raw_phone) -> str:
    """TikTok enmascara teléfonos como '(+52)5512345678'. Deja 10 dígitos."""
    digits = ''.join(filter(str.isdigit, str(raw_phone or '')))
    if digits.startswith('52') and len(digits) > 10:
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else digits.zfill(10)


def normalize_carrier(value: str) -> str:
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


def get_carrier_odoo_id(provider_name: str):
    """Mapea el nombre del carrier de la API interna al ID de carriers.list."""
    normalized = normalize_carrier(provider_name)
    for alias, carrier_id in cfg.ODOO_CARRIER_IDS.items():
        if alias in normalized:
            return carrier_id
    logger.warning(f"Odoo: sin mapeo de carrier para '{provider_name}'.")
    return None


def convert_zpl_to_pdf_bytes(zpl_string: str):
    """Convierte un ZPL a PDF con Labelary (misma lógica que Amazon/Coppel)."""
    try:
        response = requests.post(
            cfg.LABELARY_URL,
            headers={"Accept": "application/pdf"},
            data=zpl_string,
            timeout=15,
        )
        response.raise_for_status()
        return response.content or None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error convirtiendo ZPL a PDF con Labelary: {e}")
        return None


# =============================================================================
# BASE DE DATOS
# =============================================================================
def get_db_connection():
    """Abre la conexión MySQL usada para tokens y para tools.shipping_labels."""
    try:
        conn = mysql.connector.connect(**cfg.DB_CONFIG)
        logger.info("Conexión a MySQL establecida.")
        return conn
    except Exception as e:
        logger.critical(f"Error fatal al conectar a MySQL: {e}")
        return None


def fetch_processed_order_ids(conn, seller_name: str) -> set:
    """
    Devuelve los order_id de TikTok que YA tienen guía generada en
    tools.shipping_labels. Es el candado anti-duplicados entre corridas.
    """
    if not conn:
        return set()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT marketplace_id
            FROM tools.shipping_labels
            WHERE marketplace = %s
              AND label_generated = 1
              AND label_generated_at >= NOW() - INTERVAL 90 DAY
            """,
            (cfg.MARKETPLACE_NAME,),
        )
        processed = {str(row[0]) for row in cursor.fetchall()}
        cursor.close()
        logger.info(
            f"[{seller_name}] {len(processed)} órdenes TikTok con guía previa en "
            f"tools.shipping_labels (se omitirán)."
        )
        return processed
    except Exception as e:
        logger.error(f"Error consultando tools.shipping_labels: {e}")
        return set()


def upsert_shipping_label(conn, marketplace_id, sku, qty_ordered, status,
                          label_generated, label_origin='SRS_GENERATED',
                          tracking_number=None, shipping_cost=None, carrier=None,
                          carrier_service_level=None, error_log=None):
    """
    Wrapper delgado sobre `insert_shipping_label` del módulo compartido, que
    ya mantiene UN solo registro por (marketplace, marketplace_id, sku)
    (UPDATE si existe, INSERT si no). Sólo fija `marketplace`; `label_generated_at`
    se deja en manos del módulo compartido (hora del servidor), igual que en
    Amazon/Coppel, para no mezclar horario CDMX con la hora de servidor que
    ya usan `updated_at`/`inserted_at` en esa misma tabla.
    """
    insert_shipping_label(
        conn,
        marketplace_id=marketplace_id,
        marketplace=cfg.MARKETPLACE_NAME,
        sku=sku,
        qty_ordered=qty_ordered,
        status=status,
        label_generated=label_generated,
        label_origin=label_origin,
        tracking_number=tracking_number,
        shipping_cost=shipping_cost,
        carrier=carrier,
        carrier_service_level=carrier_service_level,
        error_log=error_log,
    )


# =============================================================================
# TOKENS DE TIKTOK (somos_reyes.tiktok_shop_tokens)
# =============================================================================
def load_shop_token(conn, seller_name: str):
    """Lee el token vigente de la tienda desde la tabla de tokens."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT access_token, refresh_token, access_token_expire_in,
                   refresh_token_expire_in
            FROM {cfg.TIKTOK_TOKENS_TABLE}
            WHERE seller_name = %s
            """,
            (seller_name,),
        )
        row = cursor.fetchone()
        cursor.close()
    except Exception as e:
        logger.error(f"[{seller_name}] Error leyendo tokens: {e}")
        return None

    if not row or not row[0] or not row[1]:
        logger.error(f"[{seller_name}] No hay tokens completos en la tabla.")
        return None

    return {
        'access_token': str(row[0]),
        'refresh_token': str(row[1]),
        'access_token_expire_in': int(row[2] or 0),
        'refresh_token_expire_in': int(row[3] or 0),
    }


def refresh_shop_token(conn, shop: dict, refresh_token: str):
    """Renueva el access_token contra TikTok y lo persiste en la tabla."""
    seller_name = shop['seller_name']
    try:
        response = requests.get(
            f"{cfg.TIKTOK_AUTH_URL}/api/v2/token/refresh",
            params={
                'app_key': shop['app_key'],
                'app_secret': shop['app_secret'],
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            },
            timeout=cfg.TIKTOK_TIMEOUT,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"[{seller_name}] No se pudo refrescar el token: {e}")
        return None

    if response.status_code >= 400 or payload.get('code') != 0:
        logger.error(
            f"[{seller_name}] TikTok rechazó el refresh: HTTP={response.status_code} "
            f"code={payload.get('code')} message={payload.get('message')}"
        )
        return None

    data = payload.get('data') or {}
    if not all(k in data for k in
               ('access_token', 'refresh_token',
                'access_token_expire_in', 'refresh_token_expire_in')):
        logger.error(f"[{seller_name}] Respuesta de refresh incompleta.")
        return None

    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE {cfg.TIKTOK_TOKENS_TABLE}
            SET access_token = %s, access_token_expire_in = %s,
                refresh_token = %s, refresh_token_expire_in = %s
            WHERE seller_name = %s
            """,
            (
                data['access_token'], data['access_token_expire_in'],
                data['refresh_token'], data['refresh_token_expire_in'],
                seller_name,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            cursor.close()
            logger.error(f"[{seller_name}] El UPDATE de token no afectó exactamente 1 fila.")
            return None
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        logger.error(f"[{seller_name}] Error persistiendo el token refrescado: {e}")
        return None

    logger.info(f"[{seller_name}] Access token refrescado correctamente.")
    return str(data['access_token'])


def get_valid_access_token(conn, shop: dict):
    """Devuelve un access_token vigente, refrescándolo si está por vencer."""
    seller_name = shop['seller_name']
    token = load_shop_token(conn, seller_name)
    if not token:
        return None

    now = int(datetime.now(timezone.utc).timestamp())
    if token['refresh_token_expire_in'] <= now:
        logger.error(
            f"[{seller_name}] El refresh token está vencido. La tienda debe reautorizarse."
        )
        return None
    if token['access_token_expire_in'] > now + cfg.TOKEN_REFRESH_MARGIN_SECONDS:
        return token['access_token']
    return refresh_shop_token(conn, shop, token['refresh_token'])


# =============================================================================
# API DE TIKTOK SHOP
# =============================================================================
def sign_request(app_secret: str, url: str, params: dict, body) -> str:
    """Firma HMAC-SHA256 requerida por TikTok Shop (open-api v202309)."""
    filtered = {k: v for k, v in params.items() if k not in ('access_token', 'sign')}
    param_string = ''.join(f"{k}{filtered[k]}" for k in sorted(filtered))
    sign_string = f"{urlparse(url).path}{param_string}"
    if body is not None:
        sign_string += json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    wrapped = f"{app_secret}{sign_string}{app_secret}"
    return hmac.new(app_secret.encode(), wrapped.encode(), hashlib.sha256).hexdigest()


def tiktok_request(shop: dict, access_token: str, method: str, path: str,
                   body=None, query=None):
    """
    Llamada genérica a la API de TikTok. Devuelve el payload JSON o None.
    Nunca lanza excepción: cada caller decide qué hacer con el None.
    """
    url = f"{cfg.TIKTOK_BASE_URL}{path}"
    params = {
        'app_key': shop['app_key'],
        'shop_cipher': shop['shop_cipher'],
        'timestamp': str(int(time.time())),
        **(query or {}),
    }
    params['sign'] = sign_request(shop['app_secret'], url, params, body)

    try:
        response = requests.request(
            method,
            url,
            params=params,
            headers={
                'x-tts-access-token': access_token,
                'content-type': 'application/json',
            },
            data=(json.dumps(body, separators=(',', ':'), ensure_ascii=False)
                  if body is not None else None),
            timeout=cfg.TIKTOK_TIMEOUT,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.error(f"TikTok {method} {path}: error de conexión/parseo: {e}")
        return None

    if response.status_code >= 400 or payload.get('code') not in (None, 0):
        logger.error(
            f"TikTok {method} {path}: HTTP={response.status_code} "
            f"code={payload.get('code')} message={payload.get('message')}"
        )
        return None
    return payload


def search_awaiting_orders(shop: dict, access_token: str) -> list:
    """Descarga todas las órdenes AWAITING_SHIPMENT de la ventana configurada."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=cfg.LOOKBACK_HOURS)
    orders, page_token = [], None

    while True:
        query = {'page_size': str(cfg.PAGE_SIZE)}
        if page_token:
            query['page_token'] = page_token
        payload = tiktok_request(
            shop, access_token, 'POST', '/order/202309/orders/search',
            body={
                'shipping_type': 'SELLER',                # Envío a cargo del vendedor (modalidad Bulky)[cite: 2]
                'order_status': 'AWAITING_SHIPMENT',      # Órdenes pagadas y pendientes de surtir[cite: 2]
                'is_buyer_request_cancel': False,         # Excluir órdenes donde el cliente pidió cancelar
                'create_time_ge': int(start.timestamp()), # Ventana de tiempo: buscar desde esta fecha
                'create_time_lt': int(now.timestamp()), # Ventana de tiempo: buscar hasta esta fecha
            },
            query=query,
        )
        if payload is None:
            logger.error(f"[{shop['seller_name']}] Falló la búsqueda de órdenes.")
            return orders

        data = payload.get('data') or {}
        orders.extend(data.get('orders') or [])
        page_token = data.get('next_page_token')
        if not page_token:
            break

    logger.info(f"[{shop['seller_name']}] {len(orders)} órdenes AWAITING_SHIPMENT en la ventana.")
    return orders


def is_bulky_order(order: dict) -> bool:
    """
    Bulky = envío a cargo del vendedor. TikTok lo marca con
    shipping_type='SELLER' (vs 'TIKTOK', que trae guía de plataforma).
    """
    return str(order.get('shipping_type') or '').upper() in cfg.MERCHANT_SHIPPING_TYPES


def filter_bulky_orders(orders: list, seller_name: str) -> list:
    bulky = [o for o in orders if is_bulky_order(o)]
    logger.info(
        f"[{seller_name}] {len(bulky)} de {len(orders)} órdenes son Bulky "
        f"(shipping_type en {cfg.MERCHANT_SHIPPING_TYPES})."
    )
    return bulky


def get_package_id(shop: dict, access_token: str, order: dict):
    """
    Obtiene el package_id de la orden. Si TikTok aún no creó el paquete,
    lo crea con todos los line items pendientes.
    """
    packages = order.get('packages') or []
    if packages and packages[0].get('id'):
        return str(packages[0]['id'])

    line_item_ids = [
        str(li['id']) for li in (order.get('line_items') or []) if li.get('id')
    ]
    if not line_item_ids:
        logger.error(f"Orden {order.get('id')} sin line_items; no se puede crear paquete.")
        return None

    payload = tiktok_request(
        shop, access_token, 'POST', '/fulfillment/202309/packages',
        body={'orders': [{
            'id': str(order['id']),
            'order_line_item_ids': line_item_ids,
        }]},
    )
    if not payload:
        return None
    data = payload.get('data') or {}
    package_id = data.get('package_id') or (data.get('packages') or [{}])[0].get('id')
    if package_id:
        logger.info(f"Orden {order['id']}: paquete creado en TikTok ({package_id}).")
        return str(package_id)
    logger.error(f"Orden {order['id']}: TikTok no devolvió package_id al crear el paquete.")
    return None


def carrier_name_variants(provider_name: str) -> list:
    """Nombre normalizado del carrier + sus equivalencias conocidas."""
    normalized = normalize_carrier(provider_name)
    variants = [normalized] if normalized else []
    for alias in cfg.CARRIER_NAME_ALIASES.get(normalized, []):
        alias = normalize_carrier(alias)
        if alias and alias not in variants:
            variants.append(alias)
    return variants


def match_provider(catalog: dict, provider_name: str):
    """
    Busca el carrier en un catálogo `{nombre_normalizado: provider_id}`.

    Orden: exacto sobre el nombre y sus alias, y sólo después parcial. Así
    'fedex' no se lleva 'fedexground' por aparecer antes en la lista, y
    'PAQUETEEXPRESS' sí encuentra a 'Paquetexpress'.
    """
    variants = carrier_name_variants(provider_name)
    if not variants:
        return None

    for variant in variants:
        if variant in catalog:
            return catalog[variant]

    for variant in variants:
        for candidate, provider_id in catalog.items():
            if candidate in variant or variant in candidate:
                return provider_id
    return None


def resolve_shipping_provider_id(shop: dict, access_token: str, order: dict,
                                 provider_name: str, package_id=None):
    """
    Traduce el carrier de nuestra API interna al shipping_provider_id que
    exige TikTok al reportar la guía.

    1) Catálogo real de la tienda (Get Shipping Providers). Es la fuente
       autoritativa: TikTok entrega los IDs por delivery option, así que un ID
       de otra tienda podría estar mal.
    2) Mapa TIKTOK_SHIPPING_PROVIDER_IDS del config/.env, sólo como respaldo
       si el catálogo no está disponible.

    En ambos casos se prueba primero coincidencia EXACTA y sólo después
    parcial, para que 'FedEx' no le gane a 'FedEx Ground' por orden de lista.
    """
    providers = fetch_shipping_providers(shop, access_token, order, package_id)
    if providers:
        logger.info(
            f"TikTok: paqueterías disponibles: "
            f"{[(p.get('id'), p.get('name')) for p in providers]}"
        )
        catalog = {
            normalize_carrier(p.get('name')): str(p.get('id'))
            for p in providers if p.get('name') and p.get('id')
        }
        provider_id = match_provider(catalog, provider_name)
        if provider_id:
            logger.info(
                f"TikTok: '{provider_name}' resuelto desde el catálogo "
                f"({provider_id})."
            )
            return provider_id
    else:
        logger.warning(
            "TikTok: catálogo de paqueterías no disponible; se usa el mapa "
            "TIKTOK_SHIPPING_PROVIDER_IDS como respaldo."
        )
        fallback = {
            normalize_carrier(alias): str(pid)
            for alias, pid in cfg.TIKTOK_SHIPPING_PROVIDER_IDS.items()
        }
        provider_id = match_provider(fallback, provider_name)
        if provider_id:
            logger.info(
                f"TikTok: '{provider_name}' resuelto desde el mapa de respaldo "
                f"({provider_id})."
            )
            return provider_id

    logger.error(
        f"TikTok: no se encontró shipping_provider_id para '{provider_name}'. "
        f"Disponibles: {[p.get('name') for p in providers]}"
    )
    return None


def find_delivery_option_id(shop: dict, access_token: str, order: dict,
                            package_id=None):
    """
    Busca el `delivery_option_id`, que es obligatorio para consultar el
    catálogo de paqueterías (Get Shipping Providers 202309).

    TikTok no siempre lo expone en el mismo lugar, así que se busca en cascada:
      1. TIKTOK_DELIVERY_OPTION_ID del .env (si se fijó a mano).
      2. Campo suelto en la orden o en sus line_items.
      3. Campo en los paquetes de la orden.
      4. Detalle del paquete (GET /fulfillment/202309/packages/{id}).
      5. Almacenes -> delivery options de la tienda (se cachea por tienda).
    """
    if cfg.TIKTOK_DELIVERY_OPTION_ID:
        return cfg.TIKTOK_DELIVERY_OPTION_ID

    def first_id(*candidates):
        for value in candidates:
            if value:
                return str(value)
        return None

    found = first_id(
        order.get('delivery_option_id'),
        *[li.get('delivery_option_id') for li in (order.get('line_items') or [])],
        *[pkg.get('delivery_option_id') for pkg in (order.get('packages') or [])],
    )
    if found:
        logger.info(f"TikTok: delivery_option_id {found} tomado de la orden.")
        return found

    if package_id:
        payload = tiktok_request(
            shop, access_token, 'GET', f'/fulfillment/202309/packages/{package_id}'
        )
        data = (payload or {}).get('data') or {}
        found = first_id(data.get('delivery_option_id'))
        if found:
            logger.info(f"TikTok: delivery_option_id {found} tomado del paquete.")
            return found

    # Último recurso: el catálogo de la tienda. Se cachea porque no depende
    # de la orden y consultarlo por cada pedido sería desperdicio.
    if 'delivery_option_id' in _SHOP_CACHE.get(shop['seller_name'], {}):
        return _SHOP_CACHE[shop['seller_name']]['delivery_option_id']

    warehouses = ((tiktok_request(
        shop, access_token, 'GET', '/logistics/202309/warehouses'
    ) or {}).get('data') or {}).get('warehouses') or []

    for warehouse in warehouses:
        warehouse_id = warehouse.get('id')
        if not warehouse_id:
            continue
        options = ((tiktok_request(
            shop, access_token, 'GET',
            f'/logistics/202309/warehouses/{warehouse_id}/delivery_options'
        ) or {}).get('data') or {}).get('delivery_options') or []
        found = first_id(*[opt.get('id') for opt in options])
        if found:
            logger.info(
                f"TikTok: delivery_option_id {found} tomado del almacén {warehouse_id}."
            )
            _SHOP_CACHE.setdefault(shop['seller_name'], {})['delivery_option_id'] = found
            return found

    logger.error(
        "TikTok: no se pudo determinar el delivery_option_id. Fíjalo con "
        "TIKTOK_DELIVERY_OPTION_ID o el mapa TIKTOK_SHIPPING_PROVIDER_IDS en el .env."
    )
    _SHOP_CACHE.setdefault(shop['seller_name'], {})['delivery_option_id'] = None
    return None


def fetch_shipping_providers(shop: dict, access_token: str, order: dict,
                             package_id=None) -> list:
    """
    Catálogo de paqueterías válidas (Get Shipping Providers 202309).

        GET /logistics/202309/delivery_options/{id}/shipping_providers
            ?warehouse_region=MX&buyer_region=MX

    El resultado se cachea por (tienda, delivery_option, región del comprador):
    es el mismo catálogo para todas las órdenes del mismo destino.
    """
    delivery_option_id = find_delivery_option_id(shop, access_token, order, package_id)
    if not delivery_option_id:
        return []

    buyer_region = (order.get('recipient_address') or {}).get('region_code') or 'MX'
    cache_key = (shop['seller_name'], delivery_option_id, buyer_region)
    if cache_key in _PROVIDERS_CACHE:
        return _PROVIDERS_CACHE[cache_key]

    path = cfg.TIKTOK_SHIPPING_PROVIDERS_PATH.format(
        delivery_option_id=delivery_option_id
    )
    payload = tiktok_request(
        shop, access_token, 'GET', path,
        query={
            'warehouse_region': cfg.TIKTOK_WAREHOUSE_REGION,
            'buyer_region': buyer_region,
        },
    )

    providers = ((payload or {}).get('data') or {}).get('shipping_providers') or []
    if providers:
        logger.info(f"TikTok: catálogo de paqueterías obtenido de {path}.")
    else:
        logger.error(f"TikTok: {path} no devolvió paqueterías.")

    _PROVIDERS_CACHE[cache_key] = providers
    return providers


def ship_package_with_own_label(shop: dict, access_token: str, order: dict,
                                package_id: str, tracking_number: str,
                                provider_name: str, provider_id=None):
    """
    Reporta NUESTRA guía en TikTok para que la orden quede enviada.
    Devuelve (ok: bool, detalle: str).

    Son dos endpoints con propósitos DISTINTOS, no equivalentes:

      - Ship Package (despacha, camino normal):
            POST /fulfillment/202309/packages/{package_id}/ship
            body: {handover_method, self_shipment:{tracking_number,
                   shipping_provider_id}}

      - Update Shipping Info (CORRIGE una orden YA despachada; va sobre la
        ORDEN, no sobre el paquete, y requiere el scope seller.logistics):
            POST /fulfillment/202309/orders/{order_id}/shipping_info/update
            body: {tracking_number, shipping_provider_id}

    Con `TIKTOK_SHIP_STRATEGY=auto` se despacha con 'ship' y, si falla (p. ej.
    porque un intento anterior ya lo despachó), se corrige con 'shipping_info'.
    """
    provider_id = provider_id or resolve_shipping_provider_id(
        shop, access_token, order, provider_name, package_id
    )
    if not provider_id:
        return False, f"Sin shipping_provider_id para el carrier '{provider_name}'"

    order_id = str(order.get('id') or '')
    strategy = cfg.TIKTOK_SHIP_STRATEGY
    attempts = {
        'shipping_info': ['shipping_info'],
        'ship': ['ship'],
    }.get(strategy, ['ship', 'shipping_info'])

    errors = []
    for method in attempts:
        if method == 'ship':
            body = {
                'self_shipment': {
                    'tracking_number': tracking_number,
                    'shipping_provider_id': provider_id,
                },
            }
            if cfg.TIKTOK_HANDOVER_METHOD:
                body['handover_method'] = cfg.TIKTOK_HANDOVER_METHOD
            payload = tiktok_request(
                shop, access_token, 'POST',
                f'/fulfillment/202309/packages/{package_id}/ship',
                body=body,
            )
        else:
            if not order_id:
                logger.error("TikTok: sin order_id; no se puede corregir la guía.")
                errors.append(method)
                continue
            payload = tiktok_request(
                shop, access_token, 'POST',
                f'/fulfillment/202309/orders/{order_id}/shipping_info/update',
                body={
                    'tracking_number': tracking_number,
                    'shipping_provider_id': provider_id,
                },
            )

        if payload is not None:
            target = f"paquete {package_id}" if method == 'ship' else f"orden {order_id}"
            logger.info(
                f"TikTok: {target} reportado vía '{method}' con guía "
                f"{tracking_number} ({provider_name} / provider_id={provider_id})."
            )
            if strategy == 'auto' and method != attempts[0]:
                logger.warning(
                    f"TikTok: '{attempts[0]}' falló y se resolvió con '{method}'. "
                    f"Revisa si el paquete ya venía despachado."
                )
            return True, ''

        errors.append(method)
        logger.warning(f"TikTok: '{method}' falló para el paquete {package_id}.")

    return False, (
        f"TikTok rechazó el reporte de la guía por {' y '.join(errors)} "
        f"(ver log para el código de error)"
    )


# =============================================================================
# EXTRACCIÓN DE DATOS DE LA ORDEN
# =============================================================================
def strip_accents(value: str) -> str:
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', str(value or ''))
        if unicodedata.category(ch) != 'Mn'
    )


def resolve_state_code(state_name: str) -> str:
    """
    Traduce el nombre del estado a su código de 3 letras ('Ciudad de México'
    -> 'CMX'), que es lo que exigen las paqueteras.

    Si `MAP_STATE_TO_CODE` está apagado se devuelve el nombre tal cual, para
    cuando la API interna asuma este mapeo.
    """
    state_name = str(state_name or '').strip()
    if not state_name or not cfg.MAP_STATE_TO_CODE:
        return state_name

    key = ' '.join(strip_accents(state_name).lower().split())
    code = cfg.MX_STATE_CODES.get(key)
    if code:
        return code

    # Ya venía como código (p. ej. 'MEX', 'CMX').
    if len(state_name) <= 4 and state_name.isalpha():
        return state_name.upper()

    logger.warning(
        f"Estado '{state_name}' sin código en MX_STATE_CODES; se envía tal cual."
    )
    return state_name


def parse_district_info(address: dict) -> dict:
    """
    Descompone `district_info` de TikTok en estado / ciudad / colonia.

    TikTok entrega los niveles jerárquicos (L0 = país, L1 = estado, L2 =
    municipio/ciudad, L3 = colonia) y el nombre del nivel viene localizado, así
    que NO se puede buscar por 'state'/'city': se ordena por `address_level` y
    se asigna por posición, descartando el país.

    Ejemplo real:
        [México, Ciudad de México, Álvaro Obregón, La Angostura]
        -> state='Ciudad de México', city='Álvaro Obregón', colonia='La Angostura'
    """
    districts = address.get('district_info') or []

    def level_order(entry):
        level = str(entry.get('address_level') or '')
        digits = ''.join(ch for ch in level if ch.isdigit())
        return int(digits) if digits else 99

    if districts and not all(d.get('address_level') for d in districts):
        logger.warning(
            "TikTok: district_info sin 'address_level' en todos los niveles; "
            "el orden estado/ciudad/colonia puede no ser confiable."
        )

    ordered = sorted(districts, key=level_order)
    names = [
        str(d.get('address_name') or '').strip()
        for d in ordered if str(d.get('address_name') or '').strip()
    ]

    # Descartar SÓLO el primer nivel si es el país, para que el siguiente sea
    # el estado. Nunca filtrar por nombre en todas las posiciones: el Estado de
    # México se llama "México" y se perdería.
    country_aliases = {'mexico', 'mx'}
    if names and strip_accents(names[0]).lower() in country_aliases and len(names) > 1:
        names = names[1:]

    return {
        'state': names[0] if len(names) > 0 else '',
        'city': names[1] if len(names) > 1 else '',
        'colonia': names[-1] if len(names) > 2 else '',
    }


def build_recipient_data(order: dict) -> dict:
    """Arma el bloque `recipient` que espera la API interna de guías."""
    address = order.get('recipient_address') or {}
    parts = parse_district_info(address)

    # Calle y número. `address_line2` suele traer la referencia ("casa",
    # "depto 3"), así que se anexa a la calle salvo que repita la colonia.
    line1 = str(address.get('address_line1') or '').strip()
    line2 = str(address.get('address_line2') or '').strip()
    street1 = line1 or str(address.get('address_detail') or '').strip() or '.'
    if line2 and strip_accents(line2).lower() != strip_accents(parts['colonia']).lower():
        street1 = f"{street1} {line2}".strip()

    # La colonia es lo que la paquetería espera en street2. Si district_info no
    # la trajo, se busca en las líneas 3/4 de la dirección.
    colonia = parts['colonia'] or str(
        address.get('address_line3') or address.get('address_line4') or ''
    ).strip()

    city = parts['city'] or str(address.get('address_line4') or '').strip()

    complete_address = {
        "name": (address.get('name') or 'Cliente TikTok').strip(),
        "company": "",
        "email": order.get('buyer_email') or 'sin-correo@tiktok.com',
        "phone": clean_phone(address.get('phone_number')),
        "street1": street1,
        "street2": colonia,
        "city": city.replace(',', '').strip(),
        "state": resolve_state_code(parts['state']),
        "country": address.get('region_code') or 'MX',
        "zip": str(address.get('postal_code') or '').strip(),
    }
    logger.info(f"Destinatario armado: {complete_address}")
    return complete_address


def build_items_to_quote(order: dict):
    """
    Agrupa los line_items por SKU. En TikTok cada line_item equivale a UNA
    pieza, así que la cantidad es el conteo de líneas del mismo SKU.

    Devuelve (items_para_cotizar, valor_total_orden).
    """
    grouped = {}
    for line in order.get('line_items') or []:
        sku = (line.get('seller_sku') or line.get('sku_id') or '').strip()
        if not sku:
            continue
        try:
            price = float(line.get('sale_price') or line.get('original_price') or 0)
        except (TypeError, ValueError):
            price = 0.0
        if sku not in grouped:
            grouped[sku] = {
                "sku": sku,
                "quantity": 0,
                "price": price,
                "name": line.get('product_name') or f"Producto TikTok {sku}",
            }
        grouped[sku]["quantity"] += 1

    items = list(grouped.values())

    # Valor de la orden: lo pagado por el comprador (base del ratio del 21%).
    payment = order.get('payment') or {}
    try:
        total_order_value = float(payment.get('total_amount') or 0)
    except (TypeError, ValueError):
        total_order_value = 0.0
    if total_order_value <= 0:
        total_order_value = sum(i['price'] * i['quantity'] for i in items)

    return items, total_order_value


# =============================================================================
# API INTERNA: COTIZACIÓN Y GENERACIÓN DE GUÍAS
# =============================================================================
def get_best_rates_per_box(payload: dict):
    """
    Llama a /live-rates y elige el servicio más barato que pueda cubrir TODAS
    las cajas de la orden.

    Devuelve:
        dict            -> mapa package_id -> tarifa elegida
        None            -> sin tarifas / sin cobertura
        'CONNECTION-ERROR' -> falla de red (reintentable en la próxima corrida)
    """
    try:
        logger.info(f"Cotizando en /live-rates para {len(payload['items'])} item(s)...")
        response = requests.post(
            cfg.API_URL_LIVE_RATES, json=payload,
            auth=cfg.API_AUTH, timeout=cfg.API_TIMEOUT_RATES,
        )
        response.raise_for_status()
        all_rates = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error de conexión con /live-rates: {e}")
        return 'CONNECTION-ERROR'
    except ValueError as e:
        logger.error(f"/live-rates devolvió una respuesta no JSON: {e}")
        return None

    if not all_rates or not isinstance(all_rates, list):
        logger.warning("La API no devolvió tarifas (sin cobertura o lista vacía).")
        return None

    rates_by_box = {}
    for rate in all_rates:
        if not isinstance(rate, dict):
            continue
        box_id = rate.get('package_id')
        if not box_id:
            continue
        rates_by_box.setdefault(box_id, []).append(rate)

    if not rates_by_box:
        logger.warning("No se encontraron tarifas asociadas a un 'package_id'.")
        return None

    first_box_rates = rates_by_box[list(rates_by_box.keys())[0]]
    all_services = {r['service_code']: r['service_name'] for r in first_box_rates}
    if not all_services:
        logger.warning("La primera caja no tiene servicios disponibles.")
        return None

    best_per_service = {}
    for service_code, service_name in all_services.items():
        total_cost, rates_for_service, possible = 0, {}, True
        for box_id, rates in rates_by_box.items():
            rate_for_box = next((r for r in rates if r['service_code'] == service_code), None)
            if not rate_for_box:
                possible = False
                break
            total_cost += rate_for_box['total_price']
            rates_for_service[box_id] = rate_for_box
        if possible:
            best_per_service[service_code] = {
                'total_cost_cents': total_cost,
                'rates_map': rates_for_service,
                'service_name': service_name,
            }

    if not best_per_service:
        logger.error("Ningún carrier pudo cotizar TODAS las cajas de la orden.")
        return None

    best_code = min(best_per_service, key=lambda k: best_per_service[k]['total_cost_cents'])
    best = best_per_service[best_code]
    logger.info(
        f"Mejor tarifa: {best['service_name']} - "
        f"${best['total_cost_cents'] / 100.0:.2f} para {len(rates_by_box)} caja(s)."
    )
    return best['rates_map']


def generate_labels(best_rates_map: dict, recipient_data: dict,
                    total_order_value: float) -> list:
    """Genera una guía por cada caja cotizada. Los ZPL se convierten a PDF."""
    generated_labels = []
    num_boxes = len(best_rates_map) or 1
    value_per_box = total_order_value / num_boxes

    for box_id, rate in best_rates_map.items():
        logger.info(f"Generando guía para el paquete {box_id} ({rate.get('service_name')})...")
        payload = {
            "service_code": rate['service_code'],
            "rate_id": rate['rate_id'],
            "shipper": cfg.SHIPPER_DATA,
            "recipient": recipient_data,
            "sku": rate.get('sku_child'),
            "data_sat": {
                "bienesTransp": cfg.SAT_BIENES_TRANSP,
                "valorMercancia": value_per_box,
            },
        }
        try:
            response = requests.post(
                cfg.API_URL_GENERATE_LABEL, json=payload,
                auth=cfg.API_AUTH, timeout=cfg.API_TIMEOUT_LABEL,
            )
            if response.status_code != 200:
                logger.error(
                    f"/generate-label respondió {response.status_code} para "
                    f"{box_id}: {response.text}"
                )
                continue

            label_data = response.json()
            if not label_data.get('tracking_number'):
                logger.error(f"Respuesta 200 sin tracking_number para {box_id}: {label_data}")
                continue

            provider_name = str(rate.get('service_name', 'Desconocido')).split(' - ')[0]
            pdf_url = label_data.get('pdf_url')
            zpl_data = label_data.get('zpl')
            pdf_bytes = None
            if zpl_data and not pdf_url:
                logger.info(f"Guía de {box_id} es ZPL. Convirtiendo a PDF...")
                pdf_bytes = convert_zpl_to_pdf_bytes(zpl_data)

            generated_labels.append({
                'box_id': box_id,
                'sku_parent': rate.get('sku_parent'),
                'sku_child': rate.get('sku_child'),
                'tracking_number': str(label_data['tracking_number']),
                'provider': provider_name,
                'service_level': rate.get('service_name', 'Estándar'),
                'pdf_url': pdf_url,
                'zpl': zpl_data,
                'pdf_bytes': pdf_bytes,
                'shipping_label_cost': rate.get('total_price', 0) / 100.0,
                'carrier_odoo_id': get_carrier_odoo_id(provider_name),
            })
            logger.info(f"  -> [ÉXITO] Guía {label_data['tracking_number']} para {box_id}.")
        except Exception as e:
            logger.error(f"Excepción al generar guía para {box_id}: {e}")

    return generated_labels


# =============================================================================
# ODOO
# =============================================================================
def connect_to_odoo():
    try:
        common = xmlrpc.client.ServerProxy(f'{cfg.ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(cfg.ODOO_DB, cfg.ODOO_USER, cfg.ODOO_PASSWORD, {})
        if not uid:
            logger.critical("Odoo rechazó la autenticación (uid vacío).")
            return None, None
        models = xmlrpc.client.ServerProxy(f'{cfg.ODOO_URL}/xmlrpc/2/object', allow_none=True)
        logger.info(f"Conexión a Odoo exitosa (uid={uid}).")
        return models, uid
    except Exception as e:
        logger.critical(f"Error fatal al conectar con Odoo: {e}")
        return None, None


def odoo_execute(models, uid, model: str, method: str, args: list, kwargs=None):
    return models.execute_kw(
        cfg.ODOO_DB, uid, cfg.ODOO_PASSWORD, model, method, args, kwargs or {}
    )


def search_sale_order(models, uid, tiktok_order_id: str):
    """Busca la SO por los campos de referencia de canal. Devuelve (id, name)."""
    for field in cfg.ODOO_ORDER_REFERENCE_FIELDS:
        try:
            records = odoo_execute(
                models, uid, 'sale.order', 'search_read',
                [[(field, '=', tiktok_order_id)]],
                {'fields': ['id', 'name', 'state'], 'limit': 2},
            )
        except Exception as e:
            logger.error(f"Odoo: error buscando SO por {field}={tiktok_order_id}: {e}")
            continue
        if len(records) > 1:
            logger.error(f"Odoo: varias SO para {field}={tiktok_order_id}. Se requiere revisión.")
            return None, None
        if records:
            logger.info(
                f"Odoo: SO {records[0]['name']} (id={records[0]['id']}) "
                f"encontrada por {field}."
            )
            return int(records[0]['id']), str(records[0]['name'])
    logger.warning(f"Odoo: no se encontró SO para la orden TikTok {tiktok_order_id}.")
    return None, None


def update_sale_order_tracking(models, uid, so_id: int, labels: list) -> bool:
    """Escribe tracking y carrier en la orden de venta."""
    tracking_str = ",".join(l['tracking_number'] for l in labels)
    values = {cfg.ODOO_TRACKING_FIELD: tracking_str}
    carrier_id = labels[0].get('carrier_odoo_id')
    if carrier_id:
        values[cfg.ODOO_CARRIER_FIELD] = carrier_id
    try:
        odoo_execute(models, uid, 'sale.order', 'write', [[so_id], values])
        logger.info(f"Odoo: SO {so_id} actualizada con guía(s) {tracking_str}.")
        return True
    except Exception as e:
        logger.error(f"Odoo: error escribiendo tracking en la SO {so_id}: {e}")
        return False


def get_label_file_bytes(label: dict):
    """
    Devuelve (bytes, extension) del archivo de UNA guía.
    Prioridad: PDF convertido de ZPL -> PDF de URL -> ZPL como .txt.
    """
    if label.get('pdf_bytes'):
        return label['pdf_bytes'], 'pdf'
    if label.get('pdf_url'):
        try:
            response = requests.get(label['pdf_url'], timeout=20)
            response.raise_for_status()
            return response.content, 'pdf'
        except Exception as e:
            logger.error(f"Error descargando el PDF de {label['tracking_number']}: {e}")
    if label.get('zpl'):
        return label['zpl'].encode('utf-8'), 'zpl.txt'
    return None, None


def build_consolidated_label_file(labels: list, so_name: str):
    """
    Une todas las guías en un solo archivo. Prioriza PDF; si ninguna guía es
    PDF, cae a un .zpl.txt concatenado. Devuelve (file_name, base64) o (None, None).
    Requiere PyPDF2 cuando hay más de un PDF.
    """
    files = [(label, *get_label_file_bytes(label)) for label in labels]
    pdfs = [content for _, content, ext in files if content and ext == 'pdf']
    zpls = [content for _, content, ext in files if content and ext == 'zpl.txt']

    if pdfs:
        if len(pdfs) == 1:
            return f"{so_name}.pdf", base64.b64encode(pdfs[0]).decode('utf-8')
        if PyPDF2 is None:
            return None, None  # el caller adjunta cada guía por separado
        try:
            merger = PyPDF2.PdfMerger()
            for content in pdfs:
                merger.append(io.BytesIO(content))
            buffer = io.BytesIO()
            merger.write(buffer)
            merger.close()
            return f"{so_name}.pdf", base64.b64encode(buffer.getvalue()).decode('utf-8')
        except Exception as e:
            logger.error(f"Error uniendo los PDFs de las guías: {e}")
            return None, None

    if zpls:
        consolidated = b"\n\n".join(zpls)
        return f"{so_name}.zpl.txt", base64.b64encode(consolidated).decode('utf-8')

    logger.warning("No hubo archivos válidos (PDF/ZPL) para adjuntar.")
    return None, None


def create_odoo_attachment(models, uid, so_id: int, so_name: str,
                           file_name: str, file_b64: str):
    try:
        odoo_execute(models, uid, 'sale.order.attachment', 'create', [{
            'file_name': file_name,
            'attachment': file_b64,
            'so_id': so_id,
        }])
        logger.info(f"Odoo: archivo '{file_name}' adjuntado a la SO {so_name}.")
        return True
    except Exception as e:
        logger.error(f"Odoo: error adjuntando '{file_name}' a la SO {so_name}: {e}")
        return False


def attach_labels_to_odoo(models, uid, so_id: int, so_name: str, labels: list):
    """
    Adjunta las guías al modelo custom sale.order.attachment.
    Intenta un único archivo consolidado; si no es posible (sin PyPDF2 o error
    al unir), adjunta una guía por archivo para no perder ninguna.
    """
    file_name, file_b64 = build_consolidated_label_file(labels, so_name)
    if file_name:
        create_odoo_attachment(models, uid, so_id, so_name, file_name, file_b64)
        return

    logger.warning(f"Odoo: adjuntando las {len(labels)} guías por separado en {so_name}.")
    for label in labels:
        content, extension = get_label_file_bytes(label)
        if not content:
            logger.warning(f"Guía {label['tracking_number']} sin archivo. Omitida.")
            continue
        create_odoo_attachment(
            models, uid, so_id, so_name,
            f"{so_name}_{label['tracking_number']}.{extension}",
            base64.b64encode(content).decode('utf-8'),
        )


def post_odoo_chatter(models, uid, so_id: int, so_name: str, order_id: str,
                      labels: list, shipping_cost: float):
    """Deja constancia en el chatter de la SO."""
    trackings = ", ".join(l['tracking_number'] for l in labels)
    body = (
        f"{now_cdmx_str()}. Se generó automáticamente la(s) guía(s) Bulky de TikTok "
        f"para la orden {so_name} (TikTok: {order_id}). "
        f"Guía(s): {trackings} | Carrier: {labels[0]['provider']} | "
        f"Costo de envío: ${shipping_cost:.2f}."
    )
    try:
        odoo_execute(models, uid, 'sale.order', 'message_post', [[so_id]], {'body': body})
        logger.info(f"Odoo: mensaje publicado en el chatter de {so_name}.")
    except Exception as e:
        logger.error(f"Odoo: error publicando en el chatter de {so_name}: {e}")


# =============================================================================
# GOOGLE SHEETS
# =============================================================================
def authenticate_google_sheets():
    """Acepta el JSON de la cuenta de servicio inline (Kestra) o una ruta local."""
    try:
        raw = cfg.GOOGLE_CREDS_JSON
        if not raw:
            logger.error("GOOGLE_CREDS_JSON no está definida.")
            return None
        raw = raw.strip()
        if raw.startswith('{'):
            creds = Credentials.from_service_account_info(
                json.loads(raw), scopes=cfg.SCOPES
            )
        else:
            creds = Credentials.from_service_account_file(raw, scopes=cfg.SCOPES)
        client = gspread.authorize(creds)
        logger.info("Autenticación con Google Sheets exitosa.")
        return client
    except Exception as e:
        logger.error(f"Error de autenticación en Google Sheets: {e}")
        return None


def get_or_create_worksheet(spreadsheet, title: str, headers: list):
    """Devuelve la pestaña; si no existe la crea con su fila de encabezados."""
    try:
        worksheet = spreadsheet.worksheet(title)
        if not worksheet.acell('A1').value:
            worksheet.update([headers], 'A1', value_input_option='USER_ENTERED')
        return worksheet
    except gspread.exceptions.WorksheetNotFound:
        logger.info(f"Sheets: creando la pestaña '{title}'...")
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers) + 5)
        worksheet.update([headers], 'A1', value_input_option='USER_ENTERED')
        return worksheet
    except Exception as e:
        logger.error(f"Sheets: no se pudo preparar la pestaña '{title}': {e}")
        return None


def build_sheet_report(worksheet, headers: list, attempts_header=None) -> dict:
    """
    Empaqueta la pestaña con un índice `ID TikTok -> nº de fila` construido de
    una sola lectura. Ese índice es lo que permite ACTUALIZAR la fila existente
    en vez de agregar una nueva en cada corrida.
    """
    report = {
        'worksheet': worksheet,
        'headers': headers,
        'index': {},
        'attempts': {},
        'attempts_col': (headers.index(attempts_header) if attempts_header in headers
                         else None),
    }
    if worksheet is None:
        return report

    try:
        all_values = worksheet.get_all_values()
    except Exception as e:
        logger.error(f"Sheets: no se pudo leer '{worksheet.title}' para indexar: {e}")
        return report

    key_idx = cfg.SHEET_KEY_COLUMN - 1
    for row_number, row in enumerate(all_values[1:], start=2):
        if len(row) <= key_idx:
            continue
        key = row[key_idx].strip().lstrip("'")
        if not key:
            continue
        report['index'][key] = row_number
        if report['attempts_col'] is not None and len(row) > report['attempts_col']:
            try:
                report['attempts'][key] = int(row[report['attempts_col']])
            except (TypeError, ValueError):
                report['attempts'][key] = 0

    logger.info(
        f"Sheets: '{worksheet.title}' indexada con {len(report['index'])} orden(es) previas."
    )
    return report


def setup_report_sheets(gc):
    """Abre el documento de TikTok y prepara las dos pestañas de reportería."""
    if not gc:
        return build_sheet_report(None, cfg.SHEET_SUCCESS_HEADERS), \
               build_sheet_report(None, cfg.SHEET_MANUAL_HEADERS, 'Attemps')
    try:
        spreadsheet = gc.open_by_key(cfg.SPREADSHEET_TIKTOK_ID)
    except Exception as e:
        logger.error(f"Sheets: no se pudo abrir el documento {cfg.SPREADSHEET_TIKTOK_ID}: {e}")
        spreadsheet = None

    success_ws = manual_ws = None
    if spreadsheet is not None:
        success_ws = get_or_create_worksheet(
            spreadsheet, cfg.SHEET_SUCCESS, cfg.SHEET_SUCCESS_HEADERS
        )
        manual_ws = get_or_create_worksheet(
            spreadsheet, cfg.SHEET_MANUAL, cfg.SHEET_MANUAL_HEADERS
        )

    return (
        build_sheet_report(success_ws, cfg.SHEET_SUCCESS_HEADERS),
        build_sheet_report(manual_ws, cfg.SHEET_MANUAL_HEADERS, 'Attemps'),
    )


def get_attempt_number(report: dict, key: str) -> int:
    """Nº de intento que corresponde a esta corrida para la orden dada."""
    return report['attempts'].get(key, 0) + 1


def _write_sheet_row(report: dict, key: str, row_data: list):
    """Actualiza la fila de la orden si ya existe; si no, la agrega."""
    worksheet = report['worksheet']
    row_number = report['index'].get(key)

    if row_number:
        last_col = gspread.utils.rowcol_to_a1(1, len(report['headers']))[:-1]
        worksheet.update(
            [row_data], f"A{row_number}:{last_col}{row_number}",
            value_input_option='USER_ENTERED',
        )
        logger.info(
            f"Sheets: fila {row_number} de '{worksheet.title}' actualizada "
            f"(orden {key})."
        )
        return

    response = worksheet.append_row(row_data, value_input_option='USER_ENTERED')
    updated_range = (response.get('updates') or {}).get('updatedRange', '')
    try:
        # 'Guias_automaticas_manuales'!A15:L15  ->  15
        report['index'][key] = int(''.join(
            ch for ch in updated_range.split('!')[-1].split(':')[0] if ch.isdigit()
        ))
    except (ValueError, IndexError):
        pass
    logger.info(f"Sheets: orden {key} agregada a '{worksheet.title}'.")


def upsert_sheet_row(report: dict, key: str, row_data: list):
    """
    Escribe la fila de la orden en su pestaña (una fila por orden).
    Reintenta una vez ante errores de cuota (429) o 500 de la API.
    """
    if report.get('worksheet') is None:
        logger.error("Sheets: pestaña no disponible; no se registró la fila.")
        return

    if report['attempts_col'] is not None:
        try:
            report['attempts'][key] = int(row_data[report['attempts_col']])
        except (TypeError, ValueError, IndexError):
            pass

    try:
        _write_sheet_row(report, key, row_data)
    except gspread.exceptions.APIError as api_err:
        logger.warning(f"Sheets: error de API ({api_err}). Reintentando en 2s...")
        time.sleep(2)
        try:
            _write_sheet_row(report, key, row_data)
        except Exception as e:
            logger.error(
                f"Sheets: fallo definitivo al escribir la orden {key} en "
                f"'{report['worksheet'].title}': {e}"
            )
    except Exception as e:
        logger.error(
            f"Sheets: error inesperado al escribir la orden {key} en "
            f"'{report['worksheet'].title}': {e}"
        )


# =============================================================================
# REGISTRO DE RESULTADOS (Sheets + tools.shipping_labels)
# =============================================================================
def normalize_provider(provider_name) -> str:
    """Homologa el nombre del carrier tal como se guarda en BD y en el sheet."""
    provider = str(provider_name or '').strip()
    return 'PAQUETEXPRESS' if provider == 'PAQUETEEXPRESS' else provider


def register_manual(conn, sheet_manual, ctx: dict, motivo: str, detalle: str,
                    shipping_cost=None, cost_ratio=None, carrier=None,
                    service_level=None, rates_map=None, write_db: bool = True):
    """
    Marca una orden como NO automatizable: una fila en
    `Guias_automaticas_manuales` (se actualiza en cada reintento, incluyendo el
    contador de intentos) y un registro por SKU en tools.shipping_labels.

    `carrier` / `service_level` se registran aunque no haya guía: cuando la
    cotización sí devolvió tarifa (p. ej. costo > 21 %), saber qué paquetería
    la ofrecía es justo lo que necesita CS para decidir a mano.

    `write_db=False` se usa cuando la orden YA tiene un registro exitoso en BD
    y no debe degradarse (fallo posterior a la generación de la guía).

    `shipping_cost` es el costo TOTAL de la orden y es lo que se escribe en el
    sheet. En BD, en cambio, cada renglón es de UN SKU, así que con `rates_map`
    (la cotización por caja) se reparte el costo por SKU; sin `rates_map` no hay
    forma de repartirlo y se registra el total.
    """
    order_id = ctx['order_id']
    logger.warning(f"MANUAL | Orden {order_id} | {motivo}: {detalle}")
    resumen.warning(
        f"MANUAL    | {ctx.get('seller_name', '')} | TikTok {order_id} | "
        f"{ctx.get('so_name') or 'SIN SO'} | {motivo} | {detalle}"
    )

    na = cfg.NOT_APPLICABLE
    attempt = get_attempt_number(sheet_manual, order_id)

    upsert_sheet_row(sheet_manual, order_id, [
        now_cdmx_str(),                                              # Time-stamp
        ctx.get('seller_name') or na,                                # Seller Name
        ctx.get('order_date') or na,                                 # Order date
        f"'{order_id}",                                              # ID TikTok
        ctx.get('so_name') or na,                                    # ID Odoo
        motivo,                                                      # Status
        detalle,                                                     # Reason
        attempt,                                                     # Attemps
        ", ".join(i['sku'] for i in ctx.get('items', [])) or na,     # SKU(s)
        normalize_provider(carrier) or na,                           # Carrier
        f"${shipping_cost:.2f}" if shipping_cost is not None else na,   # Total cost shipping
        f"${ctx['total_order_value']:.2f}" if ctx.get('total_order_value') else na,
        f"{cost_ratio:.1%}" if cost_ratio is not None else na,       # Ratio
    ])

    if not write_db:
        return

    items = ctx.get('items') or [{'sku': 'N/A', 'quantity': 0}]
    for item in items:
        # El renglón es de UN SKU: se guarda el costo cotizado de SUS cajas,
        # no el total de la orden (que inflaría el costo por nº de SKUs).
        sku_shipping_cost = sku_shipping_cost_from_rates(
            rates_map, item['sku'], total_fallback=shipping_cost
        ) if rates_map else shipping_cost

        upsert_shipping_label(
            conn,
            marketplace_id=order_id,
            sku=item['sku'],
            qty_ordered=item['quantity'],
            status=motivo,
            label_generated=False,
            label_origin='SRS_GENERATED',
            tracking_number=None,
            shipping_cost=sku_shipping_cost,
            carrier=normalize_provider(carrier) or None,
            carrier_service_level=service_level,
            error_log=detalle,
        )


def close_manual_row(sheet_manual: dict, ctx: dict, labels: list,
                     shipping_cost: float, cost_ratio: float):
    """
    Si la orden había caído antes en la pestaña de manuales (un DRY_RUN, una
    falla de red, un reintento), se marca como resuelta en vez de dejar la fila
    contradiciendo a la pestaña de generadas.
    """
    order_id = ctx['order_id']
    if order_id not in sheet_manual.get('index', {}):
        return

    upsert_sheet_row(sheet_manual, order_id, [
        now_cdmx_str(),
        ctx.get('seller_name') or cfg.NOT_APPLICABLE,
        ctx.get('order_date') or cfg.NOT_APPLICABLE,
        f"'{order_id}",
        ctx.get('so_name') or cfg.NOT_APPLICABLE,
        'RESUELTA_AUTOMATICAMENTE',
        f"Guía generada en una corrida posterior: "
        f"{', '.join(l['tracking_number'] for l in labels)}. Ya no requiere revisión.",
        get_attempt_number(sheet_manual, order_id),
        ", ".join(i['sku'] for i in ctx.get('items', [])) or cfg.NOT_APPLICABLE,
        normalize_provider(labels[0]['provider']),
        f"${shipping_cost:.2f}",
        f"${ctx.get('total_order_value', 0):.2f}",
        f"{cost_ratio:.1%}",
    ])


def register_success(conn, sheet_success, sheet_manual, ctx: dict, labels: list,
                     shipping_cost: float, cost_ratio: float,
                     tiktok_ok: bool, odoo_ok: bool):
    """Registra la orden automatizada en Sheets y en tools.shipping_labels."""
    order_id = ctx['order_id']
    trackings = [l['tracking_number'] for l in labels]
    main_carrier = normalize_provider(labels[0]['provider'])
    main_service = labels[0]['service_level']
    status = 'LABELS_GENERATED' if (tiktok_ok and odoo_ok) else 'LABELS_GENERATED_PARTIAL_UPDATE'

    upsert_sheet_row(sheet_success, order_id, [
        now_cdmx_str(),                                              # Time-stamp
        ctx.get('seller_name') or cfg.NOT_APPLICABLE,                # Seller Name
        ctx.get('order_date', ''),                                   # Order Date
        f"'{order_id}",                                              # ID TikTok
        ctx.get('so_name') or cfg.NOT_APPLICABLE,                    # ID Odoo
        status,                                                      # Status
        ", ".join(i['sku'] for i in ctx.get('items', [])),           # SKU(s)
        "'" + ", ".join(trackings),                                  # Guías (tracking)
        main_carrier,                                                # Carrier
        f"${shipping_cost:.2f}",                                     # Costo total guia(s)
        f"${ctx.get('total_order_value', 0):.2f}",                   # Total orden
        f"{cost_ratio:.1%}",                                         # Ratio
    ])

    if tiktok_ok and odoo_ok:
        close_manual_row(sheet_manual, ctx, labels, shipping_cost, cost_ratio)

    # Un registro por SKU, con las guías que le corresponden.
    for item in ctx.get('items', []):
        labels_for_sku = [l for l in labels if l.get('sku_parent') == item['sku']] or labels
        # Costo de envío de ESTE SKU: suma de TODAS sus cajas (mismo valor que
        # la suma de `shipping_label_cost` del JSON de tracking_number).
        sku_shipping_cost = sku_shipping_cost_from_labels(labels_for_sku)
        tracking_json = [{
            "tracking_number": l['tracking_number'],
            "carrier": normalize_provider(l['provider']),
            "package_id": l['box_id'],
            "sku_child": l['sku_child'],
            "shipping_label_cost": l['shipping_label_cost'],
        } for l in labels_for_sku]

        upsert_shipping_label(
            conn,
            marketplace_id=order_id,
            sku=item['sku'],
            qty_ordered=item['quantity'],
            status=status,
            label_generated=True,
            label_origin='SRS_GENERATED',
            tracking_number=tracking_json,
            shipping_cost=sku_shipping_cost,
            carrier=main_carrier,
            carrier_service_level=main_service,
            error_log=None if (tiktok_ok and odoo_ok) else (
                f"Guía generada. TikTok actualizado={tiktok_ok}, Odoo actualizado={odoo_ok}."
            ),
        )


# =============================================================================
# PROCESAMIENTO DE UNA ORDEN
# =============================================================================
def process_order(order, shop, access_token, conn, models, uid,
                  sheet_success, sheet_manual):
    """
    Corre el flujo completo para una orden Bulky.
    Devuelve 'processed', 'manual' o 'skipped'.
    """
    order_id = str(order['id'])
    logger.info(f"\n>>> [{shop['seller_name']}] Procesando orden {order_id} <<<")

    items, total_order_value = build_items_to_quote(order)
    ctx = {
        'seller_name': shop['seller_name'],
        'order_id': order_id,
        'order_date': epoch_to_cdmx_str(order.get('create_time')),
        'items': items,
        'total_order_value': total_order_value,
        'so_name': None,
    }

    # --- Validaciones previas -------------------------------------------
    if not items:
        register_manual(conn, sheet_manual, ctx, 'ORDER_WITHOUT_SKUS',
                        "La orden no trae line_items con SKU de vendedor.")
        return 'manual'

    if total_order_value <= 0:
        register_manual(conn, sheet_manual, ctx, 'ORDER_VALUE_ZERO',
                        "El valor de la orden es 0; no se puede validar el ratio de costo.")
        return 'manual'

    recipient_data = build_recipient_data(order)
    if not recipient_data['zip']:
        register_manual(conn, sheet_manual, ctx, 'ADDRESS_INCOMPLETE',
                        "La dirección del comprador no trae código postal.")
        return 'manual'

    # --- Orden de venta en Odoo -----------------------------------------
    so_id, so_name = search_sale_order(models, uid, order_id)
    ctx['so_name'] = so_name
    if not so_id:
        register_manual(conn, sheet_manual, ctx, 'ODOO_ORDER_MISSING',
                        f"No existe una sale.order asociada a la orden TikTok {order_id}.")
        return 'manual'

    # --- Paquete en TikTok ----------------------------------------------
    package_id = get_package_id(shop, access_token, order)
    if not package_id:
        register_manual(conn, sheet_manual, ctx, 'TIKTOK_PACKAGE_MISSING',
                        "TikTok no tiene paquete para la orden y no se pudo crear.")
        return 'manual'

    # --- Cotización ------------------------------------------------------
    best_rates_map = get_best_rates_per_box({
        "origin": {"zip": cfg.ORIGIN_ZIP, "country": "MX"},
        "destination": recipient_data,
        "items": items,
    })

    if best_rates_map == 'CONNECTION-ERROR':
        register_manual(conn, sheet_manual, ctx, 'RATES_CONNECTION_ERROR',
                        "Error de conexión con la API interna de cotización. "
                        "La orden se reintenta en la siguiente corrida.")
        return 'manual'
    if not isinstance(best_rates_map, dict) or not best_rates_map:
        register_manual(conn, sheet_manual, ctx, 'NO_COVERAGE',
                        "La API no devolvió cotizaciones válidas para el destino "
                        "(sin cobertura o SKU sin medidas).")
        return 'manual'

    # --- Regla del 21% ---------------------------------------------------
    shipping_cost = sum(r['total_price'] for r in best_rates_map.values()) / 100.0
    cost_ratio = shipping_cost / total_order_value

    # Carrier cotizado: se registra aunque la guía no llegue a generarse.
    quoted_service = str(list(best_rates_map.values())[0].get('service_name') or '')
    quoted_carrier = quoted_service.split(' - ')[0]
    quote_info = {
        'shipping_cost': shipping_cost,
        'cost_ratio': cost_ratio,
        'carrier': quoted_carrier,
        'service_level': quoted_service,
        # Permite que el registro en BD reparta el costo por SKU.
        'rates_map': best_rates_map,
    }

    if cost_ratio > cfg.LIMIT_RATIO_PERCENTAGE:
        register_manual(
            conn, sheet_manual, ctx, 'LIMIT_RATIO_OVERCOME',
            f"El costo de envío (${shipping_cost:.2f}) representa el {cost_ratio:.1%} "
            f"del valor de la orden (${total_order_value:.2f}), por encima del "
            f"límite de {cfg.LIMIT_RATIO_PERCENTAGE:.0%}.",
            **quote_info,
        )
        return 'manual'

    # --- Paqueteria válida en TikTok (ANTES de gastar la guía) ------------
    # Si TikTok no reconoce el carrier, la orden no se puede cerrar en el
    # canal: se corta aquí para no emitir una guía que quedaría sin reportar.
    provider_id = resolve_shipping_provider_id(
        shop, access_token, order, quoted_carrier, package_id
    )
    if not provider_id:
        register_manual(
            conn, sheet_manual, ctx, 'TIKTOK_PROVIDER_UNRESOLVED',
            f"TikTok no reconoce la paquetería cotizada '{quoted_carrier}'; no se "
            f"generó guía para no emitirla sin poder reportarla. Fija el mapa "
            f"TIKTOK_SHIPPING_PROVIDER_IDS en el .env.",
            **quote_info,
        )
        return 'manual'

    logger.info(
        f"Orden {order_id} APROBADA: envío ${shipping_cost:.2f} = {cost_ratio:.1%} "
        f"del valor. Generando {len(best_rates_map)} guía(s)..."
    )

    if cfg.DRY_RUN:
        logger.warning(f"DRY_RUN activo: no se genera guía para la orden {order_id}.")
        register_manual(
            conn, sheet_manual, ctx, 'DRY_RUN',
            f"Simulación: la orden pasó todas las validaciones "
            f"({len(best_rates_map)} caja(s), ${shipping_cost:.2f}).",
            **quote_info,
        )
        return 'skipped'

    # --- Generación de guías ---------------------------------------------
    labels = generate_labels(best_rates_map, recipient_data, total_order_value)
    if not labels:
        register_manual(conn, sheet_manual, ctx, 'LABEL_GENERATION_FAILED',
                        "La API interna no generó ninguna guía para la orden.",
                        **quote_info)
        return 'manual'
    if len(labels) != len(best_rates_map):
        register_manual(
            conn, sheet_manual, ctx, 'PARTIAL_LABELS',
            f"Sólo se generaron {len(labels)} de {len(best_rates_map)} guías "
            f"({', '.join(l['tracking_number'] for l in labels)}). "
            f"Requiere revisión: hay guías emitidas sin reportar.",
            **quote_info,
        )
        return 'manual'

    # --- Odoo -------------------------------------------------------------
    odoo_ok = update_sale_order_tracking(models, uid, so_id, labels)
    attach_labels_to_odoo(models, uid, so_id, so_name, labels)
    post_odoo_chatter(models, uid, so_id, so_name, order_id, labels, shipping_cost)

    # --- TikTok ------------------------------------------------------------
    # Si la guía terminó siendo de otro carrier que el cotizado, se vuelve a
    # resolver el ID en vez de reportar uno equivocado.
    generated_carrier = labels[0]['provider']
    if normalize_carrier(generated_carrier) != normalize_carrier(quoted_carrier):
        logger.warning(
            f"El carrier de la guía ({generated_carrier}) difiere del cotizado "
            f"({quoted_carrier}); se resuelve de nuevo el shipping_provider_id."
        )
        provider_id = None

    tiktok_ok, tiktok_detail = ship_package_with_own_label(
        shop, access_token, order, package_id,
        labels[0]['tracking_number'], generated_carrier, provider_id,
    )

    # --- Registro ----------------------------------------------------------
    register_success(conn, sheet_success, sheet_manual, ctx, labels,
                     shipping_cost, cost_ratio, tiktok_ok, odoo_ok)

    if not tiktok_ok or not odoo_ok:
        # write_db=False: el registro de BD ya quedó como guía generada y no
        # debe degradarse; el detalle del fallo vive en la pestaña de manuales.
        register_manual(
            conn, sheet_manual, ctx,
            'POST_LABEL_UPDATE_FAILED',
            f"Guía(s) {', '.join(l['tracking_number'] for l in labels)} generada(s), "
            f"pero Odoo actualizado={odoo_ok} y TikTok actualizado={tiktok_ok}. "
            f"{tiktok_detail}".strip(),
            carrier=labels[0]['provider'], service_level=labels[0]['service_level'],
            shipping_cost=shipping_cost, cost_ratio=cost_ratio,
            rates_map=best_rates_map, write_db=False,
        )
        return 'manual'

    resumen.info(
        f"GUIA OK   | {ctx['seller_name']} | TikTok {order_id} | {so_name} | "
        f"{', '.join(l['tracking_number'] for l in labels)} | "
        f"{normalize_provider(generated_carrier)} | ${shipping_cost:.2f} ({cost_ratio:.1%})"
    )
    return 'processed'


# =============================================================================
# PROCESAMIENTO POR TIENDA
# =============================================================================
def process_shop(shop, conn, models, uid, sheet_success, sheet_manual, counters):
    seller_name = shop['seller_name']
    logger.info(f"\n===== TIENDA: {seller_name} =====")

    access_token = get_valid_access_token(conn, shop)
    if not access_token:
        logger.error(f"[{seller_name}] Sin access token válido. Tienda omitida.")
        counters['shops_failed'] += 1
        return

    orders = filter_bulky_orders(search_awaiting_orders(shop, access_token), seller_name)
    if not orders:
        logger.info(f"[{seller_name}] No hay órdenes Bulky pendientes.")
        return

    already_processed = fetch_processed_order_ids(conn, seller_name)
    pending = [o for o in orders if str(o['id']) not in already_processed]
    counters['skipped'] += len(orders) - len(pending)

    if cfg.MAX_ORDERS > 0:
        pending = pending[:cfg.MAX_ORDERS]

    logger.info(f"[{seller_name}] {len(pending)} orden(es) a procesar en esta corrida.")
    resumen.info(
        f"TIENDA    | {seller_name} | {len(orders)} bulky pendientes | "
        f"{len(pending)} a procesar en esta corrida"
    )

    for order in pending:
        counters['found'] += 1
        try:
            result = process_order(order, shop, access_token, conn, models, uid,
                                   sheet_success, sheet_manual)
            counters[result] += 1
        except Exception as e:
            logger.exception(f"[{seller_name}] Error inesperado en la orden {order.get('id')}: {e}")
            counters['manual'] += 1
            items, total = build_items_to_quote(order)
            register_manual(
                conn, sheet_manual,
                {
                    'seller_name': seller_name,
                    'order_id': str(order.get('id')),
                    'order_date': epoch_to_cdmx_str(order.get('create_time')),
                    'items': items,
                    'total_order_value': total,
                    'so_name': None,
                },
                'UNEXPECTED_ERROR', f"{type(e).__name__}: {e}",
            )
        time.sleep(cfg.SLEEP_BETWEEN_ORDERS)


# =============================================================================
# MAIN
# =============================================================================
def emit_run_summary(counters: dict):
    """
    Cierra la corrida con el resumen operativo y publica los contadores como
    outputs de Kestra, para poder usarlos en alertas o tareas posteriores
    (p. ej. avisar a CS sólo si hay órdenes manuales).
    """
    resumen.info(
        "RESUMEN   | "
        f"guías generadas: {counters['processed']} | "
        f"manuales: {counters['manual']} | "
        f"omitidas: {counters['skipped']} | "
        f"órdenes vistas: {counters['found']} | "
        f"tiendas con error: {counters['shops_failed']}"
    )
    # Protocolo de outputs de Kestra: la línea se parsea y queda disponible
    # como {{ outputs.<task>.vars.<clave> }}.
    print("::" + json.dumps({"outputs": counters}) + "::", flush=True)


def process_tiktok_bulky_labels():
    logger.info("=== Iniciando: Guías automáticas TikTok Bulky ===")
    if cfg.DRY_RUN:
        logger.warning("MODO DRY_RUN: no se generarán guías ni se escribirá en TikTok/Odoo/BD.")

    counters = {
        'found': 0, 'processed': 0, 'manual': 0,
        'skipped': 0, 'shops_failed': 0,
    }

    shops = cfg.get_active_shops()
    if not shops:
        logger.error("No hay tiendas Bulky activas con credenciales. Fin del script.")
        return counters

    conn = get_db_connection()
    if not conn:
        logger.critical("Sin conexión a MySQL no se pueden leer los tokens. Fin del script.")
        return counters

    models, uid = connect_to_odoo()
    if not models:
        logger.critical("Sin conexión a Odoo. Fin del script.")
        conn.close()
        return counters

    sheet_success, sheet_manual = setup_report_sheets(authenticate_google_sheets())
    if sheet_success['worksheet'] is None or sheet_manual['worksheet'] is None:
        logger.warning(
            "Reportería de Google Sheets no disponible. El proceso continúa, "
            "pero los registros sólo quedarán en tools.shipping_labels."
        )

    try:
        for shop in shops:
            try:
                process_shop(shop, conn, models, uid, sheet_success, sheet_manual, counters)
            except Exception as e:
                logger.exception(f"Error fatal en la tienda {shop['seller_name']}: {e}")
                counters['shops_failed'] += 1
    finally:
        conn.close()

    logger.info(f"=== Fin del script === Resumen: {json.dumps(counters, sort_keys=True)}")
    emit_run_summary(counters)
    return counters


if __name__ == '__main__':
    process_tiktok_bulky_labels()
