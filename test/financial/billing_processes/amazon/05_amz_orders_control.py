"""
05_amz_orders_control.py
------------------------
CONTROL CRUZADO DE ORDENES AMAZON -- SOLO LECTURA / SOLO ALERTA

Problema que resuelve:

El proceso nuevo de facturacion de Amazon arranca de la base de datos
(somos_reyes.amz_transactions), no de Odoo. Eso deja un hueco: las ordenes que
SI existen en Odoo con team Amazon pero que NUNCA aparecen en las
transacciones de Amazon quedan fuera del radar del miner y no se facturarian
nunca.

Este script cruza los tres universos y clasifica cada orden, para que nadie se
quede sin explicacion:

    A) Odoo    -> sale.order con team Amazon
    B) Amazon  -> amz_transactions con 'Order Payment' liberado
    C) Odoo    -> account.move (facturas) por SO

>>> NO ESCRIBE EN ODOO NI EN MYSQL. NO CREA SALE ORDERS. <<<
La creacion de SOs es responsabilidad exclusiva de Yuju. Aqui solo se alerta.


===============================================================================
CLASIFICACION
===============================================================================
Desde Odoo:

  OK_FACTURADA              Confirmada, liquidada por Amazon y facturada.
                            Es el camino feliz.

  PENDIENTE_CON_LIQUIDACION Amazon ya liquido y NO hemos facturado.
                            >>> ACCIONABLE: el miner deberia haberla tomado.
                            Si aparece aqui, hay algo que la esta excluyendo
                            (sin cruce en odoo_master_txns_c, fuera de la
                            ventana de fechas, o ya con CFDI de Amazon).

  PENDIENTE_SIN_LIQUIDACION Confirmada, sin facturar, Amazon aun no liquida.
                            Normal si es reciente. Si pasa de UMBRAL_ALERTA_DIAS
                            hay que revisar: Amazon deberia haber liquidado ya.

  FACTURADA_SIN_LIQUIDACION Facturamos y Amazon nunca liquido.
                            >>> Si es vieja, es una venta facturada que nunca
                            nos pagaron. Cuenta por cobrar sin respaldo.

  CANCELADA_CON_FACTURA     La SO esta cancelada pero tiene factura viva.
                            >>> ACCIONABLE: requiere NOTA DE CREDITO Tipo A.
                            Una orden cancelada no puede quedarse solo con
                            factura.

  CANCELADA_SIN_FACTURA     Cancelada y sin factura. Nada que hacer.

  BORRADOR                  SO en draft/sent. Fuera de alcance de facturacion.

Desde Amazon:

  LIQUIDACION_SIN_SO        Amazon liquido una orden que no cruza con ninguna
                            SO de Odoo.
                            >>> ACCIONABLE por Yuju: falta la SO.
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

FECHA_DESDE = '2026-08-01'
TEAM_AMAZON_KEYWORD = 'Amazon'

# Dias tras los cuales una orden sin liquidacion de Amazon deja de ser normal.
# Los settlements cierran ~15 dias despues; 30 da margen suficiente.
UMBRAL_ALERTA_DIAS = 30

CHUNK_ODOO = 200
OUT_CSV = f"amz_control_ordenes_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

# Por defecto PRUEBAS. Exportar ODOO_URL / ODOO_DB para correr en produccion.
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

SLACK_WEBHOOK = os.getenv('SLACK_WEBHOOK')


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
    for intento in range(1, 4):
        try:
            if kwargs is not None:
                return models.execute_kw(ODOO_DB, uid, ODOO_PWD, modelo,
                                         metodo, args, kwargs)
            return models.execute_kw(ODOO_DB, uid, ODOO_PWD, modelo, metodo, args)
        except Exception as e:
            log.warning(f"Error leyendo {modelo}.{metodo} ({intento}/3): {e}")
            if intento == 3:
                raise
            tm.sleep(2 * intento)


def notify_slack(text):
    if not SLACK_WEBHOOK:
        log.warning(f"[SLACK no configurado] {text}")
        return
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            SLACK_WEBHOOK, data=json.dumps({'text': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.error(f"No se pudo notificar a Slack: {e}")


# =============================================================================
# UNIVERSO A -- ORDENES EN ODOO
# =============================================================================

def obtener_teams_amazon(models, uid):
    teams = leer(models, uid, 'crm.team', 'search_read',
                 [[('name', 'ilike', TEAM_AMAZON_KEYWORD)]],
                 {'fields': ['id', 'name']})
    if not teams:
        raise Exception(f"No hay equipos de venta con '{TEAM_AMAZON_KEYWORD}'.")
    for t in teams:
        log.info(f"  Equipo: {t['name']} (id={t['id']})")
    return [t['id'] for t in teams]


def obtener_ordenes_odoo(models, uid, team_ids):
    """Todas las ordenes de Amazon, incluidas las canceladas.

    Se incluyen las canceladas a proposito: una SO cancelada con factura viva
    es justo uno de los casos que hay que detectar.
    """
    ordenes = leer(models, uid, 'sale.order', 'search_read',
                   [[('team_id', 'in', team_ids),
                     ('date_order', '>=', f'{FECHA_DESDE} 00:00:00')]],
                   {'fields': ['id', 'name', 'state', 'date_order',
                               'invoice_status', 'invoice_count', 'amount_total',
                               'team_id']})
    log.info(f"Ordenes de Amazon en Odoo desde {FECHA_DESDE}: {len(ordenes)}")
    return ordenes


# =============================================================================
# UNIVERSO B -- LIQUIDACIONES DE AMAZON
# =============================================================================

QUERY_LIQUIDADAS = """
SELECT
    a.order_id                        AS amz_order_id,
    c.so                              AS odoo_so_name,
    MAX(a.settlement_id)              AS settlement_id,
    MAX(e.deposit_date)               AS deposit_date,
    MAX(IFNULL(d.IsBusinessOrder, 0)) AS is_business_order,
    MAX(IF(f.order_id IS NULL, 0, 1)) AS tiene_cfdi_amazon,
    ROUND(SUM(IFNULL(b.product_charges,0) + IFNULL(b.shipping,0)
            + IFNULL(b.shipping_discount,0) + IFNULL(b.our_price_discount,0)
            + IFNULL(b.our_price_tax,0) + IFNULL(b.shipping_tax,0)
            + IFNULL(b.shipping_tax_discount,0) + IFNULL(b.our_price_tax_disc,0)), 2)
                                      AS cfdi_total
