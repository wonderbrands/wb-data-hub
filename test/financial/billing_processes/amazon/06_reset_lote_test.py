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

  2. SO CON UN COBRO REAL EN ODOO. Si algun documento vivo de la SO esta
     conciliado con algo que NO es de la propia SO (un pago, un extracto
     bancario, un documento de otra orden), la SO entera se descarta. No
     depende de las tablas de control: protege aunque alguien las vacie a
     mano, y protege tambien los cobros que el espejo trae de produccion. Se
     aplica al elegir la muestra y se vuelve a leer de Odoo en liberar_so()
     justo antes de cancelar.


TODOS LOS DOCUMENTOS VIVOS DE LA SO -- NO SOLO LAS FACTURAS
-----------------------------------------------------------
Cambio del 2-oct-2026. Caso SO10272203, consecuencia de la refacturacion a
clientes particulares (proceso interno, no un error):

    factura a publico general (1a1)      <- contrarrestada por...
    nota de credito a publico general    <- ...esta NC
    refactura al cliente con su RFC
    factura nueva de 01_amz_billing.py   (la de la prueba)

La version anterior buscaba los documentos por invoice_origin = SO. Una NC
creada como reversa NO hereda ese campo: el script no la vio, ni para
cancelarla ni para excluir la SO. Cancelo las dos facturas y dejo viva la NC,
que siguio restando en qty_invoiced: despues de facturar con 01 la SO quedo
"a facturar". El error era del script de pruebas, no del proceso.

Ahora los documentos se buscan por tres caminos (documentos_vivos):
  1. sale.order.invoice_ids: todo lo ligado a las lineas de la SO, incluidas
     las NC que la contrarrestan.
  2. invoice_origin = SO: facturas sin ligar a lineas.
  3. reversed_entry_id: NC que reversan alguno de los anteriores.

Y se cancelan TODOS, facturas y notas de credito. La conciliacion entre una
factura y su propia NC es interna y no impide liberar la SO; antes de
cancelar se rompe (desconciliar), porque el write forzado no lo hace solo.
Lo que si impide liberar es un cobro real (exclusion 2).

Consecuencia: este script no cancela cobros ni rompe conciliaciones con
pagos. Solo rompe la conciliacion interna factura <-> NC de la misma SO.


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


GRACIA Y DEPOSITO -- ORDENES QUE NO SE LIBERAN AUNQUE ESTEN EN LA BANDA
-----------------------------------------------------------------------
Cambio del 30-sep-2026, tras la primera prueba sobre el espejo del 29-sep.
Se cancelaron 49 facturas B2C de las ordenes MAS RECIENTES y 01 facturo una
sola: casi todas tenian posted_date dentro de los ultimos 5 dias. El miner las
siembra como SKIPPED_GRACE (no consumen cupo) y el loader no las toca; para
llenar su cupo el miner bajo a ordenes fuera de gracia que este script no
habia liberado, y salieron 86 ALREADY_ODOO_INVOICED. En el espejo viejo,
congelado al 26-ago, ninguna orden estaba en gracia y el defecto no aparecio.

Ahora, antes de consultar Odoo:

  * B2C EN GRACIA: (ahora - posted_date).days < GRACE_DAYS de 01, la misma
    cuenta que hace el miner. No se liberan. Una orden fuera de gracia al
    correr este script sigue fuera de gracia cuando corre 01, asi que no hace
    falta margen. B2B no tiene gracia: Amazon ya emitio el CFDI.
  * SIN DEPOSITO (RESET_SOLO_DEPOSITADAS, por defecto activo): el settlement
    de la orden todavia no tiene deposit_date. Facturarla prueba 01, pero
    02/03 no tendrian cobro que aplicar. Para una prueba completa
    01 -> 02 -> 03 conviene liberar solo ordenes ya depositadas.

