import xmlrpc.client
import os
import csv
import logging
import time
import socket
from datetime import datetime, timedelta
import mysql.connector

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)


# ============
from dotenv import load_dotenv
load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')
# ============

RETRYABLE_EXCEPTIONS = (
    xmlrpc.client.ProtocolError,
    ConnectionError,
    TimeoutError,
    socket.error,
    socket.timeout,
)

def call_odoo(models, db, uid, pwd, model, method, args, kwargs=None, max_retries=4, backoff_base=3):
    kwargs = kwargs or {}
    last_exc = None
    for intento in range(1, max_retries + 1):
        try:
            return models.execute_kw(db, uid, pwd, model, method, args, kwargs)
        except RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            espera = backoff_base * (2 ** (intento - 1))
            log.warning(
                f"[Intento {intento}/{max_retries}] Fallo Odoo -> "
                f"modelo='{model}'. Error: {e}. Reintentando en {espera}s..."
            )
            time.sleep(espera)
    log.error(f"❌ FALLO DEFINITIVO: {last_exc}")
    raise last_exc

def clasificar_venta(warehouse_tuple):
    if not warehouse_tuple:
        return 'N/A', 'N/A'
    wh_name = warehouse_tuple[1]
    tipo = 'FULL' if 'fulfillment' in wh_name.lower() else 'DROP'
    return tipo, wh_name

def obtener_ordenes_con_out_confirmado(models, db, uid, pwd, order_ids):
    if not order_ids:
        return set()
    domain = [
        ('sale_id', 'in', order_ids), 
        ('state', '=', 'done'), 
        ('picking_type_code', '=', 'outgoing')
    ]
    pickings = call_odoo(models, db, uid, pwd, 'stock.picking', 'search_read', [domain], {'fields': ['sale_id']})
    return {p['sale_id'][0] for p in pickings if p.get('sale_id')}

def obtener_ordenes_con_devolucion(models, db, uid, pwd, order_ids):
    """
    Busca si la orden tiene un movimiento de retorno (RET) en estado 'done'.
    """
    if not order_ids:
        return set()
    domain = [
        ('sale_id', 'in', order_ids), 
        ('state', '=', 'done'), 
        ('name', 'ilike', '%RET%')  # Validamos que el folio del movimiento contenga RET
    ]
    pickings = call_odoo(models, db, uid, pwd, 'stock.picking', 'search_read', [domain], {'fields': ['sale_id']})
    return {p['sale_id'][0] for p in pickings if p.get('sale_id')}


