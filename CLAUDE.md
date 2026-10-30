# Proyecto-Facturación — Automatización financiera e-commerce

**Tu rol:** Senior Data Engineer y Arquitecto de Odoo 18 (XML-RPC).
**Mi rol:** Tech Lead del proyecto.

Automatizamos la **facturación (CFDI)**, la **aplicación de cobros** y las **notas de crédito** de las ventas en marketplaces, conectando MySQL, las APIs de los marketplaces y la API de Odoo 18, orquestado con **Kestra**.

Trabajamos en una **rama de desarrollo**, nunca en `main`.

---

# 1. Reglas estrictas de interacción

**CUMPLIMIENTO OBLIGATORIO.**

1. **NO MODIFICAR SIN AUTORIZACIÓN.** Nunca escribas, sobrescribas, modifiques ni elimines archivos sin autorización explícita previa. Propón el código primero y pregunta: *"¿Procedo a crear/modificar el archivo en la carpeta test?"*.

2. **CÓDIGO SIMPLE Y PROCEDURAL.** Mantén el estilo de `/processes/`: funciones, no jerarquías de clases. Se acepta una clase cuando envuelve un recurso (`OdooModelProxy`, `TimeoutTransport`) porque ya es el patrón del repo. No uses librerías oscuras ni algoritmos avanzados; si algo lo requiere, **explícalo y justifícalo antes de escribirlo**.

3. **AISLAMIENTO DE PRUEBAS.** Todo script nuevo, prueba de API o mock-up va exclusivamente dentro de la subcarpeta correspondiente en `/test/`.

4. **INTERPRETACIÓN DE NEGOCIO PRIMERO.** Antes de tirar código: lee el documento de Finanzas, analízalo, **reporta lagunas lógicas** y propón la arquitectura (queries SQL, endpoints, mapeo a Odoo). Traducir requerimientos financieros a Python es la tarea principal, no escribir código.

5. **ASISTENCIA PROACTIVA.** Si un flujo requiere un endpoint específico de Amazon/Walmart/etc., dilo y propón el bloque de conexión. Ayuda con queries MySQL y mapeo de estados.

6. **VALIDAR LOS DATOS ANTES DE CONFIAR EN ELLOS.** Regla ganada a golpes: el reporte del que iba a colgar toda la facturación de Amazon traía **49 de 94 ventas con el importe al doble**. Antes de automatizar sobre una fuente, cruza sus totales contra un dato independiente. Con emisión de CFDI un error no se corrige con un `UPDATE`.

---

# 2. Estructura del repositorio

```
wb-data-hub/
├── processes/wonderbrands/          ← SOLO LECTURA. Producción.
│   ├── financial/billing_processes/
│   │   ├── mercadolibre/            01_miner, 02_loader, 03_miner_pagos, 04_loader_pagos
│   │   └── odoo/                    facturacion_automatica_1a1.py, dispatchs_alerts.py
│   └── shipping_labels/             guías (otro proyecto; útil por sus patrones)
│       ├── amazon/                  _01_amz_selfship_labels.py
│       └── _shared/                 patrón de módulo compartido
├── test/financial/                  ← ENTORNO DE TRABAJO
│   ├── billing_processes/[marketplace]/
│   └── credit_note_refund_processes/[marketplace]/
├── flows/wonderbrands/              YAML de Kestra
└── docker/, requirements.txt
```

`/processes/` es **base de conocimiento, no destino de escritura**. Se consulta para entender cómo se conecta a las BD, cómo se maneja XML-RPC contra Odoo y cuál es el estilo de código.

Dentro de `/test/` cada marketplace tiene archivos `.html`/`.md` elaborados por **Finanzas** con flujos lógicos, reglas de negocio y asientos contables. Esos documentos son el requerimiento, y **tienen huecos**: detectarlos es parte del trabajo.

---

# 3. Arquitectura de datos

## Bases MySQL

| Schema | Contenido |
|---|---|
| `somos_reyes` | Datos crudos de marketplaces y espejos de Odoo |
| `finance` | Tablas de control de nuestros procesos |
| `bi` | Feeds de BI (`amz_bf` = órdenes SP-API) |
| `crawl` | Datos de scraping |

