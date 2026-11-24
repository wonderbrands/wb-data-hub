"""
00_wm_precheck.py
-----------------
Verificacion previa del flujo de Walmart. SOLO LECTURA. Si algo falla, sale
con codigo 1 y Kestra no corre los scripts siguientes.

Revisa:
  1. El candado de entorno (WM_ENTORNO contra la URL de Odoo).
  2. Que Odoo responda y tenga la CxC 105.01.007, la 205.06.003 y el equipo
     Team_Walmart (nombre exacto).
  3. Que las transacciones de Walmart (FTM) esten al dia. Sin FTM no hay
     universo de venta: se aborta.
  4. Que esten las credenciales de la API de Fenix (00b). Sin ellas no se
     aborta (03 puede replicar), pero 04 no emitira: se avisa. La frescura del
     crawler (wm_fenix_invoices_report) solo se informa: es opcional.
  5. Que la e.firma (si esta configurada) abra, sea de SRE1410278MA y este
     vigente. Si falla tampoco se aborta: solo 02 se queda sin descargar.
"""

import os
import sys

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, validar_entorno, notify_slack,
                        get_db_connection, OdooModelProxy, cargar_catalogos,
                        frescura_ftm, frescura_fenix, USAR_CRAWLER)
import _fenix_api as fenix

FTM_MAX_HORAS = float(os.getenv('WM_FTM_MAX_HORAS', '36'))


def main():
    configurar_log()
    banner('00 - PRECHECK WALMART')
    errores = []

    try:
        validar_entorno()
    except Exception as e:
        errores.append(str(e))

    if not errores:
        try:
            odoo = OdooModelProxy()
            cat = cargar_catalogos(odoo)
            log.info(f"Odoo OK | CxC id={cat['cuenta_cxc']} | 205.06.003 id="
                     f"{cat['cuenta_merc_no_enviada']} | Team_Walmart id={cat['team_walmart']}")
        except Exception as e:
            errores.append(f"Odoo: {e}")

    # e.firma para la descarga del SAT (02). Sin ella 02 no descarga, pero el
    # resto del flujo sigue: no se aborta, se avisa.
    import _sat_descarga as sat
    from _wm_common import RFC_EMISOR
    if sat.fiel_configurada():
        try:
            fiel = sat.cargar_fiel()
            if fiel['rfc'] != RFC_EMISOR:
                raise Exception(f"es del RFC {fiel['rfc']}, no de {RFC_EMISOR}")
            log.info(f"e.firma OK: {fiel['rfc']}, vigente hasta {fiel['vence']:%Y-%m-%d}")
        except Exception as e:
            log.error(f"e.firma invalida: {e}. 02 no descargara XML del SAT.")
            notify_slack(f"Walmart: e.firma invalida ({e}). No se descargan XML del SAT.")
    else:
        log.warning('Sin e.firma (SAT_FIEL_*): 02 no descargara XML del SAT.')

    db = get_db_connection()
    cur = db.cursor()
    h_ftm = frescura_ftm(cur)
    h_fenix = frescura_fenix(cur)
    cur.close()
    db.close()

    log.info(f"FTM: ultima actualizacion hace {h_ftm} h (maximo {FTM_MAX_HORAS})")
    log.info(f"Crawler de Fenix (opcional, {'en uso' if USAR_CRAWLER else 'apagado'}): "
             f"ultima carga hace {h_fenix} h")

    if h_ftm is None or h_ftm > FTM_MAX_HORAS:
        errores.append(f"FTM sin actualizar hace {h_ftm} h: sin universo de venta confiable.")

    if not fenix.credenciales_configuradas():
        aviso = ('Walmart: faltan FENIX_WALMART_USER / FENIX_WALMART_PASSWORD. 00b no consultara la API de '
                 'Fenix y 04 NO emitira facturas de WB.')
        log.warning(aviso)
        notify_slack(aviso)

    if errores:
        for e in errores:
            log.error(e)
        notify_slack('Walmart precheck FALLO: ' + ' | '.join(errores))
        sys.exit(1)
    log.info('Precheck OK.')


if __name__ == '__main__':
    main()
