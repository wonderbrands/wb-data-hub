# Notas de Crédito Mercado Libre → Odoo 18 — Plan técnico

Rama: `feat/ml-credit-notes` · Actualizado: 2026-10-07
**MVP:** ML ventas (seller 25523702, marketplace `MERCADO_LIBRE`). **Oficiales** (160190870) entra después de que la tienda principal esté en producción. El código ya es multi-tienda vía `ml_stores.py`, así que activarla será solo agregar un flujo.

---

## 1. Lo que ya está decidido (no requiere a Finanzas)

| Tema | Decisión |
|------|----------|
| **Fuente de verdad** | El CFDI de egreso que ML timbra con nuestro sello: `GET /invoices/io/documents/stream/order/{id}/xml?transaction_type=devolution`. **No se fabrican NC desde tablas**: las tablas (`ml_order_update`, `ml_order_payments`, `ml_claims`, `ml_financial_transactions_master`) solo sirven para priorizar qué órdenes consultar y para detectar faltantes. |
| **Validación del XML** | CFDI 4.0, `TipoDeComprobante="E"`, RFC emisor `SRE1410278MA`, TFD con UUID, y `CfdiRelacionados` con `TipoRelacion="01"` apuntando al UUID de la factura que está en Odoo. Lo observado: `Serie="MELINC"`. Cualquier otra cosa no entra. |
| **Cuentas** (Odoo 18, formato con `.`) | Ingreso: **402.01.001** Devoluciones, descuentos o bonificaciones sobre ventas y/o servicios a tarifa general. IVA: **208.01.001** IVA trasladado cobrado (sale del reparto de reembolso del impuesto 38; se verifica en staging). CxC: **105.01.004** Mercado Libre (después, 105.01.005 para Oficiales, ya en `ml_stores.py`). |
| **Diarios** | Se conservan los dos diarios del proceso de Odoo 15: **DEV "Facturas Devoluciones"** para NC totales o de línea completa (cancelación o devolución, casos A y B) y **BON "Facturas Bonificaciones"** para NC parciales (caso C). Los dos son de tipo venta, con cuenta por defecto 402.01.001. *Acción técnica:* verificar o crear ambos en Odoo 18. |
| **Líneas de la NC** | **DEV:** mismo producto, cantidad y precio que la línea de la factura, **sin `sale_line_ids`**. **BON:** producto **[BON_SRV] BONIFICACION_SRV**, descripción `BON_SRV-<nombre del producto>`, importe = `SubTotal` del XML. En los dos casos: `account_id = 402.01.001` explícito e impuesto 38. |
| **Vínculo con la factura** | `reversed_entry_id = factura` (vínculo contable nativo), `l10n_mx_edi_cfdi_origin = '01\|<UUID factura>'`, `invoice_origin = <SO>` (solo texto), `ref = 'MLNC:<UUID NC>'` (llave de idempotencia) y `payment_reference = '<factura Odoo> \| UUID origen <uuid> \| NC ML <serie>-<folio>'`. |
| **No se timbra nunca** | Se adjunta el XML de ML y se replica el mismo estado EDI "enviado" que el 02 pone a las facturas. |
| **Fecha contable** | La de `FechaTimbrado` de la NC, porque es la verdad fiscal. Si un periodo está bloqueado, el lote queda `PERIOD_LOCKED`, sin forzar nada. |
| **Log de conciliación** | Chatter (`message_post`) en la SO y en la factura: NC Odoo, UUID NC, serie-folio ML, tipo A/B/C y refund/claim ID si existe. |
| **COGS** (A: Mercancías Facturadas No Enviadas · B: Devoluciones en tránsito) | **Fase 2.** Las cuentas aún no existen y el MVP no depende de ellas. |

### 1.1 ¿Por qué ligar la NC a la SO reabre la orden para facturar? (respuesta al punto 2)

