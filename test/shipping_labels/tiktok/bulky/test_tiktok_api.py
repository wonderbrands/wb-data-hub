import requests, time, hmac, hashlib, json
from urllib.parse import urlparse

BASE_URL = 'https://open-api.tiktokglobalshop.com' #[cite: 1]

def sign_request(app_secret: str, url: str, params: dict, body: dict = None) -> str:
    """Genera la firma HMAC-SHA256. Si hay body, lo incluye en la firma[cite: 2]."""
    filtered = {k: v for k, v in params.items() if k not in ('access_token', 'sign')}
    param_string = ''.join(f"{k}{filtered[k]}" for k in sorted(filtered))
    sign_string = f"{urlparse(url).path}{param_string}"
    
    # Si es POST y tiene body, se serializa sin espacios[cite: 2]
    if body is not None:
        sign_string += json.dumps(body, separators=(',', ':'), ensure_ascii=False)
        
    wrapped = f"{app_secret}{sign_string}{app_secret}"
    return hmac.new(app_secret.encode(), wrapped.encode(), hashlib.sha256).hexdigest()

def tiktok_post(shop: dict, access_token: str, path: str, body: dict, query: dict = None) -> dict:
    """Ejecuta una petición POST genérica[cite: 2]."""
    url = f"{BASE_URL}{path}"
    params = {
        'app_key': shop['app_key'],
        'shop_cipher': shop['shop_cipher'],
        'timestamp': str(int(time.time())),
        **(query or {}),
    }
    params['sign'] = sign_request(shop['app_secret'], url, params, body) #[cite: 2]

    response = requests.post(
        url,
        params=params,
        headers={
            'x-tts-access-token': access_token,
            'content-type': 'application/json',
        },
        data=json.dumps(body, separators=(',', ':'), ensure_ascii=False) #[cite: 2]
    )
    return response.json()

# =========================================================
# TUS ENDPOINTS
# =========================================================

def search_orders(shop: dict, access_token: str, start_timestamp: int, end_timestamp: int):
    """Busca órdenes usando un POST con los timestamps en formato epoch[cite: 2]."""
    payload = tiktok_post(
        shop, 
        access_token, 
        '/order/202309/orders/search',
        body={
            # --- Filtros Base Originales ---
            'shipping_type': 'SELLER',                # Envío a cargo del vendedor (modalidad Bulky)[cite: 2]
            'order_status': 'AWAITING_SHIPMENT',      # Órdenes pagadas y pendientes de surtir[cite: 2]
            'is_buyer_request_cancel': True,         # Excluir órdenes donde el cliente pidió cancelar
            'create_time_ge': start_timestamp,        # Ventana de tiempo: buscar desde esta fecha[cite: 2]
            'create_time_lt': end_timestamp,          # Ventana de tiempo: buscar hasta esta fecha[cite: 2]
            
            # # --- Filtros de Seguridad Experimentales ---
            # #'is_on_hold_order': False,                # Evitar órdenes retenidas (ventana de 1h del cliente)
            # 'is_cod': False,                          # Evitar envíos con pago contra entrega
            # 'require_postal_code': False,              # Asegurar que traiga código postal para cotizar
            # 'min_total_amount': 0.01                  # Evitar órdenes que valen cero pesos
        },
        query={'page_size': '50'} #[cite: 2]
    )
    
    # Extraemos específicamente la lista de órdenes para que len() funcione bien
    # Si la API rechaza la petición, 'orders' quedará vacío para no romper el script
    orders = payload.get('data', {}).get('orders', []) if isinstance(payload.get('data'), dict) else []
    
    return orders

# =========================================================
# USO
# =========================================================
if __name__ == '__main__':
    my_shop = {
        'app_key': '6l1v9d0bsp80b',
        'app_secret': '7cd97261b1241507442a11b0261836e27a1c40d1',
        'shop_cipher': 'ROW_C5OroAAAAAARkYhVoWBOBl5GYP0ibhqs'
    }
    my_token = 'ROW_Pxmz4QAAAABC0ALo2hEss-2l92vjrtwOUsD0iEc8Gntjw1t3q8MgZfDShEuE5naPaI8DmncOf8czfpU1IdWttsFIs7j4jHbu6xlwRF-BE8Be1EV1bWW8yFo2-4w1twtOCzPpLM8YCctbIgY0IvHQtjicPy0EtPS51ykmY4CPkhY_rcLPyf8IUayQbuxqxcK6t2taC0BS19zdouuzzNoiXvf4nO6r6Amj'
    
    end_time = int(time.time())
    start_time = end_time - (72 * 3600)
    
    # Ejecuta la búsqueda
    orders = search_orders(my_shop, my_token, start_time, end_time)
    
    # Imprime la respuesta ordenada con indent=4
    print(json.dumps(orders, indent=4, ensure_ascii=False))
    
    # Imprime el conteo real
    print(f'\n\nCANTIDAD: {len(orders)}\n\n')