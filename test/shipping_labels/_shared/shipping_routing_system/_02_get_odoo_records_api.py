import os
import xmlrpc.client
from dotenv import load_dotenv
from datetime import datetime, timedelta
import logging
from _00_utilities import Utilities, dotenv_path

import mysql.connector
import re

load_dotenv(dotenv_path)
time_difference = timedelta(hours=0) # Diferencia de UTC 0 ya que la instancia jenkins tiene la misma hora que Odoo

class OdooConnection:
    def __init__(self, is_test=True):
        self.is_test = is_test
        self.odoo_credentials = self._get_odoo_access()
        self.server_url = self.odoo_credentials['url']
        self.db_name = self.odoo_credentials['db']
        self.username = self.odoo_credentials['user']
        self.password = self.odoo_credentials['password']
        self.uid = None
        self.models = None

    def _get_odoo_access(self):

        if self.is_test:
            logging.info('ODOO TEST')
            return {
                'user': os.getenv('odoo_user_dataV18'),
                'password': os.getenv('odoo_password_dataV18'),
                'db': os.getenv('odoo_db_testV18'),
                'url': os.getenv('odoo_url_testV18'),
            }
        else:
            logging.info('ODOO PROD')
            return {
                'user': os.getenv('odoo_user_dataV18'),
                'password': os.getenv('odoo_password_dataV18'),
                'db': os.getenv('odoo_dbV18'),
                'url': os.getenv('odoo_urlV18'),
            }

    def _connect(self, new_connection=False):
        logging.info('----------------------------------------------------------------')


        common = xmlrpc.client.ServerProxy(f'{self.server_url}/xmlrpc/2/common')
        uid = common.authenticate(self.db_name, self.username, self.password, {})
        models = xmlrpc.client.ServerProxy(f'{self.server_url}/xmlrpc/2/object')

        # Si es una nueva conexión, retorna la nueva conexión
        if new_connection:
            return uid, models

        # Configura la conexión global
        self.uid = uid
        self.models = models
        logging.info('----------------------------------------------------------------')

    @Utilities.measure_execution_time
    def get_orders_list_info(self, num_days, num_hours, only_one_item=False):
        self._connect()

        # CP de Tlalnepantla. ID de la empresa en Odoo = 1
        #shipper_zip = self.get_zip_code_SR(company_id=1)

        # Ubicacion completa de Tlalnepantla. ID de la empresa en Odoo = 1
        shipper = self._get_address_SR(company_id=1)

        start_date_odoo, end_date_odoo = self.get_date_range(num_days, num_hours)
        # so_names = ['SO3398130', 'SO3398131', 'SO3398132', 'SO3398133', 'SO3398134', 'SO3398135', 'SO3398136', 'SO3398137', 'SO3398138', 'SO3398139', 'SO3398140']
        so_names = ['S03412']
        so_domain = [
            #('invoice_status', '=', 'to invoice'),
            #('invoice_count', '=', '0'),
            #('date_order', '>=', start_date_odoo),
            #('date_order', '<=', end_date_odoo),
            ('name', 'in', so_names), # Prod 1 item: SO3533215 / Prod 1 items 2 cantidad diferentes: SO3533223 / Test variables  SO3398097
            ('wb_srs_flag', '=', False),
            ('team_id', "=", 'Team_Sitioweb')
            #('product_uom_qty', '=', 1)
        ]


        # ----------------------- Busqueda de órdenes que pasan por SRS -------------------------------
        start_date, end_date = self.get_date_range(n_days=30) # Para la DB
        print('Fechas de rangos: ', start_date_odoo, end_date_odoo, start_date, end_date)
        db1 = Server1db()
        so_domain = db1.get_all_marketplace_orders(start_date, start_date_odoo, end_date_odoo)
        # ------------------------------------------------------

        orders_data_list = []
        records = self.models.execute_kw(self.db_name, self.uid, self.password, 'sale.order', 'search_read', [so_domain], {'fields': ['id', 'name', 'partner_shipping_id', 'order_line']})

        adress_fields = ["zip"]

        for record in records:

            #contact = self.models.execute_kw(self.db_name, self.uid, self.password, 'res.partner', 'read', [record["partner_shipping_id"][0]], {'fields': adress_fields})
            #recipient_zip = contact[0]["zip"] if contact else None

            # Traer direccion completa del cliente con la nueva funcion _get_recipient_address()
            recipient = self._get_recipient_address(record["partner_shipping_id"][0])

            # Obtener las líneas de la orden
            order_lines = self.models.execute_kw(self.db_name, self.uid, self.password, 'sale.order.line', 'read', [record["order_line"]], {'fields': ['product_id', 'product_uom_qty']})

            #flag_only_one = 1 if only_one_item else 0  # Solo admitir ordenes de un sku y 1 item si  only_one_item = True
            # num_skus = len(order_lines)
            # if num_skus <= 0 or (only_one_item and num_skus > 1):
            #     break

            products_data = []

            # Obtener los datos del producto por cada línea
            for line in order_lines:

                quantity_items = line['product_uom_qty']
                # if quantity_items <= 0 or (only_one_item and quantity_items > 1):
                #     # Bandera para indicar qu esta liena de la orden tiene mas de un item.
                #     # Decidir si rompe el for principal y no toma en cuenta toda la orden o si.
                #     quantity_items_0_flag = True
                #     break
                # else:
                #     quantity_items_0_flag = False

                product_id = line['product_id'][0]  # ID del producto
                product_fields = ['default_code', 'name','packing_length', 'packing_width', 'packing_height', 'packing_weight']
                product_data = self.models.execute_kw(self.db_name, self.uid, self.password, 'product.product', 'read', [product_id], {'fields': product_fields})

                if product_data:
                    product_info = product_data[0]

                    # //// Filtro para no tomar en cuenta las orden de envio que son: [C-ENVIO] Costo de Envio ////
                    sku_code = product_info.get('default_code', 'NONE')
                    product_name = product_info.get('name', 'NONE')
                    if "C-ENVIO" in sku_code or "Costo de Envio" in product_name:
                        logging.info(f"En {record['name']}, {sku_code} - {product_name} NO aplica para cotización")
                        continue
                    # ////////////////////////////////////////////////////////////////////////////////////////

                    products_data.append({
                        #'product_id': product_id,
                        'sku_code':  sku_code,
                        'product_name': product_info.get('name', 'NONE'),
                        'packing_weight': product_info.get('packing_weight', 0),
                        'packing_length': product_info.get('packing_length', 0),
                        'packing_width': product_info.get('packing_width', 0),
                        'packing_height': product_info.get('packing_height', 0),
                        'quantity_items': quantity_items
                    })

            order_info = {
                "id": record["id"],
                "name": record["name"],
                "shipper": shipper,
                "recipient": recipient,
                "products": products_data
            }
            orders_data_list.append(order_info)

        return orders_data_list

    def get_date_range(self, n_days=-1, n_hours=-1):
        today = datetime.now()

        # Determinar si usamos días o horas
        if n_days == -1 and n_hours == -1:
            n_hours = 3  # Valor por defecto de 3 horas si no se pasa nada
        elif n_days != -1 and n_hours != -1:
            n_days = -1  # Si se pasan ambos, se prioriza `n_hours`

        if n_days != -1:
            # Lógica basada en `n_days`
            start_date_system = (today - timedelta(days=n_days)).replace(hour=0, minute=0, second=0, microsecond=0)
            start_date = start_date_system + time_difference
            end_date_system = today.replace(hour=23, minute=59, second=59, microsecond=0)
            end_date = end_date_system + time_difference

        elif n_hours != -1:
            # Lógica basada en `n_hours`
            start_date_system = today - timedelta(hours=n_hours)
            start_date = start_date_system + time_difference
            end_date_system = today  # La fecha final es la actual
            end_date = end_date_system + time_difference

        # Logging
        #logging.info(f"Rango de búsqueda (local Odoo CDMX): {start_date.strftime('%Y-%m-%d %H:%M:%S')} al {end_date.strftime('%Y-%m-%d %H:%M:%S')}")
        logging.info(f"Rango de búsqueda (sistema): {start_date_system.strftime('%Y-%m-%d %H:%M:%S')} al {end_date_system.strftime('%Y-%m-%d %H:%M:%S')}")

        return start_date.strftime('%Y-%m-%d %H:%M:%S'), end_date.strftime('%Y-%m-%d %H:%M:%S')

    def get_zip_code_SR(self, company_id):
        fields = ['zip']
        company_address = self.models.execute_kw(self.db_name, self.uid, self.password, 'res.company', 'read', [company_id], {'fields': fields})
        return company_address[0]["zip"]

    def _get_address_SR(self, company_id):
        # Nombre, Compañia, email, celular, calle1, calle2, ciudad, estado_code, pais, zip
        adress_fields = ["name","name","email","phone","street","street2","city","state_id","country_id","zip"]
        company_address = self.models.execute_kw(self.db_name, self.uid, self.password, 'res.company', 'read', [company_id], {'fields': adress_fields})

        def clean_phone(phone):
            """Extrae solo los números antes del primer punto en un número de teléfono.
            input: '(55) 68 30 98 28. (55) 68 30 98 29'
            output: '5568309828'

            """
            if phone and isinstance(phone, str):
                match = re.match(r'[^.]+', phone)
                if match:
                    return "".join(re.findall(r'\d+', match.group()))
            return ""

        shipper_data = tuple(
            (
                # Extraer el valor dentro de paréntesis en state_id (posición 7)
                company_address[0][field][1].split("(")[-1].split(")")[0]
                if isinstance(company_address[0].get(field), list) and len(company_address[0][field]) > 1
                   and isinstance(company_address[0][field][1], str) and "(" in company_address[0][field][1]
                else False  # Si no cumple la condición, devolver False
            )
            if i == 7 else  # Solo aplica para `state_id`

            # Manejo de `country_id` (posición 8)
            ("MX" if company_address and company_address[0].get(field) and company_address[0][field][1] == "Mexico"
             else company_address[0][field][1] if company_address and company_address[0].get(field) else "MX")
            if i == 8 else

            # Aplicar limpieza al teléfono (posición 3)
            clean_phone(company_address[0].get(field))
            if i == 3 else

            # Para otros campos, usa la lógica normal
            (company_address[0][field] if company_address else None)
            for i, field in enumerate(adress_fields)
        )

        return shipper_data

    def _get_recipient_address(self,partner_id):
        # Nombre, Compañia, email, celular, calle1, calle2, ciudad, estado_code, pais, zip
        adress_fields = ["name","company_name","email","phone","street","street_number","city","state_id","country_id","zip"]
        contact = self.models.execute_kw(self.db_name, self.uid, self.password, 'res.partner', 'read',
                                         [partner_id], {'fields': adress_fields})

        # recipient_name = contact[0]["name"] if contact else None
        # recipient_company_name = contact[0]["company_name"] if contact else None
        # recipient_email = contact[0]["email"] if contact else None
        # recipient_phone = contact[0]["phone"] if contact else None
        # recipient_street_name = contact[0]["street_name"] if contact else None
        # recipient_street_number = contact[0]["street_number"] if contact else None
        # recipient_city = contact[0]["city"] if contact else None
        # recipient_state_id = contact[0]["state_id"] if contact else None
        # recipient_country_id = contact[0]["country_id"] if contact else 'MX'
        # recipient_zip = contact[0]["zip"] if contact else None

        recipient_data = tuple(
            (
                # Si el campo `state_id` es una lista válida con un segundo elemento tipo string que contiene paréntesis
                contact[0][field][1].split("(")[-1].split(")")[0]
                if isinstance(contact[0].get(field), list) and len(contact[0][field]) > 1
                   and isinstance(contact[0][field][1], str) and "(" in contact[0][field][1]
                else False  # Si no cumple la condición, devolver False
            )
            if i == 7 else  # Solo aplica la extracción para el campo `state_id`

            # Para los demás campos, aplicar la lógica normal
            (contact[0][field] if contact and contact[0].get(field) else "MX") if i == 8 else
            (contact[0][field] if contact else None)
            for i, field in enumerate(adress_fields)
        )

        return recipient_data


    def get_all_order_data(self, domain=['date_order', '>=', '1999-12-31 00:00:00'] , fields=['name']):
        self._connect()
        return self.models.execute_kw(self.db_name, self.uid, self.password, 'sale.order', 'search_read',[domain], {'fields': fields})

    def search_valpick_id(self, so_name, type_arg='/VALPICK/',extra_info=False):  # /VALPICK/  /PICK/  /OUT/
        self._connect()
        try:
            fields = ['id', 'name', 'message_attachment_count', 'message_main_attachment_id', 'message_ids']
            search_domain = [
                ('origin', '=', so_name),
                ('name', 'like', type_arg)
            ]

            #res = self.models.execute_kw(self.db_name, self.uid, self.password, 'stock.picking', 'search_read', [search_domain], {'fields': fields})
            res = self.models.execute_kw(self.db_name, self.uid, self.password, 'stock.picking', 'search_read',
                                         [search_domain], {'fields': fields})

            attachment_count = res[0]['message_attachment_count']
            id_valpick = res[0]['id']
            valpick_name = res[0]['name']

            return (valpick_name, id_valpick, attachment_count) if extra_info else id_valpick

        except Exception as e:
           logging.error('Error en search_valpick_id:' + str(e))
           return False

    @Utilities.measure_execution_time
    def inject_SRS_info_to_odoo(self, record, results, so_name):
        """
        Inserta la información del record y las cotizaciones en Odoo.
        :param record: dict, información de la orden y productos.
        :param results: list, cotizaciones procesadas por producto.
        """
        try:
            # -------- Filtro, si no hay una sola cotizacion no se inyecta info -----------
            if any(len(product_list_record) == 0 for product_list_record in results):
                logging.warning(f"Al menos un proucto para la orden {so_name} no tiene cotizaciones disponibles. NO se procesa (Revisar dimensiones del producto)")
                return False
            # ------------------------------------------------------------------------------
            # Obtener una nueva conexión para cada insersión de info
            new_uid, new_models = self._connect(new_connection=True)

            # Actualizar los códigos postales en la orden de venta
            new_models.execute_kw(
                self.db_name, new_uid, self.password,
                'sale.order', 'write', [[record['id']], {
                    'zip_code_shipper': record['shipper'][9],
                    'zip_code_recipient': record['recipient'][9], # record['recipient_zip'],
                    'wb_srs_flag': True
                }]
            )

            # Crear líneas de enrutamiento para los productos en la orden
            for product, product_quotes in zip(record["products"], results):
                # Insertar línea de enrutamiento para el producto
                routing_line_id = new_models.execute_kw(
                    self.db_name, new_uid, self.password,
                    'sale.order.routing.line', 'create', [{
                        'sale_order_id': record['id'],
                        'product_name_srs': product['product_name'],
                        'packing_length': product['packing_length'],
                        'packing_width': product['packing_width'],
                        'packing_height': product['packing_height'],
                        'packing_weight': product['packing_weight'],
                        'quantity': product['quantity_items'],
                        'sku_code': product['sku_code'],
                    }]
                )

                # Insertar cada cotización asociada a esta línea
                for idx, quote in enumerate(product_quotes, 1):
                    new_models.execute_kw(
                        self.db_name, new_uid, self.password,
                        'sale.order.shipping.option', 'create', [{
                            'routing_line_id': routing_line_id,
                            'sale_order_id': record['id'],
                            'index': idx,  # Índice para identificar las opciones
                            'carrier': quote['provider'],
                            'service_type': quote['token'],
                            'price': quote['amount'],
                            'package_quantities': quote['quantity'],
                            'platform': quote['source'],
                            'strategy': quote['strategy'],  # Nuevo campo estrategia
                            'smallest_dimension': quote.get('smallest_dimension', ""), # Nuevo campo de dimensión más pequeña
                            'total_cost': quote['total_cost'],  # Nuevo campo de precio total

                            # ///////////////////////////////////////////////////////////
                            'rate_id': quote['rate_id'],
                            'quote_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'status_rate': "Succesful",
                        }]
                    )
            return True
        except Exception as e:
            logging.error(f"Error al procesar la orden {record['name']}: {e}")

    @Utilities.measure_execution_time
    def get_srs_info_from_odoo(self, sale_order_id):
        """
        Obtiene la información general del envío y las cotizaciones de envío de una orden de venta en Odoo.
        :param sale_order_id: ID de la orden de venta en Odoo. No el SO
        :return: Diccionario con la información organizada.
        """
        try:
            new_uid, new_models = self._connect(new_connection=True)

            # Obtener las líneas de orden que se crearon con el SRS en el modelo sale.order.routing.line
            routing_lines = new_models.execute_kw(
                self.db_name, new_uid, self.password,
                'sale.order.routing.line', 'search_read',
                [[['sale_order_id', '=', sale_order_id]]],
                {'fields': ['id', 'sku_code', 'product_name_srs', 'packing_length', 'packing_width',
                            'packing_height', 'packing_weight', 'quantity', 'first_shipping_option']}
            )

            # Para cada línea de enrutamiento, obtener sus opciones de envío
            for line in routing_lines:
                line_id = line['id']

                # Obtener la info de las cotizaciones
                """
                rate_id:
                    - eship:            ID unico de la cotizacion
                    - fedex:            Tipo de servicio
                    - Paquetexpress:    Tipo de servicio
                    - DHL:              ...pendiente...
                """
                shipping_options = new_models.execute_kw(
                    self.db_name, new_uid, self.password,
                    'sale.order.shipping.option', 'search_read',
                    [[['routing_line_id', '=', line_id]]],
                    {'fields': ['id', 'index', 'platform', 'carrier', 'service_type', 'price', 'package_quantities',
                                'strategy', 'smallest_dimension', 'total_cost',
                                'rate_id', 'quote_date', 'status_rate']}
                )

                # Agregar las opciones de envio
                line['shipping_options'] = shipping_options

            return routing_lines

        except Exception as e:
            logging.error(f"Error al obtener información de la orden {sale_order_id}: {e}")
            return None

