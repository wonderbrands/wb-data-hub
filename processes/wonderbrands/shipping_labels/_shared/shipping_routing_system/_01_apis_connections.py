import requests
import json
import os
from dotenv import load_dotenv
import logging
import time as tm
from abc import ABC, abstractmethod
from _00_utilities import dotenv_path

class ShippingAPI(ABC):
    """Interfaz base para todas las APIs de cotización.
    Heredan las clases de paqueterias:

    - eship         :   listo
    - fedex         :   listo
    - paquetexpress :   listo
    - dhl           :   pendiente
    .
    .
    .
    """

    @abstractmethod
    def _load_api_connection(self, **kwargs):
        pass

    @abstractmethod
    def construct_quotation_payload(self, **kwargs):
        pass

    @abstractmethod
    def quote_rates(self, payload):
        pass

    # //////////////////////////////////////////////////////////////////////
    # Para generacion de las guias Eric
    @abstractmethod
    def construct_generation_label_payload(self, **kwargs):
        pass

    @abstractmethod
    def get_label(self, **kwargs):
        pass


class EShipAPI(ShippingAPI):
    def __init__(self, is_test):
        self.is_test = is_test
        self.api_type = "API_KEY_eShip_TEST" if is_test else "API_KEY_eShip_PROD"
        self.base_url = "https://apiqa.myeship.co" if is_test else "https://api.myeship.co"
        self.api_key = self._load_api_connection()
        self.headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key
        }

    def _load_api_connection(self):
        load_dotenv(dotenv_path)
        api_key = os.getenv(self.api_type)
        if not api_key:
            logging.error("API Key no encontrada en las variables de entorno")
        return api_key

    def quote_rates(self, payload):
        """Realiza una solicitud a la API de eShip con reintentos en caso de error 521."""
        endpoint = "/rest/quotation"
        url = self.base_url + endpoint

        try:
            response = requests.post(url, json=payload, headers=self.headers)

            if response.status_code == 200:
                json_response = response.json()
                json_response["source"] = "eship"
                return json_response

            # Manejo de error 521 (servidor caído) con reintentos
            if response.status_code == 521:
                for i in range(1, 6):  # Intentar hasta 5 veces
                    logging.warning(f"Error 521 en eShip (api.myeship.co / Web server is down) - Reintento {i}/5")
                    tm.sleep(0.4)
                    response = requests.post(url, json=payload, headers=self.headers)

                    if response.status_code == 200:
                        json_response = response.json()
                        json_response["source"] = "eship"
                        return json_response

                logging.error("Error 521 en eShip - Se agotaron los reintentos.")

            # Manejo de otros códigos de estado inesperados
            response.raise_for_status()

        except requests.RequestException as e:
            logging.error(f"Error en la solicitud eShip: {e}")

        return None  # Retorna None si no se obtuvo una respuesta válida

    def construct_quotation_payload(self, shipper, recipient, measures, so_name):
        """Delegar la construcción del payload al método estático de eship."""
        return PayloadConstructor.construct_eship_quotation_payload(
            shipper=shipper,
            recipient=recipient,
            measures=measures,
            so_name=so_name
        )


    # //////////////////////////////////////////////////////////////////////
    ############# HECHO POR ERIC #############
    def construct_generation_label_payload(self, rate_id):
        """Delegar la construcción del payload al método estático de eship."""
        return PayloadConstructor.construct_eship_shipment_payload(
            rate_id=rate_id
        )

    def get_label(self, payload):
        try:
            # Configuro la salida de guías en PDF_4x6.
            json_label = {
                "label_format": "PDF_4x6",
                "label_firstline": "SOMOS REYES",
                "label_secondline": "NA"
            }

            requests.post(url=self.base_url + '/rest/label_settings', json=json_label, headers={
                "Content-Type": "application/json",
                "api-key": self.api_key
            })

            endpoint = "/rest/shipment"
            response = requests.post(self.base_url + endpoint, json=payload, headers=self.headers)
            response.raise_for_status()
            json_response = response.json()
            json_response["source"] = "eship"
            tracking_number = json_response["tracking_number"]
            pdf_url = json_response["label_url"]
            return {'complete_response': json_response,
                    'tracking_number': tracking_number,
                    'pdf_url': pdf_url,
                    'zpl': None}

        except requests.RequestException as e:
            logging.error(f"Error en la solicitud: {e}")
            return None

    ############# HECHO POR ERIC FIN#############

