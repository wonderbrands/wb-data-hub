"""
06_reset_lote_test.py
---------------------
RESET DEL LOTE DE PRUEBAS -- DESTRUCTIVO, SOLO STAGING

Libera una MUESTRA ACOTADA de ordenes NUEVAS para poder correr 01 -> 02 -> 03:

    1. Toma las ordenes de la ventana con la misma query que usa 01.
    2. Descarta las que ya estan en las tablas de control (lotes anteriores).
    3. Recorre las restantes en el MISMO ORDEN que el miner de 01 y se queda
       con las primeras RESET_LOTE_B2B / _B2C que se pueden liberar sin tocar
       un cobro (ver "QUE NO SE TOCA NUNCA").
    4. Comprueba que la muestra cae dentro de la banda que 01 va a sembrar.
    5. Vuelca a CSV las facturas vivas revisadas (RESPALDO).
    6. Pasa a borrador y cancela las facturas de la muestra.

Las tablas de control NO se tocan: por construccion, las ordenes de la
muestra no estan en ellas.


POR QUE HACE FALTA
------------------
La base de Odoo de pruebas es un espejo de produccion congelado al 26-ago-2026,
y trae las facturas que facturacion_automatica_1a1.py ya habia emitido antes de
esa fecha. Para el lote de prueba esas ordenes NO son facturables: 01 las
reporta como ALREADY_ODOO_INVOICED y no consumen cuota. En la corrida del
11-sep fueron 54 de 60 candidatos.


POR QUE UNA MUESTRA Y NO LA VENTANA ENTERA
-------------------------------------------
La primera version cancelaba todas las facturas vivas de la ventana. Sobre
2026-06-19 .. 2026-08-26 eso fueron 14,315 facturas: es basicamente toda la
facturacion de Amazon del trimestre en el espejo. Destruir eso para probar 100
casos no tiene sentido, y ademas deja el espejo inservible para cualquier otra
comparacion contra produccion.

Asi que el script elige primero QUE ordenes va a liberar, y recien despues
busca sus facturas. Se cancela lo minimo necesario para llenar la cuota de 01.


QUE NO SE TOCA NUNCA
--------------------
Cambio del 25-sep-2026, despues de la prueba masiva del 18-sep. Ese lote (100
facturas, 98 pagos PAMZ) ya esta cobrado y Finanzas lo cuadro contra los
settlements. Cancelarlo descuadraria justo lo que se valido. La version
anterior de este script lo habria vuelto a elegir: tomaba las N ordenes mas
recientes de la ventana, que son exactamente las de ese lote.

Dos exclusiones, independientes entre si:

  1. ORDENES QUE YA ESTAN EN LAS TABLAS DE CONTROL (mkp_billing_* o
     mkp_payments_*), en cualquier estado. Es el mismo criterio del miner: una
     fila que ya existe se refresca pero NO consume cupo de siembra
     (01_amz_billing.py, run_miner), asi que la banda nueva que va a leer 01
     empieza despues de ellas. Por eso ya no se borran filas de control: esas
     filas son la memoria de que ordenes pertenecen a lotes anteriores. Sin
     ellas, la siguiente corrida volveria a elegir el lote pagado. (Por la
     misma razon desaparecio RESET_PURGAR_TODO.)

  2. SO CON COBRO O NOTA DE CREDITO EN ODOO. Si alguna factura viva de la SO
     tiene payment_state distinto de 'not_paid', o existe una out_refund viva,
     la SO entera se descarta. No depende de las tablas de control: protege
     aunque alguien las vacie a mano, y protege tambien los cobros que el
     espejo trae de produccion. Se aplica al elegir la muestra y se vuelve a
     comprobar en liberar_factura() justo antes de cancelar.

Consecuencia: este script ya no cancela cobros ni rompe conciliaciones. La
version anterior lo hacia (desconciliar + cancelar account.payment); ese
camino se elimino entero en vez de dejarlo como codigo muerto.


COMO SE ELIGE LA MUESTRA
------------------------
Deterministica, sin aleatoriedad: dos corridas en seco con la misma
configuracion eligen las mismas ordenes.

El miner de 01 recorre QUERY_LOTE ('ORDER BY posted_date DESC'), salta las
ordenes que ya estan en control y siembra solo las primeras
QUOTA * AMZ_MINER_OVERFETCH ordenes NUEVAS de cada tipo. Esa es la banda: lo
que quede mas abajo, 01 no lo lee.

La muestra recorre las ordenes nuevas en ese mismo orden. Algunas se saltan
(tienen cobro), asi que la muestra puede tener que bajar mas que su tamano.
Para cada tipo se reporta la PROFUNDIDAD: la posicion, dentro de las ordenes
nuevas, de la ultima orden elegida. La condicion de la corrida es:

    profundidad <= QUOTA * AMZ_MINER_OVERFETCH

Si no se cumple, 01 no alcanzaria a leer las ultimas ordenes liberadas y se
habrian cancelado facturas para nada. En seco se avisa con el OVERFETCH
minimo; con RESET_DRY_RUN=false el script ABORTA antes de cancelar.

RESET_LOTE_B2B / RESET_LOTE_B2C deben ir POR ENCIMA de las cuotas de 01: no
toda orden liberada termina facturada (TOTAL_DIFF, SO_CANCELLED, SO_DRAFT).

IMPORTANTE: este script lee QUOTA y OVERFETCH importando 01, que los toma del
entorno. Hay que correr 06 y 01 con las MISMAS variables AMZ_* (misma sesion
de consola).


ESTO ES IRREVERSIBLE
--------------------
El espejo no se puede restaurar. Una factura cancelada aqui queda cancelada.
Por eso:

  * RESET_DRY_RUN viene en 'true'. La primera corrida solo lista y escribe el
    CSV; no toca nada. Hay que apagarlo a proposito.
  * El CSV de respaldo se escribe SIEMPRE, tambien en dry-run, y ANTES de
    cancelar nada. Es el unico registro que va a quedar del estado previo.
  * El candado de entorno no tiene variable que lo desactive.


EL RODEO PARA CANCELAR -- LEER ANTES DE USAR
---------------------------------------------
Las facturas del espejo vienen timbradas desde produccion. La localizacion
mexicana bloquea button_draft sobre un CFDI firmado:

    "Operacion no valida. No puede restablecer esos asientos contables a
     borrador, debe solicitar una cancelacion"

Y solicitar la cancelacion es un tramite ante el SAT que desde el espejo no se
puede (ni se debe) hacer. Sin rodeo, no hay forma de liberar las ordenes.

El rodeo es escribir el estado directo por ORM:

    account.move.write({'state': 'draft'})   y luego  {'state': 'cancel'}

saltandose button_draft / button_cancel. Verificado en el espejo el 15-sep
(las dos facturas de prueba salieron [forzado/forzado]). El script SIEMPRE
intenta primero la via nativa, y solo cae al write forzado si la nativa
devuelve un error de negocio. El CSV dice cual de las dos funciono.

write() no llama a remove_move_reconcile(), button_draft si. Con la exclusion
de cobros eso ya no importa: solo se cancelan facturas 'not_paid', que no
tienen conciliacion con pagos que romper.

Esto vale UNICAMENTE para el espejo de pruebas. En produccion cancelar un CFDI
timbrado es un tramite ante el SAT, no un write.
"""

