"""
000_historical_billing_back_analysis.py
---------------------------------------
ANALISIS RETROSPECTIVO DE FACTURACION AMAZON -- SOLO LECTURA

Problema que resuelve:

El proceso de facturacion 1 a 1 nunca supo distinguir las ordenes de Amazon
Business. Como Amazon Business emite su propio CFDI y nosotros tambien
facturamos esas ordenes, hay operaciones con DOS comprobantes timbrados ante
el SAT: uno de Amazon y uno nuestro.

Este script los identifica. NO corrige nada: la funcion corregir_caso() esta
deliberadamente vacia hasta que Finanzas defina el criterio de correccion
(cancelacion fiscal de nuestro CFDI, nota de credito, u otro).

>>> ESTE SCRIPT NO ESCRIBE EN ODOO NI EN MYSQL. <<<
Solo lee y genera un CSV.


===============================================================================
CLASIFICACION
===============================================================================
Para cada orden de Amazon con CFDI de Amazon Business:

  DOBLE_CFDI          Amazon timbro Y nosotros timbramos, con UUID distintos.
                      >>> INCIDENCIA FISCAL. Es lo que hay que corregir.

  VINCULADA_OK        Nuestra factura en Odoo lleva el UUID de Amazon.
                      Correcto: es "jalar el CFDI", no una segunda emision.

  SOLO_AMAZON         Amazon timbro y en Odoo no hay factura.
                      No hay problema fiscal, pero falta el reconocimiento de
                      ingreso y la CxC en Odoo.

  NUESTRA_SIN_TIMBRE  Tenemos factura en Odoo sin UUID (borrador o sin timbrar)
                      y Amazon si timbro. Se puede cancelar antes de timbrar:
                      es el caso mas facil de corregir.

  SIN_SO              La orden no cruza con ninguna SO de Odoo.
"""

import os
import sys
import csv
import time as tm
import logging
from datetime import datetime
import xmlrpc.client
import mysql.connector
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s | %(levelname)s | %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Claude_proyects\wb-data-hub\.env')

# =============================================================================
# CONFIGURACION
# =============================================================================

FECHA_DESDE = '2026-01-01'      # desde cuando revisar
CHUNK_ODOO = 200                # SOs por consulta a Odoo
OUT_CSV = f"amz_doble_facturacion_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

# Ambiente. Por defecto PRUEBAS: este analisis se corre primero contra el
# espejo para medir el universo sin tocar produccion.
# Para correrlo contra produccion, exportar ODOO_URL / ODOO_DB / etc.
ODOO_URL = os.getenv('ODOO_URL') or os.getenv('ODOO_TEST_URL',
                     'https://somosreyes-test26ago26-37337608.dev.odoo.com')
ODOO_DB = os.getenv('ODOO_DB') or os.getenv('ODOO_TEST_DB',
                    'somosreyes-test26ago26-37337608')
ODOO_USER = (os.getenv('ODOO_USER') or os.getenv('ODOO_TEST_USER')
             or os.getenv('odoo_user_dataV18'))
ODOO_PWD = (os.getenv('ODOO_PASSWORD') or os.getenv('ODOO_TEST_PASSWORD')
            or os.getenv('odoo_password_dataV18'))

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'root')
DB_PASS = os.getenv('DB_PASSWORD', '')


# =============================================================================
# CONEXIONES
# =============================================================================

def get_db_connection():
    return mysql.connector.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database='finance')


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout=300, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


def conectar_odoo():
    """Conexion de solo lectura."""
    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PWD]):
        raise Exception("Faltan credenciales de Odoo.")
    common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common',
                                       transport=TimeoutTransport(300))
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PWD, {})
    if not uid:
        raise Exception("No se pudo autenticar contra Odoo.")
    models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object',
                                       transport=TimeoutTransport(300))
    log.info(f"Odoo conectado: {ODOO_DB} (uid={uid})")
    return models, uid


def leer(models, uid, modelo, metodo, args, kwargs=None):
    """Wrapper de lectura con reintento. Este script solo lee, asi que
    reintentar es seguro."""
    for intento in range(1, 4):
        try:
            if kwargs is not None:
                return models.execute_kw(ODOO_DB, uid, ODOO_PWD, modelo,
                                         metodo, args, kwargs)
            return models.execute_kw(ODOO_DB, uid, ODOO_PWD, modelo, metodo, args)
        except Exception as e:
            log.warning(f"Error leyendo {modelo}.{metodo} "
                        f"(intento {intento}/3): {e}")
            if intento == 3:
                raise
            tm.sleep(2 * intento)


# =============================================================================
# LADO AMAZON
# =============================================================================