class FedExAPI(ShippingAPI):
    def __init__(self, is_test):
        self.is_test = is_test
        self.api_type = "fedex_key_test" if is_test else "fedex_key_prod"
        self.secret_type = "fedex_secret_test" if is_test else "fedex_secret_prod"
        self.base_url = "https://apis-sandbox.fedex.com" if is_test else "https://apis.fedex.com"
        self.url_carta_porte = "https://wsbeta.fedex.com/LAC/ServicesAPI/mx/cartaporte/customers" if is_test else "https://ws.fedex.com/LAC/ServicesAPI/mx/cartaporte/customers"
        self.api_key, self.client_secret = self._load_api_connection()

    def _load_api_connection(self):
        load_dotenv(dotenv_path)
        api_key = os.getenv(self.api_type)
        client_secret = os.getenv(self.secret_type)
        if not api_key and not client_secret:
            logging.error("API Key o CLient Key no encontrada en las variables de entorno")
        else:
            return api_key, client_secret

    def _outh_fedex(self):

        # ///////////////////////// Auth FedEx /////////////////////////
        url = (self.base_url + "/oauth/token")

        payload = 'grant_type=client_credentials&client_id=' + self.api_key + '&client_secret=' + self.client_secret
        headers = {
            'Content-Type': "application/x-www-form-urlencoded"
        }
        response = requests.post(url, data=payload, headers=headers)
        authorization = (response.json()['access_token'])

        return authorization

    def quote_rates(self, payload):

        url = (self.base_url + "/rate/v1/rates/quotes")

        headers = {
            'Content-Type': "application/json",
            'X-locale': "es_MX",
            'Authorization': "Bearer " + self._outh_fedex()  # Retorna el autorization
        }

        try:
            response = requests.post(url, data=payload, headers=headers)
            if response.status_code == 200:
                return self._transform_response(data=response.json())
            else:
                logging.warning(f"Error en la solicitud FedEx: {response.status_code} / {response.text}")
                # print(response.text)
                return None
        except Exception as e:
            logging.error(f"Ocurrió un error: {e}")
            return False

    def _transform_response(self, data):
        # Transformar el response de FedEx para alinearlo con el formato esperado
        try:
            output_json = {
                "source": "fedex",
                "rates": []}
            # data = json.loads(data)

            for rate in data["output"]["rateReplyDetails"]:
                # Extraer datos base
                total_base_charge = rate["ratedShipmentDetails"][0]["totalBaseCharge"]
                fuel_charge = next(
                    (s["amount"] for s in rate["ratedShipmentDetails"][0]["shipmentRateDetail"]["surCharges"] if
                     s["type"] == "FUEL"), 0)
                tax_charge = next(
                    (s["amount"] for s in rate["ratedShipmentDetails"][0]["shipmentRateDetail"]["taxes"] if
                     s["type"] == "VAT"), 0)
                total_amount = round((total_base_charge + fuel_charge + tax_charge) , 2)

                # Construir estructura deseada
                transformed_rate = {
                    "rate_id": rate["serviceType"],
                    "amount": total_amount,
                    "servicelevel": {
                        "name": rate["serviceName"],
                        "token": rate["serviceType"]
                    },
                    "provider": "FedEx",
                    "currency": "MXN",
                    "breakdown": {
                        "base_charge": total_base_charge,
                        "fuel_charge": fuel_charge,
                        "total_tax": tax_charge
                    }
                }
                output_json["rates"].append(transformed_rate)

            return output_json

        except KeyError as e:
            logging.error(f"Clave faltante: {e}")
        except json.JSONDecodeError as e:
            logging.error(f"Error al decodificar JSON: {e}")
        except Exception as e:
            logging.error(f"Ocurrió un error inesperado: {e}")

    def fedex_account(self):
        return os.getenv("fedex_account_test") if self.is_test else os.getenv("fedex_account_prod")

    def construct_quotation_payload(self,fedex_account, date, shipper, recipient, measures):
        """Delegar la construcción del payload al método estático de eship."""
        return PayloadConstructor.construct_fedex_quotation_payload(
            fedex_account=fedex_account,
            date=date,
            shipper=shipper,
            recipient=recipient,
            measures=measures,
        )


    # //////////////////////////////////////////////////////////////////////
    ############# HECHO POR ERIC #############
    def construct_generation_label_payload(self,fedex_account, service_type, shipper, recipient, measures):
        """Delegar la construcción del payload al método estático de FedEx."""
        return PayloadConstructor.construct_fedex_shipment_payload(
            fedex_account=fedex_account,
            service_type=service_type,
            shipper=shipper,
            recipient=recipient,
            measures=measures
        )

    def get_label(self, payload):

        url = (self.base_url + "/ship/v1/shipments")

        headers = {
            'Content-Type': "application/json",
            'X-locale': "es_MX",
            'Authorization': "Bearer " + self._outh_fedex()  # Retorna el autorization
        }

        try:
            response = requests.post(url, data=payload, headers=headers)
            if response.status_code == 200:
                response_json = response.json()

                url = str(response_json["output"]["transactionShipments"][0]["pieceResponses"][0]["packageDocuments"][0]["url"])
                tracking_number = str(response_json["output"]["transactionShipments"][0]["masterTrackingNumber"])
                # Obtengo el ZPL.
                zpl = self.download_fedex_label(url=url)
                if zpl:
                    # Devuelvo la respuesta.
                    return {'complete_response': response_json,
                            'tracking_number': tracking_number,
                            'zpl': zpl,
                            'pdf_url': None}
                else:
                    print('Error al obtener el ZPL.')
                    return None
            else:
                logging.warning(f"Error en la solicitud: {response.status_code}")
                # print(response.text)
                return None

        except Exception as e:
            logging.error(f"Ocurrió un error: {e}")
            return False

    def download_fedex_label(self, url):
        """
        Descarga un archivo desde la URL proporcionada y almacena su contenido en el atributo zpl.
        """
        try:
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/octet-stream"
            }
            response = requests.get(url, headers=headers)
            response.raise_for_status()  # Lanza un error si la solicitud falla
            zpl = response.content.decode("utf-8", errors="ignore")  # Decodifica el contenido binario a texto
            return zpl

        except requests.exceptions.RequestException as e:
            print(f"Error al descargar la etiqueta: {e}")
            return None

    def carta_porte(self, data_sat, tracking_number, measures):
        payload_carta_porte = {
            "customerCartaPorteFDX": {
                "guia": str(tracking_number),  # NUMERO DE GUIA
                "ubicaciones": {
                    "ubicacion": [
                        {
                            "origen": {
                                "rfcRemitente": "SRE1410278MA",  # SRE1410278MA (ES EL DE SOMOS REYES).
                                "numRegIdTrib": "",  # No se debe usar. Solo para envios internacionales.
                                "residenciaFiscal": "MEX"
                            }
                        },
                        {
                            "destino": {
                                "rfcDestinatario": "XAXX010101000",  # USAMOS EL GENERICO (PUBLICO EN GENERAL).
                                "numRegIdTrib": "",  # No se debe usar. Solo para envios internacionales.
                                "residenciaFiscal": "MEX"
                            }
                        }
                    ]
                },
                "mercancias": {
                    "numTotalMercancias": 1,
                    # En este caso es siempre 1 porque son los únicos casos que el script hace.
                    "mercancia": [
                        {
                            "bienesTransp": str(data_sat[0]),  # UNSPSC Code, de la ficha del producto Odoo.
                            "descripcion": str(data_sat[0]),  # UNSPSC Code, de la ficha del producto Odoo.
                            "cantidad": 1,
                            # En este caso es siempre 1 porque son los únicos casos que el script hace.
                            "claveUnidad": "H87",
                            # Fijamos unidad de venta, según comentarios de Carlos Rizzo 6/3/24.
                            "pesoEnKg": float(measures[0]),
                            "valorMercancia": float(data_sat[1]),  # EL PRECIO DE VENTA.
                            "moneda": "MXN",
                            "materialPeligroso": "NO",
                            # No hay ningún producto en el catálogo que sea material peligroso.
                            "cveMaterialPeligroso": "",
                            # No se debe usar. Solo para envios de material peligroso.
                            "embalaje": "",  # No se debe usar. Solo para envios de material peligroso.
                            "fraccionArancelaria": "",  # No se debe usar. Solo para envios internacionales.
                            "uuidComercioExt": ""  # No se debe usar. Solo para envios internacionales.
                        }
                    ]
                }
            }
        }

        headers = {
            'Content-Type': "application/json",
            'X-locale': "es_MX"
        }

        response_cp = requests.post(self.url_carta_porte, json=payload_carta_porte, headers=headers)

        if response_cp.status_code == 200:
            logging.info(f'Se ha generado la carta porte de manera exitosa.')
            return response_cp.status_code
        else:
            logging.error(f'No se pudo generar la carta porte.')
            return None

    ############# HECHO POR ERIC FIN #############

