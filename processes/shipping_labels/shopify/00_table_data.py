import MySQLdb
import sys
import gspread
import pandas as pd
sys.path.append("/var/lib/jenkins/m1/")
import set666 as creds

import os

# Azure
# gc = gspread.service_account(filename='C:/Users/data/Documents/update-tables-2d027f6c21f7.json')

# PC ERIC
# gc = gspread.service_account(filename='C:/Users/WonderBrandsWonderBr/Documents/update-tables-2d027f6c21f7.json')

# JENKINS
gc = gspread.service_account(filename = '/var/lib/jenkins/m1/credenciales_reportes.json')

key_gs = '1wn1lzoyUrZ6JRgbSPES3XFYCLZIEzl0a76LYyq8GPZE'

# Me conecto a la DB.



host=os.getenv("DB_HOST")
user=os.getenv("DB_USER")
password=os.getenv("DB_PASSWORD")
database=os.getenv("DB_NAME") #tools



db = MySQLdb.connect(host, user, password, 'somos_reyes', local_infile = True)
cursor = db.cursor()
# Inserto en bi_logs.srs_labels_logs todas las guías que hay que hacer.
query_insert = """REPLACE INTO bi_logs.srs_labels_logs (odoo_order_id, channel_order_reference, odoo_order_name, picking_id, channel)
SELECT a.id, a.channel_order_reference, a.name, JSON_EXTRACT(b.picking_id, '$[0]') as picking_id, 'Shopify'
FROM odoo18.sale_order as a
LEFT JOIN (SELECT origin, picking_id
           FROM somos_reyes.odoo_new_stock_move_line_live
           WHERE reference like '%WH/PICK/%'
           AND state = 'assigned') as b
ON a.name COLLATE utf8mb4_general_ci = b.origin COLLATE utf8mb4_general_ci
LEFT JOIN bi_logs.srs_labels_logs_manual_checks d
ON a.channel_order_reference COLLATE utf8mb4_general_ci =
       d.channel_order_reference COLLATE utf8mb4_general_ci
WHERE wb_srs_flag = 'True' # Que haya sido procesado por el SRS
AND a.channel = 'Shopify' # Que sea de Shopify
AND a.state <> 'cancel' # Que no esté cancelada
AND a.data_tracking_readwrite is null # Que no tenga guía
AND a.data_carrier_selection_relational is null # Que no tenga carrier en Odoo
AND date(date_sub(a.create_date, INTERVAL 6 HOUR)) >= '2026-04-17' # Que se haya creado a partir del vivo
AND d.channel_order_reference is null # Que no se haya marcado como manual"""
# Ejecuto y hago commit.
cursor.execute(query_insert)
db.commit()
# Obtengo órdenes que se deben revisar manuales.
query_insert_manuals = """# Query para las que no van.
REPLACE INTO bi_logs.srs_labels_logs_manual_checks
SELECT a.name, a.channel_order_reference, False, 'No procesada por SRS o fue procesada manualmente', now()
FROM odoo18.sale_order as a
LEFT JOIN bi_logs.srs_labels_logs as b
ON a.id = b.odoo_order_id
WHERE date(date_sub(a.create_date, INTERVAL 6 HOUR)) >= date(date_sub(now(), interval 3 day)) # Mayor a la fecha en que comienza a correr/chequear.
AND a.channel like '%Shopify%' # Que sea de Shopify
AND a.state <> 'cancel' # Que no esté cancelada
AND b.odoo_order_id is null # Que no se va a generar guía.
AND a.create_date <= date_sub(now(), INTERVAL 3 HOUR) # Que se haya creado hace más de 3 horas.
;"""
cursor.execute(query_insert_manuals)
db.commit()
# Obtengo resultado
cursor.execute("""SELECT * FROM bi_logs.srs_labels_logs_manual_checks;""")
get_check_false = cursor.fetchall()
db.close()

print('------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------ ------')
print('Google Sheets Insert.')
# Tracking Generación de Guías Shipping Routing System
sh = gc.open_by_key(key_gs)
# Obtengo la hoja.
worksheet = sh.get_worksheet_by_id(0)
# Configuro el data frame para insertar.
columns = ['order_name', 'channel_order_reference', 'check', 'check_detail', 'inserted_at']
df = pd.DataFrame(get_check_false, columns=columns)
# Convierto todas las columnas a string.
for column in columns:
    df[column] = df[column].values.astype(str)
# Inserto data frame, columnas + valores.
worksheet.clear()
worksheet.update([df.columns.values.tolist()] + df.values.tolist(), value_input_option='USER_ENTERED')