**Es comportamiento estándar de Odoo, no un bug.** En `sale.order.line`, `qty_invoiced` = cantidad facturada − cantidad acreditada en las líneas de factura ligadas por `sale_line_ids`. Con política de facturación **"cantidades ordenadas"**, `qty_to_invoice = ordenado − qty_invoiced`. Al ligar una NC, la línea vuelve a quedar "por facturar".

Odoo lo diseña así porque una NC, por sí sola, no dice si la venta se anuló o si se va a **re-emitir** (por ejemplo, una NC por datos fiscales erróneos seguida de una nueva factura). En el flujo nativo, una devolución real se cierra de una de dos maneras:

- con política **"cantidades entregadas"** + un RET con *"Actualizar cantidades en SO"*, que baja `qty_delivered` y deja `qty_to_invoice = 0`;
- reduciendo la cantidad de la SO o cancelándola.

**¿Debe poder re-facturarse? En ML, no.** El emisor fiscal es ML: si nosotros re-facturamos, duplicamos un ingreso que ML ya canceló con su egreso. Además, el 1a1 toma justamente las SO `to invoice` + `locked`, y solo excluye los equipos `MercadoLibre` posteriores al corte (`facturacion_automatica_1a1.py:630`). Por eso **la decisión correcta es la que ya usabas: no ligar `sale_line_ids`.** La trazabilidad queda por `reversed_entry_id` (factura↔NC), `invoice_origin` y el chatter.

---

## 2. Arquitectura (homóloga a 01–04)

```
ml_billing_process_flow (01→02) ─► ml_payments_flow (03→04) ─► ml_credit_notes_flow (05→06)
```

### 2.1 `05_miner_ml_credit_notes.py` — **LISTO**

Solo escribe en staging; no toca Odoo. Mismo patrón que el 01 (MySQLdb, token por seller, logs de resumen para Kestra, `SystemExit(1)` con 401).

**Ubicación:** en fase de pruebas vive en `test/financial/credit_note_refund_processes/` (en `processes/` no va nada de pruebas). Reutiliza `ml_stores.py` desde `processes/.../mercadolibre/` y el flujo Kestra apunta a la ruta de `test/`. Al pasar a producción se mueve junto a 01–04.

- **Cola `finance.mkp_credit_notes_scan`**, una fila por orden con factura viva (`ODOO_INVOICED` / `ALREADY_ODOO_INVOICED`):
  - **SWEEP:** *todas* las órdenes facturadas, revisadas a +15, +45 y +90 días desde que entraron a facturación, y luego cerradas. **Esto es el barrido histórico:** en la primera corrida, toda orden de junio a hoy se consulta una vez.
  - **Señales**, que se revisan primero, en la corrida en que aparecen, y luego con backoff de 1, 1, 2, 4 y 7 días hasta 45 días:
    - `ORDER_CANCELLED`: `ml_order_update.status = 'cancelled'`.
    - `PAYMENT_REFUNDED`: `ml_order_payments.refunded_amt > 0`; atrapa también los reembolsos parciales.
    - `CLAIM_COMPLAINANT`: `ml_claims` cerrado con `complainant`.
    - `FIN_REFUND`: `REFUND` / `REFUND_SHIPPING` en `ml_financial_transactions_master`.

    Cada señal se consulta **desde su propia tabla**, filtrada por seller y fecha de corte (conjunto chico), y se cruza por lotes de 1,000 con `IN (...)` contra la cola. No se usan `EXISTS` correlacionados desde `mkp_billing_prod`: recorren toda la facturación y no terminan (se comprobó en MySQL Workbench). Si una señal falla, se registra un warning y la corrida sigue; el barrido cubre esa orden.

    Si una señal se vence sin NC → `NEVER_CREDITED_BY_ML`, para revisión; posiblemente ML canceló la factura en el SAT en vez de emitir NC.
