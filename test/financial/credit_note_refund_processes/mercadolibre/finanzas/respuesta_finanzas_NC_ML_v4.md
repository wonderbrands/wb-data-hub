**Asunto:** RE: Notas de crédito MeLi · retorno con fecha del retorno listo, piloto y respuestas (puntos 4 y 7)

Hola Jero,

Gracias por el VoBo. Respondo siguiendo su hoja de ruta.

**Adjunto:** `Lote_prueba_NC_ML_v4_retornos_staging_2026-11-11.csv`, 7 casos de retorno, **todos OK**, con el enlace a cada asiento en staging.

| # | Acción | Estado | Detalle |
|---|--------|--------|---------|
| 1 | Piloto de 10 NC de octubre | ⏳ listo | Lo corro en cuanto cierre el barrido de NC (hoy). Antes creo en productivo la posición fiscal del tipo B y pongo la 402.01.002 en el diario BON y en BON_SRV. Al terminar les mando los 10 enlaces. Después cargo octubre (780 NC) y noviembre (318). Con eso pueden arrancar el cierre de octubre: octubre está abierto y no depende de la reapertura. |
| 2 | Reclasificación con fecha del retorno | ✅ probado | Cada retorno validado de MeLi genera Debe 205.06.003 / Haber 107.06.001 **en el mes del retorno**, exista o no la NC, conciliado contra el retorno. El crédito en 107.06.001 queda abierto hasta que entra la NC tipo B, que lo concilia al registrarse. Lote v4: NC antes del retorno, retorno antes de la NC (WH/DEV/07533 del 31-oct, conciliado al entrar su NC del 5-nov), devolución parcial, kit y retornos sin NC. En staging se reclasificaron 525 retornos de oct–nov ($684k); los 676 de jun–sep ($716k) van en la reapertura. Ver la pregunta abajo. |
| 3 | Reapertura y carga en orden | ⏳ | Propongo una primera ventana con lo que ya está listo, en este orden: facturas de Reventa a su mes, centavos de Reventa, retornos de jun–sep y NC de jul–sep (MELINC-7782 incluida). Les confirmo la hora en cuanto termine la prueba en staging de las fechas y centavos de Reventa. Los pares y las facturas por importar de jun–sep (puntos 4 a 6) van a necesitar una segunda ventana. |
| 4 | Pares de órdenes canceladas | ⏳ | Ver respuesta abajo. Primero la prueba de 5 pares en staging. |
| 5 | Flujo de facturación (TOTAL_DIFF) | ⏳ | Tomado: mapeo por motivo (centavos, envío, promociones, otros), importación de los de centavos al importe del CFDI con el cierre contra 704.23.001, y lista de órdenes sin SO para crearlas vía Yuju. |
| 6 | Refacturas | ⏳ | Ver respuesta abajo. |
| 7, 8 | Consulta a MeLi y revisión con almacén | — | De su lado. Con la regla nueva, las 14 NC tipo A con salida cancelada no generan reclasificación, y la bicicleta VINTA de WH/DEV/07291 queda fuera porque no salió en la orden. |
| 9 | Corrida en seco final y cruce | ⏳ | El barrido de NC termina hoy; se las mando en cuanto cierre. |
| 10–12 | Previo a junio, bloqueo de la emisión manual, complementos | — | Tomado. |

**Pares de órdenes canceladas (punto 4)**
El motivo es técnico. Para facturar una SO por el flujo normal de Odoo hay que confirmarla, y reconfirmar una SO cancelada vuelve a generar la orden de entrega y a reservar inventario. La alternativa que cumple lo que piden:
- La factura se liga línea por línea a la SO cancelada, **sin reconfirmarla**, con su costo (Debe 501.01.001 / Haber 205.06.003).
- Su NC tipo A también se liga a la SO y revierte el costo, así que la SO queda neta en cero.

Lo pruebo con 5 pares en staging, cada documento en el mes de su CFDI, y se los mando antes de cargar.

**Refacturas (punto 6)**
La factura vigente es correcta. INV/2026/38979 es el CFDI MELI-37970 que **timbró MeLi**; DataBot solo lo registra en Odoo, no timbra. La secuencia de MELINC-2195 fue:
1. MeLi facturó a público en general (F1, 596F7056…).
2. El cliente pidió factura con su RFC y MeLi emitió la F2 el 12-jul (605F8C1D…, a GERM730728763).
3. El 14-jul MeLi canceló la F1 con la NC MELINC-2195.

En Odoo está la F2, ligada a la SO. Falta el par F1 + NC, que se netea. De las 28 refacturas, 17 son cambio de receptor (factura con el RFC del cliente) y 11 son reemisiones al mismo receptor; las 28 tienen la F2 en Odoo y ninguna tiene la F1. Las registro como par ligado a su SO. El pendiente es conseguir el XML de cada F1, porque la API de MeLi por orden solo entrega la factura vigente. Lo busco por otra vía y, si hace falta, les pido la descarga masiva del SAT.

**Necesito de ustedes**
1. ¿Reclasificamos también los retornos de ventas anteriores a junio? Quedarían abiertos en 107.06.001 como retornos sin NC.
2. La hora de la reapertura, cuando les confirme que la prueba de Reventa está lista.

Saludos,
Serch
