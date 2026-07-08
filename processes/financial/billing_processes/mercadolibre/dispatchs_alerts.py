import xmlrpc.client
import os
import csv
import logging
from datetime import datetime, timedelta
import mysql.connector
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

#load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env') # Ajustar ruta en Kestra

def generar_reporte_alertas():
    # ── Conexiones ──────────────────────────────────────────────
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
    
    reporte = []

    # ── ALERTA 1: SO confirmada sin OUT (> 5 días) ──────────────
    log.info("Buscando Alerta 1: Retrasos > 5 días...")
    domain_retraso = [
        ('state', '=', 'sale'),
        ('date_order', '<', hace_5_dias),
        ('delivery_status', 'in', ['pending', 'partial'])
    ]
    retrasos = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order', 'search_read', [domain_retraso], {'fields': ['name', 'date_order', 'team_id']})
    
    for r in retrasos:
        reporte.append({
            'Orden': r['name'],
            'Canal': r['team_id'][1] if r.get('team_id') else 'N/A',
            'Tipo_Alerta': 'RETRASO_OUT',
            'Detalle': f"Confirmada el {r['date_order']} y aún sin despachar."
        })

    # ── ALERTA 2: Desfase Facturación–Despacho ──────────────────
    log.info("Buscando Alerta 2: Facturado vs Entregado...")
    domain_desfase = [
        ('state', 'in', ['sale', 'done']),
        ('qty_invoiced', '>', 0)
    ]
    lineas = models.execute_kw(odoo_db, uid, odoo_pwd, 'sale.order.line', 'search_read', [domain_desfase], 
                               {'fields': ['order_id', 'product_id', 'qty_invoiced', 'qty_delivered']})
    
    ordenes_desfasadas = set()
    for l in lineas:
        # Excluimos envíos (C-ENVIO) del cálculo físico
        if l['qty_invoiced'] > l['qty_delivered']:
            prod_name = l['product_id'][1].upper() if l.get('product_id') else ""
            if 'C-ENVIO' not in prod_name:
                ordenes_desfasadas.add(l['order_id'][1] if l.get('order_id') else 'Desconocida')

    for o_name in ordenes_desfasadas:
        reporte.append({
            'Orden': o_name,
            'Canal': 'N/A',
            'Tipo_Alerta': 'DESFASE_FACTURACION',
            'Detalle': "Tiene artículos facturados que aún no tienen OUT."
        })

    # ── ALERTA 3: Entrega sin OUT (Cruce ML Shipping vs Odoo) ────────────
    log.info("Buscando Alerta 3: Entregado en ML (ml_shipping) sin OUT en Odoo...")
    if db:
        # 1. Buscamos envíos entregados en la tabla de shipping de los últimos 30 días
        cursor.execute("""
            SELECT order_id 
            FROM somos_reyes.ml_shipping 
            WHERE status = 'delivered' 
              AND date_created >= UTC_TIMESTAMP() - INTERVAL 30 DAY
        """)
        # Aseguramos que los IDs sean strings para que el 'in' de Odoo funcione perfecto
        ml_delivered = [str(row['order_id']) for row in cursor.fetchall()]

        if ml_delivered:
            # 2. Cortamos en chunks para no saturar el XML-RPC de Odoo
            chunk_size = 200
            for i in range(0, len(ml_delivered), chunk_size):
                chunk = ml_delivered[i:i + chunk_size]
                
                # Buscamos en Odoo si esas órdenes siguen pendientes de despacho físico
                domain_odoo = [
                    ('channel_order_reference', 'in', chunk),
                    ('delivery_status', 'in', ['pending', 'partial']) # Odoo 18
                ]
                odoo_pendientes = models.execute_kw(
                    odoo_db, uid, odoo_pwd, 
                    'sale.order', 'search_read', 
                    [domain_odoo], 
                    {'fields': ['name', 'channel_order_reference']}
                )
                
                # 3. Agregamos al reporte las que hicieron match (Inconsistencias reales)
                for op in odoo_pendientes:
                    reporte.append({
                        'Orden': op['name'],
                        'Canal': 'Mercado Libre',
                        'Tipo_Alerta': 'FANTASMA_OUT',
                        'Detalle': f"ML marca entregado en envío ({op.get('channel_order_reference')}), pero Odoo sigue sin OUT."
                    })
        cursor.close()
        db.close()

    # ── Generar Archivo CSV ─────────────────────────────────────
    filename = f"Reporte_Inconsistencias_{hoy.strftime('%Y%m%d')}.csv"
    if reporte:
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['Orden', 'Canal', 'Tipo_Alerta', 'Detalle'])
            writer.writeheader()
            writer.writerows(reporte)
        log.info(f"✅ Reporte generado con {len(reporte)} alertas: {filename}")
    else:
        # Archivo vacío pero con cabeceras para indicar que todo está bien
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=['Orden', 'Canal', 'Tipo_Alerta', 'Detalle'])
            writer.writeheader()
        log.info("✅ Todo en orden. Cero inconsistencias hoy.")
        
    return filename

if __name__ == "__main__":
    generar_reporte_alertas()