class PaquetexpressAPI(ShippingAPI):
    def __init__(self, is_test):
        self.is_test = is_test
        self.user_type = "paquetexpress_user_test" if is_test else "paquetexpress_user_prod"
        self.password_type = "paquetexpress_password_test" if is_test else "paquetexpress_password_prod"
        self.type_api = "paquetexpress_type_test" if is_test else "paquetexpress_type_prod"
        self.token_type = "paquetexpress_token_test" if is_test else "paquetexpress_token_prod"
        self.password_login_type = "paquetexpress_password_login_test" if is_test else "paquetexpress_password_login_prod"

        self.base_url = "https://qaglp.paquetexpress.com.mx/WsQuotePaquetexpress/api" if is_test else " https://cc.paquetexpress.com.mx/WsQuotePaquetexpress/api"
        self.rad_url = "https://qaglp.paquetexpress.com.mx/RadRestFul/api" if is_test else "https://cc.paquetexpress.com.mx/RadRestFul/api"
        self.api_user, self.api_password, self.api_type, self.api_token, self.api_password_login = self._load_api_connection()

    def _load_api_connection(self):
        load_dotenv(dotenv_path)
        api_user = os.getenv(self.user_type)
        api_password = os.getenv(self.password_type)
        api_type = os.getenv(self.type_api)
        api_token = os.getenv(self.token_type)
        api_password_login = os.getenv(self.password_login_type)

        if not all([api_user, api_password, api_type, api_token]):
            logging.error("Conexión no establecida, revisar credenciales")
            api_user, api_password, api_type, api_token, api_password_login = 'x', 'x', 0, 'x', 'x' # Valores por default

        return api_user, api_password, api_type, api_token, api_password_login

    def _construct_header(self):
        return {
            "security": {
                "user": self.api_user,
                "password": self.api_password,
                "type": int(self.api_type),
                "token": self.api_token
            },
            "device": {
                "appName": "Test",
                "type": "Web",
                "ip": "177.240.106.167",
                "idDevice": "1"
            },
            "target": {
                "module": "QUOTER",
                "version": "1.0",
                "service": "quoter",
                "uri": "quotes",
                "event": "R"
            },
            "output": "JSON",
            "language": None
        }

    def quote_rates(self, payload):

        payload_complete = {"header": self._construct_header(), "body": payload["body"]}

        try:
            response = requests.post(f"{self.base_url}/apiQuoter/v2/getQuotation", json=payload_complete)
            if response.status_code == 200 and (response.json()["body"]["response"]["success"] == True):
                return self._transform_response(data=response.json())
            elif response.status_code == 200:
                message = response.json()["body"]["response"]["messages"][0]["description"]
                logging.warning(f"No hay cotizaciones disponibles para Paquetexpress: {message}")
            else:
                logging.warning(f"Error en la solicitud Paquetexpress: {response.status_code} / {response.text}")
                # print(response.text)
                return None
        except requests.RequestException as e:
            logging.error(f"Error en la solicitud a Paquetexpress: {e}")
            return None

    def _transform_response(self, data):
        """
        Transforma la respuesta de PaqueteExpress para adaptarla al formato estándar que estoy usando (response simplificada de eship).
        """
        try:
            output_json = {
                "source": "paquetexpress",
                "rates": []
            }

            # Validar la estructura de la respuesta
            quotations = data.get("body", {}).get("response", {}).get("data", {}).get("quotations", [])
            if not quotations:
                return {"source": "paquetexpress", "rates": [], "error": "No se encontraron cotizaciones"}

            # Procesar cada cotización
            for quotation in quotations:

                # Extraer cargos base
                total_base_charge = quotation["amount"]["shpAmnt"]
                discount = quotation["amount"]["discAmnt"]
                service_quote = quotation["amount"]["srvcAmnt"]
                tax_charge = quotation["amount"]["taxAmnt"]
                tax_charge_refund = quotation["amount"]["taxRetAmnt"]

                # total_amount = round(total_base_charge + tax_charge - discount, 2)

                # Extraer el total directamente, las sumas y restas de los cargos ya están incluidos ahí.
                total_amount = quotation["amount"]["totalAmnt"]

                # Extraer datos del servicio
                transformed_rate = {
                    "rate_id": quotation["id"],
                    "amount": total_amount,
                    "servicelevel": {
                        "name": quotation["serviceName"],
                        "token": quotation["serviceType"]
                    },
                    "provider": "PaqueteExpress",
                    "currency": "MXN",
                    "breakdown": {
                        "base_charge": total_base_charge,
                        "total_tax": round(float(tax_charge) - float(tax_charge_refund), 2),
                        "discount": discount,
                        "service_quote": service_quote
                    }
                }

                # Agregar al listado de rates
                output_json["rates"].append(transformed_rate)

            return output_json

        except KeyError as e:
            logging.error(f"Clave faltante en la respuesta de PaqueteExpress: {e}")
            return {"source": "paquetexpress", "rates": [], "error": f"Clave faltante: {str(e)}"}

    def construct_quotation_payload(self, shipper, recipient, measures):
        """Delegar la construcción del payload al método estático correspondiente."""
        return PayloadConstructor.construct_paquetexpress_quotation_payload(
            shipper=shipper,
            recipient=recipient,
            measures=measures
        )



    # //////////////////////////////////////////////////////////////////////
    ############# HECHO POR ERIC #############
    def get_auth_token(self):
        """ Obtiene un token de autenticación para las solicitudes. """
        url_login = f"{self.rad_url}/rad/loginv1/login"
        payload = {
            "header": {
                "security": {
                    "user": self.api_user,
                    "password": self.api_password_login
                }
            }
        }

        try:
            response = requests.post(url_login, json=payload)
            if response.status_code == 200:
                data = response.json()
                # print(response)
                # print(data)
                return data["body"]["response"]["data"]["token"]
            else:
                logging.error(f"Error al obtener el token: {response.status_code} - {response.text}")
                return None
        except requests.RequestException as e:
            logging.error(f"Excepción en la solicitud del token: {e}")
            return None


    def construct_generation_label_payload(self, service_type, shipper, recipient, measures, data_sat):
        """Delegar la construcción del payload al método estático de FedEx."""
        return PayloadConstructor.construct_paquetexpress_shipment_payload(
            service_type=service_type,
            shipper=shipper,
            recipient=recipient,
            measures=measures,
            data_sat=data_sat
        )


    def get_label(self, payload):
        """ Genera la guía con la carta porte. """
        # Genero el Token
        token = self.get_auth_token()
        if not token:
            return {"error": "No se pudo obtener el token"}
        # Defino la URL para obtener la guía.
        url = f"{self.rad_url}/rad/v1/guia"
        # Armo el payload completo con el encabezado.
        payload_complete = {
            "header": {
                "security": {
                    "user": self.api_user,
                    "type": 0,
                    "token": token
                }
            },
            "body": {
                "request": {
                    "data": [payload],
                    "objectDTO": None
                },
                "response": None
            }
        }
        # Hago la solicitud.
        try:
            response = requests.post(url, json=payload_complete)
            # Si la solicitud fue exitosa obtengo el ZPL.
            if response.json()['body']['response']['success']:
                data = response.json()
                tracking_number = data["body"]["response"]["data"]
                folio_carta_porte = data["body"]["response"]["objectDTO"].split(':')[1]
                # Obtengo el ZPL y la respuesta completa.
                complete_response = self.get_label_zpl(tracking_number=tracking_number,
                                                  token=token)
                # Si se pudo obtener el ZPL lo retorno.
                if complete_response:
                    return {'complete_response': complete_response,
                            'zpl': complete_response['retornoSolicitud']['cadenaImpresion'],
                            'tracking_number': folio_carta_porte,
                            'pdf_url': None}
                # Si no se pudo obtener el ZPL retorno None.
                else:
                    return None
            else:
                logging.warning(f"Error al generar la guía: {response.status_code} - {response.json()}")
                return None

        except requests.RequestException as e:
            logging.error(f"Excepción en la solicitud de generación de guía: {e}")
            return None


    def get_label_zpl(self, token, tracking_number):
        """
        Obtiene el código ZPL para la impresión de la etiqueta de Paquetexpress.

        :param tracking_number: Número de rastreo obtenido tras generar la guía.
        :param token: Token generado previamente, si venció lo vuelve a generar automáticamente.
        :return: Código ZPL si la solicitud es exitosa, None en caso de error.
        """

        # token = self.get_auth_token()
        # if not token:
        #     return {"error": "No se pudo obtener el token"}

        url = f"{self.rad_url}/rad/v1/infotrack"

        payload = {
            "header": {
                "security": {
                    "token": token,
                    "user": self.api_user
                }
            },
            "body": {
                "request": {
                    "data": {
                        "header": {},
                        "solicitudEnvio": {
                            "datosAdicionales": {
                                "datoAdicional": [
                                    {
                                        "claveDataAd": "getZPL",
                                        "valorDataAd": "1"
                                    }
                                ]
                            },
                            "rastreo": tracking_number
                        }
                    }
                }
            }
        }

        try:
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                data = response.json()
                if data["body"]["response"]["success"]:
                    return data["body"]["response"]["data"]
                else:
                    logging.error(f"Error en la respuesta de Paquetexpress: {data}")
                    return None
            else:
                logging.error(f"Error en la solicitud ZPL: {response.status_code}")
                return None
        except requests.RequestException as e:
            logging.error(f"Excepción en la solicitud de ZPL: {e}")
            return None



    ############# HECHO POR ERIC FIN #############

