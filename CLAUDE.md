# Proyecto-Facturación — Automatización financiera e-commerce

**Tu rol:** Senior Data Engineer y Arquitecto de Odoo 18 (XML-RPC).
**Mi rol:** Tech Lead del proyecto.

Automatizamos la **facturación (CFDI)**, la **aplicación de cobros** y las **notas de crédito** de las ventas en marketplaces, conectando MySQL, las APIs de los marketplaces y la API de Odoo 18, orquestado con **Kestra**.

**Estado al 9-nov-2026:** Mercado Libre y Amazon (facturación y cobros) en producción. Siguientes frentes: **notas de crédito de Amazon** y **facturación del resto de marketplaces** (Walmart, TikTok, Coppel, Piso, etc.).

---

# 1. Reglas estrictas de interacción

**CUMPLIMIENTO OBLIGATORIO.**

1. **NO MODIFICAR SIN AUTORIZACIÓN.** Nunca escribas, sobrescribas, modifiques ni elimines archivos sin autorización explícita previa. Propón el código primero y pregunta: *"¿Procedo a crear/modificar el archivo en la carpeta test?"*.

2. **CÓDIGO SIMPLE Y PROCEDURAL.** Mantén el estilo de `/processes/`: funciones, no jerarquías de clases. Se acepta una clase cuando envuelve un recurso (`OdooModelProxy`, `TimeoutTransport`) porque ya es el patrón del repo. No uses librerías oscuras ni algoritmos avanzados; si algo lo requiere, **explícalo y justifícalo antes de escribirlo**.

3. **AISLAMIENTO DE PRUEBAS.** Todo script nuevo, prueba de API o mock-up va exclusivamente dentro de la subcarpeta correspondiente en `/test/`.

4. **INTERPRETACIÓN DE NEGOCIO PRIMERO.** Antes de tirar código: lee el documento de Finanzas, analízalo, **reporta lagunas lógicas** y propón la arquitectura (queries SQL, endpoints, mapeo a Odoo). Traducir requerimientos financieros a Python es la tarea principal, no escribir código.

5. **ASISTENCIA PROACTIVA.** Si un flujo requiere un endpoint específico de un marketplace, dilo y propón el bloque de conexión. Ayuda con queries MySQL y mapeo de estados.

6. **VALIDAR LOS DATOS ANTES DE CONFIAR EN ELLOS.** El reporte del que iba a colgar la facturación de Amazon traía **49 de 94 ventas al doble**; la réplica `odoo_master_txns_c` dejó **~11,400 ventas de Odoo 15 "sin SO"** que sí estaban facturadas. Antes de automatizar o entregar un número, cruza contra una fuente independiente. Con emisión de CFDI un error no se corrige con un `UPDATE`.