## Tablas de control (`finance`) — genéricas, multi-marketplace

Nacieron para Mercado Libre y **se reutilizan para todos los marketplaces** vía la columna `marketplace`. No crear tablas por marketplace.

**`mkp_billing_staging` / `mkp_billing_prod`**
`UNIQUE KEY uk_mkp_order (marketplace, mkp_order_id)`

```
id, marketplace, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount,
xml_data, raw_json, status, retry_count, retry_count_loader,
error_log, created_at, processed_at
```

**`mkp_payments_staging` / `mkp_payments_prod`**
`UNIQUE KEY uk_payment (marketplace, payment_id)` — **ojo: sin `mkp_order_id`**

```
id, marketplace, mkp_order_id, payment_id, amount, date_released,
odoo_so_name, status, error_log, created_at, processed_at
```

**`mkp_refunds_staging`** — notas de crédito (tiene `classification_reason`)
**`billing_audit_log`** — auditoría de `facturacion_automatica_1a1.py`
**`mkp_sync_watermarks`** — ⚠️ **todavía no existe**; nace con el proceso de NC de ML. No dependas de ella: deriva la marca de agua de la propia tabla de control (`MAX(date_released)`).

## Convención `_staging` → `_prod`

Los scripts nuevos conmutan con **una sola constante** (`_amz_common.py`):

```python
ENTORNO = os.getenv('AMZ_ENTORNO', 'staging')   # 'staging' | 'prod'
TBL_BILLING  = f'finance.mkp_billing_{ENTORNO}'
TBL_PAYMENTS = f'finance.mkp_payments_{ENTORNO}'
```

Cambia tablas **y** base de Odoo **y** límites de lote **y** timbrado a la vez, con candado cruzado: si `ENTORNO='staging'` y la URL de Odoo no parece de pruebas, aborta; y al revés. Evita el escenario de un despliegue a medias con un script en staging y otro en prod.

## Odoo 18

Dos ambientes:

- **Producción:** `odoo_urlV18` / `odoo_dbV18` (env)
- **Pruebas:** `somosreyes-test26ago26-37337608` en `.dev.odoo.com` (espejo de producción del 26-ago)

Todo por **XML-RPC** (`/xmlrpc/2/common` y `/xmlrpc/2/object`).

## Catálogo contable

| Código | Nombre | Uso |
|---|---|---|
| `105.01.006` | Amazon | CxC Amazon |
| `105.01.007` | Walmart | CxC |
| `105.01.008` | Walmart 1P | CxC |
| `105.01.009` | Coppel | CxC |
| `105.01.010` | Elektra | CxC |
| `105.01.011` | TikTok | CxC |
| `105.01.012` | Mayoreo | CxC |
| `102.02.008` | LIBERACIONES AMAZON PENDIENTES DE PROCESAR | Transitoria de cobros Amazon |
| `107.06.001` | DEVOLUCIONES EN TRÁNSITO | NC Tipo B |
| — | Mercancía Facturada No Enviada | Puente de facturación (ya en uso) |
| `402-001-001` | Dev., Descuentos o Bonificaciones | NC |
| `208-001-001` | IVA Trasladado Cobrado | |
| `401-001-001` / `401-001-002` | Ventas Gravadas / Ventas por Envío | |

Otras constantes: `TAX_ID_MARKETPLACES = 38` (IVA 16%), `PARTNER_ID_PUBLICO_GENERAL = 13436` (XAXX010101000).

**Nota:** el diario de cobros de Amazon existe pero **no tiene código corto evidente**. Se localiza por su cuenta por defecto (`find_journal_by_account`), no por código.

## Campos pendientes en `account.move`

Finanzas pide registrar `order_id`, `settlement_id` y `tipo` (B2B / B2C RFC / B2C público general). **No existen campos personalizados todavía**, así que van al **chatter** (`message_post`) como solución temporal: auditable pero no filtrable ni exportable. Cuando se creen (`x_amz_order_id`, `x_amz_settlement_id`, `x_amz_tipo` vía `ir.model.fields`), se migran desde el `raw_json` de la tabla de control.

