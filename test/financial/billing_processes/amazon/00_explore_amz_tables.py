"""
00_explore_amz_tables.py
------------------------
Script de RECONOCIMIENTO del esquema de Amazon. SOLO LECTURA.

Objetivo: mapear las columnas reales de las tablas de Amazon contra las
"columnas de Excel" que describe el .md de Finanzas (Col A settlement_id,
Col C order_id, Col D so, Col F posted_date, Col I description,
Col X our_price_tax, Col Z product_charges).

Genera un archivo de texto (amz_schema_report.txt) para revision manual.
Este script es DESCARTABLE una vez que el esquema quede mapeado.

Requiere en el .env: DB_HOST, DB_USER, DB_PASSWORD
"""

import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env')

# ----------------------------------------------------------------------------
DB_SCHEMA = 'somos_reyes'

TABLAS = [
    'amz_transactions',
    'amz_transaction_items',
    'amz_b2b_invoices',
    'amz_tokens',
]

FILAS_MUESTRA = 5
ANCHO_VALOR   = 60          # trunca valores largos (JSON crudo, etc.)
OUT_FILE      = 'amz_schema_report.txt'
# ----------------------------------------------------------------------------


def get_db_connection():
    """Misma conexion que amazon_api_test.py."""
    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=DB_SCHEMA
    )
    cursor = conn.cursor()
    cursor.execute("SET NAMES utf8mb4;")
    cursor.execute("SET CHARACTER SET utf8mb4;")
    cursor.execute("SET character_set_connection=utf8mb4;")
    cursor.close()
    return conn


def escribir(fh, texto):
    """Imprime en consola y en el archivo a la vez."""
    print(texto)
    fh.write(texto + "\n")


def ejecutar_y_volcar(fh, conn, titulo, query, params=None):
    """
    Corre una query de lectura y vuelca el resultado.
    Si la query falla (columna inexistente, tabla ausente) lo registra
    y continua: el objetivo es descubrir el esquema, no asumirlo.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(query, params or ())
        filas = cursor.fetchall()
        columnas = [d[0] for d in cursor.description] if cursor.description else []

        escribir(fh, f"\n{'=' * 78}")
        escribir(fh, f"  {titulo}")
        escribir(fh, f"{'=' * 78}")

        if not filas:
            escribir(fh, "  (sin resultados)")
            return []

        escribir(fh, " | ".join(columnas))
        escribir(fh, "-" * 78)
        for fila in filas:
            valores = []
            for v in fila:
                if v is None:
                    valores.append('NULL')
                else:
                    valores.append(str(v)[:ANCHO_VALOR])
            escribir(fh, " | ".join(valores))
        return filas

    except Exception as e:
        escribir(fh, f"\n### ERROR en '{titulo}': {e}")
        return []
    finally:
        cursor.close()


def obtener_columnas(conn, tabla):
    """Devuelve la lista de nombres de columna de una tabla (o vacia si no existe)."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (DB_SCHEMA, tabla))
        return [r[0] for r in cursor.fetchall()]
    except Exception:
        return []
    finally:
        cursor.close()