- **Varias NC por orden:** la llave es el UUID de la NC. Si la suma de NC no cubre la factura, la orden sigue en revisión (`FOUND_PARTIAL`). Cada NC adicional se loguea como anomalía y se ve en el monitoreo de `002_*.sql`. Así observamos cómo entrega la API los casos con más de una NC.
- **Estado inicial de cada NC:** `PENDING`; `UUID_MISMATCH` si la NC relaciona otro UUID distinto al de la factura en staging (re-facturación de ML o factura del 1a1); `AMOUNT_EXCEEDS_INVOICE`; o `MANUAL_REVIEW` si tiene más de un relacionado, `TipoRelacion ≠ 01` o moneda distinta de MXN. El 05 también marca `nc_scope` como FULL o PARTIAL.
- **Robustez del endpoint:** 404 → cadencia; 429 → backoff con `Retry-After`, y si siguen llegando, corte limpio (la siguiente corrida retoma); 5xx o red → 3 reintentos; contenido inválido (JSON, HTML o factura I) → `INVALID_DOCUMENT`, sin cargar nada.
- **Parámetros:** `SCAN_MODE` (both | signals | sweep), `ONLY_ORDER_IDS` (prueba dirigida), `MAX_CALLS_PER_RUN` y `MAX_RUNTIME_MINUTES` (el histórico se completa en varias corridas si hace falta), `SLEEP_BETWEEN_CALLS`.
- **Probado offline:** 25 checks del parser, la clasificación y la cadencia contra XML reales (NC E, factura I, JSON de error, HTML, RFC ajeno, sin relacionados). **Falta la primera corrida contra la BD y la API.**

### 2.2 `06_loader_ml_credit_notes.py` — siguiente

Se construye a partir del 02, reusando `OdooModelProxy`, los lotes, el reset de `PROCESSING`, `ERROR_BG`, la lógica de reintentos de `action_post` y el resumen por lote.

1. **Validación fail-closed contra Odoo:**
   - la factura existe, está `posted`, y su `l10n_mx_edi_cfdi_uuid` coincide con el relacionado de la NC;
   - no existe ya un `account.move` con ese UUID de NC ni con `ref = MLNC:<uuid>`;
   - Σ de NC ≤ total de la factura.

   Si la factura está cancelada → `INVOICE_CANCELLED_IN_ODOO`; si no existe → `WAIT_INVOICE_IN_ODOO`. Una SO cancelada con factura viva **sí procede**: la NC es justamente lo que falta.
2. **Clasificación A/B/C:**
   - FULL o línea completa + sin OUT `done` → **A** (diario DEV);
   - FULL o línea completa + con OUT `done` → **B** (diario DEV);
   - parcial que no coincide con ninguna línea → **C** (diario BON).

   El OUT se detecta igual que en `dispatchs_alerts.py` (`stock.picking`, `picking_type_code = 'outgoing'`, `state = 'done'`).
3. **Creación** de la `out_refund` (sección 1), adjunto XML y EDI "enviado", y `action_post` por lote. Antes del `create` se re-busca la llave, y si el `create` dio timeout se adopta el registro existente en vez de reintentar.
4. **Conciliación:**
   - **Factura sin cobro:** se concilia NC ↔ factura y baja la CxC 105.
   - **Factura cobrada:** la NC queda posteada como saldo a favor en 105 (`ODOO_CREDITED_PAID`). El tramo de caja de la fase 1.5 rompe la conciliación pago↔factura, concilia NC↔factura y registra la salida de MP, todo junto y por orden. Así nunca queda un pago "flotando" a medias.
5. **Chatter** en la SO y en la factura.

### 2.3 Ajuste necesario en pagos (03)

`03_miner_ml_payments.py` inserta `transaction_amount` aunque haya un reembolso parcial (MP deja `approved / partially_refunded`). Tiene que insertar `transaction_amount − transaction_amount_refunded` y omitir el pago si el neto es ≤ 0. Sin este ajuste, una NC parcial seguida del cobro deja un sobrepago en 105.

---

## 3. Plan de acción

