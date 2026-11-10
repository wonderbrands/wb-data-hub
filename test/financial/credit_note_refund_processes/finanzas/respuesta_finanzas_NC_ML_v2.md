**Asunto:** RE: Notas de crédito MeLi · lote v2 y avance de la hoja de ruta

Hola,

Gracias por el feedback. Apliqué los ajustes y corrí un lote nuevo en staging:
**https://somosreyes-test-06-nov-2026-41423118.dev.odoo.com**
El build anterior se reconstruyó y con él se perdieron las 16 NC del 3-nov; el lote nuevo las reemplaza.

**Adjuntos**
- `Lote_prueba_NC_ML_v2_staging_2026-11-09.csv`: 19 casos (18 reales + 1 simulado), **todos OK**, con un enlace a cada NC.
- `Listado_NC_ML_cobertura_2026-11-09.csv`: 3,715 NC para el cruce de cobertura.

**Hoja de ruta**
✅ hecho y probado en staging · 🟡 parcial o propuesta · ⏳ pendiente

| # | Acción | Resp. | Estado | Detalle |
|---|--------|-------|--------|---------|
| 1 | Link de staging y las 16 NC | Serch | ✅ | Link arriba. Las 16 se perdieron con el rebuild; el lote v2 las reemplaza y cubre más casos. |
| 2 | Una cuenta por tipo de NC | Serch | ✅ | A → 402.01.003, B → 402.01.001, C → 402.01.002. Diario BON y BON_SRV con 402.01.002 en staging; en productivo se configuran antes del piloto. |
| 3 | Costo de la factura original | Serch | ✅ | El costo revertido es el de la factura, y proporcional si vuelven menos unidades (ej. 3 de 4: $810.94 de $1,081.25). El caso 1.5 difería porque esa NC manual se ligó a la SO. |
| 4 | NC = XML al centavo; ±$0.01 a 704.23.001 | Serch | ✅ | Total e IVA iguales al XML en todos los casos. En ~15% de las NC el IVA de MeLi difiere $0.01 del cálculo de Odoo y se ajusta al XML. 3 casos con centavos contra la factura → 704.23.001. |
| 5 | Factura cobrada: bajar 102.03.001 | Serch | ✅ | Asiento Debe 105.01.004 / Haber 102.03.001 por lo cobrado, conciliado contra la NC. Mecánica: asiento de reverso; el cobro original no se rompe. 6 casos (cobrada y parcialmente cobrada). |
| 6 | Tipo B a 107.06.001 y retorno WH/DEV | Serch / Jero | 🟡 | **NC:** listo, Debe 107.06.001 / Haber 501.01.001. Usa una posición fiscal solo para la NC tipo B, sin afectar otros canales. **Retorno:** propuesta abajo; hoy 225 devoluciones B ya tienen su retorno validado contra 205.06.003. |
| 7 | Criterio A/B/C y refacturas | Serch | ✅ | Ver "Respuestas". |
| 8 | Listado de NC con universo y corte | Serch | 🟡 | Adjunto, con corte al 3-nov. Reenvío el actualizado después del barrido completo (~1 día). |
| 9 | Cruce de cobertura | Jero | ⏳ | Con el listado actualizado (8). |
| 10 | Lote nuevo con ajustes y casos del punto 3 | Serch | ✅ | 19 casos, todos OK (tabla abajo). |
| 11 | Cancelar ante el SAT la RINV/2026/01731 | Finanzas | ⏳ | El proceso detecta el doble egreso y no registra la NC de MeLi hasta que se cancele la de Odoo. |
| 12 | Bloquear la emisión manual de MeLi en Odoo 18 | Will / Serch | ⏳ | Viable: restringir facturas y NC de MercadoLibre a DataBot. Lo veo con Will. |
| 13 | Reabrir periodos desde junio | Jero / Serch | ⏳ | 2,864 NC de jul-sep quedan retenidas hasta la reapertura y se registran en su mes. Acordemos la ventana. |
| 14 | Reventa: facturas de ago-sep a su mes | Serch | ⏳ | Las 20,236 de ago-sep se pasan a la fecha de su factura cuando reabran el periodo (13). No se timbra nada: el CFDI es de MeLi. |
| 15 | Reventa: centavos de 105.01.004 contra 704.23.001 | Serch | ⏳ | Mismo mecanismo que en Amazon: asiento de redondeo contra 704.23.001, conciliado con la factura. Las de oct-nov se cierran ya; las de ago-sep, después de pasarlas a su mes (14), para no deshacer la conciliación. |
| 16 | Rehacer los complementos contables desde junio | Finanzas | ⏳ | Después de 13 y 14. |
| 17 | Corregir las traducciones de las cuentas | Finanzas | ⏳ | — |
| 18 | Piloto de 10 NC de octubre en productivo | Serch | ⏳ | Listo para correr: 811 NC de oct-nov ya pasan la validación. Falta su VoBo del lote y cerrar 6 y 9. |

