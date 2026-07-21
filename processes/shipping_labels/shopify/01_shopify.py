import MySQLdb
import xmlrpc.client
from unidecode import unidecode
import datetime
import json
import requests
import re
import base64
import gspread
import pandas as pd
import ast
import os
import sys
import dotenv
sys.path.append("\home\ubuntu\wb-data-hub\processes\shipping_labels\shipping_routing_system")
import _01_apis_connections as ac
import _02_get_odoo_records_api as odoo_get
sys.path.append(r"/var/lib/jenkins/m1/")


print('Definiendo parámetros para la ejecución...')
print('')
# Defino los parámetros.
IS_TEST = False
SHIPPER = ("Somos Reyes", "Somos Reyes", "False", "5568309829", "Benito J, San Pedro Barrientos", "11", "Tlalnepantla", "MX", "MX", "54010")

# Azure
# gc = gspread.service_account(filename='C:/Users/data/Documents/update-tables-2d027f6c21f7.json')

# PC ERIC
#gc = gspread.service_account(filename='C:/Users/WonderBrandsWonderBr/Documents/update-tables-2d027f6c21f7.json')

# JENKINS
gc = gspread.service_account(filename = '/var/lib/jenkins/m1/credenciales_reportes.json')

key_gs = '1wn1lzoyUrZ6JRgbSPES3XFYCLZIEzl0a76LYyq8GPZE'

odoo_connection = odoo_get.OdooConnection(is_test=False)
paquetexpress_api = ac.PaquetexpressAPI(IS_TEST)
fedex_api = ac.FedExAPI(IS_TEST)
fedex_account = fedex_api.fedex_account()
eship_api = ac.EShipAPI(IS_TEST)

ENV_PATH = '/var/lib/jenkins/m1/.env' #JENKINS
dotenv.load_dotenv(dotenv_path=ENV_PATH)

odoo_url = os.getenv('odoo_urlV18')
odoo_user = os.getenv('odoo_user_dataV18')
odoo_password = os.getenv('odoo_password_dataV18')
odoo_db = os.getenv('odoo_dbV18')

SECRET_SHOPIFY_TOKEN= os.getenv(SECRET_SHOPIFY_TOKEN)
SECRET_SHOPIFY_URL= os.getenv(SECRET_SHOPIFY_URL)


common = xmlrpc.client.ServerProxy('{}/xmlrpc/2/common'.format(odoo_url))
uid = common.authenticate(odoo_db, odoo_user, odoo_password, {})
models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(odoo_url))
# Me conecto a la DB.

# ==========
host=os.getenv("DB_HOST")
user=os.getenv("DB_USER")
password=os.getenv("DB_PASSWORD")
database=os.getenv("DB_NAME") #tools
# ====


