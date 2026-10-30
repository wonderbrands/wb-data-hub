# Activar Amazon en producción

## Scripts

| Script | Qué hace | Cuándo |
|---|---|---|
| `_amz_common.py` | Configuración. `AMZ_ENTORNO=prod` cambia tablas `_prod` + Odoo producción + timbrado | — |
| `00_amz_precheck_prod.py` | Verifica cuentas, diario, impuesto, C-ENVIO, tablas y token. **Solo lectura** | Al inicio de cada corrida |
| `01_amz_billing.py` | Facturación B2C / vinculación B2B | Diario |
| `02_miner_amz_payments.py` | Lee cobros liberados → `finance.mkp_payments_prod` | Diario, después de 01 |
| `03_loader_amz_payments.py` | Aplica cobros y promociones en Odoo | Diario, después de 02 |
| `11_ajuste_cogs_205.py` | Cierre mensual de la 205.06.003 + `reporte_kits_<mes>.csv` | Día 1, mes anterior |

Amazon ya está fuera del 1a1 en `main` (commit `330073e`).

## Flujos de Kestra (`flows/wonderbrands/financial/amazon/`)

| Flujo | Qué corre | Cron |
|---|---|---|
| `amz_billing_payments_flow` | 00 → 01 → 02 → 03 | 06:30 y 18:30 CDMX, **apagado** |
| `amz_cierre_205_flow` | 11 + correo con el reporte de kits | Día 1, 09:00 CDMX, **apagado** |

Inputs de `amz_billing_payments_flow`:

| Input | Default | Para qué |
|---|---|---|
| `settlement_id` | vacío | Barrido de un settlement completo |
| `start_date` / `end_date` | `2026-10-01` / vacío | Ventana por fechas (saneamiento) |
| `max_facturas` | vacío | **Tope de facturas creadas en la corrida** |
| `payments_batch` | `2000` | **Tope de cobros aplicados en la corrida** |
| `cobros` | `true` | `false` = solo facturación |

Un solo flujo para todo, para que `concurrency: FAIL` impida dos corridas a la vez.

## Encendido

1. **Merge a `main`** (Kestra clona `main`) y cargar los 2 flujos en Kestra.
2. **Primera corrida limitada:** ejecutar a mano con `settlement_id=27702107971`, `max_facturas=10` y `payments_batch=10`.
   - Revisar en Odoo las 10 facturas: timbre, cuentas y 205.06.003.
   - Revisar los 10 cobros: 102.02.008, promociones y redondeo.
   - Volver a correr el mismo settlement subiendo los topes (50, 200, sin tope) hasta terminarlo. Lo ya hecho no se repite.
3. **Resto del barrido de octubre:** `27766362821`, `27819290821` y `27868960051` cuando se deposite, uno por uno. Finanzas revisa el cuadre de cada uno (`test/.../amazon/09_reporte_lote_finanzas.py`).
4. **Encender el cron diario:** `disabled: false` en `corrida_diaria`.
5. **Saneamiento ene–sep, solo facturación:** ejecuciones manuales mes por mes con `start_date` / `end_date`, `cobros=false` y `max_facturas` como tope. Los cobros históricos los cierra Contabilidad.
6. **Cierre mensual:** primero a mano con `seco=true`; si se ve bien, sin seco, y después encender `cierre_mensual`.

Recomendado: índices en `somos_reyes.amz_transactions (order_id)`, `(settlement_id)` y `(description, transaction_status, posted_date)`.

## Para detener

Apagar el cron. Volver a correr no duplica nada: las facturas llevan la clave `AUTOINV:<SO>` y se vuelve a revisar Odoo antes de crear cada una.