**Casos del punto 3**

| Caso | Estado | Resultado |
|------|--------|-----------|
| NC mayor al saldo / varias NC sobre una factura | ✅ simulado | Se rechaza: nunca más que lo facturado. No hay casos reales (cada orden tiene una sola NC). |
| Orden sin factura en Odoo | ✅ | 4 casos reales; la NC se retiene hasta que exista la factura. |
| Factura cancelada en Odoo | ✅ regla | Va a revisión manual. No hay casos reales para probar. |
| NC de Reventa | ✅ | 2 NC registradas (tipos A y B). Quedan apagadas hasta que definan si entran. |
| Devolución B con retorno validado | ✅ | 2 casos. La NC carga 107.06.001, pero hoy el retorno abona 205.06.003; por eso la propuesta del punto 6. |

El lote también incluye: A, B y C con factura con saldo y con factura cobrada, 2+ unidades, doble egreso, orden sin reembolso, refactura y periodo cerrado.

**Respuestas**
- **1.1 Cuentas:** tomado (205.06.003, 105.01.004, 402.01.002 y 402.01.003).
- **1.4 Programa de protección:** de acuerdo, queda fuera de alcance; la NC se registra igual.
- **2.5 Clasificación:**
  - A vs B, por la fecha efectiva de la SO, que Odoo llena con la primera salida (OUT) validada: sin fecha efectiva es A.
  - B vs C, por la NC: si son unidades enteras al precio facturado es B; cualquier otro importe es C.
  - Reembolsos y disputas se clasifican igual. De las NC actuales, 2,591 vienen de disputas y 778 de refunds.
- **2.4 Refacturas:** la NC solo se registra si relaciona la factura que está en Odoo **y** la orden tiene un reembolso o una disputa en MeLi. Si no, va a revisión manual, sin 402 ni costo. Hoy son 28 NC que relacionan otra factura y 5 sin reembolso.
- **Retorno WH/DEV (1.2):** propongo una reclasificación automática **solo para MeLi**. Al validar el retorno de una orden con NC tipo B, se registra Debe 205.06.003 / Haber 107.06.001 por el costo del retorno y se concilia contra la NC en 107.06.001. No cambia la configuración del almacén ni toca otros canales.
- **5 Reventa:** la carga histórica terminó correctamente; desde entonces solo corre el flujo diario. Fecha contable y centavos: puntos 14 y 15 de la tabla.
- **Fecha:** la fecha contable es la fecha fiscal. Si el mes está cerrado, la NC se retiene; no se mueve de mes.
- **Cobertura (4):**
  - **Universo:** MeLi regular desde el 1-jun con factura de MeLi en Odoo; corte al 3-nov; sin Reventa.
  - **Diferencia con sus ~7,300:** hay ~62k órdenes facturadas o cargadas después del corte (incluida la Reventa del 4-nov), reembolsos de órdenes sin factura en Odoo, y la Reventa no está incluida.

**Necesito de ustedes:** VoBo del lote, definir el retorno (6) y si Reventa entra, y la ventana de reapertura (13).

Saludos,
Serch
