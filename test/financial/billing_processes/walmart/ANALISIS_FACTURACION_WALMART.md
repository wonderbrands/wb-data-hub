# Walmart 3P — Facturación automática: análisis y diseño

Rama `feat/walmart-facturacion-automatica` · 10-nov-2026 · Fase de análisis, sin código.

**Fuentes leídas:**

- `test/financial/Playbook_Soporte_de_Ventas_v2.6.docx` (prevalece en contradicciones).
- `billing_processes/walmart/6. flujo_facturacion_walmart.html` (Finanzas, jun-2026).
- `invoice_creation_1a1_local_V18.py` e `invoice_creation_qtyV18.py` (proceso manual actual), más `wb_odoo_external_api/unit_tools/extract_orders.py`.
- `Servicio_de_Verificacion_de_Descarga_Masiva_2023.pdf` (SAT, v1.2).
- El patrón de ML (`02_loader_ml_billing.py`, `ml_billing_process_flow.yml`), `_amz_common.py`, `amz_billing_payments_flow.yml` y `facturacion_automatica_1a1.py`.

Las cifras salen de lecturas en MySQL del 10-nov-2026. Ninguna lectura de Odoo producción.

---

## 1. Resumen

- **La venta de Walmart 3P es la orden de la API.** Según el Playbook se toma de `somos_reyes.wm_financial_transactions_master` (en adelante **FTM**) con `type='Order'`. El monto es `price_without_tax_amount` / `price_amount` y la fecha `order_date`, que ya viene en hora de México.
- **FTM es una fila por línea.** La clave `customer_order_id + so_prime_line_sub_line_no` es única: 20,333 de 20,333 filas `Order`.
- **Walmart factura alrededor del 8 %.** Su autofacturador emite CFDI **nominativos** con el RFC de WB (`SRE1410278MA`) solo si el cliente pide factura, dentro de los 30 días: 197 órdenes en septiembre y 165 en octubre. En Fénix ningún CFDI va a XAXX010101000. El resto lo timbra WB a público general.
- **Fénix ya está en MySQL** (`somos_reyes.wm_fenix_invoices_report`). Trae UUID, pedido, línea (`iderp` = `so_prime_line_sub_line_no`), estado del CFDI y totales. Un UUID puede cubrir varias líneas y una orden puede tener varios UUID: 2,064 filas, 1,644 UUID y 1,325 pedidos.
- **El cruce con Odoo funciona** por `FTM.so` = `name` y por `channel_order_reference` = `customer_order_id` en `odoo_new_sale_order_live`.
  - Septiembre: 2,252 de 2,252 órdenes con SO.
  - Octubre: 1,886 de 1,889 (3 sin SO).
  - Ninguna tiene SO de otro equipo, y 14 órdenes tienen dos SO.
- **El 1a1 ya excluye `Team_Walmart`** (`facturacion_automatica_1a1.py:640`, coincidencia exacta). En el release se apagan los dos scripts manuales, no el 1a1.
- **No hay fuente SAT independiente en MySQL.**
  - `finance.sr_sat_emitidas` dejó de cargarse el 4-ago-2026 y no contiene ninguno de los 1,601 UUID `CON_XML_PDF` de Fénix.
  - `odoo_new_account_move` está congelada desde 2023.
  - Por eso hace falta la descarga masiva del SAT o, como respaldo, el ZIP de Finanzas.
- **Riesgo central: doble CFDI.** Si WB timbra a público general y después el cliente pide factura a Walmart, quedan dos CFDI para la misma venta. Fénix ya registra 51 CFDI `REEMPLAZADO`, 41 `CANCELADO` y 363 en `ERROR`.

> **CLAUDE.md §12 está desactualizado.** Dice que en MySQL no hay transacciones financieras de Walmart. Sí las hay: FTM (actualizada a diario), `wm_fenix_invoices_report`, `wm_payments_report`, `wm_return_orders` y `wm_tokens`, que se refresca a diario y es señal de que ya hay acceso a la API de Walmart. Ningún script de este repo las carga: las mantiene otro equipo.

---

## 2. Perfil de datos (10-nov-2026)

### FTM `type='Order'` por mes