class Server1db():
    def __init__(self):
        #load_dotenv(dotenv_path)
        pass

    def get_db_connection(self):
        """Establece y devuelve una conexión a la base de datos."""
        return mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            # database=os.getenv("DB_NAME")
        )

    def get_data_orders_MELI(self, date):
        connection = self.get_db_connection()
        cursor = connection.cursor()

        # ML a acordar
        query = """
                SELECT a.order_id, a.pack_id
                FROM somos_reyes.ml_order_update a
                LEFT JOIN somos_reyes.ml_shipping b
                ON a.shipping_id = b.shipping_id

                WHERE b.logistic_type = 'NA'

                AND date(a.date_created) >= %s # '2025-03-01' # CREADAS A PARTIR DE una fecha
                AND date(date_status_delivered) = '0000-00-00 00:00:00' # NO ENTREGADAS
                AND a.status = 'paid' #NO CANCELADAS
                """

        values = (date,)

        cursor.execute(query, values)
        records = cursor.fetchall()

        cursor.close()
        connection.close()

        return records

    def get_data_orders_AMZ(self, date):
        connection = self.get_db_connection()
        cursor = connection.cursor()

        # Amazon selfship
        query = """
                SELECT amazonorderid,
                       DATE_SUB(purchasedate, INTERVAL 6 HOUR) AS created_ts_local,
                       orderstatus,
                       DATE_SUB(lastupdatedate, INTERVAL 6 HOUR) AS updated_ts_local,
                       fulfillmentchannel,
                       shipservicelevel,
                       shipmentservicelevelcategory,
                       DATE_SUB(earliestshipdate, INTERVAL 6 HOUR) AS ship_by_start,
                       DATE_SUB(latestshipdate, INTERVAL 6 HOUR) AS ship_by_end,
                       DATE_SUB(earliestdeliverydate, INTERVAL 6 HOUR) AS deliver_by_start,
                       DATE_SUB(latestdeliverydate, INTERVAL 6 HOUR) AS deliver_by_end,
                       numberofitemsunshipped,
                       1 AS scheduled
                FROM bi.amz_bf
                WHERE orderstatus = 'Unshipped' #'Shipped', 'Pending', 'Canceled'
                AND purchasedate > %s
                AND fulfillmentchannel = 'MFN'  # Envío a cargo del vendedor
                AND shipservicelevel <> 'std-ez-mx'  # Que no sean de Easy Ship
                ORDER BY ship_by_end, created_ts_local;
                    """

        query = """
                SELECT amazonorderid, 'None' AS pack_id
                FROM bi.amz_bf
                WHERE orderstatus = 'Unshipped'
                AND purchasedate > %s
                AND fulfillmentchannel = 'MFN'
                AND shipservicelevel <> 'std-ez-mx' # Que no sean de Easy Ship
                ORDER BY latestshipdate, purchasedate;
                """

        values = (date,)

        cursor.execute(query, values)
        records = cursor.fetchall()

        cursor.close()
        connection.close()

        return records

    def get_data_orders_shipofy(self, date):
        connection = self.get_db_connection()
        cursor = connection.cursor()

        # Shopify
        query = """
                    SELECT order_number, 'None' AS pack_id
                    FROM somos_reyes.shopify_orders_notes
                    WHERE financial_status = 'paid'
                    AND cancel_reason = 'None'
                    AND created_at > %s
                    AND SUBSTRING_INDEX(SUBSTRING_INDEX(payment_gateway_names, "'", -2), "'", 1) NOT IN ('manual', 'Bank Deposit');
                """

        values = (date,)

        cursor.execute(query, values)
        records = cursor.fetchall()

        cursor.close()
        connection.close()

        return records

    def build_odoo_domain(self, start_date, end_date, *marketplace_lists):
        """
        Construye un domain de búsqueda en Odoo con filtros dinámicos basados en múltiples listas de registros de marketplaces.

        :param start_date: Fecha de inicio del rango.
        :param end_date: Fecha de fin del rango.
        :param marketplace_lists: Listas de registros de marketplaces (ej. ml_records, amz_records).
        :return: Lista `so_domain` para filtrar en Odoo.
        """

        # Domain base con filtros de fecha y wb_srs_flag (estos SIEMPRE se aplican, con AND)
        so_domain = [
            ('date_order', '>=', start_date),
            ('date_order', '<=', end_date),
            ('wb_srs_flag', '=', False),
            ('data_tracking_readwrite', 'not ilike', 'TURBO'), # Se agrega para descartar envíos express por logística interna // 31-07-2025
        ]

        channel_refs = set()
        yuju_pack_ids = set()

        # Procesar todas las listas de órdenes (ML, Amazon, etc.)
        for records in marketplace_lists:
            for order in records:
                order_ref, pack_id = order  # Desempaquetar tupla
                channel_refs.add(order_ref)
                if pack_id and pack_id != 'None':  # Excluir si solo es 'None'
                    yuju_pack_ids.add(pack_id)

        # Construcción de condiciones OR dinámicas
        or_conditions = []

        if channel_refs:
            or_conditions.append(('channel_order_reference', 'in', list(channel_refs)))
        if yuju_pack_ids: # Solo agregar si hay valores válidos
            or_conditions.append(('yuju_pack_id', 'in', list(yuju_pack_ids)))

        # Condiciones OR fijas de los equipos
        team_conditions = [
            #('team_id', '=', 'Team_Sitioweb'),
            ('team_id', '=', 'Team_Elektra'),
            ('team_id', '=', 'Team_Coppel'),
        ]
        team_block = ['|'] * (len(team_conditions) - 1) + team_conditions

        # Construir el bloque OR final combinando team_id y dinámicos (si existen)
        if or_conditions:
            # Si hay más de una condición, agregamos '|' entre ellas
            or_block = ['|'] * (len(or_conditions) - 1) + or_conditions
            # Agregamos el OR de los team_id y las condiciones dinámicas
            final_or_condition = ['|'] + team_block + or_block
        else:
            final_or_condition = team_block

        # Unimos la parte AND (filtros fijos) con la parte OR
        so_domain = ['&'] + so_domain + final_or_condition

        return so_domain

    def get_all_marketplace_orders(self, start_date, start_date_odoo, end_date_odoo):
        """
        Llama dinámicamente a todos los métodos que obtienen órdenes de distintos marketplaces.
        Retorna el `so_domain` para Odoo.
        """
        marketplace_orders = []

        # Buscar métodos que empiezan con "get_data_orders_"
        for method_name in dir(self):
            if method_name.startswith("get_data_orders_"):  # Solo métodos relevantes
                method = getattr(self, method_name)  # Obtener método
                records = method(start_date)  # Llamarlo con el parámetro `start_date`
                marketplace_orders.append(records)

        # Llamar a `build_odoo_domain` con los resultados
        return self.build_odoo_domain(start_date_odoo, end_date_odoo, *marketplace_orders)


