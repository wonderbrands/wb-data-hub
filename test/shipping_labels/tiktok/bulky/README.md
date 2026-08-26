# TikTok Bulky – Guías automáticas

Automatiza la 2ª modalidad de envío de TikTok Shop ("Tiendas Bulky"): TikTok no
entrega la guía, la generamos nosotros con las paqueteras en convenio.

Arquitectura estructurada/funcional, igual que Amazon y Coppel. Sin clases, sin
el paquete POO de `44_TikTok_fulfillment`.

## Archivos

| Archivo | Rol |
|---|---|
| `tiktok_bulky_fulfillment.py` | Script principal. Único punto de entrada. |
| `tiktok_bulky_config.py` | Config unificado: lee el `.env`, define tiendas, URLs, reglas y mapas. |
| `.env.example` | Plantilla de variables. Copiar a `.env`. |

Del módulo compartido `_shared/_00_shipping_labels_db.py` sólo reutiliza la
normalización del tracking a JSON (la ruta la resuelve el config buscando en
`test/` y en `processes/`). El `INSERT` no: ver *Un registro por orden* abajo.

## Flujo

```
por tienda (Neon / KingsHouse / ColorDreams Home)
 ├─ token vigente de somos_reyes.tiktok_shop_tokens (refresh si vence en <1h)
 ├─ /order/202309/orders/search  → AWAITING_SHIPMENT de las últimas 72 h
 ├─ filtro Bulky: shipping_type == 'SELLER'
 ├─ descarta las que ya tienen guía en tools.shipping_labels (anti-duplicados)
 └─ por orden
     ├─ valida SKUs, valor de orden y CP del destinatario
     ├─ busca la sale.order en Odoo (channel_order_reference / channel_order_id)
     ├─ obtiene (o crea) el package_id en TikTok
     ├─ POST /live-rates      → mejor servicio que cubra TODAS las cajas
     ├─ REGLA 21%: costo/valor > 0.21 → manual
     ├─ resuelve el shipping_provider_id de TikTok (ANTES de gastar la guía)
     ├─ POST /generate-label  → una guía por caja (ZPL → PDF vía Labelary)
     ├─ Odoo: tracking + carrier + PDF adjunto + mensaje en el chatter
     ├─ TikTok: reporta la guía (tracking + shipping_provider_id)
     ├─ MySQL: UPSERT en tools.shipping_labels (uno por SKU)
     └─ GSheets: Guias_automaticas_generadas
```

Cualquier check que falle corta el flujo, registra la fila en
`Guias_automaticas_manuales` y deja el registro con `label_generated = 0`.

## Un registro por orden (no bitácora)

Tanto en `tools.shipping_labels` como en las dos pestañas, **cada orden ocupa
una sola fila que se actualiza en cada corrida**; no se acumula un renglón por
intento como en Coppel.

- **BD**: llave lógica `(marketplace, marketplace_id, sku)`. Si la fila existe
  se hace `UPDATE`; si no, `INSERT`. `label_generated_at` nunca se borra: una
  actualización posterior conserva la fecha original de la guía.
- **Sheets**: llave `ID TikTok` (columna D). El script indexa la pestaña con
  una sola lectura al arrancar y luego actualiza la fila correspondiente.
- **`Attemps`** (pestaña de manuales) conserva el "cuántas veces falló": se lee
  del sheet y se incrementa en cada corrida.
- Cuando una orden que estaba en manuales finalmente se resuelve, su fila pasa
  a `RESUELTA_AUTOMATICAMENTE` en vez de quedar contradiciendo a la pestaña de
  generadas.
- El `carrier` y el `carrier_service_level` se guardan **aunque no se genere la
  guía**, siempre que la cotización haya devuelto tarifa (p. ej. costo > 21 %):
  es justo el dato que CS necesita para resolver a mano.

Recomendado (no obligatorio, el script hace `SELECT` previo):

```sql
ALTER TABLE tools.shipping_labels
  ADD UNIQUE KEY uq_marketplace_order_sku (marketplace, marketplace_id, sku);
```

Ojo: ese índice aplicaría a Amazon y Coppel, que **sí** insertan una fila por
intento. Créalo sólo si migras esos scripts al mismo criterio.

### Motivos que mandan una orden a revisión manual