| Mes | Líneas | Órdenes | Sin `so` | Líneas con CFDI de Walmart | Total c/IVA | Líneas WFS |
|---|---|---|---|---|---|---|
| 2026-01 | 1,756 | 1,726 | 0 | 4 | 5,524,039.20 | 227 |
| 2026-02 | 1,650 | 1,625 | 4 | 18 | 4,354,895.00 | 363 |
| 2026-03 | 1,748 | 1,721 | 2 | 151 | 5,010,995.96 | 633 |
| 2026-04 | 1,654 | 1,617 | 0 | 166 | 4,543,277.00 | 602 |
| 2026-05 | 2,386 | 2,348 | 0 | 168 | 6,825,436.70 | 794 |
| 2026-06 | 1,695 | 1,664 | 0 | 158 | 4,743,691.25 | 612 |
| 2026-07 | 2,627 | 2,573 | 6 | 163 | 6,923,946.99 | 1,368 |
| 2026-08 | 2,194 | 2,162 | 8 | 147 | 5,634,351.70 | 1,248 |
| 2026-09 | 2,277 | 2,252 | 0 | 200 | 5,799,234.73 | 1,102 |
| 2026-10 | 1,920 | 1,889 | 3 | 170 | 4,416,675.93 | 1,221 |
| 2026-11 | 426 | 423 | 0 | 1 | 786,297.91 | 345 |

- **FTM va atrasada respecto a Odoo.** Su última orden es del 6-nov, mientras Odoo ya tiene 1,273 SO de `Team_Walmart` en noviembre.
- **Estados de la orden (`order_status`):** `Delivered` 19,213 · `Shipped` 867 · `Created` 155 · `SentForFulfillment` 82 · `Acknowledged` 16.

### FTM sep–oct contra el estado de la SO

| Mes | Estado SO | `invoice_status` | CFDI de Walmart | Órdenes |
|---|---|---|---|---|
| 2026-09 | sale | invoiced | no | 2,013 |
| 2026-09 | sale | invoiced | sí | 149 |
| 2026-09 | sale | to invoice | no | 31 |
| 2026-09 | sale | to invoice | sí | 47 |
| 2026-09 | cancel | no | no / sí | 5 / 1 |
| 2026-09 | draft | no | no | 6 |
| 2026-10 | sale | invoiced | no | 1,593 |
| 2026-10 | sale | invoiced | sí | 127 |
| 2026-10 | sale | to invoice | no | 76 |
| 2026-10 | sale | to invoice | sí | 37 |
| 2026-10 | cancel | no | no | 33 |
| 2026-10 | draft | no | no / sí | 19 / 1 |

- **Pendientes de sep–oct:**
  - 84 órdenes con CFDI de Walmart esperan la carga del XML (47 + 37).
  - 107 sin CFDI de Walmart esperan el timbrado de WB (31 + 76).
- **SO de `Team_Walmart` de sep–oct sin fila `Order` en FTM:** 100 canceladas, 48 `to invoice` y 15 `invoiced`. Según el Playbook son "solo Odoo" y no son venta.
- **Montos:** de las 1,346 líneas con CFDI de Walmart, 1,224 cuadran ±$1 entre `price_amount` y lo facturado; 69 están por arriba y 53 por abajo.
- **Retenciones y killers:** el 100 % de las líneas de sep–oct trae `withholding_iva_amount` e `withholding_isr_amount`, y 1,427 de 4,197 traen `killer ≠ 0`.

### Fénix (`wm_fenix_invoices_report`)

| Tipo | Modo | `estado_cfdi` | Filas | Creación |
|---|---|---|---|---|
| I | EN_AUTOMATICO | CON_XML_PDF | 1,596 | nov-2025 → 2-nov-2026 |
| I | EN_AUTOMATICO | ERROR | 363 | nov-2025 → 30-oct-2026 |
| I | EN_AUTOMATICO | REEMPLAZADO | 51 | mar → oct-2026 |
| I | EN_AUTOMATICO | CANCELADO | 41 | nov-2025 → sep-2026 |
| I | EN_AUTOMATICO | CANCELANDO | 1 | 19-oct-2026 |
| I | HIBRIDO | NUEVO | 7 | sep → oct-2026 |
| E | EN_AUTOMATICO | CON_XML_PDF | 5 | 20-jul-2026 |

- La última carga es del **2-nov-2026**: 8 días sin actualizarse.

---

## 3. Playbook vs HTML