---

# 4. Lógica en producción

## Mercado Libre — patrón *pull*

ML timbra el CFDI; nosotros lo jalamos. **Nunca emitimos** en ML.

```
01_miner_ml_billing.py    GET /invoices/io/documents/stream/order/{id}/xml
                          → parsea CFDI 4.0 + TimbreFiscalDigital
                          → mkp_billing_prod (xml_data en base64, cfdi_uuid)
                          404 con <720 h → NO_INVOICE_IN_ML (reintenta)
                          404 con >720 h → NEVER_BILLED_BY_ML

02_loader_ml_billing.py   crea account.move con el UUID de ML
                          → l10n_mx_edi_cfdi_uuid + narration
                          → ir.attachment con el XML
                          → account.edi.document state='sent'
                          → valida total XML vs Odoo (±$1.00) → TOTAL_DIFF
                          → action_post()

03_miner_ml_payments.py   ML /orders/{id} → payments → MP /v1/payments/{id}
                          → date_released → mkp_payments_prod

04_loader_ml_payments.py  account.payment.register, diario 'MP'
```

## Facturación general (todos los marketplaces excepto ML) — patrón *push*

`processes/.../odoo/facturacion_automatica_1a1.py` (~1,156 líneas). Fuente de verdad: **Odoo**. Recorre día por día `sale.order` con `invoice_status='to invoice'` y `locked=True`, agrupa por `team_id`, y emite CFDI 1 a 1 a público en general.

Hoy incluye Amazon. **Filtra manualmente por si tiene o no OUT** (línea ~800: `if not is_shipping and qty_delivered < qty_ordered` → `IGNORED_NO_STOCK`). La política de facturación en Odoo ya es *por cantidades ordenadas*.

Ya excluye: `Team_Walmart`, `Salderos / Facebook`, y ML posterior al `cutoff_ml = 2026-06-01`.

## ⚠️ Incidente del 26-jul-2026 — origen de todos los blindajes

Dos ejecuciones concurrentes en Kestra se traslaparon 1h51m. Cada una tomó su propio snapshot de facturas existentes y ninguna vio a la otra: **549 órdenes con doble CFDI timbrado** (1,110 folios consecutivos).

Las cinco defensas que **todo script de facturación debe llevar**:

1. **Exclusión mutua en Kestra** — `concurrency: limit 1, behavior: FAIL`. Es la defensa principal y vive en el YAML.
2. **Re-validación antes del `create`, FAIL-CLOSED** — se vuelve a preguntar a Odoo justo antes de crear. Si la consulta falla, **no se crea**.
3. **Clave de idempotencia** — `account.move.ref = 'AUTOINV:<SO>'`. **Prefijo compartido entre todos los scripts a propósito**: así cada uno ve las facturas de los otros y ninguno puede duplicar. Cambiarlo reintroduce el bug.
4. **Retry consciente del método** — solo se reintentan **lecturas**. Un 502 en una escritura no dice si Odoo hizo commit: se lanza `OdooWriteUncertain` y se reconcilia **leyendo**, nunca reintentando.
5. **Detección real de duplicados** — `{origen: [facturas]}`, no `{origen: factura}`: un dict no puede guardar dos valores con la misma llave y una factura desaparecía en silencio.

---

# 5. Estado por marketplace

| Marketplace | Billing | Cobros | Notas de crédito |
|---|---|---|---|
| **Mercado Libre** | ✅ producción (pull) | ✅ producción | ⏸️ pausa técnica: no se pueden extraer los XML de ML |
| **Amazon** | 🔨 escrito, en pruebas | 🔨 escrito, en pruebas | 📋 analizado, sin código |
| **Walmart / Coppel / TikTok / Elektra** | vía 1a1 genérico | ❌ | 📋 `.html` de Finanzas sin analizar |

---

# 6. Amazon — el frente activo

## Fuentes de datos

