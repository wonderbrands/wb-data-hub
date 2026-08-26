import logging
import requests
import json
import os

# --- CONFIGURACIÓN DE LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

API_KEY_MIRAKL = 'a7cd0f6c-fc62-4fc9-9a98-886ad1fbe1c1'
MIRAKL_API_BASE_URL = "https://coppel-prod.mirakl.net/api"


def load_mirakl_carrier_map(mirakl_headers: dict) -> dict:
    """
    Llama al endpoint SH21 de Mirakl para obtener la lista de carriers
    y crea un mapa de {label_lower: standard_code}.
    Guarda el resultado en carrier_map.json
    """
    url = f"{MIRAKL_API_BASE_URL}/shipping/carriers"
    carrier_map = {}

    # Mapa de fallback
    fallback_map = {
        "fedex": "fedex",
        "paqueteexpress": "paquetexpress",
        "dhl": "dhl",
        "estafeta": "estafeta",
        "segmail": "segmail"
    }

    try:
        response = requests.get(url, headers=mirakl_headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        carriers = data.get('carriers', [])
        if not carriers:
            logger.warning("Mirakl SH21: La API no devolvió carriers. Usando mapa de fallback.")
            carrier_map = fallback_map
        else:
            for carrier in carriers:
                label = (carrier.get('label', '').lower()).replace(" ", "")
                print(label)
                standard_code = carrier.get('standard_code')
                if label and standard_code:
                    carrier_map[label] = standard_code

            # Añadir mapeos comunes por si acaso
            # for code in ['fedex', 'dhl', 'paquetexpress', 'estafeta', 'segmail']:
            #     if code not in carrier_map:
            #         carrier_map[code] = code

        # Guardar resultado en JSON
        with open("carrier_map.json", "w", encoding="utf-8") as f:
            json.dump(carrier_map, f, ensure_ascii=False, indent=4)

        logger.info(f"Mirakl SH21: Mapa de carriers cargado y guardado en carrier_map.json ({len(carrier_map)} mapeos).")
        return carrier_map

    except Exception as e:
        logger.error(f"Mirakl SH21: Error al llamar a /api/shipping/carriers: {e}. Usando mapa de fallback.")
        # Guardar el fallback también
        with open("carrier_map.json", "w", encoding="utf-8") as f:
            json.dump(fallback_map, f, ensure_ascii=False, indent=4)
        return fallback_map


def load_carrier_map_from_json(file_path: str = "carrier_map.json") -> dict:
    """Carga el JSON guardado y devuelve el diccionario carrier_map."""
    if not os.path.exists(file_path):
        logger.error(f"No existe el archivo {file_path}")
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        carrier_map = json.load(f)
    logger.info(f"Mapa de carriers cargado desde {file_path} ({len(carrier_map)} mapeos).")
    return carrier_map


if __name__ == "__main__":
    headers_mirakl = {
        "Authorization": API_KEY_MIRAKL,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    # Cargar desde API y guardar
    carrier_map = load_mirakl_carrier_map(headers_mirakl)

    # Leer desde el JSON guardado
    carrier_map_from_json = load_carrier_map_from_json()
    # print(carrier_map_from_json)
