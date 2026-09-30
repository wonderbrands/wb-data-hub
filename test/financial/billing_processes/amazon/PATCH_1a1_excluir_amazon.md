# Patch: excluir Amazon de `facturacion_automatica_1a1.py`

Amazon pasa a tener su propio proceso (`01_amz_billing.py`). El script 1 a 1 debe dejar de facturarlo, o las dos corridas competirían por las mismas órdenes.

> **No apliques este patch hasta que `01_amz_billing.py` esté validado en pruebas.** El orden correcto es el de la sección "Secuencia de corte" al final. Entre medias hay una ventana en la que Amazon se queda sin facturar por nadie.

---

## El cambio

Archivo: `processes/wonderbrands/financial/billing_processes/odoo/facturacion_automatica_1a1.py`
Función: `filter_and_group_by_team()`, al final (~línea 640)

```python
    teams_dict.pop('Team_Walmart', None)
    teams_dict.pop('Salderos / Facebook', None)

+   # Amazon migro a su propio proceso: test/financial/billing_processes/amazon/01_amz_billing.py
+   # Necesita reglas que el resto de marketplaces NO debe heredar (exclusion
+   # de ordenes B2B y facturacion sin restriccion de OUT).
+   # Se remueve por coincidencia de nombre, igual que account_for_team().
+   for _team in [t for t in teams_dict if 'Amazon' in t]:
+       teams_dict.pop(_team, None)

    return teams_dict
```

Se filtra por coincidencia (`'Amazon' in t`) y no por nombre exacto porque `account_for_team()` ya usa ese criterio — si existieran `Team_Amazon` y `Team_Amazon_FBA`, ambos quedan cubiertos.

**Verificar antes de aplicar** cuáles son los nombres reales:

```python
models.execute_kw(db, uid, pwd, 'crm.team', 'search_read',
                  [[('name', 'ilike', 'Amazon')]], {'fields': ['id', 'name']})
```

---

## Lo que NO se toca

`context.accounts['amazon']` y la rama `'Amazon' in team_name` de `account_for_team()` se quedan como están. Cuestan nada y evitan un `KeyError` si algo vuelve a entrar por ahí.

---

## Por qué el prefijo de idempotencia es compartido

`01_amz_billing.py` escribe `ref = 'AUTOINV:<SO>'` — el **mismo** prefijo que el 1 a 1.

Es deliberado. Durante la transición los dos scripts pueden llegar a ver la misma orden, y `count_live_invoices()` de ambos busca por `invoice_origin` **o** por `ref`. Comparten la llave, así que cada uno ve las facturas del otro y ninguno puede duplicar.

Si le pusiéramos un prefijo distinto (`AMZINV:`), cada script sería ciego a las facturas del otro. Sería exactamente el escenario del 26-jul: dos procesos con snapshots independientes que no se ven.

---

## Secuencia de corte

Sin traslape y sin hueco:

1. **Crear las tablas** — `DDL_amz_tables.sql`.
2. **Verificar el diario `AMZ`** — que exista y apunte a `102.02.008`. `03_loader_amz_payments.py` aborta si no.
3. **Correr `01_amz_billing.py` con `TEST_ORDER_LIMIT = 5`**, con el 1 a 1 todavía facturando Amazon. Las 5 órdenes de prueba deben salir como `IGNORED_DUPLICATE` si el 1 a 1 ya las tomó — eso **prueba que la idempotencia compartida funciona**. Si alguna se factura dos veces, parar todo.
4. **Definir `AMZ_START_DATE`** con Finanzas. Marca desde cuándo aplica la facturación sin restricción de OUT. Órdenes anteriores conservan el tratamiento actual.
5. **Aplicar este patch** y desplegar el 1 a 1 sin Amazon.
6. **Activar `01_amz_billing.py` en Kestra** con `concurrency: limit 1, behavior: FAIL`. No es opcional.
7. **Quitar `TEST_ORDER_LIMIT`** (ponerlo en `None`) una vez validado.

Entre los pasos 5 y 6 nadie factura Amazon. Que sea el mismo despliegue.

---

## Riesgo abierto: el backlog del cambio de regla

En el momento en que se factura sin restricción de OUT, todas las órdenes de Amazon que hoy quedan fuera se vuelven elegibles **de golpe**.

`AMZ_START_DATE` es la contención: acota el universo a órdenes recientes. Pero conviene medir el volumen antes de correr en serio:

```python
# Cuántas órdenes de Amazon se volverían facturables
domain = [('team_id', 'in', team_ids),
          ('state', '=', 'sale'),
          ('invoice_status', '=', 'to invoice'),
          ('date_order', '>=', '2026-08-01')]
models.execute_kw(db, uid, pwd, 'sale.order', 'search_count', [domain])
```

Si el número sorprende, subir `AMZ_START_DATE` antes de la primera corrida real.
