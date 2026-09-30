# Reunión Finanzas — Amazon
### Guion de presentación · 28-ago-2026

---

## Cómo usar este documento

Cinco bloques con tiempo estimado. Los bloques 4 y 5 son los que necesitan decisión de Finanzas — si el tiempo se acorta, sacrifica detalle de los bloques 1 y 2, no de esos.

Al final hay dos anexos: uno con las explicaciones a fondo de los dos conceptos difíciles (para ti, antes de entrar), y otro con la ruta de notas de crédito.

**Encuadre general:** no llegas con un retraso, llegas con un hallazgo. El análisis destapó que el reporte del que iba a colgar todo el proceso tenía importes duplicados. Si hubiéramos avanzado a la velocidad planeada, habríamos automatizado sobre datos malos. Ese es el mensaje.

---

# BLOQUE 1 — Dónde estamos (3 min)

> "Traigo tres cosas: lo que encontramos al validar el reporte, el proceso de facturación y cobros ya definido, y la ruta de notas de crédito.
>
> Empiezo por lo que encontramos, porque cambió el orden de todo lo demás."

**Estado en una línea:** facturación y cobros de Amazon están **diseñados y escritos**; faltan tres decisiones de Finanzas para poder probarlos en el ambiente de pruebas de Odoo.

Lo que ya está listo:

- Query del reporte BI 3.0 corregida
- Script de facturación dedicado para Amazon
- Miner + loader de aplicación de cobros
- Tablas de control y plan de corte con el proceso actual

---

# BLOQUE 2 — Lo que encontramos en el reporte (7 min)

> "Antes de escribir una sola línea de facturación, validamos el reporte contra los datos crudos de Amazon. Encontramos tres problemas. Dos de ellos habrían provocado errores fiscales."

## Problema 1 — Importes al doble

**Qué pasaba:** el reporte cruzaba con una tabla de órdenes que tiene varias filas por orden. Al sumar, todos los importes se multiplicaban.

**Cuánto:** en la muestra que revisamos, **49 de 94 ventas traían el importe al doble**.

**Ejemplo concreto** — orden `701-8131997-1237840`:

| | Reporte decía | Amazon dice |
|---|---|---|
| Cantidad | 2 piezas | **1 pieza** |
| Producto | $2,782.82 | **$1,391.41** |
| IVA | $445.26 | **$222.63** |

> "Si hubiéramos facturado con este reporte, más de la mitad de los CFDI de Amazon habrían salido al doble. Eso es un problema fiscal, no un problema de reporte."

**Cómo lo detectamos:** el reporte trae un campo de cabecera que Amazon manda ya calculado y que no pasa por la suma. Ese campo decía $1,087.38 mientras la suma decía $2,174.76 — exactamente el doble. Ahí saltó.

**Estado:** corregido. La corrección está en `AMZ_facturacion_corregida.sql`, documentada línea por línea.

## Problema 2 — La misma venta aparece varias veces

**Qué pasa:** Amazon publica cada venta en dos momentos. Primero como **retenida** (`DEFERRED`), cuando aún no libera el dinero. Después como **liberada** (`RELEASED`), cuando lo deposita. Las dos versiones conviven en la tabla.

Y hay un segundo nivel: aun quitando las retenidas, **la misma pieza puede aparecer en dos liquidaciones distintas**.

**Ejemplo concreto** — orden `702-2265086-8559438`, que son 2 piezas del mismo SKU:

| Pieza | Liquidación | Estado | Depósito |
|---|---|---|---|
| A | 27044880571 | Liberada | 30-jul |
| B | 27044880571 | Liberada | 30-jul |
| **A otra vez** | 27172333181 | Liberada | 06-ago |

Son 3 renglones para 2 piezas.

> "Lo confirmamos por fuera: Amazon emitió dos facturas de $1,379.00 para esa orden, una por pieza. Dos, no tres."

**Riesgo si no se corrige:** aplicaríamos el cobro dos veces sobre la misma pieza. Amazon la depositó una sola vez.

**Estado:** resuelto. El proceso ahora identifica cada pieza por su identificador único y se queda con el registro más reciente. → *Esto conecta con la pregunta del Bloque 4.*