| # | Diferencia | Impacto | Recomendación |
|---|---|---|---|
| 1 | **Fuente.** El HTML habla de "el reporte Walmart"; el Playbook nombra FTM `type='Order'` | El HTML no dice qué tabla usar | Usar FTM. El CSV de Finanzas queda solo como control mientras corren en paralelo |
| 2 | **Universo.** El HTML toma toda fila con `order_date` del periodo, "order_status irrelevante". El Playbook excluye la SO "solo Odoo" y cuenta la SO cancelada con movimiento | En sep–oct hay 39 órdenes con SO cancelada y 26 con SO en borrador que sí están en FTM | Igual que Amazon: borrador → `action_confirm`; cancelada → se factura y la NC va por devoluciones. **Confirmar con Finanzas** |
| 3 | **SO faltante.** El HTML dice "Alerta + Crear SO"; CLAUDE.md §8 dice que Yuju crea las SO y nosotros nunca | Contradicción directa | Solo alerta (`NO_SO`) |
| 4 | **Barrido.** El HTML barre desde el 1-ene-2026 con tope de 12 meses; el Playbook no habla de facturación hacia atrás | El SAT no timbra con más de 72 h: lo histórico no se puede emitir con su fecha | El barrido solo detecta y reporta. El histórico es una decisión aparte, como Amazon ene–sep |
| 5 | **Killers.** El HTML no los menciona. El Playbook dice que el autofacturador de Walmart los emite cada jueves (serie PGA) al RFC de Walmart y van a 403.01.005 | Son CFDI de WB que hoy están en apuntes misceláneos a la 401 | Fuera de esta fase, pero usan el mismo mecanismo de jalar el XML del SAT |
| 6 | **Walmart 1P.** El HTML no lo menciona; el Playbook lo trata como canal propio (OC, Retail Link, RFC de Walmart) | El 1a1 factura `Team_Walmart_1P` **a público general**, cuando el receptor debería ser el RFC de Walmart (NWM9709244W4) | Fuera de alcance; reportar el receptor. El flujo nuevo solo toca `Team_Walmart` y la 105.01.007 |
| 7 | **NC por sustitución.** El HTML pide una "NC por sustitución" si el cliente pide factura después del CFDI a público general | Sustituir un CFDI individual se hace con cancelación motivo 01 y relación 04, no con NC. Y el CFDI que sustituye lo emite Walmart, no nosotros | Definir quién cancela el nuestro y cómo se detecta (estado `DOBLE_CFDI`) |
| 8 | **Asiento.** El HTML pone costo contra 205.06.003 (sin restricción de OUT) | `invoice_creation_1a1_local_V18.py` factura `qty_delivered`, o sea **con** restricción de OUT | Sin restricción de OUT, como Amazon. Riesgo: el 55 % de las líneas de sep–oct son WFS (2,323 de 4,197), igual que el caso FBA |
| 9 | **Cobros.** El HTML cobra por fecha de liberación, contra una cuenta puente cuyo código está "por confirmar", y deja las retenciones para la fase 2 | El 100 % de las líneas trae retención de IVA e ISR. El Playbook advierte que el campo `venta` se repite por orden | Cobros en otra fase; pedir el código de la cuenta puente |
| 10 | **Devoluciones.** El Playbook usa `type='Refund'` reconstruido por línea; el HTML es solo de facturación | Fénix tiene 5 CFDI de egreso emitidos por Walmart | Va al documento 7 (devoluciones); el miner los registra pero no los procesa |

---

## 4. Huecos de negocio para Finanzas

1. **Gracia y cierre de mes.** El cliente tiene 30 días para pedir factura, pero la venta se factura en su mes y dentro de las 72 h. ¿Cuántos días de gracia antes de timbrar a público general? Toda venta de fin de mes que el cliente facture después generará un doble CFDI.
2. **Doble CFDI.** ¿Quién cancela el CFDI de WB cuando Walmart emite después, y con qué UUID sustituto? ¿Qué pasa en Odoo con nuestra factura y con su cobro?
3. **Fénix en `ERROR` (363).** El cliente pidió factura y Walmart no la emitió. ¿La emite WB nominativa, a público general, o se espera?
4. **`REEMPLAZADO`, `CANCELANDO` e `HIBRIDO/NUEVO`.** ¿Qué significan exactamente? ¿Se carga el XML original o solo el sustituto?
5. **Órdenes mixtas.** Si unas líneas tienen CFDI de Walmart y otras no, ¿WB timbra el resto?
6. **Monto.** En 122 de 1,346 líneas con CFDI de Walmart el XML no cuadra ±$1 contra `price_amount`. ¿Cuál manda?
7. **Envío.** ¿Walmart cobra envío al cliente (`charge_type` en `wm_order_line`)? ¿Va con `C-ENVIO` / 401.01.002? Ninguno de los dos documentos lo dice.
8. **Fecha de la factura cargada desde el XML.** Si Walmart emite en noviembre el CFDI de una venta de octubre, la factura en Odoo queda en noviembre, contra la regla de "venta en su mes". ¿Se acepta?
9. **Retenciones de IVA e ISR** y la cuenta puente "Liberaciones Walmart Pendientes de Procesar" (código pendiente).
10. **Qué filtra hoy el CSV de Finanzas.** Si el CSV excluye algo que FTM sí trae, esa regla tiene que quedar escrita.

---

## 5. Reglas de negocio de los scripts actuales

### `invoice_creation_1a1_local_V18.py` (timbra WB)

- **Entrada:** `facturacion_global.csv`, columna `so_name`.
- **Cabecera de la factura:**
  - `partner_id` 13436, `team_id` 30, CxC 105.01.007 forzada en la línea `asset_receivable`, impuesto 38.
  - Uso S01, forma de pago 3, PUE.
