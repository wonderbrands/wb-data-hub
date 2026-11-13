# Amazon — Estado actual y ruta de Notas de Crédito
### 09-sep-2026

---

# PARTE 1 — Resumen de lo hecho

## Lo que quedó cerrado

**Corrección del reporte BI 3.0** (`AMZ_facturacion_corregida.sql`)

Tres defectos que habrían causado errores fiscales:

| # | Defecto | Impacto medido |
|---|---|---|
| 1 | `amazon_orders` sin deduplicar en el JOIN | **49 de 94 ventas con importes al doble** |
| 2 | `promo_rebates` duplica `shipping_discount` | IVA dejaba de ser 16% exacto en 19 filas |
| 3 | `transaction_status` en la PK → misma venta 2 veces | Riesgo de doble cobro |

**Fórmula de facturación validada al centavo:**

```
cfdi_base  = product_charges + shipping + shipping_discount + our_price_discount
cfdi_iva   = our_price_tax + shipping_tax + shipping_tax_discount + our_price_tax_disc
neto       = cfdi_base + cfdi_iva + amazon_fees + marketplace_vat_* + amazon_for_all_fee + shipping_hb
           = total_amount  ✓
```

**Regla de deduplicación:** la fila autoritativa de cada pieza es la de `order_adjustment_item_id` con `posted_date` más reciente. Sin esto, una pieza que Amazon re-publica en dos settlements se cobraría dos veces.

**ETL al día.** Tu compañero lo corrigió. Última transacción vs. última inserción: minutos de diferencia. El rezago de un mes desapareció.

## Scripts escritos

| Archivo | Qué hace | Estado |
|---|---|---|
| `AMZ_facturacion_corregida.sql` | Reporte BI 3.0 con los 3 fixes | ✅ listo |
| `01_amz_billing.py` | Facturación productiva Amazon | ⚠️ usa tablas `amz_*` que no existen |
| `02_miner_amz_payments.py` | Miner de cobros por settlement | ⚠️ igual |
| `03_loader_amz_payments.py` | Aplicación de cobros en Odoo | ⚠️ igual |
| `04_amz_billing_test_batch.py` | **Lote de prueba B2B + B2C** | ✅ usa `finance.mkp_billing_staging` |
| `PATCH_1a1_excluir_amazon.md` | Sacar Amazon del 1a1 general | ✅ listo, sin aplicar |

## Pendientes, por quién los desbloquea

**Finanzas:**

- 🔴 Estados de cuenta del **30-jul** (`27044880571`) y **06-ago** (`27172333181`) — sin esto la regla de deduplicación queda sobre una suposición
- 🔴 Fecha de inicio (`AMZ_START_DATE`) — se define tras validar el lote de hoy
- 🟡 Aceptar que una factura puede cobrarse en **varios settlements** (cobros parciales) y corregir el Paso 6 de su documento
- 🟡 Tratamiento de `LabmanLabelRefund`
- 🟡 Reglas de clasificación A/B/C de devoluciones

**Tú / Infra:**

- 🟡 Campos personalizados en `account.move` → **acordado: por ahora al chatter**
- 🟢 Diario contable Amazon → **ya existe**
- 🔴 Migrar los 3 scripts productivos de `amz_*` a las tablas `mkp_*`

**Bloqueo técnico nuevo:**

- 🔴 **Amazon no expone el XML del CFDI de Amazon Business.** `amz_b2b_invoices` solo trae metadata. Podemos sellar el UUID pero no adjuntar el archivo como hace ML en `02_loader_ml_billing.py` (`ir.attachment` + `account.edi.document`). Es el mismo bloqueo que tiene ML en notas de crédito. **Vale la pena mencionarlo hoy.**

## El lote de prueba de hoy

`04_amz_billing_test_batch.py` — 5 B2B + 5 B2C contra `somosreyes-test26ago26-37337608`.

