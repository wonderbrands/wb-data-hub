"""
00_conteo_lote_test.py
----------------------
CONTEO DEL LOTE ELEGIBLE -- SOLO LECTURA

Responde una sola pregunta antes de correr 01_amz_billing.py:

    "Con la configuracion actual, cuantas ordenes B2B y cuantas B2C hay
     realmente disponibles para facturar?"

No escribe nada. Ni MySQL, ni Odoo, ni Slack. Se puede correr en cualquier
momento sin consecuencias.


POR QUE IMPORTA
---------------
01_amz_billing.py trabaja con cuotas por tipo (QUOTA_B2B / QUOTA_B2C). Si se
pide un lote de prueba de 10 B2B y en la ventana solo existen 3, la corrida
termina sin llenar la cuota y sin que quede claro por que. Este script lo dice
antes, no despues.


POR QUE IMPORTA QUERY_LOTE DE 01 EN VEZ DE COPIARLA
---------------------------------------------------
Una copia de la query se desincroniza en la primera correccion que se le haga
a 01. Contar sobre una query distinta a la que factura no sirve de nada: el
punto entero es saber que va a ver el miner.

Se carga con importlib porque el nombre '01_amz_billing' empieza con digito y
no es un identificador valido de Python, asi que no se puede escribir
'import 01_amz_billing'. Es el unico motivo del rodeo; el modulo tiene su
guarda if __name__ == '__main__', asi que importarlo no ejecuta nada.
"""

import os
import sys
import logging
import importlib.util
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, TBL_BILLING, MARKETPLACE,
    banner, get_db_connection, a_float,
    validar_entorno, OdooModelProxy,
)

# El conteo consulta Odoo para descontar las ordenes que YA tienen factura
# viva. Se puede apagar si Odoo no esta disponible; entonces el numero de
# elegibles queda sobreestimado y el log lo dice.
CONSULTAR_ODOO = os.getenv('CONTEO_SIN_ODOO', '').strip().lower() not in ('1', 'true', 'si')

# Tamano de lote para preguntarle a Odoo por varias SO de una sola llamada.
LOTE_ODOO = 300


