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
  4. Que Fenix este al dia. Si no lo esta NO se aborta (03 puede replicar los
     CFDI que ya tienen XML), pero 04 no va a emitir nada: se avisa.
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
                        frescura_ftm, frescura_fenix)

FTM_MAX_HORAS = float(os.getenv('WM_FTM_MAX_HORAS', '36'))
FENIX_MAX_HORAS = float(os.getenv('WM_FENIX_MAX_HORAS', '48'))


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
    log.info(f"Fenix: ultima carga hace {h_fenix} h (maximo {FENIX_MAX_HORAS})")

    if h_ftm is None or h_ftm > FTM_MAX_HORAS:
        errores.append(f"FTM sin actualizar hace {h_ftm} h: sin universo de venta confiable.")

    if h_fenix is None or h_fenix > FENIX_MAX_HORAS:
        aviso = (f"Walmart: Fenix lleva {h_fenix} h sin cargarse. 04 NO emitira "
                 f"facturas de WB hasta que se actualice (no sabemos si Walmart ya facturo).")
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
