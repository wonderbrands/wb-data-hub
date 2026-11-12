# Amazon: facturación y cobros en Odoo 18

Contexto mínimo para operar el flujo y para construir el siguiente (notas de crédito).

**Estado (3-nov-2026):** en producción en Kestra. Facturación desde el 1-oct-2026 (Amazon salió del 1a1 ese día); octubre cerrado. Lo anterior a octubre no lo factura este flujo: ver *Fuera de alcance*.

## Scripts (`processes/.../amazon/`)

| Script | Qué hace |
|---|---|
| `_amz_common.py` | Configuración. `AMZ_ENTORNO=prod` → tablas `_prod`, Odoo de producción, timbrado. Cuentas, `OdooModelProxy` (solo reintenta lecturas). |
| `00_amz_precheck_prod.py` | Solo lectura: cuentas, diario, impuesto, C-ENVIO, tablas, token. Si falta algo, corta la corrida. |
| `01_amz_billing.py` | Facturación. **Miner** (Amazon → `mkp_billing_prod`) + **loader** (crea la factura en Odoo). |
| `02_miner_amz_payments.py` | Cobros liberados por settlement → `mkp_payments_prod`. |
| `03_loader_amz_payments.py` | Aplica los cobros a las facturas en Odoo. |
| `11_ajuste_cogs_205.py` | Cierre mensual de la 205.06.003 + `reporte_kits_<mes>.csv`. |

Las copias de trabajo y los scripts de un solo uso (saneamiento 12, centavos 13, reportes) viven en `test/financial/billing_processes/amazon/`. Todo cambio va en las dos copias.

## Flujos de Kestra (`flows/wonderbrands/financial/amazon/`)

| Flujo | Corre | Horario (CDMX) |
|---|---|---|
| `amz_billing_payments_flow` | 00 → 01 → 02 → 03 | 02:30 y 15:00 diario |
| `amz_cierre_mercFacNoEnv_flow` | 11 + correo de kits | día 2, 09:00 |

`concurrency: FAIL` y sin retry: nunca dos corridas a la vez (incidente del 26-jul-2026, 549 dobles CFDI). Los horarios esquivan ML (00/06/12/18 h), el 1a1 (04:30 y 19:30) y el housekeeping (03:15). Una corrida manual larga que se traslape hace fallar la del cron.

El flujo clona la rama de `vars.branch` (`main`): lo que no esté en `main` no corre en producción.

## Fuentes (MySQL `somos_reyes`)

| Tabla | Uso |
|---|---|
| `amz_transactions` / `amz_transaction_items` | Venta e importes por pieza. `description='Order Payment'`. Viene del ETL de Amazon, que no está en este repo. |
| `amz_b2b_invoices` | CFDI que emitió Amazon Business (`receipt_type` I = ingreso, E = egreso). |
| `amazon_orders` | `IsBusinessOrder`, `OrderTotal`, `PurchaseDate`. **N filas por orden**: siempre agrupar. |
| `amz_settlement_report` | `deposit_date` por settlement. |
| `odoo_master_txns_c` | Puente `order_id` de Amazon → SO de Odoo. Las SO las crea Yuju; nunca nosotros. |

**Deduplicación:** una pieza (`shipment_id`) aparece como `DEFERRED` y luego `RELEASED`/`DEFERRED_RELEASED`, a veces con la misma `posted_date`. Manda la fila liberada más reciente; la `DEFERRED` solo cuenta si la pieza no tiene liberada. `amazon_fees` ya incluye `amazon_for_all_fee` y `shipping_hb`.

## Facturación (01)

**Miner.** Lee desde el día 1 del mes anterior (`start_date` vacío) y siembra una fila por orden:
- **B2B (Amazon Business):** una orden es Business si tiene `IsBusinessOrder` o si Amazon ya emitió su CFDI. **Las Business las factura solo Amazon.**
  - Con CFDI: se replica en Odoo con su UUID, receptor, uso, forma de pago y fecha. **No se timbra**, porque Amazon no da el XML.
  - Sin CFDI: `B2B_SIN_CFDI`. No se factura; en cuanto Amazon emita el CFDI, se vincula.
