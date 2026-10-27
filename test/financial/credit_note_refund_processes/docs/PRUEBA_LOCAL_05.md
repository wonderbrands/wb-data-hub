# Prueba local del 05 → tablas `*_staging`

Script: `test/financial/credit_note_refund_processes/05_miner_ml_credit_notes.py`

| Qué toca | Cómo |
|----------|------|
| **Escribe** | Solo `finance.mkp_credit_notes_scan_staging` (la **cola**: qué órdenes revisar y cuándo) y `finance.mkp_credit_notes_staging` (las **NC** encontradas). |
| **Lee** | `finance.mkp_billing_prod`, `somos_reyes.tokens` y las tablas de señales de `somos_reyes`. |
| **API ML** | Solo `GET` al endpoint de devolución. |
| **Odoo** | No lo toca. |

Las credenciales se toman solas de `C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wonderbrands\.env`.

---

## Cómo se configura una corrida

Todo se controla editando el bloque **`PRUEBA LOCAL`** al inicio del script y ejecutándolo (botón Run del IDE, o `python test\financial\credit_note_refund_processes\05_miner_ml_credit_notes.py` desde la raíz del repo):

```python
LOCAL_RUN               = "signals"     # preflight | orders | signals | sweep | both
LOCAL_ORDER_IDS         = "2000018238217954,2000016723575980,2000016949680478,2000017527328146"
LOCAL_MAX_CALLS_PER_RUN = 200
```

| `LOCAL_RUN` | Llena la cola con… | Consulta en ML… | Escribe |
|-------------|--------------------|-----------------|---------|
| `preflight` | nada | 1 orden de prueba (la primera de `LOCAL_ORDER_IDS`) | **nada** |
| `orders` | solo las órdenes de `LOCAL_ORDER_IDS` | solo esas órdenes | sí |
| `signals` | órdenes facturadas **con señal de reembolso** | solo las de señal que ya toca revisar | sí |
| `sweep` | **todas** las órdenes facturadas (~94.5k) | las que ya toca revisar, señal o no | sí |
| `both` | señales + todas | primero las de señal, luego el resto | sí |

- **`LOCAL_ORDER_IDS`** solo se usa en `orders` (y la primera orden, en `preflight`). En los demás modos se ignora, aunque tenga valores.
- **`LOCAL_MAX_CALLS_PER_RUN`** limita cuántas órdenes se **consultan en ML** en la corrida. Llenar la cola no cuenta para este tope: en `sweep` se encolan las ~94.5k órdenes, pero solo se consultan 200.
- **"Ya toca revisar"** significa `next_check_at <= ahora` en la cola. Una orden recién encolada por señal toca de inmediato. Una encolada por barrido toca a los 15 días de facturada (todo el histórico de junio a septiembre toca ya).
- El primer renglón del log confirma el modo real. Ejemplo: `Modo: signals | tope de consultas a ML: 200 | ...`. Si dice `orders (prueba dirigida...)`, estás en prueba dirigida.

### ¿Qué es una "señal"?

Es evidencia, en nuestras tablas de `somos_reyes`, de que la orden tuvo un reembolso. Sirve para **priorizar** qué preguntarle a ML; la NC solo existe si ML la devuelve.

| Señal | Condición |
|-------|-----------|
| `ORDER_CANCELLED` | `ml_order_update.status = 'cancelled'` |
| `PAYMENT_REFUNDED` | `ml_order_payments.refunded_amt > 0` (atrapa también los reembolsos parciales) |
| `CLAIM_COMPLAINANT` | `ml_claims` cerrado a favor del comprador |
| `FIN_REFUND` | `REFUND` / `REFUND_SHIPPING` en `ml_financial_transactions_master` |

El **barrido** (`sweep`) existe porque alguna NC puede no dejar señal en esas tablas. Revisa todas las órdenes facturadas a los 15, 45 y 90 días de facturadas, y después las cierra.

---

## Paso 1 · Preflight (no escribe nada)

```python
LOCAL_RUN = "preflight"
```