- **Blindajes que ya trae:**
  - Idempotencia `AUTOINV:<SO>`.
  - Snapshot `{origen: [facturas]}`; con más de una factura viva → `DUPLICADO_FISCAL`.
  - Revalidación fail-closed antes del `create`.
  - `OdooWriteUncertain` con reconciliación leyendo.
- **Problemas:**
  - **Factura `qty_delivered`**, o sea con restricción de OUT, contra el patrón de CLAUDE.md §8 y el asiento del HTML.
  - **`invoice_date`:** hoy si se corre en el mes; si no, el último día del mes. Desde el día 4 el SAT lo rechaza por las 72 h.
  - Registra en `finance.billing_audit_log`, no en `mkp_billing`.

### `invoice_creation_qtyV18.py` (timbra Walmart, carga el XML)

- **Entrada:** `autofacturacion.csv` (`Marketplace Reference`, `Folio`, `xml` = UUID) más una carpeta local de XML del SAT.
- **Cómo liga orden y XML:** solo por el CSV. El comentario del script dice que "Walmart NO coloca el Marketplace reference en el Folio del xml".
- **Qué crea:**
  - Una factura **por XML** con `quantity=1` y el `price_unit` de la SO.
  - Escribe `l10n_mx_edi_cfdi_uuid`, `narration` e `invoice_date` (del atributo `Fecha` del XML), un `ir.attachment` y un `account.edi.document` `sent`.
  - Después hace `action_post`.
- **Problemas:**
  - Asigna los XML a las líneas **en el orden en que aparecen, sin cruzar el SKU**: un XML puede quedar en la línea equivocada.
  - No valida el total contra el XML.
  - `ref=''`: sin idempotencia `AUTOINV:`. Lo único que evita duplicar es comparar los UUID ya grabados.
  - El partner y la CxC salen de la SO: no fuerza la 105.01.007.
  - Hace `unlink` como rollback, que es una escritura reintentada sobre un estado que no conoce.
  - El parser del CSV arma `dict(zip(Folio, …))`, que colapsa los folios repetidos.
  - Lee `odoo_new_sale_order` (no `_live`) con `LIKE '%walmart%'`.

---

## 6. Cómo validar "qué facturar"

**Ningún archivo se usa solo.** Hay tres patas que se cruzan y una de control:

| Pata | Fuente | Clave |
|---|---|---|
| Walmart vendió | FTM `type='Order'` | orden `customer_order_id`; línea `so_prime_line_sub_line_no` |
| CFDI de Walmart | `wm_fenix_invoices_report` | `UPPER(uuid)`, `pedido`, `iderp` = línea |
| El CFDI existe y está vigente | XML del SAT (descarga masiva o ZIP de Finanzas) | UUID |
| Hay SO | `odoo_new_sale_order_live` | `channel_order_reference` = `customer_order_id`; `FTM.so` = `name` |
| Control durante el paralelo | CSV de Finanzas | `Marketplace Reference`, `Folio`, `xml`; `so_name` |

**Reglas del cruce:**

- **Duplicados:**
  - Una orden con más de una SO viva → `SO_DUPLICADA`, se aparta.
  - Una orden con más de una factura viva en Odoo → `DUPLICADO_FISCAL`.
  - El mismo UUID en dos facturas → `DUPLICADO_FISCAL`.
  - Siempre `{origen: [facturas]}` y `{uuid: [facturas]}`.
- **Montos:** Σ `price_amount` de las líneas que cubre el UUID contra el `Total` del XML, ±$1. Fuera de tolerancia → `TOTAL_DIFF`.
- **Frescura, fail-closed:**
  - Si Fénix tiene más de 24 h sin cargarse, **no se timbra nada**: sin Fénix al día no sabemos si Walmart ya facturó.
  - Si FTM tiene más de 24 h, no se corre el miner.
- **Independencia:** los campos `wm_invoice_*` de FTM no cuentan como pata independiente, porque casi seguro salen de Fénix. La pata independiente es el XML del SAT.
- **Retiro del CSV:** después de dos cierres en paralelo sin diferencias sin explicar.

---

## 7. Arquitectura propuesta

### Scripts (en `test/financial/billing_processes/walmart/`)