- **B2C:** lo timbramos nosotros a público general (partner 13436). Antes espera **6 días de gracia desde la compra** (`PurchaseDate`) por si el cliente autofactura, y la orden tiene que estar cobrada.

**Cierre de mes (días ≤ `dias_cierre_mes`, default 1).** La venta de un mes anterior (por `date_order` de la SO en CDMX) sale con **fecha del último día de ese mes, a las 23:59:59**:
- sin periodo de gracia;
- incluye las órdenes que Amazon cobró y aún no liberó (`DEFERRED`).

Una corrida que arranca en cierre termina en cierre, aunque pase la medianoche (`HOY_CORRIDA`). Si ya no alcanza a timbrar dentro de las 72 h del SAT, no crea la factura (`FUERA_72H`) y la deja pendiente; la siguiente corrida la emite con fecha del día y avisa por Slack.

**Loader.** Al arrancar regresa todas las `B2B_FACTURADA_PG` (de cualquier mes) a `PENDING_B2B_LINK`: si Finanzas ya canceló la factura a público general, se vincula el CFDI de Amazon; si sigue viva, vuelve a `B2B_FACTURADA_PG`. Luego, por orden, con candado `PROCESSING`:
1. Toda venta que Amazon cobró se factura: la SO en borrador se confirma y la SO cancelada se factura igual (su NC sale por devoluciones).
2. Revalida en Odoo justo antes de crear (fail-closed). Una SO con factura viva, incluida una global, no se refactura.
3. Valida contra Amazon (±$1.00) antes de crear. Si no cuadra → `TOTAL_DIFF`.
4. **Envío:** Yuju no pone C-ENVIO en la SO. Se agrega a la factura solo si lo confirma el settlement, el depósito o la API. Si la factura ya existía sin envío, se emite otra aparte: `AUTOINV:<SO>:ENVIO`.
5. **Promoción pagada por Amazon:** la B2C se factura por lo que pagó el comprador; el excedente lo asienta 03.
6. **Kits:** en SO cancelada, la línea se crea sin liga a la SO (para que tome costo) y se religa después de publicar.
7. **Centavo:** en B2B se ajusta la línea de IVA al CFDI de Amazon. **En B2C nunca**: el PAC lo rechaza (CFDI40119). Ese centavo lo salda 03.
8. Publica, timbra (B2C) y concilia la 205.06.003 contra la salida de almacén (`conciliar_salida`).

**Identificación en Odoo.**
- `ref` de la factura:
  - `AUTOINV:<SO>`: prefijo compartido con el 1a1; así ninguno duplica al otro.
  - `AUTOINV:<SO>:<uuid>`: una factura por cada CFDI de Amazon cuando la orden trae varios.
  - `AUTOINV:<SO>:ENVIO`: la factura aparte de envío.
- El chatter lleva `order_id`, `settlement_id`, el tipo y los importes, porque no existen campos personalizados en `account.move`.

## Cobros (02 → 03)

- `payment_id = <settlement_id>:<order_id>`. Un cobro es una orden en un settlement. Amazon libera **por pieza**, así que una factura puede quedar pagada parcialmente hasta el siguiente settlement.
- 03 registra el pago por lo liberado en el diario AMZ (102.02.008 LIBERACIONES AMAZON PENDIENTES DE PROCESAR).
  - **Hasta ±$0.15 por factura** contra lo liberado: se salda en el mismo pago contra 704.23.001.
  - **Excedente:** si es promoción confirmada, va a un asiento `AUTOPROMO:<settlement>:<orden>` (403.01.002 + 208.01.001). Nunca se aplica a la factura más que su saldo.
- 03 busca la SO en `mkp_billing_prod`. Un cobro de una orden que 01 nunca leyó queda en `NO_INVOICE`.
- **Business sin el CFDI de Amazon vinculado** (`PENDING_B2B_LINK`, `B2B_FACTURADA_PG`, `B2B_SIN_CFDI`, o cualquier orden con `IsBusinessOrder` que no esté en `ODOO_B2B_LINKED`): el cobro espera en `NO_INVOICE`. Así nunca se aplica a una factura nuestra que Finanzas va a cancelar. El resto de los cobros sigue aplicándose.
- **Solo contra 105.01.006.** El asistente de pago usa la CxC de la factura; una factura del autofacturador trae 105.01.001. Si alguna factura abierta de la SO no está en 105.01.006, el cobro no se aplica: queda en `CXC_INCORRECTA` y avisa por Slack.