def explorar_estructura(fh, conn, tabla):
    """Estructura, volumen y muestra de filas de una tabla."""
    ejecutar_y_volcar(
        fh, conn,
        f"ESTRUCTURA -- {DB_SCHEMA}.{tabla}",
        f"""
        SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (DB_SCHEMA, tabla)
    )

    ejecutar_y_volcar(
        fh, conn,
        f"INDICES -- {DB_SCHEMA}.{tabla}",
        f"SHOW INDEX FROM {DB_SCHEMA}.{tabla}"
    )

    ejecutar_y_volcar(
        fh, conn,
        f"VOLUMEN -- {DB_SCHEMA}.{tabla}",
        f"SELECT COUNT(*) AS total_filas FROM {DB_SCHEMA}.{tabla}"
    )

    # amz_tokens guarda credenciales: mostramos estructura pero NO el contenido.
    if tabla == 'amz_tokens':
        escribir(fh, "\n  (muestra omitida: la tabla contiene credenciales)")
        return

    ejecutar_y_volcar(
        fh, conn,
        f"MUESTRA -- {DB_SCHEMA}.{tabla}",
        f"SELECT * FROM {DB_SCHEMA}.{tabla} LIMIT {FILAS_MUESTRA}"
    )


def perfilar_transacciones(fh, conn):
    """
    Perfilado de negocio sobre amz_transactions.
    Cada bloque se ejecuta solo si las columnas que necesita existen,
    porque todavia no sabemos como se llaman realmente.
    """
    cols = obtener_columnas(conn, 'amz_transactions')
    if not cols:
        escribir(fh, "\n### No se pudo leer amz_transactions. Se omite el perfilado.")
        return

    escribir(fh, f"\n\n>>> Columnas detectadas en amz_transactions:\n    {cols}")

    def tiene(*requeridas):
        return all(c in cols for c in requeridas)

    # 1) Tipos de movimiento -> separa Order Payment / Refund / DisbursementOnlyRefund
    if tiene('transaction_type', 'description'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 1 -- Tipos de movimiento (transaction_type + description)",
            f"""
            SELECT transaction_type, description, COUNT(*) AS n
            FROM {DB_SCHEMA}.amz_transactions
            GROUP BY transaction_type, description
            ORDER BY n DESC
            LIMIT 50
            """
        )
    elif tiene('transaction_type'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 1 -- Tipos de movimiento (transaction_type)",
            f"""
            SELECT transaction_type, COUNT(*) AS n
            FROM {DB_SCHEMA}.amz_transactions
            GROUP BY transaction_type
            ORDER BY n DESC
            LIMIT 50
            """
        )

    # 2) Cobertura temporal disponible
    if tiene('posted_date'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 2 -- Cobertura temporal",
            f"""
            SELECT MIN(posted_date) AS fecha_min,
                   MAX(posted_date) AS fecha_max,
                   COUNT(*)         AS total
            FROM {DB_SCHEMA}.amz_transactions
            """
        )

    # 3) Settlements: cuantos hay y si traen ya la fecha de liberacion
    if tiene('settlement_id'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 3 -- Settlements recientes",
            f"""
            SELECT settlement_id, COUNT(*) AS movimientos
            FROM {DB_SCHEMA}.amz_transactions
            GROUP BY settlement_id
            ORDER BY settlement_id DESC
            LIMIT 10
            """
        )

    # 4) Nulos en los campos clave del .md
    campos_clave = [c for c in
                    ('settlement_id', 'order_id', 'so', 'posted_date',
                     'description', 'transaction_type',
                     'product_charges', 'our_price_tax')
                    if c in cols]
    if campos_clave:
        selects = ",\n                   ".join(
            f"SUM(CASE WHEN {c} IS NULL THEN 1 ELSE 0 END) AS nulos_{c}"
            for c in campos_clave
        )
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 4 -- Nulos en campos clave del .md",
            f"""
            SELECT COUNT(*) AS total,
                   {selects}
            FROM {DB_SCHEMA}.amz_transactions
            """
        )

    # 5) Muestra concreta de una venta (Order Payment) ya cruzada con SO
    if tiene('description', 'order_id'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 5 -- Muestra de ventas (description = 'Order Payment')",
            f"""
            SELECT *
            FROM {DB_SCHEMA}.amz_transactions
            WHERE description = 'Order Payment'
            ORDER BY id DESC
            LIMIT 3
            """ if 'id' in cols else f"""
            SELECT *
            FROM {DB_SCHEMA}.amz_transactions
            WHERE description = 'Order Payment'
            LIMIT 3
            """
        )

    # 6) Muestra de devoluciones -> valida los dos sabores del .md de NC
    if tiene('transaction_type'):
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 6 -- Muestra de devoluciones (transaction_type = 'Refund')",
            f"""
            SELECT *
            FROM {DB_SCHEMA}.amz_transactions
            WHERE transaction_type = 'Refund'
            LIMIT 3
            """
        )


def perfilar_items(fh, conn):
    """
    Perfilado de amz_transaction_items.
    Define la granularidad: si el importe vive a nivel SKU, las notas de
    credito parciales son viables.
    """
    cols = obtener_columnas(conn, 'amz_transaction_items')
    if not cols:
        escribir(fh, "\n### No se pudo leer amz_transaction_items. Se omite el perfilado.")
        return

    escribir(fh, f"\n\n>>> Columnas detectadas en amz_transaction_items:\n    {cols}")

    # Items por transaccion -> confirma si una orden puede traer varios SKUs
    fk = next((c for c in ('transaction_id', 'amz_transaction_id', 'order_id')
               if c in cols), None)
    if fk:
        ejecutar_y_volcar(
            fh, conn,
            f"PERFIL 7 -- Items por transaccion (agrupado por {fk})",
            f"""
            SELECT n_items, COUNT(*) AS transacciones
            FROM (
                SELECT {fk}, COUNT(*) AS n_items
                FROM {DB_SCHEMA}.amz_transaction_items
                GROUP BY {fk}
            ) t
            GROUP BY n_items
            ORDER BY n_items
            LIMIT 20
            """
        )


def perfilar_b2b(fh, conn):
    """Perfilado de amz_b2b_invoices -> define si B2B se resuelve con un JOIN."""
    cols = obtener_columnas(conn, 'amz_b2b_invoices')
    if not cols:
        escribir(fh, "\n### No se pudo leer amz_b2b_invoices. Se omite el perfilado.")
        return

    escribir(fh, f"\n\n>>> Columnas detectadas en amz_b2b_invoices:\n    {cols}")

    if 'order_id' in cols:
        ejecutar_y_volcar(
            fh, conn,
            "PERFIL 8 -- Ordenes B2B distintas vs. filas",
            f"""
            SELECT COUNT(*)                 AS filas,
                   COUNT(DISTINCT order_id) AS ordenes_distintas
            FROM {DB_SCHEMA}.amz_b2b_invoices
            """
        )


def main():
    print("Conectando a la base de datos...")
    conn = get_db_connection()

    with open(OUT_FILE, 'w', encoding='utf-8') as fh:
        escribir(fh, "REPORTE DE RECONOCIMIENTO -- TABLAS AMAZON")
        escribir(fh, f"Schema: {DB_SCHEMA}")

        for tabla in TABLAS:
            explorar_estructura(fh, conn, tabla)

        perfilar_transacciones(fh, conn)
        perfilar_items(fh, conn)
        perfilar_b2b(fh, conn)

    conn.close()
    print(f"\nListo. Reporte guardado en: {os.path.abspath(OUT_FILE)}")


if __name__ == "__main__":
    main()