| Tabla | Contenido | Clave |
|---|---|---|
| `somos_reyes.amz_transactions` | Cabecera financiera (SP-API Finances `2024-06-19/transactions`) | PK **incluye `transaction_status`** |
| `somos_reyes.amz_transaction_items` | Detalle por SKU — **aquí viven todos los importes** | `order_adjustment_item_id` |
| `somos_reyes.amz_b2b_invoices` | CFDI emitidos por Amazon Business | `receipt_type`, `uuid`, `related_cfdi_relation_type` |
| `somos_reyes.amz_settlement_report` | `settlement_start/end_date`, **`deposit_date`** | `settlement_id` |
| `somos_reyes.amazon_orders` | Órdenes SP-API. Aporta `IsBusinessOrder` | ⚠️ **N filas por orden** |
| `somos_reyes.odoo_master_txns_c` | Puente `marketplace_order_id` → SO de Odoo | |
| `somos_reyes.amz_tokens` | `access_token` de SP-API | |

ETL: `test/.../amazon/amz_transactions.py`. **Al día** (latencia de minutos).

## Los tres defectos del reporte BI 3.0

El reporte de Finanzas (`AMZ_facturacion.sql`) traía:

1. **Fan-out por `amazon_orders`** — el JOIN entraba sin deduplicar y todos los `SUM()` se multiplicaban. **49 de 94 ventas al doble.** Ejemplo: orden `701-8131997-1237840`, reporte `product_charges = 2782.82` / qty 2, real `1391.41` / qty 1.
2. **Doble conteo de descuentos** — `promo_rebates` es el roll-up y `shipping_discount` el detalle del mismo descuento. Sumar ambos rompe el 16% exacto de IVA.
3. **Duplicidad por `transaction_status`** — la PK lo incluye, así que la misma venta coexiste como `DEFERRED` y luego `RELEASED`/`DEFERRED_RELEASED`.

Corregido en `AMZ_facturacion_corregida.sql`.

## Fórmula de facturación — validada al centavo

```
cfdi_base  = product_charges + shipping + shipping_discount + our_price_discount
cfdi_iva   = our_price_tax + shipping_tax + shipping_tax_discount + our_price_tax_disc
cfdi_total = cfdi_base + cfdi_iva

neto = cfdi_total + amazon_fees + marketplace_vat_principal
                  + marketplace_vat_shipping
     = item_amount por pieza  ·  = total_amount   (cabecera de amz_transactions)
```

**`amazon_fees` ya incluye `amazon_for_all_fee` y `shipping_hb`.** Sumarlos aparte los cuenta dos veces (24% de las piezas los traen). Con solo `amazon_fees` el 99.7% de las piezas cuadra contra `item_amount`. `marketplace_vat_*` es el IVA que retiene Amazon (50% del IVA del CFDI).

Verificación (`702-6941044-9293042`, envío gratis promocional):
`1555.21 + 50.86 − 50.86 = 1555.21` base · `248.83 + 8.14 − 8.14 = 248.83` IVA (16% exacto) · `1804.04 − 444.45 − 124.42 = 1235.17 = total_amount` ✓

**Nunca** sumar `promo_rebates` junto con `shipping_discount`. **Nunca** sumar `total_amount` entre filas (es de cabecera, se repite por ítem).

## La regla de deduplicación

**La clave única de negocio es `order_adjustment_item_id`**, quedándose con el `posted_date` más reciente.

Caso `702-2265086-8559438` (2 piezas), ya filtrando `DEFERRED`, devuelve 3 filas:

```
165346845825161  settlement 27044880571  DEFERRED_RELEASED  posted 07-24  depósito 07-30
164910950935841  settlement 27044880571  DEFERRED_RELEASED  posted 07-27  depósito 07-30
165346845825161  settlement 27172333181  RELEASED           posted 07-30  depósito 08-06
^^^ misma pieza, re-publicada en otro settlement
```

Solo hay 2 piezas. Lo confirma `amz_b2b_invoices`: Amazon emitió dos CFDI de $1,379.00. Sin deduplicar, esa pieza se cobra dos veces.