## Estados

**`finance.mkp_billing_prod`** (`marketplace='AMAZON'`, una fila por orden)

| Estado | Significado | ¿Se reevalúa? |
|---|---|---|
| `PENDING_B2B_LINK` / `PENDING_WB_INVOICE` | Por facturar (B2B / B2C) | lo toma el loader |
| `PROCESSING` | Tomada por una corrida; se libera sola a las 6 h | — |
| `SKIPPED_GRACE` | B2C dentro de los 6 días de gracia (desde la compra) | sí |
| `B2B_SIN_CFDI` | Orden Business sin CFDI de Amazon. No se factura | sí: se vincula cuando Amazon emita el CFDI |
| `ODOO_B2B_LINKED` | Factura B2B con el UUID de Amazon | final |
| `ODOO_INVOICED` | B2C emitida y timbrada por WB. `cfdi_uuid` = nuestro UUID; si el PAC falló, `error_log` empieza con `TIMBRADO_FALLIDO` | final |
| `ALREADY_ODOO_INVOICED` | Ya tenía factura (1a1, global o ambas) | final |
| `B2B_FACTURADA_PG` | Amazon emitió el CFDI B2B pero la orden tiene una factura viva a público general. Finanzas la cancela (SAT motivo 02) y se vincula sola | sí, cada corrida, de cualquier mes |
| `B2B_NO_RECEPTOR` / `SO_DRAFT` / `ENVIO_SIN_CONFIRMAR` | Excepción temporal | sí |
| `B2B_SPLIT_MISMATCH` | Varios CFDI de Amazon que no se pudieron repartir | a mano |
| `TOTAL_DIFF` | Odoo ≠ Amazon por más de $1; no se facturó | a mano |
| `NO_SO_IN_ODOO` | Sin SO en Odoo (le toca a Yuju). Solo alerta | no |
| `ERROR` | Fallo de Odoo (`FECHA_INCORRECTA`, create rechazado…). El miner actualiza sus datos pero no la reabre | a mano: regresarla a `PENDING_*` |
| `SANEO_CANCELACION` | Solo el saneamiento de oct-2026: cancelación pedida al SAT | script 12 |

En `raw_json`:
- `importes`: base, IVA, total, fees, depósito.
- `amazon`: posted/purchase date, red, SKUs, OrderTotal.
- `cfdi_amazon_business`.
- `facturas_odoo`: cada factura con UUID, fecha y timbre.

**`finance.mkp_payments_prod`** (una fila por orden y settlement)

| Estado | Significado | ¿Se reevalúa? |
|---|---|---|
| `PENDING` | Minado, por aplicar | lo toma 03 |
| `ODOO_PAID` | Aplicado (puede ser parcial) | final |
| `ALREADY_PAID` | La factura ya estaba pagada | final |
| `NO_INVOICE` | Sin factura todavía, o Business esperando su CFDI | cada 6 h durante 60 días |
| `CXC_INCORRECTA` | La factura no está en 105.01.006; no se aplicó | a mano: Contabilidad corrige la cuenta y TI la regresa a `PENDING` |
| `ERROR` | Fallo de Odoo | a mano |
| `HISTORICO_CONTABILIDAD` | Depósito del 23-sep-2026 (settlement 27581522881, 1,586 cobros de septiembre) que cierra Contabilidad | 03 no lo toca |

## Qué atender a mano

Lo avisa Slack o el reporte de la corrida:

| Caso | Quién | Acción |
|---|---|---|
| `B2B_FACTURADA_PG` | Finanzas | Cancelar la factura a público general (motivo 02). La vinculación y el cobro salen solos. |
| `CXC_INCORRECTA` | Contabilidad → TI | Contabilidad corrige la CxC de la factura a 105.01.006; TI regresa el cobro a `PENDING`. |
| `TOTAL_DIFF`, `B2B_SPLIT_MISMATCH`, `ERROR` | TI + Finanzas | Revisar la orden; regresarla a `PENDING_*` cuando esté corregida. |
| `NO_SO_IN_ODOO` | Yuju | Crear la SO; el miner la toma en la siguiente corrida mientras esté en la ventana. |
| `TIMBRADO_FALLIDO` en `error_log` | TI | La factura quedó publicada sin timbre; timbrar desde Odoo. |
| Cobro aplicado a una factura que después se canceló | Contabilidad → TI | Contabilidad cancela el pago; TI regresa el cobro a `PENDING` (03 no lo detecta solo; caso PAMZ/2026/00710). |

