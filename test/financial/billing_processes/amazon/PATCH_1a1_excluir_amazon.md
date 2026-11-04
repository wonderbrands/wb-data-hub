# Corte: Amazon sale del 1a1 y pasa a `01_amz_billing.py`

**Estado (2-oct-2026):** cambio de código listo en el PR [wonderbrands/wb-data-hub#5](https://github.com/wonderbrands/wb-data-hub/pull/5), **sin mergear**. El filtro `marketplace` de ML, que es prerrequisito, está en el PR [wonderbrands/wb-data-hub#4](https://github.com/wonderbrands/wb-data-hub/pull/4).

Decisión del Tech Lead: Amazon sale del 1a1 desde el **2-oct-2026**. Las órdenes de octubre se facturan con el proceso nuevo dentro del mes.

---

## El cambio (PR #5)

`processes/wonderbrands/financial/billing_processes/odoo/facturacion_automatica_1a1.py`, en `filter_and_group_by_team()`:

```python
    teams_dict.pop('Team_Walmart', None)
    teams_dict.pop('Salderos / Facebook', None)

    for _team in [t for t in teams_dict if 'Amazon' in t]:
        teams_dict.pop(_team, None)
```

El equipo en Odoo es `Team_Amazon`. Se filtra por coincidencia de nombre, el mismo criterio que `account_for_team()`, para cubrir cualquier variante.

`context.accounts['amazon']` y la rama `'Amazon' in team_name` de `account_for_team()` no se tocan: no cuestan nada y evitan un `KeyError` si algo vuelve a entrar.

---

## Reglas del corte

**R1. El 1a1 deja de facturar todo Amazon, de cualquier fecha.** No se usa un corte por fecha como `cutoff_ml`: el 1a1 filtra por `date_order` y `01` por `posted_date`, así que una fecha deja una franja de órdenes visible para los dos flujos. Son flujos de Kestra distintos y su `concurrency: limit 1` no los separa entre sí; es el patrón del incidente del 26-jul.

**R2. `01` factura desde `AMZ_START_DATE` (`posted_date`, hora CDMX), sin `AMZ_END_DATE`.** Es la única fecha de corte. Propuesta: `2026-04-17`, el arranque de Odoo 18 en producción, para que el barrido histórico cubra lo que el 1a1 dejó sin facturar.

Rezago medido por el Tech Lead, SO de Amazon pendientes de factura en Odoo:

| Mes | abr | may | jun | jul | ago | sep | **Total** |
|---|---|---|---|---|---|---|---|
| SO | 135 | 190 | 436 | 128 | 126 | 73 | **1,088** |

**R3. Lo que el 1a1 ya facturó dentro de la ventana no se toca.** `01` lo ve como `ALREADY_ODOO_INVOICED` por la idempotencia compartida (`ref = AUTOINV:<SO>` + `invoice_origin`). Esto incluye las B2B que el 1a1 facturó a público en general cuando Amazon ya había emitido el CFDI (doble CFDI): `01` **no** las corrige. Se identifican con `000_historical_billing_back_analysis.py` y su corrección es decisión de Finanzas.

**R4. Lo anterior a `AMZ_START_DATE` sin factura no lo factura nadie.** Con `AMZ_START_DATE = 2026-04-17` el universo queda cubierto desde el arranque de Odoo 18.

**R5. Prerrequisitos en producción:**

| Prerrequisito | Estado |
|---|---|
| Filtro `marketplace` en los 4 scripts de ML | PR #4, sin mergear |
| `status` de `mkp_*_prod` en `VARCHAR(40)` | ✅ |
| Diario *Amazon Settlements* → `102.02.008`, con la configuración de "Pagado" | Lo crea Finanzas en producción |
| Índices MySQL de las fuentes de Amazon | Pendiente (ver abajo) |
| Flujos de Kestra de Amazon (facturación y cobros) | Pendiente |
| Nombre del equipo en Odoo | ✅ `Team_Amazon` |
| Tope de arranque `AMZ_PROD_MAX_FACTURAS` | ✅ en `01` |

**R6. Secuencia:**

1. Mergear el PR #4 (ML). No cambia el comportamiento de ML.
2. Esperar a que no haya un 1a1 corriendo y mergear el PR #5. Desde la siguiente ejecución del 1a1, nadie factura Amazon hasta el paso 4.
3. Cargar los flujos de Kestra de Amazon en un horario que **nunca** se cruce con el del 1a1.
4. Primera corrida de `01` en producción con `AMZ_PROD_MAX_FACTURAS` (por ejemplo 50) y revisión manual. Después, el barrido por tramos.

**R7. Controles después de cada corrida:**

- La query de doble CFDI devuelve 0: ninguna orden con CFDI de Amazon Business facturada como B2C por `01`.
- Ninguna SO con más de una factura viva sin desglose B2B.

**R8. Reversa:** pausar el flujo de Amazon y revertir el commit del PR #5. La idempotencia compartida evita que el 1a1 duplique lo que `01` haya facturado.

---

## Índices pendientes

La query del lote (`QUERY_LOTE`) tarda más de 4 min. En Kestra eso es un timeout esperando a pasar. Antes de crearlos hay que ver qué existe (`SHOW INDEX FROM ...`):

```sql
somos_reyes.amz_transactions       (description, transaction_status, posted_date)
somos_reyes.amz_transactions       (shipment_id, posted_date)
somos_reyes.amz_transactions       (order_id)
somos_reyes.amz_transaction_items  (transaction_id)
somos_reyes.amz_b2b_invoices       (order_id, receipt_type)
somos_reyes.amazon_orders          (amazonorderid)
somos_reyes.odoo_master_txns_c     (marketplace_order_id)
somos_reyes.amz_settlement_report  (settlement_id)
```
