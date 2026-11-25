"""
18_reabrir_total_diff.py -- USO UNICO, al pasar a produccion el ajuste del 24-nov.

TOTAL_DIFF es un estado final: 01 no lo vuelve a ver. Al 25-nov habia 102
ordenes ahi, casi todas por liberacion parcial (la SO coincide con lo que pago
el cliente y Amazon solo libero parte), mas SO10347964 / SO10340253 (sin
OrderTotal en amazon_orders) y SO10393600 (Business con envio). El codigo nuevo
de 01 resuelve los tres casos, pero solo para ordenes pendientes.

Este script regresa TODAS las TOTAL_DIFF a pendiente (B2B -> PENDING_B2B_LINK,
lo demas -> PENDING_WB_INVOICE) para que la siguiente corrida de 01 las
reevalue. Las que sigan sin cuadrar regresan solas a TOTAL_DIFF.

Simulacion por defecto; escribe solo con --aplicar. Correr DESPUES de que el
01 nuevo este en main (Kestra lo toma en <= 15 min) y ANTES de una corrida.

  $env:AMZ_ENTORNO='prod'; python 18_reabrir_total_diff.py            # lista
  $env:AMZ_ENTORNO='prod'; python 18_reabrir_total_diff.py --aplicar
"""
import sys
import json
import argparse
import logging

from _amz_common import ENTORNO, TBL_BILLING, MARKETPLACE, get_db_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('18_reabrir_total_diff')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--aplicar', action='store_true')
    args = ap.parse_args()

    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT id, mkp_order_id, odoo_so_name, total_amount, error_log, raw_json
                    FROM {TBL_BILLING} WHERE marketplace = %s AND status = 'TOTAL_DIFF'
                    ORDER BY id""", (MARKETPLACE,))
    filas = cur.fetchall()
    log.info(f"{ENTORNO}: {len(filas)} orden(es) en TOTAL_DIFF | "
             f"{'APLICAR' if args.aplicar else 'SIMULACION'}")
    cambios = {'PENDING_B2B_LINK': 0, 'PENDING_WB_INVOICE': 0}
    for f in filas:
        try:
            tipo = ((json.loads(f['raw_json'] or '{}') or {}).get('log_conciliacion') or {}).get('tipo')
        except (ValueError, TypeError):
            tipo = None
        nuevo = 'PENDING_B2B_LINK' if tipo == 'B2B' else 'PENDING_WB_INVOICE'
        cambios[nuevo] += 1
        log.info(f"  {f['odoo_so_name']} {f['mkp_order_id']} -> {nuevo} | {(f['error_log'] or '')[:90]}")
        if args.aplicar:
            cur.execute(f"""UPDATE {TBL_BILLING}
                            SET status = %s, retry_count_loader = 0,
                                error_log = CONCAT('REABIERTA 24-nov (era TOTAL_DIFF): ', IFNULL(error_log, ''))
                            WHERE id = %s AND status = 'TOTAL_DIFF'""", (nuevo, f['id']))
    if args.aplicar:
        db.commit()
    cur.close()
    db.close()
    log.info(f"{'Reabiertas' if args.aplicar else 'Se reabririan'}: {cambios}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