FROM somos_reyes.amz_transactions a
JOIN somos_reyes.amz_transaction_items b
    ON a.transaction_id = b.transaction_id
-- Fila autoritativa por pieza (misma regla que el miner de facturacion)
JOIN (
    SELECT b2.order_adjustment_item_id, MAX(a2.posted_date) AS max_posted
    FROM somos_reyes.amz_transactions a2
    JOIN somos_reyes.amz_transaction_items b2
        ON a2.transaction_id = b2.transaction_id
    WHERE a2.transaction_status <> 'DEFERRED'
      AND a2.description = 'Order Payment'
      AND a2.posted_date >= DATE_SUB(%s, INTERVAL 90 DAY)
    GROUP BY b2.order_adjustment_item_id
) dedup
    ON dedup.order_adjustment_item_id = b.order_adjustment_item_id
   AND dedup.max_posted = a.posted_date
LEFT JOIN (
    SELECT marketplace_order_id, MAX(order_id) AS so
    FROM somos_reyes.odoo_master_txns_c
    GROUP BY marketplace_order_id
) c ON a.order_id = c.marketplace_order_id
LEFT JOIN (
    SELECT amazonorderid, MAX(IsBusinessOrder) AS IsBusinessOrder
    FROM somos_reyes.amazon_orders
    GROUP BY amazonorderid
) d ON a.order_id = d.amazonorderid
LEFT JOIN (
    SELECT settlement_id, MAX(deposit_date) AS deposit_date
    FROM somos_reyes.amz_settlement_report
    GROUP BY settlement_id
) e ON a.settlement_id = e.settlement_id
LEFT JOIN (
    SELECT DISTINCT order_id
    FROM somos_reyes.amz_b2b_invoices
    WHERE receipt_type = 'Invoice'
      AND (status IS NULL OR status <> 'Cancelado')
) f ON a.order_id = f.order_id
WHERE a.transaction_status <> 'DEFERRED'
  AND a.description = 'Order Payment'
  AND DATE(DATE_SUB(a.posted_date, INTERVAL 6 HOUR)) >= %s
