# Prueba del 06 · Loader de NC → Odoo 18

> **v3 (10-nov), condiciones de Finanzas para el piloto:**
> - **Reverso del cobro en el diario Mercado Pago** (`JOURNAL_CODE_COBRO`, código MP; antes MISC), con la fecha de la NC.
> - **Retorno WH/DEV (solo tipo B):** asiento MISC Debe 205.06.003 / Haber 107.06.001 por el costo del retorno, conciliado contra el retorno (205.06.003) y contra la NC (107.06.001).
>   - Toma solo productos que salieron en la orden, así que los kits casan por componente.
>   - Tope: el costo abierto de la NC en 107.06.001; lo que no vuelve queda abierto ahí.
>   - Fecha: la más reciente entre la NC y el retorno.
>   - `ref MLNC-RET:<UUID>:<retorno>`.
>   - Corre al registrar la NC y en un barrido al final de cada corrida `orders` o `pending`. También existe `LOCAL_RUN = "returns"` para correr solo el barrido.
> - **Reventa activada** (`INCLUDE_RESALE=true`).
> - **Corrida en seco:** `LOCAL_RUN = "preflight"` + `LOCAL_PREFLIGHT_ALL = True` recorre todo el universo, sin importar el estado en staging, y escribe `finanzas/Corrida_en_seco_NC_ML_<odoo>_<fecha>.csv` con el estado que asignaría a cada NC. Si falta la posición fiscal B, el preflight la avisa y sigue.
> - **Lote v3:** 22 casos, todos OK (`finanzas/Lote_prueba_NC_ML_v3_staging_2026-11-10.csv`).
> - **Antes del piloto en productivo:** crear la posición fiscal *ML NC Devolución (tipo B)* y poner 402.01.002 en el diario BON y en BON_SRV.

> **v2 (9-nov), con el feedback de Finanzas.** Las reglas vigentes están en el encabezado del script; prevalecen sobre las secciones de abajo, que describen la v1:
> - **Cuenta por tipo:** A → 402.01.003 (DEV), B → 402.01.001 (DEV), C → 402.01.002 (BON).
> - **Clasificación:** A vs B por la fecha efectiva de la SO; B vs C según si la NC son unidades enteras.
> - **Monto e IVA = XML al centavo.** El IVA se ajusta vía `tax_totals`; ±$0.01 contra la factura → 704.23.001.
> - **Costo:** el de la factura original. En B se manda a 107.06.001 con la posición fiscal *ML NC Devolución (tipo B)* (205.06.003 → 107.06.001), que **hay que crear en productivo**.
> - **Factura cobrada:** asiento MISC Debe 105.01.004 / Haber 102.03.001 (`ref MLNC-COBRO:<UUID>`).
> - **Fecha contable = fecha fiscal.** Si el mes está cerrado → `PERIOD_LOCKED` (no se mueve).
> - **Revisión manual:** sin reembolso ni disputa en MeLi (posible refactura) y Reventa (`INCLUDE_RESALE=false`).
> - **Lote v2:** staging `somosreyes-test-06-nov-2026-41423118`, 18 casos + 1 simulado, todos OK (`finanzas/Lote_prueba_NC_ML_v2_staging_2026-11-09.csv`).

Script: `test/financial/credit_note_refund_processes/06_loader_ml_credit_notes.py`

| Qué toca | Cómo |
|----------|------|
| **Lee** | NC en `PENDING` de `finance.mkp_credit_notes_staging` (las dejó el 05). |
| **Crea en Odoo** | La NC (`out_refund`) ligada a su factura, con el XML de ML adjunto. **No timbra.** |
| **Registra** | El resultado en la misma tabla staging (`status`, `odoo_refund_id`, `odoo_refund_name`, `operation_class`…). |
| **Credenciales** | `wb-data-hub\.env` (`odoo_urlV18` / `odoo_dbV18`, o `odoo_url_testV18` / `odoo_db_testV18`, más `odoo_user_dataV18`). |

## Configuración (bloque `PRUEBA LOCAL` del script)

```python
LOCAL_RUN            = "preflight"   # preflight | orders | pending
LOCAL_ODOO           = "test"        # test | prod   (prod = Odoo productivo REAL)
LOCAL_ORDER_IDS      = "2000018238217954"
LOCAL_MONTH          = ""            # "2026-10" = solo NC timbradas ese mes
LOCAL_MAX_NC_PER_RUN = 10
```

