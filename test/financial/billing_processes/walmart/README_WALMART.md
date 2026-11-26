# Walmart 3P — Facturación automática (primera versión)

Rama `feat/facturacion-walmart`. Análisis y decisiones pendientes: [ANALISIS_FACTURACION_WALMART.md](ANALISIS_FACTURACION_WALMART.md).

## Quién factura cada venta

| Fuente | Para qué |
|---|---|
| `somos_reyes.wm_financial_transactions_master` (FTM), `type='Order'` | Qué vendió Walmart. Una fila por línea (`so_prime_line_sub_line_no`) |
| `somos_reyes.wm_order_line` (API de Walmart, al día) | El **UPC** de cada línea: es el `NoIdentificacion` del CFDI de Walmart (232 de 232 en octubre). Lo usan el ligado y el candado B2 |
| **API de Fénix** (`00b`, filas `PRECFDI:<cfdiid>` en la tabla de control) | **Qué líneas pidió facturar el cliente en Walmart**, en vivo: `RECIBIDO` (Fénix ya timbró), `CON_ERROR`, `NUEVO`, `BLOQUEADO`. No trae el UUID |
| **Descarga masiva del SAT** (e.firma de `SRE1410278MA`, `02`) | El XML de cada CFDI que timbró Fénix (PAC `ICA050422AA6`, serie `FI`): se guarda en un repositorio (`CFDI:<UUID>`) y se liga con su línea |
| `somos_reyes.wm_fenix_invoices_report` (crawler de Data, **opcional**) | El UUID de cada línea. Si está al día liga el ~13 % que la fuente propia no puede (`WM_USAR_CRAWLER`) |
| `somos_reyes.odoo_new_sale_order_live` | La SO (`channel_order_reference` = `customer_order_id`) |

Solo las ventas autofacturadas en Walmart (~8 %) necesitan el XML del SAT. El resto, que es la mayoría, las timbra 04 en Odoo.

Por línea: las piezas con CFDI vigente de Walmart igualan las vendidas → **la timbró Walmart**. La línea tiene una petición `RECIBIDO` pero falta ligar su CFDI → **espera** (`PENDING_WM_UUID`). Petición con error, nueva o bloqueada → **revisar**. Nadie dice nada → **la timbramos nosotros**.

**Por qué el crawler sigue siendo opcional y no se eliminó.** La API de Fénix no devuelve el UUID (ver "Fuentes de Walmart" abajo), y el XML timbrado casi nunca trae el pedido (3 de 232 en el `Folio`). Sin el crawler, `ligar_cfdis` liga un XML con su petición de tres formas, todas exigiendo precio × piezas = total, mismo UPC y una sola respuesta posible:

1. **Folio con el pedido** (3 de 232).
2. **Camino rápido:** petición entre 0 y 30 min antes del timbrado (110).
3. **Tramo de folios:** el `Folio` de la serie FI crece en el mismo orden que el `cfdiid` de la petición. Los CFDI sin ligar entre dos ya ligados se emparejan en orden con las peticiones que caen entre esas dos (90).

Con los 232 CFDI de octubre: **203 ligados (87 %), los 203 iguales al crawler, 0 errores**. Los 29 restantes quedan `SIN_LIGAR`: los liga el crawler si está al día o Finanzas a mano (lista en el CSV de 05); mientras tanto el candado B2 frena las órdenes que podrían ser suyas. Lo que evita el doble CFDI (saber si el cliente pidió factura) no depende del crawler.

## Scripts

| Script | Hace | Escribe |
|---|---|---|
| `_wm_common.py` | Conmutador `WM_ENTORNO`, Odoo, MySQL, CFDI, funciones de Odoo (de `amazon/01`), repositorio de CFDI y ligado (`ligar_cfdis`) | — |
| `_fenix_api.py` | Cliente de la API_SELLERS de Fénix (solo métodos de lectura) | — |
| `_sat_descarga.py` | Cliente del web service de descarga masiva del SAT v1.5 (firmado con la e.firma) y consulta pública de estado de un CFDI | — |
| `test_sat_descarga.py` | Pruebas del firmado (sin SAT y sin la e.firma real) | — |
| `00_wm_precheck.py` | Candado de entorno, Odoo, cuentas, equipo, frescura de FTM, credenciales de Fénix, e.firma | — |
| `00b_wm_fenix_etl.py` | **ETL de la API de Fénix**: peticiones de factura por línea → filas `WALMART_FENIX` y la marca `ETL:ULTIMA_CORRIDA` | MySQL |
| `01_wm_miner.py` | Clasifica cada orden y llena la tabla de control | MySQL |
| `02_wm_xml_loader.py` | Descarga del SAT, guarda los CFDI de Fénix en el repositorio, llena las filas `PENDING_WM_XML` y liga CFDI con línea. Respaldo: ZIP o carpeta (`WM_XML_PATH`) | MySQL |
| `03_wm_loader_pull.py` | Replica cada CFDI de Walmart en Odoo, **sin timbrar**, solo si el SAT lo reporta `Vigente` | Odoo + MySQL |
| `04_wm_billing_push.py` | Factura a público general por SO y la timbra (solo en prod) | Odoo + MySQL |
| `05_wm_control.py` | Conteos, alertas, CFDI de Walmart sin ligar, cruce contra el CSV de Finanzas | CSV + Slack |

Kestra (borradores en `kestra/`, no en `flows/`): `wm_billing_flow` (00→00b→01→02→01→03→04→05, diario 10:30 CDMX) y `wm_carga_xml_flow` (02 con el ZIP, manual, solo de respaldo).

## Descarga masiva del SAT (02)

**Cómo pide.** El SAT es asíncrono: lo que se pide en una corrida se descarga en otra.

1. **Avanza lo pedido antes.** Verifica las solicitudes abiertas; si están terminadas, descarga sus paquetes (ZIP en base64) y guarda cada XML.
2. **Pide por día.** Pide los CFDI **emitidos** por `SRE1410278MA`, tipo I, vigentes, de 00:00:00 a 23:59:59, para cada día con CFDI de Walmart de UUID conocido sin XML (fecha del crawler) y para el día de cada petición `RECIBIDO` de las órdenes `PENDING_WM_UUID` más `WM_SAT_DIAS_TRAS_PETICION` (1). Solo días ya cerrados.
3. **Segunda descarga (definitiva).** El PAC tiene hasta 72 h para entregar un CFDI al SAT, así que lo que se pide al día siguiente puede llegar incompleto.
   - Una solicitud del día D hecha antes de D+4 es **provisional**. Al cumplirse D+4, el día se pide otra vez: es la segunda y última que permite el SAT.
   - Si la primera ya se hizo en D+4 o después, es definitiva y no se repite.
   - Los CFDI de Walmart que solo trae la definitiva se avisan por Slack, porque su orden pudo timbrarse a público general.
   - Si la definitiva falla, el día se queda con lo que bajó la provisional.
   - Variable: `WM_SAT_DIAS_DEFINITIVA` (4).

   Se descartaron dos alternativas:
   - **Pedir un rango semanal:** baja ~7 veces más XML.
   - **Pedir metadatos y luego folios:** requiere otro tipo de solicitud y otro lector.
4. **Pide por folio.** Lo que siga faltando después de la descarga definitiva de su día se pide UUID por UUID.

**Pruebas:** `WM_SAT_SOLO_DIAS=AAAA-MM-DD[,…]` pide solo esos días; apaga el barrido, los pendientes y los folios.

