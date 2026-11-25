"""
17_prueba_hoja_ruta_24nov.py -- prueba en STAGING de los cambios de 01 por la
hoja de ruta de Finanzas del 24-nov. Solo corre con AMZ_ENTORNO='staging'.

Casos (ordenes reales; la copia de Odoo tiene datos al 13-nov):
  P1  Liberacion parcial fuera del cierre -> LIBERACION_PARCIAL, sin factura.
      Si Amazon ya libero todo, se factura normal (punto 8: SO10389274/SO10399284).
  P2  La misma orden en el cierre (01 corre como si fuera 1-dic 08:30) ->
      factura con fecha 30-nov por lo que pago el cliente.
  P3  Sin OrderTotal en amazon_orders (SO10347964, SO10340253) -> la API da el
      total y se factura por la SO (promocion de Amazon).
  P4  Business con envio (SO10393600) -> factura AB-1245 en AMZB con C-ENVIO,
      igual al CFDI de Amazon (793.00).
  P5  Business con CFDI de septiembre (periodo cerrado en la copia) y sin
      factura -> 01 vincula el CFDI en AMZB con fecha de factura de septiembre
      y fecha contable en el primer dia abierto (antes Odoo lo rechazaba).
  P6  is_amazon_business: SO10393600 queda marcada.

Uso (desde esta carpeta):
  $env:AMZ_ENTORNO='staging'; ..\\..\\..\\..\\.venv\\Scripts\\python.exe 17_prueba_hoja_ruta_24nov.py
Deja lote_auditoria_nov/prueba_hoja_ruta_<fecha>.csv
"""
import os
import sys
import csv
import json
import logging
import subprocess
import importlib.util
import xmlrpc.client
from datetime import datetime

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (ENTORNO, ODOO_URL, TBL_BILLING, MARKETPLACE, validar_entorno,  # noqa: E402
                         get_db_connection, OdooModelProxy, ejecutar_sin_retorno, a_float)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger('prueba_24nov')

PARCIALES = {'701-4216452-5504242': 'SO10405491', '702-7311091-9554657': 'SO10393143',
             '702-7256502-4007439': 'SO10389274', '701-3090063-1573804': 'SO10399284'}
SIN_TOTAL = {'702-2607037-2401020': 'SO10347964', '701-7403597-5274661': 'SO10340253'}
ENVIO_B2B = {'701-6801641-7987408': 'SO10393600'}
TS = datetime.now().strftime('%Y%m%d_%H%M')
SALIDA = os.path.join(_dir, 'lote_auditoria_nov')


def q(db, sql, params=()):
    cur = db.cursor(dictionary=True)
    cur.execute(sql, params)
    filas = cur.fetchall()
    cur.close()
    db.commit()
    return filas


def fila(db, order_id):
    r = q(db, f"SELECT id, status, odoo_so_name, total_amount, error_log, raw_json FROM {TBL_BILLING} "
              f"WHERE marketplace=%s AND mkp_order_id=%s", (MARKETPLACE, order_id))
    return r[0] if r else None


def reabrir_fila(db, order_id, b2b):
    """Si la orden ya tiene fila cerrada en staging, la regresa a pendiente."""
    f = fila(db, order_id)
    if not f:
        return 'sin fila (la siembra el miner)'
    cur = db.cursor()
    cur.execute(f"UPDATE {TBL_BILLING} SET status=%s, retry_count_loader=0 WHERE id=%s",
                ('PENDING_B2B_LINK' if b2b else 'PENDING_WB_INVOICE', f['id']))
    db.commit()
    cur.close()
    return f"{f['status']} -> pendiente"


def facturas(odoo, so_name):
    return odoo.execute_kw('account.move', 'search_read',
                           [[('move_type', '=', 'out_invoice'), ('invoice_origin', '=', so_name)]],
                           {'fields': ['id', 'name', 'state', 'journal_id', 'invoice_date', 'date',
                                       'amount_total', 'l10n_mx_edi_cfdi_uuid', 'ref'],
                            'order': 'id asc'})