| `LOCAL_RUN` | Qué hace | Escribe |
|-------------|----------|---------|
| `preflight` | Valida Odoo (diarios, cuentas, producto, campos) y muestra, por cada NC, **qué haría**: diario, tipo A/B/C, líneas, si concilia o queda como saldo a favor, o por qué no entra. | **nada** |
| `orders` | Registra solo las NC de `LOCAL_ORDER_IDS`. | sí |
| `pending` | Registra las NC `PENDING`, de la más reciente a la más antigua, filtradas por `LOCAL_MONTH`, hasta `LOCAL_MAX_NC_PER_RUN`. | sí |

## Qué hace con cada NC

1. Busca la factura en Odoo **por el UUID que relaciona la NC**. Si no está, queda en `WAIT_INVOICE_IN_ODOO`; si está cancelada, `INVOICE_CANCELLED_IN_ODOO`; si la SO tiene otra factura, `UUID_MISMATCH`.
2. Revisa duplicados:
   - Si el UUID de la NC ya está en Odoo → `ALREADY_ODOO_CREDITED`.
   - Si la factura ya tiene una NC manual → `MANUAL_REVIEW` (posible doble egreso).
   - Si la suma de NC excede la factura → `AMOUNT_EXCEEDS_INVOICE`.
3. Arma las líneas:
   - **NC total o de una línea completa → diario DEV.** Mismo producto, cantidad y precio que la factura. Es tipo **A** si la SO no tiene OUT `done` y **B** si lo tiene.
   - **NC parcial → diario BON.** Producto `[BON_SRV]` con nombre `BON_SRV-<producto>`, por el subtotal del XML. Tipo **C**.
   - En ambos casos: cuenta 402.01.001, impuesto 38 (→ 208.01.001) y **sin ligar a la SO**.
4. Crea la NC con:
   - `ref = MLNC:<UUID>` (es la llave contra duplicados);
   - `reversed_entry_id = factura`;
   - `l10n_mx_edi_cfdi_origin = 01|<UUID factura>`;
   - en referencia de pago: factura, UUID origen y serie-folio de ML.

   Luego redirige la CxC a la cuenta de la factura, escribe el UUID, adjunta `<UUID>.xml` y la publica.
5. **Factura con saldo** → concilia NC ↔ factura (`ODOO_CREDITED`). **Factura ya cobrada** → la NC queda como saldo a favor en la CxC (`ODOO_CREDITED_PAID`), en espera del registro de la salida de dinero de MP (fase siguiente).
6. Deja una nota en el chatter de la factura y de la SO.

**Fecha contable:** hay bloqueo de periodo al **30-sep-2026**. La NC se crea con `invoice_date` = fecha de la NC (fecha fiscal) y sin forzar la fecha contable. Si el mes está cerrado, Odoo la registra en el primer periodo abierto y el log lo indica.

**Si se corta a la mitad:** vuelve a correr igual. La NC se encuentra por `ref` y se retoma; no se duplica.

---

## Paso 1 · Preflight (ya probado contra productivo, solo lectura)

```python
LOCAL_RUN = "preflight"; LOCAL_ODOO = "prod"; LOCAL_ORDER_IDS = ""; LOCAL_MONTH = "2026-10"; LOCAL_MAX_NC_PER_RUN = 60
```

Resultado del 03-nov: **60 de 60 `READY`**.

## Paso 1b · Prueba con escritura en Odoo staging ✅ (03-nov)

Staging `somosreyes-test-29-10-2026-40886239` (copia del 29-oct; usa `LOCAL_ODOO = "test"`, que lee `ODOO_TEST_URL` / `ODOO_TEST_DB`).

**10 NC registradas** (6 DEV, 4 BON): 6 conciliadas y 4 como saldo a favor. Verificado en todas:

- asiento 402.01.001 / 208.01.001 / 105.01.004;
- UUID y XML adjunto;
- `reversed_entry_id` y `l10n_mx_edi_cfdi_origin`;
- chatter en la factura y en la SO;
- **0 líneas ligadas a la SO**, y la SO no cambió su estado de facturación.

**Idempotencia:** con dos filas devueltas a `PENDING`, la siguiente corrida retomó las mismas NC sin duplicar; una nueva corrida tomó 0 y en Odoo hay una NC por `ref`.

Hallazgos para Finanzas:

- **Costo de venta automático en DEV:** Odoo agrega Debe **205.06.003** / Haber **501.01.001** por el costo (reversa anglosajona del producto). Las BON no lo llevan. Hay que confirmar que 205.06.003 sea "Mercancías facturadas no enviadas". El tipo B debería ir a "Devoluciones en tránsito", cuenta que aún no existe.
- **Centavos:** si la factura en Odoo ya traía un centavo de diferencia contra el CFDI de ML, la NC la hereda. Ejemplo: RDEV/2026/01004 por $2,574.01 contra el XML de $2,574.00.