Esperado:

- `[ESQUEMA] OK: columnas, largos y llaves únicas`
- `[API] 200 -> NC ... -> se guardaría como PENDING/FULL`
- `=== PREFLIGHT OK — no se escribió nada ===`

El aviso de `sql_mode sin STRICT` es solo informativo: el 05 abre su propia sesión en modo estricto.

---

## Paso 2 · Prueba dirigida (4 órdenes)

```python
LOCAL_RUN       = "orders"
LOCAL_ORDER_IDS = "2000018238217954,2000016723575980,2000016949680478,2000017527328146"
```

| Orden | Esperado |
|-------|----------|
| 2000018238217954 | NC `MELINC-6869` → `PENDING` / `FULL` (confirmada en el preflight). |
| 2000016723575980 | NC a Público en General (según una traza anterior, no confirmada). |
| 2000016949680478 | NC a persona moral (según una traza anterior). |
| 2000017527328146 | Control sin NC → 404 → la cola la deja `WAITING` para mañana. |

Si alguna orden no existe en `mkp_billing_prod`, el log lo avisa (`ONLY_ORDER_IDS sin fila en mkp_billing_prod`) y se omite.

```sql
SELECT mkp_order_id, signal_source, scan_status, check_count, last_http_status, next_check_at
FROM   finance.mkp_credit_notes_scan_staging
WHERE  marketplace = 'MERCADO_LIBRE' ORDER BY id;

SELECT n.mkp_order_id, n.cfdi_uuid, n.related_invoice_uuid, b.cfdi_uuid AS uuid_factura,
       n.serie, n.folio, n.rfc_receptor, n.total_amount, b.total_amount AS total_factura,
       n.fecha_timbrado, n.nc_scope, n.status, n.error_log
FROM   finance.mkp_credit_notes_staging n
JOIN   finance.mkp_billing_prod b ON b.marketplace = n.marketplace AND b.mkp_order_id = n.mkp_order_id
WHERE  n.marketplace = 'MERCADO_LIBRE';
-- Esperado: related_invoice_uuid = uuid_factura, status = PENDING, serie = MELINC

SELECT mkp_order_id, LEFT(CONVERT(FROM_BASE64(xml_data) USING utf8mb4), 400) AS xml_inicio
FROM   finance.mkp_credit_notes_staging WHERE marketplace = 'MERCADO_LIBRE';
```

**Idempotencia:** corre otra vez exactamente igual. El resumen debe decir `NC nuevas: 0 | NC ya conocidas: N` y no debe aparecer ninguna fila nueva.

---

## Paso 3 · Señales, acotado

```python
LOCAL_RUN               = "signals"
LOCAL_MAX_CALLS_PER_RUN = 200
```

Qué pasa:

1. Por cada señal, el script trae los order_id de su tabla (los mismos conteos del preflight: ~9.9k, ~10.2k, ~6.3k, ~3.5k).
2. Encola las que están **facturadas** en `mkp_billing_prod`.
3. Consulta en ML solo 200.

En el log:

```
Órdenes encoladas/promovidas (modo signals): {'ORDER_CANCELLED': '9914 con señal / 7xxx encoladas', 'PAYMENT_REFUNDED': '10208 con señal / 1xxx encoladas', ...}
Modo: signals | tope de consultas a ML: 200 | ...
Órdenes a consultar en esta corrida: 200
Resumen -> Consultadas: 200 | NC nuevas: ... | Sin NC (404): ...
```

- **"con señal"** > **"encoladas"** es normal, por dos razones:
  - una orden que ya entró por una señal anterior no se vuelve a contar;
  - las órdenes con señal que no están facturadas (`NO_INVOICE_IN_ML`, `TOTAL_DIFF`, etc.) no entran.
- **Si vuelves a correr el paso 3,** consulta las **siguientes** 200: las ya revisadas tienen su próxima revisión en el futuro.