def cancelar_factura(odoo, move_id):
    """Igual que 15_lote: en el espejo la localizacion puede bloquear el boton."""
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', move_id), '|', ('matched_debit_ids', '!=', False),
                               ('matched_credit_ids', '!=', False)]], {'fields': ['id']})
    if lineas:
        ejecutar_sin_retorno(odoo, 'account.move.line', 'remove_move_reconcile',
                             [x['id'] for x in lineas])
    for destino, boton in (('draft', 'button_draft'), ('cancel', 'button_cancel')):
        try:
            ejecutar_sin_retorno(odoo, 'account.move', boton, [move_id])
        except xmlrpc.client.Fault:
            pass
        if odoo.execute_kw('account.move', 'read', [[move_id]], {'fields': ['state']})[0]['state'] != destino:
            odoo.execute_kw('account.move', 'write', [[move_id], {'state': destino}])


def correr_01(ordenes, log_path):
    env = dict(os.environ, AMZ_ENTORNO='staging', AMZ_ORDER_IDS=','.join(ordenes),
               PYTHONIOENCODING='utf-8')
    with open(log_path, 'w', encoding='utf-8') as fh:
        r = subprocess.run([sys.executable, os.path.join(_dir, '01_amz_billing.py')],
                           cwd=_dir, env=env, stdout=fh, stderr=subprocess.STDOUT)
    return r.returncode