**Repositorio.** De todo lo descargado, los CFDI de Fénix (PAC `ICA050422AA6`, serie `FI`, certificado con OU=Walmart) se guardan como filas `CFDI:<UUID>` (`marketplace='WALMART_FENIX'`), estén pendientes o no. Lo demás (Mercado Libre, Amazon, público general) se descarta. Después `ligar_cfdis` liga cada CFDI suelto con su línea y la segunda pasada del miner crea las filas `<orden>:<UUID>`.

**Seguimiento.** Cada solicitud es una fila de la misma tabla de control, con `marketplace='SAT_DESCARGA'` y `mkp_order_id` = `EMITIDOS:<día>` o `FOLIO:<uuid>`:
- **Estados:** `SOLICITADA` → `TERMINADA` → `DESCARGADA`, o bien `SIN_CFDI`, `RECHAZADA`, `VENCIDA` o `ERROR`.
- **`raw_json`:** el `IdSolicitud`, los paquetes y los ya descargados.
- **`retry_count`:** veces que se pidió. El SAT admite 2 solicitudes con los mismos parámetros "de por vida" (código 5002), y no se pasa de ahí.

Un error del SAT no corta el flujo: se avisa por Slack y se reintenta en la siguiente corrida.

**Firmado.** RSA-SHA1 con la e.firma, armado como texto canónico, igual que la implementación de referencia [phpcfdi/sat-ws-descarga-masiva](https://github.com/phpcfdi/sat-ws-descarga-masiva). La única dependencia nueva es `cryptography`, ya agregada a `requirements.txt` y a `docker/python_librariesv2.Dockerfile`.

**e.firma en local** (en el `.env`; la contraseña nunca en el chat ni en el repo):

```
SAT_FIEL_CER_PATH=C:\ruta\segura\efirma_SRE1410278MA.cer
SAT_FIEL_KEY_PATH=C:\ruta\segura\efirma_SRE1410278MA.key
SAT_FIEL_PASSWORD=...
```

**e.firma en Kestra (EC2).** Va como secrets en el `.env` del docker-compose de Kestra, igual que los demás. Kestra OSS decodifica una vez el base64 de `SECRET_*`. Como el script espera el **archivo en base64**, el `.cer` y el `.key` se codifican **dos veces**:

```bash
echo "SECRET_SAT_FIEL_CER_B64=$(base64 -w0 efirma.cer | base64 -w0)" >> .env
echo "SECRET_SAT_FIEL_KEY_B64=$(base64 -w0 efirma.key | base64 -w0)" >> .env
echo "SECRET_SAT_FIEL_PASSWORD=$(printf '%s' 'LA_CONTRASENA' | base64 -w0)" >> .env
docker compose up -d   # para que Kestra lea los secrets nuevos
```

Los tres secrets tienen que existir antes de activar el flujo: `secret()` falla si no existen. Cuando se renueve la e.firma, se cambian los tres; `00` y `02` avisan por Slack 30 días antes de que venza.

**API de Fénix en Kestra.** Dos secrets más: `SECRET_FENIX_WALMART_USER` y `SECRET_FENIX_WALMART_PASSWORD` (base64 una vez).

**Imagen.** `build_and_publish_docker_image` clona `main`: el Dockerfile con `cryptography` tiene que estar fusionado antes de construir. Se construye con `docker/python_librariesv2.Dockerfile` y el tag `v2.0`; ningún flujo de `main` usa `v2.0` (todos usan `v1`), así que reemplazarla no cambia nada en producción. Los flujos de Walmart ya apuntan a `wonderbrands-python:v2.0`.

## Tabla de control

`finance.mkp_billing_{staging,prod}`, `marketplace='WALMART'`.

- `mkp_order_id = <customer_order_id>`: una fila por orden que factura WB o que está en espera.
- `mkp_order_id = <customer_order_id>:<UUID>`: una fila por CFDI (pieza) de Walmart, con `cfdi_uuid` y `xml_data` (base64).

| Estado | Significa | Lo toma |
|---|---|---|
| `PENDING_WB_INVOICE` | Factura WB | 04 (después de la gracia) |
| `PENDING_WM_UUID` | Walmart facturó (petición `RECIBIDO` en la API) y falta ligar su CFDI | 02 (descarga y liga) |
| `PENDING_WM_XML` | CFDI de Walmart sin XML | 02 |
| `PENDING_WM_LINK` | CFDI de Walmart con XML | 03 |
| `WB_INVOICED` / `WM_LINKED` / `ALREADY_ODOO_INVOICED` | Cerrada | — |
| `NO_SO`, `SO_DUPLICADA`, `SO_OTRO_EQUIPO`, `SO_CANCELADA` | Problema con la SO | 01 la reevalúa |
| `ORDEN_MIXTA`, `WM_CFDI_REVISAR`, `WM_CFDI_EXCEDENTE` | Fénix no deja decidir quién factura | 01 la reevalúa |
| `DOBLE_CFDI` | CFDI de Walmart y otra factura viva en la SO | Finanzas cancela la nuestra |
| `RECLASIFICAR` | Apareció Fénix justo antes de timbrar | 01 la reevalúa |
| `BORRADOR_AJENO` | La SO tiene un borrador sin CFDI de otro proceso | Finanzas lo cancela; 01 la reevalúa |
| `FACTURA_MANUAL_SERIE` | Bici eléctrica sin CFDI de Walmart: se factura a mano con número de serie | CS / Finanzas |
| `TOTAL_DIFF`, `XML_INVALIDO` | No cuadra | Finanzas |
| `WM_CFDI_NO_VIGENTE` | El SAT reporta cancelado el CFDI de Walmart: no se replica | Finanzas |
| `ERROR` | Se reintenta hasta 3 veces | 03 / 04 |

**Fuente propia de Fénix** (`marketplace='WALMART_FENIX'`, misma tabla):

| `mkp_order_id` | Estados | Escribe |
|---|---|---|
| `PRECFDI:<cfdiid>` | `RECIBIDO`, `CON_ERROR`, `NUEVO` | 00b (cada corrida reescribe todo; una petición que desaparece se queda) |
| `BLOQUEADO:<id>` | `BLOQUEADO` (falta serie, lote o pedimento) | 00b |
| `CFDI:<UUID>` | `SIN_LIGAR` → `LIGADO` (`raw_json`: `iderp`, `orden`, `cfdiid`, `metodo` = `crawler`, `folio_pedido`, `api_ventana` o `folio_tramo`) | 02 |
| `ETL:ULTIMA_CORRIDA` | `OK` | 00b, solo si las cuatro llamadas a la API salieron bien |

## Blindajes (CLAUDE.md §6)

- `concurrency: limit 1, behavior: FAIL`, sin retry.
- Revalidación fail-closed antes de cada `create` (`facturas_existentes`: `invoice_origin`, `ref AUTOINV:<SO>%` y `invoice_ids`, que ve las globales y las del proceso manual con `ref=''`).
- Idempotencia: `AUTOINV:<SO>` (WB) y `AUTOINV:<SO>:<UUID>` (Walmart).
- Escritura con red caída → `OdooWriteUncertain` → se busca por `ref`, nunca se reintenta.
- 04 no emite si la última corrida buena del ETL de la API de Fénix (00b) tiene más de `WM_FENIX_MAX_HORAS` (6 en el flujo), y justo antes de emitir vuelve a buscar peticiones de la API y filas del crawler para las órdenes del lote.
- 03 solo replica un CFDI que el SAT reporta `Vigente` (consulta pública); si el servicio no responde, no crea.
- Un CFDI de Walmart solo se liga a una línea cuando no hay duda (crawler, folio con el pedido, camino rápido o un tramo de folios con una sola forma de emparejar); nunca por aproximación. Una petición recibe un solo CFDI y una línea no recibe más CFDI que piezas vendidas.

## Probar en local

Desde esta carpeta, con el Python del `.venv` del repo. Por defecto `WM_ENTORNO=staging` (tablas `_staging`, `ODOO_TEST_URL`, timbrado apagado, tope de 20 facturas).

```powershell
$env:WM_DRY_RUN = "1"; $env:WM_START_DATE = "2026-10-01"; $env:WM_END_DATE = "2026-11-01"
..\..\..\..\.venv\Scripts\python.exe 01_wm_miner.py          # solo lee
Remove-Item Env:WM_DRY_RUN
..\..\..\..\.venv\Scripts\python.exe 00_wm_precheck.py
..\..\..\..\.venv\Scripts\python.exe 00b_wm_fenix_etl.py     # API de Fenix (FENIX_WALMART_* en el .env)
..\..\..\..\.venv\Scripts\python.exe 01_wm_miner.py          # escribe mkp_billing_staging
..\..\..\..\.venv\Scripts\python.exe test_sat_descarga.py    # firmado, sin SAT
..\..\..\..\.venv\Scripts\python.exe 02_wm_xml_loader.py     # SAT (SAT_FIEL_* en el .env); correr de nuevo mas tarde para descargar
# Respaldo con ZIP: $env:WM_XML_PATH = "C:\ruta\xmls_walmart.zip"
..\..\..\..\.venv\Scripts\python.exe 03_wm_loader_pull.py
..\..\..\..\.venv\Scripts\python.exe 04_wm_billing_push.py
..\..\..\..\.venv\Scripts\python.exe 05_wm_control.py
```

## Validación de solo lectura (12-nov-2026)

- **Miner en `WM_DRY_RUN` (sep y oct).** Septiembre: 2,044 órdenes para WB, 265 CFDI de Walmart, 6 SO canceladas, 7 duplicadas y 25 `WM_CFDI_REVISAR`. Octubre: 1,684 para WB, 197 CFDI de Walmart, 3 sin SO, 32 SO canceladas, 6 duplicadas, 5 mixtas y 22 `WM_CFDI_REVISAR`. Cuadra con el perfil del análisis.
- **Fénix contra el SAT.** De los 197 CFDI de Walmart de octubre, 192 están en la carpeta `xmls_walmart` de Finanzas. En los 192 coinciden el UUID, el emisor `SRE1410278MA` y el total al centavo con Fénix; son tipo I, nominativos y de un solo concepto. Los otros 5 no están en la carpeta. 187 cuadran ±$1 contra el precio de Walmart; los 5 restantes se van a `TOTAL_DIFF`.
- **CSV de Finanzas.** `autofacturacion.csv` de octubre trae XML solo en 2 de 129 filas: el miner encuentra 197 CFDI de Walmart en el mes. Además, 48 de las 163 SO de `facturacion_global.csv` (la lista para timbrar WB) ya tienen CFDI vigente de Walmart. Según los resultados del 1a1 manual, 21 se timbraron el 2–3 de noviembre (ej. SO10311316 → INV/2026/181091) y los 26 CFDI de Walmart de esas SO son anteriores al 2 de noviembre: **probable doble CFDI, confirmar en Odoo y en el SAT**.
- **`00_wm_precheck.py` contra el Odoo de pruebas** (`somosreyes-test-06-nov-2026`): encuentra la CxC, la 205.06.003 y `Team_Walmart` (id 30). Fénix: 244.7 h sin cargarse.
- **Sin ejecutar:** 01 escribiendo, 02, 03, 04 y 05 (escriben en MySQL u Odoo).

## Validación del cliente del SAT (13-nov-2026)

- **`test_sat_descarga.py`, ida y vuelta** con una e.firma ficticia: los cinco sobres (autenticación, solicitud por día, solicitud por folio, verificación y descarga) son XML válido, el digest corresponde al nodo firmado y la firma RSA-SHA1 se verifica con la llave pública.
- **Contra la referencia**, con la e.firma de pruebas del SAT (`EKU9003173C9`) y los casos de phpcfdi: los cinco sobres salen idénticos en forma canónica, con el mismo `DigestValue` y el mismo `SignatureValue`. También se leen bien las respuestas de ejemplo (token, verificación con 2 paquetes, paquete ZIP).
- **Contra el SAT real**, con esa misma e.firma pública de pruebas:
  - La autenticación **devolvió token**.
  - La solicitud llegó y el SAT la rechazó con `305 Certificado Inválido`: el certificado de pruebas no es de producción. No hubo 301/302 (*XML o sello mal formado*), así que el formato y la firma son aceptados.
- **No probado:** el ciclo completo con la e.firma real de `SRE1410278MA` (solicitud aceptada, verificación, descarga y carga a la tabla).

## Prueba con la e.firma real y en Odoo pruebas (19-nov-2026)

**SAT, e.firma de `SRE1410278MA`:**
- **e.firma:** abre con su contraseña del `.env` y la llave corresponde al certificado, vigente hasta el 15-sep-2029.
- **Autenticación:** token recibido.
- **Solicitud por folio** del CFDI de Walmart `78EB8339-…F15C` (SO10187391): `5000 Solicitud Aceptada`.
- **Verificación:** horas después quedó `TERMINADA`, con 1 CFDI y 1 paquete.
- **Descarga:** ZIP con el XML; UUID, emisor `SRE1410278MA`, total $1,358.99 y serie-folio `FI 3482`, el mismo Folio del CSV de Finanzas de julio.
- **No probado:** el ciclo dentro de 02, con sus filas `SAT_DESCARGA`. Para no gastar las 2 solicitudes "de por vida" por día, la carga de XML de esta prueba se hizo con la carpeta de Finanzas.

**Flujo en `somosreyes-test-13-10-2026-41883942`, ventana octubre, timbrado apagado:**

| Paso | Resultado |
|---|---|
| 00 | Odoo, CxC, 205.06.003, `Team_Walmart` y la e.firma OK. Fénix: 413 h sin carga |
| 01 | 1,949 filas: 1,684 para WB, 197 CFDI de Walmart, 68 en espera (32 SO canceladas, 22 REVISAR, 6 SO duplicadas, 5 mixtas, 3 sin SO) |
| 02 (carpeta de Finanzas) | 192 de 197 XML cargados; los 5 que faltan no están en la carpeta |
| 03 (tope 20) | 20 facturas creadas, todas con dif $0.00; los pedidos de varias unidades salen con una factura por CFDI (SO10352113: 4). 30 adoptadas del proceso manual (UUID con sufijo `@`). **6 `DOBLE_CFDI` reales**: facturas a público general que el 1a1 manual timbró el 2–3 de noviembre (INV/2026/181084 a 181106) sobre pedidos que Walmart ya había facturado al cliente |
| 04 (tope 20; `WM_FENIX_MAX_HORAS=1000` solo para la prueba) | 20 facturas creadas, dif de ±$0.02 como máximo. 432 ya facturadas por el proceso manual. 7 SO en borrador confirmadas |
| 05 | Conteos y alertas OK. Cruce con el CSV de Finanzas: 36 SO del CSV están fuera de la ventana de octubre |

**Asiento de ejemplo (INV/2026/255548, CFDI de Walmart):** cliente GRUPO CALZAPATO (receptor del CFDI), Dr 105.01.007 729.00 / Cr 401.01.001 628.45 / Cr 208.01.001 100.55 / Dr 501.01.001 249.53 / Cr 205.06.003 249.53, conciliada con la salida; XML adjunto, uso G03.

**205.06.003 con WFS.** Las 40 facturas son WFS:
- **21 conciliadas** con su salida.
- **10 sin salida porque Walmart no las ha enviado** (`Created`).
- **8 `Delivered` y sin salida en Odoo** al 13-nov: validar con Operaciones cómo y cuándo se registra la salida de WFS.

**Lo que quedó en la base de pruebas:**
- **Facturas:** INV/2026/255545 a 255584, 40 publicadas sin timbrar.
- **Clientes nuevos:** 9 (ids 170523 a 170531).
- **SO:** 7 confirmadas.
- **Tabla de control:** `finance.mkp_billing_staging` con `marketplace='WALMART'`.

## Contraste con los correos de Finanzas (17 al 19-nov-2026)

| Punto del correo | Qué pasó (evidencia) | Cómo lo cubre este flujo |
|---|---|---|
| 1. Pedidos de varias unidades (29 borradores, 166 pedidos incompletos) | El CSV de Finanzas traía **una fila (un Folio) por pedido** (`Julio/autofacturacion.csv`: `600000104852023,3482`), así que solo entraba un CFDI. El script recorría las piezas de la línea y creaba la factura de la segunda sin XML. En Odoo pruebas: SO10187391 tiene 339171 publicada y 339172 en borrador, creada 4 s después por el mismo script ("Autofacturación (Odoo 18)"), sin adjunto | El universo de CFDI sale de Fénix (una fila por CFDI). Una factura por UUID (`AUTOINV:<SO>:<UUID>`). 03 solo crea facturas con XML y valida el total antes del `create` |
| UUID en Odoo | Las facturas del proceso manual guardaron el UUID con el sufijo del archivo (`78EB8339-…F15C@3000000000XX0`). Un cruce por UUID exacto no las reconoce | `normalizar_uuid` quita el sufijo: esas facturas se adoptan, no se duplican. **Para el cruce de Finanzas:** comparar sin el sufijo |
| 2. SO10022462 y SO10016216 (1 unidad) | Su única factura es un borrador de DataBot sin chatter ni adjunto (3-may 19:47 y 31-may 20:50). El del 3-may coincide con la corrida de la factura global (INV/2026/00458, 3-may 19:27) | Estado `BORRADOR_AJENO`: no se crea nada encima. Cuando Finanzas cancela el borrador, la orden se reevalúa y se replica el CFDI de Walmart |
| 3. SO10292125 (cierre a público en general) | Bitácora `Septiembre/resultado_walmart_1a1_20261002_160526.csv`: `ERROR_CONFIRMACION`, *Posting indeterminado: message_post: 502 BAD GATEWAY*. El 502 llegó en el chatter, antes de publicar; quedó el borrador `AUTOINV:SO10292125` (CxC 105.01.007) y nadie lo retomó | 04 retoma su propio borrador (`AUTOINV:<SO>` en borrador) en la corrida siguiente, y escribe el chatter después de publicar |
| Correo 2, 1.1: carga mensual de facturas de Walmart en 105.01.001 | Confirmado en Odoo pruebas (339171, 25577: CxC 105.01.001, cliente "Walmart") | 03 fuerza la 105.01.007. El cliente de la factura es el receptor del CFDI (RFC del comprador), no "Walmart": **confirmar con Finanzas** |
| Correo 2, 1.1: equipo de la factura | — | 03 y 04 ponen `Team_Walmart` |
| Correo 2, 2.1: asientos | La factura original lleva el costo contra 205.06.003 | 03 y 04 son la "factura original": Dr CxC / Cr 401 y 208, Dr 501 / Cr 205.06.003 |
| Correo 3: Walmart | Solicitadas por el cliente → portal de Walmart con nuestros sellos. No solicitadas → script automático. Portal de Somos Reyes → nada | 02/03 replican las del portal de Walmart; 04 timbra las no solicitadas. El bloqueo del portal de Somos Reyes es otra tarea; si algo entra por ahí, la revalidación lo ve |
| Correo 3: bicis eléctricas | Walmart las bloqueó en su portal y las deriva a Somos Reyes; se facturan a mano (CS + Finanzas) con número de serie. En FTM hay 629 líneas en 2026 (`EBIKE500-*`, `EBIKE350-B`, `EX-1*`) | Estado `FACTURA_MANUAL_SERIE`: 04 no las timbra. Si Walmart ya emitió el CFDI, se replica igual. Prefijos en `WM_SKUS_FACTURA_MANUAL` (confirmar la lista, acción 12) |
| Acción 24: sanear 166 pedidos y 348 CFDI | — | Correr el miner con `WM_START_DATE` histórico (FTM empieza en ene-2026). Primero cancelar los borradores ajenos. Los 43 de Odoo 15 quedan en `NO_SO` |

## Fuente propia de Fénix: validación en staging (23-nov-2026)

**API de Fénix** (`00b`): 1,258 peticiones recibidas, 209 con error y 14 nuevas; 0 bloqueadas. La API ignora el rango de fechas y siempre regresa todo. De los 197 CFDI de octubre del crawler, la petición de 196 está en la API (el `invoiceno` del crawler coincide). El que falta, pedido **600000121084541** (CFDI `aa32c3ce`, vigente), no sale en ninguna lista de la API: por eso existen el barrido del SAT y el candado B2 de 04.

**Repositorio** (carpeta de octubre de Finanzas, 140,514 archivos leídos, 52 min): 232 CFDI de Fénix reconocidos por PAC + serie; **los 232 están en el crawler** (ningún falso positivo).

**Ligado sin crawler** (solo camino rápido, 23-nov): 110 de 232 ligados, **los 110 iguales al crawler**, 0 errores; 122 quedan `SIN_LIGAR`. Con el crawler encendido se ligan los 122 restantes. El 24-nov subió a 203 con el folio (ver abajo).

**Miner de octubre:**

| | Solo crawler (19-nov) | API + repositorio, crawler apagado | API + repositorio + crawler |
|---|---|---|---|
| Para timbrar WB | 1,684 | 1,681 | 1,680 |
| CFDI de Walmart (filas) | 197 | 105 + 63 órdenes `PENDING_WM_UUID` | 198 + 13 `PENDING_WM_UUID` |
| `WM_CFDI_REVISAR` | 22 | 17 | 17 |
| `ORDEN_MIXTA` | 5 | 0 | 0 |

Sin crawler, solo 1 orden queda para timbrar WB cuando Walmart ya la facturó (la 600000121084541). **Candado B2** (CFDI sin ligar timbrado hasta 7 días después de la venta, mismo total y mismo UPC): el 23-nov, sin crawler, detenía ese caso y el 6.9 % de las órdenes de WB (118 de 1,721). Con el ligado por folio y el UPC de `wm_order_line` baja a 0.6 % (ver abajo).

**Flujo completo en `somosreyes-test-13-10-2026-41883942`** (00 → 00b → 01 → 02 → 01 → 03 → 04 → 05, crawler encendido, 02 sin e.firma para no gastar solicitudes del SAT):
- 03: 20 facturas con dif $0.00 (INV/2026/255585 a 255604), todas confirmadas `Vigente` en el SAT antes del `create`; 30 adoptadas; 2 `TOTAL_DIFF`; más `DOBLE_CFDI` (lista abajo).
- 04: 20 facturas a público general, dif ±$0.01 (INV/2026/255605 a 255624); 330 ya facturadas; 0 detenidas por B o B2.
- 05: ETL de Fénix con 0.1 h; crawler con 517 h.

- 03 sin tope sobre el resto de octubre: 23 creadas más, 51 adoptadas, 2 `TOTAL_DIFF`, 0 errores. Ya no queda ningún CFDI de Walmart de octubre pendiente de replicar.

**Dobles CFDI de octubre confirmados en el SAT** (las dos facturas vigentes): **15 facturas a público general** sobre 20 CFDI de Walmart. Las 6 del correo (INV/2026/181084, 181088, 181089, 181097, 181105, 181106) y además:

| Factura a cancelar | SO | Total | CFDI de Walmart |
|---|---|---|---|
| INV/2026/181098 | SO10325753 | $4,349.00 | 1 (05-oct) |
| INV/2026/181107 | SO10330699 | $5,818.00 | 2 (06-oct) |
| INV/2026/181113 | SO10335253 | $1,799.00 | 1 (09-oct) |
| INV/2026/181119 | SO10336204 | $2,899.00 | 1 (09-oct) |
| INV/2026/181120 | SO10326227 | $2,998.99 | 1 (06-oct) |
| INV/2026/181121 | SO10328570 | $5,499.00 | 1 (06-oct) |
| INV/2026/181142 | SO10324742 | $549.00 | 1 (07-oct) |
| INV/2026/181154 | SO10340774 | $2,945.01 | 5 de $589 (12-oct) |
| INV/2026/181173 | SO10349009 | $1,199.00 | 1 (15-oct) |

## Fuentes de Walmart y ligado por folio (24-nov-2026)

### Qué trae cada fuente

| Fuente | Cómo se llena | Trae | No trae | Última carga |
|---|---|---|---|---|
| `wm_fenix_invoices_report` (crawler, `crawler_wm_invoices.py`) | Selenium sobre el portal `edifacturacionsellers.com` (Reportes > Reporte CFDI, últimos 30 días). Lo corre Data a mano en Windows (lee la versión de Chrome del registro) | Una fila por petición: `identificador`, `invoice`, pedido, línea, **UUID**, `estado_cfdi` (`CON_XML_PDF`, `ERROR`, `CANCELADO`, `REEMPLAZADO`), UPC, total | — | **2-nov-2026** (22 días) |
| API de Fénix (`00b`) | En vivo, cada corrida | Las mismas peticiones: **`cfdiid` = `identificador` e `invoiceno` = `invoice`** (232 de 232) y su estado | **El UUID** y el estado del CFDI | Cada corrida |
| FTM (`wm_financial_transactions_master`) | ETL de Data (Yanina), fuera de este repo; carga diaria (24-nov: 67 filas nuevas y 455 actualizadas). Es una tabla **consolidada** por línea (`so_prime_line_sub_line_no`): la orden de la API de Walmart (`wm_order_line`), los pagos del reporte de pagos de Walmart (`wm_payments_report`: un renglón por línea y concepto, de ahí `venta`, `comision`, `killer`, retenciones, cargos WFS y sus fechas de pago; `venta` coincide en 4,010 de 4,108 líneas de sep–oct), las devoluciones (`wm_return_orders`, filas `type='Refund'`) y el crawler de Fénix (`wm_invoice_*`) | Ventas, devoluciones y cobros. Las columnas `wm_invoice_uuids`, `wm_invoice_count`… **salen del crawler**: los 1,622 UUID están en el crawler y noviembre tiene 1 | Fuente independiente de UUID | 22-nov |
| `wm_new_orders` / `wm_order_line` | API de Walmart | Orden, línea, SKU, **UPC**, precio, estado | Nada de facturación | 24-nov |

**¿La API sustituye al crawler?**
- **Para saber quién pidió factura, sí.** Están 196 de 197 peticiones de octubre; la que falta la cubren el barrido del SAT y el candado B2.
- **Para saber qué UUID tiene cada factura, no.** La API tiene 12 métodos (especificación OpenAPI completa), y ninguno de sus modelos trae el UUID del CFDI timbrado:
  - `descargarLista` entrega la petición (pre-CFDI), no el CFDI.
  - `recibirCfdiTimbrado` sirve para **subir** un CFDI, no para descargarlo.
- **El script viejo tampoco lo sacaba de una API.** `invoice_creation_qtyV18.py` tomaba el UUID de la columna `xml` de `autofacturacion.csv`, que Finanzas armaba a mano con el mismo reporte del portal.
- **El sustituto:** el XML del SAT más `ligar_cfdis`, que liga el 87 % sin el crawler.

### Pruebas (solo lectura, o tabla temporal de MySQL que no toca staging)

| # | Prueba | Para qué | Resultado |
|---|---|---|---|
| 1 | Columnas y fecha de carga de las cuatro tablas | Saber qué trae cada una | Tabla de arriba |
| 2 | UUID de FTM contra el crawler | ¿FTM es otra fuente de UUID? | 1,622 de 1,622 están en el crawler: no |
| 3 | Campos de la API dentro del XML | Buscar una llave exacta | Sin addenda; ningún `cfdiid`, `invoiceno` ni `reqtransactionid` en el XML |
| 4 | `Folio` del XML contra el `cfdiid` (verdad: crawler) | ¿El orden alinea? | Folios 4075–4374 (229): **0 inversiones**. 3 folios traen el pedido |
| 5 | UPC del XML contra `wm_order_line` | ¿Sirve para distinguir? | 232 de 232 iguales (y 2,064 de 2,064 contra el crawler) |
| 6 | Simulación de reglas | Elegir la regla | Ventana de 24 h + UPC: 128 bien. Ventana de 7 días: 1 mal. Sin API (fecha de venta + UPC): 2 a 4 mal. **Las tres se descartaron.** Tramo de folios: +90, **0 mal** |
| 7 | Tramo de folios con la mitad de las anclas | ¿Falla si hay menos ligas previas? | 0 errores; cobertura 70–76 % |
| 8 | `ligar_cfdis()` real, crawler apagado, sobre tabla temporal | Probar el código, no la simulación | **203 de 232 bien** (110 camino rápido + 90 tramo + 3 folio con pedido), **0 mal**, 29 `SIN_LIGAR`. Staging intacta |
| 9 | Candado B2 con el UPC de `wm_order_line`, sin crawler | ¿Cuánto frena? | **10 de 1,721 órdenes WB (0.6 %)**, antes 118 (6.9 %). El caso 600000121084541 sigue detenido |

**Cambios en el código:**
- `_wm_common.ligar_cfdis`: folio con el pedido, tramo de folios, UPC, y una petición por CFDI.
- `_wm_common.upc_por_linea`: nueva.
- `04.cfdi_sin_ligar_posible`: usa el UPC de la línea en vez del mapa aprendido. Se borró `upc_por_sku`.

**No probado:**
- **Otro mes.** Solo octubre tiene XML en el repositorio. No se pidió otro mes al SAT para no gastar solicitudes que producción va a necesitar.
- **La corrida completa en Odoo con el ligado nuevo.** En staging los 232 ya estaban ligados por el crawler, así que 01 y 03 no tendrían nada nuevo que hacer.

## Pruebas del 24-nov-2026 (tarde): SAT real, SIN_LIGAR y noviembre

Todo en staging (`finance.mkp_billing_staging`) y Odoo pruebas `somosreyes-test-13-10-2026-41883942` (copia con órdenes hasta el 13-nov), crawler apagado, timbrado apagado.

### Ciclo de 02 con el SAT real

Ya había corrido completo el 23-nov para el 30 y el 31-oct (1,353 y 11,953 CFDI; 1 y 2 paquetes). El 24-nov se pidieron con `WM_SAT_SOLO_DIAS` el **6-oct** y del **1 al 23-nov**:

| Qué | Resultado |
|---|---|
| Solicitudes | 24 aceptadas (5000). El SAT terminó la verificación de las 24 en 25 minutos |
| Descarga | 24 paquetes, 73,737 CFDI de la empresa. Se guardaron 535 de Fénix (noviembre) y se desechó el resto |
| 6-oct contra la carpeta de Finanzas | El SAT trajo 34 CFDI de Fénix y **los 34 ya estaban** en el repositorio cargado con la carpeta de Finanzas: 0 nuevos y 0 faltantes |
| Fallas de red | 3 corridas con *timeout* al conectar con el SAT. La corrida siguiente retomó donde iba, sin perder paquetes ni repetir solicitudes |
| Segunda descarga | Del 1 al 20-nov quedaron `definitiva=True`, porque se pidieron en D+4 o después. Del 21 al 23-nov quedaron provisionales: se vuelven a pedir del 25 al 27-nov |

Costo: el 6-oct y del 1 al 23-nov ya gastaron 1 de sus 2 solicitudes. Producción no los vuelve a pedir si se siembra con estas filas (ver "Pendiente antes de producción").

### Por qué queda SIN_LIGAR (octubre, crawler apagado)

Son 29 de 232 CFDI, de **13 órdenes: el 0.7 % de las órdenes de Walmart del mes** (12.5 % de los CFDI de Walmart).

| CFDI | Motivo |
|---|---|
| 22 | **Ambiguos.** El mismo cliente compró piezas iguales al mismo precio en varias órdenes: 10 CFDI de $589 en 2 órdenes, 10 de $1,359 en 3 órdenes (con 6 órdenes candidatas) y 2 de $3,738 en la misma línea. En cada grupo **todos los CFDI tienen el mismo receptor** |
| 4 | El total del CFDI es menor que el precio de FTM (de $70 a $600). Aunque se ligaran caerían en `TOTAL_DIFF`, así que Finanzas los ve de todos modos |
| 2 | Una sola petición candidata, pero a 68 min y a 12 días del timbrado: fuera de la ventana y de un tramo |
| 1 | El pedido no tiene petición en la API (600000121084541) |

**¿Requiere a una persona?** No siempre. Con el crawler al día se ligan los 29.

Sin el crawler hay una regla segura, **todavía no implementada**, que ligaría 12 de los 22 ambiguos. Se aplica a un grupo cerrado: N CFDI con el mismo receptor, UPC y total, que solo pueden ir a N piezas pedidas, y esas piezas no tienen otro CFDI posible. En ese caso cualquier forma de emparejarlos da el mismo resultado contable: mismo cliente, producto y precio, y cada orden recibe sus piezas.

Lo que queda (el grupo de $1,359, los 2 fuera de ventana y el que no está en la API) sí necesita el crawler o la liga manual.

### Noviembre (ventas del 1 al 12-nov, crawler apagado)

| Paso | Resultado |
|---|---|
| 00b | 1,267 peticiones recibidas, 210 con error y 14 nuevas |
| 01 | 1,514 órdenes para WB, 85 `PENDING_WM_UUID`, 30 SO canceladas, 8 duplicadas, 7 `WM_CFDI_REVISAR`, 13 bicis eléctricas y 1 sin SO |
| 02 | 535 CFDI de Fénix (1 al 23-nov): **356 ligados (66 %)** (256 por el camino rápido, 15 por el folio con el pedido y 85 por tramo) y 179 `SIN_LIGAR` |
| 01 (segunda pasada, con la corrección) | 101 CFDI de Walmart de las ventas del 1 al 12-nov tomaron su XML del repositorio |
| 03 (sin tope) | **92 facturas sin timbrar (INV/2026/255698 a 255789), las 92 con dif $0.00**. 89 quedaron conciliadas con su salida y 3 no tienen salida todavía. Se crearon clientes nuevos con el RFC del receptor. Las otras 9 no se crearon: el SAT no respondió la consulta de vigencia, así que 03 se detuvo como debe y las reintenta en la corrida siguiente |
| 04 (tope 50) | 50 facturas a público general (INV/2026/255648 a 255697), dif de $0.01 como máximo, 0 errores. El candado B2 detuvo **21 órdenes** (`RECLASIFICAR`), el 1.4 % |

**Noviembre liga menos que octubre (66 % contra 87 %).** La semana del Buen Fin (13 al 20-nov) trajo de 38 a 75 CFDI de Walmart por día, contra unos 8 en octubre. Con tanto volumen hay más piezas iguales y más brecha entre la petición y el timbrado. De los 179 `SIN_LIGAR`:
- 75 no tienen ninguna petición candidata;
- 59 tienen una a ≤ 30 min, pero son ambiguos;
- 45 tienen la candidata más cercana a más de 30 min.

**Lo que no se pudo verificar:** si los 356 ligados de noviembre son correctos. El crawler no se carga desde el 2-nov, así que no hay contra qué comparar. Hace falta que Data lo corra una vez.

**Lo que quedó en la base de pruebas:** INV/2026/255648 a 255789 (142 facturas publicadas sin timbrar) y 68 clientes nuevos creados por 03 (ids 170551 a 170618).

**Error encontrado y corregido.** La segunda pasada de 01 creaba las filas `<orden>:<UUID>` sin XML: el XML solo se copiaba del repositorio al empezar 02. Por eso 03 no replicaba lo recién ligado hasta la corrida siguiente, aunque el YAML dice que lo hace en la misma. Ahora 01 llama a `llenar_xml_desde_repositorio` (en `_wm_common`) al terminar de escribir.

## Respuesta de Finanzas (25-nov-2026) y cambios

| Punto | Pedido | Cambio | Por qué y cómo |
|---|---|---|---|
| A1 | IVA como **no cobrado** (209.01.001) hasta el cobro | 03 y 04 usan el impuesto **14** (`WM_TAX_ID`), no el 38 | En Odoo el 14 ("16%") es exigible al cobro con transitoria 209.01.001: al publicar va a la 209 y Odoo lo pasa a la 208 al conciliar el cobro. El 38 (ML y Amazon) es exigible al facturar. `cargar_catalogos` valida que el impuesto sea al cobro con la 209, y si no, no factura. **ML y Amazon van en su propia rama** |
| A3 | Bicis eléctricas sin trato especial | `WM_SKUS_FACTURA_MANUAL` vacío por defecto | Ya no existe `FACTURA_MANUAL_SERIE`: pasada la gracia, 04 las factura a público general |
| A4 | Gracia lo más larga posible, desde `order_date` | `WM_GRACE_DAYS=7` | 434 peticiones de ventas hasta el 9-oct: 94 % llega en 3 días, 99.8 % en 5 y 100 % en 10. Más de 7 días casi no evita dobles CFDI y amontona el cierre de mes (todo lo pendiente se timbra los días 1 y 2; ~500 órdenes con 7 días) |
| B2 | Una factura por CFDI vigente; público general una por orden | Sin cambio | Ya era así (03 por UUID, 04 por SO). 03 respeta la cantidad del CFDI (hay de 2 y 4 piezas) |
| B3 / D4 | Completitud siempre contra el SAT | 05 reporta **completitud contra el SAT** | El universo es el repositorio de la descarga masiva: CFDI en Odoo, ligados sin factura y sin ligar. Los que faltan salen en el CSV (`SAT_SIN_FACTURA_EN_ODOO`) |
| C1 | Importar los CFDI faltantes y corregir la existente | 03 adopta una factura existente **solo si coincide** con el CFDI | Si no coinciden total, cliente (RFC), CxC o diario → `WM_ADOPTADA_REVISAR` con el motivo. Esas facturas las corrige el traslado de 2026 (C3). Las piezas que faltan se importan normal |
| C2 | Datos cruzados (UUID con otro producto y monto) | 03 nunca usa la línea de otro SKU; el UPC del CFDI tiene que ser el de la línea | **Causa:** si la línea del SKU ya estaba facturada, 03 tomaba "la única línea pendiente" aunque fuera otro producto. En SO10339388 la línea GARDENSET3 la había tomado INV/2026/171511 (proceso manual, con el UUID equivocado), y 03 puso el CFDI de $1,605 sobre la línea LIVGREEN4. Ahora → `WM_LINEA_REVISAR` |
| C3 / D5 | CFDI de Walmart en el diario WALM con su serie y folio | 03 crea en el diario `WALM` (`WM_JOURNAL_CODE`) con nombre `FI-<folio>` | Mismo patrón que `MELI-xxxxx` y `AB-xxxx`. No consume la secuencia INV. 04 (público general) sigue en INV |
| C6 | Monto: `price_amount` cruzado con la SO | Sin cambio | 04 factura las líneas de la SO y las cruza contra `price_amount` (±$1, si no `TOTAL_DIFF`). 03 toma el monto del CFDI y lo cruza contra `price_amount` por pieza. Nunca se usa `tax_amount` |
| C7 | Solo CFDI vigentes | Sin cambio | El crawler solo cuenta `CON_XML_PDF` (incluye `REFACTURAR`); la descarga pide solo vigentes, y 03 consulta el SAT antes de crear |

**Regla de grupo cerrado** (`WM_GRUPO_CERRADO`, 5.º método de `ligar_cfdis`). Aplica a N CFDI del mismo receptor, UPC, total y piezas que solo pueden ser de N peticiones libres, y esas peticiones de ningún otro CFDI. Se emparejan en orden de folio y `cfdiid`. Medida contra el crawler en octubre (tabla temporal):

| Modo | Ligas nuevas (oct + nov) | Octubre | Errores contra el crawler |
|---|---|---|---|
| `orden` (por defecto): solo si las N peticiones son de la misma orden | 21 | 207 de 232 (89 %) | 0 |
| `cliente`: también entre órdenes del mismo cliente | 71 | 227 de 232 (98 %) | 2: CFDI cruzados entre dos órdenes del mismo cliente |

**Prueba en Odoo pruebas (25-nov, staging, timbrado apagado).** El diario `WALM` (id 231) se creó en la copia de pruebas, porque esta es del 13-nov y Finanzas lo creó después en prod.

| Qué | Resultado |
|---|---|
| 03, CFDI de noviembre | **14 facturas en el diario WALM, nombradas `FI-4384`, `FI-4714`…**, dif $0.00. No consumieron la secuencia INV: la siguiente de 04 fue INV/2026/255798 |
| IVA | FI-4384: Dr 105.01.007 1,149.00 / Cr 401.01.001 990.52 / **Cr 209.01.001 158.48** / Dr 501.01.001 640.80 / Cr 205.06.003 640.80. Las 10 de público general de 04 (INV/2026/255798 a 255807) también van a la 209 |
| C1, SO10352113 | INV/2026/171541 → `WM_ADOPTADA_REVISAR`: "cliente Walmart (sin RFC) vs receptor GCA951222FA5; CxC 105.01.001; diario Customer Invoices". Las otras 4 piezas ya estaban importadas |
| C2, SO10339388 | INV/2026/171511 (UUID 8B982DAA) → `WM_ADOPTADA_REVISAR`: "total 1,605.00 vs CFDI 3,270.01; cliente Walmart; CxC 105.01.001". El CFDI C10C6640 → `WM_LINEA_REVISAR`: la línea GARDENSET3 ya la facturó 171511, así que **no se crea nada cruzado** (antes salió INV/2026/255564, que se canceló en pruebas) |
| Facturas del proceso manual (octubre) | Las 111 que 03 había adoptado → `WM_ADOPTADA_REVISAR`. Las 111 tienen cliente "Walmart" sin RFC, CxC 105.01.001 y diario INV; 1 además con total distinto. Es la lista para el traslado de 2026 (C3) |
| Grupo cerrado (modo `orden`) | 17 CFDI de noviembre ligados de más; se crearon junto con los 9 que el SAT no respondió el 24-nov |
| 05, completitud contra el SAT | 767 CFDI de Fénix descargados: 163 en Odoo, 162 sin ligar y 442 ligados sin factura. Los 442 son en su mayoría las 111 por corregir y CFDI de ventas del 13 al 23-nov, fuera de la ventana de la prueba |

Emparejar por fecha en lugar de folio cruzaba 8 de 24. Un CFDI en la orden equivocada da el mismo asiento, pero confunde la NC de una devolución, por eso el modo por defecto es `orden`.

## Cobros (fuera de esta fase; se hacen en otra sesión)

Este flujo **solo factura**. Walmart no tiene cobros automáticos (CLAUDE.md §7). Mientras tanto Finanzas los sigue aplicando como hoy. En Odoo una factura queda pagada cuando su línea de CxC (105.01.007) se concilia con un pago (`account.payment.register`): `payment_state` pasa a `paid`, o a `partial`. Así lo hacen ML (04) y Amazon (03).

**Cuentas ya creadas por Finanzas (25-nov):** 102.03.008 Walmart (transitoria de cobros) y diario **WMT**. Al cobrar, el IVA pasa solo de la 209 a la 208, porque el impuesto es al cobro.

**Qué pide Finanzas** (`6. flujo_facturacion_walmart.html`, §6):
1. Tomar las órdenes cuya fecha de liberación cae en el periodo.
2. Registrar un pago por factura, por el total con IVA (`price_amount`), que salde la CxC.
3. Asiento: Dr "Liberaciones Walmart Pendientes de Procesar" (**código pendiente**) / Cr 105.01.007. La cuenta puente se concilia después contra el banco, igual que en Amazon.
4. Las retenciones de IVA e ISR quedan para una fase 2.

**Qué trae FTM** (`wm_financial_transactions_master`, por línea, `type='Order'`):
- `fecha_pago_venta` / `venta`.
- `fecha_pago_comision` / `comision` y `costo_de_envio`.
- `killer` / `killercomision`, `penalty`, `reclamo` y los cargos de WFS (inventario extraviado, almacenaje, envío, manejo), cada uno con su fecha de pago.
- `withholding_iva_amount`, `withholding_isr_amount`, `refund_amount`.
- Las devoluciones van en filas `type='Refund'`.
- No hay id de depósito ni de liquidación.
- **De dónde salen:** del reporte de pagos de Walmart, `somos_reyes.wm_payments_report`. Tiene un renglón por línea y concepto:
  - **Conceptos:** Venta, Comision, Killer, Costo de envio, Retencion IVA/ISR, WFS, Penalty, Reclamo, Publicidad.
  - **Último pago:** 18-nov, cargado el 21-nov.
  - **Retenciones:** el reporte las trae como egreso, así que el depósito llega neto de retenciones.
  - **Para cuadrar contra el banco** conviene usar este reporte directo, no FTM.

**Perfil de sep–oct-2026** (solo lectura, 24-nov; 4,197 líneas de 4,141 órdenes):

| Dato | Resultado |
|---|---|
| Líneas con `fecha_pago_venta` | 4,108, repartidas en **11 fechas** (9-sep a 18-nov): Walmart libera por lotes |
| `venta` contra `price_amount` | Iguales en 3,979 de 4,035 órdenes de una línea. `venta` es el total con IVA sin restar retenciones. Las 56 restantes no se han revisado |
| `venta` repetido por orden (advertencia del Playbook) | No aparece en esta ventana: en las 55 órdenes de varias líneas, la suma de `venta` es igual a la de `price_amount`. Revisar otros meses |
| `comision` | En las 4,108 líneas pagadas, siempre con la misma fecha que la venta: el depósito llega neto |
| Otros cargos | `killer` en 1,764 líneas, `costo_de_envio` en 1,809, envío WFS en 167, `penalty` en 0 |

**Antes de diseñar:**
1. **Cuadrar contra el banco** (regla 6). La suma de todos los conceptos de una `payment_date` de `wm_payments_report` (ingresos menos egresos, retenciones incluidas) tiene que igualar el depósito del estado de cuenta. Hasta que cuadre, FTM no sirve como fuente de cobro.
2. **Preguntas a Finanzas:**
   - ¿Cuál es el código de la cuenta puente?
   - ¿Qué cuentas llevan comisión, envío, killers, cargos de WFS y retenciones? El HTML solo cubre el cobro bruto.
   - ¿Cómo aplican hoy los cobros de Walmart, y contra qué CxC? Las cargas manuales usaban la 105.01.001; este flujo usa la 105.01.007.
   - ¿Cómo se cobra una orden con doble CFDI o con NC?
3. **Datos:** ¿las fechas de pago de FTM se llenan después, sobre líneas ya cargadas? Eso decide cada cuánto relee el miner.

**Propuesta** (patrón de Amazon 02 → 03, ver `README_AMAZON.md`), en su rama `feat/cobros-walmart` desde `main`:
- **Miner:** una fila por orden liberada en `finance.mkp_payments_{entorno}`, con `payment_id = '<fecha_pago_venta>:<customer_order_id>'` porque no hay id de depósito.
- **Loader:**
  - `account.payment.register` contra cada factura de la orden (`AUTOINV:<SO>` y las `AUTOINV:<SO>:<UUID>`; una orden puede tener varias), en el diario de la cuenta puente.
  - Solo si la factura está en la 105.01.007 (`CXC_INCORRECTA`).
  - Redondeo hasta $0.15 por factura contra 704.23.001.
  - Las escrituras con `OdooWriteUncertain` se confirman leyendo.
- No se aplica cobro a una orden en `DOBLE_CFDI`, porque una de sus facturas se va a cancelar (CLAUDE.md §8).

## Pendiente antes de producción (24-nov-2026)

**Desarrollo (nuestro):**
1. ~~Segunda descarga de cada día~~ **hecha el 24-nov** (`WM_SAT_DIAS_DEFINITIVA`, ver "Descarga masiva del SAT").
2. ~~Ciclo de 02 con el SAT real~~ **probado** (23 y 24-nov, ver arriba).
3. **Ligado manual.** Hoy Finanzas ve los `SIN_LIGAR` en el CSV de 05, pero no puede cargar la liga. Falta una entrada `uuid,orden` (CSV) que lea 02. Con el crawler apagado es el 0.7 % de las órdenes en un mes normal, y más en Buen Fin.
4. ~~Regla de grupo cerrado~~ **hecha el 25-nov** (modo `orden`, 0 errores).
5. **Validar las ligas de noviembre** cuando Data corra el crawler una vez (373 ligas sin verificar).
6. **Corrida de noviembre completo** (ventas del 13 al 30) en una copia nueva de Odoo pruebas. La actual llega al 13-nov. **No frena la salida.**
6b. **Traslado de 2026 al diario WALM** (C3): mover las facturas importadas del proceso manual y corregir cliente (receptor del CFDI) y CxC (105.01.007) desde el 31-jul. Base: `credit_note_refund_processes/traslado_diarios_terceros.py` (el de MeLi). La lista son las filas `WM_ADOPTADA_REVISAR`. **No frena la salida:** el flujo nuevo no las toca, solo las reporta.
6c. **Revisión periódica de dobles CFDI** (D1): cancelar en el mismo mes o hacer NC en un mes posterior. Va después de la auditoría contra el SAT de Luis (D2). Hoy el flujo los detecta (`DOBLE_CFDI`), pero no los cancela.
6d. **IVA 209 en ML y Amazon** (A1): rama propia (`fix/iva-209-ml-amazon`), porque son flujos en producción.

**Finanzas (25-nov):** respondieron A1–A4 y B1–B4 (ver "Respuesta de Finanzas"). Para salir falta:

7. Visto bueno a la prueba del 25-nov: FI-xxxx en el diario WALM y el IVA en la 209 (D5).
8. Confirmar que en producción el diario se llama exactamente `WALM` y es de tipo venta.
9. ~~Gracia, cliente = receptor, bicis eléctricas~~: resuelto (7 días, sí y sin trato especial).

**Finanzas y Operaciones (no frenan la salida):**
- Cancelar las 15 facturas con doble CFDI de octubre y los borradores ajenos, **en Odoo producción y ante el SAT**. Son facturas reales del proceso manual; la copia de pruebas se desecha. El flujo ya los deja en `DOBLE_CFDI` y `BORRADOR_AJENO`.
- Revisar con Operaciones la salida de WFS.

**Infraestructura y salida:**

10. **Tablas `_staging` y `_prod`.** Todas las pruebas usan `finance.mkp_billing_staging` y Odoo pruebas. `_prod` está vacía para Walmart. El cambio lo hace una sola variable, `WM_ENTORNO=prod`, que ya trae el YAML de Kestra: entra en vigor cuando el flujo llega a `flows/` en `main`.

   El repositorio de CFDI y las solicitudes al SAT no dependen del entorno: el mismo día vale igual en staging y en prod. Antes de la primera corrida de prod se copian de staging, para no volver a pedir días que ya gastaron solicitudes:

   ```sql
   INSERT IGNORE INTO finance.mkp_billing_prod
     (marketplace, mkp_order_id, cfdi_uuid, total_amount, xml_data, raw_json, status,
      error_log, retry_count, retry_count_loader, processed_at)
   SELECT marketplace, mkp_order_id, cfdi_uuid, total_amount, xml_data, raw_json, status,
          error_log, retry_count, retry_count_loader, processed_at
   FROM finance.mkp_billing_staging
   WHERE marketplace = 'SAT_DESCARGA'
      OR (marketplace = 'WALMART_FENIX' AND mkp_order_id LIKE 'CFDI:%');
   ```

   No se copian las filas `WALMART`, porque apuntan a facturas de Odoo pruebas, ni las `PRECFDI`, que 00b reescribe en cada corrida.
11. **Secrets en Kestra:** `SAT_FIEL_*` y `FENIX_WALMART_*`, en el `.env` del EC2.
12. **Orden de salida:**
   1. PR con `requirements.txt` y el Dockerfile.
   2. Reconstruir `wonderbrands-python:v2.0`.
   3. Dar de alta los secrets.
   4. PR con `processes/` y `flows/`.

   Si el YAML llega a `main` antes que la imagen o los secrets, la corrida de las 10:30 falla.
13. **Corte:**
    - Fijar una fecha de arranque (`start_date`). Ese día se dejan de correr los dos scripts manuales. `Team_Walmart` ya está fuera del 1a1, y la revalidación ve las facturas del proceso manual.
    - Los primeros días, con `max_facturas` bajo.
14. **Fuentes diarias:** FTM y `wm_order_line` tienen que cargarse a diario. El crawler es opcional: sin él se liga del 66 % (Buen Fin) al 87 % (octubre); con él, el resto. Automatizarlo (hoy es Selenium a mano) quitaría casi toda la liga manual.