```sql
SELECT signal_source, scan_status, last_http_status, COUNT(*)
FROM   finance.mkp_credit_notes_scan_staging
WHERE  marketplace = 'MERCADO_LIBRE'
GROUP  BY signal_source, scan_status, last_http_status;
-- WAITING + last_http_status NULL = encoladas aún sin consultar

SELECT status, nc_scope, COUNT(*), SUM(total_amount)
FROM   finance.mkp_credit_notes_staging
WHERE  marketplace = 'MERCADO_LIBRE'
GROUP  BY status, nc_scope;
```

Revisa también `Errores: ... http={...}` en el resumen. Si aparecen 429 o 403, hay que subir `SLEEP_BETWEEN_CALLS` (0.1 s por defecto) antes del barrido completo.

**Para terminar todas las señales** (~10k órdenes, ~1 h), sube el tope y corre una vez más:

```python
LOCAL_RUN               = "signals"
LOCAL_MAX_CALLS_PER_RUN = 20000
```

---

## Paso 4 · Barrido, acotado

```python
LOCAL_RUN               = "both"
LOCAL_MAX_CALLS_PER_RUN = 200
```

Encola las ~94.5k órdenes facturadas como `SWEEP` (sin consultarlas todavía; las que ya estaban por señal se quedan como están) y consulta 200: primero las de señal pendientes, luego las de barrido.

```sql
SELECT signal_source, scan_status, COUNT(*), MIN(next_check_at), MAX(next_check_at)
FROM   finance.mkp_credit_notes_scan_staging
WHERE  marketplace = 'MERCADO_LIBRE'
GROUP  BY signal_source, scan_status;
-- SWEEP + señales ≈ 94.5k + las 55 ALREADY_ODOO_INVOICED
```

---

## Paso 5 · Barrido histórico completo

```python
LOCAL_RUN               = "both"
LOCAL_MAX_CALLS_PER_RUN = 100000
```

Son ~94.5k consultas × ~0.4 s ≈ **10–11 h**. Cada corrida se corta sola a las 5 h (`MAX_RUNTIME_MINUTES`) y la siguiente retoma donde quedó. Repite hasta que `Órdenes a consultar en esta corrida` sea pequeño (solo las que tocan por calendario).

```sql
SELECT scan_status, COUNT(*) FROM finance.mkp_credit_notes_scan_staging
WHERE marketplace = 'MERCADO_LIBRE' GROUP BY scan_status;

-- Órdenes con más de una NC (para ver cómo las entrega la API)
SELECT mkp_order_id, COUNT(*), GROUP_CONCAT(cfdi_uuid), SUM(total_amount)
FROM finance.mkp_credit_notes_staging
WHERE marketplace = 'MERCADO_LIBRE' GROUP BY mkp_order_id HAVING COUNT(*) > 1;

-- Revisión manual: documentos inválidos y señales vencidas sin NC
SELECT mkp_order_id, signal_source, scan_status, last_http_status, last_checked_at
FROM finance.mkp_credit_notes_scan_staging
WHERE marketplace = 'MERCADO_LIBRE' AND scan_status IN ('INVALID_DOCUMENT', 'NEVER_CREDITED_BY_ML');
```

---

## Reiniciar staging entre pruebas (solo tablas de prueba)

```sql
DELETE FROM finance.mkp_credit_notes_staging      WHERE marketplace = 'MERCADO_LIBRE';
DELETE FROM finance.mkp_credit_notes_scan_staging WHERE marketplace = 'MERCADO_LIBRE';
```

---

## Otros parámetros (rara vez hace falta tocarlos)

Siguen como variables de entorno con default en el script: `SLEEP_BETWEEN_CALLS` (0.1), `MAX_RUNTIME_MINUTES` (300), `SWEEP_CHECK_OFFSETS_DAYS` (15,45,90), `SIGNAL_MAX_DAYS` (45), `SIGNALS` (las 4), `NC_TABLE` / `NC_SCAN_TABLE` (las `*_staging`). Para cambiar alguno en local, edita su valor por defecto en el `os.getenv(...)` del script.