GROUP BY a.order_id, c.so
"""


def obtener_liquidadas(db_conn):
    cursor = db_conn.cursor(dictionary=True)
    cursor.execute(QUERY_LIQUIDADAS, (FECHA_DESDE, FECHA_DESDE))
    rows = cursor.fetchall()
    cursor.close()
    log.info(f"Ordenes liquidadas por Amazon desde {FECHA_DESDE}: {len(rows)}")
    return rows


# =============================================================================
# UNIVERSO C -- FACTURAS EN ODOO
# =============================================================================

def obtener_facturas(models, uid, so_names):
    resultado = {}
    lista = [s for s in so_names if s]
    for i in range(0, len(lista), CHUNK_ODOO):
        chunk = lista[i:i + CHUNK_ODOO]
        facturas = leer(models, uid, 'account.move', 'search_read',
                        [[('invoice_origin', 'in', chunk),
                          ('move_type', '=', 'out_invoice'),
                          ('state', '!=', 'cancel')]],
                        {'fields': ['id', 'name', 'state', 'invoice_origin',
                                    'amount_total', 'amount_residual',
                                    'l10n_mx_edi_cfdi_uuid']})
        for f in facturas:
            resultado.setdefault(f['invoice_origin'], []).append(f)
    log.info(f"SOs con factura viva en Odoo: {len(resultado)}")
    return resultado


# =============================================================================
# CLASIFICACION
# =============================================================================

def dias_desde(fecha_str):
    try:
        dt = datetime.strptime(str(fecha_str)[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).days


def clasificar_orden(orden, liquidada, facturas):
    """Devuelve (clasificacion, accionable, detalle)."""
    estado = orden['state']
    antiguedad = dias_desde(orden['date_order'])
    tiene_factura = bool(facturas)

    if estado in ('draft', 'sent'):
        return 'BORRADOR', False, f"SO en estado '{estado}'"

    if estado == 'cancel':
        if tiene_factura:
            return ('CANCELADA_CON_FACTURA', True,
                    f"Cancelada con {len(facturas)} factura(s) viva(s): "
                    f"{', '.join(f['name'] for f in facturas)}. "
                    f"Requiere nota de credito Tipo A.")
        return 'CANCELADA_SIN_FACTURA', False, 'Cancelada y sin factura'

    # --- state == 'sale' ---
    if liquidada and tiene_factura:
        return ('OK_FACTURADA', False,
                f"Liquidada (settlement {liquidada['settlement_id']}) "
                f"y facturada")

    if liquidada and not tiene_factura:
        motivo = []
        if liquidada.get('tiene_cfdi_amazon'):
            motivo.append('Amazon ya emitio el CFDI (B2B): no nos toca facturar')
        if not liquidada.get('odoo_so_name'):
            motivo.append('sin cruce en odoo_master_txns_c')
        detalle = ('; '.join(motivo) if motivo
                   else 'El miner deberia haberla tomado. Revisar exclusiones.')
        # Si Amazon ya facturo, no es una omision nuestra.
        accionable = not liquidada.get('tiene_cfdi_amazon')
        return 'PENDIENTE_CON_LIQUIDACION', accionable, detalle

    if not liquidada and tiene_factura:
        viejo = antiguedad is not None and antiguedad > UMBRAL_ALERTA_DIAS
        return ('FACTURADA_SIN_LIQUIDACION', viejo,
                f"Facturada sin liquidacion de Amazon "
                f"({antiguedad} dias). "
                + ('Cuenta por cobrar sin respaldo de deposito.' if viejo
                   else 'Normal si es reciente.'))

    viejo = antiguedad is not None and antiguedad > UMBRAL_ALERTA_DIAS
    return ('PENDIENTE_SIN_LIQUIDACION', viejo,
            f"Sin facturar y sin liquidacion ({antiguedad} dias). "
            + ('Amazon deberia haber liquidado ya.' if viejo
               else 'Dentro del plazo normal.'))


# =============================================================================
# REPORTE
# =============================================================================

CAMPOS_CSV = ['clasificacion', 'accionable', 'odoo_so_name', 'amz_order_id',
              'estado_so', 'date_order', 'dias', 'invoice_status',
              'facturas_odoo', 'total_odoo', 'saldo_odoo',
              'settlement_id', 'deposit_date', 'total_amazon',
              'is_business_order', 'tiene_cfdi_amazon', 'detalle']


def escribir_csv(filas):
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUT_CSV)
    with open(ruta, 'w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=CAMPOS_CSV)
        writer.writeheader()
        for f in filas:
            writer.writerow({k: f.get(k) for k in CAMPOS_CSV})
    return ruta


def run():
    db_conn = get_db_connection()
    log.info("Leyendo liquidaciones de Amazon (puede tardar)...")
    liquidadas_rows = obtener_liquidadas(db_conn)
    db_conn.close()

    # Indices por SO y por order_id
    liq_por_so = {r['odoo_so_name']: r for r in liquidadas_rows
                  if r['odoo_so_name']}
    liq_sin_so = [r for r in liquidadas_rows if not r['odoo_so_name']]

    models, uid = conectar_odoo()
    team_ids = obtener_teams_amazon(models, uid)
    ordenes = obtener_ordenes_odoo(models, uid, team_ids)

    facturas = obtener_facturas(models, uid, [o['name'] for o in ordenes])

    filas = []
    conteo = {}

    # --- Desde Odoo ---
    for orden in ordenes:
        so = orden['name']
        liquidada = liq_por_so.get(so)
        facts = facturas.get(so, [])

        clasificacion, accionable, detalle = clasificar_orden(
            orden, liquidada, facts)
        conteo[clasificacion] = conteo.get(clasificacion, 0) + 1

        filas.append({
            'clasificacion': clasificacion,
            'accionable': 'SI' if accionable else '',
            'odoo_so_name': so,
            'amz_order_id': liquidada['amz_order_id'] if liquidada else None,
            'estado_so': orden['state'],
            'date_order': orden['date_order'],
            'dias': dias_desde(orden['date_order']),
            'invoice_status': orden['invoice_status'],
            'facturas_odoo': ', '.join(f['name'] for f in facts) or None,
            'total_odoo': sum(f['amount_total'] for f in facts) if facts else None,
            'saldo_odoo': sum(f['amount_residual'] for f in facts) if facts else None,
            'settlement_id': liquidada['settlement_id'] if liquidada else None,
            'deposit_date': liquidada['deposit_date'] if liquidada else None,
            'total_amazon': liquidada['cfdi_total'] if liquidada else None,
            'is_business_order': liquidada['is_business_order'] if liquidada else None,
            'tiene_cfdi_amazon': liquidada['tiene_cfdi_amazon'] if liquidada else None,
            'detalle': detalle,
        })

    # --- Desde Amazon: liquidaciones sin SO ---
    for r in liq_sin_so:
        conteo['LIQUIDACION_SIN_SO'] = conteo.get('LIQUIDACION_SIN_SO', 0) + 1
        filas.append({
            'clasificacion': 'LIQUIDACION_SIN_SO',
            'accionable': 'SI',
            'odoo_so_name': None,
            'amz_order_id': r['amz_order_id'],
            'settlement_id': r['settlement_id'],
            'deposit_date': r['deposit_date'],
            'total_amazon': r['cfdi_total'],
            'is_business_order': r['is_business_order'],
            'tiene_cfdi_amazon': r['tiene_cfdi_amazon'],
            'detalle': 'Amazon liquido una orden sin SO en Odoo. '
                       'Corresponde a Yuju crearla.',
        })

    ruta = escribir_csv(filas)
    reportar(conteo, filas, ruta)


def reportar(conteo, filas, ruta):
    orden_salida = [
        'CANCELADA_CON_FACTURA',
        'PENDIENTE_CON_LIQUIDACION',
        'LIQUIDACION_SIN_SO',
        'FACTURADA_SIN_LIQUIDACION',
        'PENDIENTE_SIN_LIQUIDACION',
        'OK_FACTURADA',
        'CANCELADA_SIN_FACTURA',
        'BORRADOR',
    ]

    log.info('')
    log.info('=' * 82)
    log.info('CONTROL CRUZADO DE ORDENES AMAZON')
    log.info('=' * 82)
    for clave in orden_salida:
        if clave not in conteo:
            continue
        acc = sum(1 for f in filas
                  if f['clasificacion'] == clave and f['accionable'] == 'SI')
        marca = f'  <<< {acc} accionable(s)' if acc else ''
        log.info(f"  {clave:<28}{conteo[clave]:>6}{marca}")
    log.info('-' * 82)
    log.info(f"  {'TOTAL':<28}{len(filas):>6}")
    log.info('=' * 82)

    accionables = [f for f in filas if f['accionable'] == 'SI']

    canceladas = [f for f in filas
                  if f['clasificacion'] == 'CANCELADA_CON_FACTURA']
    if canceladas:
        log.info('')
        log.warning(f"{len(canceladas)} orden(es) CANCELADAS con factura viva.")
        log.warning("Requieren nota de credito Tipo A. Una orden cancelada no "
                    "puede quedarse solo con factura.")
        for f in canceladas[:10]:
            log.info(f"  {f['odoo_so_name']:<14} {f['facturas_odoo']} "
                     f"total={f['total_odoo']}")

    pendientes = [f for f in filas
                  if f['clasificacion'] == 'PENDIENTE_CON_LIQUIDACION'
                  and f['accionable'] == 'SI']
    if pendientes:
        log.info('')
        log.warning(f"{len(pendientes)} orden(es) liquidadas por Amazon SIN "
                    f"facturar y sin CFDI de Amazon.")
        log.warning("El miner deberia haberlas tomado. Revisar por que no.")
        for f in pendientes[:10]:
            log.info(f"  {f['odoo_so_name']:<14} order={f['amz_order_id']} "
                     f"-> {f['detalle']}")

    log.info('')
    log.info(f"Reporte: {ruta}")

    if accionables:
        notify_slack(
            f":clipboard: *Control de ordenes Amazon* — "
            f"{len(accionables)} caso(s) accionable(s) de {len(filas)} "
            f"revisados.\n"
            f"• Canceladas con factura (requieren NC): {len(canceladas)}\n"
            f"• Liquidadas sin facturar: {len(pendientes)}\n"
            f"• Liquidaciones sin SO (Yuju): "
            f"{conteo.get('LIQUIDACION_SIN_SO', 0)}\n"
            f"Detalle en `{os.path.basename(ruta)}`")


if __name__ == '__main__':
    log.info('=' * 82)
    log.info('CONTROL CRUZADO DE ORDENES AMAZON -- SOLO LECTURA')
    log.info(f'Odoo: {ODOO_DB}')
    log.info(f'Desde: {FECHA_DESDE}')
    log.info('=' * 82)
    t0 = tm.time()
    try:
        run()
        log.info(f'Tiempo total: {round(tm.time() - t0, 2)}s')
    except Exception as e:
        log.error(f'Fallo critico: {e}')
        sys.exit(1)