if __name__ == "__main__":
    #odoo_connection = OdooConnection(is_test=True)
    #records = odoo_connection.get_orders_list_info(10,-1)
    #logging.info(f"Numero de records: {len(records)}")
    #for record in records:
    #    logging.info(record)

    sr1 = Server1db()
    mkp_list = [('702-0472956-2617036', 'None'), ('702-9270384-8414609', 'None'), ('701-0588755-5912240', 'None'), ('701-1763916-1207411', 'None'), ('701-2996163-2318634', 'None'), ('701-5755588-1695425', 'None'), ('701-3480581-8845839', 'None'), ('701-3038321-8808246', 'None'), ('702-3246239-5923466', 'None'), ('701-7428596-6321833', 'None'), ('701-6188542-7359469', 'None'), ('702-2921976-8423420', 'None'), ('702-3632266-3691434', 'None'), ('701-7028125-4301028', 'None'), ('702-4841273-2725851', 'None'), ('702-8284578-5084243', 'None'), ('702-3024167-6267446', 'None'), ('701-9378405-7924204', 'None'), ('702-7687645-8428250', 'None'), ('702-8912794-8911464', 'None'), ('702-3030753-7148212', 'None'), ('701-5311745-4185810', 'None'), ('702-1985503-1472264', 'None'), ('701-8841994-1580233', 'None'), ('701-8135525-8251465', 'None'), ('701-2927476-1325818', 'None')]
    mkp_list2 = [('2000010944169804', 'None'), ('2000010950629958', 'None'), ('2000010955862202', 'None'), ('2000010962087786', 'None'), ('2000010978617146', 'None'), ('2000010984294334', 'None'), ('2000010994700466', 'None'), ('2000011000252748', 'None'), ('2000011000488436', 'None'), ('2000011001775756', 'None'), ('2000011003864668', 'None'), ('2000011005957478', 'None'), ('2000011012376352', 'None'), ('2000011013557138', 'None'), ('2000011016023026', 'None'), ('2000011017412086', 'None'), ('2000011017802968', 'None'), ('2000011020286556', 'None'), ('2000011023100312', 'None'), ('2000011024482446', 'None'), ('2000011024765144', 'None'), ('2000011025728480', 'None'), ('2000011028381264', 'None'), ('2000011029582164', 'None'), ('2000011031446926', 'None'), ('2000011031518336', 'None'), ('2000011032993188', 'None'), ('2000011037076358', 'None'), ('2000011037722376', 'None'), ('2000011042780724', 'None'), ('2000011043294858', 'None'), ('2000011045473716', 'None'), ('2000011045925032', 'None'), ('2000011048262970', 'None'), ('2000011049400578', 'None'), ('2000011050034876', 'None'), ('2000011050470070', 'None'), ('2000011051977740', 'None'), ('2000011052036044', 'None'), ('2000011053079948', 'None'), ('2000011058152516', 'None'), ('2000011063967916', 'None'), ('2000011065040024', 'None'), ('2000011065515414', 'None'), ('2000011067286022', 'None'), ('2000011076815004', 'None'), ('2000011081454566', 'None'), ('2000011082054642', 'None'), ('2000011084956636', 'None'), ('2000011087252700', 'None'), ('2000011095713622', 'None'), ('2000011096662210', 'None'), ('2000011098274406', 'None'), ('2000011100113312', 'None'), ('2000011100732392', 'None'), ('2000011105660732', 'None'), ('2000011106055798', 'None'), ('2000011108499252', 'None'), ('2000011108542980', 'None'), ('2000011110241348', 'None'), ('2000011110457940', 'None'), ('2000011110978952', 'None'), ('2000011117954738', 'None'), ('2000011119013740', 'None'), ('2000011119323218', 'None'), ('2000011120803230', 'None'), ('2000011120911878', 'None'), ('2000011120934124', 'None'), ('2000011125763286', 'None'), ('2000011126963706', 'None'), ('2000011128921110', 'None'), ('2000011129892400', 'None'), ('2000011129952448', 'None'), ('2000011130010804', 'None'), ('2000011130116302', 'None'), ('2000011130766156', 'None'), ('2000011136360916', 'None'), ('2000011139133700', 'None'), ('2000011142930340', 'None'), ('2000011144290752', 'None'), ('2000011144497120', 'None'), ('2000011147036036', 'None'), ('2000011147889196', 'None'), ('2000011151467358', 'None'), ('2000011155421150', 'None'), ('2000011156120112', 'None'), ('2000011156705334', 'None'), ('2000011156794138', 'None'), ('2000011157131214', 'None'), ('2000011158650908', 'None'), ('2000011160009178', 'None'), ('2000011160228862', 'None'), ('2000011164547858', 'None'), ('2000011171138704', 'None'), ('2000011171980760', 'None'), ('2000011173754172', 'None'), ('2000011174301272', 'None'), ('2000011179215488', 'None'), ('2000011179645494', 'None'), ('2000011180149698', 'None'), ('2000011180761654', 'None'), ('2000011181365540', 'None'), ('2000011186620048', 'None'), ('2000011190666210', 'None'), ('2000011193364890', 'None'), ('2000011196174754', 'None'), ('2000011197721912', 'None'), ('2000011197941486', 'None'), ('2000011202686624', 'None'), ('2000011202902282', 'None'), ('2000011203621912', 'None'), ('2000011204454542', 'None'), ('2000011208467874', 'None'), ('2000011212075830', 'None'), ('2000011213079704', 'None'), ('2000011216448078', 'None'), ('2000011217840038', 'None'), ('2000011218068978', 'None'), ('2000011218538166', 'None'), ('2000011218870796', 'None'), ('2000011219303128', 'None'), ('2000011219308414', 'None'), ('2000011219464192', 'None'), ('2000011221287716', 'None'), ('2000011222850698', 'None')]
    domain = sr1.build_odoo_domain('2024-12-31 00:00:00', '2024-12-31 23:59:59', mkp_list, mkp_list2)
    print(domain)

    # domain = [
    #     ('name', '=', 'SO3398088'),
    #     # ('team_id', '=', 'Team_MercadoLibre'),
    #     # ('date_order', '>=', '2024-12-31 00:00:00'),
    #     # ('date_order', '<=', '2024-12-31 23:59:59'),
    #     # ('data_tracking_readwrite', '!=', 'False')
    #
    # ]

    # //////////////////////////////////////////////////////////////////////////////////////////////

    # domain = [
    #     ('date_order', '>=', '2024-01-27 06:00:00'),
    #     ('date_order', '<=', '2025-01-28 05:59:59'),
    #     ('name', '=', 'SO3398097'),
    #     ('wb_srs_flag', '=', True)
    # ]
    #
    # conecction = OdooConnection()
    # fields = ['name','data_tracking_readwrite', 'wb_srs_flag', 'date_order']
    #
    # response = conecction.get_all_order_data(domain, fields)
    # for res in response:
    #     logging.info(res)

