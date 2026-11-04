"""
10_conciliar_205.py
-------------------
Rehace la conciliacion de la 205.06.003 (Mercancias facturadas - no enviadas)
de las facturas que ya creo 01_amz_billing.py.

Por que existe: correo de Jero del 28-oct-2026 sobre el lote 6. Al refacturar,
la salida de almacen seguia conciliada con la factura cancelada y la factura
nueva quedaba abierta (44 de 48). Desde el 28-oct 01_amz_billing.py rehace la
conciliacion al crear cada factura (conciliar_salida); este script aplica lo
mismo a las facturas creadas antes de ese cambio.

Usa EXACTAMENTE la funcion de 01 (se carga el modulo), para que no haya dos
versiones de la regla. Solo toca conciliaciones de la 205.06.003; nunca rompe
una conciliacion con una factura viva (ver conciliar_salida en 01).

Uso:
    python 10_conciliar_205.py            # facturas de la tabla de control
    python 10_conciliar_205.py --seco     # solo lista el estado, no concilia
"""

import os
import sys
import logging
import importlib.util
from collections import Counter

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    TBL_BILLING, MARKETPLACE, IDEMPOTENCY_PREFIX, ACCOUNT_MERC_NO_ENVIADA,
    banner, get_db_connection, validar_entorno, OdooModelProxy, get_account_id,
)

SECO = '--seco' in sys.argv


def cargar_01():
    spec = importlib.util.spec_from_file_location(
        'amz_billing_mod', os.path.join(_dir, '01_amz_billing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    m = cargar_01()
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT DISTINCT odoo_so_name FROM {TBL_BILLING}
                    WHERE marketplace = %s AND odoo_so_name IS NOT NULL
                      AND status IN ('ODOO_INVOICED', 'ODOO_B2B_LINKED')""",
                (MARKETPLACE,))
    nombres = [r['odoo_so_name'] for r in cur.fetchall()]
    db.close()
    log.info(f"{len(nombres)} SO facturadas por 01 en la tabla de control.")

    odoo = OdooModelProxy(timeout=300)
    cuenta = get_account_id(odoo, ACCOUNT_MERC_NO_ENVIADA)
    estados, problemas = Counter(), []
    for i in range(0, len(nombres), 200):
        sos = odoo.execute_kw('sale.order', 'search_read',
                              [[('name', 'in', nombres[i:i + 200])]],
                              {'fields': ['id', 'name']})
        for so in sos:
            facturas = odoo.execute_kw(
                'account.move', 'search_read',
                [[('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                  ('ref', 'like', f"{IDEMPOTENCY_PREFIX}{so['name']}%")]],
                {'fields': ['id', 'name']})
            for f in facturas:
                if SECO:
                    lineas = odoo.execute_kw(
                        'account.move.line', 'search_read',
                        [[('move_id', '=', f['id']), ('account_id', '=', cuenta)]],
                        {'fields': ['reconciled']})
                    estado = ('SIN_LINEAS' if not lineas else
                              'CONCILIADA' if all(l['reconciled'] for l in lineas)
                              else 'ABIERTA')
                    estados[estado] += 1
                    continue
                r = m.conciliar_salida(odoo, so, f['id'], cuenta)
                estados[r['estado']] += 1
                if r['estado'] in ('PARCIAL', 'ERROR') or r['detalle'].startswith('rota'):
                    problemas.append(f"{so['name']} {f['name']}: {r['estado']} {r['detalle']}")
        log.info(f"  ...{min(i + 200, len(nombres))}/{len(nombres)} SO")

    log.info('')
    log.info('=' * 78)
    log.info(f"205.06.003 {'(EN SECO)' if SECO else ''}: "
             f"{', '.join(f'{k} {v}' for k, v in sorted(estados.items()))}")
    log.info('=' * 78)
    for p in problemas:
        log.info(f"  {p}")


if __name__ == '__main__':
    banner('CONCILIACION 205.06.003 -- FACTURAS AMAZON')
    validar_entorno()
    run()
