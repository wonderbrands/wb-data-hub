"""
00b_wm_fenix_etl.py
-------------------
ETL de la API de Fenix (Walmart) a la tabla de control. Reemplaza al crawler
de Data para saber QUE LINEAS PIDIO FACTURAR EL CLIENTE en Walmart, que es lo
que evita el doble CFDI: si el cliente pidio factura, nosotros no timbramos a
publico general.

Escribe en finance.mkp_billing_{entorno}, marketplace = 'WALMART_FENIX' (no
toca somos_reyes.wm_fenix_invoices_report, que sigue llenando el crawler):
  PRECFDI:<cfdiid>    una por peticion de factura. status:
                        RECIBIDO   Fenix ya timbro (el XML se baja del SAT en 02)
                        CON_ERROR  Fenix no pudo timbrar (RFC del cliente, etc.)
                        NUEVO      peticion sin atender
  BLOQUEADO:<id>      linea detenida por Fenix hasta recibir numero de serie,
                      lote o pedimento.
  ETL:ULTIMA_CORRIDA  marca de la ultima corrida completa. 04 no emite si esta
                      marca es vieja (WM_FENIX_MAX_HORAS): fail-closed.
raw_json: cfdiid, iderp (= FTM.so_prime_line_sub_line_no), orderno, invoiceno,
reqtransactionid, error, peticion_cdmx (hora de la peticion, del invoiceno) y
visto_en (ultima corrida que la vio).

La API regresa siempre el conjunto completo (ignora el rango de fechas), asi
que cada corrida reescribe todo. Una fila que ya no aparece se queda como
estaba: una peticion que alguna vez existio sigue frenando la factura a
publico general hasta que una persona lo revise.

Al final liga los CFDI del repositorio que sigan sueltos (ligar_cfdis), por si
la peticion llego despues que el XML.

Un error de la API NO detiene el flujo (03 sigue replicando): avisa por
Slack y no actualiza la marca, con lo que 04 no emite.

Variables: FENIX_WALMART_USER, FENIX_WALMART_PASSWORD, FENIX_API_URL,
           WM_DRY_RUN=1 (no escribe).
"""

import os
import sys
from collections import Counter

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, get_db_connection, notify_slack, a_json,
                        ahora_cdmx, lotes, TBL_BILLING, MKP_FENIX, CLAVE_ETL,
                        PRE_RECIBIDO, PRE_CON_ERROR, PRE_NUEVO, PRE_BLOQUEADO, PRIORIDAD_PRE,
                        ligar_cfdis)
import _fenix_api as fenix

DRY_RUN = os.getenv('WM_DRY_RUN', '0').strip() in ('1', 'true', 'TRUE')


def fila_precfdi(p, status, visto):
    t = fenix.peticion_cdmx(p.get('invoiceno'))
    return {'clave': f"PRECFDI:{p['cfdiid']}", 'status': status, 'error': (p.get('error') or '').strip()[:900] or None,
            'raw': {'cfdiid': p['cfdiid'], 'iderp': p.get('iderp'), 'orderno': p.get('orderno'),
                    'primeline': p.get('primeline'), 'invoiceno': p.get('invoiceno'),
                    'reqtransactionid': p.get('reqtransactionid'), 'error': p.get('error'),
                    'fechapublicacion': p.get('fechapublicacion'),
                    'peticion_cdmx': f"{t:%Y-%m-%d %H:%M:%S}" if t else None, 'visto_en': visto}}


def fila_bloqueado(b, visto):
    iderp = f"{b.get('orderno')}-{b.get('primeline')}"
    motivo = ', '.join(k for k in ('serializado', 'lotificado', 'pedimentado') if b.get(k))
    return {'clave': f"BLOQUEADO:{b['id']}", 'status': PRE_BLOQUEADO, 'error': f"Falta: {motivo}" if motivo else None,
            'raw': dict(b, iderp=iderp, visto_en=visto)}


def main():
    banner(f"00b - ETL API DE FENIX{' | DRY RUN' if DRY_RUN else ''}")
    if not fenix.credenciales_configuradas():
        log.error('Sin FENIX_WALMART_USER / FENIX_WALMART_PASSWORD: no se consulta Fenix. 04 no emitira.')
        notify_slack('Walmart 00b: faltan las credenciales de la API de Fenix. 04 no emitira hasta corregirlo.')
        return

    visto = f"{ahora_cdmx():%Y-%m-%d %H:%M:%S}"
    try:
        listas = {PRE_RECIBIDO: fenix.listar_recibidos(),
                  PRE_CON_ERROR: fenix.listar_con_error(),
                  PRE_NUEVO: fenix.listar_nuevos()}
        bloqueados = fenix.listar_bloqueados()
    except fenix.ErrorFenix as e:
        log.error(f"API de Fenix: {e}")
        notify_slack(f"Walmart 00b: la API de Fenix fallo ({str(e)[:200]}). 04 no emitira hasta la siguiente "
                     f"corrida buena.")
        return

    filas = {}
    for status, registros in listas.items():
        for p in registros:
            if not p.get('cfdiid'):
                continue
            f = fila_precfdi(p, status, visto)
            previa = filas.get(f['clave'])
            if previa is None or PRIORIDAD_PRE[status] > PRIORIDAD_PRE[previa['status']]:
                filas[f['clave']] = f
    for b in bloqueados:
        if b.get('id') is not None:
            f = fila_bloqueado(b, visto)
            filas[f['clave']] = f

    conteo = Counter(f['status'] for f in filas.values())
    log.info(f"API: recibidos={len(listas[PRE_RECIBIDO])} con_error={len(listas[PRE_CON_ERROR])} "
             f"nuevos={len(listas[PRE_NUEVO])} bloqueados={len(bloqueados)} -> filas {dict(conteo)}")
    sin_hora = sum(1 for f in filas.values() if f['clave'].startswith('PRECFDI:') and not f['raw']['peticion_cdmx'])
    log.info(f"Peticiones sin hora en el invoiceno (no entran al ligado rapido): {sin_hora}")

    if DRY_RUN:
        log.info('DRY RUN: no se escribio nada.')
        return

    db = get_db_connection()
    cur = db.cursor()
    try:
        for bloque in lotes(list(filas.values()), 300):
            cur.executemany(f"""
                INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, raw_json, status, error_log,
                                           retry_count, retry_count_loader, processed_at)
                VALUES (%s, %s, %s, %s, %s, 0, 0, NOW())
                ON DUPLICATE KEY UPDATE raw_json = VALUES(raw_json), error_log = VALUES(error_log),
                                        processed_at = NOW(), status = VALUES(status)
            """, [(MKP_FENIX, f['clave'], a_json(f['raw']), f['status'], f['error']) for f in bloque])
            db.commit()
        cur.execute(f"""
            INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, raw_json, status, retry_count,
                                       retry_count_loader, processed_at)
            VALUES (%s, %s, %s, 'OK', 0, 0, NOW())
            ON DUPLICATE KEY UPDATE raw_json = VALUES(raw_json), processed_at = NOW()
        """, (MKP_FENIX, CLAVE_ETL, a_json({'corrida': visto, 'filas': dict(conteo)})))
        db.commit()
        stats = ligar_cfdis(db)
        if stats:
            log.info(f"Ligado de CFDI sueltos: {dict(stats)}")
    finally:
        cur.close()
        db.close()
    log.info(f"Escritas {len(filas)} filas de Fenix y la marca {CLAVE_ETL}.")


if __name__ == '__main__':
    configurar_log()
    main()
