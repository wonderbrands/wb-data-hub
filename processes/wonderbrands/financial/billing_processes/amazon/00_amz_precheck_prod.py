"""
00_amz_precheck_prod.py
-----------------------
VERIFICACION PREVIA (SOLO LECTURA) antes de encender Amazon en produccion.

Revisa en Odoo y MySQL lo que 01/02/03/11 dan por hecho. No escribe nada.
Sale con codigo 1 si falta algo, para que Kestra marque la tarea en rojo.

    AMZ_ENTORNO=prod python 00_amz_precheck_prod.py
"""

import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, TBL_BILLING, TBL_PAYMENTS, TAX_ID_MARKETPLACES, PARTNER_ID_PUBLICO_GENERAL,
    ACCOUNT_AMAZON_CXC, ACCOUNT_LIBERACIONES, ACCOUNT_MERC_NO_ENVIADA,
    ACCOUNT_CUPONES_AMAZON, ACCOUNT_IVA_TRASLADADO, ACCOUNT_REDONDEO,
    validar_entorno, get_db_connection, OdooModelProxy, get_amazon_team_ids,
    find_journal_by_account,
)

CUENTAS = [ACCOUNT_AMAZON_CXC, ACCOUNT_LIBERACIONES, ACCOUNT_MERC_NO_ENVIADA,
           ACCOUNT_CUPONES_AMAZON, ACCOUNT_IVA_TRASLADADO, ACCOUNT_REDONDEO,
           '501.01.001', '401.01.002']

fallas = []


def revisar(ok, que, detalle=''):
    log.info(f"[{'OK ' if ok else 'FALTA'}] {que} {detalle}")
    if not ok:
        fallas.append(que)


def run():
    validar_entorno()
    odoo = OdooModelProxy()

    for code in CUENTAS:
        a = odoo.execute_kw('account.account', 'search_read', [[('code', '=', code)]],
                            {'fields': ['name', 'reconcile'], 'context': {'lang': 'es_MX'}})
        revisar(bool(a), f"cuenta {code}", a[0]['name'] if a else '')
    a = odoo.execute_kw('account.account', 'search_read', [[('code', '=', ACCOUNT_MERC_NO_ENVIADA)]],
                        {'fields': ['reconcile']})
    revisar(bool(a) and a[0]['reconcile'], f"{ACCOUNT_MERC_NO_ENVIADA} permite conciliar")

    try:
        j = find_journal_by_account(odoo, ACCOUNT_LIBERACIONES)
        revisar(True, f"diario de cobros con cuenta {ACCOUNT_LIBERACIONES}", str(j))
    except Exception as e:
        revisar(False, f"diario de cobros con cuenta {ACCOUNT_LIBERACIONES}", str(e))
    revisar(bool(odoo.execute_kw('account.journal', 'search', [[('type', '=', 'general')]],
                                 {'limit': 1})), "diario general (cierre mensual)")

    t = odoo.execute_kw('account.tax', 'search_read', [[('id', '=', TAX_ID_MARKETPLACES)]],
                        {'fields': ['name', 'amount']})
    revisar(bool(t) and t[0]['amount'] == 16, f"impuesto {TAX_ID_MARKETPLACES} (IVA 16%)",
            t[0]['name'] if t else '')
    p = odoo.execute_kw('res.partner', 'search_read', [[('id', '=', PARTNER_ID_PUBLICO_GENERAL)]],
                        {'fields': ['vat']})
    revisar(bool(p) and p[0]['vat'] == 'XAXX010101000', f"publico en general {PARTNER_ID_PUBLICO_GENERAL}")
    e = odoo.execute_kw('product.product', 'search_read', [[('default_code', '=', 'C-ENVIO')]],
                        {'fields': ['property_account_income_id']})
    revisar(bool(e), "producto C-ENVIO",
            str(e[0]['property_account_income_id']) if e else '')
    revisar(bool(get_amazon_team_ids(odoo)), "equipo de ventas de Amazon")

    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    for tabla in (TBL_BILLING, TBL_PAYMENTS):
        try:
            cur.execute(f"SHOW COLUMNS FROM {tabla} LIKE 'status'")
            col = cur.fetchone()
            revisar(col is not None and 'varchar(40)' in col['Type'], f"tabla {tabla} (status varchar(40))",
                    col['Type'] if col else '')
        except Exception as ex:
            revisar(False, f"tabla {tabla}", str(ex))
    cur.execute("SELECT access_token FROM somos_reyes.amz_tokens LIMIT 1")
    revisar(bool(cur.fetchone()), "token de SP-API en somos_reyes.amz_tokens")
    db.close()

    if ENTORNO != 'prod':
        log.warning(f"Corrio con AMZ_ENTORNO='{ENTORNO}'. Para encender, correrlo con 'prod'.")
    if fallas:
        log.error(f"FALTAN {len(fallas)}: {', '.join(fallas)}")
        sys.exit(1)
    log.info("Todo listo.")


if __name__ == '__main__':
    run()