> ⚠️ **Sin validar contra banco.** "Gana el `posted_date` más reciente" es nuestra inferencia, Amazon no lo documenta. Se pidieron a Finanzas los estados de cuenta del **30-jul** (`27044880571`) y **06-ago** (`27172333181`). **Al 10-sep-2026 sin respuesta.**

## Cobros parciales — corrige un supuesto de Finanzas

El Paso 6 del flujo dice *"el cobro se aplica por el total de la factura, saldando la CxC por completo"*, asumiendo que cada factura pertenece a un solo settlement.

**Falso.** La unidad que Amazon libera es la **pieza**, no la orden. En el ejemplo, las 2 piezas caen en settlements distintos: $1,379.00 el 30-jul y $1,379.00 el 06-ago sobre una factura de $2,758.00.

Consecuencias que Finanzas debe aceptar: una factura puede estar **parcialmente pagada** durante semanas (no es error); un corte de mes puede mostrar saldos parciales en `105.01.006` (no es descuadre); la conciliación va contra la **transitoria**, no factura por factura.

## Bloqueo abierto: sin XML en B2B

Amazon **no expone el XML** del CFDI de Amazon Business. `amz_b2b_invoices` solo trae metadata. Podemos sellar `l10n_mx_edi_cfdi_uuid` y `narration`, pero **no** crear `ir.attachment` + `account.edi.document` como hace `02_loader_ml_billing.py`. Es el mismo muro que tiene ML en notas de crédito.

## Requerimientos de Finanzas para Amazon

1. **Excluir B2B** de nuestra emisión → en su lugar, **jalar el CFDI de Amazon a Odoo**
2. **Eliminar la restricción de OUT** → facturar sobre cantidad pedida; la contrapartida cae en Mercancía Facturada No Enviada (si operaciones deja órdenes sin surtir, esa cuenta se infla — **es la señal que Finanzas quiere**)
3. Factura individual por operación (ya se hacía)
4. Verificar que la SO no esté ya facturada (Odoo **y** autofacturador de Amazon)
5. Periodo de gracia: **5 días** para que el cliente autofacture con su RFC

---

# 7. Amazon — scripts

Todos en `test/financial/billing_processes/amazon/`.

**Producción (29-oct-2026):** `_amz_common`, `00_amz_precheck_prod` (solo lectura), `01`, `02`, `03` y `11` están copiados **idénticos** en `processes/wonderbrands/financial/billing_processes/amazon/`, con `ACTIVAR_AMAZON_PRODUCCION.md`. Flujos: `flows/wonderbrands/financial/amazon/amz_billing_payments_flow.yml` (00→01→02→03, inputs `settlement_id`, `max_facturas`, `payments_batch`, `cobros`) y `amz_cierre_205_flow.yml` (11 + correo). Cron de los dos **apagado** hasta el barrido. Todo cambio a un script va en las dos copias.

| Script | Función | Estado |
|---|---|---|
| `_amz_common.py` | Config, interruptor `ENTORNO`, `OdooModelProxy`, cuentas | ✅ |
| `01_amz_billing.py` | **Miner + loader de facturación** (B2B y B2C) | 🔨 pruebas |
| `02_miner_amz_payments.py` | **Miner** de cobros: settlements → `mkp_payments_*` | 🔨 pruebas |
| `03_loader_amz_payments.py` | **Loader** de cobros: aplica pagos en Odoo | 🔨 pruebas |
| `000_historical_billing_back_analysis.py` | Detecta doble CFDI (Amazon + nuestro). Solo lectura | ✅ |
| `05_amz_orders_control.py` | Control cruzado Odoo ↔ Amazon ↔ facturas. Solo alerta | ✅ |
| `06_reset_lote_test.py` | Libera órdenes en el espejo (solo staging). `RESET_ORDENES` = lista explícita | 🔨 pruebas |
| `09_reporte_lote_finanzas.py` | Excel del lote para Finanzas (modo settlement con `AMZ_SETTLEMENT_ID`) | ✅ |
| `10_conciliar_205.py` | Rehace la conciliación 205.06.003 de facturas ya creadas | ✅ |
| `11_ajuste_cogs_205.py` | Ajuste mensual al COGS por diferencia de costeo factura vs salida | ✅ |
| `AMZ_facturacion_corregida.sql` | Reporte BI 3.0 con los 3 fixes | ✅ |
| `04_amz_billing_test_batch.py` | **DEPRECADO** — absorbido por `01` | ⛔ |
| `DDL_amz_tables.sql` | **DEPRECADO** — se usan las tablas `mkp_*` | ⛔ |
| `PATCH_1a1_excluir_amazon.md` | Sacar Amazon del 1a1 general | 📋 sin aplicar |
| `REUNION_FINANZAS_amazon.md` | Guion de presentación | ✅ |
| `ESTADO_Y_RUTA_NOTAS_CREDITO.md` | Estado + ruta de NC | ✅ |