- Usa `finance.mkp_billing_staging`, sin tablas nuevas
- `settlement_id`, `tipo` y desglose de importes van en `raw_json` (y al chatter de la factura)
- **`DO_STAMPING = False`**: crea y publica, no timbra. Publicar ya genera el asiento, que es lo que Finanzas necesita revisar
- Aborta si la URL de Odoo no contiene `test` o `.dev.odoo.com`
- Imprime tabla comparativa **Total Odoo vs. Total Amazon** por orden

> **Lo que Finanzas debe revisar:** en una factura B2C, que la contrapartida caiga en *Mercancía Facturada No Enviada* y la CxC en `105.01.006`. En una B2B, que el UUID de Amazon quedó sellado.

---
---

# PARTE 2 — Ruta de Notas de Crédito

## Punto de partida

**No arranca de cero.** Se reutiliza:

- Las mismas tablas ya corregidas y validadas (`amz_transactions` + `amz_transaction_items`)
- La misma fórmula de importes (llegan en negativo)
- La misma regla de deduplicación por `order_adjustment_item_id`
- El patrón de sweeps + watermark de `05_miner_ml_refunds.py`
- La tabla `finance.mkp_refunds_staging` que ya existe para ML

## Los tres tipos según Finanzas

| Tipo | Situación | Asiento COGS |
|---|---|---|
| **A** | Cancelación antes de despacho | Revierte *Mercancía Facturada No Enviada* |
| **B** | Devolución post-despacho, producto retorna | *Devoluciones en Tránsito* `107.06.001` |
| **C** | Bonificación sin retorno | Sin movimiento |

Ingresos es igual en los tres: `402-001-001` Dev./Desc./Bonif. y `208-001-001` IVA contra `105.01.006`.

---

## FASE 1 — Miner de devoluciones `[arranca ya, sin dependencias]`

**Objetivo:** poblar `finance.mkp_refunds_staging` con `marketplace = 'AMAZON'`.

**Filtros:**

```sql
transaction_type = 'Refund'
description IN ('Refund', 'DisbursementOnlyRefund')
transaction_status <> 'DEFERRED'
```

**Decisión de granularidad — la única de diseño en esta fase.**

El reporte actual colapsa las devoluciones a nivel transacción, porque su `GROUP BY` solo baja a nivel pieza cuando `description = 'Order Payment'`. Pero el detalle **sí existe** en `amz_transaction_items`.

```sql
-- Actual (colapsa refunds)
CASE WHEN description = 'Order Payment' THEN transaction_item_id ELSE NULL END

-- Propuesto (habilita NC parciales por SKU)
CASE WHEN description IN ('Order Payment','Refund') THEN transaction_item_id ELSE NULL END
```

**Recomendación:** ir a nivel pieza. Si una orden de 3 SKUs devuelve 1, la NC debe ir sobre esa línea. Bajar de granularidad después es fácil; subir requiere rehacer el miner.

**Entregable:** `05_miner_amz_refunds.py`

---

## FASE 2 — Perfilado de NC de Amazon Business `[arranca ya, sin dependencias]`

**Hipótesis:** las notas de crédito B2B de Amazon **ya están en la base**. `amz_b2b_invoices` tiene tres campos que lo sugieren:

- `receipt_type` — hoy filtramos `'Invoice'`; hay que ver qué otros valores existen
- `related_cfdi_relation_type` — tipo de relación CFDI; el `01` es *nota de crédito de documentos relacionados*
- `cancellation_date`

**Una sola consulta lo resuelve:**

```sql
SELECT receipt_type,
       related_cfdi_relation_type,
       COUNT(*)                      AS n,
       COUNT(DISTINCT order_id)      AS ordenes,
       MIN(date)                     AS desde,
       MAX(date)                     AS hasta
FROM somos_reyes.amz_b2b_invoices
GROUP BY receipt_type, related_cfdi_relation_type
ORDER BY n DESC;
```