| Script | Qué hace |
|---|---|
| `_wm_common.py` | Plantilla de `_amz_common`: `WM_ENTORNO` con candado cruzado, `TBL_BILLING`, `MARKETPLACE='WALMART'`, `OdooModelProxy`, `OdooWriteUncertain`, `ejecutar_sin_retorno`, `normalizar_uuid` |
| `00_wm_precheck.py` | Frescura de FTM, Fénix y el repositorio de XML; candado de entorno. Si algo falla, aborta |
| `01_wm_sat_sync.py` | Asíncrono: en cada corrida verifica y descarga la solicitud anterior y crea la siguiente. Respaldo: lee un ZIP de una carpeta de entrada. Guarda XML y estatus por UUID |
| `02_wm_miner.py` | Arma y actualiza `mkp_billing_{entorno}` y asigna estados |
| `03_wm_loader_pull.py` | Una factura en Odoo por UUID; adjunta el XML; **no timbra** |
| `04_wm_billing_push.py` | Una factura por SO a público general y la timbra |
| `05_wm_control.py` | Reporte de alertas y cruce contra el CSV de Finanzas |

**Amazon B2B.** El CFDI de Amazon Business sale con el RFC de WB, así que aparece en las emitidas del SAT. `01_wm_sat_sync` puede dar el XML de las B2B que hoy no tienen adjunto, con un modo "solo adjuntar" que busca la factura por `l10n_mx_edi_cfdi_uuid`. Por eso el repositorio de XML debe ser genérico, no de Walmart.

### Registro en `finance.mkp_billing_{staging,prod}`

Columnas existentes: `marketplace`, `mkp_order_id`, `odoo_so_name`, `cfdi_uuid`, `total_amount`, `xml_data`, `raw_json`, `status`, `retry_count`, `retry_count_loader`, `error_log`.

- **WB timbra:** una fila por orden, `mkp_order_id = customer_order_id`.
- **Walmart timbra:** una fila por CFDI, `mkp_order_id = f"{customer_order_id}:{UUID}"`, con `cfdi_uuid` y `xml_data`. Mismo separador `:` que `payment_id`. Así `uk_mkp_order` sigue siendo idempotente aunque una orden traiga varios CFDI.
- `raw_json` guarda las líneas de FTM, las filas de Fénix y `facturas_odoo`.

### Estados

| Estado | Condición | Siguiente |
|---|---|---|
| `NO_SO` / `SO_DUPLICADA` / `SO_OTRO_EQUIPO` | El cruce con Odoo falla | Alerta |
| `PENDING_GRACE` | Sin CFDI de Walmart y dentro de la gracia | Espera |
| `WB_TO_STAMP` | Gracia vencida, o cierre de mes (días 1–2) | 04 |
| `WB_STAMPED` | Factura timbrada | Final |
| `WM_CFDI_PENDING_XML` | Fénix dice `CON_XML_PDF` y no está el XML | 01 |
| `WM_CFDI_READY` | XML presente y cuadra | 03 |
| `WM_CFDI_LINKED` | Factura con el UUID de Walmart | Final |
| `WM_CFDI_ERROR` / `WM_CFDI_CANCELLED` / `WM_CFDI_REPLACED` | Estado de Fénix o del SAT | Finanzas |
| `DOBLE_CFDI` | Factura timbrada por WB y además CFDI vigente de Walmart | Finanzas cancela la nuestra |
| `ALREADY_ODOO_INVOICED` | Factura viva previa (proceso manual) | Final, se audita |
| `TOTAL_DIFF` / `CXC_INCORRECTA` / `WRITE_UNCERTAIN` / `ERROR_*` | | Reconciliar leyendo |

### Criterio "timbra WB" vs "timbra Walmart" (por línea, `iderp`)

- Hay un CFDI de Fénix **vigente** (`CON_XML_PDF`, tipo I) que cubre la línea → timbra Walmart.
- Si no, y vencieron la gracia y la frescura → timbra WB.
- `ERROR`, `CANCELANDO` y los estados desconocidos → hold (fail-closed).

### Mapeo a Odoo

| Campo | Push (WB timbra) | Pull (Walmart timbra) |
|---|---|---|
| `partner_id` | 13436 (XAXX010101000) | Partner por RFC receptor del XML; se crea si falta, como el `partner_map` de ML |
| `team_id` | `Team_Walmart` (30, se resuelve por nombre) | igual |
| CxC | 105.01.007, forzada en la línea `asset_receivable` | igual |
| Impuesto | 38 | 38 |
| Líneas | `product_uom_qty` de la SO (sin restricción de OUT); `sale_line_ids` | Por concepto del XML, ligado a la línea de la SO por `iderp`/SKU |
| `ref` | `AUTOINV:<SO>` | `AUTOINV:<SO>:<UUID>` |
| `invoice_date` | Hoy, o el último día del mes en los días 1–2 | `Fecha` del XML |
| Uso, forma y método de pago | S01, 03, PUE | Del XML |
| CFDI | `account.move.send.wizard` (apagado en staging) | `l10n_mx_edi_cfdi_uuid` + `narration`, `ir.attachment` del XML, `account.edi.document` `sent` |
| Chatter | `customer_order_id`, `purchase_order_id`, fulfillment | igual + UUID |