## Problema 3 — Descuentos contados dos veces

El reporte trae el mismo descuento en dos columnas: una con el total y otra con el detalle. Sumar ambas descuadra la factura.

**Ejemplo** — orden con envío gratis promocional: se cobran $50.86 de flete y Amazon los bonifica. Si se resta la bonificación dos veces, la base gravable queda mal y **el IVA deja de ser el 16% exacto**.

**Estado:** corregido. Agregamos campos ya calculados (`cfdi_base`, `cfdi_iva`, `cfdi_total`) para que nadie tenga que decidir qué columna sumar. Validado al centavo contra la muestra.

> **Si preguntan por qué no se había detectado:** el reporte cuadra a nivel total contra el depósito de Amazon; el error solo se ve al bajar a nivel orden. Es el tipo de cosa que aparece al automatizar, porque el script sí tiene que bajar a ese nivel.

---

# BLOQUE 3 — El nuevo proceso de facturación (8 min)

## La decisión: Amazon sale del proceso general

> "Hoy Amazon se factura con el mismo script que Walmart, Coppel, Elektra y TikTok. Ustedes nos pidieron tres reglas nuevas para Amazon. Si las metemos al script general, se las aplicamos a todos los marketplaces sin querer.
>
> Amazon tiene ahora su propio script. El general deja de tocar Amazon."

**Beneficio para ellos:** cambios futuros en Amazon no ponen en riesgo la facturación de los demás.

## Los cuatro requerimientos, uno por uno

**1. Excluir órdenes B2B** ✅

Se detectan por dos vías: la marca de Amazon Business en la orden, y —más confiable— la existencia real del CFDI en la tabla de facturas de Amazon Business.

> "Preferimos la segunda: no preguntamos si *debería* estar facturada, verificamos que el comprobante exista."

**2. Eliminar la restricción de OUT** ✅

Toda orden reconocida por Amazon se factura, haya salido físicamente o no.

> "La contrapartida va a Mercancía Facturada No Enviada, que ya funciona hoy. Si operaciones deja órdenes sin surtir, esa cuenta se infla — y entiendo que eso es justo lo que ustedes quieren ver para poder accionar."

**3. Factura individual por operación** ✅ — ya se hacía así.

**4. Verificar que no esté ya facturada** ✅

Se valida contra Odoo y contra el autofacturador de Amazon. Y hay una **validación de última hora**: justo antes de crear el CFDI se vuelve a preguntar a Odoo. Si esa consulta falla, **no se factura**.

> "Después del incidente de julio con las 549 facturas duplicadas, este script nace con esa defensa desde el día uno. Ante la duda, no factura."

## Periodo de gracia

Configurado en **5 días**: es lo que le damos al cliente para autofacturar con su RFC antes de emitir a público en general.

> "Es un parámetro, se cambia en un renglón. ¿Cinco días les parece? ¿O prefieren otro número?"

---

# BLOQUE 4 — Aplicación de cobros: dos cosas que hay que aclarar (10 min)

> "Aquí es donde más los necesito, porque los datos nos mostraron algo que no coincide con el flujo que ustedes documentaron."

## Aclaración 1 — Una factura puede cobrarse en varios settlements

**Lo que dice el documento de Finanzas (Paso 6):**

> *"El cobro se aplica por el total de la factura con IVA, saldando la CxC por completo."*

Eso supone que **cada factura pertenece a una sola liquidación**, y que cuando esa liquidación se libera, la factura queda saldada.

**Lo que muestran los datos:** las piezas de una misma orden pueden liberarse en liquidaciones distintas.

Volviendo a `702-2265086-8559438`:

```
Factura total: $2,758.00  (2 piezas × $1,379.00)

  30-jul   liquidación 27044880571   cobro de $1,379.00   → queda debiendo $1,379.00
  06-ago   liquidación 27172333181   cobro de $1,379.00   → saldada
```

**Por qué pasa:** Amazon libera fondos por envío y por pieza conforme se van confirmando. Una orden de varias unidades, o con envíos separados, se parte de forma natural.

**Lo que esto significa para ustedes — tres consecuencias prácticas:**

