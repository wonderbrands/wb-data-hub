# Barrido histórico de Amazon — contexto para la sesión que lo implementa

Redactado el 10-nov-2026 a partir de la auditoría de Finanzas del 8-nov (Jero), la base del script 14 y el código en producción. Este documento es el brief completo: con esto, más `CLAUDE.md` y `processes/.../amazon/README_AMAZON.md`, se pueden escribir los scripts.

**Rama:** nueva, desde `main`, por ejemplo `feat/barrido-historico-amazon`. **Requiere que `fix/invoice-amazon` ya esté fusionada** (ver §1.2).

---

## 0. Qué hay que hacer

Son dos barridos con objetivos distintos. **Ninguno aplica cobros.**

| Parte | Qué | Volumen | Fuente |
|---|---|---|---|
| **A. Business** | Las órdenes Amazon Business las factura **solo Amazon**. Hay que cancelar nuestras facturas de esas órdenes, sacar con NC parcial las que están dentro de facturas globales y registrar en Odoo los CFDI de Amazon que falten | A6: 82 + 8 · A7: 339 · A8: 368 · A9: 24 | Excel de Finanzas, hojas A6 a A9 |
| **B. Facturación histórica** | Facturar las ventas que Amazon cobró entre enero y octubre de 2026 y que nadie facturó | 4,960 órdenes (Odoo 18: ~1,700) | `14_base_historico_amazon.py` |

---

## 1. Contexto

### 1.1 El flujo en producción (resumen)

- `amz_billing_payments_flow` (Kestra, 02:30 y 15:00 CDMX) corre `00 → 01 → 02 → 03`. Factura desde el 1-oct-2026; **octubre está cerrado**. Antes de octubre, Amazon lo facturaba el 1a1 (Amazon salió del 1a1 el 1-oct).
- **01** es miner + loader de facturación:
  - **B2B:** vincula el CFDI de Amazon, sin timbrar y con la **fecha del CFDI**.
  - **B2C:** timbra a público general.
- **02/03** son los cobros por settlement.
- Detalle completo en `README_AMAZON.md`.
- El miner acepta una ventana: `AMZ_START_DATE` / `AMZ_END_DATE` (en el flujo, inputs `start_date` / `end_date`; `end_date` es exclusivo). Con `cobros=false` el flujo no corre 02 ni 03.

### 1.2 Reglas nuevas en `fix/invoice-amazon` (fusionar antes de este trabajo)

- **Business = `IsBusinessOrder` o CFDI de Amazon.** Antes se decidía solo por el CFDI, y por eso 75 órdenes Business sin CFDI salieron a público general (A6).
- **`B2B_SIN_CFDI`:** orden Business sin CFDI de Amazon. **No se factura.** Se reevalúa en cada corrida y pasa a `PENDING_B2B_LINK` cuando Amazon emite el CFDI.
- `detectar_b2b_tardio()` también saca de la emisión B2C a las Business sin CFDI que estuvieran en `PENDING_WB_INVOICE`.
- **03 no aplica cobros** a una orden Business que no esté en `ODOO_B2B_LINKED` (queda en `NO_INVOICE`, que se reintenta 60 días).
- **Gracia:** 6 días desde la compra. **Tolerancia de redondeo:** $0.15 por factura contra 704.23.001.

### 1.3 Lo que ya existe y se reutiliza