class DHL(ShippingAPI):
    def __init__(self, *args):
        pass


class PayloadConstructor:
    @staticmethod
    def construct_eship_quotation_payload(shipper, recipient, measures, so_name):
        return {
            "address_from": {
                "name": shipper[0],
                "company": shipper[1],
                "email": shipper[2],
                "phone": shipper[3],
                "street1": shipper[4],
                "street2": shipper[5],
                "city": shipper[6],
                "state": shipper[7],
                "country": shipper[8],
                "zip": shipper[9]
            },
            "address_to": {
                "name": recipient[0],
                "company": recipient[1],
                "email": recipient[2],
                "phone": recipient[3],
                "street1": recipient[4],
                "street2": recipient[5],
                "city": recipient[6],
                "state": recipient[7],
                "country": recipient[8],
                "zip": recipient[9]
            },
            "parcels": [
                {
                    "length": measures[1],
                    "height": measures[3],
                    "width": measures[2],
                    "distance_unit": "cm",
                    "weight": measures[0],
                    "mass_unit": "kg",
                    "reference": "Reference 1"
                }
            ],
            "order_info": {
                "order_num": so_name,
                "shipment_type": "Next Day",
                "status": 0,
                "paid": 1
            }
        }

    @staticmethod
    def construct_fedex_quotation_payload(fedex_account, date, shipper, recipient, measures):

        payload = {
        "accountNumber": {
            "value": fedex_account
        },
        "requestedShipment": {
            "shipper": {
                "address": {
                    "postalCode": shipper[9],
                    "countryCode": "MX",

                }
            },
            "recipient": {
                "address": {
                    "postalCode": recipient[9],
                    "countryCode": "MX",

                }
            },
            # "serviceType": "STANDARD_OVERNIGHT", # Si no se coloca, devuelve todas las opciones disponibles
            "preferredCurrency": "MXN",  # NMP/MXN
            'X-locale': "es_MX",
            "rateRequestType": [
                "ACCOUNT",
                "LIST",
                "PREFERRED",
            ],
            "shipDateStamp": date,
            "pickupType": "DROPOFF_AT_FEDEX_LOCATION",
            # "CONTACT_FEDEX_TO_SCHEDULE" "DROPOFF_AT_FEDEX_LOCATION" "USE_SCHEDULED_PICKUP"
            "requestedPackageLineItems": [
                {
                    "weight": {
                        "units": "KG",
                        "value": measures[0]
                    },
                    "dimensions": {
                        "length": measures[1],
                        "width": measures[2],
                        "height": measures[3],
                        "units": "CM"
                    }
                }
            ],
            "packagingType": "YOUR_PACKAGING"
        },
        "carrierCodes": [
            "FDXG",
            "FDXE"
        ]
    }

        payload = json.dumps(payload)
        return payload

    @staticmethod
    def construct_paquetexpress_quotation_payload(shipper, recipient, measures):
        return {
            "body": {
                "request": {
                    "data": {
                        "clientAddrOrig": {
                            "zipCode": shipper[9],
                            "colonyName": "LA AZTECA"
                        },
                        "clientAddrDest": {
                            "zipCode": recipient[9],
                            "colonyName": "COLOSO"
                        },
                        "services": {
                            "dlvyType": "1",
                            "ackType": "N",
                            "totlDeclVlue": 0,
                            "invType": "A",
                            "radType": "1"
                        },
                        "otherServices": {
                            "otherServices": []
                        },
                        "shipmentDetail": {
                            "shipments": [
                                {
                                    "sequence": 1,
                                    "quantity": 1,
                                    "shpCode": "2",
                                    "weight": measures[0],
                                    "longShip": measures[1],
                                    "widthShip": measures[2],
                                    "highShip": measures[3]
                                }
                            ]
                        },
                        "quoteServices": [
                            "ALL"
                        ]
                    },
                    "objectDTO": None
                },
                "response": None
            }
        }

    @staticmethod
    def construct_dhl_payload_quotation(*args):
        pass

    ############# HECHO POR ERIC #############
    @staticmethod
    def construct_eship_shipment_payload(rate_id):
        return {
            "rate_id": rate_id,
            "label_format": "PDF_4x6"
        }

    @staticmethod
    def construct_fedex_shipment_payload(fedex_account, service_type, shipper, recipient, measures):
        payload = {
            "labelResponseOptions": "URL_ONLY",
            "requestedShipment": {
                "shipper": {
                        "address": {
                            "streetLines": [shipper[4],shipper[5]],
                            "city": shipper[6],
                            "stateOrProvinceCode": shipper[7],
                            "postalCode": shipper[9],
                            "countryCode": shipper[8]
                        },
                        "contact": {
                            "personName": shipper[0],
                            "companyName": shipper[1],
                            "phoneNumber": shipper[3]
                        },
                    },
                "recipients": [{
                        "address": {
                            "streetLines": [recipient[4], recipient[5]],
                            "city": recipient[6],
                            "stateOrProvinceCode": recipient[7],
                            "postalCode": recipient[9],
                            "countryCode": recipient[8]
                        },
                        "contact": {
                            "personName": recipient[0],
                            "emailAddress": recipient[2],
                            "phoneNumber": recipient[3]
                        },
                        "deliveryInstructions": recipient[4]+' '+recipient[5]
                    }],
                "serviceType": service_type, # Si no se coloca, devuelve todas las opciones disponibles
                "packagingType": "YOUR_PACKAGING",
                "pickupType": "USE_SCHEDULED_PICKUP",
                "shippingChargesPayment": {
                    "paymentType": "SENDER"
                    ###string Enum: "SENDER" "RECIPIENT" "THIRD_PARTY" "COLLECT" Indicates who and how the shipment will be paid for.Required for Express and Ground.
                },
                "labelSpecification": {
                    "labelFormatType": "COMMON2D",
                    "imageType": "ZPLII",  # "ZPLII",#"PDF", "EPL2"
                    "labelStockType": "STOCK_4X6",
                },
                "preferredCurrency": "MXN",  # NMP/MXN
                'X-locale': "es_MX",
                # "shipDateStamp": date,
                "requestedPackageLineItems": [
                    {
                        "weight": {
                            "units": "KG",
                            "value": float(measures[0])
                        },
                        "dimensions": {
                            "length": float(measures[1]),
                            "width": float(measures[2]),
                            "height": float(measures[3]),
                            "units": "CM"
                        }
                    }
                ],
            },
            "accountNumber": {
                "value": fedex_account
            },
        }

        payload = json.dumps(payload)
        return payload

    @staticmethod
    def construct_paquetexpress_shipment_payload(service_type, shipper, recipient, measures, data_sat):
        payload = {
            "billRad": 'REQUEST', # Quien pagará la solicitud, sólo con REQUEST o ORIGIN pueden ser a crédito, DESTINATION 3ra opcion
            "billClntId": '3106926',
            "pymtMode": "PAID",  # Modo de pago (PAID=PAGADO, TO_PAY=Flete por cobrar)
            "pymtType": "C",  # N= CONTADO, C=CREDITO Tipo de pago (CREDITO, CONTADO)
            "comt": "Generación de guía automática",
            "radGuiaAddrDTOList": [
                {
                    "addrLin1": shipper[8],  # País
                    "addrLin3": shipper[7],  # Estado
                    "addrLin4": shipper[6],  # Ciudad
                    "addrLin5": shipper[6],  # Población ########
                    "addrLin6": shipper[5], # Colonia #########
                    "zipCode": shipper[9],  # Código Postal
                    "clntName": shipper[0],  # Nombre del remitente
                    "email": shipper[2],  # Email si es válido
                    "phno1": shipper[3],  # Teléfono
                    "contacto": shipper[0],  # Nombre de contacto
                    "strtName": shipper[4],  # Dirección completa
                    "drnr": '.',
                    "addrType": "ORIGIN"
                },
                {
                    "addrLin1": recipient[8],  # País
                    "addrLin3": recipient[7],  # Estado
                    "addrLin4": recipient[6],  # Ciudad
                    "addrLin5": recipient[6],  # Población ########
                    "addrLin6": recipient[5],  # Colonia #########
                    "zipCode": recipient[9],  # Código Postal
                    "clntName": recipient[0],  # Nombre del destinatario
                    "email": recipient[2],  # Email si es válido
                    "phno1": recipient[3],  # Teléfono
                    "contacto": recipient[0],  # Nombre de contacto
                    "strtName": recipient[4],  # Dirección completa
                    "drnr": '.',
                    "addrType": "DESTINATION"
                }
            ],
            "radSrvcItemDTOList": [
                {
                    "srvcId": "PACKETS",
                    "productIdSAT": str(data_sat[0]), # UNSPSC Code
                    "weight": str(measures[0]),  # Peso
                    "volL": str(measures[1]),  # Largo
                    "volW": str(measures[2]),  # Ancho
                    "volH": str(measures[3]),  # Alto
                    "cont": "Paquete",  # Contenido genérico
                    "qunt": '1'  # Siempre 1 paquete por guía
                }
            ],
            "listSrvcItemDTO": [{
              "srvcId": "EAD",
              "value1": ""
            },
              {
                  "srvcId": "RAD",
                  "value1": ""
              },
                {
                    "srvcId": "INV",
                    "value1": str(data_sat[1]) # Valor del producto
                }
            ],
            "typeSrvcId": service_type, # Clave de servicio: STD-T = Estándar SEG-2D = Express segundo día SEG-DS = Express día siguiente SEG-A12 = Express antes de las 12 SEG-MD = Express mismo día
            "listRefs": []
        }

        return payload
    ############# HECHO POR ERIC FIN #############


