# Walmart 3P — Facturación automática (primera versión)

Rama `feat/facturacion-walmart`. Análisis y decisiones pendientes: [ANALISIS_FACTURACION_WALMART.md](ANALISIS_FACTURACION_WALMART.md).

## Quién factura cada venta

| Fuente (`somos_reyes`) | Para qué |
|---|---|
| `wm_financial_transactions_master` (FTM), `type='Order'` | Qué vendió Walmart. Una fila por línea (`so_prime_line_sub_line_no`) |
| `wm_fenix_invoices_report` (Fénix) | Qué facturó el autofacturador de Walmart con nuestro RFC. **Un CFDI por pieza**: `iderp` = línea, `cantidad` = 1 |
| `odoo_new_sale_order_live` | La SO (`channel_order_reference` = `customer_order_id`) |
| Web service de descarga masiva del SAT (e.firma de `SRE1410278MA`) | El XML de cada CFDI de Walmart. Fénix solo da el UUID. Respaldo: el ZIP de Finanzas |

Solo las ventas autofacturadas en Walmart (~8 %) necesitan el XML del SAT. El resto, que es la mayoría, las timbra 04 en Odoo.

Por línea: las piezas con CFDI vigente de Walmart (`CON_XML_PDF`) igualan las vendidas → **la timbró Walmart**. Si Fénix no tiene nada → **la timbramos nosotros**. Cualquier otro caso queda en espera.

## Scripts

| Script | Hace | Escribe |
|---|---|---|
| `_wm_common.py` | Conmutador `WM_ENTORNO`, Odoo, MySQL, CFDI, funciones de Odoo (de `amazon/01`) | — |
| `00_wm_precheck.py` | Candado de entorno, Odoo, cuentas, equipo, frescura de FTM y Fénix | — |
| `01_wm_miner.py` | Clasifica cada orden y llena la tabla de control | MySQL |
| `_sat_descarga.py` | Cliente del web service de descarga masiva del SAT v1.5: autenticación, solicitud, verificación y descarga, firmadas con la e.firma | — |
| `test_sat_descarga.py` | Pruebas del firmado (sin SAT y sin la e.firma real) | — |
| `02_wm_xml_loader.py` | Pide y descarga del SAT los XML de los CFDI de Walmart y los guarda en sus filas `PENDING_WM_XML`. Respaldo: ZIP o carpeta (`WM_XML_PATH`) | MySQL |
| `03_wm_loader_pull.py` | Replica cada CFDI de Walmart en Odoo, **sin timbrar** | Odoo + MySQL |
| `04_wm_billing_push.py` | Factura a público general por SO y la timbra (solo en prod) | Odoo + MySQL |
| `05_wm_control.py` | Conteos, alertas, cruce contra el CSV de Finanzas | CSV + Slack |

Kestra (borradores en `kestra/`, no en `flows/`): `wm_billing_flow` (00→01→02→03→04→05, diario 10:30 CDMX) y `wm_carga_xml_flow` (02 con el ZIP, manual, solo de respaldo).

## Descarga masiva del SAT (02)

**Cómo pide.** El SAT es asíncrono: lo que se pide en una corrida se descarga en otra.

1. **Avanza lo pedido antes.** Verifica las solicitudes abiertas; si están terminadas, descarga sus paquetes (ZIP en base64) y guarda cada XML en su fila.
2. **Pide por día.** Para cada día con CFDI de Walmart sin XML (fecha de Fénix), pide los CFDI **emitidos** por `SRE1410278MA`, tipo I, vigentes, de 00:00:00 a 23:59:59. Solo días ya cerrados.
3. **Pide por folio.** Lo que siga faltando después de descargar su día se pide UUID por UUID.

Los XML que no son de Walmart se descartan; no hay repositorio intermedio.

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

**Imagen.** Hay que reconstruir la imagen con `build_and_publish_docker_image` usando `docker/python_librariesv2.Dockerfile`. Para no tocar los flujos que ya usan `wonderbrands-python:v1`, conviene publicarla con un tag nuevo (por ejemplo `v1.1`) y apuntar solo los flujos de Walmart a ese tag mientras se prueba.

## Tabla de control

`finance.mkp_billing_{staging,prod}`, `marketplace='WALMART'`.

- `mkp_order_id = <customer_order_id>`: una fila por orden que factura WB o que está en espera.
- `mkp_order_id = <customer_order_id>:<UUID>`: una fila por CFDI (pieza) de Walmart, con `cfdi_uuid` y `xml_data` (base64).

| Estado | Significa | Lo toma |
|---|---|---|
| `PENDING_WB_INVOICE` | Factura WB | 04 (después de la gracia) |
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
| `ERROR` | Se reintenta hasta 3 veces | 03 / 04 |

## Blindajes (CLAUDE.md §6)

- `concurrency: limit 1, behavior: FAIL`, sin retry.
- Revalidación fail-closed antes de cada `create` (`facturas_existentes`: `invoice_origin`, `ref AUTOINV:<SO>%` y `invoice_ids`, que ve las globales y las del proceso manual con `ref=''`).
- Idempotencia: `AUTOINV:<SO>` (WB) y `AUTOINV:<SO>:<UUID>` (Walmart).
- Escritura con red caída → `OdooWriteUncertain` → se busca por `ref`, nunca se reintenta.
- 04 no emite si Fénix tiene más de `WM_FENIX_MAX_HORAS` sin cargarse, y vuelve a consultar Fénix justo antes de emitir.

## Probar en local

Desde esta carpeta, con el Python del `.venv` del repo. Por defecto `WM_ENTORNO=staging` (tablas `_staging`, `ODOO_TEST_URL`, timbrado apagado, tope de 20 facturas).

```powershell
$env:WM_DRY_RUN = "1"; $env:WM_START_DATE = "2026-10-01"; $env:WM_END_DATE = "2026-11-01"
..\..\..\..\.venv\Scripts\python.exe 01_wm_miner.py          # solo lee
Remove-Item Env:WM_DRY_RUN
..\..\..\..\.venv\Scripts\python.exe 00_wm_precheck.py
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

## Pendiente antes de producción

1. Decisiones de Finanzas: días de gracia, doble CFDI, SO cancelada, órdenes mixtas, Fénix en `ERROR` (ver el análisis, §10).
2. Yanina (ETL de marketplaces): que Fénix y FTM se carguen a diario. Fénix lleva más de 10 días sin carga, y así 04 no emite.
3. Finanzas revisa los asientos de la prueba (INV/2026/255545 a 255584) y Operaciones, la salida de WFS (8 `Delivered` sin salida).
4. Finanzas confirma: cliente de la factura = receptor del CFDI; lista de SKU de bicis eléctricas; cancelar los borradores ajenos y las 6 (21 en total) facturas a público general con doble CFDI.
5. Secrets de la e.firma en el `.env` de Kestra y reconstruir la imagen con `cryptography`.
6. Las filas `SAT_DESCARGA` dependen de `WM_ENTORNO` (staging y prod llevan su propio seguimiento). Como el SAT limita 2 solicitudes por parámetros, en prod conviene no repetir en staging las solicitudes por día.
6. Al activar: copiar a `processes/` y `flows/` por PR y dejar de correr los dos scripts manuales.
