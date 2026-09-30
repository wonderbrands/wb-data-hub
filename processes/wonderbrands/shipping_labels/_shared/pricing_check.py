"""
Check de pricing para ventas en marketplaces.

Regla:
  1. Fuente principal: mati.api_channel_targets_by_marketplace (wb1).
     Busca el SKU exacto + mes de la venta (YYYYMM, hora México UTC-6) + canal.
     Mínimo permitido = target_price * (100 - margen) / 100  -> por defecto 85%.
  2. Fallback: Sheet oficial de pricing (col A = SKU, col C = precio mínimo).
     Se usa SOLO si en MATI no existe la fila SKU/mes/canal.
     El valor de la columna C se toma TAL CUAL (no se le descuenta el margen).
  3. Si no se puede determinar un mínimo, o el precio de venta es menor que
     el mínimo, el check NO aprueba (en la auto eso manda la orden a manual).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MEXICO_UTC_OFFSET = timezone(timedelta(hours=-6))

# Sheet oficial del fallback de pricing (el mismo que usa la auto de TikTok no bulky):
# https://docs.google.com/spreadsheets/d/1bFJNl40OcB72l-t_jsdD253Qa_xp044Mi9db6vO77es/edit?gid=0
FALLBACK_SHEET_KEY = "1bFJNl40OcB72l-t_jsdD253Qa_xp044Mi9db6vO77es"
FALLBACK_WORKSHEET_ID = 0  # pestaña gid=0


def load_fallback_sheet_rows(service_account_file: str) -> list[list[Any]] | None:
    """
    Lee las columnas A:C del Sheet oficial del fallback de pricing.

    Devuelve None si no se pudo leer; check_pricing lo trata como
    FALLBACK_UNAVAILABLE para los SKUs que no estén en MATI.
    La cuenta de servicio necesita acceso de lectura al Sheet
    (compartirlo con el email de la cuenta de servicio).
    """
    try:
        import gspread

        client = gspread.service_account(filename=service_account_file)
        worksheet = client.open_by_key(FALLBACK_SHEET_KEY).get_worksheet_by_id(
            FALLBACK_WORKSHEET_ID
        )
        return worksheet.get("A:C") if worksheet is not None else None
    except Exception:
        return None


def check_pricing(
    sku: str,
    sale_price: Any,
    sale_timestamp: Any,
    mysql_connection: Any,
    service_account_file: str | None = None,
    sheet_rows: list[list[Any]] | None = None,
    channel_id: str = "otros_marketplaces",
    safety_margin_percent: int | Decimal = 15,
) -> dict[str, Any]:
    """
    Valida si el precio de venta de un SKU cumple el mínimo permitido.

    Parámetros
    ----------
    sku : str
        SKU del vendedor (en TikTok es `seller_sku`). Se compara EXACTO
        (distingue mayúsculas); solo se quitan espacios en los extremos.
    sale_price : str | int | float | Decimal
        Precio de venta de la línea (en TikTok es `sale_price`).
    sale_timestamp : int | str
        Momento de la venta como Unix timestamp en segundos (UTC).
        En TikTok se usa `paid_time` y, si no viene, `create_time`.
    mysql_connection :
        Conexión DB-API abierta a wb1 (por ejemplo MySQLdb.connect(...)).
        La función no la cierra.
    service_account_file : str | None
        Ruta al JSON de la cuenta de servicio de Google. Con esto la función
        lee sola el Sheet oficial (FALLBACK_SHEET_KEY), y SOLO cuando el SKU
        no está en MATI.
    sheet_rows : list[list] | None
        Opcional. Filas del Sheet ya leídas con load_fallback_sheet_rows(),
        para no leerlo en cada llamada cuando validás muchos SKUs.
        Si se pasa, tiene prioridad sobre service_account_file.
        Sin filas ni cuenta de servicio (o si la lectura falla), el SKU que
        no esté en MATI queda como FALLBACK_UNAVAILABLE.
    channel_id : str
        Canal de MATI a consultar. Por defecto "otros_marketplaces".
    safety_margin_percent : int | Decimal
        Descuento que se aplica al target de MATI. 15 -> mínimo = 85% del target.

    Devuelve
    --------
    dict con:
        approved (bool)        True si el precio es >= al mínimo.
        minimum (Decimal|None) Mínimo permitido, si se pudo calcular.
        source (str|None)      "MATI_TARGET_85" o "GOOGLE_SHEETS_MINIMUM".
        problem (str|None)     Código del motivo cuando approved es False:
            MISSING_SALE_TIME        timestamp de venta ausente o inválido
            DUPLICATED_SKU_MONTH     más de una fila en MATI para SKU/mes/canal
            INVALID_TARGET_PRICE     target_price de MATI vacío, <= 0 o inválido
            FALLBACK_UNAVAILABLE     no está en MATI y el Sheet no se pudo leer
            FALLBACK_SKU_MISSING     no está ni en MATI ni en el Sheet
            FALLBACK_SKU_DUPLICATED  SKU repetido en la columna A del Sheet
            FALLBACK_PRICE_INVALID   precio de la columna C vacío/cero/inválido
            PRICE_BELOW_MINIMUM      precio de venta ausente o menor al mínimo

    Ejemplo
    -------
        conn = MySQLdb.connect(host=..., user=..., passwd=..., db="mati")

        # Un SKU suelto:
        r = check_pricing("SKU-1", "849.99", 1727740800, conn,
                          service_account_file="sa.json")
        # {'approved': False, 'minimum': Decimal('850.00'), ...}

        # Muchos SKUs: leer el Sheet una sola vez y reutilizarlo.
        rows = load_fallback_sheet_rows("sa.json")
        for sku, price, ts in lineas:
            r = check_pricing(sku, price, ts, conn, sheet_rows=rows)
    """
    sku = str(sku or "").strip()
    result = {"approved": False, "minimum": None, "source": None, "problem": None}

    # --- 1. Mes de la venta (YYYYMM) en hora de México -----------------------
    try:
        ts = int(sale_timestamp)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        # Sin fecha no se puede buscar en MATI ni se pasa al fallback.
        result["problem"] = "MISSING_SALE_TIME"
        return result
    yearmonth = (
        datetime.fromtimestamp(ts, timezone.utc)
        .astimezone(MEXICO_UTC_OFFSET)
        .strftime("%Y%m")
    )

    # --- 2. Fuente principal: target mensual de MATI -------------------------
    cursor = mysql_connection.cursor()
    try:
        cursor.execute(
            """
            SELECT target_price
            FROM mati.api_channel_targets_by_marketplace
            WHERE sku = %s AND yearmonth = %s AND channel_id = %s
            """,
            (sku, yearmonth, channel_id),
        )
        mati_rows = cursor.fetchall()
    finally:
        cursor.close()

    if len(mati_rows) > 1:
        # Ambiguo: no se elige una fila, y NO se usa el fallback.
        result["problem"] = "DUPLICATED_SKU_MONTH"
        return result

    if len(mati_rows) == 1:
        try:
            target = Decimal(str(mati_rows[0][0]))
        except (InvalidOperation, TypeError):
            target = Decimal("0")
        if target <= 0:
            # La fila existe pero está mal: tampoco se usa el fallback.
            result["problem"] = "INVALID_TARGET_PRICE"
            return result
        multiplier = (Decimal("100") - Decimal(str(safety_margin_percent))) / Decimal("100")
        result["minimum"] = target * multiplier
        result["source"] = "MATI_TARGET_85"

    # --- 3. Fallback: Sheet oficial (solo si la fila MATI NO existe) ---------
    else:
        result["source"] = "GOOGLE_SHEETS_MINIMUM"
        if sheet_rows is None and service_account_file:
            sheet_rows = load_fallback_sheet_rows(service_account_file)
        if sheet_rows is None:
            result["problem"] = "FALLBACK_UNAVAILABLE"
            return result
        # Se saltea el encabezado; se buscan TODAS las filas con ese SKU
        # para detectar duplicados.
        matches = []
        for row in sheet_rows[1:]:
            padded = list(row) + [""] * max(0, 3 - len(row))
            if sku and str(padded[0] or "").strip() == sku:
                matches.append(padded[2])  # columna C
        if not matches:
            result["problem"] = "FALLBACK_SKU_MISSING"
            return result
        if len(matches) > 1:
            result["problem"] = "FALLBACK_SKU_DUPLICATED"
            return result
        minimum = _parse_price(matches[0])
        if minimum is None:
            result["problem"] = "FALLBACK_PRICE_INVALID"
            return result
        result["minimum"] = minimum  # sin descuento: el Sheet ya es el mínimo

    # --- 4. Comparación: aprueba si precio >= mínimo (inclusive) -------------
    try:
        price = Decimal(str(sale_price))
    except (InvalidOperation, TypeError):
        price = None
    if price is None or price < result["minimum"]:
        result["problem"] = "PRICE_BELOW_MINIMUM"
        return result

    result["approved"] = True
    return result


def _parse_price(value: Any) -> Decimal | None:
    """
    Convierte el texto de una celda del Sheet en Decimal.
    Acepta "$1,234.50 MXN", "1,234.50", "1.234,50", "1234.5", "1,234".
    Devuelve None para celdas vacías, errores de Sheets ("#N/A"),
    formatos ambiguos o valores <= 0.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    # Quita moneda, símbolo $ y espacios (incluido el espacio duro).
    cleaned = re.sub(r"(?i)\bMXN\b", "", text)
    cleaned = cleaned.replace("$", "").replace("\u00a0", "").replace(" ", "")
    if not cleaned or re.search(r"[^0-9.,]", cleaned):
        return None

    if "," in cleaned and "." in cleaned:
        # Con ambos separadores, el que aparece último es el decimal.
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        integer, fraction = cleaned.rsplit(decimal_sep, 1)
        groups = integer.split(thousands_sep)
        if (
            not fraction.isdigit()
            or not 1 <= len(fraction) <= 2
            or not groups[0].isdigit()
            or not 1 <= len(groups[0]) <= 3
            or any(not g.isdigit() or len(g) != 3 for g in groups[1:])
        ):
            return None
        normalized = "".join(groups) + "." + fraction
    elif "," in cleaned or "." in cleaned:
        sep = "," if "," in cleaned else "."
        parts = cleaned.split(sep)
        if len(parts) == 2:
            integer, fraction = parts
            if not integer or not fraction:
                return None
            if len(fraction) == 3 and 1 <= len(integer) <= 3:
                normalized = integer + fraction  # "1,234" -> miles
            elif len(fraction) > 2:
                return None
            else:
                normalized = integer + "." + fraction  # "12,5" -> decimal
        elif len(parts) > 2 and 1 <= len(parts[0]) <= 3 and all(
            len(g) == 3 for g in parts[1:]
        ):
            normalized = "".join(parts)  # "1.234.567" -> miles
        else:
            return None
    else:
        normalized = cleaned

    try:
        price = Decimal(normalized)
    except InvalidOperation:
        return None
    return price if price > 0 else None