**Ojo con la numeración:** `02` es el **miner** y `03` el **loader**, igual que en ML (`03_miner_ml_payments` → `04_loader_ml_payments`).

## Estados en `mkp_billing_*` para Amazon

```
PENDING_B2B_LINK       Amazon ya facturó; falta vincular en Odoo
PENDING_WB_INVOICE     nos toca emitir el CFDI
PROCESSING             tomada por una corrida (candado)
ODOO_B2B_LINKED        factura creada con el UUID de Amazon sellado
ODOO_INVOICED          CFDI emitido por WB
ALREADY_ODOO_INVOICED  ya tenía factura viva en Odoo (incluye factura global)
B2B_FACTURADA_PG       B2B con factura viva a público general: anularla antes de vincular (se reevalúa)
SKIPPED_GRACE          dentro del periodo de gracia
SO_CANCELLED           ya no se produce: la SO cancelada SE FACTURA (NC por devoluciones)
SO_DRAFT               SO en borrador que Odoo no dejó confirmar (la que sí, se confirma y factura)
NO_SO_IN_ODOO          sin SO cruzada → solo alerta
ENVIO_SIN_CONFIRMAR    OrderTotal > SO y ninguna segunda fuente confirma envío (se reevalúa)
ERROR                  fallo recuperable
```

`status` es `varchar(40)` en staging y prod. Staging era `varchar(20)` y truncaba `ALREADY_ODOO_INVOICED` (corregido el 28-oct-2026).

## Reglas del correo de Finanzas del 28-oct-2026 (flujo v2)

- **Toda venta que Amazon cobró se factura.** SO en borrador → `action_confirm` y factura. SO cancelada → se factura sin reabrirla (la NC sale por devoluciones). Las excepciones viejas (`SO_DRAFT`, `SO_CANCELLED`, `B2B_NO_RECEPTOR`, `B2B_FACTURADA_PG`) se reevalúan en cada corrida.
- **Factura global:** `facturas_existentes()` también lee `sale.order.invoice_ids`; una SO dentro de una global (invoice_origin con comas) no se refactura.
- **Cliente B2B nuevo:** se crea con el RFC del CFDI y teléfono/correo genéricos de Yuju (`11111` / `market@somos-reyes.com`).
- **205.06.003 (Mercancías facturadas - no enviadas):** `conciliar_salida()` en 01, después de publicar: rompe la conciliación de la salida con facturas canceladas o revertidas, concilia la factura vieja con su NC si la hubo, y concilia la vigente con la salida (por producto; kits por importe). Con OUT posterior, Odoo concilia solo al validar la salida. Si el costo promedio cambió entre factura y salida queda un residuo en la 205.06.003 (probado: +10% → 106.14).
- **Primera corrida en producción = un settlement completo:** `AMZ_SETTLEMENT_ID` en 01, 02 y 09 (`AMZ_ORDER_IDS` agrega órdenes sueltas, solo pruebas).
- **XML-RPC:** `reconcile` y `remove_move_reconcile` no devuelven nada; hacen el trabajo y fallan al responder ("cannot marshal None"). Usar `ejecutar_sin_retorno()` y confirmar leyendo.

## Reglas del correo de Finanzas del 28-oct-2026, segundo

