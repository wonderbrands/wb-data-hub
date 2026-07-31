from datetime import datetime
import _01_apis_connections, _02_get_odoo_records_api
from concurrent.futures import ThreadPoolExecutor
import inspect
import logging
from _00_utilities import Utilities


class ShippingQuotation:
    def __init__(self, order_data, is_test=False, top_number=3):
        self.order_data = order_data
        self.is_test = is_test
        self.top_number = top_number
        # -----------------------------------------------------------------------------------
        # Nombre, Compañia, email, celular, calle1, calle2, ciudad, estado_code, pais, zip
        #self.shipper = ("None", "None", "None", "None", "None", "None", "None", "None", "MX", order_data['shipper_zip'])
        # self.recipient = ("None", "None", "None", "None", "None", "None", "None", "None", "MX", order_data['recipient_zip'])
        self.shipper = order_data['shipper']
        self.recipient = order_data['recipient']
        # -----------------------------------------------------------------------------------
        self.first_product = order_data['products'][0]
        self.measures = (self.first_product['packing_weight'], self.first_product['packing_length'],
                         self.first_product['packing_width'], self.first_product['packing_height'])
        self.so_name = order_data['name']
        self.today_date = datetime.now().strftime("%Y-%m-%d")
        self.smallest_dimentions_transform = {
            'packing_length': 'Por largo',
            'packing_width': 'Por ancho',
            'packing_height': 'Por alto',
        }

        # Registro dinámico de APIs
        self.apis = self._load_apis()

    def _load_apis(self):
        """
        Carga dinámicamente las APIs registradas. Agregar una nueva API solo requiere registrarla aquí.
        """
        api_classes = {
            "eship": _01_apis_connections.EShipAPI,
            "fedex": _01_apis_connections.FedExAPI,
            "paquetexpress": _01_apis_connections.PaquetexpressAPI
            # DHL...
        }

        # Instanciar cada API con el modo (test o producción)
        return {name: api_class(self.is_test) for name, api_class in api_classes.items()}
        # return {
        #     name: api_class(True if name == "paquetexpress" else self.is_test)  # Parche temporal para Paquetexpress (No se tienen credenciales de prod)
        #     for name, api_class in api_classes.items()
        # }

    def _generate_payloads(self, measures):
        """
        Payloads con parametros dinamicos.

        Aqui revisamos con inspect los parametros que requerimos de cada metodo de payloads.
        inspect.signature > diccionario con los nombres de los parámetros.
        """
        payloads = {}

        try:

            # Argumentos base comunes
            base_args = {
                "shipper": self.shipper,
                "recipient": self.recipient,
                "measures": measures,
                "so_name": self.so_name,
                "date": self.today_date,
                "fedex_account": self.apis["fedex"].fedex_account() if "fedex" in self.apis else None
            }

            for name, api in self.apis.items():
                # Obtener la lista de parámetros del método construct_quotation_payload
                method_params = inspect.signature(api.construct_quotation_payload).parameters
                # print(name, method_params)

                # Filtrar solo los argumentos que requiere el método construct_quotation_payload
                filtered_args = {key: value for key, value in base_args.items() if key in method_params}

                # Construir el payload con los argumentos específicos
                payloads[name] = api.construct_quotation_payload(**filtered_args)


        except Exception as e:
            logging.error('Error en carga de payloads: ', e)

        return payloads

    @Utilities.measure_execution_time
    def _fetch_quotes(self, payloads):
        """
        Obtiene las cotizaciones de todas las APIs registradas.
        """
        quotes = []
        for name, api in self.apis.items():
            payload = payloads.get(name)
            if not payload:
                logging.warning(f"No se generó payload para {name}.")
                continue

            try:
                # Consultar cotizaciones
                response = api.quote_rates(payload)
                if not self._validate_response(response, name):
                    continue

                # Procesar las cotizaciones válidas
                source = response.get("source", name)  # Obtener la paqueteria de ls response
                rates = response.get("rates", [])

                # Agregar 'source' a cada cotización
                for rate in rates:
                    rate["source"] = source
                    quotes.append(rate)

            except Exception as e:
                logging.error(f"Error al consultar la API {name}: {e}")
                pass

        return quotes

    def _validate_response(self, response, api_name):
        """
        Valida que la respuesta de la api tenga la estructura base que manejamos.
        Debe tener source y rates como llaves mas generales del json.
        :param response: Respuesta JSON de la API.
        :param api_name: Nombre de la API para logging.
        :return: Booleano indicando si es válida.
        """
        if not response or "rates" not in response:
            logging.warning(f"La respuesta de {api_name} no tiene cotizaciones válidas. {response}")
            return False
        return True

    def get_quotations(self):
        """Obtiene cotizaciones para paquetes individuales y combinados."""
        results = []

        for product in self.order_data['products']:
            # Caso: Solo calcular cotización individual si quantity_items == 1
            if product['quantity_items'] == 1:
                logging.info("////////////     1 solo item     ///////////")
                measures = (product['packing_weight'], product['packing_length'], product['packing_width'],
                            product['packing_height'])

                # Generar payloads dinámicamente
                payloads = self._generate_payloads(measures)

                # Consultar cotizaciones
                quotes = self._fetch_quotes(payloads)

                # Agregar metadatos y resultados
                for quote in quotes:
                    quote["strategy"] = "Un paquete"
                    quote["quantity"] = 1
                    quote["total_cost"] = quote["amount"]

                results.append({
                    "product_name": product['product_name'],
                    "sku_code": product['sku_code'],
                    "combined_quotes": [],  # No aplica combinaciones
                    "individual_quotes": quotes,
                })

            # /////////////////////// MAS DE UN ITEM POR SKU /////////////////////////////
            else:
                logging.info("////////////     VARIOS items     ///////////")
                combined_measures = self.calculate_package_measures(product)
                combined_weight = combined_measures['weight']
                combined_dimensions = combined_measures['dimensions']
                smallest_dim_key = combined_measures['smallest_dimension_key']

                # Generar payloads dinámicamente para el paquete combinado
                combined_payloads = self._generate_payloads(
                    (combined_weight, *combined_dimensions.values())
                )

                # Consultar cotizaciones para el paquete combinado
                combined_quotes = self._fetch_quotes(combined_payloads)

                for quote in combined_quotes:
                    quote["strategy"] = "Agrupar producto"
                    quote["smallest_dimension"] = self.smallest_dimentions_transform.get(smallest_dim_key, 'NO APLICA')
                    quote["total_cost"] = quote["amount"]
                    quote["quantity"] = 1

                # ------------------------------------------------------------------------------------------------------

                # Generar payloads dinámicamente para paquetes individuales
                measures = (product['packing_weight'], product['packing_length'],
                            product['packing_width'], product['packing_height'])
                individual_payloads = self._generate_payloads(measures)

                # Consultar cotizaciones para paquetes individuales
                individual_quotes = self._fetch_quotes(individual_payloads)

                for quote in individual_quotes:
                    quote["strategy"] = "Paquete por producto"
                    quote["quantity"] = product['quantity_items']
                    quote["total_cost"] = quote["amount"] * product['quantity_items']

                # Guardar resultados por producto
                results.append({
                    "product_name": product['product_name'],
                    "sku_code": product['sku_code'],
                    "combined_quotes": combined_quotes,
                    "individual_quotes": individual_quotes,
                })

        logging.info("------------------------------------------------------------------")
        sorted_result = self._sort_shipping_options(results)
        # print(sorted_result)

        return sorted_result

    def calculate_package_measures(self, product):
        """
        Calcula las medidas del paquete considerando productos con múltiples unidades.
        Identifica la dimensión más pequeña para reportar.
        :param product: Información del producto (diccionario).
        :return: Tuple con medidas ajustadas (peso, largo, ancho, alto, menor_dimensión).

        example:    entrada > {'sku_code': 'SGXMAS210', 'product_name': 'Arbol De Navidad 210 Cm Artificial Pino Navideño Adornos', 'packing_weight': 8.0, 'packing_length': 110.0, 'packing_width': 22.0, 'packing_height': 23.0, 'quantity_items': 2.0}
                    salida > (16.0, 110.0, 23.0, 44.0, 'packing_width')
        """
        quantity = int(product['quantity_items'])
        weight = product['packing_weight'] * quantity
        dimensions = {
            'packing_length': product['packing_length'],
            'packing_width': product['packing_width'],
            'packing_height': product['packing_height'],
        }

        # Encontrar la dimensión más pequeña
        smallest_dim_key = min(dimensions, key=dimensions.get)
        smallest_dim_value = dimensions[smallest_dim_key]

        # Crear las dimensiones combinadas realizando una copiia
        combined_dimensions = dimensions.copy()
        combined_dimensions[smallest_dim_key] = smallest_dim_value * quantity

        # Retornar las medidas combinadas como diccionario
        return {
            'weight': weight,
            'dimensions': combined_dimensions,
            # Mantenemos las claves originales, aunque podriamos modificar los nombres. Que ahgan sentido
            'smallest_dimension_key': smallest_dim_key
        }

    def _sort_shipping_options(self, results):
        """
        Ordena las opciones de envío (combinadas e individuales) por total_cost,
        simplifica la información y conserva los datos agregados después de obtener las cotizaciones.
        :param results: Lista de cotizaciones por producto.
        :return: Lista de cotizaciones ordenadas y simplificadas.
        """
        sorted_results = []

        for product_data in results:
            # Combinar todas las opciones de envío (combinadas e individuales)
            all_quotes = product_data["combined_quotes"] + product_data["individual_quotes"]

            # Simplificar las cotizaciones mientras conservamos los metadatos agregados después de las respuestas de las APIs
            simplified_quotes = [
                {
                    "rate_id": quote["rate_id"], # Obtenemos el id de cada cotizacion, independiente del carrier
                    "amount": quote["amount"],  # Directamente accedes a los valores del JSON como en price_compare
                    "provider": quote["provider"],
                    "token": quote.get("servicelevel", {}).get("token", ""),
                    # Si no hay servicelevel, el valor es vacío
                    "source": quote.get("source", "unknown"),
                    "strategy": quote.get("strategy"),
                    "smallest_dimension": quote.get("smallest_dimension", 'NO APLICA'),
                    "quantity": quote.get("quantity"),
                    "total_cost": quote.get("total_cost"),
                }
                for quote in all_quotes
            ]

            # Ordenar por total_cost (igual que en price_compare, pero usando "total_cost" en vez de "amount")
            sorted_quotes = sorted(simplified_quotes, key=lambda x: x["total_cost"])

            # Aplicando el top_number si es válido
            if isinstance(self.top_number, int) and self.top_number > 0:
                sorted_quotes = sorted_quotes[:self.top_number]

            # Agregar las cotizaciones ordenadas al resultado final
            sorted_results.append(sorted_quotes)

        return sorted_results