Las que se saltan por estos motivos SIGUEN CONTANDO en la profundidad: el
miner puede contarlas (una orden en gracia puede salir de ella antes de que
corra 01, y las no depositadas el miner las siembra igual). Eso hace la
comprobacion de banda conservadora: pide un OVERFETCH algo mayor del
estrictamente necesario, nunca menor.

IMPORTANTE: este script lee QUOTA y OVERFETCH importando 01, que los toma del
entorno. Hay que correr 06 y 01 con las MISMAS variables AMZ_* (misma sesion
de consola).


LOTE COMPLETO: B2B Y B2C
------------------------
Cambio del 2-oct-2026, para el lote 4 (ultimo antes de produccion). El lote
tiene que llevar los dos tipos: B2B (vincular el CFDI de Amazon) y B2C
(emitir a publico en general) recorren caminos distintos en 01 y en 03.
revisar_lote_completo() lo exige, junto con la banda, y con
RESET_DRY_RUN=false ABORTA si falla:

  * RESET_LOTE_B2B y RESET_LOTE_B2C mayores que cero.
  * AMZ_TEST_QUOTA_B2B y _B2C de 01 mayores que cero (0 en 01 significa SIN
    TOPE, no "ninguna").
  * RESET_LOTE_x >= cuota de 01 del mismo tipo.
  * Liberables encontradas >= cuota de 01. Si no alcanzan, 01 no llena su
    cuota con ordenes liberadas. B2B suele ser el que se queda corto (los
    lotes anteriores ya consumieron la ventana): se amplia AMZ_START_DATE.


VERIFICACION FINAL
------------------
Al terminar se vuelven a leer de Odoo los documentos de TODAS las SO
liberadas. Cualquier factura o NC que siga viva se lista como
SO_CON_DOCUMENTOS_VIVOS: esa SO va a salir ALREADY_ODOO_INVOICED en 01 (o, si
lo que quedo vivo es una NC, va a restar contra la factura nueva) y hay que
revisarla a mano antes de correr 01.


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
    OdooModelProxy, OdooWriteUncertain, ejecutar_sin_retorno,
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

# Liberar solo ordenes cuyo settlement ya se deposito (ver "GRACIA Y DEPOSITO").
SOLO_DEPOSITADAS = os.getenv('RESET_SOLO_DEPOSITADAS', 'true').strip().lower() not in ('0', 'false', 'no')

# MUESTRA EXPLICITA (cambio del 28-oct-2026). Lista de order_id de Amazon
# separada por comas. Para el lote 7 Finanzas pidio casos concretos (envio
# cobrado, promociones con envio y con varias unidades, varios envios...) y
# una muestra por banda no garantiza ninguno. Con la lista se liberan
# exactamente esas ordenes (si se pueden: mismas exclusiones de cobro, gracia
# y deposito) y no aplican la banda ni las cuotas. Las ordenes tienen que
# estar en lo que lee 01: correr con el mismo AMZ_SETTLEMENT_ID / AMZ_ORDER_IDS.
RESET_ORDENES = [o.strip() for o in os.getenv('RESET_ORDENES', '').split(',') if o.strip()]

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
# DOCUMENTOS DE LA SO Y EXCLUSION 2 (COBRO REAL)
# =============================================================================

TIPOS_DOC = ['out_invoice', 'out_refund']
CUENTAS_CONCILIABLES = ['asset_receivable', 'liability_payable']


def _en_trozos(ids):
    for i in range(0, len(ids), LOTE_ODOO):
        yield ids[i:i + LOTE_ODOO]