def cargar_modulo_billing():
    ruta = os.path.join(_dir, '01_amz_billing.py')
    spec = importlib.util.spec_from_file_location('amz_billing_mod', ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def leer_staging(cursor):
    """Lo que ya esta registrado, para no contarlo como disponible."""
    cursor.execute(f"""
        SELECT mkp_order_id, status FROM {TBL_BILLING}
        WHERE marketplace = %s
    """, (MARKETPLACE,))
    return {r['mkp_order_id']: r['status'] for r in cursor.fetchall()}


def clasificar(row, ya_en_staging, hoy, grace_days):
    """Reproduce la decision del miner, sin escribir.

    Los tres estados posibles son los mismos que produce run_miner():
      NUEVA_ELEGIBLE  se insertaria como pendiente y consumiria cuota
      NUEVA_GRACIA    se insertaria como SKIPPED_GRACE (no consume cuota)
      YA_EN_STAGING:x ya existe; el miner la salta
    """
    es_b2b = bool(row['amz_cfdi_uuid'])
    tipo = 'B2B' if es_b2b else 'B2C'

    st = ya_en_staging.get(row['mkp_order_id'])
    if st:
        return tipo, f'YA_EN_STAGING:{st}'

    # El periodo de gracia solo aplica a B2C: si Amazon ya emitio el CFDI no
    # hay nada que esperar.
    if not es_b2b and row['posted_date']:
        posted = row['posted_date']
        if isinstance(posted, str):
            posted = datetime.strptime(posted[:19], '%Y-%m-%d %H:%M:%S')
        if (hoy - posted).days < grace_days:
            return tipo, 'NUEVA_GRACIA'

    return tipo, 'NUEVA_ELEGIBLE'


def facturas_vivas_en_odoo(nombres_so):
    """SO que ya tienen una factura viva en Odoo.

    Es EL dato que faltaba. Contar filas que no estan en la tabla de control
    sobreestima el lote: en una ventana pasada, facturacion_automatica_1a1 ya
    facturo la mayoria en produccion y el espejo arrastra esas facturas. En la
    corrida del 11-sep fueron 54 de 60 candidatos.

    Se pregunta por lotes para no hacer una llamada por orden.
    """
    if not nombres_so:
        return set()

    odoo = OdooModelProxy(timeout=300)
    vivas = set()
    for i in range(0, len(nombres_so), LOTE_ODOO):
        trozo = nombres_so[i:i + LOTE_ODOO]
        r = odoo.execute_kw(
            'account.move', 'search_read',
            [[('move_type', '=', 'out_invoice'), ('state', '!=', 'cancel'),
              ('invoice_origin', 'in', trozo)]],
            {'fields': ['invoice_origin']})
        vivas.update(x['invoice_origin'] for x in r if x.get('invoice_origin'))
        log.info(f"  Odoo: {min(i + LOTE_ODOO, len(nombres_so))}/"
                 f"{len(nombres_so)} SO consultadas, {len(vivas)} ya facturadas")
    return vivas


def run():
    m = cargar_modulo_billing()

    log.info(f"AMZ_START_DATE      : {m.AMZ_START_DATE}")
    log.info(f"AMZ_END_DATE        : {m.AMZ_END_DATE or '(sin limite: hasta hoy)'}")
    log.info(f"GRACE_DAYS          : {m.GRACE_DAYS}")
    log.info(f"DEDUP_LOOKBACK_DAYS : {m.DEDUP_LOOKBACK_DAYS}")
    log.info(f"Cuotas configuradas : B2B={m.QUOTA_B2B} | B2C={m.QUOTA_B2C}")
    log.info('')

    db_conn = get_db_connection()
    cursor = db_conn.cursor(dictionary=True)

    log.info("Ejecutando QUERY_LOTE (la misma que usa el miner)...")
    sql, params = m.construir_query_lote()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    log.info(f"{len(rows)} orden(es) devueltas por la query.")

    ya = leer_staging(cursor)
    cursor.close()
    db_conn.close()

    hoy = datetime.now()
    conteo = {}
    montos = {}
    for row in rows:
        clave = clasificar(row, ya, hoy, m.GRACE_DAYS)
        conteo[clave] = conteo.get(clave, 0) + 1
        montos[clave] = montos.get(clave, 0.0) + a_float(row['cfdi_total'])

    log.info('')
    log.info('=' * 78)
    log.info('LOTE DISPONIBLE')
    log.info('=' * 78)
    log.info(f"{'TIPO':<6}{'SITUACION':<34}{'ORDENES':>9}{'IMPORTE':>18}")
    log.info('-' * 78)
    for clave in sorted(conteo):
        log.info(f"{clave[0]:<6}{clave[1]:<34}{conteo[clave]:>9}"
                 f"{montos[clave]:>18,.2f}")
    log.info('-' * 78)

    eleg_b2b = conteo.get(('B2B', 'NUEVA_ELEGIBLE'), 0)
    eleg_b2c = conteo.get(('B2C', 'NUEVA_ELEGIBLE'), 0)
    log.info(f"  En la tabla de control -> B2B: {eleg_b2b} | B2C: {eleg_b2c}")
    log.info('=' * 78)

    # --- Descuento de las que ya tienen factura viva en Odoo ---
    reales_b2b, reales_b2c = eleg_b2b, eleg_b2c
    if CONSULTAR_ODOO:
        log.info('')
        log.info('Consultando Odoo para descontar las que ya estan facturadas...')
        nuevos = [r for r in rows
                  if clasificar(r, ya, hoy, m.GRACE_DAYS)[1] == 'NUEVA_ELEGIBLE'
                  and r['odoo_so_name']]
        nombres = sorted({r['odoo_so_name'] for r in nuevos})
        vivas = facturas_vivas_en_odoo(nombres)
        reales_b2b = sum(1 for r in nuevos
                         if r['amz_cfdi_uuid'] and r['odoo_so_name'] not in vivas)
        reales_b2c = sum(1 for r in nuevos
                         if not r['amz_cfdi_uuid'] and r['odoo_so_name'] not in vivas)
        log.info('')
        log.info('=' * 78)
        log.info(f"  Ya facturadas en Odoo   : {len(vivas)} de {len(nombres)} SO")
        log.info(f"  FACTURABLES DE VERDAD   -> B2B: {reales_b2b} | "
                 f"B2C: {reales_b2c}")
        log.info('=' * 78)
    else:
        log.warning('CONTEO_SIN_ODOO activo: los elegibles de arriba NO '
                    'descuentan las ordenes que ya tienen factura en Odoo, '
                    'asi que estan sobreestimados.')

    # El veredicto: alcanza o no alcanza para el lote de prueba pedido.
    for tipo, disp, cuota in (('B2B', reales_b2b, m.QUOTA_B2B),
                              ('B2C', reales_b2c, m.QUOTA_B2C)):
        if cuota is None:
            continue
        if disp < cuota:
            log.warning(
                f"{tipo}: solo hay {disp} facturables y la cuota pide {cuota}. "
                f"Opciones: bajar la cuota, ampliar la ventana por abajo "
                f"(AMZ_START_DATE) o (solo B2C) reducir AMZ_GRACE_DAYS.")
        else:
            necesarios = cuota * m.MINER_OVERFETCH
            log.info(f"{tipo}: {disp} facturables cubren la cuota de {cuota}. OK")
            if disp < necesarios:
                log.info(f"  (el miner sembrara hasta {necesarios} candidatos "
                         f"con AMZ_MINER_OVERFETCH={m.MINER_OVERFETCH}; hay "
                         f"{disp} facturables, suficiente para la cuota)")

    gracia = conteo.get(('B2C', 'NUEVA_GRACIA'), 0)
    if gracia:
        log.info(f"Nota: {gracia} orden(es) B2C estan dentro del periodo de "
                 f"gracia de {m.GRACE_DAYS} dias. El miner las registra como "
                 f"SKIPPED_GRACE y NO consumen cuota.")


if __name__ == '__main__':
    banner('CONTEO DEL LOTE ELEGIBLE -- AMAZON (solo lectura)')
    try:
        if CONSULTAR_ODOO:
            validar_entorno()
        run()
    except Exception as e:
        log.error(f'Fallo el conteo: {e}')
        sys.exit(1)
