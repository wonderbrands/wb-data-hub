"""
Diagnóstico (SOLO LECTURA) de los pagos aprobados que no tienen cobro registrado.

Toma los pagos de Pagos_por_aplicar_ML_<fecha>.csv (salida de analisis_cobros_ml.py) y, para
cada uno, repite las mismas consultas que hace el 03 (ML /orders y MP /v1/payments) para saber
en qué paso lo descarta. No escribe en MySQL ni en Odoo.

Salida: finanzas/Diagnostico_pagos_sin_cobro_ML_<fecha>.csv
Corre desde la raíz del repo:
    .venv/Scripts/python.exe test/financial/credit_note_refund_processes/mercadolibre/diagnostico_pagos_sin_cobro.py
"""
import csv
import os
import sys
import time
from datetime import datetime, timedelta

import mysql.connector
import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..', '..'))
load_dotenv(os.path.join(REPO, '.env'))

# ── Configuración ──────────────────────────────────────────────
INPUT_CSV = os.path.join(HERE, 'finanzas', 'Pagos_por_aplicar_ML_2026-11-19.csv')
CATEGORIES = ('B1', 'B2')          # prefijos de la columna Categoría a diagnosticar
ML_SELLER_ID = '25523702'
MKP_MARKETPLACE = 'MERCADO_LIBRE'
MP_UTC_OFFSET_HOURS = 4            # igual que el 03
SLEEP = 0.3


def main():
    rows = [r for r in csv.DictReader(open(INPUT_CSV, encoding='utf-8-sig'))
            if r['Categoría'][:2] in CATEGORIES]
    print(f"Pagos a diagnosticar: {len(rows)}")

    db = mysql.connector.connect(host=os.getenv('DB_HOST'), user=os.getenv('DB_USER'),
                                 password=os.getenv('DB_PASSWORD'))
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT token FROM somos_reyes.tokens WHERE seller_id = %s", (ML_SELLER_ID,))
    ml_headers = {'Authorization': f"Bearer {cur.fetchone()['token']}"}
    mp_headers = {'Authorization': f"Bearer {os.getenv('mercado_pago_token')}"}

    out = []
    for r in rows:
        order_id, payment_id = r['Orden'], r['Pago (payment_id)']
        res = {'categoria': r['Categoría'][:2], 'orden': order_id, 'so': r['SO'],
               'factura': r['Factura'], 'pago': payment_id, 'monto': r['Monto pago']}

        # Paso A del 03: ¿el pago aparece aprobado en la orden de ML?
        ro = requests.get(f"https://api.mercadolibre.com/orders/{order_id}", headers=ml_headers, timeout=15)
        res['ml_http'] = ro.status_code
        pays = ro.json().get('payments', []) if ro.status_code == 200 else []
        in_order = [p for p in pays if str(p.get('id')) == str(payment_id)]
        res['ml_pagos_en_orden'] = len(pays)
        res['ml_status_pago'] = in_order[0].get('status') if in_order else 'NO_ESTA_EN_LA_ORDEN'
        res['ml_pack_id'] = ro.json().get('pack_id') if ro.status_code == 200 else ''

        # Paso B del 03: fecha de liberación en MP
        rp = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=mp_headers, timeout=15)
        res['mp_http'] = rp.status_code
        mp = rp.json() if rp.status_code == 200 else {}
        res['mp_status'] = mp.get('status', '')
        res['mp_status_detail'] = mp.get('status_detail', '')
        res['mp_money_release_status'] = mp.get('money_release_status', '')
        res['mp_date_released'] = mp.get('date_released') or ''
        res['mp_money_release_date'] = mp.get('money_release_date') or ''
        res['mp_order_id'] = (mp.get('order') or {}).get('id', '')
        res['mp_external_reference'] = mp.get('external_reference', '')

        # Veredicto: dónde lo descarta el 03
        rel = res['mp_date_released'] or res['mp_money_release_date']
        if ro.status_code != 200:
            v = 'ERROR_API_ML'
        elif not in_order:
            v = 'PAGO_NO_ESTA_EN_LA_ORDEN'   # el 03 recorre los pagos de la orden: nunca lo ve
        elif res['ml_status_pago'] != 'approved':
            v = 'NO_APROBADO_EN_ML'
        elif rp.status_code != 200:
            v = 'ERROR_API_MP'
        elif not rel:
            v = 'SIN_FECHA_LIBERACION'
        else:
            d = datetime.strptime(rel[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=MP_UTC_OFFSET_HOURS)
            v = 'LIBERACION_FUTURA' if d > datetime.utcnow() else 'LIBERADO_EL_03_DEBIO_TOMARLO'
        res['veredicto'] = v
        out.append(res)
        time.sleep(SLEEP)

    # ¿Está ya en mkp_payments_prod por otra orden (carrito)?
    ids = [o['pago'] for o in out]
    cur.execute(f"SELECT payment_id, mkp_order_id, status FROM finance.mkp_payments_prod "
                f"WHERE marketplace=%s AND payment_id IN ({','.join(['%s'] * len(ids))})",
                (MKP_MARKETPLACE, *ids))
    reg = {str(x['payment_id']): x for x in cur.fetchall()}
    for o in out:
        x = reg.get(str(o['pago']))
        o['registrado_con_orden'] = x['mkp_order_id'] if x else ''
        o['registrado_status'] = x['status'] if x else ''

    stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    path = os.path.join(HERE, 'finanzas', f'Diagnostico_pagos_sin_cobro_ML_{stamp}.csv')
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    from collections import Counter
    for (c, v), n in sorted(Counter((o['categoria'], o['veredicto']) for o in out).items()):
        amt = sum(float(o['monto'] or 0) for o in out if o['categoria'] == c and o['veredicto'] == v)
        print(f"  {c} {v}: {n} | ${amt:,.2f}")
    print(f"Archivo: {path}")
    cur.close()
    db.close()


if __name__ == '__main__':
    sys.exit(main())