| Pieza | Dónde | Para qué |
|---|---|---|
| `solicitar_cancelacion()`, `estado_cancelacion()`, `cancelar()` | `test/.../amazon/12_sanear_facturas_amazon.py` | Cancelación SAT desde Odoo: `button_request_cancel` abre el asistente, se crea con `cancellation_reason='02'`, y `l10n_mx_edi_cfdi_try_sat` confirma. **En dos pasadas:** el SAT no confirma al instante. |
| `procesar()` (camino B2B) | `01_amz_billing.py` | Vincular el CFDI de Amazon: partner (`resolver_partner_b2b`), varios CFDI por orden (`armar_partes_split`), centavo contra el CFDI (`cuadrar_con_cfdi`), fecha del CFDI, conciliación de la 205.06.003 (`conciliar_salida`). |
| `reabrir_b2b_pg()` | `01_amz_billing.py` | Cada corrida regresa las `B2B_FACTURADA_PG` a `PENDING_B2B_LINK`; si nuestra factura ya se canceló, se vincula sola. |
| `facturas_existentes()`, `netear_con_nc()`, `es_global()` | `01_amz_billing.py` | Detectar facturas vivas de la SO (incluye globales y NC que netean). |
| `contrapartidas()`, `reconciliar()` | `12_sanear_facturas_amazon.py` | Romper y rehacer conciliaciones. |
| `ejecutar_sin_retorno()`, `OdooModelProxy`, `OdooWriteUncertain` | `_amz_common.py` | Blindajes XML-RPC. |
| `14_base_historico_amazon.py` | `test/.../amazon/` | Base de la Parte B (solo lectura, MySQL). |
| `000_historical_billing_back_analysis.py` | `test/.../amazon/` | Doble CFDI B2B, abr–nov. Lee Odoo prod: lo corre Sergio. |

---

## 2. Insumos