**El flujo no usa la 105.01.008:** esa cuenta es de Walmart 1P.

### Blindajes (CLAUDE.md §6)

1. **Exclusión mutua:** `concurrency: limit 1, behavior: FAIL`, sin `retry` en ninguna tarea.
2. **Revalidación fail-closed:** justo antes de cada `create`, `search_count` por `invoice_origin` **y** por `ref like 'AUTOINV:<SO>%'`. Buscar también por `invoice_origin` es necesario porque `invoice_creation_qtyV18.py` creaba facturas con `ref=''`. Si la consulta falla, no se crea.
3. **Idempotencia:** `AUTOINV:<SO>` y `AUTOINV:<SO>:<UUID>`, con el prefijo compartido.
4. **Solo se reintentan lecturas:** una escritura que falla por red lanza `OdooWriteUncertain` y se reconcilia leyendo. Se elimina el `unlink` de rollback del script actual.
5. **Duplicados:** `{origen: [facturas]}` y `{uuid: [facturas]}`.

### Queries de solo lectura (base del miner)

```sql
-- A. Universo por línea, con SO
SELECT f.customer_order_id, f.purchase_order_id, f.so_prime_line_sub_line_no, f.so, f.sku,
       f.quantity, f.price_without_tax_amount, f.tax_amount, f.price_amount,
       f.fulfillment_type, f.order_status, f.order_date,
       s.name AS so_odoo, s.state, s.invoice_status, s.team_name
FROM somos_reyes.wm_financial_transactions_master f
LEFT JOIN somos_reyes.odoo_new_sale_order_live s
       ON s.channel_order_reference = f.customer_order_id
WHERE f.type = 'Order' AND f.order_date >= %(desde)s AND f.order_date < %(hasta)s;

-- B. CFDI de Walmart por línea
SELECT UPPER(uuid) AS uuid, tipo_documento, pedido, iderp, prime_line, sub_line, cantidad,
       subtotal, total_factura, rfc_receptor, usocfdi, estado_cfdi, fecha_creacion, inserted_at
FROM somos_reyes.wm_fenix_invoices_report
WHERE pedido IN (%s);

-- C. Frescura (precheck)
SELECT (SELECT MAX(updated_at)  FROM somos_reyes.wm_financial_transactions_master) AS ftm,
       (SELECT MAX(inserted_at) FROM somos_reyes.wm_fenix_invoices_report)         AS fenix;

-- D. SO duplicadas por orden
SELECT channel_order_reference, GROUP_CONCAT(name)
FROM somos_reyes.odoo_new_sale_order_live
WHERE team_name = 'Team_Walmart' AND state <> 'cancel'
GROUP BY 1 HAVING COUNT(*) > 1;
```

Lo que hay en Odoo se lee por XML-RPC, porque las réplicas de facturas están congeladas. En producción lo corre Sergio.

---

## 8. Descarga masiva del SAT

- **Requisito.** La e.firma vigente **del RFC emisor `SRE1410278MA`**, que es el de la empresa (persona moral). Si la FIEL disponible es personal (RFC de persona física), **no sirve**. El certificado tiene que ser el de Somos Reyes, con `.cer`, `.key` y contraseña. El sello CSD no sirve para este servicio.
- **Riesgo de la e.firma.** Tiene poder legal (declaraciones, contratos). El propio PDF del SAT pide no guardarla en equipo de terceros. En Kestra iría en secrets cifrados y con acceso restringido. Lo tiene que aprobar Finanzas o Dirección.
- **Flujo.** Son cuatro servicios SOAP:
  1. Autenticación: token de vida corta.
  2. Solicitud: `RfcEmisor`, rango de fechas y `TipoSolicitud` (CFDI o Metadata).
  3. Verificación: estado 1–6 (Aceptada, En proceso, Terminada, Error, Rechazada, Vencida); cuando es 3 devuelve `IdsPaquetes`.
  4. Descarga: cada paquete es un ZIP en base64.

  La solicitud tarda de minutos a horas, por eso el script es asíncrono entre corridas.
- **Límites.**
  - **Vienen en el PDF:** el paquete vence a las 72 h (estado 6); tope de resultados por solicitud (5003); solicitudes "por vida" con los mismos parámetros (5002); solicitud duplicada vigente (5005); límite de descargas diarias por folio (5011); certificado revocado o caduco (305).
  - **Hay que verificarlos en el portal** (el PDF no los trae): el tope de CFDI por solicitud (se conoce como 200 mil en XML y 1 millón en metadata) y si los cancelados se descargan en XML o solo en metadata.