QUERY_CFDI_AMAZON = """
SELECT
    i.order_id                       AS amz_order_id,
    c.so                             AS odoo_so_name,
    COUNT(DISTINCT i.uuid)           AS n_cfdi_amazon,
    GROUP_CONCAT(DISTINCT i.uuid ORDER BY i.date SEPARATOR ' | ')  AS uuids_amazon,
    MIN(i.date)                      AS primera_fecha,
    MAX(i.total)                     AS total_amazon,
    MAX(i.receiver_rfc)              AS receiver_rfc,
    MAX(i.receiver_name)             AS receiver_name,
    MAX(IFNULL(d.IsBusinessOrder,0)) AS is_business_order
FROM somos_reyes.amz_b2b_invoices i
LEFT JOIN (
    SELECT marketplace_order_id, MAX(order_id) AS so
    FROM somos_reyes.odoo_master_txns_c
    GROUP BY marketplace_order_id
) c ON i.order_id = c.marketplace_order_id
LEFT JOIN (
    SELECT amazonorderid, MAX(IsBusinessOrder) AS IsBusinessOrder
    FROM somos_reyes.amazon_orders
    GROUP BY amazonorderid
) d ON i.order_id = d.amazonorderid
WHERE i.receipt_type = 'Invoice'
  AND (i.status IS NULL OR i.status <> 'Cancelado')
  AND (i.cancellation_date IS NULL OR i.cancellation_date = '')
  AND i.date >= %s
GROUP BY i.order_id, c.so
ORDER BY i.order_id
"""


def obtener_cfdi_amazon(db_conn):
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_CFDI_AMAZON, (FECHA_DESDE,))
    rows = cursor.fetchall()
    cursor.close()
    log.info(f"Ordenes con CFDI de Amazon Business desde {FECHA_DESDE}: {len(rows)}")
    return rows


# =============================================================================
# LADO ODOO
# =============================================================================

def obtener_facturas_odoo(models, uid, so_names):
    """Facturas de cliente por SO. Devuelve {so_name: [facturas]}.

    Se consulta por lotes para no reventar el XML-RPC con miles de nombres.
    """
    resultado = {}
    lista = [s for s in so_names if s]

    for i in range(0, len(lista), CHUNK_ODOO):
        chunk = lista[i:i + CHUNK_ODOO]
        facturas = leer(models, uid, 'account.move', 'search_read',
                        [[('invoice_origin', 'in', chunk),
                          ('move_type', '=', 'out_invoice'),
                          ('state', '!=', 'cancel')]],
                        {'fields': ['id', 'name', 'state', 'invoice_origin',
                                    'amount_total', 'l10n_mx_edi_cfdi_uuid',
                                    'invoice_date', 'ref']})
        for f in facturas:
            resultado.setdefault(f['invoice_origin'], []).append(f)

        log.info(f"  Odoo: {min(i + CHUNK_ODOO, len(lista))}/{len(lista)} SOs "
                 f"consultadas")
    return resultado


def normalizar_uuid(valor):
    if not valor or valor is False:
        return None
    return str(valor).strip().upper()


# =============================================================================
# CLASIFICACION
# =============================================================================

def clasificar(caso, facturas_odoo):
    """Determina el estado de una orden. Devuelve (clasificacion, detalle)."""
    so = caso['odoo_so_name']

    if not so:
        return 'SIN_SO', 'La orden no cruza con ninguna SO de Odoo'

    facturas = facturas_odoo.get(so, [])
    if not facturas:
        return 'SOLO_AMAZON', 'Amazon timbro; en Odoo no hay factura'

    uuids_amazon = {normalizar_uuid(u)
                    for u in (caso['uuids_amazon'] or '').split(' | ')
                    if normalizar_uuid(u)}

    con_timbre = [f for f in facturas if normalizar_uuid(f.get('l10n_mx_edi_cfdi_uuid'))]
    sin_timbre = [f for f in facturas if not normalizar_uuid(f.get('l10n_mx_edi_cfdi_uuid'))]

    if not con_timbre:
        return ('NUESTRA_SIN_TIMBRE',
                f"{len(sin_timbre)} factura(s) en Odoo sin UUID "
                f"(estado: {', '.join(f['state'] for f in sin_timbre)})")

    uuids_odoo = {normalizar_uuid(f['l10n_mx_edi_cfdi_uuid']) for f in con_timbre}
    ajenos = uuids_odoo - uuids_amazon

    if not ajenos:
        return ('VINCULADA_OK',
                'Nuestra factura lleva el UUID de Amazon: esta vinculada')

    return ('DOBLE_CFDI',
            f"Amazon: {len(uuids_amazon)} UUID | Nuestro(s) UUID distinto(s): "
            f"{', '.join(sorted(ajenos))}")


# =============================================================================
# CORRECCION -- PENDIENTE DE DEFINIR CON FINANZAS
# =============================================================================

def corregir_caso(models, uid, caso, clasificacion):
    """Corrige una incidencia de doble facturacion.

    NO IMPLEMENTADO A PROPOSITO.

    Antes de escribir esto Finanzas tiene que definir, para cada caso
    DOBLE_CFDI, cual de los dos comprobantes se conserva y como se anula el
    otro. Las opciones sobre la mesa:

      a) Cancelar fiscalmente NUESTRO CFDI ante el SAT y dejar el de Amazon
         como el valido. Requiere que el CFDI no tenga mas de 24 h o que el
         receptor acepte la cancelacion; a publico en general (XAXX010101000)
         la cancelacion sin aceptacion tiene reglas propias.

      b) Emitir una nota de credito por el total de nuestra factura, dejando
         el ingreso reconocido una sola vez. No elimina el CFDI del SAT pero
         corrige el efecto contable y fiscal.

      c) Dejar ambos y ajustar solo contablemente. Cierra el libro pero deja
         el ingreso duplicado ante el SAT.

    Cada camino tiene implicaciones fiscales distintas y ninguna es reversible.
    La decision no es tecnica.

    Mientras no haya criterio, este script solo reporta.
    """
    pass


