"""
05_wm_control.py
----------------
Reporte de control del flujo de Walmart. SOLO LECTURA.

  1. Conteo por estado de la tabla de control (marketplace='WALMART').
  2. Lo que necesita a una persona: DOBLE_CFDI, TOTAL_DIFF, XML_INVALIDO,
     ERROR agotados, y las esperas (sin SO, SO duplicada, mixtas, REVISAR...).
  3. Facturas de WB con timbrado fallido.
  4. Frescura de FTM, del ETL de la API de Fenix (00b) y del crawler.
  4b. Fuente propia de Fenix (marketplace='WALMART_FENIX'): peticiones por
     estado y CFDI de Walmart SIN_LIGAR (Finanzas los liga a mano o espera al
     crawler); van al CSV con UUID, total, receptor y fecha.
  5. Cruce opcional contra el CSV de Finanzas (WM_CSV_FINANZAS), en los dos
     sentidos, mientras corren en paralelo. Se aceptan las columnas de los
     archivos actuales: 'Marketplace Reference' (autofacturacion.csv) o
     'so_name' (facturacion_global.csv).

Escribe un CSV con el detalle en WM_REPORTE_DIR (por defecto, esta carpeta) y
manda el resumen a Slack.
"""

import os
import sys
import csv
import json
from collections import Counter

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, notify_slack, get_db_connection,
                        frescura_fenix, frescura_ftm, TBL_BILLING, MARKETPLACE, HOY_CORRIDA,
                        ST_DOBLE_CFDI, ST_TOTAL_DIFF, ST_XML_INVALIDO, ST_ERROR, ST_NO_SO,
                        ST_SO_DUPLICADA, ST_SO_OTRO_EQUIPO, ST_SO_CANCELADA, ST_ORDEN_MIXTA,
                        ST_WM_REVISAR, ST_WM_EXCEDENTE, ST_RECLASIFICAR, ST_PENDING_WM_XML,
                        ST_WB_INVOICED, ST_MANUAL_SERIE, ST_BORRADOR_AJENO, ST_WM_NO_VIGENTE,
                        ST_PENDING_WM_UUID, MKP_FENIX, CFDI_SIN_LIGAR, frescura_etl_fenix)

WM_CSV_FINANZAS = os.getenv('WM_CSV_FINANZAS', '').strip()
WM_REPORTE_DIR = os.getenv('WM_REPORTE_DIR', _dir)

ATENCION = (ST_DOBLE_CFDI, ST_TOTAL_DIFF, ST_XML_INVALIDO, ST_NO_SO, ST_SO_DUPLICADA,
            ST_SO_OTRO_EQUIPO, ST_SO_CANCELADA, ST_ORDEN_MIXTA, ST_WM_REVISAR,
            ST_WM_EXCEDENTE, ST_RECLASIFICAR, ST_MANUAL_SERIE, ST_BORRADOR_AJENO,
            ST_WM_NO_VIGENTE)


def leer_csv_finanzas(ruta):
    """Ordenes de Walmart y SO del CSV de Finanzas."""
    ordenes, sos = set(), set()
    with open(ruta, encoding='utf-8-sig', newline='') as fh:
        for row in csv.DictReader(fh):
            ref = (row.get('Marketplace Reference') or '').strip()
            so = (row.get('so_name') or '').strip()
            if ref:
                ordenes.add(ref)
            if so:
                sos.add(so)
    return ordenes, sos