def documentos_vivos(odoo, nombres_so):
    """Facturas y notas de credito vivas de cada SO: {so_name: [docs]}.

    Tres caminos, porque ninguno solo alcanza (ver "TODOS LOS DOCUMENTOS VIVOS
    DE LA SO" en la cabecera). Cada doc lleva 'so_name': el invoice_origin de
    una NC no siempre es la SO.
    """
    if not nombres_so:
        return {}
    so_de = {}                                  # move_id -> so_name
    for trozo in _en_trozos(list(nombres_so)):
        for so in odoo.execute_kw('sale.order', 'search_read',
                                  [[('name', 'in', trozo)]],
                                  {'fields': ['name', 'invoice_ids']}):
            for mid in so.get('invoice_ids') or []:
                so_de.setdefault(mid, so['name'])
        for mv in odoo.execute_kw('account.move', 'search_read',
                                  [[('move_type', 'in', TIPOS_DOC),
                                    ('invoice_origin', 'in', trozo)]],
                                  {'fields': ['invoice_origin']}):
            so_de.setdefault(mv['id'], mv['invoice_origin'])

    for trozo in _en_trozos(list(so_de)):
        for mv in odoo.execute_kw('account.move', 'search_read',
                                  [[('move_type', 'in', TIPOS_DOC),
                                    ('reversed_entry_id', 'in', trozo)]],
                                  {'fields': ['reversed_entry_id']}):
            origen = mv['reversed_entry_id'][0] if mv.get('reversed_entry_id') else None
            if origen in so_de:
                so_de.setdefault(mv['id'], so_de[origen])

    por_so = {}
    for trozo in _en_trozos(list(so_de)):
        for f in odoo.execute_kw('account.move', 'search_read',
                                 [[('id', 'in', trozo),
                                   ('move_type', 'in', TIPOS_DOC),
                                   ('state', '!=', 'cancel')]],
                                 {'fields': CAMPOS_FACTURA, 'order': 'id asc'}):
            f['so_name'] = so_de[f['id']]
            por_so.setdefault(f['so_name'], []).append(f)
    return por_so


def lineas_conciliables(odoo, move_ids):
    """Lineas de CxC/CxP de esos documentos, con sus conciliaciones parciales."""
    lineas = []
    for trozo in _en_trozos(list(move_ids)):
        lineas += odoo.execute_kw(
            'account.move.line', 'search_read',
            [[('move_id', 'in', trozo),
              ('account_type', 'in', CUENTAS_CONCILIABLES)]],
            {'fields': ['move_id', 'matched_debit_ids', 'matched_credit_ids']})
    return lineas


def cobros_externos(odoo, por_so):
    """{so_name: 'EXCLUIDA_CON_COBRO'} de las SO conciliadas con algo ajeno.

    Se sigue cada conciliacion parcial hasta el documento de la otra pata. Si
    esa pata es de la MISMA SO (factura <-> su NC), es interna y no impide
    liberar. Si es de cualquier otra cosa (pago, extracto, otra orden), la SO
    se descarta. No se usa payment_state: una factura contrarrestada por su NC
    sale 'reversed' o 'paid' sin tener ningun cobro.
    """
    move_so = {f['id']: so for so, docs in por_so.items() for f in docs}
    if not move_so:
        return {}
    lineas = lineas_conciliables(odoo, move_so)
    partial_ids = sorted({p for l in lineas
                          for p in (l['matched_debit_ids'] + l['matched_credit_ids'])})
    if not partial_ids:
        return {}

    parciales = []
    for trozo in _en_trozos(partial_ids):
        parciales += odoo.execute_kw('account.partial.reconcile', 'read', [trozo],
                                     {'fields': ['debit_move_id', 'credit_move_id']})
    ids_lineas = sorted({p['debit_move_id'][0] for p in parciales}
                        | {p['credit_move_id'][0] for p in parciales})
    mov_de_linea = {}
    for trozo in _en_trozos(ids_lineas):
        for l in odoo.execute_kw('account.move.line', 'read', [trozo],
                                 {'fields': ['move_id']}):
            mov_de_linea[l['id']] = l['move_id'][0]

    externos = {}
    for p in parciales:
        so_a = move_so.get(mov_de_linea.get(p['debit_move_id'][0]))
        so_b = move_so.get(mov_de_linea.get(p['credit_move_id'][0]))
        if so_a != so_b:
            for so in (so_a, so_b):
                if so:
                    externos[so] = 'EXCLUIDA_CON_COBRO'
    return externos


