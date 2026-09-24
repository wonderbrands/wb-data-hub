import os

# Registro de tiendas de Mercado Libre habilitadas para el proceso de guías.
# Kestra solo inyecta ML_SELLER_ID; el resto de parámetros se derivan de aquí
# para evitar combinaciones inconsistentes (ej. seller de Oficiales con control id 1).
SELLERS = {
    '25523702': {'seller_name': 'SOMOS-REYES', 'print_control_id': 1},
    '160190870': {'seller_name': 'SOMOS-REYES OFICIALES', 'print_control_id': 2},
}

# Default = tienda original, para que el flow productivo actual siga funcionando sin cambios.
DEFAULT_SELLER_ID = '25523702'


def get_seller_config():
    seller_id = os.getenv('ML_SELLER_ID', DEFAULT_SELLER_ID).strip()
    if seller_id not in SELLERS:
        raise ValueError(f"ML_SELLER_ID '{seller_id}' no está registrado en ml_sellers.SELLERS")
    return {'seller_id': seller_id, **SELLERS[seller_id]}