db = MySQLdb.connect(host, user, password, 'somos_reyes', local_infile = True)
cursor = db.cursor()
# Obtengo órdenes de Odoo.
query_orders = """
SELECT a.channel_order_reference, odoo_order_name as odoo_order_name, odoo_order_id, picking_id, partner_shipping_id,
 c.contact_email, c.shipping_address, c.id as shopify_id, b.amount_total + ifnull(shipping_amount, 0) 'paid_amount'
    FROM bi_logs.srs_labels_logs a
    LEFT JOIN somos_reyes.odoo_new_sale_order_live b
    ON a.odoo_order_name = b.name
    LEFT JOIN somos_reyes.shopify_orders_notes c
    ON a.channel_order_reference = c.order_number
    LEFT JOIN (SELECT order_id, if(discount_allocations = '[]', price, 0) 'shipping_amount'
               FROM somos_reyes.shopify_shipping
               WHERE code like '%Envío%') d
    ON c.order_number = d.order_id
    WHERE a.try_get_label = '0'
    AND a.get_label = '0'
    AND a.tracking_number is null
    AND a.picking_id is not null;
"""
cursor.execute(query_orders)
results = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
# Itero por cada resultado.
for result in results:
    # Defino variables de la orden.
    order_number = str(result['channel_order_reference'])
    odoo_order_name = str(result['odoo_order_name'])
    odoo_order_id = int(result['odoo_order_id'])
    shopify_id = str(result['shopify_id'])
    picking_id = int(result['picking_id'])
    partner_shipping_id = int(result['partner_shipping_id'])
    paid_amount = float(result['paid_amount'])
    print('Shopify Order ID:', order_number)
    print('Odoo SO Name:', odoo_order_name)
    # Inserto log en la tabla de logs.
    update_query = """UPDATE bi_logs.srs_labels_logs
                        SET try_get_label = %s WHERE odoo_order_id = %s;"""
    cursor.execute(update_query, (True, odoo_order_id))
    db.commit()
    # Consulto SKU. # METER VARIABLE DE TIMESTAMP DE INSERCION DE GUIA AL CAMPO QUE VA A AGREGAR SERGIO.
    check_sku_query = """SELECT default_code, sum(a.product_qty) as product_qty
    FROM somos_reyes.odoo_new_sale_order_line_live a
    LEFT JOIN somos_reyes.odoo_new_product_product_bis b
    ON a.product_id = b.id
    WHERE a.order_name = %s
    AND b.default_code <> 'C-ENVIO'
    GROUP BY 1;"""
    cursor.execute(check_sku_query, (odoo_order_name,))
    result_skus = cursor.fetchall()
    if len(result_skus) > 1 or result_skus[0][1] > 1:
        packages_number = 2
    else:
        packages_number = None
        sku = result_skus[0][0]
        # Consulto detalles del paquete.
        query_sku_info = "select * from somos_reyes.catalogo_descriptivo where active = 1 and default_code = '" + sku + "' limit 1;"
        cursor.execute(query_sku_info)
        res = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]
        for rec in res:
            # INFORMACION DEL PAQUETE
            packing_length = float(rec['packing_length'])
            packing_width = float(rec['packing_width'])
            packing_height = float(rec['packing_height'])
            packing_weight = float(rec['packing_weight'])
            packages_number = int(rec['packages_number'])
            # INFORMACIÓN DEL SAT
            unspsc = int(rec['unspsc_id'])
            precio_odoo = float(rec['precio_odoo'])
            # SKUs con más de 1 paquete.
            if sku in ['71110-SZ', '300205-SZ', 'KINGSINGLE8', '300206-SZ', 'KINGBUNK2', '78410-SZ',
                       '40601-SZ', '15401-SZ', 'KINGSINGLE9', '78310-SZ', 'KINGDOUBLE3', '40528-SZ',
                       'COMBO-KINGSMAN-148', 'KINGBUNK4', 'KHMESAX6SN', 'SOFALUXE-GRI', 'BEDCOU2-BLA',
                       '78410-SZ', 'DNGLEX4-NEG', 'DNG-GLAZI6-NEG', 'KINGBUNK2', 'BEDTW1-MAD', 'DRESMULTI6-BLA',
                       'DNG-GLAZI6-GRI', 'DNG-GLAZI6-MAR', 'BEDFLEX-DREAMTWO']:
                packages_number = 2

    # Inserto log en la tabla de logs.
    update_query = """UPDATE bi_logs.srs_labels_logs
                        SET packages_number = %s WHERE odoo_order_id = %s;"""
    cursor.execute(update_query, (packages_number, odoo_order_id))
    db.commit()
    # Si el SKU se va con un paquete solamente.
    if packages_number == 1:
        # Defino variables del cliente.
        try:
            client_contact_email = str(result['contact_email'])
            client_shipping_address = ast.literal_eval(result['shipping_address'])
            client_shipping_address_first_name = str(client_shipping_address['first_name'])
            client_shipping_address_last_name = str(client_shipping_address['last_name'])
            client_shipping_address_address1 = str(client_shipping_address['address1'][:35])
            client_shipping_address_address2 = str(client_shipping_address['address2'][:35])
            client_shipping_address_city = str(client_shipping_address['city'])
            client_shipping_address_country = str(client_shipping_address['country'])
            client_shipping_address_province = str(client_shipping_address['province'])
            client_shipping_address_zip = str(client_shipping_address['zip'])
            client_shipping_address_phone = str(client_shipping_address['phone'])
            # Defino detalles del cliente.
            # (name, company, email, phone, street1, street2, city, state, country, zip)
            recipient = (client_shipping_address_first_name + ' ' + client_shipping_address_last_name,
                         "False",
                         client_contact_email,
                         client_shipping_address_phone,
                         client_shipping_address_address1,
                         client_shipping_address_address2,
                         client_shipping_address_city,
                         client_shipping_address_province,
                         "MX",
                         client_shipping_address_zip)
            measures = (packing_weight, packing_length, packing_width, packing_height)
            data_sat = (unspsc, precio_odoo)
            # Obtengo detalles del SRS.
            print('')
            print('Obteniendo detalles de Shipping Routing System...')
            print('')
            srs_detail = odoo_connection.get_srs_info_from_odoo(sale_order_id=odoo_order_id)
            srs_first_option = srs_detail[0]['shipping_options'][0]
            first_option_service_type = srs_first_option['rate_id']
            first_option_cost = float(srs_first_option['total_cost'])
            print('Platform First Option:', srs_first_option['platform'])
            print('Service/Rate ID:', first_option_service_type)
            print('')
            # Valido costo.
            print('paid_amount:', paid_amount)
            print('first_option_cost:', first_option_cost)
            print('check_gasto:', first_option_cost <= 0.21 * paid_amount)
            if first_option_cost <= 0.21 * paid_amount:
                # Inserto log en la tabla de logs.
                update_check_gasto = """UPDATE bi_logs.srs_labels_logs
                                        SET check_gasto_logistico = %s WHERE odoo_order_id = %s;"""
                cursor.execute(update_check_gasto, (True, odoo_order_id))
                db.commit()
                # Genero guía.
                response_label = None
                if srs_first_option['platform'] == 'eship':
                    print('Generando guía en eShip...')
                    print('')
                    # Obtengo el payload.
                    payload_eship_shipping = eship_api.construct_generation_label_payload(rate_id=first_option_service_type)
                    # Obtengo la guía.
                    response_label = eship_api.get_label(payload=payload_eship_shipping)
                    if response_label:
                        carrier = response_label['complete_response']['provider']
                        if 'fedex' in carrier.lower():
                            carrier_code = 1
                        elif 'dhl' in carrier.lower():
                            carrier_code = 3
                        elif 'paquetexpress' in carrier.lower():
                            carrier_code = 4
                        elif 'estafeta' in carrier.lower():
                            carrier_code = 2
                        elif 'segmail' in carrier.lower():
                            carrier_code = 7

                        print('Listo, guía guardada....')

                elif srs_first_option['platform'] == 'fedex':
                    print('Generando guía en FedEx...')
                    print('')
                    # Obtengo el payload
                    payload_fedex_shipping = fedex_api.construct_generation_label_payload(
                        fedex_account=fedex_account,
                        service_type=first_option_service_type,
                        shipper=SHIPPER,
                        recipient=recipient,
                        measures=measures
                    )
                    # Obtengo la guía.
                    response_label = fedex_api.get_label(payload_fedex_shipping)
                    if response_label:
                        # Genero la carta porte.
                        response_carta_porte = fedex_api.carta_porte(data_sat=data_sat,
                                                                     tracking_number=response_label['tracking_number'],
                                                                     measures=measures)
                        carrier = 'FedEx'
                        carrier_code = 1

                        print('Listo, guía guardada....')

                elif srs_first_option['platform'] == 'paquetexpress':
                    print('Generando guía en Paquetexpress...')
                    print('')
                    payload_paquetexpress_shipping = paquetexpress_api.construct_generation_label_payload(
                        service_type=first_option_service_type,
                        shipper=SHIPPER,
                        recipient=recipient,
                        measures=measures,
                        data_sat=data_sat)

                    try:
                        response_label = paquetexpress_api.get_label(payload=payload_paquetexpress_shipping)
                    except TypeError:
                        response_label = None

                    if response_label:
                        carrier = 'PaquetExpress'
                        carrier_code = 4

                        print('Listo, guía guardada....')
                        print('')
                # Si se pudo obtener la guía, la descargo y la inserto en Odoo.
                if response_label:
                    # Inserto log en la tabla de logs.
                    update_query = """UPDATE bi_logs.srs_labels_logs
                                            SET get_label = %s WHERE odoo_order_id = %s;"""
                    cursor.execute(update_query, (True, odoo_order_id))
                    db.commit()
                    # Defino tracking_number.
                    tracking_number = response_label['tracking_number']
                    # Si la guía es en formatpo zpl.
                    if response_label['zpl']:
                        zpl_text = response_label['zpl']
                        file_name = odoo_order_name + '.txt'
                        with open(file_name, "w", encoding="utf-8") as file:
                            file.write(zpl_text)
                    # Si la guía es en formato PDF.
                    elif response_label['pdf_url']:
                        pdf_url = response_label['pdf_url']
                        file_name = odoo_order_name + '.pdf'
                        try:
                            response = requests.get(pdf_url)
                            response.raise_for_status()
                            with open(file_name, "wb") as file:
                                file.write(response.content)
                        except:
                            print('No se pudo generar la guía.')
                    # Si no hubo ningún problema con la generación del archivo, lo convierto a base64.
                    if os.path.exists(file_name):
                        # Leer el archivo en base64
                        with open(file_name, "rb") as file:
                            file_data = base64.b64encode(file.read()).decode("utf-8")
                        # Inserto el archivo en Odoo.
                        attachment_data = {
                            'attachment': file_data,  # binario en base64
                            'file_name': file_name,  # nombre del archivo
                            'so_id': odoo_order_id  # ID de la orden de venta (sale.order)
                        }
                        try:
                            attachment_id = models.execute_kw(
                                odoo_db,
                                uid,
                                odoo_password,
                                'sale.order.attachment',
                                'create',
                                [attachment_data]
                            )

                            # Inserto en Odoo el número de guía.
                            carrier_tracking_ref = tracking_number

                            upd_sale_order = models.execute_kw(odoo_db, uid, odoo_password, 'sale.order',
                                                               'write',
                                                               [[odoo_order_id], {'data_tracking_readwrite': carrier_tracking_ref,
                                                                          'data_carrier_selection_relational': carrier_code}])
                            # Inserto mensaje en Odoo.
                            message = {
                                'body': tracking_number + ". Guía creada de forma automática por el equipo de BI.",
                                'message_type': 'comment',
                            }
                            # En SO.
                            write_msg_so = models.execute_kw(odoo_db, uid, odoo_password, 'sale.order', 'message_post',
                                                             [odoo_order_id], message)
                            # En PICK.
                            #write_msg_sp = models.execute_kw(odoo_db, uid, odoo_password, 'stock.picking', 'message_post',
                            #                                 [picking_id], message)
                            # Inserto log en la tabla de logs.
                            update_query = """UPDATE bi_logs.srs_labels_logs
                                                    SET insert_label_odoo = %s,
                                                     tracking_number = %s,
                                                      attachment_id = %s,
                                                       yuju_carrier_tracking_ref = %s
                                                    WHERE odoo_order_id = %s;"""
                            cursor.execute(update_query, (True, tracking_number, attachment_id, carrier_tracking_ref, odoo_order_id))
                            db.commit()
                            # Borro guía.
                            if os.path.exists(file_name):
                                os.remove(file_name)
                                print("Archivo eliminado.")
                            else:
                                print("El archivo no existe.")

                            # Actualizo datos en Shopify.
                            v_api = '2024-01'
                            access_token = SECRET_SHOPIFY_TOKEN
                            shop_url = SECRET_SHOPIFY_URL
                            headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
                            order_url = f"https://{shop_url}/admin/api/{v_api}/orders/{shopify_id}.json"

                            body = {
                                "order": {
                                    "id": shopify_id,
                                    "note": 'Se ha creado la guia de ' + carrier + ': Numero de guia es: ' + tracking_number,
                                }
                            }

                            json_data = json.dumps(body)
                            response = requests.put(order_url, headers=headers, data=json_data)
                            if response.status_code == 200:
                                print(f"Se actualizo de manera correcta la nota en la orden. {shopify_id}")
                            else:
                                print(f"Error al actualizar la orden. Código de estado: {response.status_code}")
                                print(response.text)
                            # ///////////////////////// POST Shipofy Fulfillment (Sergio) /////////////////////////
                            try:
                                shop_url = 'somosreyes.myshopify.com'
                                http_varible = 'https://'
                                url_prefix = '/admin/api/2024-01'
                                transport_company = carrier

                                # Obtener fulfillment de la orden, puede contener lista de items
                                fulfillment_orders = requests.get(
                                    http_varible + shop_url + url_prefix + '/orders/' + shopify_id + '/fulfillment_orders.json',
                                    headers=headers).json()
                                # Obtener el fulfillment_id de la orden en cuestion
                                fulfillment_id = fulfillment_orders['fulfillment_orders'][0]['id']
                                # Obtener el location_id de la orden
                                location_id = \
                                requests.get(http_varible + shop_url + url_prefix + '/locations.json', headers=headers).json()[
                                    'locations'][0]['id']
                                # Obtener el fulfillment line item
                                fulfillment_order_line_items = []

                                for item in fulfillment_orders['fulfillment_orders'][0][
                                    'line_items']:  # de cada orden, obtener line_items
                                    fulfillment_order_line_items.append(
                                        # Añadir a la lista, los items de la orden y la cantidad que se libera
                                        {
                                            'id': item['id'],
                                            'quantity': item['quantity'],
                                        }
                                    )

                                body = {
                                    "fulfillment":
                                        {
                                            "notify_customer": 'true',
                                            "location_id": location_id,
                                            "tracking_info":
                                                {
                                                    "url": "",
                                                    "company": transport_company,
                                                    "number": tracking_number,
                                                },
                                            "line_items_by_fulfillment_order": [
                                                {
                                                    "fulfillment_order_id": fulfillment_id,
                                                    "fulfillment_order_line_items": fulfillment_order_line_items
                                                }
                                            ]
                                        }
                                }

                                # Info de track
                                response = requests.post(http_varible + shop_url + url_prefix + '/fulfillments.json', json=body,
                                                         headers=headers)
                                response_json = response.json()
                                if response.status_code == 201:
                                    print("Se actualizó a fulfilled correctamente.")
                            except:
                                print('Error Shopify Input')
                        except:
                            print('No se pudo adjuntar la guía.')
                else:
                    print('No se pudo generar la guìa.')
            else:
                print('Error validación de costo logístico.')
        except Exception as e:
            print(e)
    else:
        print('La cantidad de paquetes para este SKU es mayor a 1.')

# Inserto órdenes manuales.
query_insert_manuals = """INSERT INTO bi_logs.srs_labels_logs_manual_checks
SELECT odoo_order_name, a.channel_order_reference, False,
       case when picking_id is null then 'PICK no encontrado o sin reserva.'
       when packages_number > '1' then 'Mayor a 1 paquete.'
       when check_gasto_logistico = 0 then 'Costo logístico superior al 21%. REVISAR' 
       when get_label = 0 then 'No se pudo obtener la guía.'
       when insert_label_odoo = 0 then 'No se pudo insertar la guía en Odoo.' end,
       now()
FROM bi_logs.srs_labels_logs a
LEFT JOIN bi_logs.srs_labels_logs_manual_checks b
ON a.channel_order_reference = b.channel_order_reference
LEFT JOIN odoo18.sale_order c
ON a.odoo_order_name = c.name
WHERE (try_get_label = 1 OR (a.picking_id is null AND c.create_date <= date_sub(now(), INTERVAL 3 HOUR)))
AND b.order_name is null
AND (packages_number <> 1
OR get_label <> 1
OR insert_label_odoo <> 1);"""

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