| `status` | Cuándo |
|---|---|
| `LIMIT_RATIO_OVERCOME` | El costo de la guía supera el 21 % del valor de la orden. |
| `NO_COVERAGE` | `/live-rates` no devolvió cotizaciones (sin cobertura o SKU sin medidas). |
| `RATES_CONNECTION_ERROR` | Falla de red con la API interna. Se reintenta en la siguiente corrida. |
| `LABEL_GENERATION_FAILED` | Ninguna guía se generó. |
| `PARTIAL_LABELS` | Se generaron menos guías que cajas. **Hay guías emitidas sin reportar.** |
| `ODOO_ORDER_MISSING` | No existe la `sale.order` en Odoo. |
| `TIKTOK_PACKAGE_MISSING` | TikTok no tiene paquete y no se pudo crear. |
| `TIKTOK_PROVIDER_UNRESOLVED` | TikTok no reconoce la paquetería cotizada. **No se genera guía**, para no emitirla sin poder reportarla. |
| `ADDRESS_INCOMPLETE` / `ORDER_WITHOUT_SKUS` / `ORDER_VALUE_ZERO` | Datos insuficientes. |
| `POST_LABEL_UPDATE_FAILED` | Guía generada, pero falló la actualización en Odoo o TikTok. |
| `UNEXPECTED_ERROR` | Excepción no contemplada. |
| `DRY_RUN` | Simulación: la orden pasó todas las validaciones pero no se generó guía. |

### Columnas de las pestañas

- **`Guias_automaticas_generadas`** (12): `Time-stamp, Seller Name, Order Date,
  ID TikTok, ID Odoo, Status, SKU(s), Guías (tracking), Carrier,
  Costo total guia(s), Total orden, Ratio`
- **`Guias_automaticas_manuales`** (13): `Time-stamp, Seller Name, Order date,
  ID TikTok, ID Odoo, Status, Reason, Attemps, SKU(s), Carrier,
  Total cost shipping, Total order, Ratio`

El orden está definido en `SHEET_SUCCESS_HEADERS` / `SHEET_MANUAL_HEADERS` del
config y debe coincidir con las pestañas reales. `SHEET_KEY_COLUMN` se calcula
desde los encabezados, así que mover columnas no rompe el índice.

## Credenciales

Todo se lee de variables de entorno; **el código no contiene secretos**. En
local viven en el `.env` junto al script (o donde apunte `ENV_PATH`); en Kestra
se inyectan con `secret()` en el bloque `env` de la task, igual que el flow de
Coppel.

Se reutilizan los nombres que ya usa el repo:

- **Odoo 18**: `odoo_urlV18`, `odoo_dbV18`, `odoo_user_dataV18`, `odoo_password_dataV18`
- **MySQL**: `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
- **API interna de guías**: `AUTH_USER`, `AUTH_PASS`
- **Google Sheets**: `GOOGLE_CREDS_JSON` (JSON inline o ruta al archivo)

Nuevas, una terna por tienda (`slug` = `KH`, `CDH`, `NEON`):

```
TIKTOK_<SLUG>_ENABLED       # true/false
TIKTOK_<SLUG>_SELLER_NAME   # debe coincidir con tiktok_shop_tokens.seller_name
TIKTOK_<SLUG>_APP_KEY
TIKTOK_<SLUG>_APP_SECRET
TIKTOK_<SLUG>_SHOP_CIPHER
```

Los `access_token` / `refresh_token` **no** van en el `.env`: se leen y
actualizan en `somos_reyes.tiktok_shop_tokens` por `seller_name`.

Para agregar una tienda basta con sumar su entrada a `_SHOP_DEFINITIONS` en
`tiktok_bulky_config.py` y sus 5 variables. Neon ya está declarada y apagada.

## Kestra

Flow: `flows/wonderbrands/shipping_labels/tiktok/tiktok_bulky_labels_pipeline.yml`
(cron `30 7,11,15,19`, desfasado de Coppel para no competir por la API de guías).

**Sólo son secretos las credenciales.** Todo lo demás va en claro dentro del
flow, para poder ajustar reglas sin tocar el `.env` del servidor:

| En el `.env` del servidor (15) | En el flow (26) |
|---|---|
| `AUTH_USER`, `AUTH_PASS` | `DB_HOST`, `DB_USER`, `DB_NAME`, `odoo_urlV18`, `odoo_dbV18` |
| `DB_PASSWORD_KESTRA` | `SPREADSHEET_TIKTOK_ID` |
| `odoo_user_dataV18`, `odoo_password_dataV18` | `LOG_LEVEL`, `TIKTOK_BULKY_DRY_RUN`, `TEST_API_LABELS_SR` |
| `GOOGLE_CREDS_TIKTOK_JSON` | `TIKTOK_BULKY_MAX_ORDERS`, `_LOOKBACK_HOURS`, `_PAGE_SIZE`, `SLEEP_BETWEEN_ORDERS` |
| `TIKTOK_{KH,NEON,CDH}_APP_KEY` | `LIMIT_RATIO_PERCENTAGE`, `ORIGIN_ZIP`, `SAT_BIENES_TRANSP` |
| `TIKTOK_{KH,NEON,CDH}_APP_SECRET` | `TIKTOK_*_ENABLED`, `TIKTOK_*_SELLER_NAME` |
| `TIKTOK_{KH,NEON,CDH}_SHOP_CIPHER` | `TIKTOK_HANDOVER_METHOD`, `_SHIP_STRATEGY`, `_WAREHOUSE_REGION`, `MAP_STATE_TO_CODE` |

Ojo: el secreto de Google se llama `GOOGLE_CREDS_TIKTOK_JSON` (Coppel usa
`SHIPPING_INFO_COPPEL_JSON`); hay que darlo de alta.

`concurrency: limit 1 / CANCEL` no es opcional: dos ejecuciones simultáneas
podrían generar guías duplicadas. El `retry` sí es seguro, porque una orden con
guía queda con `label_generated=1` y se omite al reintentar.

### Logs en Kestra

`LOG_LEVEL=ERROR` silencia el detalle técnico, pero el **resumen operativo usa
su propio logger** (`tiktok_bulky.resumen`) y siempre se ve:

```
TIENDA    | Neon Deportes | 5 bulky pendientes | 5 a procesar en esta corrida
GUIA OK   | Neon Deportes | TikTok 5857187 | SO10250335 | 876279163286 | FEDEX | $205.32 (16.5%)
MANUAL    | Neon Deportes | TikTok 5857188 | SO10250336 | LIMIT_RATIO_OVERCOME | costo 42.1%
RESUMEN   | guías generadas: 4 | manuales: 1 | omitidas: 1 | órdenes vistas: 6 | tiendas con error: 0
```

Los contadores también se publican como outputs de Kestra
(`{{ outputs.process_tiktok_bulky_orders.vars.manual }}`), para condicionar
alertas. El flow trae comentado el bloque de aviso a Slack cuando hay manuales.

Para depurar una corrida basta con cambiar `LOG_LEVEL: "INFO"` en el flow.

## Ejecución local

```bash
cp .env.example .env   # llenar credenciales
```

Simulación (cotiza y valida, no genera guías ni escribe en TikTok/Odoo/BD; sí
registra en la pestaña de manuales para poder revisar el resultado esperado):

```bash
TIKTOK_BULKY_DRY_RUN=true python tiktok_bulky_fulfillment.py
```

Piloto de una orden real:

```bash
TIKTOK_BULKY_MAX_ORDERS=1 python tiktok_bulky_fulfillment.py
```

Corrida normal:

```bash
python tiktok_bulky_fulfillment.py
```

## Dirección del destinatario

TikTok no etiqueta los niveles de `district_info` en inglés ni en un orden
garantizado, así que **no se busca por `address_level_name`**: se ordena por
`address_level` (L0 país, L1 estado, L2 municipio, L3 colonia) y se asigna por
posición, descartando el país.

```
[México, Ciudad de México, Álvaro Obregón, La Angostura]
  -> state='CMX'  city='Álvaro Obregón'  street2='La Angostura'