def correr_01_en_cierre(ordenes, log_path):
    """01 en este proceso, con el dia de la corrida fijado al 1-dic 08:30."""
    os.environ['AMZ_ORDER_IDS'] = ','.join(ordenes)
    spec = importlib.util.spec_from_file_location('amz01_cierre', os.path.join(_dir, '01_amz_billing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOY_CORRIDA = datetime(2026, 12, 1, 8, 30)
    fh = logging.FileHandler(log_path, encoding='utf-8')
    logging.getLogger().addHandler(fh)
    try:
        log.info(f"CIERRE simulado: en_cierre={mod.en_cierre_de_mes()} fecha "
                 f"{mod.inicio_de_mes():%Y-%m-%d}")
        mod.main()
    finally:
        logging.getLogger().removeHandler(fh)
        fh.close()
        os.environ.pop('AMZ_ORDER_IDS', None)


def main():
    if ENTORNO != 'staging' or ('test' not in ODOO_URL.lower() and '.dev.odoo.com' not in ODOO_URL.lower()):
        raise Exception("Solo corre en staging.")
    validar_entorno()
    os.makedirs(SALIDA, exist_ok=True)
    db = get_db_connection()
    odoo = OdooModelProxy(timeout=300)

    # --- P5: Business con CFDI de Amazon de septiembre y sin factura ---
    # (Las 6 de septiembre de la hoja A5 no sirven: el espejo no deja
    # cancelarlas, su periodo esta cerrado.)
    sept_so, sept_orden = None, None
    for c in q(db, """SELECT i.order_id o, s.name so
                      FROM somos_reyes.amz_b2b_invoices i
                      JOIN somos_reyes.odoo_new_sale_order_live s ON s.channel_order_reference = i.order_id
                      WHERE i.receipt_type = 'I' AND i.date >= '2026-09-01' AND i.date < '2026-10-01'
                        AND (i.status IS NULL OR i.status <> 'CANCEL')
                        AND (i.cancellation_date IS NULL OR i.cancellation_date = '')
                        AND s.invoice_status = 'to invoice' AND s.state IN ('sale', 'done')
                      GROUP BY i.order_id, s.name HAVING COUNT(*) = 1 LIMIT 40"""):
        if c['so'] <= 'SO10408465' and not [x for x in facturas(odoo, c['so']) if x['state'] != 'cancel']:
            sept_so, sept_orden = c['so'], c['o']
            break
    log.info(f"P5: Business de septiembre sin factura -> {sept_so} {sept_orden}")

    preparacion = {}
    for o in list(PARCIALES) + list(SIN_TOTAL):
        preparacion[o] = reabrir_fila(db, o, False)
    for o in ENVIO_B2B:
        preparacion[o] = reabrir_fila(db, o, True)
    if sept_orden:
        preparacion[sept_orden] = reabrir_fila(db, sept_orden, True)
    for o, p in preparacion.items():
        log.info(f"  prep {o}: {p}")

    ordenes = list(PARCIALES) + list(SIN_TOTAL) + list(ENVIO_B2B) + ([sept_orden] if sept_orden else [])
    log.info(f"Corrida normal de 01 con {len(ordenes)} ordenes (tarda: amz_transactions sin indice)...")
    rc = correr_01(ordenes, os.path.join(SALIDA, f'prueba_hoja_ruta_01_normal_{TS}.log'))
    log.info(f"01 normal termino con codigo {rc}")

    en_espera = [o for o in PARCIALES if (fila(db, o) or {}).get('status') == 'LIBERACION_PARCIAL']
    if en_espera:
        log.info(f"Corrida de CIERRE (1-dic simulado) con {en_espera}...")
        correr_01_en_cierre(en_espera, os.path.join(SALIDA, f'prueba_hoja_ruta_01_cierre_{TS}.log'))

    # --- Evaluacion ---
    filas = []

    def agregar(caso, orden, esperado, ok, detalle):
        f = fila(db, orden) or {}
        filas.append({'caso': caso, 'orden': orden, 'so': f.get('odoo_so_name'),
                      'estado_tabla': f.get('status'), 'esperado': esperado,
                      'resultado': 'OK' if ok else 'FALLA', 'detalle': detalle,
                      'error_log': (f.get('error_log') or '')[:250]})

    for o, so in PARCIALES.items():
        f = fila(db, o) or {}
        raw = json.loads(f.get('raw_json') or '{}') if f else {}
        cli = (raw.get('amazon') or {}).get('order_total_cliente')
        vivas = [x for x in facturas(odoo, so) if x['state'] == 'posted']
        if o in en_espera or 'LIBERACION_PARCIAL' in (f.get('error_log') or ''):
            ok = (f.get('status') == 'ODOO_INVOICED' and len(vivas) == 1
                  and str(vivas[0]['invoice_date']) == '2026-11-30'
                  and abs(vivas[0]['amount_total'] - a_float(cli)) <= 1.0)
            agregar('P1+P2', o, 'LIBERACION_PARCIAL y en el cierre factura del 30-nov por lo pagado', ok,
                    f"cliente {cli} | facturas {[(v['name'], v['invoice_date'], v['amount_total']) for v in vivas]}")
        else:
            ok = f.get('status') == 'ODOO_INVOICED' and len(vivas) == 1 \
                and abs(vivas[0]['amount_total'] - a_float(cli)) <= 1.0
            agregar('P1', o, 'ya liberada completa: se factura normal', ok,
                    f"cliente {cli} | facturas {[(v['name'], v['invoice_date'], v['amount_total']) for v in vivas]}")

    for o, so in SIN_TOTAL.items():
        f = fila(db, o) or {}
        vivas = [x for x in facturas(odoo, so) if x['state'] == 'posted']
        ok = (f.get('status') == 'ODOO_INVOICED' and len(vivas) == 1
              and 'ORDER_TOTAL_API' in (f.get('error_log') or ''))
        agregar('P3', o, 'API da el total; factura por la SO', ok,
                f"facturas {[(v['name'], v['amount_total']) for v in vivas]}")

    for o, so in ENVIO_B2B.items():
        f = fila(db, o) or {}
        vivas = [x for x in facturas(odoo, so) if x['state'] == 'posted']
        envio = []
        if vivas:
            envio = odoo.execute_kw('account.move.line', 'search_read',
                                    [[('move_id', '=', vivas[-1]['id']), ('product_id.default_code', '=', 'C-ENVIO')]],
                                    {'fields': ['price_unit']})
        ok = (f.get('status') == 'ODOO_B2B_LINKED' and len(vivas) == 1 and vivas[0]['name'] == 'AB-1245'
              and vivas[0]['journal_id'][1].startswith('Facturas Amazon Business')
              and abs(vivas[0]['amount_total'] - 793.00) <= 0.005 and bool(envio))
        agregar('P4', o, 'AB-1245 en AMZB, 793.00, con C-ENVIO', ok,
                f"facturas {[(v['name'], v['journal_id'][1], v['amount_total']) for v in vivas]} | envio {envio}")
        marca = odoo.execute_kw('sale.order', 'search_read', [[('name', '=', so)]],
                                {'fields': ['is_amazon_business']})
        agregar('P6', o, 'SO marcada is_amazon_business', bool(marca and marca[0]['is_amazon_business']),
                f"{marca}")

    if sept_orden:
        f = fila(db, sept_orden) or {}
        vivas = [x for x in facturas(odoo, sept_so) if x['state'] == 'posted']
        ok = (f.get('status') == 'ODOO_B2B_LINKED' and len(vivas) >= 1
              and all(x['journal_id'][1].startswith('Facturas Amazon Business') for x in vivas)
              and all(str(x['invoice_date']) <= '2026-09-30' < str(x['date']) for x in vivas))
        agregar('P5', sept_orden, 'CFDI de sept vinculado en AMZB; fecha contable despues del candado', ok,
                f"{sept_so}: {[(v['name'], v['invoice_date'], v['date'], v['amount_total']) for v in vivas]}")

    path = os.path.join(SALIDA, f'prueba_hoja_ruta_{TS}.csv')
    with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    for r in filas:
        log.info(f"{r['caso']:6} {r['resultado']:5} {r['orden']} {r['so']} {r['estado_tabla']} | {r['detalle']}")
    log.info(f"CSV: {path}")


def prueba_parcial():
    """P1b/P2b con una SO real del espejo y un cobro parcial armado a mano.

    Las ordenes parciales reales que caben en el espejo (SO <= SO10408465) ya
    se liberaron completas al 25-nov, asi que las ramas nuevas no se
    ejercitaban. Aqui se toma una B2C de noviembre sin factura y se siembra su
    fila con lo liberado = la mitad de la SO y lo pagado por el cliente = la SO.
    Se llama a procesar() de 01 dos veces: en un dia normal (debe quedar en
    LIBERACION_PARCIAL, sin factura) y en el cierre del 1-dic (debe facturar
    con fecha 30-nov por lo que pago el cliente)."""
    if ENTORNO != 'staging' or ('test' not in ODOO_URL.lower() and '.dev.odoo.com' not in ODOO_URL.lower()):
        raise Exception("Solo corre en staging.")
    validar_entorno()
    db = get_db_connection()
    odoo = OdooModelProxy(timeout=300)
    spec = importlib.util.spec_from_file_location('amz01_parcial', os.path.join(_dir, '01_amz_billing.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    so = None
    for c in q(db, """SELECT s.name, s.channel_order_reference o FROM somos_reyes.odoo_new_sale_order_live s
                      WHERE s.team_name = 'Team_Amazon' AND s.state = 'sale' AND s.invoice_status = 'to invoice'
                        AND s.name >= 'SO10395000' AND s.name <= 'SO10408465'
                      ORDER BY s.name LIMIT 60"""):
        if [x for x in facturas(odoo, c['name']) if x['state'] != 'cancel']:
            continue
        info = mod.buscar_so(odoo, c['name'])
        if info and info.get('state') == 'sale' and mod.venta_de_mes_anterior(info, datetime(2026, 12, 1, 8, 30)):
            lineas = mod.armar_lineas(odoo, info)
            total = mod.total_estimado(lineas) if lineas else 0
            if total > 100:
                so, orden = info, c['o'] or f"PRUEBA-{c['name']}"
                break
    if not so:
        raise Exception("No hay SO de noviembre sin factura en el espejo.")
    pagado = round(total, 2)
    liberado = round(total / 2, 2)
    log.info(f"P2b: {so['name']} {orden} | pagado {pagado} | liberado {liberado}")
    raw = {'log_conciliacion': {'amazon_order_id': orden, 'odoo_so': so['name'], 'tipo': 'B2C_PUBLICO_GENERAL'},
           'importes': {'cfdi_total': liberado, 'envio_base': 0, 'envio_iva': 0, 'deposito': 0, 'fees_total': 0},
           'amazon': {'order_total_cliente': pagado, 'is_business_order': 0},
           'generado_por': '17_prueba_hoja_ruta_24nov.py (cobro parcial armado)'}
    cur = db.cursor()
    cur.execute(f"""INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, odoo_so_name, total_amount, raw_json,
                                               status, retry_count, retry_count_loader)
                    VALUES (%s, %s, %s, %s, %s, 'PENDING_WB_INVOICE', 0, 0)
                    ON DUPLICATE KEY UPDATE odoo_so_name=VALUES(odoo_so_name), total_amount=VALUES(total_amount),
                        raw_json=VALUES(raw_json), status='PENDING_WB_INVOICE', retry_count_loader=0""",
                (MARKETPLACE, orden, so['name'], liberado, json.dumps(raw)))
    db.commit()
    cur.close()

    account_cxc = mod.get_account_id(odoo, mod.ACCOUNT_AMAZON_CXC)
    catalogos = mod.cargar_catalogos(odoo)
    filas = []
    for etiqueta, hoy in (('P1b dia normal', datetime(2026, 11, 25, 15, 0)),
                          ('P2b cierre 1-dic', datetime(2026, 12, 1, 8, 30))):
        mod.HOY_CORRIDA = hoy
        rec = q(db, f"SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, raw_json, status, "
                    f"retry_count_loader FROM {TBL_BILLING} WHERE marketplace=%s AND mkp_order_id=%s",
                (MARKETPLACE, orden))[0]
        if rec['status'] == 'LIBERACION_PARCIAL':
            # lo que haria el miner al refrescarla
            cur = db.cursor()
            cur.execute(f"UPDATE {TBL_BILLING} SET status='PENDING_WB_INVOICE' WHERE id=%s", (rec['id'],))
            db.commit()
            cur.close()
            rec['status'] = 'PENDING_WB_INVOICE'
        mod.claim(db, rec['id'])
        stats = mod.nuevos_stats()
        mod.procesar(odoo, db, rec, account_cxc, stats, catalogos, {})
        f = fila(db, orden)
        vivas = [x for x in facturas(odoo, so['name']) if x['state'] == 'posted']
        filas.append((etiqueta, f['status'], [(v['name'], v['invoice_date'], v['amount_total']) for v in vivas],
                      (f['error_log'] or '')[:160]))
    for r in filas:
        log.info(f"{r[0]}: estado {r[1]} | facturas {r[2]} | {r[3]}")
    ok1 = filas[0][1] == 'LIBERACION_PARCIAL' and not filas[0][2]
    ok2 = (filas[1][1] == 'ODOO_INVOICED' and len(filas[1][2]) == 1 and str(filas[1][2][0][1]) == '2026-11-30'
           and abs(filas[1][2][0][2] - pagado) <= 1.0)
    log.info(f"P1b {'OK' if ok1 else 'FALLA'} | P2b {'OK' if ok2 else 'FALLA'}")


if __name__ == '__main__':
    if '--parcial' in sys.argv:
        prueba_parcial()
    else:
        main()
