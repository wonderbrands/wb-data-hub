"""
19_alerta_sin_so.py -- Amazon: ordenes cobradas que siguen sin SO en Odoo.

01 siembra estas ordenes como NO_SO_IN_ODOO y avisa por Slack, pero Slack solo
lo ve Sergio. La SO la baja Yuju (el flujo nunca la crea), y quien le da
seguimiento con Yuju es Will: este script arma la lista para un correo diario
(flujo amz_alerta_sin_so_flow).

Que manda: las ordenes en NO_SO_IN_ODOO cobradas hace AMZ_SIN_SO_DIAS dias o
mas (2 por defecto: el cobro llega ~6 dias despues de la compra, asi que una
orden de 2 dias sin SO ya no es un atraso normal de Yuju). Cruza contra la
replica odoo_new_sale_order_live: si la SO ya existe ahi, lo que falta es el
puente odoo_master_txns_c, no Yuju.

Solo lee MySQL. Deja en la carpeta actual:
  ordenes_sin_so.csv   la lista (adjunto del correo)
  email_body.html      el cuerpo del correo
Si no hay ordenes no escribe nada y el correo no sale.

  $env:AMZ_ENTORNO='prod'; python 19_alerta_sin_so.py
"""
import os
import csv
import json
import html
import logging
from datetime import datetime, timedelta, timezone

from _amz_common import ENTORNO, TBL_BILLING, MARKETPLACE, UTC_local, get_db_connection, a_float

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('19_alerta_sin_so')

DIAS = int(os.getenv('AMZ_SIN_SO_DIAS', '2'))
SALIDA = os.getenv('AMZ_SIN_SO_SALIDA', '.')


def main():
    hoy = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_local)
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT mkp_order_id, total_amount, raw_json FROM {TBL_BILLING}
                    WHERE marketplace = %s AND status = 'NO_SO_IN_ODOO'""", (MARKETPLACE,))
    filas = []
    for r in cur.fetchall():
        try:
            amz = (json.loads(r['raw_json'] or '{}') or {}).get('amazon') or {}
        except (ValueError, TypeError):
            amz = {}
        cobro = str(amz.get('posted_date') or '')[:19]
        try:
            fecha = datetime.strptime(cobro, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            fecha = None
        dias = (hoy - fecha).days if fecha else None
        if dias is not None and dias < DIAS:
            continue
        filas.append({'orden_amazon': r['mkp_order_id'],
                      'fecha_cobro': fecha.strftime('%Y-%m-%d') if fecha else '',
                      'dias_sin_so': dias if dias is not None else '',
                      'importe_cliente': round(a_float(r['total_amount']), 2),
                      'so_en_odoo': ''})
    if filas:
        ids = [f['orden_amazon'] for f in filas]
        cur.execute(f"""SELECT channel_order_reference o, MAX(name) so
                        FROM somos_reyes.odoo_new_sale_order_live
                        WHERE channel_order_reference IN ({', '.join(['%s'] * len(ids))})
                        GROUP BY channel_order_reference""", tuple(ids))
        en_odoo = {r['o']: r['so'] for r in cur.fetchall()}
        for f in filas:
            f['so_en_odoo'] = en_odoo.get(f['orden_amazon'], '')
    cur.close()
    db.close()

    filas.sort(key=lambda f: (f['fecha_cobro'], f['orden_amazon']))
    faltan = [f for f in filas if not f['so_en_odoo']]
    log.info(f"{ENTORNO}: {len(filas)} orden(es) sin SO con {DIAS}+ dias desde el cobro; "
             f"{len(faltan)} no existen en Odoo (Yuju).")
    if not filas:
        return 0

    with open(os.path.join(SALIDA, 'ordenes_sin_so.csv'), 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)

    total = sum(f['importe_cliente'] for f in faltan)
    renglones = ''.join(
        f"<tr><td>{html.escape(f['orden_amazon'])}</td><td>{f['fecha_cobro']}</td>"
        f"<td align='right'>{f['dias_sin_so']}</td><td align='right'>${f['importe_cliente']:,.2f}</td></tr>"
        for f in faltan[:60])
    puente = [f for f in filas if f['so_en_odoo']]
    nota_puente = (f"<p>{len(puente)} orden(es) ya tienen SO en Odoo pero el flujo todavia no la ve "
                   f"(lo revisa Data): {', '.join(html.escape(f['orden_amazon'] + ' ' + f['so_en_odoo']) for f in puente[:20])}.</p>"
                   if puente else '')
    cuerpo = f"""
<p>Ordenes de Amazon que Amazon ya cobro y que <b>no existen en Odoo</b> (sin SO) desde hace {DIAS} dias o mas.
Sin SO no se pueden facturar. Hay que pedirle a Yuju que las baje o relance a Odoo; en cuanto exista la SO,
el flujo las factura solo.</p>
<p><b>{len(faltan)} orden(es) por ${total:,.2f}</b> (lista completa en el adjunto).</p>
<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-family:Arial;font-size:12px">
<tr style="background:#eee"><th>Orden Amazon</th><th>Cobro</th><th>Dias</th><th>Importe</th></tr>
{renglones}
</table>
{'<p>(Se muestran 60; el resto esta en el adjunto.)</p>' if len(faltan) > 60 else ''}
{nota_puente}
<p>Saludos,<br><b>Data team</b></p>
"""
    with open(os.path.join(SALIDA, 'email_body.html'), 'w', encoding='utf-8') as fh:
        fh.write(cuerpo)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
