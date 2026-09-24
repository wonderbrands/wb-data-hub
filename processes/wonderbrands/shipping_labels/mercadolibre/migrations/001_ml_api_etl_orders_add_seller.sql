-- =====================================================================
-- Migración: multi-tienda para el proceso de guías de Mercado Libre
--   25523702  -> 'SOMOS-REYES'
--   160190870 -> 'SOMOS-REYES OFICIALES'
--
-- ORDEN DE DESPLIEGUE: ejecutar ANTES de hacer merge del código.
-- Es compatible con el código actual en producción:
--   * Todos los consumidores (ETL, inyector, reporte, test_zpl) usan columnas
--     por NOMBRE (DictCursor / listas explícitas), nunca por índice ni SELECT *.
--   * El INSERT actual no manda seller_id/seller_name -> toma el DEFAULT
--     (SOMOS-REYES), que es el valor correcto para la tienda original.
-- =====================================================================

-- 0) Pre-checks (solo lectura). Confirmar llave única y versión antes de correr.
SHOW CREATE TABLE tools.ml_api_etl_orders;   -- revisar la UNIQUE KEY que usa ON DUPLICATE KEY
SELECT VERSION();                             -- >= 8.0.12 permite ADD COLUMN con ALGORITHM=INSTANT
SELECT seller_id FROM somos_reyes.tokens WHERE seller_id = '160190870';  -- debe regresar 1 fila

-- 1) Columnas nuevas. Se agregan al FINAL (compatible con INSTANT) y con DEFAULT
--    de la tienda original, así el histórico queda poblado en el mismo paso.
ALTER TABLE tools.ml_api_etl_orders
    ADD COLUMN seller_id   VARCHAR(20) NOT NULL DEFAULT '25523702',
    ADD COLUMN seller_name VARCHAR(50) NOT NULL DEFAULT 'SOMOS-REYES',
    ALGORITHM = INSTANT;
-- Si el motor rechaza INSTANT (MySQL < 8.0.12), repetir sin la línea ALGORITHM.

-- 2) Actualización explícita del histórico (idempotente; con el DEFAULT
--    debería afectar 0 filas, se deja como garantía y documentación).
UPDATE tools.ml_api_etl_orders
SET    seller_id = '25523702', seller_name = 'SOMOS-REYES'
WHERE  seller_id IS NULL OR seller_id = '' OR seller_name IS NULL OR seller_name = '';

-- 3) Índice para los filtros nuevos:
--    ETL      -> WHERE seller_id = ?                (carga de caché)
--    Inyector -> WHERE seller_id = ? AND print_status = 'READY_TO_PRINT' AND processed_successfully = 0
ALTER TABLE tools.ml_api_etl_orders
    ADD INDEX idx_seller_print (seller_id, print_status, processed_successfully),
    ALGORITHM = INPLACE, LOCK = NONE;

-- 4) Configuración de impresión para Oficiales (id = 2), clonada de id = 1.
--    INSERT IGNORE: si la fila id = 2 ya existe no se toca.
INSERT IGNORE INTO tools.ml_print_controls
    (id, force_print_days_ahead, active_until, ml_order_lookback_days, cutoff_time_cdmx)
SELECT 2, force_print_days_ahead, active_until, ml_order_lookback_days, cutoff_time_cdmx
FROM   tools.ml_print_controls
WHERE  id = 1;

-- 5) Validación
SELECT seller_id, seller_name, COUNT(*) AS filas
FROM   tools.ml_api_etl_orders
GROUP  BY seller_id, seller_name;

SELECT * FROM tools.ml_print_controls WHERE id IN (1, 2);

-- ---------------------------------------------------------------------
-- ROLLBACK (solo si se revierte también el código a la versión anterior)
-- ---------------------------------------------------------------------
-- ALTER TABLE tools.ml_api_etl_orders DROP INDEX idx_seller_print;
-- ALTER TABLE tools.ml_api_etl_orders DROP COLUMN seller_name, DROP COLUMN seller_id;
-- DELETE FROM tools.ml_print_controls WHERE id = 2;
