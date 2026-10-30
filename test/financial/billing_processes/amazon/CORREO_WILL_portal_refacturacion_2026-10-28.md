**Para:** Will
**CC:** Jero
**Asunto:** Portal de autofacturación: refacturas que quedan a medias (NC en borrador, factura sin timbrar)

Hola Will:

Revisando la facturación de Amazon, Finanzas encontró refacturas del portal que no se completaron. El resultado son dos facturas vigentes por la misma venta y un cobro de Amazon que no se puede aplicar. Te paso el detalle para que lo revises.

**Qué debería pasar** cuando el cliente pide su factura con RFC:
1. NC contra la factura a público en general, publicada y timbrada (relación 01 con su UUID).
2. Factura nueva al cliente, publicada y timbrada.

**Qué pasó** en estas 3 órdenes (todas de producción, creadas por "Public user"):

| SO | Factura a público en general | NC del portal | Factura al cliente |
|---|---|---|---|
| SO10261320 | INV/2026/125576, timbrada | **Borrador** (11-sep 12:47), sin UUID | INV/2026/130756, publicada **sin UUID** |
| SO10268782 | INV/2026/129336, timbrada | **Borrador** (21-sep 18:38), sin UUID | INV/2026/138060, publicada **sin UUID** |
| SO10264633 | INV/2026/126506, timbrada | **Borrador** (25-sep 23:10), sin UUID | INV/2026/141980, publicada **sin UUID** |

Detalles que pueden ayudar:
- **La NC no queda ligada como reversa.** Tiene `ref = "NC para: SO…"` y `l10n_mx_edi_cfdi_origin = 01|<UUID de la factura a público en general>`, pero `reversed_entry_id` está vacío y la NC no está conciliada con la factura original.
- **La factura al cliente se crea unos segundos después de la NC aunque la NC no se publique.** Si falla el paso 1, el paso 2 sigue igual.
- **No es un error generalizado.** En el mismo periodo otras 44 refacturas del portal sí quedaron completas (NC timbrada y factura nueva timbrada).

**Preguntas**
1. ¿Por qué la NC se queda en borrador? ¿Hay algún error al publicarla o timbrarla que no se registre en el chatter?
2. ¿La factura al cliente se timbra en producción? En estas 3 está publicada sin UUID.
3. ¿Se puede hacer que, si la NC no se publica y timbra, el portal no emita la factura nueva (o avise)? Así no quedan dos facturas vigentes.
4. ¿Se puede crear la NC como reversa de la factura original (`reversed_entry_id`) y conciliarla con ella? Con eso Odoo marca la factura a público en general como revertida y nuestro proceso de cobros la descarta sola.

Mientras tanto, a estas 3 órdenes no les aplicamos el cobro y las marcamos para revisión.

Gracias,
Sergio