- **Costo.** Sin costo del SAT.
- **Firmado.**
  - Autenticación: WS-Security con un `Timestamp` firmado y el certificado como `BinarySecurityToken`.
  - Solicitud, Verificación y Descarga: firma XMLDSig *enveloped* dentro del nodo de la solicitud, con canonicalización C14N, digest SHA-1 y firma RSA-SHA1 con la llave privada (`.key`, PKCS#8 DER cifrado con la contraseña), y `KeyInfo` con emisor y serie del certificado.
  - Hacen falta `cryptography` y `lxml`. **Ninguna de las dos está en `requirements.txt` ni en los Dockerfiles**, así que hay que agregarlas a la imagen `wonderbrands-python:v1`. *(Actualización 13-nov: la implementación solo necesita `cryptography`, ya agregada; ver README_WALMART.md.)*
  - Se propone escribir unas 200 líneas propias en lugar de una librería poco conocida como `cfdiclient`.
- **Respaldo.** Una carpeta de entrada para el ZIP que manda Finanzas. El script trata igual un XML venga de donde venga, y ningún UUID se carga sin XML.

---

## 9. Kestra (propuesta; no se crea en `flows/` hasta el PR)

```yaml
id: wm_billing_flow
namespace: wonderbrands.financial.walmart
description: |
  Walmart 3P: XML del SAT (01), miner (02), carga de CFDI de Walmart sin timbrar (03)
  y timbrado propio a público general (04). Control y alertas (05).
  Team_Walmart ya está excluido del 1a1. Este flujo reemplaza los scripts manuales.

concurrency:
  limit: 1
  behavior: FAIL          # sin cola, sin retry (incidente 26-jul-2026)

inputs:
  - id: start_date
    type: STRING
    required: false
    displayName: "Desde (AAAA-MM-DD). Vacío = día 1 del mes anterior"
  - id: max_facturas
    type: STRING
    required: false
    displayName: "Tope de facturas creadas (vacío = sin tope)"

variables:
  repo_url: "https://github.com/wonderbrands/wb-data-hub.git"
  branch: "main"
  odoo_url: "https://somosreyes.odoo.com"
  odoo_db: "wonderbrands2026-main-28990431"
  db_user: "kestra_bot"
  db_name: "finance"
  db1_host: "wonderbrands1.cuwd36ifbz5t.us-east-1.rds.amazonaws.com"
  py: "processes/wonderbrands/financial/billing_processes/walmart"

tasks:
  - id: wd
    type: io.kestra.plugin.core.flow.WorkingDirectory
    tasks:
      - id: clone_repo
        type: io.kestra.plugin.git.Clone
        url: "{{ vars.repo_url }}"
        branch: "{{ vars.branch }}"
        username: "{{ secret('GIT_USER') }}"
        password: "{{ secret('GIT_TOKEN_WB') }}"

      - id: wm_pipeline
        type: io.kestra.plugin.scripts.shell.Commands
        containerImage: wonderbrands-python:v1
        taskRunner:
          type: io.kestra.plugin.scripts.runner.docker.Docker
          pullPolicy: NEVER
        env:
          WM_ENTORNO: "prod"
          WM_START_DATE: "{{ inputs.start_date ?? '' }}"
          WM_MAX_FACTURAS: "{{ inputs.max_facturas ?? '' }}"
          ODOO_URL: "{{ vars.odoo_url }}"
          ODOO_DB: "{{ vars.odoo_db }}"
          ODOO_USER: "{{ secret('odoo_user_dataV18') }}"
          ODOO_PASSWORD: "{{ secret('odoo_password_dataV18') }}"
          DB_HOST: "{{ vars.db1_host }}"
          DB_USER: "{{ vars.db_user }}"
          DB_PASSWORD: "{{ secret('DB_PASSWORD_KESTRA') }}"
          DB_NAME: "{{ vars.db_name }}"
          SAT_FIEL_CER_B64: "{{ secret('SAT_FIEL_CER_B64') }}"
          SAT_FIEL_KEY_B64: "{{ secret('SAT_FIEL_KEY_B64') }}"
          SAT_FIEL_PASSWORD: "{{ secret('SAT_FIEL_PASSWORD') }}"
          SLACK_WEBHOOK: "{{ secret('SLACK_WEBHOOK') }}"
          KESTRA_EXECUTION_ID: "{{ execution.id }}"
        commands:            # un solo contenedor: si un paso falla, no corren los siguientes
          - python {{ vars.py }}/00_wm_precheck.py
          - python {{ vars.py }}/01_wm_sat_sync.py
          - python {{ vars.py }}/02_wm_miner.py
          - python {{ vars.py }}/03_wm_loader_pull.py
          - python {{ vars.py }}/04_wm_billing_push.py
          - python {{ vars.py }}/05_wm_control.py

triggers:
  # CDMX. Huecos de Odoo: ML 00/06/12/18 · MLO 03/09/15/21 · AMZ 02:30/15:00
  # 1a1 04:30/19:30 · alertas 08:00 · cierre AMZ día 2 09:00.
  # 10:30: entre MLO 09:00 y ML 12:00. Los días 1 y 2 hacen el cierre del mes
  # anterior (fecha = último día, dentro de las 72 h). Nunca el día 3.
  - id: corrida_diaria
    type: io.kestra.plugin.core.trigger.Schedule
    cron: "30 10 * * *"
    timezone: America/Mexico_City
    recoverMissedSchedules: LAST

errors:
  - id: slack_alert
    type: io.kestra.plugin.slack.notifications.SlackIncomingWebhook
    url: "{{ secret('SLACK_WEBHOOK') }}"
    payload: |
      {"text": "🚨 Falló wm_billing_flow · ejecución {{ execution.id }}"}
```

- **Horas:** el script calcula la hora CDMX desde UTC (−6 fijo) y usa fronteras de día exclusivas. FTM y Fénix ya vienen en hora de México (Playbook §3.3).
- **Choque de horario:** el día 2, la corrida de las 10:30 puede encimarse con el cierre de Amazon de las 09:00 si este tarda más de 1.5 h. No duplica CFDI, pero carga Odoo a la vez.
- **Release:**
  1. Fusionar el flujo con el PR.
  2. Ese mismo día, dejar de correr `invoice_creation_1a1_local_V18.py` y `invoice_creation_qtyV18.py`.
  3. Mantener `teams_dict.pop('Team_Walmart')` del 1a1. Es coincidencia exacta y no saca a `Team_Walmart_1P`.
  4. La primera corrida en prod va con `max_facturas` bajo.
  5. Las facturas que hizo el proceso manual entran como `ALREADY_ODOO_INVOICED`, porque se buscan también por `invoice_origin`.

---

## 10. Decisiones pendientes

1. Días de gracia antes de timbrar a público general, y qué se hace con las ventas de fin de mes.
2. Proceso del doble CFDI: quién cancela, motivo 01 con UUID sustituto, y qué pasa en Odoo y con el cobro.
3. Tratamiento de Fénix en `ERROR`, `REEMPLAZADO`, `CANCELANDO` e `HIBRIDO/NUEVO`.
4. Órdenes mixtas (CFDI de Walmart solo en algunas líneas).
5. Si manda el monto del XML o el de la orden cuando difieren más de $1.
6. SO cancelada o en borrador que sí está en FTM: ¿se factura?
7. Envío: ¿hay cobro al cliente y va con `C-ENVIO`?
8. e.firma de la persona moral: confirmar que es del RFC de la empresa y aprobar que viva en Kestra. Si no se aprueba, queda solo el ZIP manual.
9. Agregar `cryptography` a la imagen *(hecho en el Dockerfile el 13-nov; falta reconstruir la imagen)*.
10. Repositorio de XML genérico: una tabla nueva en `finance` o revivir `sr_sat_emitidas` (sin carga desde el 4-ago).
11. Data: quién carga `wm_fenix_invoices_report` y FTM, con qué frecuencia (Fénix lleva 8 días sin cargarse), y si viene de la API de Walmart.
12. Una fila por CFDI en `mkp_billing` (`<orden>:<UUID>`) para el pull.
13. Killers y Walmart 1P: fuera de esta fase, pero reportar que el 1a1 factura 1P a público general.
14. Validar cómo registra Odoo la salida de WFS (55 % de las líneas) para liberar la 205.06.003.

---

## 11. Qué se verificó y qué no

**Verificado (lecturas MySQL del 10-nov-2026):**

- Todas las cifras de los apartados 1 y 2.
- El cruce de FTM contra `odoo_new_sale_order_live` por las dos llaves, y la unicidad de la clave por línea de FTM.
- Que `sr_sat_emitidas` no contiene ningún UUID de Fénix y que `odoo_new_account_move` está congelada.
- Que el 1a1 excluye `Team_Walmart`.
- Que `cryptography` y `lxml` no están en `requirements.txt` ni en los Dockerfiles.

**No verificado:**

- **Odoo producción (lo corre Sergio):** qué facturas y UUID tienen hoy las SO de Walmart, si hay doble CFDI histórico, el `team_id` 30 y las salidas de WFS.
- **Archivos de Finanzas:** no hubo CSV ni ZIP de un cierre real para comparar contra lo calculado.
- **El SAT:** el PDF solo cubre el servicio de Verificación. Los servicios de Autenticación, Solicitud y Descarga y los topes se describen de memoria y hay que confirmarlos en el portal.
- **La API de Walmart y Fénix:** no se probó, y no se sabe de dónde sale `wm_tokens`.
- **Código:** no se escribió ni se compiló.