if __name__ == "__main__":
    # # Configuración inicial
    is_test = False
    #shipper = ("False", "False", "False", "False", "False", "False", "Cuajimalpa", "False", "MX", "05120")
    #recipient = ("False", "False", "False", "False", "False", "False", "Huixquilucan", "False", "MX", "52763")
    #measures = (2, 30, 20, 10) # peso, largo, ancho, alto
    so_name = "BA12041"

    shipper = ("False", "False", "False", "False", "False", "False", "Cuajimalpa", "False", "MX", "54010") # La azteca
    recipient = ("False", "False", "False", "False", "False", "False", "Huixquilucan", "False", "MX", "83040") # Coloso
    measures = (33, 183, 34, 12)  # weight, length, width, height

    # /////////////////////////////////////////////////////////////////////////////////////


    # Construcción del payload para eShip
    #payload_eship = PayloadConstructor.construct_eship_quotation_payload(shipper, recipient, measures, so_name)

    # Consumo del API eShip
    eship_api = EShipAPI(is_test)
    start = tm.time()
    payload_eship = eship_api.construct_quotation_payload(shipper=shipper, recipient=recipient, measures=measures, so_name=so_name)
    response_eship = eship_api.quote_rates(payload_eship)
    end = tm.time()

    # Salida eShip
    if response_eship:
        print("Respuesta eShip:")
        print(json.dumps(response_eship, indent=4))
    print(f"Tiempo transcurrido eShip: {round(end-start, 2)} [sec]")

    print('*-*-*-*-*-*-*-*-*-*-*-*-*')

    # /////////////////////////////////////////////////////////////////////////////////////

    # Consumo del API FedEx
    fedex_api = FedExAPI(is_test)
    fedex_account = fedex_api.fedex_account()
    date = "2025-01-14"

    # Construcción del payload para FedEx
    #payload_fedex = PayloadConstructor.construct_fedex_quotation_payload(fedex_account, date, shipper, recipient, measures)

    start = tm.time()
    payload_fedex = fedex_api.construct_quotation_payload(
        fedex_account=fedex_account,
        date=date,
        shipper=shipper,
        recipient=recipient,
        measures=measures
    )
    response_fedex = fedex_api.quote_rates(payload_fedex)
    end = tm.time()

    # Salida FedEx
    if response_fedex:
        print("Respuesta FedEx:")
        print(json.dumps(response_fedex, indent=4))
    print(f"Tiempo transcurrido FedEx: {round(end-start, 2)} [sec]")

    print('*-*-*-*-*-*-*-*-*-*-*-*-*')

    # /////////////////////////////////////////////////////////////////////////////////////


    # Construcción del payload para paquetexpress
    #payload_paquetexpress = PayloadConstructor.construct_paquetexpress_quotation_payload(shipper, recipient, measures)

    # Consumo de api paquetexpress
    paquetexpress_api = PaquetexpressAPI(is_test)
    payload_paquetexpress = paquetexpress_api.construct_quotation_payload(shipper=shipper, recipient=recipient, measures=measures)
    response_paquetexpress = paquetexpress_api.quote_rates(payload_paquetexpress)

    # Salida
    if response_paquetexpress:
        print("Respuesta Paquetexpress:")
        print(json.dumps(response_paquetexpress, indent=4))
    else:
        print("No se pudo obtener la cotización de Paquetexpress.")


