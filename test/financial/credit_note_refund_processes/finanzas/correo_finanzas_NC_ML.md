**Para:** Finanzas
**Asunto:** Notas de crédito Mercado Libre: lote de prueba para revisión y definiciones pendientes

Hola equipo,

Ya tenemos automatizado el registro en Odoo de las notas de crédito (NC) que Mercado Libre timbra con nuestro sello. Antes de cargarlas en productivo necesitamos su revisión y visto bueno.

**Lote de prueba (adjunto `Lote_prueba_NC_ML_staging_2026-11-03.csv`)**
Son 16 NC registradas en **Odoo staging** (no en productivo). Cada fila trae un enlace para abrirla:
- **A · Cancelación antes del despacho** (4) y **B · Devolución posterior al despacho** (5): diario *Facturas Devoluciones* (DEV).
- **C · Bonificación parcial** (6): diario *Facturas Bonificaciones* (BON), producto BON_SRV.
- 1 caso que el proceso **no registra** y deja para revisión manual (ver punto 5).

**Cómo queda cada NC**
- **Asiento:** Debe 402.01.001 (subtotal) y 208.01.001 (IVA); Haber 105.01.004 (total).
- **CFDI:** se adjunta el XML de Mercado Libre y no se vuelve a timbrar.
- **Ligas:** queda ligada a la factura original, no a la orden de venta.
- **Si la factura tenía saldo,** la NC se concilia contra ella. **Si ya estaba cobrada,** queda como saldo a favor en 105.01.004.

**Lo que necesitamos de ustedes**
1. **Visto bueno del lote**, para pasar a un piloto de 10 NC de octubre en productivo.
2. **Costo de venta en devoluciones (A y B):** Odoo registra en automático Debe **205.06.003** "Goods Received - No Invoices (copia)" / Haber **501.01.001** Costo de venta.
   - ¿Es la cuenta correcta para *Mercancías facturadas no enviadas*?
   - Para el caso B, su flujo pide *Devoluciones en tránsito*, que aún no existe: ¿pueden crearla y compartirnos el código?
3. **Periodo cerrado al 30-sep:** hay 2,892 NC de julio a septiembre (~$6.8 M). Opciones: registrarlas con fecha contable en el periodo abierto (conservando la fecha fiscal de la NC), o abrir temporalmente esos meses. ¿Cuál prefieren?
4. **NC de facturas ya cobradas:** por ahora quedan como saldo a favor en la CxC hasta que registremos la salida del dinero en Mercado Pago. ¿Están de acuerdo? ¿Cómo tratamos los reembolsos que cubre Mercado Libre con su programa de protección, donde el dinero no sale de nuestra cuenta?
5. **Posible doble egreso:** la orden 2000018417135844 (factura INV/2026/131228) ya tiene una NC manual, **RINV/2026/01731** del 29-oct, timbrada por Odoo. Además, Mercado Libre emitió su propia NC (MELINC-7782, $5,679.00). Por favor revísenla; probablemente haya que cancelar una de las dos ante el SAT.

Como referencia: hay **3,685 NC listas para cargar (~$9 M)** de julio a la fecha. Además, hay 28 casos en los que Mercado Libre volvió a facturar la orden; esos los revisaremos aparte.

Gracias,
Sergio