```

El país se descarta sólo si es el PRIMER nivel: el Estado de México también se
llama "México" y filtrar por nombre en todas las posiciones lo borraría.

`address_line2` suele traer la referencia ("casa", "depto 3") y se anexa a
`street1`, salvo que duplique la colonia.

### Código de estado

Las paqueteras exigen el código de 3 letras y TikTok manda el nombre completo,
así que `MX_STATE_CODES` (en el config) traduce los 32 estados y sus variantes
(`Ciudad de México` → `CMX`, `Estado de México` → `MEX`, `Michoacán de Ocampo`
→ `MICH`, …).

**Este mapeo debería vivir en la API interna de cotización/guías**, no aquí: es
el mismo problema para Amazon, Coppel y cualquier canal futuro, y tenerlo en un
solo lugar evita que cada script arrastre su propio diccionario. Cuando la API
lo absorba, basta poner `MAP_STATE_TO_CODE=false` y se enviará el nombre.

## Pendientes de validar contra las tiendas reales

1. **Endpoint de reporte de guía**: en self-shipment conviven
   `PUT .../packages/{id}/shipping_info` y `POST .../packages/{id}/ship`. Con
   `TIKTOK_SHIP_STRATEGY=auto` (default) se intenta el primero y, si falla, el
   segundo; el log dice cuál funcionó para fijarlo en el `.env`.
2. **`TIKTOK_HANDOVER_METHOD`**: por defecto `DROP_OFF`. Si el shop lo rechaza
   en self-shipment, déjalo vacío y el campo no se envía.
3. **`shipping_type`**: el filtro Bulky asume `'SELLER'`. Confirmado en
   KingsHouse (19 de 19 órdenes).

## Paqueterías de TikTok (shipping_provider_id)

TikTok exige SU id de paquetería al reportar la guía. El catálogo se obtiene con
(`Get Shipping Providers 202309`):

```
GET /logistics/202309/delivery_options/{delivery_option_id}/shipping_providers
    ?warehouse_region=MX&buyer_region=MX
```

La ruta **exige** el `delivery_option_id`; no existe variante sin él (por eso
`/logistics/202309/shipping_providers` devuelve `404 / 36009009`). Como las
órdenes de KingsHouse no traen ese campo en `line_items`,
`find_delivery_option_id()` lo busca en cascada:

1. `TIKTOK_DELIVERY_OPTION_ID` del `.env`
2. campo suelto en la orden / `line_items` / `packages`
3. detalle del paquete (`GET /fulfillment/202309/packages/{id}`)
4. almacenes → `GET /logistics/202309/warehouses/{id}/delivery_options`

El `delivery_option_id` y el catálogo se cachean por corrida. Una vez que veas
los `(id, name)` en el log, **fija el mapa y olvídate del lookup**:

```
TIKTOK_SHIPPING_PROVIDER_IDS={"ESTAFETA":"7117...","FEDEX":"7117..."}
```

### Reporte de la guía a TikTok

Son dos endpoints con propósitos **distintos**, no dos alternativas:

| | Endpoint | Para qué |
|---|---|---|
| `ship` | `POST /fulfillment/202309/packages/{package_id}/ship` | Despacha el paquete. Camino normal. |
| `shipping_info` | `POST /fulfillment/202309/orders/{order_id}/shipping_info/update` | **Corrige** la guía de una orden ya despachada. Va sobre la ORDEN, no el paquete. Requiere scope `seller.logistics`. |

`TIKTOK_SHIP_STRATEGY=auto` (default) despacha con `ship` y, si falla, corrige
con `shipping_info` — el caso de un reintento sobre un paquete que un intento
anterior ya despachó.

### Nombres de paqueterías

Nuestra API y TikTok no los escriben igual (`PAQUETEEXPRESS` vs
`Paquetexpress`, `J&TExpress` vs `J&T MX`). `CARRIER_NAME_ALIASES` en el config
resuelve esas equivalencias, y el match prueba **exacto antes que parcial** para
que un nombre corto no le gane a uno más específico.

`SEGMAIL` no existe en el catálogo de TikTok: si una cotización lo elige, la
orden se va a manual sin generar guía.

### El provider se resuelve antes de generar la guía

Si TikTok no reconoce la paquetería cotizada, la orden se corta con
`TIKTOK_PROVIDER_UNRESOLVED` **sin generar guía**. Antes se generaba primero y
el fallo se descubría después, dejando guías reales emitidas y pagadas que
nadie podía reportar al canal.

## Dependencias

`requests`, `python-dotenv`, `mysql-connector-python`, `gspread`,
`google-auth`, y opcionalmente `PyPDF2` (sólo para consolidar guías multicaja
en un único PDF; sin él cada guía se adjunta por separado en Odoo).