## Cuentas

| Cuenta | Uso |
|---|---|
| 105.01.006 | CxC Amazon (todas las facturas) |
| 102.02.008 | Transitoria de cobros (diario AMZ) |
| 205.06.003 / 501.01.001 | Mercancía facturada no enviada / costo de venta (anglosajona) |
| 401.01.001 / 401.01.002 | Ventas / envío (la pone el producto C-ENVIO) |
| 403.01.002 + 208.01.001 | Promoción pagada por Amazon |
| 704.23.001 | Redondeo de centavos |

Impuesto 38 (IVA 16 %). Partner público general 13436.

## Para notas de crédito (siguiente flujo)

- **Devoluciones en Amazon:**
  - `description='Refund'`, con el mismo ciclo `DEFERRED` → liberada que las ventas, así que necesita la misma deduplicación.
  - `DisbursementOnlyRefund`: bonificación sin retorno (tipo C).
  - `LabmanLabelRefund`: reembolso de guía, no de producto. Su tratamiento está pendiente con Finanzas.
- **B2B:** Amazon emite su propio CFDI de egreso (`amz_b2b_invoices.receipt_type='E'`, relación 03). Se replica igual que el de ingreso; no se timbra.
- **B2C:** la NC se timbra con `reversed_entry_id` y `l10n_mx_edi_cfdi_origin` relación 01 contra el UUID de la factura. La factura se encuentra por `ref AUTOINV:<SO>`, o por `cfdi_uuid` / `raw_json.facturas_odoo` en la tabla. Si la orden está en `ALREADY_ODOO_INVOICED`, la factura es del 1a1, o es una global con muchas SO.
- **Tipos de Finanzas:**

  | Tipo | Cuándo | Costo |
  |---|---|---|
  | A | Cancelada antes del envío | Revierte la 205.06.003, que se concilia con la NC |
  | B | Devuelta | 107.06.001 |
  | C | Bonificación | Sin movimiento |

  En los tres: 402 + 208 contra 105.01.006. Las SO canceladas que 01 facturó esperan su NC tipo A.
- **Cobro:** el reembolso llega restado en el settlement. Hoy 02 solo mina `Order Payment`, así que la 102.02.008 no cuadra contra el depósito mientras no se apliquen las devoluciones.
- **Blindajes:** concurrencia FAIL, revalidación antes de crear, idempotencia por `ref` (propuesta: `AUTONC:<order_id>:<item>`) y escrituras sin reintento a ciegas.

## Huecos conocidos

- Una SO "a facturar" sin transacción de Amazon (pedida y no enviada al cierre) no se factura hasta que Amazon cobre. El 1a1 sí la facturaba.
- Un cobro de una orden sin fila en facturación (sin SO en Odoo, o venta fuera de la ventana de 01) queda en `NO_INVOICE` y deja de reintentarse a los 60 días.

## Fuera de alcance de este flujo

- **Ventas anteriores a octubre de 2026 sin facturar.** Finanzas pidió reabrir periodos y asentar cada factura en su mes. 01 no lo hace: factura con fecha del día (o del cierre) y el SAT no permite un CFDI B2C con más de 72 h de antigüedad. Lo anterior a la migración a Odoo 18 tiene la SO en Odoo 15, donde este flujo no opera. Se define con Finanzas sobre la base de `test/.../amazon/14_base_historico_amazon.py`.
- **Doble CFDI histórico de Amazon Business** (240 órdenes abr–nov, `000_historical_billing_back_analysis.py`): la cancelación es de Finanzas; al cancelar, 01 vincula el CFDI de Amazon solo.
- **Anular automáticamente las `B2B_FACTURADA_PG`**, **IVA no cobrado 209.01.001 → 208.01.001** y **notas de crédito**: fuera de este flujo.