1. Una factura puede estar **parcialmente pagada** durante días o semanas. **No es un error.**
2. En un corte de mes van a ver saldos parciales en la cuenta de Amazon. **Tampoco es un descuadre.**
3. **No se puede conciliar** "total del settlement = suma de facturas saldadas". La conciliación correcta es contra la cuenta transitoria, no factura por factura.

**Lo que ya hicimos:** el script aplica el importe liberado en cada liquidación y nunca más que el saldo pendiente. Odoo maneja pagos parciales de forma nativa.

**Lo que necesito de ustedes:** confirmar que este comportamiento es aceptable contablemente, y ajustar el documento del flujo.

## Aclaración 2 — Necesito un estado de cuenta para validar

> "Les explico el problema en simple."

**El problema:** cuando la misma pieza aparece en dos liquidaciones, tenemos que decidir cuál es la buena. Nosotros elegimos **la más reciente**, porque todo apunta a que esa es la definitiva.

**El punto:** eso es **nuestra lectura de los datos**, no una regla que Amazon documente en ningún lado.

**Cómo se comprueba:** tomamos una liquidación completa, sumamos lo que nuestro criterio dice que Amazon debió depositar, y lo comparamos contra lo que el banco recibió ese día.

- Si cuadra al peso → el criterio es correcto y automatizamos con confianza.
- Si no cuadra → la diferencia nos dice cuál es la regla buena.

**Lo que pido, concreto:**

> "Necesito el estado de cuenta de dos depósitos: el del **30 de julio** (liquidación `27044880571`) y el del **6 de agosto** (liquidación `27172333181`). Con eso hago el cruce y les confirmo el criterio en un par de días."

> "Es la única forma de comprobarlo. Sin ese cruce estaríamos automatizando sobre una suposición, y ya vimos hoy a dónde lleva eso."

## Los asientos que genera el cobro

```
102.02.008  Liberaciones Amazon Pendientes de Procesar    Venta + IVA
    105.01.006  Amazon (CxC)                                          Venta + IVA
```

La transitoria permite conciliar contra el banco antes de dar el proceso por cerrado. La diferencia contra el depósito real son las comisiones de Amazon, que el proceso ya deja calculadas por orden y por liquidación.

---

# BLOQUE 5 — Lo que necesito para avanzar (5 min)

> "Tres decisiones y una validación. Con eso arranco pruebas."

## Decisión 1 — Fecha de inicio

Al quitar la restricción de OUT, todas las órdenes de Amazon que hoy no se facturan se vuelven facturables **de golpe**. Necesitamos una fecha de corte.

**Mi propuesta de secuencia:**

1. Les mando **casos de ejemplo facturados en el ambiente de pruebas de Odoo**
2. Ustedes validan asientos y cuentas
3. Con su visto bueno, fijamos la fecha de inicio
4. Salida a producción

> "No quiero fijar la fecha antes de que vean los asientos reales. Prefiero que primero validen los ejemplos."

## Decisión 2 — El diario contable

Necesito confirmar que existe en Odoo un diario de Amazon apuntando a **102.02.008 Liberaciones Amazon Pendientes de Procesar**.

> "El script se detiene si el diario apunta a otra cuenta. Es a propósito: si estuviera mal configurado, los cobros de Amazon se irían a bancos y el error no se vería hasta la conciliación."

**Pregunta directa:** ¿existe ese diario? ¿Con qué nombre?

## Decisión 3 — Campos nuevos en Odoo

El log de conciliación que pidieron necesita tres datos que Odoo no guarda hoy: **order_id de Amazon**, **settlement_id** y **tipo** (B2B / B2C con RFC / B2C público general).

Se pueden dejar como comentario en la factura, pero **así no se pueden filtrar ni exportar a Excel**.

**Mi recomendación:** crear tres campos personalizados en la factura. Quedan filtrables, exportables y consultables. Requiere permiso de administrador en Odoo.

> "¿Lo gestiono yo o va por su lado?"

## Validación

Los dos estados de cuenta del Bloque 4.

## Compromisos que adquiero