| Archivo | Dónde | Contenido |
|---|---|---|
| `Amazon_auditoria_produccion_hallazgos_2026-11-08.xlsx` | `C:\Users\Sergio Gil Guerrero\Downloads\`. Copiarlo a `test/.../amazon/`; los CSV están en `.gitignore`, revisar también el xlsx | Hoja **Resumen** + bases A1 a A9 |
| `4. flujo_facturacion_amazon_v3.html` | `C:\Users\Sergio Gil Guerrero\Downloads\` | Reglas vigentes de Finanzas (v3, 9-nov) |
| `base_saneamiento_historico_amazon_20261103_2301.csv` | `test/.../amazon/` del checkout principal (no versionado) | Base de la Parte B, una fila por orden, con `categoria` |
| `amz_doble_facturacion_20261103_1846.csv` | Ídem | Salida del 000: 240 `DOBLE_CFDI`, 151 `SOLO_AMAZON`, 137 `VINCULADA_OK`. Jero la pidió para cruzarla con A7 |

**Columnas de las hojas que se usan:**
- **A6 (Business oct–nov):** `Orden Amazon, SO, Total orden, CFDI Amazon (Sí/No), UUID Amazon (8 car.), RFC receptor Amazon, Factura Somos Reyes, Fecha factura, Total factura, Receptor, Cuenta por cobrar, Origen (Flujo/Portal), Cobro PAMZ, Acción`.
- **A7 (Business histórico, facturas individuales y nominativas nuestras):** `Odoo, Orden Amazon, Mes, SO, Total orden, UUID CFDI Amazon, Total CFDI Amazon, Factura, Fecha factura, Total factura, Cliente, RFC, Tipo de factura, Estado de pago, Cobro aplicado, Acción`.
- **A8 (Business dentro de globales):** `Odoo, Orden Amazon, Mes, SO, Total orden, UUID CFDI Amazon, Total CFDI Amazon, Factura global, Fecha factura, Total global, Estado de pago, Acción`. Tiene 630 filas: 368 con CFDI de Amazon ("NC parcial") y el resto "Queda en la global".
- **A9 (CFDI de Amazon sin registrar y sin factura nuestra):** `Odoo, Orden Amazon, Mes, SO, Total orden, UUID CFDI Amazon (puede traer varios separados por ';'), Total CFDI Amazon, Acción`.

> **Regla 6 de `CLAUDE.md`:** el Excel es una foto del 4/5-nov (A4 y A5 al 8-nov). Antes de escribir en Odoo, **revalidar cada fila contra Odoo y contra `amz_b2b_invoices`**: estado de la factura, CFDI vivo, cobro aplicado. No confiar en el archivo para decidir.

---

## 3. Criterios de Finanzas (correo de Jero, 8 y 9-nov-2026)

**Business:**
- El 100 % de la facturación de órdenes Business la hace Amazon. **Si Amazon no emite CFDI, nosotros tampoco facturamos.**
- **A7:** cancelar nuestras facturas (individuales y nominativas). Si la orden tiene CFDI de Amazon, vincularlo.
  - Las **nominativas** dependen de que el receptor acepte la cancelación.
  - 32 tienen **cobro aplicado**: Finanzas cancela el cobro antes (acordado en 2.4: "los cancelamos nosotros y vos los reaplicás").
- **A8:** la global **no se cancela**. Se emite una **NC parcial relacionada con cada global** por las órdenes Business que tienen CFDI de Amazon. Las que no tienen CFDI de Amazon **quedan en la global**.
- **A9:** registrar el CFDI de Amazon **con su fecha**.
- **A6 (oct–nov):** cancelar las 75 a público general del flujo y las 7 del portal. Vincular las 8 con CFDI de Amazon pendientes.
- Por el volumen, Finanzas prefiere que **la cancelación, las NC y la vinculación las ejecute un proceso, no a mano**, con validación de Jero.

**Histórico (Parte B), hasta el 31-oct:**

| Categoría (base 14) | Criterio de Finanzas |
|---|---|
| SO confirmada sin factura | Se factura **con fecha del día y sin cobros**, excluyendo las Business. El asiento lo reclasifica Finanzas |
| SO cancelada y devuelta en Amazon | Factura y después NC, igual que el flujo actual (la NC sale del flujo de devoluciones, que todavía no existe) |
| B2B con CFDI de Amazon | Se registra con la fecha del CFDI (incluye A9) |
| SO en borrador | Se confirma y se factura |
| Otros | Los revisa Finanzas con la base |
| Sin SO (FBA) | Hay que crear la SO, por Yuju o por el flujo. **Regla del repo: nunca creamos SO; esto es de Yuju** |
| Odoo 15 | Se define en una llamada con Jero. **Fuera de alcance** hasta entonces |

**Periodos:** Jero pidió reabrir los periodos y asentar cada factura en su fecha ("nosotros después peinamos la contabilidad").
- Esto aplica a lo que **conserva su fecha**: CFDI de Amazon (B2B) y NC parciales, si así se decide.
- Las B2C que timbramos van con fecha del día, porque el SAT no timbra con más de 72 h de antigüedad.

---

## 4. Parte A — Barrido Business

### 4.1 Orden de ejecución

```
1. Revalidar (solo lectura): cada fila de A6–A9 contra Odoo y amz_b2b_invoices.
   Salida: CSV con la acción a ejecutar y la razón. Jero lo valida.
2. Finanzas cancela los cobros aplicados a las facturas a cancelar
   (A6 "Cobro PAMZ", A7 "Cobro aplicado" = True). Sin eso no se puede cancelar.
3. Cancelar (A6 y A7): motivo 02, en dos pasadas (solicitar → confirmar con try_sat).
4. Vincular el CFDI de Amazon a las que lo tienen (A6, A7 con UUID, A9).
5. NC parcial a las globales (A8).
6. Reporte final por fila + lista de cobros a reaplicar (los de 03 quedan en
   NO_INVOICE y se aplican solos cuando la orden pasa a ODOO_B2B_LINKED).