class OrderProcessor:
    def __init__(self, carrier_apis_test=False, odoo_test=False, top_number=3):
        self.carrier_apis_test = carrier_apis_test
        self.odoo_test = odoo_test
        self.top_number = top_number
        self.odoo_connection = _02_get_odoo_records_api.OdooConnection(is_test=self.odoo_test)

    @Utilities.trace_thread
    def _process_record(self, record):
        """Procesa una orden individual.
            :param record: Diccionario con la info de una orden.
                ex: {'id': 1397801, 'name': 'SO3398096', 'shipper_zip': '54010', 'recipient_zip': '47463',
                'products': [{'sku_code': 'SGXMAS210', 'product_name': 'Arbol De Navidad 210 Cm Artificial Pino Navideño Adornos',
                'packing_weight': 8.0, 'packing_length': 110.0, 'packing_width': 22.0, 'packing_height': 23.0, 'quantity_items': 1.0},
                 {'sku_code': '125206', 'product_name': 'Colchon Inflable Individual Go Campamento 2000024590 Coleman',
                 'packing_weight': 2.4, 'packing_length': 33.0, 'packing_width': 8.0, 'packing_height': 28.0, 'quantity_items': 1.0}]}

        """

        try:
            so_name = record['name']
            logging.info(f"Procesando la orden {so_name}") # , \n{record}")
            quotation = ShippingQuotation(record, self.carrier_apis_test, self.top_number)
            results = quotation.get_quotations()

            # logging.info("\nCotizaciones obtenidas por producto:")
            # for product_idx, product_quotes in enumerate(results, 1):
            #     product = record["products"][product_idx - 1]
            #     logging.info(f"Producto {product_idx}: {product['product_name']} (SKU: {product['sku_code']})")
            #
            #     # Imprimir cada cotización
            #     for idx, quote in enumerate(product_quotes, 1):
            #         logging.info(f"  Opción {idx}:")
            #         logging.info(f"      Proveedor: {quote['provider']}")
            #         logging.info(f"      Token: {quote['token']}")
            #         logging.info(f"      Precio individual: {quote['amount']} MXN")
            #         logging.info(f"      Cantidad: {quote['quantity']}")
            #         logging.info(f"      Plataforma: {quote['source']}")
            #         logging.info(f"      Agrupar items por: {quote['smallest_dimension']}")
            #         logging.info(f"      Estrategia: {quote['strategy']}")
            #         logging.info(f"      Costo total: {quote['total_cost']} MXN")

            info_to_odoo = self.odoo_connection.inject_SRS_info_to_odoo(record, results, so_name)

            if info_to_odoo:
                logging.info(f"Orden {so_name} procesada, información en Odoo insertada")


        except Exception as e:
            logging.error(f"Error en la solicitud para: {record['name']}, {e}")

    def process_orders(self, num_days=-1, num_hours=-1, max_workers=None):  # Si no se reciben parámetros explicitos de los días, se colocan en -1
        """Procesa múltiples órdenes en paralelo."""
        records = self.odoo_connection.get_orders_list_info(num_days,num_hours)

        # Paralelización usando ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as executor:  # max_workers=N
            executor.map(self._process_record, records)


if __name__ == "__main__":
    order_processor = OrderProcessor(carrier_apis_test=False, odoo_test=True, top_number=3)
    order_processor.process_orders(num_days=360, max_workers=1)

    # test_data = {'id': 1397801, 'name': 'SO3398096', 'shipper_zip': '54010', 'recipient_zip': '47463', 'products': [{'sku_code': 'SGXMAS210', 'product_name': 'Arbol De Navidad 210 Cm Artificial Pino Navideño Adornos', 'packing_weight': 8.0, 'packing_length': 110.0, 'packing_width': 22.0, 'packing_height': 23.0, 'quantity_items': 2.0}, {'sku_code': '125206', 'product_name': 'Colchon Inflable Individual Go Campamento 2000024590 Coleman', 'packing_weight': 2.4, 'packing_length': 33.0, 'packing_width': 8.0, 'packing_height': 28.0, 'quantity_items': 1.0}]}
    # #test_measures = ShippingQuotation(test_data,False,top_number=3)
    # prodcut_info_test = test_data['products'][0]
    # #print(test_measures.calculate_package_measures(prodcut_info_test))
    # #test_measures.get_quotations()
    #
    # quotation = ShippingQuotation(test_data, False, 3)
    # results = quotation.get_quotations()
    # print(results)