def main():
    banner('05 - CONTROL WALMART')
    db = get_db_connection()
    cur = db.cursor(dictionary=True)

    cur.execute(f"""SELECT status, COUNT(*) n FROM {TBL_BILLING}
                    WHERE marketplace = %s GROUP BY status ORDER BY status""", (MARKETPLACE,))
    conteo = {r['status']: r['n'] for r in cur.fetchall()}
    for s, n in conteo.items():
        log.info(f"  {s:<24} {n:>7}")

    marcas = ','.join(['%s'] * len(ATENCION))
    cur.execute(f"""
        SELECT mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, status, error_log,
               retry_count_loader, processed_at
        FROM {TBL_BILLING}
        WHERE marketplace = %s
          AND (status IN ({marcas})
               OR (status = %s AND retry_count_loader >= 3)
               OR (status = %s AND error_log LIKE 'TIMBRADO_FALLIDO%%'))
        ORDER BY status, mkp_order_id
    """, tuple([MARKETPLACE] + list(ATENCION) + [ST_ERROR, ST_WB_INVOICED]))
    detalle = cur.fetchall()

    cur2 = db.cursor()
    h_ftm, h_fenix, h_etl = frescura_ftm(cur2), frescura_fenix(cur2), frescura_etl_fenix(cur2)
    cur2.close()

    # Fuente propia de Fenix
    cur.execute(f"""SELECT SUBSTRING_INDEX(mkp_order_id, ':', 1) AS tipo, status, COUNT(*) n FROM {TBL_BILLING}
                    WHERE marketplace = %s GROUP BY 1, 2 ORDER BY 1, 2""", (MKP_FENIX,))
    fuente = {f"{r['tipo']}/{r['status']}": r['n'] for r in cur.fetchall()}
    log.info(f"Fuente propia de Fenix: {fuente}")
    cur.execute(f"""SELECT cfdi_uuid, total_amount, raw_json FROM {TBL_BILLING}
                    WHERE marketplace = %s AND mkp_order_id LIKE 'CFDI:%%' AND status = %s
                    ORDER BY cfdi_uuid""", (MKP_FENIX, CFDI_SIN_LIGAR))
    sin_ligar = []
    for r in cur.fetchall():
        raw = json.loads(r['raw_json']) if isinstance(r['raw_json'], str) else (r['raw_json'] or {})
        sin_ligar.append((r['cfdi_uuid'], r['total_amount'], raw))

    cruce = []
    if WM_CSV_FINANZAS:
        ordenes_fin, sos_fin = leer_csv_finanzas(WM_CSV_FINANZAS)
        cur.execute(f"""SELECT SUBSTRING_INDEX(mkp_order_id, ':', 1) AS orden, odoo_so_name, status
                        FROM {TBL_BILLING} WHERE marketplace = %s""", (MARKETPLACE,))
        nuestras = cur.fetchall()
        ordenes_nuestras = {r['orden'] for r in nuestras}
        sos_nuestras = {r['odoo_so_name'] for r in nuestras if r['odoo_so_name']}
        for o in sorted(ordenes_fin - ordenes_nuestras):
            cruce.append({'lado': 'SOLO_FINANZAS', 'clave': o})
        for s in sorted(sos_fin - sos_nuestras):
            cruce.append({'lado': 'SOLO_FINANZAS', 'clave': s})
        log.info(f"CSV Finanzas: {len(ordenes_fin)} ordenes + {len(sos_fin)} SO; "
                 f"{len(cruce)} no estan en la tabla de control.")
    cur.close()
    db.close()

    nombre = os.path.join(WM_REPORTE_DIR, f"control_walmart_{HOY_CORRIDA:%Y%m%d_%H%M}.csv")
    with open(nombre, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['seccion', 'mkp_order_id', 'odoo_so_name', 'cfdi_uuid', 'total', 'status',
                    'retries', 'detalle'])
        for r in detalle:
            w.writerow(['ATENCION', r['mkp_order_id'], r['odoo_so_name'], r['cfdi_uuid'],
                        r['total_amount'], r['status'], r['retry_count_loader'], r['error_log']])
        for c in cruce:
            w.writerow(['CRUCE_FINANZAS', c['clave'], '', '', '', c['lado'], '', ''])
        for uuid, total, raw in sin_ligar:
            w.writerow(['CFDI_WALMART_SIN_LIGAR', '', '', uuid, total, CFDI_SIN_LIGAR, '',
                        f"fecha={raw.get('fecha')} receptor={raw.get('receptor_rfc')} {raw.get('receptor_nombre')} "
                        f"upc={raw.get('upc')} folio={raw.get('serie')}{raw.get('folio')}"])
    log.info(f"Detalle: {nombre}")

    por_estado = Counter(r['status'] for r in detalle)
    resumen = (f"Walmart control | FTM {h_ftm} h | ETL Fenix {h_etl} h | crawler {h_fenix} h | "
               f"facturadas WB={conteo.get(ST_WB_INVOICED, 0)} "
               f"replicadas WM={conteo.get('WM_LINKED', 0)} "
               f"sin XML={conteo.get(ST_PENDING_WM_XML, 0)} "
               f"sin ligar={conteo.get(ST_PENDING_WM_UUID, 0)} ordenes / {len(sin_ligar)} CFDI | "
               f"peticiones con error={fuente.get('PRECFDI/CON_ERROR', 0)} nuevas={fuente.get('PRECFDI/NUEVO', 0)} | "
               f"atencion: " + (', '.join(f"{s}={n}" for s, n in sorted(por_estado.items())) or 'nada'))
    log.info(resumen)
    notify_slack(resumen)


if __name__ == '__main__':
    configurar_log()
    main()