- **Kits en SO cancelada:** la línea del kit se crea **sin** `sale_line_ids` para que Odoo tome el costo del kit (ligada, el costo sale 0 porque los movimientos están cancelados), y **después de publicar** se vuelve a ligar (`religar_kits`): el asiento no cambia y la SO ve su factura. En SO no cancelada se deja ligada (costo exacto de los componentes). La 205.06.003 de una SO cancelada se concilia con la NC, no con un OUT.
- **Kit con lista de materiales en 0** (TOLIXKIT#4-CLA1ROJ / CLA1BLA piden 0 sillas → costo del kit 0): la factura sale sin costo; 01 lo avisa (`kit_sin_costo`) y el cierre mensual asienta el costo de lo que salió. Corregir la lista es de Catálogo.
- **Envío:** Yuju no pone C-ENVIO en la SO. Fuente 1: campos de envío del settlement. Si no vienen, `OrderTotal − SO` es envío solo si lo confirma el **depósito** (`item_amount − venta − comisiones − IVA retenido = envío × (1 − 0.08/1.16)`) o la **API** (`getOrderItems` ShippingPrice + ShippingTax). Sin confirmación → `ENVIO_SIN_CONFIRMAR`. La cuenta 401.01.002 la pone el producto C-ENVIO (no se fuerza en el script).
- **Factura ya emitida sin envío** (1a1): factura aparte `AUTOINV:<SO>:ENVIO` con solo C-ENVIO. 01 no la cuenta como duplicado; 03 reparte el cobro entre las dos y la regla de promoción usa la suma.
- **Envío fuera de los campos del settlement:** 02 lo suma al cobro (3 órdenes sep-oct, todas FBA de $59).
- **B2C:** el centavo se ajusta para que la factura sea igual a OrderTotal (mismo mecanismo que B2B).
- **Cierre mensual de la 205.06.003** (`11_ajuste_cogs_205.py`): SO de Amazon con cada línea entregada y facturada completa (`qty_delivered`/`qty_invoiced` por línea; `delivery_status` no basta con devoluciones), factura propia y salida en la 205.06.003. Residuo → 501.01.001 con conciliación completa. Dos etiquetas: "…diferencia de costeo de producto entre facturación y salida" y "…diferencia entre kit facturado y componentes despachados" (incluye factura de kit sin costo). Reporte `reporte_kits_<mes>.csv`: diferencia de costo / de cantidad / factura sin costo. **No ajusta** (solo cuenta): devoluciones pendientes de NC, facturas globales, y SO cuya salida no está en la 205.06.003 (hueco histórico del 1a1: ~855 SO).
- **Redondeo:** 704.23.001 confirmada para producción. **B2B ya facturada a público general:** cancelación SAT motivo 02.

## `payment_id` compuesto en cobros

`uk_payment (marketplace, payment_id)` **no incluye** `mkp_order_id`. En ML basta porque `payment_id` es el pago de Mercado Pago (uno por orden). En Amazon el identificador del depósito es el `settlement_id` y **un settlement agrupa cientos de órdenes**: guardar solo el settlement haría que únicamente la primera orden entrara.

```python
payment_id = f"{settlement_id}:{order_id}"   # '27172333181:702-2265086-8559438'
```

Separador `:` porque el `order_id` ya contiene guiones. Se recupera con `split(':', 1)[0]`.

---

# 8. Reglas de negocio transversales

- **Yuju crea las Sale Orders.** Ningún script nuestro hace `create` de `sale.order`. Si una orden de Amazon no existe en Odoo → **solo alertar**.
- **Zona horaria:** `DATE_SUB(posted_date, INTERVAL 6 HOUR)` = UTC → CDMX. México no aplica horario de verano desde 2022, así que −6 es fijo todo el año.
- **`AMZ_START_DATE`:** contención contra facturación masiva retroactiva. Al quitar la restricción de OUT, todo el rezago se vuelve elegible de golpe. Sin definir con Finanzas.
- **Fronteras de día exclusivas** (`<`, no `<=`): con `<=` una orden en el límite se procesa en dos lotes.
- **Timbrado apagado en pruebas.** Publicar la factura ya genera el asiento, que es lo que Finanzas revisa. No se timbra contra el PAC desde el espejo.

---

# 9. Notas de crédito — ruta

Tres tipos según Finanzas:

| Tipo | Situación | COGS |
|---|---|---|
| **A** | Cancelación antes de despacho | Revierte Mercancía Facturada No Enviada |
| **B** | Devolución post-despacho, el producto retorna | Devoluciones en Tránsito `107.06.001` |
| **C** | Bonificación sin retorno | Sin movimiento |

Ingresos igual en los tres: `402-001-001` y `208-001-001` contra `105.01.006`.

**Clasificación propuesta** (pendiente de confirmar con Finanzas):

```
description = 'DisbursementOnlyRefund'      → Tipo C
sin stock.picking de salida en 'done'       → Tipo A
con salida en 'done'                        → Tipo B
```

⚠️ **Riesgo FBA:** 89 de 100 filas de la muestra son AFN (FBA). Si Odoo no registra la salida igual que en MFN, la regla A/B necesita otro discriminador.

**Fases:** 1) miner de devoluciones + granularidad por pieza · 2) perfilado de NC B2B en `amz_b2b_invoices` (`receipt_type`, `related_cfdi_relation_type`) · 3) clasificador A/B/C · 4) loader de NC (`move_type='out_refund'`, `reversed_entry_id`, `l10n_mx_edi_cfdi_origin` relación `01`, idempotencia `AUTONC:<order_id>:<item_id>`) · 5) reverso de cobro.