# =============================================================================
# REPORTE
# =============================================================================

CAMPOS_CSV = ['clasificacion', 'amz_order_id', 'odoo_so_name',
              'is_business_order', 'n_cfdi_amazon', 'uuids_amazon',
              'total_amazon', 'receiver_rfc', 'receiver_name',
              'facturas_odoo', 'uuids_odoo', 'total_odoo', 'primera_fecha',
              'detalle']


def escribir_csv(resultados):
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUT_CSV)
    with open(ruta, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=CAMPOS_CSV)
        writer.writeheader()
        for r in resultados:
            writer.writerow({k: r.get(k) for k in CAMPOS_CSV})
    return ruta


def run():
    db_conn = get_db_connection()
    casos = obtener_cfdi_amazon(db_conn)
    db_conn.close()

    if not casos:
        log.info("No hay CFDI de Amazon Business en el periodo.")
        return

    models, uid = conectar_odoo()
    so_names = [c['odoo_so_name'] for c in casos if c['odoo_so_name']]
    log.info(f"Consultando facturas en Odoo para {len(so_names)} SOs...")
    facturas_odoo = obtener_facturas_odoo(models, uid, so_names)

    resultados = []
    conteo = {}

    for caso in casos:
        clasificacion, detalle = clasificar(caso, facturas_odoo)
        conteo[clasificacion] = conteo.get(clasificacion, 0) + 1

        facturas = facturas_odoo.get(caso['odoo_so_name'], [])
        resultados.append({
            'clasificacion': clasificacion,
            'amz_order_id': caso['amz_order_id'],
            'odoo_so_name': caso['odoo_so_name'],
            'is_business_order': caso['is_business_order'],
            'n_cfdi_amazon': caso['n_cfdi_amazon'],
            'uuids_amazon': caso['uuids_amazon'],
            'total_amazon': caso['total_amazon'],
            'receiver_rfc': caso['receiver_rfc'],
            'receiver_name': caso['receiver_name'],
            'facturas_odoo': ', '.join(f['name'] for f in facturas) or None,
            'uuids_odoo': ', '.join(
                normalizar_uuid(f['l10n_mx_edi_cfdi_uuid']) or '-'
                for f in facturas) or None,
            'total_odoo': sum(f['amount_total'] for f in facturas) if facturas else None,
            'primera_fecha': caso['primera_fecha'],
            'detalle': detalle,
        })

        # Sin implementar: ver docstring.
        if clasificacion == 'DOBLE_CFDI':
            corregir_caso(models, uid, caso, clasificacion)

    ruta = escribir_csv(resultados)

    log.info('')
    log.info('=' * 78)
    log.info('ANALISIS RETROSPECTIVO -- RESULTADOS')
    log.info('=' * 78)
    orden = ['DOBLE_CFDI', 'NUESTRA_SIN_TIMBRE', 'SOLO_AMAZON',
             'VINCULADA_OK', 'SIN_SO']
    for clave in orden:
        if clave in conteo:
            marca = ' <<< INCIDENCIA FISCAL' if clave == 'DOBLE_CFDI' else ''
            log.info(f"  {clave:<22}{conteo[clave]:>6}{marca}")
    log.info('-' * 78)
    log.info(f"  {'TOTAL':<22}{len(resultados):>6}")
    log.info('=' * 78)

    dobles = [r for r in resultados if r['clasificacion'] == 'DOBLE_CFDI']
    if dobles:
        importe = sum(r['total_odoo'] or 0 for r in dobles)
        log.info('')
        log.warning(f"{len(dobles)} orden(es) con DOS CFDI timbrados.")
        log.warning(f"Importe facturado por nosotros en esos casos: "
                    f"${importe:,.2f}")
        log.info('')
        log.info('Primeros 10 casos:')
        for r in dobles[:10]:
            log.info(f"  {r['odoo_so_name']:<14} order={r['amz_order_id']} "
                     f"factura={r['facturas_odoo']}")
        log.info('')
        log.info('La correccion NO esta implementada: Finanzas debe definir')
        log.info('primero si se cancela nuestro CFDI, se emite nota de credito,')
        log.info('o se ajusta solo contablemente. Ver corregir_caso().')

    log.info('')
    log.info(f"Reporte: {ruta}")


if __name__ == '__main__':
    log.info('=' * 78)
    log.info('ANALISIS RETROSPECTIVO DE DOBLE FACTURACION -- AMAZON')
    log.info(f'Odoo: {ODOO_DB}')
    log.info('Modo: SOLO LECTURA')
    log.info('=' * 78)
    t0 = tm.time()
    try:
        run()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico: {e}')
        sys.exit(1)