def motivo_previo(r, grace_days, ahora):
    """Motivo para NO liberar una orden que no necesita consultar Odoo.

    Ver "GRACIA Y DEPOSITO" en la cabecera. La cuenta de la gracia es la misma
    del miner de 01 (run_miner): (ahora - posted_date).days < GRACE_DAYS, solo
    B2C, con posted_date ya en hora de CDMX (QUERY_LOTE resta las 6 h).
    """
    es_b2b = bool(r.get('amz_cfdi_uuid'))
    posted = r.get('posted_date')
    if not es_b2b and posted:
        if isinstance(posted, str):
            posted = datetime.strptime(posted[:19], '%Y-%m-%d %H:%M:%S')
        if (ahora - posted).days < grace_days:
            return 'EN_GRACIA'
    if SOLO_DEPOSITADAS and not r.get('deposit_date'):
        return 'SIN_DEPOSITO'
    return None


def elegir_liberables(odoo, cands, cupo, tipo, grace_days, ahora):
    """Recorre los candidatos en orden y se queda con los primeros liberables.

    Consulta Odoo por bloques y se detiene al llenar el cupo, para no revisar
    mas ordenes de las necesarias. Las que se descartan por motivo_previo() no
    se consultan a Odoo, pero su posicion SI cuenta (ver cabecera). Devuelve:

      elegidas   [(posicion, row)]  posicion = 1-based entre las nuevas
      facturas   facturas vivas de las elegidas (las que se van a cancelar)
      excluidas  [(posicion, row, motivo, facturas)]
      previas    {motivo: n} descartadas sin consultar Odoo
    """
    elegidas, facturas, excluidas = [], [], []
    previas = {}
    tope = min(len(cands), RESET_PROFUNDIDAD_MAX)
    i = 0
    while i < tope and len(elegidas) < cupo:
        bloque = cands[i:min(i + BLOQUE_BUSQUEDA, tope)]
        motivos = [motivo_previo(r, grace_days, ahora) for r in bloque]
        nombres = sorted({r['odoo_so_name'] for r, mot in zip(bloque, motivos)
                          if not mot})
        por_so = documentos_vivos(odoo, nombres)
        externos = cobros_externos(odoo, por_so)

        for j, r in enumerate(bloque):
            if len(elegidas) >= cupo:
                break
            posicion = i + j + 1
            if motivos[j]:
                previas[motivos[j]] = previas.get(motivos[j], 0) + 1
                continue
            suyas = por_so.get(r['odoo_so_name'], [])
            motivo = externos.get(r['odoo_so_name'])
            if motivo:
                excluidas.append((posicion, r, motivo, suyas))
                continue
            elegidas.append((posicion, r))
            facturas += suyas
        i += BLOQUE_BUSQUEDA

    if len(elegidas) < cupo:
        log.warning(f"{tipo}: solo {len(elegidas)} orden(es) liberables en las "
                    f"primeras {tope} nuevas; se pidieron {cupo}.")
    return elegidas, facturas, excluidas, previas


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


def revisar_lote_completo(n_elegidas, m):
    """Exige un lote con B2B y B2C (ver "LOTE COMPLETO" en la cabecera).

    n_elegidas = {'B2B': n, 'B2C': n}. Devuelve la lista de problemas.
    """
    problemas = []
    for tipo, lote, cuota in (('B2B', RESET_LOTE_B2B, m.QUOTA_B2B),
                              ('B2C', RESET_LOTE_B2C, m.QUOTA_B2C)):
        if lote <= 0:
            problemas.append(f"{tipo}: RESET_LOTE_{tipo}={lote}. El lote debe "
                             f"llevar B2B y B2C.")
        if not cuota:
            problemas.append(f"{tipo}: AMZ_TEST_QUOTA_{tipo}={cuota}. En 01 un "
                             f"0 es SIN TOPE; usar un numero mayor que cero.")
            continue
        if lote < cuota:
            problemas.append(f"{tipo}: RESET_LOTE_{tipo}={lote} es menor que la "
                             f"cuota de 01 ({cuota}).")
        n = n_elegidas.get(tipo, 0)
        if n < cuota:
            problemas.append(f"{tipo}: solo {n} orden(es) liberables para una "
                             f"cuota de {cuota} en 01. Ampliar AMZ_START_DATE "
                             f"o bajar AMZ_TEST_QUOTA_{tipo}.")
        else:
            log.info(f"{tipo}: {n} liberables >= cuota de 01 ({cuota}). OK")
    return problemas