Las fases 1 y 2 **no dependen de ninguna decisión de Finanzas**.

**Sobre el reverso de cobro (fase 5):** el documento pide romper la aplicación del cobro. Con cobros parciales de varios settlements eso implica deshacer y rehacer varios pagos — frágil y difícil de auditar. Propuesta alternativa: conciliar la NC directamente contra `102.02.008`, que refleja el movimiento real del dinero.

**Casos no contemplados en el `.md` de Finanzas:**

1. `LabmanLabelRefund` — 4 de 100 filas. Reembolso de guía, no de producto. ¿NC o ajuste de gasto?
2. Comisiones por devolución (`refund_commission*`) — sin tratamiento definido
3. Devolución de mes cerrado
4. Devolución sin venta previa

---

# 10. Pendientes bloqueantes

**Finanzas:**

- 🔴 Estados de cuenta del 30-jul (`27044880571`) y 06-ago (`27172333181`) — sin esto la deduplicación queda sobre una suposición
- 🔴 `AMZ_START_DATE`
- 🟡 Aceptar cobros parciales y corregir el Paso 6 de su documento
- 🟡 Tratamiento de `LabmanLabelRefund` y de las comisiones por devolución
- 🟡 Reglas de clasificación A/B/C
- 🟡 Criterio de corrección para el doble CFDI histórico (`corregir_caso()` está en `pass`)

**Técnicos:**

- 🔴 Aplicar `PATCH_1a1_excluir_amazon.md` y desplegar en el mismo release que activar `01_amz_billing.py` en Kestra (entre ambos pasos nadie factura Amazon)
- 🟡 Campos personalizados en `account.move` (requiere admin de Odoo)
- 🟡 Índices: `amz_transaction_items(order_adjustment_item_id)` y `amz_transactions(description, transaction_status, posted_date)` — sin el primero, el `GROUP BY` de deduplicación es table scan
- 🟡 Sin resolver: XML del CFDI de Amazon Business

---

# 11. Verificación de código

El sandbox Linux (`mcp__workspace__bash`) puede estar caído. Cuando lo esté, dilo y ofrece el comando para que yo lo corra en PowerShell:

```powershell
cd "...\test\financial\billing_processes\amazon"
python -m py_compile _amz_common.py 01_amz_billing.py 02_miner_amz_payments.py 03_loader_amz_payments.py
```

**No afirmes que un script está verificado si no lo pudiste ejecutar.** Di qué revisaste y qué no.