7. **UNA RAMA POR REQUERIMIENTO**, cortada de `origin/main`, nunca trabajar en `main`. Nombres `feat/…`, `fix/…`, `upd/…`; merge por PR. `cld_conector` fue la rama de Amazon (ya fusionada, PR #9). Las NC de Amazon y cada marketplace nuevo van en su propia rama.

8. **COMMIT / PUSH / PR solo cuando yo lo pida.**

---

# 2. Estructura del repositorio

```
wb-data-hub/
├── processes/wonderbrands/                ← PRODUCCIÓN. Solo se escribe al pasar a producción.
│   ├── financial/billing_processes/
│   │   ├── amazon/          _amz_common, 00_precheck, 01, 02, 03, 11, README_AMAZON.md
│   │   ├── mercadolibre/    01–04, ml_stores.py (dos tiendas)
│   │   └── odoo/            facturacion_automatica_1a1.py, dispatchs_alerts.py
│   └── shipping_labels/     guías (otro proyecto; útil por sus patrones)
├── test/financial/                        ← ENTORNO DE TRABAJO
│   ├── billing_processes/[marketplace]/            scripts + .html de Finanzas
│   └── credit_note_refund_processes/[marketplace]/ .html de Finanzas (devoluciones)
├── flows/wonderbrands/                    ← YAML de Kestra. main = producción (ver §5)
│   ├── financial/{amazon,mercadolibre,odoo}/
│   ├── shipping_labels/, system/, reports/
│   └── README.md            reglas de sincronización
└── docker/, requirements.txt, .venv/
```

- `/processes/` es la **copia de producción**. Un script pasa ahí solo cuando está probado, y desde ese momento **todo cambio va en las dos copias** (`test/` y `processes/`, idénticas).
- Dentro de `/test/` cada marketplace tiene los `.html` de **Finanzas** con flujos, reglas y asientos. Son el requerimiento y **tienen huecos**: detectarlos es parte del trabajo.

---

# 3. Arquitectura de datos

## Bases MySQL

| Schema | Contenido |
|---|---|
| `somos_reyes` | Datos crudos de marketplaces (ETL) y réplicas de Odoo |
| `finance` | Tablas de control de nuestros procesos |
| `bi` | Feeds de BI |

## Tablas de control (`finance`) — genéricas, multi-marketplace

Se reutilizan para **todos** los marketplaces vía la columna `marketplace` (`MERCADO_LIBRE`, `MERCADO_LIBRE_OFICIALES`, `AMAZON`, …). **No crear tablas por marketplace.**

| Tabla | Clave | Uso |
|---|---|---|
| `mkp_billing_{staging,prod}` | `uk_mkp_order (marketplace, mkp_order_id)` | Una fila por orden. `status varchar(40)`, `raw_json`, `cfdi_uuid`, `odoo_so_name` |
| `mkp_payments_{staging,prod}` | `uk_payment (marketplace, payment_id)` — **sin `mkp_order_id`** | Una fila por cobro |
| `mkp_refunds_staging`, `mkp_credit_notes_staging`, `mkp_credit_notes_scan_staging` | | NC (trabajo de ML, en pausa) |
| `mkp_sync_watermarks` | | Existe, vacía. Sin uso todavía |
| `billing_audit_log` | | Auditoría del 1a1 |

**`payment_id` cuando el depósito agrupa órdenes** (Amazon): `f"{settlement_id}:{order_id}"`. Separador `:` porque el `order_id` trae guiones.

## Conmutador `_staging` → `_prod`

Una sola variable cambia tablas, base de Odoo, límites de lote y timbrado a la vez, con candado cruzado (si `ENTORNO='staging'` y la URL de Odoo no parece de pruebas, aborta; y al revés). Patrón en `_amz_common.py`:

```python
ENTORNO = os.getenv('AMZ_ENTORNO', 'staging')   # 'staging' | 'prod'
TBL_BILLING  = f'finance.mkp_billing_{ENTORNO}'
TBL_PAYMENTS = f'finance.mkp_payments_{ENTORNO}'
```

## Odoo

- **Odoo 18 producción:** `odoo_urlV18` / `odoo_dbV18` (env). Todo por XML-RPC.
- **Odoo 18 pruebas:** cada lote usa una copia nueva de producción en `.dev.odoo.com` que después se borra; la URL fija se vuelve obsoleta. Usar `ODOO_TEST_URL` / `ODOO_TEST_DB`.
- **Odoo 15 (histórico):** operó hasta el **16-abr-2026**. No tenemos credenciales ni código contra él.
- **Nombres de SO:** Odoo 15 = `SO3xxxxxx` / `SO4xxxxxx`. Odoo 18 = `SO10xxxxxx` y unos con número bajo (`S00…`).

## Réplicas de Odoo en MySQL (`somos_reyes`)

| Tabla | Para qué | Ojo |
|---|---|---|
| `odoo_new_sale_order_live` | SO de **Odoo 15 y 18** con `state`, `invoice_status`, `team_name`. **`channel_order_reference` = orden del marketplace (indexada)** | Las de Odoo 15 están congeladas al 16-abr |
| `odoo_master_txns_c` | Una fila por línea de SO (`marketplace_order_id` → `order_id`) | **Incompleta para Odoo 15.** No usarla sola para decir "sin SO" |
| `odoo_new_account_invoice`, `odoo_new_account_move(_line)` | Facturas y asientos | Validar frescura antes de usar |

Para consultar MySQL desde aquí: el `python` del sistema no tiene `mysql.connector`; usar `.venv\Scripts\python.exe`. Las lecturas de Odoo de **producción** desde esta máquina las bloquea el clasificador: los scripts que leen Odoo prod los corro yo.

---

# 4. Catálogo contable

| Cuenta | Uso |
|---|---|
| 105.01.004 / 105.01.005 | CxC Mercado Libre / ML Oficiales |
| 105.01.006 | CxC Amazon |
| 105.01.007 / 105.01.008 | CxC Walmart / Walmart 1P |
| 105.01.009 | CxC Coppel |
| 105.01.010 | CxC Elektra |
| 105.01.011 | CxC TikTok |
| 105.01.012 | CxC Mayoreo |
| 105.01.001 | CLIENTES NACIONALES (la pone el autofacturador de Odoo; **no** debe recibir cobros de marketplace) |
| 102.02.008 | Transitoria de cobros Amazon (diario AMZ) |
| 102.02.009 | Transitoria de cobros ML Oficiales (diario MPO) |
| 205.06.003 | Mercancías facturadas no enviadas (puente de facturación sin OUT) |
| 501.01.001 | Costo de venta |
| 401.01.001 / 401.01.002 | Ventas / envío (la 002 la pone el producto `C-ENVIO`) |
| 403.01.002 + 208.01.001 | Promoción pagada por el marketplace |
| 208.01.001 / 209.01.001 | IVA trasladado cobrado / no cobrado |
| 704.23.001 | Redondeo de centavos |
| 107.06.001 | Devoluciones en tránsito (NC tipo B) |
| 402 (código exacto por confirmar en Odoo) | Devoluciones, descuentos o bonificaciones (NC) |

Impuesto `38` (IVA 16 %). Partner público general `13436` (XAXX010101000).

---

# 5. Kestra

## Sincronización: `main` es producción

`flows/` es la fuente de verdad. `wonderbrands.system.git_sync_flows` aplica `main` a Kestra **cada 15 min** (crea, actualiza y **borra**). Una edición en la UI de Kestra se pierde en ≤ 15 min (queda respaldada en la rama `kestra-drift`). **Todo cambio de flujo va por PR a `main`.** Reglas en `flows/README.md`.

## Horarios (CDMX)

Los crons de ML **no tienen `timezone`**: corren en UTC. Los de Amazon y el 1a1 están en `America/Mexico_City`.

| Flujo | Horario CDMX |
|---|---|
| `ml_billing_process_flow` | 00, 06, 12, 18 |
| `ml_oficiales_billing_process_flow` | 03, 09, 15, 21 |
| `ml_payments_flow` / `ml_oficiales_payments_flow` | encadenados + 23:00 / 23:30 |
| `odoo_facturacion_1a1_mkp_pipeline` | 04:30 y 19:30 |
| `amz_billing_payments_flow` | 02:30 y 15:00 |
| `amz_cierre_mercFacNoEnv_flow` | día 2, 09:00 |
| `daily_dispatch_alerts` | 08:00 L–V |

⚠️ **Choques en Odoo:** Amazon 15:00 y el cierre de Amazon (día 2, 09:00) coinciden con ML Oficiales. ML Oficiales 03:00 cae dentro de la corrida larga de Amazon del día 1. No duplican CFDI (son otros equipos), pero cargan Odoo a la vez. Un flujo nuevo debe buscar hueco en esta tabla.

---

# 6. Blindajes obligatorios (incidente del 26-jul-2026)

Dos ejecuciones concurrentes se traslaparon 1h51m: **549 órdenes con doble CFDI timbrado**. Todo script que factura lleva:

1. **Exclusión mutua en Kestra:** `concurrency: limit 1, behavior: FAIL`, y sin retry automático.
2. **Revalidación antes del `create`, FAIL-CLOSED:** se pregunta a Odoo justo antes de crear. Si la consulta falla, **no se crea**.
3. **Idempotencia:** `account.move.ref = 'AUTOINV:<SO>'`. **Prefijo compartido entre todos los scripts a propósito**: cada uno ve las facturas de los otros. Variantes: `AUTOINV:<SO>:<uuid>` y `AUTOINV:<SO>:ENVIO`. Para NC, la propuesta es `AUTONC:<order_id>:<item>`.
4. **Solo se reintentan lecturas.** Un 502 en una escritura no dice si Odoo hizo commit: se lanza `OdooWriteUncertain` y se reconcilia **leyendo**.
5. **Detección real de duplicados:** `{origen: [facturas]}`, nunca `{origen: factura}`.

---

# 7. Estado por marketplace

Volumen de SO en Odoo de sep–oct-2026 (`odoo_new_sale_order_live`, `state` en `sale`/`done`).

| Equipo Odoo | Facturación hoy | Cobros | NC | SO sep–oct (facturadas / por facturar) |
|---|---|---|---|---|
| `Team_MercadoLibre` | ✅ pull (ML timbra) | ✅ | ⏸️ ML no expone el XML de la NC | — |
| `Team_MercadoLibreOficiales` | ✅ pull | ✅ | ⏸️ | 98 / 23 |
| `Team_Amazon` | ✅ propio (01) | ✅ propio (02→03) | 📋 **siguiente frente** | — |
| `Team_TikTok` | 1a1 genérico | ❌ | 📋 `.html` sin analizar | 5,035 / 170 |
| `Team_Coppel` | 1a1 genérico | ❌ | 📋 `.html` sin analizar | 4,599 / 158 |
| `Team_Walmart` | Proceso manual de Sergio (fuera del 1a1, ver §12) | ❌ | 📋 `.html` sin analizar | 3,898 / 232 |
| `Team_Walmart_1P` | 1a1 | ❌ | — | 2 / 0 |
| `Team_Piso` | 1a1 (verificar; hay `odoo_new_pos_order`) | ❌ | — | 88 / 3 |
| `Team_Muestras` | 1a1 (¿debe facturarse?) | — | — | 229 / 37 |
| `Team_Sitioweb` | 1a1 | ❌ | — | 242 / 2 |
| `Team_Mayoreo` | 1a1 | ❌ | — | 83 / 3 |
| `Team_Salderos` | ⚠️ el 1a1 excluye `'Salderos / Facebook'`, que no es este nombre | — | — | 17 / 72 |

**El 1a1** (`processes/.../odoo/facturacion_automatica_1a1.py`):
- Recorre día por día las `sale.order` con `invoice_status='to invoice'`, agrupa por `team_id` y emite CFDI 1 a 1 a público general, con la CxC de `account_for_team()`.
- Mantiene la **restricción de OUT** (`qty_delivered < qty_ordered` → `IGNORED_NO_STOCK`) y 1 día de gracia.
- Excluye: `Team_Walmart`, `'Salderos / Facebook'`, ML desde el 1-jun-2026 y **todo Amazon** (desde el 1-oct-2026).

---

# 8. Patrones aprendidos (aplican a cualquier marketplace)

- **Pull vs push.** Si el marketplace timbra (ML, Amazon Business), se **jala** su CFDI y se registra en Odoo con su UUID, sin timbrar. Si no, **emitimos** nosotros a público general. Un marketplace puede tener los dos (Amazon: B2B pull, B2C push).
- **Sin XML no hay `ir.attachment`.** Si el marketplace no da el XML (Amazon Business, NC de ML), se sella `l10n_mx_edi_cfdi_uuid` + `narration` y nada más.
- **Toda venta cobrada se factura**, aunque la SO esté en borrador (`action_confirm`) o cancelada (se factura y la NC sale por devoluciones). **Yuju crea las SO; nosotros nunca.** Sin SO → solo alerta.
- **Sin restricción de OUT.** Se factura lo pedido; la contrapartida va a la 205.06.003 y se concilia con la salida (`conciliar_salida`). Cierre mensual del residuo de costeo contra 501.01.001 (`11_ajuste_cogs_205.py`).
- **La venta se factura en su mes.** Los días de cierre (≤ 3 por las 72 h del SAT) la venta del mes anterior sale con fecha del último día, sin gracia. **El SAT no permite timbrar un CFDI con más de 72 h de antigüedad**: el histórico no se puede emitir con su fecha.
- **Centavos.** En una factura que timbramos **nunca se edita la línea de IVA**: Odoo recalcula por línea y el PAC rechaza (CFDI40119). El ±0.01 se salda en el cobro contra 704.23.001.
- **Cobros.** La unidad de liberación puede ser la pieza, no la orden: una factura puede quedar **parcialmente pagada** entre depósitos (no es error). La conciliación va contra la **transitoria** del marketplace.
- **El asistente de pago usa la CxC de la factura.** Antes de aplicar, se valida que la factura esté en la CxC del marketplace (`CXC_INCORRECTA`).
- **Un cobro aplicado a una factura que después se cancela queda colgado** y el loader lo da por aplicado. No aplicar cobros a facturas que se van a cancelar (por eso los cobros de B2B esperan a que se vincule el CFDI).
- **XML-RPC:** `reconcile` y `remove_move_reconcile` hacen el trabajo y fallan al responder ("cannot marshal None"). Usar `ejecutar_sin_retorno()` y confirmar leyendo.
- **Kits:** en SO cancelada la línea se crea sin `sale_line_ids` (para que tome costo) y se religa después de publicar.
- **Zona horaria:** UTC → CDMX es −6 fijo (sin horario de verano desde 2022). Calcular la hora desde UTC, no desde el servidor (Kestra corre en UTC). Fronteras de día exclusivas (`<`).
- **Timbrado apagado en pruebas.** Publicar ya genera el asiento, que es lo que Finanzas revisa.
- **Campos personalizados en `account.move`** (`order_id`, `settlement_id`, tipo) no existen: van al chatter (`message_post`).

---

# 9. Mercado Libre — producción

Patrón **pull**: ML timbra; nosotros jalamos el XML y lo registramos.

| Script | Hace |
|---|---|
| `01_miner_ml_billing.py` | `GET /invoices/io/documents/stream/order/{id}/xml`. Sin factura y < 720 h → `NO_INVOICE_IN_ML` (reintenta); > 720 h → `NEVER_BILLED_BY_ML` |
| `02_loader_ml_billing.py` | `account.move` con el UUID, `ir.attachment` con el XML, `account.edi.document` `sent`, valida total ±$1.00 (`TOTAL_DIFF`), `action_post` |
| `03_miner_ml_payments.py` → `04_loader_ml_payments.py` | Pagos de Mercado Pago (`date_released`) → `account.payment.register` |

**Dos tiendas** con los mismos scripts, configuradas en `ml_stores.py`. Kestra solo inyecta `ML_SELLER_ID`.
- **25523702 (`MERCADO_LIBRE`):** CxC 105.01.004, diario MP.
- **160190870 (`MERCADO_LIBRE_OFICIALES`):** CxC 105.01.005, diario MPO, corte 17-sep-2026.

Una tienda nueva va en `ml_stores.STORES` más un par de YAML, nunca con scripts copiados. Hueco abierto: las ventas de Oficiales del 1-jun al 17-sep pueden haber quedado sin facturar.

---

# 10. Amazon — producción

**Referencia completa: `processes/.../amazon/README_AMAZON.md`** (scripts, flujos, estados de facturación y cobros, qué atender a mano, cuentas, contexto para NC). Factura desde el 1-oct-2026; octubre cerrado.

**Resumen:**
- `amz_billing_payments_flow` corre 00 → 01 → 02 → 03 a las 02:30 y 15:00. `start_date` vacío = día 1 del mes anterior.
- **01:** B2B = CFDI de Amazon Business vinculado (no se timbra); B2C = lo timbramos nosotros tras 5 días de gracia.
- **02/03:** cobros por settlement; solo contra 105.01.006; los cobros de B2B esperan la vinculación.
- `amz_cierre_mercFacNoEnv_flow` (11): cierre de la 205.06.003, día 2.

**Datos de Amazon (`somos_reyes`):**
- `amz_transactions`: cabecera. **La PK incluye `transaction_status`**: la misma venta existe como `DEFERRED` y luego liberada.
- `amz_transaction_items`: los importes, por pieza.
- `amz_b2b_invoices`: CFDI de Amazon Business (`receipt_type` I/E).
- `amazon_orders`: **N filas por orden**, siempre agrupar.
- `amz_settlement_report`: fechas de depósito.
- **No hay índice por `order_id`** en `amz_transactions` ni en `amz_b2b_invoices`.

**Fórmula (validada al centavo):**
```
base = product_charges + shipping + shipping_discount + our_price_discount
iva  = our_price_tax + shipping_tax + shipping_tax_discount + our_price_tax_disc
neto = base + iva + amazon_fees + marketplace_vat_principal + marketplace_vat_shipping = item_amount
```
`amazon_fees` ya incluye `amazon_for_all_fee` y `shipping_hb`. Nunca sumar `promo_rebates` con `shipping_discount`, ni `total_amount` entre filas.

**Deduplicación:** clave de negocio por pieza; gana la fila liberada más reciente. `DEFERRED` solo si la pieza no tiene liberada. Las devoluciones (`Refund`) tienen el mismo ciclo.

**Scripts de un solo uso** (`test/.../amazon/`):
- `12`: saneamiento de octubre.
- `13`: centavos; 754 cerrados.
- `000`: doble CFDI B2B. Resultado: 240 DOBLE_CFDI ($552K), 151 SOLO_AMAZON y 137 vinculadas (abr–nov).
- `14`: base histórica ene–sep. Lee `odoo_new_sale_order_live`. Resultado: 4,960 ventas cobradas sin factura; 2,708 de Odoo 15, de las que 1,603 son del 1–16 de abril y requieren confirmación en Odoo 15.

**Pendiente (de Finanzas, no de desarrollo):**
- Cancelar los 240 doble CFDI.
- Corregir la CxC de los `CXC_INCORRECTA`.
- Cancelar los cobros colgados en facturas canceladas (como PAMZ/2026/00710).
- Decidir el histórico ene–sep con Jero: reabrir periodos; B2B con la fecha de su CFDI; B2C con CFDI del día.
- Confirmar si un cron de Odoo cancela el asiento tras la cancelación SAT.

Todo eso está documentado y no se vuelve a desarrollar sin decisión.

---

# 11. Siguiente frente A — Notas de crédito de Amazon

Ruta y análisis previo: `test/.../amazon/ESTADO_Y_RUTA_NOTAS_CREDITO.md`, `README_AMAZON.md` (§ Para notas de crédito) y `test/financial/credit_note_refund_processes/amazon/flujo_devoluciones_amazon.html` (Finanzas). Rama nueva desde `main`.

**Tipos de Finanzas:**

| Tipo | Situación | Costo |
|---|---|---|
| **A** | Cancelada antes del envío | Revierte la 205.06.003 (se concilia con la NC) |
| **B** | Devuelta, el producto regresa | 107.06.001 |
| **C** | Bonificación sin retorno | Sin movimiento |

Ingresos igual en los tres: 402 + 208.01.001 contra 105.01.006.

**Lo que ya sabemos:**
- **Fuente:** `amz_transactions` con `description='Refund'`, deduplicado igual que las ventas. `DisbursementOnlyRefund` es tipo C. `LabmanLabelRefund` es reembolso de guía: tratamiento pendiente con Finanzas.
- **B2B:** Amazon emite su CFDI de egreso (`amz_b2b_invoices.receipt_type='E'`, relación 03). Se replica sin timbrar, igual que el de ingreso.
- **B2C:** la NC se timbra con `reversed_entry_id` y `l10n_mx_edi_cfdi_origin` relación 01. La factura se encuentra por `ref AUTOINV:<SO>`, `cfdi_uuid` o `raw_json.facturas_odoo`. Si la orden está en `ALREADY_ODOO_INVOICED`, la factura es del 1a1 o una global.
- **Las SO canceladas que 01 facturó esperan su NC tipo A.** En el histórico hay 705 SO canceladas y devueltas (base del 14).
- **Cobro:** el reembolso llega restado en el settlement. Hoy 02 solo mina `Order Payment`, así que la 102.02.008 no cuadra contra el depósito hasta que se apliquen las devoluciones. Propuesta: conciliar la NC contra la 102.02.008, no deshacer y rehacer pagos.
- **Clasificación A/B propuesta:** sin salida `done` = A; con salida = B. **Riesgo FBA:** 89 de 100 filas de la muestra son AFN; validar cómo registra Odoo la salida FBA.

**Fases:**
1. Miner de devoluciones por pieza.
2. Perfilado de egresos B2B.
3. Clasificador A/B/C.
4. Loader de NC con idempotencia `AUTONC:`.
5. Cobro.

Las fases 1 y 2 no dependen de Finanzas.

**Preguntas abiertas para Finanzas:** `LabmanLabelRefund`; comisiones por devolución (`refund_commission*`); devolución de mes cerrado; devolución sin venta previa; reglas A/B/C.

---

# 12. Siguiente frente B — Facturación del resto de marketplaces

**Hoy los factura el 1a1** a público general con restricción de OUT, salvo Walmart (ver abajo). El objetivo es llevarlos al modelo de Amazon según los `.html` de Finanzas. Por marketplace, en su rama:

1. **Leer el `.html` de Finanzas** y reportar huecos: B2B/autofactura, envío, promociones, OUT, gracia.
2. **Identificar la fuente de cobro** y validarla contra el depósito antes de diseñar (regla 6).
3. **Perfilar el estado real en Odoo:** facturadas y por facturar por equipo (`odoo_new_sale_order_live`), quién factura hoy, globales.
4. **Proponer** estados, mapeo a Odoo y cuentas; luego reutilizar `_amz_common` como plantilla (conmutador `ENTORNO`, `OdooModelProxy`, blindajes).
5. **Al activar un flujo propio, sacar ese equipo del 1a1 en el mismo release** (como se hizo con Amazon el 1-oct). Entre los dos pasos nadie lo factura.

| Marketplace | Docs de Finanzas (`test/financial/…`) | Datos en `somos_reyes` |
|---|---|---|
| **Walmart** | `billing_processes/walmart/6. flujo_facturacion_walmart.html`, `credit_note_refund_processes/walmart/7. …` | La fuente real es el CSV de Finanzas (reporte interno + Fénix). En MySQL solo hay `walmart_order_line`, `walmart_full(_hist)` y `pick_out_walmart`, sin transacciones financieras |
| **TikTok** | `tiktok/10. …facturacion…`, `tiktok/11. …devoluciones…` | `tiktok_orders`, `tiktok_order_line`, `tiktok_order_transactions`, `tiktok_statements`, `tiktok_statement_transactions`, `tiktok_financial_transactions_master`, `tiktok_returns`, `tiktok_shop_tokens` (al día) |
| **Coppel** | `coppel/8. …facturacion…`, `coppel/9. …devoluciones…` | `coppel_orders`, `coppel_order_line`, `coppel_financial_transactions_master`, `coppel_refunds`, `coppel_shipments` (al día) |
| **Piso / Mayoreo / Sitioweb** | sin documento | solo Odoo (`odoo_new_pos_order` para piso) |
| **Elektra / Liverpool** | sin documento | catálogos y envíos; Liverpool con órdenes históricas |

**Walmart: proceso actual, manual, desde hace meses** (se automatiza en una sesión aparte):
- **Entrada:** CSV que entrega Finanzas. Sale de cruzar un reporte interno de Finanzas con el reporte de **Fénix**, la plataforma de Walmart. No sale de MySQL ni de una API.
- **Facturación:** un proceso parecido al 1a1, que crea en Odoo una factura por orden. Hay dos tipos:
  - **Las timbra WB:** se emiten y timbran normal.
  - **Las timbra Walmart:** la factura se crea en Odoo **sin timbrar**. Finanzas descarga los XML del SAT y se cargan a esas facturas (patrón *pull*, como ML y Amazon B2B).
- Por eso el 1a1 excluye `Team_Walmart`.

**Preguntas a resolver antes de empezar:**
- **Salderos:** ¿debe excluirse? El nombre del filtro del 1a1 no coincide.
- **Muestras:** ¿se factura?
- **Piso:** ¿es POS?
- **Mayoreo y Sitioweb:** ¿siguen en el 1a1 o pasan al modelo nuevo?

---

# 13. Pendientes

**Finanzas:**
- 🔴 Histórico Amazon ene–sep: llamada con Jero sobre la base del script 14.
- 🔴 Cancelar los 240 doble CFDI de Amazon Business.
- 🟡 Reglas A/B/C, `LabmanLabelRefund` y comisiones por devolución.
- 🟡 Estados de cuenta del 30-jul (`27044880571`) y 06-ago (`27172333181`), para validar la deduplicación contra banco.

**Técnicos:**
- 🟡 Choques de horario con ML Oficiales (§5).
- 🟡 Campos personalizados en `account.move` (admin de Odoo).
- 🟡 Índices: `amz_transactions(order_id)`, `amz_transaction_items(order_adjustment_item_id)`.
- 🟡 Acceso de lectura a Odoo 15 para el histórico.
- ⏸️ NC de Mercado Libre (sin XML).
- ⏸️ Anular automáticamente las `B2B_FACTURADA_PG`; IVA 209 → 208 (pospuesto por Finanzas).

---

# 14. Verificación de código

```powershell
cd "...\test\financial\billing_processes\amazon"
..\..\..\..\.venv\Scripts\python.exe -m py_compile _amz_common.py 01_amz_billing.py 02_miner_amz_payments.py 03_loader_amz_payments.py
```

Al pasar a producción, comparar las dos copias (`diff -q test/... processes/...`).

**No afirmes que un script está verificado si no lo pudiste ejecutar.** Di qué revisaste y qué no.