| Fecha | Paso | Salida |
|-------|------|--------|
| **Mié 7-oct** | ✅ 05 + DDL + flujo Kestra escritos y probados offline. Pendiente hoy: **aplicar `002_mkp_credit_notes_prod.sql`** (sección 0 primero, para ajustar el tipo de `mkp_order_id`). Correr el flujo con `only_order_ids=2000018238217954,2000016723575980,2000016949680478`, apuntando `vars.branch` a esta rama o con el merge ya hecho (el 05 no toca Odoo). | La prueba dirigida trae las NC conocidas con estado `PENDING`. |
| **Mié 7-oct (noche)** | **Barrido histórico** del 05: ejecución manual con `scan_mode=sweep`, y luego `both`. | Cola sembrada y NC de junio a hoy en staging. |
| **Jue 8-oct** | **Inventario** con las queries de monitoreo: # NC, FULL/PARTIAL, `UUID_MISMATCH`, multi-NC, `NEVER_CREDITED_BY_ML`. Arranca el 06. En Odoo 18 staging: verificar o crear los diarios DEV y BON y el producto BON_SRV, el reparto de reembolso del impuesto 38 → 208.01.001, y el estado EDI de una factura ML real. | Volumen real y prerrequisitos de Odoo listos. |
| **Jue 8 – Vie 9** | 06 + ajuste del 03, probados en **staging de Odoo.sh**: A sin cobro, B, C, factura cobrada, público general, SO cancelada, re-ejecución (0 duplicados) y kill a media corrida (se reanuda). | Los 8 casos en verde. |
| **Lun 12-oct** | **Canary en producción:** `ONLY_ORDER_IDS` con ~10 NC (todos los casos). Finanzas revisa los asientos. | VoBo de Finanzas. |
| **Mar 13 – Mié 14** | **Barrido histórico del 06** por mes (jun → oct). Conciliación: Σ NC en Odoo = Σ NC en staging en estados finales. | Diferencia = 0; cola manual documentada. |
| **Jue 15-oct** | **Producción diaria:** el 06 se agrega a `ml_credit_notes_flow`, encadenado después de pagos. | 3 corridas limpias. |
| **Fase 1.5** | Tramo de caja (romper conciliación + salida MP con la fecha del refund, solo si hubo débito real en MP; los casos BPP o con cobertura de ML quedan en la cola manual). | — |
| **Fase 2** | COGS A/B y liberación con el RET, Oficiales (flujo `ml_oficiales_credit_notes_flow` con `ML_SELLER_ID=160190870`), validación SAT de vigencia de la NC y de facturas canceladas por ML. | — |

### Lo único que se le pide a Finanzas

- **VoBo del canary** (lun 12-oct) y de cada mes del histórico.
- **Si aparece `PERIOD_LOCKED`:** abrir el periodo o autorizar la fecha.
- **Antes de la fase 1.5 (no bloquea el MVP):** tratamiento de los reembolsos con cobertura de ML (BPP, `applied_coverage=true`), donde el dinero pudo no salir de nuestra cuenta de MP.

---

## 4. Riesgos y edge cases

### 4.1 Duplicar una NC o un timbrado (prioridad 1)

