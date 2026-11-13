**Asunto:** RE: Notas de crédito MeLi · barrido, numeración de CFDI de terceros, retornos y refacturas

Hola Jero,

Respondo por punto. Todo lo probé en el staging nuevo, copia de producción del 13-nov:
**https://somosreyes-test-13-10-2026-41883942.dev.odoo.com**

**Adjuntos**
- `Lote_prueba_NC_ML_v5_staging_2026-11-13.csv`: 14 pruebas (retornos, NC y facturas con serie-folio), con enlaces.
- `Piloto_NC_ML_productivo_2026-11-11.csv`: las 10 NC del piloto en productivo.

**1. Barrido y piloto**
- **Barrido:** sí, consultó a MeLi las ~25k órdenes que faltaban y terminó bien. La cola cubre las 167,216 órdenes facturadas por el autofacturador desde el 1-jun; ahora solo quedan las que se van venciendo por calendario. Hay 4,075 NC descargadas (eran 3,715 al 3-nov).
- **Aclaración:** el barrido baja las NC de las órdenes ya facturadas. Las facturas y NC de las órdenes canceladas (~5,500 pares) siguen pendientes del ajuste al flujo de facturación.
- **Piloto:** ya está en productivo. Son 10 NC de octubre (A, B y C; sin cobrar, cobradas y parcialmente cobradas; centavos; 3 con retorno reclasificado en su fecha y conciliado contra la NC), todas verificadas.
- **Siguiente:** con su VoBo del piloto cargo octubre (780) y noviembre. Tomado lo del cierre de octubre.

**2. Numeración de los CFDI de terceros**
Hay una restricción de Odoo que define la solución: **Odoo numera el siguiente documento de un diario a partir del folio más alto que haya en ese diario, sin importar la serie.** Lo confirmé en staging:

| Prueba | Resultado |
|--------|-----------|
| NC de MeLi en los diarios de Somos Reyes (DEV/BON) | ❌ La siguiente NC **manual nuestra** salió MELINC-11837 y MELINC-11814. |
| Factura de MeLi renombrada dentro de INV | ❌ La siguiente factura **propia** salió MELI-TEST-2. Además, el folio de MeLi ya va en 188,535 y el de INV en 255,525: compartir diario chocaría pronto. |
| NC de MeLi en diarios exclusivos (NC MeLi Devoluciones y NC MeLi Bonificaciones) | ✅ MELINC-11844, MELINC-11840 y MELINC-11820, con su contabilidad correcta. Las manuales nuestras siguen con RDEV/2026/01032 y RBON/2026/00079. |
| Facturas en un diario exclusivo "Facturas MeLi" | ✅ Se publican con su serie-folio, en cualquier orden de folio. |
| Traslado al diario exclusivo de facturas ya cargadas | ✅ Una cobrada (INV/2026/254710 → MELI-188535) y una cobrada y enviada (INV/2026/251864 → MELI-186179) quedaron con el mismo UUID, XML y asiento, y con el cobro y la 205.06.003 conciliados como antes. |
| Traslado de una factura en periodo cerrado | Bloqueado por la fecha de bloqueo: requiere la reapertura. |

Mi propuesta:
- **Flujo recurrente:** diarios exclusivos de terceros con nombre = serie-folio del CFDI: Facturas MeLi (MELI-xxxx), NC MeLi (MELINC-xxxx) y Facturas Amazon Business (AB-xxxx).
- **Histórico:** trasladarlo a esos diarios con el procedimiento probado: borrador → quitar nombre → diario y serie-folio → publicar → volver a conciliar cobro, NC y 205.06.003. Funciona porque son CFDI de terceros que Odoo no timbró; los timbrados por nosotros no se pueden regresar a borrador.
  - **Volumen:** ~170,400 facturas de MeLi (Regular y Reventa), 121 de Oficiales, 152 de Amazon Business y las 10 NC del piloto.
  - Oct–nov se puede hacer ya; jun–sep, con los periodos abiertos.
  - Les paso el tiempo estimado antes de pedir la ventana.

Necesito que decidan, con quien administre Odoo:
- **(a)** Para las NC de MeLi: dos diarios (devoluciones y bonificaciones, como en la prueba) o uno, con el tipo A/B/C por cuenta.
- **(b)** La fecha de arranque de los diarios nuevos.

Es un cambio en el flujo de facturación (MeLi y Amazon), así que lo trabajo en su propia rama. Mientras tanto las NC siguen entrando con RDEV/RBON, como el piloto, y van en la lista del histórico.

**3. Retornos**
- **Alcance:** confirmado, se reclasifican todos los retornos validados de órdenes de MeLi con salida de almacén, sin importar el tipo de NC (una cancelación con retorno también). Entran Regular y Reventa; Oficiales entra cuando sumemos su flujo de NC.
- **Corte:** ya aplicado, solo órdenes que entraron al flujo nuevo de NC (las facturadas por el autofacturador desde el 1-jun). En staging:
  - 579 retornos de periodo abierto ($759k) se reclasificaron en su fecha, sin errores.
  - 567 de jun–sep ($623k) van en la reapertura.
  - 303 de órdenes fuera del flujo no se reclasifican.

**4. Refacturas (401.01.006)**
Su esquema es viable: F2 ya está en Odoo (401.01.001 con costo), pero es un CFDI de MeLi, así que se puede regresar a borrador para cambiarla. Implica:
- Las 28 F2 son de jul–sep: se corrigen con los periodos abiertos, rehaciendo la conciliación de su cobro.
- "Sin costo" en F2 y en la NC: Odoo genera el costo solo para las líneas de producto inventariable. Para que no lo genere, esas líneas van con un producto de servicio (como BON_SRV) a 401.01.006. La liga a la SO queda a nivel documento (origen), no por línea de producto.

Una variante con el mismo neto, sin tocar F2: F2 queda como la venta (401.01.001 con costo) y F1 + NC entran contra 401.01.006 sin costo. Solo cambia qué documento lleva la venta, normalmente en el mismo mes: en MELINC-2195, F2 es del 12-jul y la NC del 14-jul.

Pruebo las dos en staging con 3 refacturas y ustedes eligen. Para F1 necesito su XML, porque la API de MeLi solo entrega la factura vigente. ¿Pueden bajar del SAT los 28 XML? Les paso la lista de UUID.

**Sobre los 5 casos "sin reembolso":** los revisé y no son refacturas. MeLi emitió la factura y su NC el mismo día, sin otra factura vigente. Siguen en revisión manual.

**5. Reapertura**
Les aviso en cuanto esté lista la prueba en staging de Reventa (fechas y centavos).

**Necesito de ustedes**
1. El VoBo del piloto, para cargar octubre y noviembre.
2. Las decisiones (a) y (b) del punto 2.
3. Los 28 XML de F1 del SAT, para probar las dos opciones de refacturas.

Saludos,
Serch