> Después de probar en staging, las filas de la tabla staging quedan con IDs del Odoo de **staging**. Regrésalas antes de cargar en productivo:
> ```sql
> UPDATE finance.mkp_credit_notes_staging
> SET status='PENDING', error_log=NULL, operation_class=NULL, odoo_so_name=NULL, odoo_invoice_id=NULL,
>     odoo_refund_id=NULL, odoo_refund_name=NULL, invoice_payment_state=NULL, retry_count_loader=0, processed_at=NULL
> WHERE marketplace='MERCADO_LIBRE' AND mkp_order_id IN (...órdenes probadas...);
> ```

## Paso 2 · Canary: 10 NC de octubre (periodo abierto)

Cubren todos los caminos:

| Orden | Caso |
|-------|------|
| 2000019233321980 | DEV · A · factura con saldo → concilia |
| 2000019163763708 | DEV · B · factura con saldo → concilia |
| 2000019251065416 | DEV · A · cantidad 2 → concilia |
| 2000018911435118 | DEV · B · factura pagada → saldo a favor |
| 2000019244126292 | DEV · A · factura pagada → saldo a favor |
| 2000019206671526 | BON · C · factura pagada → saldo a favor |
| 2000019154365274 | BON · C · monto chico, factura pagada |
| 2000019207252706 | BON · C · factura parcialmente cobrada → concilia |
| 2000019109402410 | BON · C · factura con saldo de $0.02 → concilia $0.02, el resto a favor |
| 2000019052369356 | DEV · B · factura con saldo → concilia |

Sugerencia: primero **1 sola** NC (la primera) y se revisa en Odoo; después las otras 9.

```python
LOCAL_RUN = "orders"; LOCAL_ODOO = "prod"; LOCAL_ORDER_IDS = "2000019233321980"
```

### Qué revisa Finanzas en Odoo por cada NC del canary

- [ ] Diario DEV o BON correcto; nombre de la rectificativa.
- [ ] Asiento: Debe **402.01.001** (subtotal) y **208.01.001** (IVA); Haber **105.01.004** (total).
- [ ] Ligada a la factura original (campo "Reversión de"); referencia de pago con factura, UUID origen y folio ML.
- [ ] XML de ML adjunto; **no** aparece como "por timbrar".
- [ ] Factura con saldo: queda **Revertida** o pagada por la NC. Factura ya cobrada: la NC aparece como saldo a favor del cliente.
- [ ] Nota en el chatter de la factura y de la SO.
- [ ] La SO **no** volvió a "Por facturar".

```sql
SELECT mkp_order_id, status, operation_class, odoo_refund_name, invoice_payment_state, error_log
FROM finance.mkp_credit_notes_staging
WHERE mkp_order_id IN ('2000019233321980','2000019163763708','2000019251065416','2000018911435118','2000019244126292',
                       '2000019206671526','2000019154365274','2000019207252706','2000019109402410','2000019052369356');
```

### Si una NC del canary quedó mal

1. En Odoo, abrir la NC → **Restablecer a borrador** (deshace la conciliación) → **Cancelar**.
2. Regresarla a la cola:
   ```sql
   UPDATE finance.mkp_credit_notes_staging
   SET status = 'PENDING', odoo_refund_id = NULL, odoo_refund_name = NULL, error_log = NULL
   WHERE mkp_order_id = '<orden>';
   ```
   El 06 ignora las NC canceladas en Odoo, así que la siguiente corrida crea una nueva.

## Paso 3 · Histórico mes por mes (después del visto bueno del canary)

```python
LOCAL_RUN = "pending"; LOCAL_ODOO = "prod"; LOCAL_MONTH = "2026-10"; LOCAL_MAX_NC_PER_RUN = 1000
```

Orden: **2026-10 → 2026-11 → 2026-09 → 2026-08 → 2026-07**. Hay visto bueno de Finanzas entre meses. Jul–sep quedan con fecha contable en el periodo abierto (ver "Fecha contable" arriba).

```sql
SELECT DATE_FORMAT(fecha_timbrado,'%Y-%m') mes, status, COUNT(*), SUM(total_amount)
FROM finance.mkp_credit_notes_staging WHERE marketplace = 'MERCADO_LIBRE'
GROUP BY mes, status ORDER BY mes, status;
```
