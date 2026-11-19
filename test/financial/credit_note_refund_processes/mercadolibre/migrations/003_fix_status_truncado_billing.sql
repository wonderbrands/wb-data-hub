-- =====================================================================
-- 003: corrige estados truncados en finance.mkp_billing_prod
--
-- Causa: status era VARCHAR(20) y el servidor corre con sql_mode SIN STRICT
-- ('NO_ENGINE_SUBSTITUTION'), así que el 02 escribía 'ALREADY_ODOO_INVOICED' (21)
-- y MySQL guardaba 'ALREADY_ODOO_INVOICE' (20) sin error. La columna ya es VARCHAR(40):
-- solo falta reparar las filas viejas.
--
-- Efecto colateral a revisar ANTES de aplicar: el 03 de pagos filtra
-- status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED'), así que estas órdenes NUNCA
-- entraron a cobros. Al corregirlas, la siguiente corrida de ml_payments_flow
-- intentará aplicarles el cobro en Odoo (paso 1.c).
-- =====================================================================


-- 1.a) ¿Qué está truncado? Cualquier valor que llene exactamente los 20 caracteres viejos.
SELECT marketplace, status, CHAR_LENGTH(status) AS largo, COUNT(*) AS filas
FROM   finance.mkp_billing_prod
WHERE  CHAR_LENGTH(status) = 20
GROUP  BY marketplace, status;
-- Esperado: solo 'ALREADY_ODOO_INVOICE' (55 en MERCADO_LIBRE). Si aparece otro valor,
-- NO correr el paso 2 a ciegas: averiguar su valor completo primero.

-- 1.b) ¿Otras columnas de estado en finance con el mismo riesgo?
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
FROM   information_schema.COLUMNS
WHERE  TABLE_SCHEMA = 'finance'
  AND  COLUMN_NAME LIKE '%status%'
  AND  CHARACTER_MAXIMUM_LENGTH < 30
ORDER  BY TABLE_NAME;

-- 1.c) Las 55 órdenes: ¿tienen SO en Odoo y ya tienen cobro registrado en staging?
--      Revisar en Odoo el payment_state de esas facturas ANTES del paso 2: si ya están
--      pagadas (por ejemplo, a mano), el 04 fallará con ERROR para esas órdenes
--      (no duplica el cobro: Odoo no deja pagar una factura saldada).
SELECT b.marketplace, b.mkp_order_id, b.odoo_so_name, b.cfdi_uuid, b.total_amount, b.processed_at,
       p.status AS pago_staging
FROM   finance.mkp_billing_prod b
LEFT JOIN finance.mkp_payments_prod p
       ON p.marketplace = b.marketplace AND p.mkp_order_id = b.mkp_order_id
WHERE  b.status = 'ALREADY_ODOO_INVOICE'
ORDER  BY b.processed_at;


-- 2) Corrección
START TRANSACTION;

UPDATE finance.mkp_billing_prod
SET    status = 'ALREADY_ODOO_INVOICED'
WHERE  status = 'ALREADY_ODOO_INVOICE';
-- Debe reportar 55 filas afectadas. Si no, ROLLBACK y revisar.

SELECT status, COUNT(*) FROM finance.mkp_billing_prod
WHERE  status LIKE 'ALREADY_ODOO_INVOICE%' GROUP BY status;

COMMIT;
-- ROLLBACK;
