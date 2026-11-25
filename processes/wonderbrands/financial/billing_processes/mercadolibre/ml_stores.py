import os

# Registro de tiendas de Mercado Libre habilitadas para facturación y cobros (01-04).
# Kestra solo inyecta ML_SELLER_ID; el resto de parámetros se derivan de aquí
# para evitar combinaciones inconsistentes (ej. seller de Oficiales con la CxC de
# la tienda original, o cobros de Oficiales aplicados en el diario 'MP').
#
#   marketplace          -> valor de la columna 'marketplace' en finance.mkp_billing_prod
#                           y finance.mkp_payments_prod
#   account_code_cxc     -> CxC a la que el 02 redirige la línea por cobrar de la factura
#   mp_journal_code      -> código corto del diario de cobros en Odoo (04)
#   mp_account_code      -> cuenta por defecto esperada en ese diario; si no es None,
#                           el 04 aborta cuando el diario no la tiene (candado)
#   billing_start        -> corte sobre somos_reyes.ml_order_update.date_created,
#                           que se guarda en UTC-4 (hora de los servidores de ML)
#   invoice_journal_code -> diario de terceros de las facturas de MeLi (02); el nombre es la serie-folio del CFDI
#   nc_journal_code      -> diario de terceros de las NC de MeLi (06)
#   nc_cobro_account     -> transitoria del reverso del cobro de una NC (06)
#   rem_journal_code     -> diario de NC de reembolsos fuera de MeLi (06: doble egreso y retornos)
#   team_name            -> equipo de ventas de la tienda en Odoo (06: retornos)
STORES = {
    '25523702': {
        'seller_name': 'SOMOS-REYES',
        'marketplace': 'MERCADO_LIBRE',
        'account_code_cxc': '105.01.004',
        'mp_journal_code': 'MP',
        'mp_account_code': None,
        # Paciente Cero: 2026-06-01 19:50:58 UTC
        'billing_start': '2026-06-01 15:50:55',
        'invoice_journal_code': 'MELI',
        'nc_journal_code': 'MLNC',
        'nc_cobro_account': '102.03.001',
        'rem_journal_code': 'MLREM',
        'team_name': 'Team_MercadoLibre',
    },
    '160190870': {
        'seller_name': 'SOMOS-REYES OFICIALES',
        'marketplace': 'MERCADO_LIBRE_OFICIALES',
        'account_code_cxc': '105.01.005',     # MERCADO LIBRE (OFICIALES)
        'mp_journal_code': 'MPO',
        'mp_account_code': '102.02.009',      # MERCADO PAGO OFICIALES
        # Arranque del autofacturador de Oficiales:
        # 2026-09-17 15:56:00 CDMX (UTC-6) = 2026-09-17 17:56:00 UTC-4
        'billing_start': '2026-09-17 17:56:00',
        # Diarios y cuenta creados por Finanzas (24-nov): código de 5 caracteres; el nombre sale del CFDI
        'invoice_journal_code': 'MLOF',
        'nc_journal_code': 'MLONC',
        'nc_cobro_account': '102.03.007',     # Mercado Pago Oficiales (cuenta transitoria)
        'rem_journal_code': 'MLORM',          # reembolsos fuera de MeLi de Oficiales
        'team_name': 'Team_MercadoLibreOficiales',
    },
}

# Default = tienda original, para que el flow productivo actual siga funcionando sin cambios.
DEFAULT_SELLER_ID = '25523702'


def get_store_config():
    seller_id = os.getenv('ML_SELLER_ID', DEFAULT_SELLER_ID).strip()
    if seller_id not in STORES:
        raise ValueError(f"ML_SELLER_ID '{seller_id}' no está registrado en ml_stores.STORES")
    return {'seller_id': seller_id, **STORES[seller_id]}
