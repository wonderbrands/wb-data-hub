-- =====================================================================
-- Migración 002: tablas de control de Notas de Crédito ML
--   05_miner_ml_credit_notes.py  -> escribe mkp_credit_notes_scan y mkp_credit_notes_prod
--   06_loader_ml_credit_notes.py -> lee mkp_credit_notes_prod y registra en Odoo
--
-- Diferencia clave vs finance.mkp_billing_prod: una orden puede tener VARIAS NC
-- (parciales, producto + envío, re-emisión), así que la llave es el UUID de la NC
-- y no el mkp_order_id. El "¿ya revisé esta orden en ML?" vive aparte en
-- mkp_credit_notes_scan (1 fila por orden facturada).
--
-- ORDEN DE DESPLIEGUE:
--   1. Sección 0 (solo lectura): tipo real de mkp_order_id y dimensionamiento.
--   2. Secciones 1 y 2 (DDL). Ajustar el tipo de mkp_order_id al de mkp_billing_prod.
--   3. Primer run del miner 05 con ONLY_ORDER_IDS (prueba dirigida) y luego el barrido.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0) Pre-checks y dimensionamiento (solo lectura)
-- ---------------------------------------------------------------------
-- El tipo de mkp_order_id en las tablas nuevas debe ser IDÉNTICO al de
-- mkp_billing_prod (el miner hace JOIN por esa columna en cada corrida).
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
FROM   information_schema.COLUMNS
WHERE  TABLE_SCHEMA = 'finance'
  AND  TABLE_NAME = 'mkp_billing_prod'
  AND  COLUMN_NAME IN ('marketplace', 'mkp_order_id', 'cfdi_uuid', 'created_at');

-- Universo del barrido (1 llamada a ML por orden en el histórico).
SELECT b.marketplace, b.status, COUNT(*) AS ordenes, MIN(b.created_at) AS desde, MAX(b.created_at) AS hasta
FROM   finance.mkp_billing_prod b
WHERE  b.marketplace = 'MERCADO_LIBRE'
  AND  b.status IN ('ODOO_INVOICED', 'ALREADY_ODOO_INVOICED')
GROUP  BY b.marketplace, b.status;

-- Señales de reembolso — CORRER UNA POR UNA.
-- Mismas queries que SIGNAL_QUERIES en el 05. Cada una parte de SU tabla filtrada por
-- tienda + fecha de corte (conjunto chico) y solo después cruza contra mkp_billing_prod.
-- NO usar EXISTS correlacionados desde mkp_billing_prod: recorren toda la facturación
-- contra tablas grandes sin índice útil y no terminan.
--
-- 0.a Conteo directo en la tabla de la señal (sin cruce). Debe ser rápido; si alguno
--     no termina, revisar índices con el bloque 0.c.
SELECT 'ORDER_CANCELLED' AS senal, COUNT(DISTINCT order_id) AS ordenes
FROM   somos_reyes.ml_order_update
WHERE  seller_id = '25523702' AND date_created >= '2026-06-01 15:50:55' AND status = 'cancelled';

SELECT 'PAYMENT_REFUNDED' AS senal, COUNT(DISTINCT order_id) AS ordenes
FROM   somos_reyes.ml_order_payments
WHERE  seller_id = '25523702' AND date_created >= '2026-06-01 15:50:55' AND refunded_amt > 0;

SELECT 'CLAIM_COMPLAINANT' AS senal, COUNT(DISTINCT resource_id) AS ordenes
FROM   somos_reyes.ml_claims
WHERE  seller_user_id = '25523702' AND date_created >= '2026-06-01 15:50:55'
  AND  status = 'closed' AND resolution_benefited LIKE '%complainant%';

SELECT 'FIN_REFUND' AS senal, COUNT(DISTINCT order_id) AS ordenes
FROM   somos_reyes.ml_financial_transactions_master
WHERE  user_id = '25523702' AND transaction_date >= '2026-06-01 15:50:55'
  AND  transaction_type IN ('REFUND', 'REFUND_SHIPPING');

-- 0.b Cuántas de esas órdenes tienen factura viva en Odoo, y cuántas NO (si ML les
--     emite NC no habrá factura contra la cual registrarla: descuadre a revisar aparte).
--     La tabla derivada se materializa una sola vez; cambiar la subconsulta por la de
--     cada señal del bloque 0.a.
SELECT b.status, COUNT(*) AS ordenes
FROM  (SELECT DISTINCT order_id
       FROM   somos_reyes.ml_order_payments
       WHERE  seller_id = '25523702' AND date_created >= '2026-06-01 15:50:55' AND refunded_amt > 0) x
JOIN   finance.mkp_billing_prod b
       ON b.marketplace = 'MERCADO_LIBRE' AND b.mkp_order_id = x.order_id
GROUP  BY b.status;

-- 0.c Índices disponibles (si 0.a tarda: hace falta índice por seller/fecha o por order_id)
-- SHOW INDEX FROM somos_reyes.ml_order_update;
-- SHOW INDEX FROM somos_reyes.ml_order_payments;
-- SHOW INDEX FROM somos_reyes.ml_claims;
-- SHOW INDEX FROM somos_reyes.ml_financial_transactions_master;
-- SHOW INDEX FROM finance.mkp_billing_prod;


