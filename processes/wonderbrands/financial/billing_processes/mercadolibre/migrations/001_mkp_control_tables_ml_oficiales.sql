-- =====================================================================
-- Migración: multi-tienda ML en las tablas de control de facturación y cobros
--   seller 25523702  -> marketplace 'MERCADO_LIBRE'            (SOMOS-REYES)
--   seller 160190870 -> marketplace 'MERCADO_LIBRE_OFICIALES'  (SOMOS-REYES OFICIALES)
--
-- Las tablas finance.mkp_billing_prod / finance.mkp_payments_prod ya son
-- multi-marketplace (UNIQUE (marketplace, mkp_order_id) y UNIQUE (marketplace,
-- payment_id)). NO se crean tablas nuevas. Lo único que puede requerir ALTER es
-- que la columna 'marketplace' no tenga espacio para 'MERCADO_LIBRE_OFICIALES'
-- (23 caracteres) o que sea ENUM.
--
-- ORDEN DE DESPLIEGUE:
--   1. Pasos 0-3 de este archivo (antes de hacer merge del código a main).
--   2. Crear el secreto MERCADO_PAGO_TOKEN_OFICIALES en Kestra.
--   3. Merge del código a main (ml_stores.py + scripts 01-04).
--   4. Desplegar los YAML modificados de ML ventas.
--   5. Desplegar los YAML nuevos de Oficiales. NUNCA antes del paso 3: el código
--      viejo de main ignora el seller en la query y escribe 'MERCADO_LIBRE'
--      a mano, así que facturaría órdenes de Oficiales con la CxC de ventas.
--   6. Paso 5 de este archivo (validación).
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0) Pre-checks (solo lectura)
-- ---------------------------------------------------------------------
SHOW CREATE TABLE finance.mkp_billing_prod;
SHOW CREATE TABLE finance.mkp_payments_prod;

-- Tipo y largo de la columna marketplace. Se necesita VARCHAR >= 23 (no ENUM).
SELECT TABLE_NAME, COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT
FROM   information_schema.COLUMNS
WHERE  TABLE_SCHEMA = 'finance'
  AND  TABLE_NAME IN ('mkp_billing_prod', 'mkp_payments_prod')
  AND  COLUMN_NAME = 'marketplace';

-- Token de ML de Oficiales (debe regresar 1 fila; el flujo de guías ya lo usa)
SELECT seller_id FROM somos_reyes.tokens WHERE seller_id = '160190870';

-- Órdenes de Oficiales que el miner 01 tomaría a partir del corte
-- (2026-09-17 15:56:00 CDMX = 17:56:00 UTC-4, misma zona que date_created)
SELECT COUNT(*) AS candidatas_oficiales
FROM   somos_reyes.ml_order_update o
WHERE  o.seller_id = '160190870'
  AND  o.date_created >= '2026-09-17 17:56:00'
  AND  o.status IN ('paid', 'closed');


-- ---------------------------------------------------------------------
-- 1) ALTER condicional — SOLO si el paso 0 mostró ENUM o VARCHAR < 23
-- ---------------------------------------------------------------------
-- Copiar NULL/DEFAULT tal cual aparecen en SHOW CREATE TABLE; MODIFY COLUMN
-- reemplaza la definición completa y si se omiten se pierden.
-- Ampliar un VARCHAR dentro del mismo rango de bytes de longitud (< 256 bytes;
-- en utf8mb4 = hasta 63 caracteres) es INPLACE en MySQL 8: sin copia de tabla.
--
-- ALTER TABLE finance.mkp_billing_prod
--     MODIFY COLUMN marketplace VARCHAR(50) NOT NULL,
--     ALGORITHM = INPLACE, LOCK = NONE;
--
-- ALTER TABLE finance.mkp_payments_prod
--     MODIFY COLUMN marketplace VARCHAR(50) NOT NULL,
--     ALGORITHM = INPLACE, LOCK = NONE;
--
-- Si es ENUM: el MySQL rechazará INPLACE al convertir a VARCHAR; quitar la línea
-- ALGORITHM y correrlo en una ventana sin ejecuciones de los flujos ML.


-- ---------------------------------------------------------------------
-- 2) Diagnóstico de contaminación (solo lectura)
-- ---------------------------------------------------------------------
-- Hasta hoy el miner 01 NO filtraba por seller_id: recorría todas las órdenes de
-- ml_order_update con el token de 25523702 y las guardaba como 'MERCADO_LIBRE'.
-- Si Oficiales ya escribía en ml_order_update, hay filas suyas bajo 'MERCADO_LIBRE'.
-- Se usa EXISTS (no JOIN) para no multiplicar filas si ml_order_update repite order_id.
SELECT b.status,
       COUNT(*)                                              AS filas,
       SUM(b.odoo_so_name IS NOT NULL OR b.cfdi_uuid IS NOT NULL) AS con_huella_odoo
FROM   finance.mkp_billing_prod b
WHERE  b.marketplace = 'MERCADO_LIBRE'
  AND  EXISTS (SELECT 1 FROM somos_reyes.ml_order_update o
               WHERE o.order_id = b.mkp_order_id AND o.seller_id = '160190870')
GROUP  BY b.status;

