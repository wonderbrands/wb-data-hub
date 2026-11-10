-- =====================================================================
-- Migración 004: pasar el inventario de NC de *_staging a *_prod
--   Se corre UNA vez, antes del piloto en productivo, para que el 05 y el 06 trabajen
--   sobre tablas definitivas (las _staging pueden borrarse o reiniciarse).
--   Copia lo ya descargado de ML (XML incluidos): no hay que volver a barrer la API.
--
-- ORDEN:
--   1. Que NO esté corriendo el 05 ni el 06 (ni local ni en Kestra).
--   2. Crear las tablas con la migración 002 (secciones 1 y 2).
--   3. Correr esta 004.
--   4. Apuntar el 05 y el 06 a las _prod: NC_TABLE=finance.mkp_credit_notes_prod y
--      NC_SCAN_TABLE=finance.mkp_credit_notes_scan (variables en Kestra, o cambiar el
--      default en los scripts al promoverlos a processes/).
--   5. Conservar las _staging como respaldo hasta cerrar la carga histórica.
-- =====================================================================

-- 1) Cola de revisión del 05 (1 fila por orden)
INSERT INTO finance.mkp_credit_notes_scan
      (marketplace, mkp_order_id, signal_source, reference_date, scan_status, check_count,
       last_http_status, first_checked_at, last_checked_at, next_check_at, created_at)
SELECT marketplace, mkp_order_id, signal_source, reference_date, scan_status, check_count,
       last_http_status, first_checked_at, last_checked_at, next_check_at, created_at
FROM   finance.mkp_credit_notes_scan_staging
ON DUPLICATE KEY UPDATE scan_status = VALUES(scan_status);

-- 2) NC descargadas (1 fila por CFDI de egreso)
INSERT INTO finance.mkp_credit_notes_prod
      (marketplace, mkp_order_id, cfdi_uuid, related_invoice_uuid, tipo_relacion, serie, folio,
       rfc_receptor, subtotal, tax_amount, total_amount, fecha_timbrado, xml_data, raw_json,
       nc_scope, ml_refund_id, ml_claim_id, status, error_log, created_at)
SELECT marketplace, mkp_order_id, cfdi_uuid, related_invoice_uuid, tipo_relacion, serie, folio,
       rfc_receptor, subtotal, tax_amount, total_amount, fecha_timbrado, xml_data, raw_json,
       nc_scope, ml_refund_id, ml_claim_id,
       -- Lo que hizo el 06 en staging apunta al Odoo de PRUEBAS: vuelve a la cola.
       CASE WHEN status IN ('UUID_MISMATCH', 'AMOUNT_EXCEEDS_INVOICE') THEN status ELSE 'PENDING' END,
       CASE WHEN status IN ('UUID_MISMATCH', 'AMOUNT_EXCEEDS_INVOICE') THEN error_log ELSE NULL END,
       created_at
FROM   finance.mkp_credit_notes_staging
ON DUPLICATE KEY UPDATE id = id;   -- idempotente por (marketplace, cfdi_uuid)

-- 3) Verificación: los conteos deben coincidir
SELECT 'scan' AS tabla,
       (SELECT COUNT(*) FROM finance.mkp_credit_notes_scan_staging) AS staging,
       (SELECT COUNT(*) FROM finance.mkp_credit_notes_scan)         AS prod
UNION ALL
SELECT 'nc',
       (SELECT COUNT(*) FROM finance.mkp_credit_notes_staging),
       (SELECT COUNT(*) FROM finance.mkp_credit_notes_prod);

SELECT status, COUNT(*), SUM(total_amount)
FROM   finance.mkp_credit_notes_prod
WHERE  marketplace = 'MERCADO_LIBRE'
GROUP  BY status;
-- Esperado: todo en PENDING salvo UUID_MISMATCH, y odoo_refund_id vacío en todas.