-- ---------------------------------------------------------------------
-- 1) Notas de crédito extraídas de ML (1 fila = 1 CFDI de egreso)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS finance.mkp_credit_notes_prod (
    id                    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    marketplace           VARCHAR(50)   NOT NULL,
    mkp_order_id          VARCHAR(32)   NOT NULL,     -- AJUSTAR al tipo de mkp_billing_prod

    -- CFDI de la NC (TipoDeComprobante = 'E')
    cfdi_uuid             CHAR(36)      NOT NULL,
    related_invoice_uuid  CHAR(36)      NULL,         -- CfdiRelacionados/CfdiRelacionado/@UUID
    tipo_relacion         VARCHAR(10)   NULL,         -- '01' esperado
    serie                 VARCHAR(25)   NULL,         -- 'MELINC' observado
    folio                 VARCHAR(40)   NULL,
    rfc_receptor          VARCHAR(13)   NULL,
    subtotal              DECIMAL(14,2) NULL,
    tax_amount            DECIMAL(14,2) NULL,
    total_amount          DECIMAL(14,2) NULL,
    fecha_timbrado        DATETIME      NULL,         -- Hora local del SAT (CDMX)
    xml_data              LONGTEXT      NULL,         -- Base64, igual que mkp_billing_prod
    raw_json              LONGTEXT      NULL,

    -- Clasificación
    nc_scope              VARCHAR(10)   NULL,         -- FULL | PARTIAL (lo llena el 05)
    operation_class       CHAR(1)       NULL,         -- A | B | C (lo llena el 06)
    ml_refund_id          VARCHAR(32)   NULL,
    ml_claim_id           VARCHAR(32)   NULL,

    -- Trazabilidad Odoo (lo llena el 06)
    odoo_so_name          VARCHAR(64)   NULL,
    odoo_invoice_id       INT           NULL,
    odoo_refund_id        INT           NULL,
    odoo_refund_name      VARCHAR(64)   NULL,
    invoice_payment_state VARCHAR(20)   NULL,         -- payment_state de la factura al registrar la NC

    -- Control
    status                VARCHAR(40)   NOT NULL DEFAULT 'PENDING',
    error_log             TEXT          NULL,
    retry_count_loader    INT           NOT NULL DEFAULT 0,
    created_at            DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at          DATETIME      NULL,

    PRIMARY KEY (id),
    UNIQUE KEY uk_mkp_nc_uuid (marketplace, cfdi_uuid),
    KEY idx_mkp_nc_order  (marketplace, mkp_order_id),
    KEY idx_mkp_nc_status (marketplace, status)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- Estados:
--   05 -> PENDING | UUID_MISMATCH | AMOUNT_EXCEEDS_INVOICE | MANUAL_REVIEW
--   06 -> PROCESSING | REFUND_DRAFT | REFUND_POSTED | ODOO_CREDITED | ALREADY_ODOO_CREDITED
--         | WAIT_INVOICE_IN_ODOO | INVOICE_CANCELLED_IN_ODOO | PERIOD_LOCKED | ERROR | ERROR_BG


-- ---------------------------------------------------------------------
-- 2) Cola de revisión del endpoint de devolución (1 fila = 1 orden facturada)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS finance.mkp_credit_notes_scan (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    marketplace       VARCHAR(50)  NOT NULL,
    mkp_order_id      VARCHAR(32)  NOT NULL,          -- AJUSTAR al tipo de mkp_billing_prod
    signal_source     VARCHAR(40)  NOT NULL,          -- SWEEP | ORDER_CANCELLED | PAYMENT_REFUNDED
                                                      -- | CLAIM_COMPLAINANT | FIN_REFUND | MANUAL
    reference_date    DATETIME     NOT NULL,          -- SWEEP: entrada a mkp_billing_prod; señal: detección;
                                                      -- FOUND_PARTIAL: fecha de la última NC encontrada
    scan_status       VARCHAR(30)  NOT NULL DEFAULT 'WAITING',
                                                      -- WAITING | FOUND | FOUND_PARTIAL | CLOSED
                                                      -- | NEVER_CREDITED_BY_ML | INVALID_DOCUMENT
    check_count       INT          NOT NULL DEFAULT 0,
    last_http_status  SMALLINT     NULL,
    first_checked_at  DATETIME     NULL,
    last_checked_at   DATETIME     NULL,
    next_check_at     DATETIME     NULL,
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (id),
    UNIQUE KEY uk_mkp_nc_scan (marketplace, mkp_order_id),
    KEY idx_mkp_nc_scan_due (marketplace, scan_status, next_check_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;


-- ---------------------------------------------------------------------
-- 3) Monitoreo (solo lectura) — correr tras cada barrido
-- ---------------------------------------------------------------------
-- Avance de la cola
-- SELECT signal_source, scan_status, COUNT(*) FROM finance.mkp_credit_notes_scan
-- WHERE marketplace = 'MERCADO_LIBRE' GROUP BY signal_source, scan_status;
--
-- Inventario de NC
-- SELECT status, nc_scope, COUNT(*), SUM(total_amount) FROM finance.mkp_credit_notes_prod
-- WHERE marketplace = 'MERCADO_LIBRE' GROUP BY status, nc_scope;
--
-- Órdenes con más de una NC (punto 8: ver cómo las entrega la API)
-- SELECT mkp_order_id, COUNT(*), GROUP_CONCAT(cfdi_uuid) FROM finance.mkp_credit_notes_prod
-- WHERE marketplace = 'MERCADO_LIBRE' GROUP BY mkp_order_id HAVING COUNT(*) > 1;