SELECT p.status, COUNT(*) AS filas
FROM   finance.mkp_payments_prod p
WHERE  p.marketplace = 'MERCADO_LIBRE'
  AND  EXISTS (SELECT 1 FROM somos_reyes.ml_order_update o
               WHERE o.order_id = p.mkp_order_id AND o.seller_id = '160190870')
GROUP  BY p.status;

-- INTERPRETACIÓN:
--   * Sin filas                                  -> saltar al paso 4.
--   * Solo NO_INVOICE_IN_ML / NEVER_BILLED_BY_ML
--     con con_huella_odoo = 0                    -> el token de ventas no pudo leer el
--                                                   XML; nunca tocaron Odoo. Paso 3.
--   * Cualquier fila con con_huella_odoo > 0, o
--     cualquier fila en mkp_payments_prod        -> DETENERSE. Hay facturas de Oficiales
--                                                   en Odoo con CxC 105.01.004 (y quizá
--                                                   cobros en el diario MP). Requiere
--                                                   reclasificación contable con Finanzas,
--                                                   NO se corrige con un UPDATE.


-- ---------------------------------------------------------------------
-- 3) Corrección — SOLO filas sin huella en Odoo. Correr ANTES del primer
--    run de ml_oficiales_billing_process_flow: si el miner de Oficiales ya
--    insertó esas órdenes, este UPDATE choca con uk_mkp_order.
-- ---------------------------------------------------------------------
-- Se re-etiquetan al marketplace correcto conservando su estado. Las que caen
-- después del corte y siguen en NO_INVOICE_IN_ML las retomará el miner de
-- Oficiales con su propio token; las anteriores al corte quedan como histórico.
START TRANSACTION;

UPDATE finance.mkp_billing_prod b
SET    b.marketplace = 'MERCADO_LIBRE_OFICIALES'
WHERE  b.marketplace = 'MERCADO_LIBRE'
  AND  b.status IN ('NO_INVOICE_IN_ML', 'NEVER_BILLED_BY_ML')
  AND  b.odoo_so_name IS NULL
  AND  b.cfdi_uuid IS NULL
  AND  EXISTS (SELECT 1 FROM somos_reyes.ml_order_update o
               WHERE o.order_id = b.mkp_order_id AND o.seller_id = '160190870');

-- Revisar que rowcount = suma de 'filas' del paso 2 para esos estados, luego:
COMMIT;
-- (o ROLLBACK; si no cuadra)


-- ---------------------------------------------------------------------
-- 4) Índice (OPCIONAL)
-- ---------------------------------------------------------------------
-- Los scripts filtran por (marketplace, status). La UNIQUE (marketplace, ...)
-- ya sirve como prefijo para 'marketplace'; con 2 tiendas + Amazon el volumen
-- aún no lo exige. Revisar primero si ya existe algo equivalente:
SHOW INDEX FROM finance.mkp_billing_prod;
SHOW INDEX FROM finance.mkp_payments_prod;
-- ALTER TABLE finance.mkp_billing_prod  ADD INDEX idx_mkp_status (marketplace, status), ALGORITHM = INPLACE, LOCK = NONE;
-- ALTER TABLE finance.mkp_payments_prod ADD INDEX idx_mkp_status (marketplace, status), ALGORITHM = INPLACE, LOCK = NONE;


-- ---------------------------------------------------------------------
-- 5) Validación post-despliegue (después de la primera corrida de cada flujo)
-- ---------------------------------------------------------------------
SELECT marketplace, status, COUNT(*) AS filas
FROM   finance.mkp_billing_prod
WHERE  marketplace IN ('MERCADO_LIBRE', 'MERCADO_LIBRE_OFICIALES')
GROUP  BY marketplace, status
ORDER  BY marketplace, status;

SELECT marketplace, status, COUNT(*) AS filas
FROM   finance.mkp_payments_prod
WHERE  marketplace IN ('MERCADO_LIBRE', 'MERCADO_LIBRE_OFICIALES')
GROUP  BY marketplace, status
ORDER  BY marketplace, status;

-- Debe regresar 0: ninguna orden de Oficiales nueva bajo 'MERCADO_LIBRE'
-- (sustituir la fecha por la del despliegue).
SELECT COUNT(*) AS contaminadas_post_deploy
FROM   finance.mkp_billing_prod b
WHERE  b.marketplace = 'MERCADO_LIBRE'
  AND  b.created_at >= '2026-10-02 00:00:00'
  AND  EXISTS (SELECT 1 FROM somos_reyes.ml_order_update o
               WHERE o.order_id = b.mkp_order_id AND o.seller_id = '160190870');


-- ---------------------------------------------------------------------
-- ROLLBACK (solo si se revierte también el código)
-- ---------------------------------------------------------------------
-- Las filas de Oficiales no se borran: el código anterior las ignoraría porque
-- filtraba por marketplace = 'MERCADO_LIBRE'. Si se aplicó el paso 1, el VARCHAR
-- ampliado es compatible con el código anterior y no hace falta revertirlo.
-- Si se aplicó el paso 4:
-- ALTER TABLE finance.mkp_billing_prod  DROP INDEX idx_mkp_status;
-- ALTER TABLE finance.mkp_payments_prod DROP INDEX idx_mkp_status;