def generar_reporte_alertas():
    odoo_url = os.getenv("odoo_urlV18")
    odoo_db = os.getenv("odoo_dbV18")
    odoo_user = os.getenv("odoo_user_dataV18")
    odoo_pwd = os.getenv("odoo_password_dataV18")

    common = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/common')
    uid = common.authenticate(odoo_db, odoo_user, odoo_pwd, {})
    models = xmlrpc.client.ServerProxy(f'{odoo_url}/xmlrpc/2/object')

    try:
        db = mysql.connector.connect(
            host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME")
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        log.error(f"Error BD: {e}")
        db = None

    hoy = datetime.now()
    hace_5_dias = (hoy - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
    
    # ── CAMBIO CLAVE: Usamos un diccionario para consolidar por orden ──
    reporte_dict = {}

    def agregar_al_reporte(orden_data, tipo_venta, almacen_nombre, tipo_alerta, detalle):
        """Función auxiliar para agregar o actualizar una orden en el reporte"""
        nombre = orden_data['name']
        if nombre in reporte_dict:
            # Si la orden ya existe, concatenamos la nueva alerta para no duplicar filas
            if tipo_alerta not in reporte_dict[nombre]['Tipo_Alerta']:
                reporte_dict[nombre]['Tipo_Alerta'] += f" + {tipo_alerta}"
                reporte_dict[nombre]['Detalle'] += f" | {detalle}"
        else:
            # Si es la primera vez que la vemos, la creamos
            reporte_dict[nombre] = {
                'Orden': nombre,
                'Referencia marketplace': orden_data.get('channel_order_reference') or 'N/A',
                'Canal': orden_data['team_id'][1] if orden_data.get('team_id') else 'N/A',
                'Tipo_Venta': tipo_venta,
                'Almacen': almacen_nombre,
                'Tipo_Alerta': tipo_alerta,
                'Detalle': detalle
            }

    # ── ALERTA 1: SO confirmada sin OUT ──────────────
    log.info("Buscando Alerta 1: Retrasos > 5 días...")
    domain_retraso = [('state', '=', 'sale'), ('date_order', '<', hace_5_dias), ('delivery_status', 'in', ['pending', 'partial'])]
    retrasos = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', [domain_retraso], {'fields': ['id', 'name', 'date_order', 'team_id', 'channel_order_reference', 'warehouse_id']})
    retrasos_ids = [r['id'] for r in retrasos]
    ordenes_ya_surtidas = obtener_ordenes_con_out_confirmado(models, odoo_db, uid, odoo_pwd, retrasos_ids)

    for r in retrasos:
        if r['id'] in ordenes_ya_surtidas:
            continue 
        tipo_venta, almacen_nombre = clasificar_venta(r.get('warehouse_id'))
        agregar_al_reporte(
            r, tipo_venta, almacen_nombre, 
            'RETRASO_OUT', 
            f"Confirmada el {r['date_order']} sin OUT."
        )

    # ── ALERTA 2: Desfase Facturación–Despacho ──────────────
    log.info("Buscando Alerta 2: Facturado vs Entregado...")
    domain_desfase = [
        ('state', 'in', ['sale', 'done']),
        ('qty_invoiced', '>', 0),
        ('order_id.date_order', '>=', (hoy - timedelta(days=60)).strftime('%Y-%m-%d %H:%M:%S')),
    ]
    lineas = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', [domain_desfase], {'fields': ['order_id', 'product_id', 'qty_invoiced', 'qty_delivered']})

    ordenes_desfasadas_ids = set()
    for l in lineas:
        if l['qty_invoiced'] > l['qty_delivered']:
            prod_name = l['product_id'][1].upper() if l.get('product_id') else ""
            if 'C-ENVIO' not in prod_name and l.get('order_id'):
                ordenes_desfasadas_ids.add(l['order_id'][0])

    ordenes_desfasadas_ids = list(ordenes_desfasadas_ids)

    if ordenes_desfasadas_ids:
        chunk_size = 200
        for i in range(0, len(ordenes_desfasadas_ids), chunk_size):
            chunk = ordenes_desfasadas_ids[i:i + chunk_size]
            domain_orders = [[('id', 'in', chunk)]]

            orders_data = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', domain_orders, {'fields': ['id', 'name', 'team_id', 'channel_order_reference', 'warehouse_id']})
            ordenes_con_devolucion = obtener_ordenes_con_devolucion(models, odoo_db, uid, odoo_pwd, chunk)

            for o in orders_data:
                tipo_venta, almacen_nombre = clasificar_venta(o.get('warehouse_id'))
                
                if o['id'] in ordenes_con_devolucion:
                    tipo_alerta = 'REQUIERE_NOTA_CREDITO'
                    detalle_texto = "RET confirmado pero factura alta."
                else:
                    tipo_alerta = 'DESFASE_FACTURACION'
                    detalle_texto = "Artículos facturados sin OUT."

                agregar_al_reporte(o, tipo_venta, almacen_nombre, tipo_alerta, detalle_texto)

    # ── ALERTA 3: Entrega sin OUT (Cruce ML Shipping vs Odoo) ────────────
    log.info("Buscando Alerta 3: Entregado en ML sin OUT en Odoo...")
    if db:
        cursor.execute("""
            SELECT order_id FROM somos_reyes.ml_shipping 
            WHERE status = 'delivered' AND date_created >= UTC_TIMESTAMP() - INTERVAL 30 DAY
        """)
        ml_delivered = [str(row['order_id']) for row in cursor.fetchall()]

        if ml_delivered:
            chunk_size = 200
            for i in range(0, len(ml_delivered), chunk_size):
                chunk = ml_delivered[i:i + chunk_size]
                domain_odoo = [('channel_order_reference', 'in', chunk), ('delivery_status', 'in', ['pending', 'partial'])]
                odoo_pendientes = call_odoo(models, odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', [domain_odoo], {'fields': ['id', 'name', 'team_id', 'channel_order_reference', 'warehouse_id']})
                
                pendientes_ids = [op['id'] for op in odoo_pendientes]
                ordenes_ya_surtidas_ml = obtener_ordenes_con_out_confirmado(models, odoo_db, uid, odoo_pwd, pendientes_ids)

                for op in odoo_pendientes:
                    if op['id'] in ordenes_ya_surtidas_ml:
                        continue 
                    tipo_venta, almacen_nombre = clasificar_venta(op.get('warehouse_id'))
                    
                    agregar_al_reporte(
                        op, tipo_venta, almacen_nombre, 
                        'FANTASMA_OUT_ML', 
                        "ML entregado, Odoo sin OUT."
                    )
        cursor.close()
        db.close()

    # ── Generar CSV ────────────
    filename = f"Reporte_Inconsistencias_{hoy.strftime('%Y%m%d')}.csv"
    columnas = ['Orden', 'Referencia marketplace', 'Canal', 'Tipo_Venta', 'Almacen', 'Tipo_Alerta', 'Detalle']
    
    # Extraemos los valores del diccionario para escribirlos en el CSV
    reporte_final = list(reporte_dict.values())

    if reporte_final:
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columnas)
            writer.writeheader()
            writer.writerows(reporte_final)
        log.info(f"✅ Reporte generado con {len(reporte_final)} órdenes únicas: {filename}")
    else:
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=columnas)
            writer.writeheader()
        log.info("✅ Todo en orden. Cero inconsistencias hoy.")

    return filename


if __name__ == "__main__":
    generar_reporte_alertas()