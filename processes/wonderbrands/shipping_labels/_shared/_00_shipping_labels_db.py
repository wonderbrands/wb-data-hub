"""
Módulo compartido para registrar guías generadas (o fallidas) en la tabla
`tools.shipping_labels`.

Esta tabla es transversal a todas las automatizaciones de guías (Amazon
Self-Ship, Coppel, y futuras integraciones), por lo que la función vive en
un módulo aparte para no duplicar código en cada script.

Uso típico:

    from _00_shipping_labels_db import insert_shipping_label

    insert_shipping_label(
        conn,
        marketplace_id=order_id,
        marketplace='Amazon',
        sku=row['sku'],
        qty_ordered=row['quantity_ordered'],
        status='LABELS_GENERATED',
        label_generated=True,
        tracking_number=[...],   # lista, dict, o string ya serializado a JSON
        shipping_cost=125.50,
        carrier='FEDEX',
        carrier_service_level='Estándar',
        error_log=None
    )

Nota: la función reutiliza la conexión (`conn`) que cada script ya abre para
su propia base de datos; no abre ni cierra conexiones por su cuenta.
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger()


def _normalize_tracking_to_json(tracking_number):
    """
    Normaliza el parámetro tracking_number a un string JSON válido.

    Acepta:
      - None                      -> None
      - list / dict                -> se serializa con json.dumps()
      - string ya serializado JSON  -> se respeta tal cual (permite reutilizar
                                        un tracking_json_str ya construido en
                                        el script, p. ej. el de amz_label_queue)
      - string "plano" (un solo tracking) -> se envuelve en una lista JSON
                                              para mantener el formato uniforme
                                              (arreglo) incluso en envíos de
                                              una sola caja.
    """
    if tracking_number is None:
        return None

    if isinstance(tracking_number, (list, dict)):
        return json.dumps(tracking_number, ensure_ascii=False)

    if isinstance(tracking_number, str):
        try:
            json.loads(tracking_number)
            # Ya es un JSON válido (objeto o arreglo) -> se respeta tal cual
            return tracking_number
        except (json.JSONDecodeError, TypeError):
            # Es un tracking "plano" -> se envuelve en un arreglo
            return json.dumps([tracking_number], ensure_ascii=False)

    # Cualquier otro tipo (número, etc.) -> best effort
    return json.dumps([tracking_number], ensure_ascii=False)


def insert_shipping_label(
    conn,
    marketplace_id,
    marketplace,
    sku,
    qty_ordered,
    status,
    label_generated: bool,
    tracking_number=None,
    shipping_cost=None,
    carrier=None,
    carrier_service_level=None,
    error_log=None,
    label_generated_at=None,
):
    """
    Inserta UN registro en `tools.shipping_labels`.

    Se espera UNA llamada por SKU (a nivel línea de orden). Para SKUs
    multicaja, `tracking_number` debe incluir todos los tracking numbers
    generados para ese SKU (lista de strings o de dicts); la función se
    encarga de castear/formatear correctamente a JSON.

    Parámetros
    ----------
    conn : conexión mysql.connector ya abierta (reutilizada del script que
           llama a esta función).
    marketplace_id : str  -> ID de la orden en el marketplace de origen
                              (amazonorderid, order_id de Mirakl, etc.)
    marketplace : str     -> 'Amazon', 'Coppel', etc.
    sku : str             -> SKU (parent/offer_sku) al que corresponde el renglón
    qty_ordered : int
    status : str          -> p. ej. 'LABELS_GENERATED', 'LIMIT_RATIO_OVERCOME',
                              'SKU_NOT_SUPPORT', 'Costo_guia_excesivo', etc.
    label_generated : bool
    tracking_number : list | dict | str | None
    shipping_cost : float | None
    carrier : str | None
    carrier_service_level : str | None
    error_log : str | None
    label_generated_at : str | None -> si no se especifica y label_generated
                                        es True, se usa el datetime actual.
    """
    if conn is None:
        logger.error(
            f"[tools.shipping_labels] No hay conexión a BD; no se pudo "
            f"insertar el registro de {marketplace} / {marketplace_id} / {sku}."
        )
        return

    tracking_json = _normalize_tracking_to_json(tracking_number)

    if label_generated and not label_generated_at:
        label_generated_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        cursor = conn.cursor()
        query = """
            INSERT INTO tools.shipping_labels
                (marketplace_id, marketplace, sku, qty_ordered, status,
                 label_generated, label_generated_at, tracking_number,
                 shipping_cost, carrier, carrier_service_level, error_log)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(query, (
            marketplace_id,
            marketplace,
            sku,
            qty_ordered,
            status,
            label_generated,
            label_generated_at,
            tracking_json,
            shipping_cost,
            carrier,
            carrier_service_level,
            error_log,
        ))
        conn.commit()
        cursor.close()
        logger.info(
            f"[tools.shipping_labels] Registrado: marketplace={marketplace} "
            f"marketplace_id={marketplace_id} sku={sku} status={status} "
            f"label_generated={label_generated}"
        )
    except Exception as e:
        # Nunca debe tumbar el flujo principal del script por un fallo
        # al insertar en esta tabla de "solo registro".
        logger.error(
            f"[tools.shipping_labels] Error insertando registro para "
            f"{marketplace} / {marketplace_id} / {sku}: {e}"
        )