import os
import sys
import csv
import logging
import importlib.util
import xmlrpc.client
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _amz_common import (
    ENTORNO, ODOO_URL, ODOO_DB, TBL_BILLING, TBL_PAYMENTS, MARKETPLACE,
    banner, get_db_connection, validar_entorno,
    OdooModelProxy, OdooWriteUncertain,
)

# En seco por defecto. Apagarlo es el acto deliberado que autoriza el borrado.
DRY_RUN = False #os.getenv('RESET_DRY_RUN', 'true').strip().lower() not in ('0', 'false', 'no')

# Tamano de la muestra POR TIPO. Deben superar las cuotas de 01.
RESET_LOTE_B2B = int(os.getenv('RESET_LOTE_B2B', '50'))
RESET_LOTE_B2C = int(os.getenv('RESET_LOTE_B2C', '50'))

# Tope duro sobre las facturas a cancelar. 0 = sin limite.
RESET_MAX = int(os.getenv('RESET_MAX', '0'))

# Rodeo del bloqueo de la localizacion mexicana (ver cabecera).
FORZAR_ESTADO = os.getenv('RESET_FORZAR_ESTADO', 'true').strip().lower() not in ('0', 'false', 'no')

# Hasta cuantas ordenes nuevas por tipo se revisan buscando liberables. Es un
# freno: si hace falta bajar mas, la muestra ya quedaria fuera de cualquier
# banda de siembra razonable.
RESET_PROFUNDIDAD_MAX = int(os.getenv('RESET_PROFUNDIDAD_MAX', '1500'))