| Caso | Control |
|------|---------|
| Dos corridas o reintentos crean dos NC | UNIQUE `(marketplace, cfdi_uuid)` en staging + re-búsqueda por UUID y `ref` en Odoo justo antes del `create` + `concurrency.limit: 1` + reset de `PROCESSING`. |
| El `create` llegó a Odoo pero la respuesta se perdió (502) | Se busca por `ref = MLNC:<uuid>` y se adopta el registro; nunca se reintenta a ciegas. |
| Odoo re-timbra la NC ("Enviar e imprimir") | Estado EDI "enviado" igual al de las facturas. Se verifica en staging. Aviso a Finanzas: estas NC no se timbran. |
| Ya existe una NC manual (timbrada por Odoo) para la factura | Si hay `out_refund` con `reversed_entry_id = factura` → `ALREADY_ODOO_CREDITED` + alerta (posible doble egreso en el SAT). |
| SO cancelada en Odoo con factura del proceso automático | Procede. Se registra en el log. |
| Factura cancelada en Odoo y CFDI vigente | `INVOICE_CANCELLED_IN_ODOO` → manual. |
| La factura en Odoo es del 1a1, o ML re-facturó la orden (se observó un caso con dos facturas I para la misma orden) | `UUID_MISMATCH` desde el 05 → manual. Nunca se registra una NC contra un UUID que no relaciona. |
| El endpoint devuelve la factura I o JSON con 200 | Se valida `TipoDeComprobante = E` → `INVALID_DOCUMENT`. |
| ML cancela la NC y emite otra | Si llega un UUID nuevo en una orden ya cubierta al 100%, sale como anomalía y queda `AMOUNT_EXCEEDS_INVOICE`; no se carga. |

### 4.2 Endpoint de ML

- Regresa **un documento por llamada**. El comportamiento con varias NC se observa con datos reales (monitoreo "órdenes con más de una NC") y se ajusta si hace falta (punto 8).
- Si no hay NC: 404 `{"message":"Invoice não encontrada"}`.
- Rate limit: se maneja con 429/`Retry-After` y corte limpio. Duración del histórico ≈ *órdenes facturadas × (latencia ~0.3 s + 0.1 s)*: unas 11 h por cada 100k órdenes, repartidas en corridas de hasta 5 h (`MAX_RUNTIME_MINUTES`). La sección 0 del SQL da el número exacto.
- Zonas horarias: `ml_order_update.date_created` está en UTC-4; `created_at` y la cola en UTC; `FechaTimbrado` en hora CDMX.

### 4.3 Odoo

- **Periodos cerrados del histórico:** se agrupa por mes; si un mes está bloqueado, todo ese lote queda `PERIOD_LOCKED`.
- **Impuesto 38 con base flujo:** la NC pegaría primero en "IVA no cobrado" y pasaría a 208.01.001 al conciliar. Se verifica en staging.
- **Factura con varios pagos o pagada parcialmente:** se resuelve en la fase 1.5. En el MVP, la NC queda como saldo a favor.
- **Carrera con el 04:** se evita con el encadenamiento después de pagos y re-leyendo `payment_state` justo antes de conciliar.
- **Pedido multi-línea con devolución de una sola línea:** se emparejan los conceptos de la NC con las líneas de la factura (descripción + importe). Si coincide una línea completa → DEV; si no coincide → BON.
- **NC solo de envío** (`REFUND_SHIPPING`): la factura de ML excluye el envío → `MANUAL_REVIEW`.

### 4.4 Fuera del alcance de este flujo

- Órdenes anteriores al corte, facturadas por el 1a1 (timbradas por Odoo): ML no emite NC de facturas que no timbró.
- Órdenes con NC de ML pero sin factura en Odoo (`NEVER_BILLED_BY_ML`, `TOTAL_DIFF`, `ORDER_NEVER_IN_ODOO`): la sección 0 del SQL las dimensiona como descuadre aparte.
- Referencias de iteraciones anteriores (`ml_traza/`, `test/.../05_miner_ml_refunds.py`): `ml_traza` se usó solo como muestra de forma del XML; `05_miner_ml_refunds.py` no se usó.

### 4.5 Criterios de aceptación del MVP

1. 0 NC duplicadas (UUID único en `account.move`).
2. Σ NC en Odoo = Σ NC en staging en estados finales, por mes.
3. Toda NC cargada tiene: XML adjunto, UUID, `reversed_entry_id`, diario DEV o BON, cuenta 402.01.001, conciliación (si la factura no estaba cobrada) y chatter en la SO y la factura.
4. Ninguna SO vuelve a `to invoice` por una NC.
5. Toda NC no cargada queda con un estado explícito y su `error_log`.
6. Re-ejecutar el histórico completo crea 0 registros nuevos.
