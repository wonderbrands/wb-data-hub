# IVA no cobrado (209.01.001) en ML y Amazon: resumen para Finanzas

**Petición:** correo de Jero del 25-nov-2026, punto A1. El IVA de las facturas de marketplace queda como **no cobrado** (209.01.001) hasta que se cobra, en vez de **cobrado** (208.01.001) desde que se factura. Se corrigen primero Mercado Libre y Amazon, solo para las facturas nuevas. No hay barrido histórico.

**Rama:** `fix/iva-209-ml-amazon`. Todavía no está en producción.

## Qué cambia

| | Antes | Ahora |
|---|---|---|
| Impuesto en la factura | 38 "IVA(16%) Marketplaces", exigible al facturar | 14 "16%", exigible al cobro |
| Asiento de la factura | Cr 208.01.001 | Cr **209.01.001** |
| Al conciliar el cobro | nada más | Odoo crea un asiento de **base de flujo** en el diario CBMX "Effectively Paid": Dr 209.01.001 / Cr 208.01.001 |

- Aplica a **ML (las dos tiendas)** y a **Amazon (B2C y Business)**. Walmart ya nace así.
- Los cobros se registran igual que hoy (ML 04, Amazon 03). El traspaso de 209 a 208 lo hace Odoo solo, con la fecha del cobro.
- **Candado:** si alguien cambia el impuesto 14 en Odoo (deja de ser al cobro o de usar la 209.01.001), la facturación de ML y de Amazon se detiene antes de crear facturas y avisa. El precheck de Amazon también lo revisa.

## Prueba en Odoo pruebas (copia de producción al 13-nov), 26-nov-2026

Facturas de prueba con `ref` `PRUEBA-IVA209:<caso>`. Script: `test/financial/billing_processes/amazon/20_prueba_iva_al_cobro.py`.

**ML.** Factura como la registra 02 (sin timbrar) y un cobro con 1 centavo de más, como 04.

| Asiento | Cuenta | Debe | Haber |
|---|---|---:|---:|
| Factura INV/2026/255823 | 105.01.004 CxC ML | 1,000.00 | |
| | 401.01.001 Ventas | | 862.07 |
| | **209.01.001 IVA no cobrado** | | **137.93** |
| | 501.01.001 / 205.06.003 (costo) | 1,136.66 | 1,136.66 |
| Base de flujo CBMX/…/OD5399 | 209.01.001 | 137.93 | |
| | **208.01.001 IVA cobrado** | | **137.93** |
| | 899.01.001 Base imponible (se netea) | 862.07 | 862.07 |

**Amazon B2C.** Primero un cobro parcial y luego el resto con −0.01 de redondeo a 704.23.001, como 03.
- Factura INV/2026/255824: Cr 209.01.001 137.93.
- Cobro parcial (499.99 de 999.99): la base de flujo pasa **68.97** a la 208 y en la 209 quedan 68.96.
- Cobro del resto: la base de flujo pasa **68.96**. En la 209 queda **0.00**.
- El IVA pasa a la 208 en proporción a lo cobrado. Una factura parcialmente pagada entre depósitos deja en la 209 el IVA de lo que falta cobrar.

**Amazon Business con centavo de IVA.** 01 ajusta la línea de IVA para igualarla al CFDI de Amazon.
- Factura INV/2026/255825: total 1,399.31, Cr 209.01.001 **193.02** (ya con el ajuste de +0.01).
- Cobro completo: la base de flujo pasa **193.02**. En la 209 queda 0.00. El ajuste de centavos sigue funcionando con la línea de IVA en la 209.

**Validación.** Amazon y ML aceptan el impuesto 14 y rechazan el 38.

**Timbrado.** En pruebas el timbrado está apagado, así que esta prueba no timbra. El impuesto 14 tiene la misma configuración SAT que el 38 (IVA, Tasa, 16 %). En la copia de producción hay **5,011 facturas ya timbradas con el impuesto 14** (Piso, Walmart, Mayoreo, Salderos), así que el PAC lo acepta.

## Lo que Finanzas va a ver distinto

1. **Saldo de la 209.01.001:** es el IVA de las facturas de ML y Amazon que aún no se cobran. Se vacía conforme entran los cobros.
2. **Muchos asientos nuevos en CBMX:** uno por cada cobro aplicado a cada factura. En ML son miles al mes y en Amazon puede haber más de uno por factura cuando hay cobros parciales.
3. **Mes de transición:** las facturas emitidas antes del cambio siguen con el IVA en la 208 y sus cobros no generan base de flujo. No hay barrido histórico, como se pidió.
4. **Redondeo (704.23.001):** no afecta al IVA. Al saldarse la factura, pasa a la 208 todo su IVA.
5. **Promociones pagadas por Amazon (asiento `AUTOPROMO`):** el excedente sigue yendo a 403.01.002 + **208.01.001**. Es dinero ya cobrado, así que el IVA va directo a cobrado.

## Preguntas para Finanzas

1. **Facturas del 1a1 (TikTok, Coppel, Piso, Mayoreo, Sitioweb…):** siguen con el impuesto 38 (IVA cobrado al facturar). ¿También pasan a la 209?
2. **Notas de crédito de ML (06):** siguen con el 38 (Dr 208.01.001). Los números cuadran: si la NC se concilia contra una factura con IVA en la 209, Odoo pasa ese IVA a la 208 y la NC lo cancela ahí. ¿Prefieren que la NC también use el impuesto 14, para que todo pase por la 209? Lo mismo aplica a las NC de Amazon cuando se construyan.