| Entregable | Cuándo |
|---|---|
| Ejemplos facturados en ambiente de pruebas | *(comprometer fecha)* |
| Cruce contra estados de cuenta | 2 días después de recibirlos |
| Reporte BI 3.0 corregido para su uso | Ya disponible |

---

# BLOQUE 6 — Notas de crédito: por dónde vamos (7 min)

> "Adelanto la ruta de devoluciones para que vean que no arranca de cero. Buena parte del trabajo de facturación se reutiliza."

## Buenas noticias

**1. La fuente de datos es la misma.** Las devoluciones viven en las mismas tablas que ya corregimos y validamos. No hay que construir ingesta nueva.

**2. Se puede identificar el SKU devuelto.** El reporte actual colapsa las devoluciones a nivel orden, pero el dato **sí existe** a nivel pieza. Cambiando el agrupado, tenemos el detalle.

> "Esto importa: significa que **sí podemos emitir notas de crédito parciales**. Si una orden de 3 productos devuelve 1, la NC va sobre esa línea, no sobre el total."

**3. Las notas de crédito de Amazon Business probablemente ya están en la base.** La tabla de comprobantes de Amazon tiene un campo de **tipo de relación de CFDI** — que es justo lo que identifica a una nota de crédito. Falta perfilarlo, pero si está, el flujo B2B se resuelve con un cruce, igual que en facturación.

**4. La clasificación A/B/C se reutiliza de Mercado Libre.** Ya existe el patrón; hay que adaptar el disparador.

## Las tres preguntas abiertas

**Pregunta 1 — ¿Cómo distinguimos Tipo A de Tipo B?**

El documento dice que la clasificación depende de si hubo despacho y retorno, pero deja la lógica exacta pendiente de definir con nosotros.

**Mi propuesta:**

| Tipo | Regla técnica | Asiento |
|---|---|---|
| **A** — Cancelación antes de despacho | Sin movimiento de salida en Odoo | Revierte Mercancía Facturada No Enviada |
| **B** — Devolución post-despacho | Con salida; el producto retorna | Devoluciones en Tránsito `107.06.001` |
| **C** — Bonificación sin retorno | Amazon lo marca explícitamente | Sin movimiento de costo |

> "¿Les hace sentido? Es la regla que voy a programar salvo que me digan otra cosa."

**Pregunta 2 — Apareció un caso que no está en el documento**

En la muestra encontramos movimientos de tipo **`LabmanLabelRefund`** — reembolsos de guía de envío, no devoluciones de producto. Cuatro casos en cien.

> "¿Esto lleva nota de crédito, o es un ajuste de gasto que no toca al cliente? No está contemplado en el flujo que nos pasaron."

**Pregunta 3 — Comisiones por devolución**

Amazon cobra una comisión por procesar cada devolución, y a veces reversa la comisión original de la venta. El documento de devoluciones no menciona su tratamiento.

> "¿Cómo lo quieren reconocer?"

## Ruta propuesta

| Fase | Contenido | Depende de |
|---|---|---|
| **1** | Miner de devoluciones + tabla de control | Nada — arranca ya |
| **2** | Motor de clasificación A/B/C | Respuesta a Pregunta 1 |
| **3** | Perfilado de NC de Amazon Business | Nada — arranca ya |
| **4** | Loader de NC en Odoo | Fases 1-3 + cuentas validadas |
| **5** | Reverso de cobro | Que cobros esté en producción |

> "Las fases 1 y 3 no dependen de nadie. Las arranco esta semana en paralelo a que ustedes validan los ejemplos de facturación."

**Punto de honestidad sobre la Fase 5:** el reverso del cobro es la parte más delicada. El documento dice que "se rompe la aplicación del cobro". Con lo que vimos hoy de los cobros parciales, romper y rehacer conciliaciones es frágil. Mi propuesta es conciliar la NC directamente contra la transitoria, que refleja mejor cómo se mueve el dinero. Lo traigo con detalle a la siguiente reunión.

---

# Cierre (2 min)