```

### 4.2 Cancelación (A6, A7)

- **Motivo 02, no 01.** El motivo 01 exige un CFDI **nuestro** que sustituya al cancelado (relación 04). Aquí el comprobante que vale es el de Amazon, de otro emisor, así que no hay sustitución posible: lo correcto es 02. (Finanzas pidió sustitución solo para **reemisiones nuestras**, A1.)
- Cancelar **siempre desde Odoo** (`button_request_cancel` + `l10n_mx_edi_cfdi_try_sat`), nunca desde el portal del SAT.
  - **Hallazgo A5:** seis facturas canceladas en el SAT por fuera quedaron con el asiento vivo.
  - En cambio, la nominativa INV/2026/159589, cancelada por el flujo de Odoo, se canceló sola al aceptar el receptor.
  - El cron de Odoo parece procesar solo las facturas en `cancel_requested`.
- Después de `try_sat`, **leer el estado del asiento**. Si el SAT dice cancelado y el asiento sigue `posted`, se reporta (no se fuerza).
- **Nominativas:** quedan en `cancel_requested` hasta que el receptor acepta. El script se puede volver a correr; lo pendiente se reporta.
- **Control de 72 h:** no aplica. No se emite nada nuestro en reemplazo.
- **Antes de cancelar:** verificar que la factura no tenga pagos conciliados. Si los tiene, **no cancelar** y reportar ("Finanzas debe cancelar el cobro primero").

### 4.3 Vinculación del CFDI de Amazon (A6, A7 con UUID, A9)

**Opción recomendada:** usar el loader de 01, no reescribirlo.
- Las órdenes de **oct–nov** ya tienen fila en `mkp_billing_prod`. Al cancelar nuestra factura:
  - la `B2B_FACTURADA_PG` se vincula sola (`reabrir_b2b_pg`);
  - las `ODOO_INVOICED` / `ALREADY_ODOO_INVOICED` de una Business necesitan pasar a `PENDING_B2B_LINK`, porque `ODOO_INVOICED` es final y el miner no la reabre. Una sentencia `UPDATE` acotada por lista, con autorización.
- Las **históricas (A7, A9)** no tienen fila. Correr el miner de 01 con ventana `start_date`/`end_date` del mes y `cobros=false` las siembra:
  - las Business con CFDI quedan en `PENDING_B2B_LINK`;
  - si la SO aún tiene nuestra factura viva, quedan en `B2B_FACTURADA_PG`.
  - Ojo: la misma corrida siembra también las B2C del mes; ver la Parte B, que conviene hacer en la misma pasada.
- La factura B2B lleva `invoice_date` = fecha del CFDI. **Si el periodo está cerrado (lock date), Odoo rechaza la publicación:** Finanzas tiene que reabrir los periodos antes.
- Varios CFDI por orden (A9 trae `;`): `armar_partes_split()` crea una factura por CFDI. Si no puede repartir, deja `B2B_SPLIT_MISMATCH` y se revisa a mano.

### 4.4 NC parcial a globales (A8) — desarrollo nuevo

Es lo único que no existe. Diseño propuesto (validar con Jero antes de escribir):
- **Una NC por global** (agrupando las órdenes Business de esa global), no una por orden: menos CFDI y la relación 01 apunta a un solo UUID. Confirmar con Finanzas.
- **Documento:**
  - `move_type='out_refund'`, partner público general 13436 (el de la global), diario de la global;
  - `reversed_entry_id` = la global;
  - `l10n_mx_edi_cfdi_origin = '01|<UUID de la global>'`.
- **Fiscales:** uso del CFDI G02, forma de pago la de la global.
- **Fecha:** del día. Es un CFDI nuestro y el SAT no timbra con más de 72 h de antigüedad, así que no puede llevar la fecha de la global. Finanzas reclasifica si lo necesita.
- **Líneas:** las líneas de la global ligadas a las SO de esas órdenes (`sale_line_ids`), mismo producto, cantidad y precio, para que el importe sea exactamente lo que la global facturó de esa SO. **No** usar el total de Amazon: la NC revierte lo que facturamos nosotros.
- **Timbrar** (es CFDI nuestro de egreso).
- **Idempotencia:** `ref = 'AUTONC:GLOBAL:<factura global>:<lote>'`, revalidando antes de crear (FAIL-CLOSED).
- **Preguntas abiertas antes de codificar:**
  1. **Cuenta de ingresos de la NC:** ¿401 (cancelación de la venta) o 402 (devoluciones y bonificaciones)? Para Finanzas no es una devolución.
  2. **Costo:** la global ya llevó costo de esas SO. ¿La NC debe revertir el costo? Si la NC está ligada a `sale_line_ids`, Odoo puede generar asientos de inventario; probar en el espejo.
  3. **Pagos:** la global suele estar "pagada parcialmente" (Odoo 15/18). La NC reduce su saldo; si la global ya está pagada, la NC queda con saldo a favor. ¿Cómo lo quiere Contabilidad?
  4. **Después de la NC**, ¿01 ve la SO como "sin factura viva"? `facturas_existentes()` + `netear_con_nc()` netean factura contra NC **por SO**. Con una global de cientos de SO y una NC parcial hay que verificar que la SO quede libre para vincular el CFDI de Amazon, y que no deje libres a las demás SO de la global.
- **Odoo 15:** A8 incluye globales de Odoo 15 (columna `Odoo`). No hay credenciales: solo Odoo 18 hasta definirlo.

### 4.5 Estados sugeridos

Para el control del barrido, en un CSV de salida por corrida o en `raw_json` de `mkp_billing_prod`. **No crear tablas nuevas.**

`REVALIDADA`, `CANCELACION_SOLICITADA`, `CANCELADA`, `CANCELACION_RECHAZADA`, `CON_COBRO` (no se cancela), `VINCULADA`, `NC_EMITIDA`, `SIN_CAMBIO` (ya estaba bien), `ERROR`.

---

## 5. Parte B — Facturación histórica (ene–oct, sin cobros)

### 5.1 Recomendación

**No escribir un script nuevo para facturar: correr el flujo actual con ventana y `cobros=false`, mes por mes, solo Odoo 18.** El loader de 01 ya implementa casi todos los criterios de Jero:

| Criterio de Finanzas | Lo que hace 01 hoy |
|---|---|
| SO confirmada sin factura → fecha del día, sin cobros | Fuera del cierre de mes, la B2C de un mes anterior sale con fecha del día y la avisa en "Venta mes anterior fuera cierre". `cobros=false` no corre 02/03 |
| Excluir las Business | `B2B_SIN_CFDI` (con `fix/invoice-amazon` fusionada) |
| B2B con CFDI → fecha del CFDI | Camino B2B de `procesar()` |
| SO en borrador → confirmar y facturar | `confirmar_so()` |
| SO cancelada → factura (la NC sale después) | Se factura igual (regla del 28-oct) |
| SO ya facturada (incluida global) | `ALREADY_ODOO_INVOICED` |
| Sin SO | `NO_SO_IN_ODOO` (alerta; Yuju) |

Ventajas: los cinco blindajes, idempotencia `AUTOINV:<SO>` compartida con el 1a1, conciliación de la 205.06.003, un solo código que ya pasó por producción.

### 5.2 Alcance por grupo (base 14, versión 23:01)

| Grupo | Órdenes | Qué hacer |
|---|---|---|
| Odoo 18, SO confirmada sin factura | 1,017 · $2.24M | Flujo con ventana |
| Odoo 18, SO en borrador | 62 · $0.16M | Flujo con ventana |
| Odoo 18, SO cancelada (y cancelada devuelta) | 12 + 510 · $1.89M | Flujo con ventana (factura); la NC queda para el flujo de devoluciones |
| Odoo 18, B2B con CFDI de Amazon | 14 | Flujo con ventana (vincula con la fecha del CFDI) |
| Odoo 18, otros (SO sin nada a facturar) | 60 | Revisar con Finanzas antes |
| Odoo 15 (ene – 16-abr) | 2,708 + 195 + 29 + … | **Fuera.** El estado de Odoo 15 en la réplica está congelado al 16-abr: 1,603 de las 2,708 son del 1 al 16 de abril y pueden haberse facturado después. Llamada con Jero |
| Sin SO en Odoo (FBA) | 270 | Yuju |

### 5.3 Cómo correrlo

1. **Prerrequisitos:**
   - `fix/invoice-amazon` fusionada;
   - Finanzas reabrió los periodos (para la fecha de los CFDI B2B);
   - Jero validó la base.
2. **Probar primero en el espejo** (`AMZ_ENTORNO=staging`, `ODOO_TEST_URL`/`ODOO_TEST_DB` de una copia fresca), con un mes y `max_facturas` bajo.
3. **Producción:** ejecución manual del mismo flujo `amz_billing_payments_flow` en Kestra. Así la exclusión mutua cubre la corrida.
   - Inputs:
     - `start_date=2026-04-17`, `end_date=2026-05-01`, luego mes por mes hasta `2026-10-01` (exclusivo; octubre ya está cerrado por el flujo);
     - `cobros=false`;
     - `max_facturas` para el primer mes.
   - Elegir un hueco del horario (ver `CLAUDE.md` §5). Una corrida que se traslape con la de 02:30 o 15:00 hace fallar la segunda (FAIL); no relanzar a mano.
4. **Nunca en los días 1 a 3 del mes.** En el cierre, `fecha_factura()` pondría fecha del último día del mes anterior a ventas de cualquier mes previo.
5. **Validar cada mes:** conteos por estado de `mkp_billing_prod` contra la base 14 (Odoo 18) y reporte para Finanzas (`09_reporte_lote_finanzas.py`, adaptar al rango).

### 5.4 Riesgos a revisar antes de correr

- **El miner une Amazon → SO con `odoo_master_txns_c`.** Para Odoo 18 está bien; para Odoo 15 está incompleta. Con ventana desde el 17-abr no debería pesar, pero comparar contra la base 14 (que usa `odoo_new_sale_order_live`).
- **Slack:** cada B2C de mes anterior se reporta en "Venta mes anterior fuera cierre". Con cientos por corrida, el mensaje solo lista 15; esperado.
- **Grace:** irrelevante (ventas viejas).
- **`DEFERRED`:** fuera del cierre no se leen; en ventas viejas todo está liberado.
- **Cobros de esas ventas:** quedan sin aplicar (Finanzas cierra los históricos). 02 no mina settlements anteriores a `AMZ_PAYMENTS_DESDE` (16-oct).

---

## 6. Reglas del repo que aplican

- Scripts nuevos solo en `test/.../amazon/` (por ejemplo, `15_barrido_business.py`). Pasan a `processes/` solo si van a Kestra; un barrido único no va.
- **Modo en seco por defecto** (`--aplicar` explícito), `--limite N` y CSV de salida por corrida, igual que 12 y 13.
- Blindajes:
  - revalidar justo antes de escribir (FAIL-CLOSED);
  - idempotencia por `ref`;
  - solo reintentar lecturas;
  - `ejecutar_sin_retorno()` para `reconcile` y `remove_move_reconcile`.
- Correr con el flujo de Kestra sin ejecución en curso: un script fuera de Kestra no está cubierto por la exclusión mutua.
- Lecturas de Odoo prod: las corre Sergio (el clasificador las bloquea desde la sesión).
- Commit/push/PR solo cuando Sergio lo pida.

## 7. Preguntas abiertas para Finanzas

1. NC parcial a la global: ¿una por global o una por orden? ¿Cuenta 401 o 402? ¿Revierte costo?
2. Las 32 facturas de A7 con cobro aplicado: confirmar que Finanzas cancela esos cobros antes del barrido.
3. Nominativas (A6 portal y A7): ¿quién da seguimiento a la aceptación del receptor?
4. Reapertura de periodos: ¿qué meses y hasta cuándo?
5. Parte B, "otros" (60 en Odoo 18): ¿qué hacer con las SO confirmadas sin nada a facturar?
6. Odoo 15: llamada pendiente.