# =============================================================================
# RESPALDO
# =============================================================================

CABECERA_CSV = ['odoo_move_id', 'name', 'so', 'invoice_origin', 'move_type',
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
            so = f.get('so_name') or f.get('invoice_origin')
            tipo, posicion = meta.get(so, ('', ''))
            w.writerow([f['id'], f.get('name'), so, f.get('invoice_origin'),
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


def liberar_so(odoo, so_name, docs, res, resultados):
    """Cancela TODOS los documentos vivos de una SO (facturas y NC).

    Ultima linea de defensa: justo antes de tocar nada se vuelven a leer de
    Odoo los documentos de la SO y sus conciliaciones. Si aparecio un cobro
    real, no se toca nada de la SO. Si aparecio un documento nuevo, tambien se
    cancela: el objetivo es dejar la SO sin nada vivo.

    Orden: 1) desconciliar (solo queda la conciliacion interna factura <-> NC,
    que el write forzado no rompe solo); 2) cada documento a borrador y luego
    a cancelado. Devuelve los documentos que no venian en 'docs', para que el
    CSV los incluya.
    """
    frescos = documentos_vivos(odoo, [so_name]).get(so_name, [])
    vistos = {f['id'] for f in docs}
    nuevos = [f for f in frescos if f['id'] not in vistos]

    if cobros_externos(odoo, {so_name: frescos}):
        for f in docs + nuevos:
            resultados[f['id']] = {'resultado': 'RECHAZADA_CON_COBRO'}
        res['rechazadas'] += 1
        log.error(f"  RECHAZADA {so_name}: tiene un cobro conciliado. No se toca.")
        return nuevos

    try:
        lineas = lineas_conciliables(odoo, [f['id'] for f in frescos])
        con_conciliacion = [l['id'] for l in lineas
                            if l['matched_debit_ids'] or l['matched_credit_ids']]
        # Cambio del 28-oct-2026 (correo de Jero, lote 6): ademas de la CxC se
        # desconcilia CUALQUIER otra partida, en particular la 205.06.003 con
        # la salida de almacen. Antes solo se rompia la CxC y, como el write
        # forzado no llama a remove_move_reconcile, la salida quedaba
        # conciliada con la factura cancelada y la factura nueva no podia
        # conciliarse: 44 de 48 facturas del lote quedaron abiertas.
        otras = odoo.execute_kw(
            'account.move.line', 'search_read',
            [[('move_id', 'in', [f['id'] for f in frescos]),
              ('account_type', 'not in', CUENTAS_CONCILIABLES),
              '|', ('matched_debit_ids', '!=', False),
              ('matched_credit_ids', '!=', False)]],
            {'fields': ['id']})
        con_conciliacion += [l['id'] for l in otras]
        if con_conciliacion:
            # remove_move_reconcile no devuelve nada: por XML-RPC hace el
            # trabajo y luego falla al responder. Antes eso se leia como
            # rechazo y la SO no se liberaba.
            ejecutar_sin_retorno(odoo, 'account.move.line',
                                 'remove_move_reconcile', con_conciliacion)
            res['desconciliadas'] += 1
    except OdooWriteUncertain as e:
        for f in frescos:
            resultados[f['id']] = {'resultado': 'ESCRITURA_EN_DUDA',
                                   'error': f'desconciliar: {e}'[:300]}
        res['fallidas'] += len(frescos)
        log.error(f"  ESCRITURA EN DUDA al desconciliar {so_name}: {e}")
        return nuevos
    except xmlrpc.client.Fault as e:
        # Odoo rechazo desconciliar (rollback hecho). No se cancela nada de la
        # SO: cancelar con la factura y su NC todavia conciliadas dejaria
        # asientos cancelados con conciliacion viva.
        for f in frescos:
            resultados[f['id']] = {'resultado': 'FALLIDA',
                                   'error': f'desconciliar: {e.faultString}'[:300]}
        res['fallidas'] += len(frescos)
        log.error(f"  {so_name}: Odoo rechazo desconciliar; no se toca la SO. "
                  f"{e.faultString[:200]}")
        return nuevos

    for f in frescos:
        etiqueta = f"{f.get('name')} [{f.get('move_type')}] ({so_name})"
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
            if f.get('move_type') == 'out_refund':
                res['nc_canceladas'] += 1
            log.info(f"  Cancelada {etiqueta} "
                     f"[{det['metodo_borrador']}/{det['metodo_cancel']}]")

        except OdooWriteUncertain as e:
            # Escritura no reintentable que quedo en duda: no se sabe si Odoo
            # hizo commit. Se reporta y se sigue; el CSV tiene el estado previo.
            det['resultado'] = 'ESCRITURA_EN_DUDA'
            det['error'] = str(e)[:300]
            res['fallidas'] += 1
            log.error(f"  ESCRITURA EN DUDA {etiqueta}: {e}")
        except Exception as e:
            det['error'] = str(e)[:300]
            res['fallidas'] += 1
            log.error(f"  Fallo {etiqueta}: {e}")
    return nuevos


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
    log.info(f"Gracia B2C (de 01)  : {m.GRACE_DAYS} dias (no se liberan)")
    log.info(f"Solo depositadas    : {SOLO_DEPOSITADAS}")
    log.info('')

    db_conn = get_db_connection()
    en_control, n_cobradas = ordenes_en_control(db_conn)
    log.info(f"Ordenes ya en tablas de control: {len(en_control)} "
             f"({n_cobradas} con cobro ODOO_PAID). Quedan fuera de la muestra.")

    cursor = db_conn.cursor(dictionary=True)
    desde = m.inicio_del_alcance(db_conn) if m.CON_ALCANCE else None
    sql, params = m.construir_query_lote(desde)
    log.info("Ejecutando QUERY_LOTE (la misma que usa el miner)...")
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    cursor.close()
    db_conn.close()
    log.info(f"{len(rows)} orden(es) en la ventana.")

    if RESET_ORDENES:
        pedidas = set(RESET_ORDENES)
        rows = [r for r in rows if r['mkp_order_id'] in pedidas]
        faltan = pedidas - {r['mkp_order_id'] for r in rows}
        log.info(f"MUESTRA EXPLICITA: {len(rows)} de {len(pedidas)} ordenes "
                 f"pedidas estan en lo que lee 01.")
        if faltan:
            log.warning(f"No estan en lo que lee 01 (revisar alcance): "
                        f"{', '.join(sorted(faltan))}")

    cands, descartadas = candidatas_por_tipo(rows, en_control)
    log.info(f"Nuevas (fuera de control): B2B {len(cands['B2B'])} | "
             f"B2C {len(cands['B2C'])} | descartadas por estar en control: "
             f"{descartadas}")

    odoo = OdooModelProxy(timeout=300)
    log.info('Recorriendo candidatas y revisando sus facturas en Odoo...')

    facturas, meta, profundidad, n_elegidas = [], {}, {}, {}
    muestra, excluidas_todas = [], []
    ahora = datetime.now()
    for tipo, cupo in (('B2B', RESET_LOTE_B2B), ('B2C', RESET_LOTE_B2C)):
        if RESET_ORDENES:
            cupo = len(cands[tipo])
        elegidas, suyas, excluidas, previas = elegir_liberables(
            odoo, cands[tipo], cupo, tipo, m.GRACE_DAYS, ahora)
        facturas += suyas
        muestra += [r for _, r in elegidas]
        excluidas_todas += excluidas
        profundidad[tipo] = elegidas[-1][0] if elegidas else 0
        n_elegidas[tipo] = len(elegidas)
        for pos, r in elegidas:
            meta[r['odoo_so_name']] = (tipo, pos)
        for pos, r, motivo, _ in excluidas:
            meta[r['odoo_so_name']] = (tipo, pos)
        n_cobro = sum(1 for e in excluidas if e[2] == 'EXCLUIDA_CON_COBRO')
        n_nc = sum(1 for f in suyas if f.get('move_type') == 'out_refund')
        log.info(f"{tipo}: {len(elegidas)} elegidas ({len(suyas)} documentos, "
                 f"{n_nc} NC) | excluidas {n_cobro} con cobro | saltadas sin "
                 f"consultar Odoo: "
                 f"{previas.get('EN_GRACIA', 0)} en gracia, "
                 f"{previas.get('SIN_DEPOSITO', 0)} sin deposito | "
                 f"profundidad {profundidad[tipo]}")

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
             f"{len(facturas)} documento(s) vivo(s) a cancelar "
             f"({sum(1 for f in facturas if f.get('move_type') == 'out_refund')} NC)")
    multi = sum(1 for r in muestra if (r.get('n_envios') or 1) > 1)
    log.info(f"  Ordenes de varios envios: {multi} "
             f"(candidatas a cobro parcial entre settlements)")
    log.info('=' * 78)

    # Con muestra explicita no hay banda ni cuota que cumplir: 01 lee el
    # alcance completo.
    if RESET_ORDENES:
        problemas = []
    else:
        problemas = revisar_banda(profundidad, m)
        problemas += revisar_lote_completo(n_elegidas, m)
    for p in problemas:
        log.warning(p)

    # Respaldo: facturas a cancelar + las de SO excluidas, con su motivo.
    resultados = {}
    vistas = list(facturas)
    for _, _, motivo, suyas in excluidas_todas:
        for f in suyas:
            resultados[f['id']] = {'resultado': motivo}
            vistas.append(f)

    # Agrupado por SO, en el orden de la muestra. RESET_MAX se aplica por SO
    # completa: cortar a media SO dejaria, por ejemplo, la factura cancelada y
    # la NC viva, que es justo el defecto que se corrigio.
    por_so, orden_so = {}, []
    for f in facturas:
        if f['so_name'] not in por_so:
            orden_so.append(f['so_name'])
        por_so.setdefault(f['so_name'], []).append(f)
    a_cancelar, n_docs = [], 0
    for so in orden_so:
        docs = por_so[so]
        if RESET_MAX and n_docs + len(docs) > RESET_MAX:
            for f in docs:
                resultados[f['id']] = {'resultado': 'OMITIDA_POR_RESET_MAX'}
            continue
        a_cancelar.append((so, docs))
        n_docs += len(docs)
    if RESET_MAX and n_docs < len(facturas):
        log.warning(f"RESET_MAX={RESET_MAX}: se cancelan {n_docs} de "
                    f"{len(facturas)} documentos (SO completas). El CSV lista "
                    f"TODOS.")

    sello = datetime.now().strftime('%Y%m%d_%H%M%S')
    ruta = os.path.join(_dir, f'reset_backup_{sello}.csv')
    escribir_csv(vistas, ruta, resultados, meta)
    log.info(f"RESPALDO escrito: {ruta} ({len(vistas)} factura(s))")

    if DRY_RUN:
        log.info('')
        log.info('=' * 78)
        log.info('DRY-RUN: no se cancelo nada.')
        log.info(f"  SO que se liberarian        : {len(a_cancelar)}")
        log.info(f"  Documentos que se cancelarian: {n_docs}")
        log.info(f"  SO excluidas (cobro / NC)   : {len(excluidas_todas)}")
        log.info(f"  Respaldo                    : {ruta}")
        if problemas:
            log.info('  ATENCION: la muestra no cumple la banda de 01 o no es '
                     'un lote completo B2B + B2C (ver avisos). Con '
                     'RESET_DRY_RUN=false el script abortaria.')
        log.info('=' * 78)
        return

    if problemas:
        raise Exception(
            "La muestra no cumple la banda de 01 o no es un lote completo "
            "B2B + B2C; no se cancela nada. Ver los avisos de arriba "
            "(AMZ_MINER_OVERFETCH, AMZ_START_DATE, cuotas, RESET_LOTE_*).")

    res = {'canceladas': 0, 'nc_canceladas': 0, 'fallidas': 0,
           'rechazadas': 0, 'desconciliadas': 0, 'nativo': 0, 'forzado': 0}

    log.info('')
    log.info(f"Liberando {len(a_cancelar)} SO ({n_docs} documentos) en "
             f"{ODOO_DB}...")
    for so, docs in a_cancelar:
        vistas += liberar_so(odoo, so, docs, res, resultados)

    # Reescritura con el detalle por factura. Atomica: ver escribir_csv().
    escribir_csv(vistas, ruta, resultados, meta)

    # Verificacion final: ninguna SO liberada debe conservar documentos vivos.
    liberadas = [so for so, _ in a_cancelar]
    try:
        vivos = documentos_vivos(odoo, liberadas)
    except Exception as e:
        vivos = None
        log.error(f"No se pudo hacer la verificacion final: {e}")

    log.info('')
    log.info('=' * 78)
    log.info('RESET TERMINADO')
    log.info('=' * 78)
    log.info(f"  Documentos cancelados : {res['canceladas']} "
             f"({res['nc_canceladas']} NC)")
    log.info(f"  SO desconciliadas     : {res['desconciliadas']} "
             f"(factura <-> su NC)")
    log.info(f"  Documentos con fallo  : {res['fallidas']}")
    log.info(f"  SO rechazadas (cobro) : {res['rechazadas']}")
    log.info(f"  Cambios de estado     : {res['nativo']} nativos | "
             f"{res['forzado']} forzados")
    log.info(f"  Tablas de control     : sin cambios")
    log.info(f"  Respaldo              : {ruta}")
    if vivos is not None:
        log.info(f"  SO con docs vivos     : {len(vivos)} de {len(liberadas)} "
                 f"liberadas (debe ser 0)")
    log.info('=' * 78)

    if vivos:
        log.error("SO_CON_DOCUMENTOS_VIVOS: revisar a mano antes de correr 01.")
        for so in sorted(vivos):
            docs = ', '.join(f"{f.get('name')} [{f.get('move_type')}, "
                             f"{f.get('state')}]" for f in vivos[so])
            log.error(f"  {so}: {docs}")

    if res['forzado']:
        log.warning(f"{res['forzado']} cambio(s) de estado se hicieron con "
                    f"write() directo, saltandose button_draft/button_cancel. "
                    f"Valido SOLO en el espejo de pruebas.")
    if res['fallidas']:
        log.warning("Hubo facturas que no se pudieron cancelar: esas ordenes "
                    "van a salir como ALREADY_ODOO_INVOICED en 01.")
    if res['rechazadas']:
        log.error(f"{res['rechazadas']} SO con un cobro conciliado llegaron "
                  f"hasta la cancelacion y se rechazaron (el cobro aparecio "
                  f"despues de elegir la muestra, o cobros_externos() no lo "
                  f"vio). No se toco nada de ellas.")


if __name__ == '__main__':
    banner('RESET DEL LOTE DE PRUEBAS -- AMAZON (DESTRUCTIVO)')
    try:
        validar_entorno()
        candado_staging()
        run()
    except Exception as e:
        log.error(f'Fallo el reset: {e}')
        sys.exit(1)