> "Resumo. Encontramos que el reporte tenía importes duplicados y lo corregimos. Facturación y cobros de Amazon están escritos, con las tres reglas que pidieron y con las defensas para no repetir lo de julio. Necesito de ustedes: los dos estados de cuenta, confirmar el diario contable, y validar los ejemplos que les voy a mandar del ambiente de pruebas.
>
> Notas de crédito arranca esta semana en paralelo, en las dos fases que no dependen de decisiones."

**Cierra pidiendo fecha de siguiente sesión.**

---
---

# ANEXO A — Para ti, antes de entrar

## A.1 El supuesto "un settlement = la factura se salda por completo"

**Dónde está.** Paso 6 del `flujo_facturacion_amazon.html`:

> *"Para cada settlement_id liberado en el período, identificar todas las órdenes Order Payment que pertenecen a ese grupo. Estas son las facturas a las que se aplicará cobro. [...] El cobro se aplica por el total de la factura con IVA, saldando la CxC por completo."*

**El modelo mental de Finanzas.** Una liquidación es una caja cerrada. Dentro van órdenes completas. Cuando Amazon deposita, todas las facturas de esa caja quedan saldadas. Relación uno a muchos: *un settlement → N facturas, cada factura en un solo settlement*.

Bajo ese modelo la conciliación sería: *total depositado = suma de las facturas del settlement − comisiones*. Limpio.

**Por qué no se cumple.** La unidad que Amazon libera **no es la orden, es la pieza**. Cada pieza tiene su propio ciclo: puede quedar retenida por revisión de fraude, por reclamo abierto, o simplemente porque se envió después.

Así que la relación real es *muchos a muchos*: **una factura puede tocar varios settlements**.

**El caso que lo prueba** — `702-2265086-8559438`, 2 piezas del SKU KHCESPIB:

```
Pieza 164910950935841  →  settlement 27044880571  →  depósito 30-jul  →  $1,379.00
Pieza 165346845825161  →  settlement 27172333181  →  depósito 06-ago  →  $1,379.00
                                                              Factura:   $2,758.00
```

Siete días entre un cobro y el otro. Del 30 de julio al 6 de agosto, esa factura tiene saldo pendiente de $1,379.00 en la cuenta de Amazon. **Correcto, no un error.**

Y cruza el cierre de mes: al 31 de julio la factura aparece a medio pagar.

**Los tres puntos que tienes que dejarles claros:**

1. Saldo parcial en `105.01.006` no es un descuadre; es una pieza aún no liberada.
2. La conciliación no se hace factura por factura contra el settlement. Se hace contra la transitoria `102.02.008`.
3. El documento del flujo hay que corregirlo en ese punto.

**Si te preguntan "¿y esto qué tan seguido pasa?"** — Sé honesto: en la muestra de 100 filas vimos un caso claro. Lo correcto es medirlo sobre un mes completo, y eso lo puedes tener rápido: es una consulta sobre las tablas ya corregidas. Ofrécelo como entregable.

---

## A.2 Por qué necesito los estados de cuenta

**El problema en una frase.** Amazon nos manda la misma pieza dos veces, en dos liquidaciones distintas, con dos fechas de depósito distintas. Tenemos que decidir cuál es la real.

**Qué decidimos.** Quedarnos con **la más reciente**.

**Por qué creemos que es la correcta.** Las liquidaciones cierran unos 15 días después. Durante ese periodo Amazon reacomoda: una pieza que estaba en la liquidación de julio puede terminar pagándose en la de agosto. El registro más reciente refleja dónde acabó realmente el dinero.

**Por qué no basta con creerlo.** Amazon no documenta esta regla en ningún lado. Es una inferencia nuestra, muy razonable, pero inferencia.

Y el costo de equivocarse es asimétrico:

- Si elegimos mal el registro → cobramos por el monto equivocado
- Si elegimos ambos → **cobramos dos veces la misma pieza**

Ninguno de los dos se detecta solo. Aparecen meses después como un descuadre en la cuenta de Amazon que nadie sabe explicar.

**Cómo se comprueba — el cruce, en tres pasos:**

1. Tomo la liquidación `27044880571` completa
2. Sumo lo que mi criterio dice que Amazon depositó el 30 de julio
3. Lo comparo contra el abono real en el estado de cuenta de ese día