Si aparecen valores distintos de `Invoice`, el flujo B2B de devoluciones se resuelve con un cruce y **cero desarrollo nuevo**.

> Ojo: aplica el mismo bloqueo del XML. Tendríamos UUID pero no archivo.

---

## FASE 3 — Motor de clasificación A/B/C `[depende de Finanzas]`

**Regla propuesta:**

```
si description = 'DisbursementOnlyRefund'        -> Tipo C   (Amazon lo marca)
si no hay stock.picking de salida en 'done'      -> Tipo A
si hay salida en 'done'                          -> Tipo B
```

**Riesgo abierto — FBA.** En la muestra, **89 de 100 filas son AFN (FBA)**: el despacho lo hace Amazon. Hay que verificar si Odoo registra la salida igual que en MFN. Si no, la regla A/B necesita otro discriminador — probablemente el `stock.picking` de retorno.

**Verificación concreta antes de programar:**

```python
# Para 10 órdenes AFN con devolución conocida, ver si tienen OUT en 'done'
models.execute_kw(db, uid, pwd, 'stock.picking', 'search_read',
    [[('origin', 'in', lista_de_SOs), ('picking_type_code', '=', 'outgoing')]],
    {'fields': ['origin', 'state', 'date_done']})
```

**Entregable:** `06_classifier_amz_refunds.py`

---

## FASE 4 — Loader de NC en Odoo `[depende de 1, 2, 3]`

Mecanismo: `account.move` con `move_type = 'out_refund'`, vinculada a la factura original vía `reversed_entry_id` y `l10n_mx_edi_cfdi_origin` (relación `01` del SAT).

- **B2B** → sella el UUID de Amazon, sin timbrar (el comprobante ya existe)
- **B2C** → WB emite y timbra

Idempotencia: `ref = 'AUTONC:<order_id>:<item_id>'`. Mismo criterio que `AUTOINV:`, con el `item_id` porque una orden puede tener varias devoluciones parciales.

**Entregable:** `07_loader_amz_credit_notes.py`

---

## FASE 5 — Reverso del cobro `[depende de que cobros esté en producción]`

**Lo que pide el documento de Finanzas:** romper la aplicación del cobro si la factura ya estaba pagada.

**Objeción técnica:** con cobros parciales de varios settlements, romper la conciliación implica deshacer y rehacer varios pagos. Es frágil y difícil de auditar.

**Propuesta alternativa:** conciliar la NC directamente contra `102.02.008 Liberaciones Amazon Pendientes de Procesar`. El reembolso viaja neteado en el mismo depósito de Amazon, así que esto refleja el movimiento real del dinero y no toca pagos ya conciliados.

Llevar a la siguiente reunión con el asiento propuesto lado a lado.

---

## Casos no contemplados en el `.md` de Finanzas

Preguntas concretas para hoy:

1. **`LabmanLabelRefund`** — 4 de 100 filas. Reembolso de guía, no de producto. ¿Lleva NC o es ajuste de gasto de fletes?
2. **Comisiones por devolución** — existen `refund_commission`, `refund_commission_base`, `refund_commission_tax` y el documento no las trata.
3. **Devolución de mes cerrado** — la NC se emite en un periodo posterior al de la factura.
4. **Devolución sin venta previa** — orden anterior al inicio del proceso automatizado.

---

## Secuencia sugerida

```
Semana 1   Fase 1 (miner) + Fase 2 (perfilado B2B)     ← sin dependencias
           Verificación FBA/OUT                          ← insumo de Fase 3
Semana 2   Fase 3 (clasificador), con reglas confirmadas
Semana 3   Fase 4 (loader NC) + lote de prueba
Después    Fase 5, cuando cobros esté en producción
```

**Para la reunión:** las fases 1 y 2 arrancan hoy sin necesidad de ninguna decisión de Finanzas. Anúncialas como en marcha — mueve la conversación de "voy tarde" a "voy en dos frentes en paralelo".