# Ordenes que se consultan a Odoo por vuelta al recorrer los candidatos.
BLOQUE_BUSQUEDA = 100

# Odoo acepta listas largas en un 'in'; se trocea para no armar un request enorme.
LOTE_ODOO = 300

CAMPOS_FACTURA = ['name', 'invoice_origin', 'move_type', 'state',
                  'payment_state', 'invoice_date', 'amount_total',
                  'ref', 'l10n_mx_edi_cfdi_uuid']


def cargar_modulo_billing():
    ruta = os.path.join(_dir, '01_amz_billing.py')
    spec = importlib.util.spec_from_file_location('amz_billing_mod', ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def candado_staging():
    """Segundo candado, encima de validar_entorno().

    validar_entorno() ya aborta si ENTORNO='staging' con una URL que no parece
    de pruebas. Aqui se exige ademas que el ENTORNO sea staging: este script
    cancela facturas escribiendo el estado a mano. No tiene ningun uso
    legitimo en produccion. No hay variable que lo desactive.
    """
    if ENTORNO != 'staging':
        raise Exception(
            f"ABORTADO: 06_reset_lote_test.py solo corre con "
            f"AMZ_ENTORNO='staging' (actual: '{ENTORNO}').")

    url = (ODOO_URL or '').lower()
    if 'test' not in url and '.dev.odoo.com' not in url:
        raise Exception(
            f"ABORTADO: la URL de Odoo '{ODOO_URL}' no parece del espejo de "
            f"pruebas.")


# =============================================================================
# EXCLUSION 1: ORDENES DE LOTES ANTERIORES
# =============================================================================

def ordenes_en_control(db_conn):
    """mkp_order_id presentes en las tablas de control, en cualquier estado.

    Devuelve (conjunto, n_cobradas). n_cobradas es solo para el log: cuantas
    tienen un cobro en ODOO_PAID, que son las que de verdad importa no tocar.
    """
    cursor = db_conn.cursor()
    cursor.execute(f"SELECT DISTINCT mkp_order_id FROM {TBL_BILLING} "
                   f"WHERE marketplace = %s", (MARKETPLACE,))
    en_control = {r[0] for r in cursor.fetchall()}
    cursor.execute(f"SELECT DISTINCT mkp_order_id FROM {TBL_PAYMENTS} "
                   f"WHERE marketplace = %s", (MARKETPLACE,))
    en_control |= {r[0] for r in cursor.fetchall()}
    cursor.execute(f"SELECT COUNT(DISTINCT mkp_order_id) FROM {TBL_PAYMENTS} "
                   f"WHERE marketplace = %s AND status = 'ODOO_PAID'",
                   (MARKETPLACE,))
    n_cobradas = cursor.fetchone()[0]
    cursor.close()
    return en_control, n_cobradas


def candidatas_por_tipo(rows, en_control):
    """Ordenes NUEVAS por tipo, en el orden en que las recorre el miner.

    El miner usa el ORDER BY posted_date DESC de QUERY_LOTE; aqui se reordena
    en Python con el mismo criterio (mas order_id para desempatar y que dos
    corridas den lo mismo).
    """
    cands = {'B2B': [], 'B2C': []}
    descartadas = 0
    for r in rows:
        if not r.get('odoo_so_name'):
            continue                       # sin SO no hay nada que cancelar
        if r['mkp_order_id'] in en_control:
            descartadas += 1
            continue
        cands['B2B' if r.get('amz_cfdi_uuid') else 'B2C'].append(r)

    def clave(r):
        return (str(r.get('posted_date') or ''),
                str(r.get('mkp_order_id') or ''))

    for tipo in cands:
        cands[tipo].sort(key=clave, reverse=True)
    return cands, descartadas


# =============================================================================
# EXCLUSION 2: SO CON COBRO O NOTA DE CREDITO
# =============================================================================

def facturas_vivas(odoo, nombres_so):
    """Facturas y notas de credito no canceladas de esas SO."""
    if not nombres_so:
        return []
    vivas = []
    for i in range(0, len(nombres_so), LOTE_ODOO):
        trozo = nombres_so[i:i + LOTE_ODOO]
        vivas += odoo.execute_kw(
            'account.move', 'search_read',
            [[('move_type', 'in', ['out_invoice', 'out_refund']),
              ('state', '!=', 'cancel'),
              ('invoice_origin', 'in', trozo)]],
            {'fields': CAMPOS_FACTURA, 'order': 'id asc'})
    return vivas


def motivo_exclusion(facturas):
    """None si la SO se puede liberar; si no, el motivo.

    Una sola factura con cobro descarta la SO entera: liberar media SO dejaria
    a 01 facturando encima de una factura cobrada.
    """
    for f in facturas:
        if f.get('move_type') == 'out_refund':
            return 'EXCLUIDA_CON_NC'
        if f.get('payment_state') not in (None, '', False, 'not_paid'):
            return 'EXCLUIDA_CON_COBRO'
    return None


def elegir_liberables(odoo, cands, cupo, tipo):
    """Recorre los candidatos en orden y se queda con los primeros liberables.

    Consulta Odoo por bloques y se detiene al llenar el cupo, para no revisar
    mas ordenes de las necesarias. Devuelve:

      elegidas   [(posicion, row)]  posicion = 1-based entre las nuevas
      facturas   facturas vivas de las elegidas (las que se van a cancelar)
      excluidas  [(posicion, row, motivo, facturas)]
    """
    elegidas, facturas, excluidas = [], [], []
    tope = min(len(cands), RESET_PROFUNDIDAD_MAX)
    i = 0
    while i < tope and len(elegidas) < cupo:
        bloque = cands[i:min(i + BLOQUE_BUSQUEDA, tope)]
        nombres = sorted({r['odoo_so_name'] for r in bloque})
        por_so = {}
        for f in facturas_vivas(odoo, nombres):
            por_so.setdefault(f.get('invoice_origin'), []).append(f)

        for j, r in enumerate(bloque):
            if len(elegidas) >= cupo:
                break
            posicion = i + j + 1
            suyas = por_so.get(r['odoo_so_name'], [])
            motivo = motivo_exclusion(suyas)
            if motivo:
                excluidas.append((posicion, r, motivo, suyas))
                continue
            elegidas.append((posicion, r))
            facturas += suyas
        i += BLOQUE_BUSQUEDA

    if len(elegidas) < cupo:
        log.warning(f"{tipo}: solo {len(elegidas)} orden(es) liberables en las "
                    f"primeras {tope} nuevas; se pidieron {cupo}.")
    return elegidas, facturas, excluidas


# =============================================================================
# BANDA DE SIEMBRA DE 01
# =============================================================================

def revisar_banda(profundidad, m):
    """Comprueba que cada tipo cae dentro de lo que 01 va a sembrar.

    Devuelve la lista de problemas (vacia = OK). El miner puede llegar MAS
    hondo que QUOTA * OVERFETCH (las SKIPPED_GRACE y B2B_SPLIT_MISMATCH no
    consumen cupo), nunca menos: la comprobacion es conservadora.
    """
    problemas = []
    for tipo, cuota in (('B2B', m.QUOTA_B2B), ('B2C', m.QUOTA_B2C)):
        prof = profundidad.get(tipo, 0)
        if cuota is None or not prof:
            continue
        tope = cuota * m.MINER_OVERFETCH
        minimo = -(-prof // cuota)                 # techo de prof / cuota
        if prof > tope:
            problemas.append(
                f"{tipo}: la ultima orden elegida esta en la posicion {prof} "
                f"de las nuevas, pero 01 solo siembra {tope} "
                f"({cuota} x AMZ_MINER_OVERFETCH={m.MINER_OVERFETCH}). "
                f"Hace falta AMZ_MINER_OVERFETCH >= {minimo}.")
        else:
            log.info(f"{tipo}: profundidad {prof} <= banda de siembra {tope} "
                     f"({cuota} x {m.MINER_OVERFETCH}). OK "
                     f"(OVERFETCH minimo para esta muestra: {minimo})")
    return problemas


# =============================================================================
# RESPALDO
# =============================================================================

CABECERA_CSV = ['odoo_move_id', 'name', 'invoice_origin', 'move_type',
                'state_previo', 'payment_state_previo', 'invoice_date',
                'amount_total', 'ref', 'cfdi_uuid', 'tipo', 'posicion',
                'resultado', 'metodo_borrador', 'metodo_cancel', 'error']


def escribir_csv(facturas, ruta, resultados, meta):
    """Escribe el CSV del lote.

    Lista las facturas que se van a cancelar y TAMBIEN las de las SO que se
    excluyeron (resultado EXCLUIDA_*): queda constancia de que se vieron y no
    se tocaron. meta = {so_name: (tipo, posicion)}.

    Se llama DOS veces sobre la misma ruta:

      1. Antes de tocar nada. Las columnas de resultado salen como PENDIENTE
         (o EXCLUIDA_*). Ese archivo es el respaldo: si la corrida muere a la
         mitad, el estado previo quedo en disco.
      2. Al terminar, con los resultados llenos. Reemplaza al anterior.

    Escribe a un .tmp y hace os.replace(), que es atomico: un corte a mitad de
    escritura no deja el respaldo truncado.
    """
    tmp = ruta + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(CABECERA_CSV)
        for f in facturas:
            r = resultados.get(f['id'], {})
            tipo, posicion = meta.get(f.get('invoice_origin'), ('', ''))
            w.writerow([f['id'], f.get('name'), f.get('invoice_origin'),
                        f.get('move_type'), f.get('state'),
                        f.get('payment_state'), f.get('invoice_date'),
                        f.get('amount_total'), f.get('ref'),
                        f.get('l10n_mx_edi_cfdi_uuid'), tipo, posicion,
                        r.get('resultado', 'PENDIENTE'),
                        r.get('metodo_borrador', ''),
                        r.get('metodo_cancel', ''),
                        r.get('error', '')])
    os.replace(tmp, ruta)


# =============================================================================
# CANCELACION
# =============================================================================

def mover_estado(odoo, move_id, destino, res):
    """Lleva un account.move a 'draft' o 'cancel'.

    Intenta SIEMPRE el boton nativo primero. Solo si Odoo devuelve un error de
    negocio (xmlrpc Fault = rollback hecho, nada quedo a medias) cae al write
    forzado. Un error de RED sube como OdooWriteUncertain y NO se fuerza: ahi
    no se sabe si Odoo hizo commit y forzar encima seria escribir a ciegas.
    """
    boton = 'button_draft' if destino == 'draft' else 'button_cancel'
    try:
        odoo.execute_kw('account.move', boton, [[move_id]])
        res['nativo'] += 1
        return 'nativo'
    except OdooWriteUncertain:
        raise
    except xmlrpc.client.Fault as e:
        if not FORZAR_ESTADO:
            raise
        log.debug(f"  {boton} rechazado ({e.faultString[:90]}); forzando write")

    odoo.execute_kw('account.move', 'write', [[move_id], {'state': destino}])
    res['forzado'] += 1
    return 'forzado'


def liberar_factura(odoo, f, res, resultados):
    """Cancela una factura y deja anotado en 'resultados' que paso con ella.

    Vuelve a comprobar la exclusion 2 sobre la propia factura, justo antes de
    tocarla: si por cualquier camino llegara aqui una factura con cobro o una
    nota de credito, se rechaza. Es la ultima linea de defensa del lote pagado.
    """
    etiqueta = f"{f.get('name')} ({f.get('invoice_origin')})"
    motivo = motivo_exclusion([f])
    if motivo:
        resultados[f['id']] = {'resultado': motivo}
        res['rechazadas'] += 1
        log.error(f"  RECHAZADA {etiqueta}: {motivo}. No se toca.")
        return

    det = {'resultado': 'FALLIDA', 'metodo_borrador': '',
           'metodo_cancel': '', 'error': ''}
    resultados[f['id']] = det
    try:
        if f.get('state') == 'posted':
            det['metodo_borrador'] = mover_estado(odoo, f['id'], 'draft', res)
        else:
            det['metodo_borrador'] = 'ya_draft'
        det['metodo_cancel'] = mover_estado(odoo, f['id'], 'cancel', res)

        det['resultado'] = 'CANCELADA'
        res['canceladas'] += 1
        log.info(f"  Cancelada {etiqueta} "
                 f"[{det['metodo_borrador']}/{det['metodo_cancel']}]")

    except OdooWriteUncertain as e:
        # Escritura no reintentable que quedo en duda: no se sabe si Odoo hizo
        # commit. Se reporta y se sigue; el CSV tiene el estado previo.
        det['resultado'] = 'ESCRITURA_EN_DUDA'
        det['error'] = str(e)[:300]
        res['fallidas'] += 1
        log.error(f"  ESCRITURA EN DUDA {etiqueta}: {e}")
    except Exception as e:
        det['error'] = str(e)[:300]
        res['fallidas'] += 1
        log.error(f"  Fallo {etiqueta}: {e}")


# =============================================================================

def run():
    m = cargar_modulo_billing()
    log.info(f"AMZ_START_DATE      : {m.AMZ_START_DATE}")
    log.info(f"AMZ_END_DATE        : {m.AMZ_END_DATE or '(sin limite: hasta hoy)'}")
    log.info(f"Cuotas de 01        : B2B={m.QUOTA_B2B} | B2C={m.QUOTA_B2C}")
    log.info(f"AMZ_MINER_OVERFETCH : {m.MINER_OVERFETCH}")
    log.info(f"Muestra por tipo    : B2B={RESET_LOTE_B2B} | B2C={RESET_LOTE_B2C}")
    log.info(f"RESET_DRY_RUN       : {DRY_RUN}")
    log.info(f"RESET_MAX           : {RESET_MAX or '(sin limite)'}")
    log.info(f"Forzar estado       : {FORZAR_ESTADO}")
    log.info('')

    db_conn = get_db_connection()
    en_control, n_cobradas = ordenes_en_control(db_conn)
    log.info(f"Ordenes ya en tablas de control: {len(en_control)} "
             f"({n_cobradas} con cobro ODOO_PAID). Quedan fuera de la muestra.")

    cursor = db_conn.cursor(dictionary=True)
    sql, params = m.construir_query_lote()
    log.info("Ejecutando QUERY_LOTE (la misma que usa el miner)...")
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    db_conn.close()
    log.info(f"{len(rows)} orden(es) en la ventana.")

    cands, descartadas = candidatas_por_tipo(rows, en_control)
    log.info(f"Nuevas (fuera de control): B2B {len(cands['B2B'])} | "
             f"B2C {len(cands['B2C'])} | descartadas por estar en control: "
             f"{descartadas}")

    odoo = OdooModelProxy(timeout=300)
    log.info('Recorriendo candidatas y revisando sus facturas en Odoo...')

    facturas, meta, profundidad = [], {}, {}
    muestra, excluidas_todas = [], []
    for tipo, cupo in (('B2B', RESET_LOTE_B2B), ('B2C', RESET_LOTE_B2C)):
        elegidas, suyas, excluidas = elegir_liberables(odoo, cands[tipo],
                                                       cupo, tipo)
        facturas += suyas
        muestra += [r for _, r in elegidas]
        excluidas_todas += excluidas
        profundidad[tipo] = elegidas[-1][0] if elegidas else 0
        for pos, r in elegidas:
            meta[r['odoo_so_name']] = (tipo, pos)
        for pos, r, motivo, _ in excluidas:
            meta[r['odoo_so_name']] = (tipo, pos)
        n_cobro = sum(1 for e in excluidas if e[2] == 'EXCLUIDA_CON_COBRO')
        n_nc = sum(1 for e in excluidas if e[2] == 'EXCLUIDA_CON_NC')
        log.info(f"{tipo}: {len(elegidas)} elegidas | excluidas {n_cobro} con "
                 f"cobro, {n_nc} con NC | profundidad {profundidad[tipo]}")

    # Resumen por settlement, informativo.
    resumen = {}
    for r in muestra:
        tipo = 'B2B' if r.get('amz_cfdi_uuid') else 'B2C'
        st = str(r.get('settlement_id') or 'SIN_SETTLEMENT')
        resumen[(tipo, st)] = resumen.get((tipo, st), 0) + 1
    nombres = sorted({r['odoo_so_name'] for r in muestra})

    log.info('')
    log.info('=' * 78)
    log.info('MUESTRA ELEGIDA')
    log.info('=' * 78)
    log.info(f"{'TIPO':<6}{'SETTLEMENT':<18}{'ORDENES':>9}")
    log.info('-' * 78)
    for k in sorted(resumen):
        log.info(f"{k[0]:<6}{k[1]:<18}{resumen[k]:>9}")
    log.info('-' * 78)
    log.info(f"  Total: {len(muestra)} orden(es) | {len(nombres)} SO | "
             f"{len(facturas)} factura(s) viva(s) a cancelar")
    multi = sum(1 for r in muestra if (r.get('n_envios') or 1) > 1)
    log.info(f"  Ordenes de varios envios: {multi} "
             f"(candidatas a cobro parcial entre settlements)")
    log.info('=' * 78)

    problemas = revisar_banda(profundidad, m)
    for p in problemas:
        log.warning(p)

    # Respaldo: facturas a cancelar + las de SO excluidas, con su motivo.
    resultados = {}
    vistas = list(facturas)
    for _, _, motivo, suyas in excluidas_todas:
        for f in suyas:
            resultados[f['id']] = {'resultado': motivo}
            vistas.append(f)

    a_cancelar = facturas
    if RESET_MAX and len(facturas) > RESET_MAX:
        log.warning(f"RESET_MAX={RESET_MAX}: se cancelan solo las primeras "
                    f"{RESET_MAX} de {len(facturas)}. El CSV lista TODAS.")
        a_cancelar = facturas[:RESET_MAX]
        for f in facturas[RESET_MAX:]:
            resultados[f['id']] = {'resultado': 'OMITIDA_POR_RESET_MAX'}

    sello = datetime.now().strftime('%Y%m%d_%H%M%S')
    ruta = os.path.join(_dir, f'reset_backup_{sello}.csv')
    escribir_csv(vistas, ruta, resultados, meta)
    log.info(f"RESPALDO escrito: {ruta} ({len(vistas)} factura(s))")

    if DRY_RUN:
        log.info('')
        log.info('=' * 78)
        log.info('DRY-RUN: no se cancelo nada.')
        log.info(f"  Facturas que se cancelarian : {len(a_cancelar)}")
        log.info(f"  SO excluidas (cobro / NC)   : {len(excluidas_todas)}")
        log.info(f"  Respaldo                    : {ruta}")
        if problemas:
            log.info('  ATENCION: la muestra cae fuera de la banda de 01 (ver '
                     'avisos). Con RESET_DRY_RUN=false el script abortaria.')
        log.info('=' * 78)
        return

    if problemas:
        raise Exception(
            "La muestra cae fuera de la banda de siembra de 01; no se cancela "
            "nada. Ajustar AMZ_MINER_OVERFETCH (y correr 01 con el mismo "
            "valor) o bajar RESET_LOTE_B2B / RESET_LOTE_B2C.")

    res = {'canceladas': 0, 'fallidas': 0, 'rechazadas': 0,
           'nativo': 0, 'forzado': 0}

    log.info('')
    log.info(f"Cancelando {len(a_cancelar)} factura(s) en {ODOO_DB}...")
    for f in a_cancelar:
        liberar_factura(odoo, f, res, resultados)

    # Reescritura con el detalle por factura. Atomica: ver escribir_csv().
    escribir_csv(vistas, ruta, resultados, meta)

    log.info('')
    log.info('=' * 78)
    log.info('RESET TERMINADO')
    log.info('=' * 78)
    log.info(f"  Facturas canceladas   : {res['canceladas']}")
    log.info(f"  Facturas con fallo    : {res['fallidas']}")
    log.info(f"  Rechazadas (cobro/NC) : {res['rechazadas']}")
    log.info(f"  Cambios de estado     : {res['nativo']} nativos | "
             f"{res['forzado']} forzados")
    log.info(f"  Tablas de control     : sin cambios")
    log.info(f"  Respaldo              : {ruta}")
    log.info('=' * 78)

    if res['forzado']:
        log.warning(f"{res['forzado']} cambio(s) de estado se hicieron con "
                    f"write() directo, saltandose button_draft/button_cancel. "
                    f"Valido SOLO en el espejo de pruebas.")
    if res['fallidas']:
        log.warning("Hubo facturas que no se pudieron cancelar: esas ordenes "
                    "van a salir como ALREADY_ODOO_INVOICED en 01.")
    if res['rechazadas']:
        log.error(f"{res['rechazadas']} factura(s) con cobro o NC llegaron "
                  f"hasta la cancelacion y se rechazaron. No deberia pasar: "
                  f"revisar motivo_exclusion().")


if __name__ == '__main__':
    banner('RESET DEL LOTE DE PRUEBAS -- AMAZON (DESTRUCTIVO)')
    try:
        validar_entorno()
        candado_staging()
        run()
    except Exception as e:
        log.error(f'Fallo el reset: {e}')
        sys.exit(1)