Resultados posibles:

| Resultado | Significado |
|---|---|
| Cuadra al peso | Criterio correcto. Automatizamos. |
| Difiere por el monto de una pieza | Estamos incluyendo o excluyendo de más. Se corrige. |
| Difiere por otra cosa | Hay un concepto que no estamos considerando. Aparece al restar. |

Los tres desenlaces son útiles. **Por eso el cruce vale la pena aunque cuadre.**

**Cómo pedirlo, en corto:**

> "Necesito el estado de cuenta de dos depósitos de Amazon: el del 30 de julio y el del 6 de agosto. Solo el monto y la fecha del abono. Es para confirmar que la regla que programé reproduce exactamente lo que Amazon deposita — si no lo verifico contra banco, estaríamos automatizando sobre una suposición."

**Si te dicen "confía en el dato de Amazon":** el dato de Amazon es justo el que viene duplicado. El banco es la única fuente que no puede estar duplicada, porque el dinero entró una sola vez.

---

# ANEXO B — Detalle técnico de notas de crédito

*(No para presentar. Respaldo si preguntan.)*

## Fuente de datos

Mismas tablas. Los filtros:

```sql
transaction_type = 'Refund'
description IN ('Refund', 'DisbursementOnlyRefund')
```

Los importes llegan en negativo, con las mismas columnas ya validadas en facturación (`cfdi_base`, `cfdi_iva`, `cfdi_total`).

## Granularidad

El reporte actual agrupa las devoluciones a nivel transacción porque su `GROUP BY` solo baja a nivel pieza cuando la descripción es `Order Payment`. El detalle **sí existe** en `amz_transaction_items`; se recupera extendiendo el `CASE` para incluir `Refund`.

**Decisión pendiente de Finanzas:** ¿NC a nivel línea o a nivel orden?

## Clasificación propuesta

| Tipo | Condición técnica |
|---|---|
| C | `description = 'DisbursementOnlyRefund'` → clasificación inmediata |
| A | Sin `stock.picking` de salida en estado `done` |
| B | Con salida en `done` |

**Riesgo abierto:** en FBA (89 de 100 filas de la muestra) el despacho lo hace Amazon. Hay que verificar si Odoo registra la salida igual. Si no, la regla A/B necesita otro discriminador — probablemente el retorno físico (`stock.picking` de tipo devolución).

## Amazon Business

`amz_b2b_invoices` tiene tres campos que apuntan a que las NC ya están ahí:

- `receipt_type` — perfilar qué valores existe además de `Invoice`
- `related_cfdi_relation_type` — tipo de relación CFDI; el `01` es *nota de crédito de documentos relacionados*
- `cancellation_date`

**Consulta para perfilarlo:**

```sql
SELECT receipt_type, related_cfdi_relation_type, COUNT(*) AS n
FROM somos_reyes.amz_b2b_invoices
GROUP BY receipt_type, related_cfdi_relation_type
ORDER BY n DESC;
```

Si aparecen valores distintos de `Invoice`, el flujo B2B de devoluciones se resuelve con un cruce, sin desarrollo adicional.

## Casos no contemplados en el flujo de Finanzas

1. **`LabmanLabelRefund`** — 4 de 100 filas. Reembolso de guía, no de producto.
2. **Comisiones por devolución** — las columnas `refund_commission`, `refund_commission_base`, `refund_commission_tax` existen y el documento no las trata.
3. **Devolución de mes cerrado** — la NC se emite en un periodo posterior al de la factura.
4. **Devolución sin venta previa** — orden anterior al inicio del proceso automatizado.

## Reverso del cobro

El documento pide romper la aplicación del cobro cuando la factura ya estaba pagada.

**Objeción técnica:** con cobros parciales de varios settlements, romper la conciliación implica deshacer varios pagos y rehacerlos. Es frágil y difícil de auditar.

**Propuesta alternativa:** conciliar la NC directamente contra `102.02.008`. El reembolso viaja neteado en el mismo depósito de Amazon, así que esto refleja el movimiento real del dinero y no toca los pagos ya conciliados.

Llevar a la siguiente reunión con el asiento propuesto.
