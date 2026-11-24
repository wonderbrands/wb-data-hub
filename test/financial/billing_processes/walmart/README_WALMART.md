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
3. **Pide por folio.** Lo que siga faltando después de descargar su día se pide UUID por UUID.

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
| FTM (`wm_financial_transactions_master`) | ETL de Data | Ventas, devoluciones y cobros. Las columnas `wm_invoice_uuids`, `wm_invoice_count`… **salen del crawler**: los 1,622 UUID están en el crawler y noviembre tiene 1 | Fuente independiente de UUID | 22-nov |
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

## Pendiente antes de producción

1. Decisiones de Finanzas: días de gracia, doble CFDI, SO cancelada, órdenes mixtas, Fénix en `ERROR` (ver el análisis, §10).
2. FTM y `wm_order_line` tienen que seguir cargándose a diario (hoy al día). El crawler de Fénix es opcional: sin él se liga ~87 %; si Data lo automatiza (hoy es Selenium en Windows, a mano), liga el resto. Si no, Finanzas liga a mano los `SIN_LIGAR` del CSV de 05.
3. Finanzas revisa los asientos de la prueba (INV/2026/255545 a 255624) y Operaciones, la salida de WFS.
4. Finanzas confirma: cliente de la factura = receptor del CFDI; lista de SKU de bicis eléctricas; cancelar los borradores ajenos y las 15 facturas a público general con doble CFDI de octubre (6 ya en el correo del 23-nov, 9 nuevas).
5. Kestra: secrets `SAT_FIEL_*` y `FENIX_WALMART_*` en el `.env` del EC2; fusionar el Dockerfile a `main` y reconstruir `wonderbrands-python:v2.0`.
6. Las filas `SAT_DESCARGA` dependen de `WM_ENTORNO`. El SAT limita 2 solicitudes por parámetros: en staging no repetir las solicitudes por día que producción va a necesitar (por eso 02 corrió sin e.firma en la prueba del 23-nov).
7. Ligado manual: hoy Finanzas ve los `SIN_LIGAR` en el CSV de 05, pero no hay forma de cargar su liga. Falta una entrada simple (por ejemplo un CSV `uuid,orden` que lea 02).
8. Al activar: copiar a `processes/` y `flows/` por PR y dejar de correr los dos scripts manuales.
