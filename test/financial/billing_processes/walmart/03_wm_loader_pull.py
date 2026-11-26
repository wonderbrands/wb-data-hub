"""
03_wm_loader_pull.py
--------------------
Replica en Odoo los CFDI que emitio el autofacturador de Walmart (patron PULL,
como Mercado Libre y Amazon Business). NO timbra: el comprobante ya existe en
el SAT.

Toma las filas PENDING_WM_LINK (CFDI de Walmart con su XML ya cargado por 02).
Una factura de Odoo por CFDI. Walmart emite un CFDI por PIEZA, asi que una
linea de 3 piezas son 3 facturas, cada una con cantidad 1.

Por cada fila:
  1. Valida el XML: UUID igual al de la fila, emisor nuestro, tipo I, un solo
     concepto, y total contra el precio por pieza de Walmart (+-$1).
  2. SO: existe, es de Team_Walmart, no esta cancelada; en borrador se
     confirma (Walmart ya vendio).
  3. El UPC del CFDI (NoIdentificacion) es el de la linea ligada; la linea de
     la SO es la del MISMO SKU con piezas pendientes. Si no -> WM_LINEA_REVISAR
     (nunca se usa la linea de otro producto: caso SO10339388).
  4. Cliente por el RFC del receptor del XML (se crea si no existe).
  5. REVALIDACION FAIL-CLOSED justo antes de crear: facturas vivas de la SO.
       - una con este UUID o con la ref AUTOINV:<SO>:<UUID>  -> se adopta si
         coincide con el CFDI (total, cliente, CxC, diario); si no,
         WM_ADOPTADA_REVISAR (lo corrige el traslado de 2026)
       - otras piezas de Walmart de la misma orden            -> normal
       - cualquier otra (AUTOINV:<SO> a publico general, global, manual
         con otro UUID)                                        -> DOBLE_CFDI
     Si la consulta falla, no se crea.
  5b. ESTADO EN EL SAT (servicio publico de consulta): solo se replica un CFDI
     Vigente. Cancelado -> WM_CFDI_NO_VIGENTE. No Encontrado o el servicio no
     responde -> se queda PENDING_WM_LINK para la siguiente corrida.
  6. create en el diario WALM con ref AUTOINV:<SO>:<UUID> -> nombre = serie-folio
     del CFDI (FI-4252) -> base y CxC 105.01.007 -> UUID y narration -> IVA al
     centavo del CFDI (impuesto al cobro: 209.01.001) -> XML adjunto -> action_post.
  7. 205.06.003 contra la salida y chatter.

Variables: WM_ENTORNO, WM_MAX_FACTURAS.
"""

import os
import sys
import json
import xmlrpc.client
from collections import Counter

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, validar_entorno, notify_slack,
                        get_db_connection, OdooModelProxy, OdooWriteUncertain,
                        cargar_catalogos, buscar_so, facturas_existentes, resumen_factura,
                        confirmar_so, buscar_por_ref, leer_lineas_so, comando_linea,
                        total_estimado, fijar_cxc, cuadrar_base_con_cfdi,
                        cuadrar_iva_con_cfdi, conciliar_salida, postear_chatter,
                        reset_stuck_processing, claim, update_fila, leer_cfdi_b64,
                        sku_de_producto, a_float, normalizar_uuid,
                        TBL_BILLING, MARKETPLACE, RFC_EMISOR, IDEMPOTENCY_PREFIX,
                        PARTNER_ID_PUBLICO_GENERAL, PAYMENT_METHOD_TRANSFERENCIA,
                        TOLERANCIA_TOTAL, MAX_FACTURAS, ACCOUNT_MERC_NO_ENVIADA,
                        CLIENTE_TELEFONO_GENERICO, CLIENTE_CORREO_GENERICO,
                        ST_PENDING_WM_LINK, ST_WM_LINKED, ST_DOBLE_CFDI, ST_NO_SO,
                        ST_SO_CANCELADA, ST_SO_OTRO_EQUIPO, ST_TOTAL_DIFF,
                        ST_XML_INVALIDO, ST_ERROR, ST_BORRADOR_AJENO, ST_WM_NO_VIGENTE,
                        ST_LINEA_REVISAR, ST_ADOPTADA_REVISAR, upc_por_linea)
import _sat_descarga as sat


def fetch_pendientes(db):
    cur = db.cursor(dictionary=True)
    cur.execute(f"""
        SELECT id, mkp_order_id, odoo_so_name, cfdi_uuid, total_amount, raw_json, xml_data
        FROM {TBL_BILLING}
        WHERE marketplace = %s AND status = %s AND retry_count_loader < 3
        ORDER BY id ASC
    """, (MARKETPLACE, ST_PENDING_WM_LINK))
    filas = cur.fetchall()
    cur.close()
    return filas


def uuids_walmart_de_orden(db, pedido):
    """Todos los UUID de Walmart de la orden (cualquier estado de fila)."""
    cur = db.cursor()
    cur.execute(f"""SELECT cfdi_uuid FROM {TBL_BILLING}
                    WHERE marketplace = %s AND mkp_order_id LIKE %s""",
                (MARKETPLACE, f"{pedido}:%"))
    salida = {normalizar_uuid(r[0]) for r in cur.fetchall() if r[0]}
    cur.close()
    return salida


def resolver_cliente(odoo, cfdi, catalogos, cache):
    """Partner por RFC del receptor. Publico general -> 13436."""
    rfc = cfdi['receptor_rfc']
    if not rfc or rfc == 'XAXX010101000':
        return PARTNER_ID_PUBLICO_GENERAL
    if rfc in cache:
        return cache[rfc]
    encontrados = odoo.execute_kw('res.partner', 'search_read', [[('vat', '=', rfc)]],
                                  {'fields': ['id'], 'limit': 1})
    if encontrados:
        cache[rfc] = encontrados[0]['id']
        return cache[rfc]
    vals = {
        'name': cfdi['receptor_nombre'] or f'Cliente {rfc}',
        'vat': rfc,
        'zip': cfdi['receptor_cp'] or False,
        'l10n_mx_edi_fiscal_regime': cfdi['receptor_regimen'] or False,
        'country_id': catalogos['mx_country_id'],
        'company_type': 'person' if len(rfc) == 13 else 'company',
        'phone': CLIENTE_TELEFONO_GENERICO,
        'email': CLIENTE_CORREO_GENERICO,
        'comment': 'Creado desde un CFDI del autofacturador de Walmart. Telefono y '
                   'correo genericos: el CFDI no trae datos de contacto.',
    }
    try:
        cache[rfc] = odoo.execute_kw('res.partner', 'create', [vals],
                                     {'context': {'tracking_disable': True}})
        log.info(f"Cliente creado desde CFDI de Walmart: {rfc} -> id={cache[rfc]}")
        return cache[rfc]
    except OdooWriteUncertain:
        encontrados = odoo.execute_kw('res.partner', 'search_read', [[('vat', '=', rfc)]],
                                      {'fields': ['id'], 'limit': 1})
        if encontrados:
            cache[rfc] = encontrados[0]['id']
            return cache[rfc]
        return None
    except xmlrpc.client.Fault as e:
        log.error(f"No se pudo crear el cliente {rfc}: {e.faultString[:300]}")
        return None


def elegir_linea_so(lineas, sku, piezas):
    """Linea de la SO del MISMO SKU con piezas pendientes de facturar. Nunca se
    usa la linea de otro producto: asi se cruzaron SO10339388 (correo de
    Finanzas, C2) en el proceso manual y en la prueba del 19-nov (la linea de su
    SKU ya la habia facturado otra factura con el UUID equivocado)."""
    del_sku = [l for l in lineas if sku_de_producto(l.get('product_id')) == (sku or '').upper()]
    pendientes = [l for l in del_sku if l['product_uom_qty'] - l['qty_invoiced'] >= piezas]
    if pendientes:
        return pendientes[0], None
    if del_sku:
        return None, (f"La linea del SKU {sku} ya esta facturada ({del_sku[0]['qty_invoiced']:g} de "
                      f"{del_sku[0]['product_uom_qty']:g}): otra factura de la SO la tomo. Revisar que "
                      f"esa factura tenga el UUID y el producto correctos")
    return None, (f"La SO no tiene piezas pendientes del SKU {sku}: la linea ya la facturo otra factura "
                  f"de la SO (revisar que esa factura tenga el UUID y el producto correctos) o la SO "
                  f"no trae ese producto")


def upc_de_linea(db, iderp):
    """UPC (sin ceros a la izquierda) de la linea de Walmart, de wm_order_line."""
    if not iderp:
        return None
    cur = db.cursor()
    r = upc_por_linea(cur, {iderp})
    cur.close()
    return r.get(iderp)


def revisar_adoptada(odoo, inv_id, cfdi, catalogos):
    """Una factura que ya existia con el UUID (proceso manual) se adopta solo si
    coincide con el CFDI. Lo que no coincide (C1 y C2 del correo de Finanzas) se
    reporta; lo corrige el traslado de 2026 al diario WALM."""
    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['name', 'amount_total', 'partner_id', 'journal_id']})[0]
    problemas = []
    if abs(a_float(inv['amount_total']) - cfdi['total']) > TOLERANCIA_TOTAL:
        problemas.append(f"total {a_float(inv['amount_total']):,.2f} vs CFDI {cfdi['total']:,.2f}")
    if inv['partner_id']:
        vat = odoo.execute_kw('res.partner', 'read', [[inv['partner_id'][0]]], {'fields': ['vat']})[0]['vat']
        if (vat or '').upper() != (cfdi['receptor_rfc'] or '').upper():
            problemas.append(f"cliente {inv['partner_id'][1]} ({vat or 'sin RFC'}) vs receptor {cfdi['receptor_rfc']}")
    cxc = odoo.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('display_type', '=', 'payment_term')]],
                          {'fields': ['account_id']})
    if any(l['account_id'][0] != catalogos['cuenta_cxc'] for l in cxc):
        problemas.append(f"CxC {cxc[0]['account_id'][1]}")
    if not inv['journal_id'] or inv['journal_id'][0] != catalogos['diario_walmart']:
        problemas.append(f"diario {inv['journal_id'][1] if inv['journal_id'] else '-'}")
    return inv, problemas


def procesar(odoo, db, fila, catalogos, cache_clientes, stats):
    rid = fila['id']
    uuid = normalizar_uuid(fila['cfdi_uuid'])
    so_name = fila['odoo_so_name']
    raw = json.loads(fila['raw_json']) if isinstance(fila['raw_json'], str) else (fila['raw_json'] or {})
    pedido = raw.get('customer_order_id') or fila['mkp_order_id'].split(':', 1)[0]
    linea_wm = raw.get('linea') or {}
    etiqueta = f"{so_name} {uuid[:8]}"

    # --- 1. XML ---
    try:
        cfdi = leer_cfdi_b64(fila['xml_data'])
    except Exception as e:
        update_fila(db, rid, ST_XML_INVALIDO, f"XML ilegible: {e}")
        return 'xml_invalido'
    problemas = []
    if cfdi['uuid'] != uuid:
        problemas.append(f"UUID del XML {cfdi['uuid']} distinto al de la fila")
    if cfdi['emisor_rfc'] != RFC_EMISOR:
        problemas.append(f"emisor {cfdi['emisor_rfc']}")
    if cfdi['tipo'] != 'I':
        problemas.append(f"tipo {cfdi['tipo']}")
    if len(cfdi['conceptos']) != 1:
        problemas.append(f"{len(cfdi['conceptos'])} conceptos (se esperaba 1 por pieza)")
    if problemas:
        update_fila(db, rid, ST_XML_INVALIDO, '; '.join(problemas))
        return 'xml_invalido'

    # Casi siempre un CFDI por pieza, pero hay CFDI de 2 piezas (1,398.01 = 2 x 699).
    piezas = cfdi['conceptos'][0]['cantidad'] or 1
    precio_pieza = a_float(linea_wm.get('price_amount')) / max(int(linea_wm.get('quantity') or 1), 1)
    esperado = round(precio_pieza * piezas, 2)
    if abs(cfdi['total'] - esperado) > TOLERANCIA_TOTAL:
        update_fila(db, rid, ST_TOTAL_DIFF,
                    f"CFDI {cfdi['total']:,.2f} vs Walmart {esperado:,.2f} "
                    f"({piezas:g} pieza(s) x {precio_pieza:,.2f})")
        return 'total_diff'

    # --- 2. SO ---
    so = buscar_so(odoo, so_name) if so_name else None
    if not so:
        update_fila(db, rid, ST_NO_SO, f"SO {so_name} no existe en Odoo")
        return 'no_so'
    if so['state'] == 'cancel':
        update_fila(db, rid, ST_SO_CANCELADA, 'SO cancelada en Odoo (decision pendiente de Finanzas)')
        return 'so_cancelada'
    if not so['team_id'] or so['team_id'][0] != catalogos['team_walmart']:
        update_fila(db, rid, ST_SO_OTRO_EQUIPO, f"equipo {so['team_id']}")
        return 'otro_equipo'
    if so['state'] in ('draft', 'sent'):
        motivo = confirmar_so(odoo, so)
        if motivo:
            update_fila(db, rid, ST_ERROR, motivo, sumar_retry=True)
            return 'error'
        stats['so_confirmadas'] += 1

    # --- 3. Linea de la SO ---
    concepto = cfdi['conceptos'][0]
    # El CFDI tiene que ser de la linea a la que se ligo: su NoIdentificacion es
    # el UPC de la linea en Walmart (232 de 232 en octubre).
    upc_cfdi = (concepto.get('no_identificacion') or '').lstrip('0')
    upc_wm = upc_de_linea(db, linea_wm.get('iderp'))
    if upc_cfdi and upc_wm and upc_cfdi != upc_wm:
        update_fila(db, rid, ST_LINEA_REVISAR,
                    f"El CFDI es del UPC {upc_cfdi} y la linea {linea_wm.get('iderp')} ({linea_wm.get('sku')}) "
                    f"es del UPC {upc_wm}: el CFDI esta ligado a otra linea")
        return 'linea_revisar'
    lineas = leer_lineas_so(odoo, so)
    linea_so, aviso_linea = elegir_linea_so(lineas, linea_wm.get('sku'), piezas)

    # --- 5. REVALIDACION FAIL-CLOSED (antes de crear cualquier cosa) ---
    ref = f"{IDEMPOTENCY_PREFIX}{so['name']}:{uuid}"
    try:
        vivas = facturas_existentes(odoo, so['name'], so)
    except Exception as e:
        update_fila(db, rid, ST_ERROR, f"Revalidacion fallida, no se crea: {e}", sumar_retry=True)
        return 'error'
    uuids_orden = uuids_walmart_de_orden(db, pedido)
    mias = [f for f in vivas if normalizar_uuid(f.get('l10n_mx_edi_cfdi_uuid')) == uuid
            or f.get('ref') == ref]
    ajenas = [f for f in vivas if f not in mias
              and normalizar_uuid(f.get('l10n_mx_edi_cfdi_uuid')) not in uuids_orden
              and not (f.get('ref') or '').startswith(f"{IDEMPOTENCY_PREFIX}{so['name']}:")]
    if len(mias) == 1 and mias[0]['state'] == 'posted':
        inv_adoptada, problemas = revisar_adoptada(odoo, mias[0]['id'], cfdi, catalogos)
        if problemas:
            update_fila(db, rid, ST_ADOPTADA_REVISAR,
                        f"Ya existia {inv_adoptada['name']} con este UUID, pero no coincide con el CFDI: "
                        + '; '.join(problemas), facturas=[resumen_factura(mias[0])])
            return 'adoptada_revisar'
        update_fila(db, rid, ST_WM_LINKED, 'Ya existia en Odoo (proceso manual o corrida previa)',
                    facturas=[resumen_factura(mias[0])])
        return 'adoptada'
    if len(mias) > 1:
        update_fila(db, rid, ST_DOBLE_CFDI, f"{len(mias)} facturas vivas con el UUID {uuid}",
                    facturas=[resumen_factura(f) for f in mias])
        return 'doble_cfdi'
    if ajenas and all(f['state'] == 'draft' for f in ajenas):
        # Patron de los procesos anteriores (correo de Finanzas del 17-nov: 26
        # pedidos con borrador sin CFDI). No es un doble CFDI, pero no se crea
        # nada encima: Finanzas cancela el borrador y la orden se reevalua.
        update_fila(db, rid, ST_BORRADOR_AJENO,
                    'La SO tiene un borrador sin CFDI de otro proceso: cancelarlo',
                    facturas=[resumen_factura(f) for f in ajenas])
        return 'borrador_ajeno'
    if ajenas:
        update_fila(db, rid, ST_DOBLE_CFDI,
                    'La SO tiene otra factura viva que no es de Walmart: cancelar la nuestra '
                    'antes de replicar el CFDI de Walmart',
                    facturas=[resumen_factura(f) for f in ajenas])
        return 'doble_cfdi'

    # --- 5b. Estado en el SAT (solo si se va a crear o terminar una factura) ---
    try:
        est = sat.consultar_estado(cfdi['emisor_rfc'], cfdi['receptor_rfc'], cfdi['total'], uuid)
    except sat.ErrorSAT as e:
        update_fila(db, rid, ST_PENDING_WM_LINK, f"No se pudo consultar el estado en el SAT: {str(e)[:300]}")
        return 'sat_sin_respuesta'
    if est['estado'] == 'Cancelado':
        update_fila(db, rid, ST_WM_NO_VIGENTE, f"El SAT reporta el CFDI {est['estado']} ({est['estatus_cancelacion']})")
        return 'no_vigente'
    if est['estado'] != 'Vigente':
        update_fila(db, rid, ST_PENDING_WM_LINK, f"SAT: {est['estado']} ({est['codigo']}); se reintenta")
        return 'sat_no_encontrado'

    if mias:   # borrador huerfano de una corrida anterior: se retoma
        inv_id = mias[0]['id']
        stats['retomadas'] += 1
    else:
        if not linea_so:
            update_fila(db, rid, ST_LINEA_REVISAR, aviso_linea,
                        facturas=[resumen_factura(f) for f in vivas])
            return 'linea_revisar'
        partner_id = resolver_cliente(odoo, cfdi, catalogos, cache_clientes)
        if not partner_id:
            update_fila(db, rid, ST_ERROR, f"No se pudo resolver el cliente {cfdi['receptor_rfc']}",
                        sumar_retry=True)
            return 'error'

        cantidad = concepto['cantidad'] or 1
        precio = round((concepto['importe'] - concepto['descuento']) / cantidad, 6)
        comandos = [comando_linea(linea_so, cantidad, price_unit=precio, discount=0)]
        if abs(total_estimado(comandos) - cfdi['total']) > TOLERANCIA_TOTAL:
            update_fila(db, rid, ST_TOTAL_DIFF,
                        f"Odoo estimado {total_estimado(comandos):,.2f} vs CFDI {cfdi['total']:,.2f}")
            return 'total_diff'

        fecha = (cfdi['fecha'] or '')[:10]
        vals = {
            'ref': ref,
            'move_type': 'out_invoice',
            'journal_id': catalogos['diario_walmart'],
            'partner_id': partner_id,
            'invoice_origin': so['name'],
            'invoice_line_ids': comandos,
            'team_id': catalogos['team_walmart'],
            'l10n_mx_edi_usage': cfdi['receptor_uso'] or 'S01',
            'l10n_mx_edi_payment_method_id': catalogos['formas_pago'].get(
                cfdi['forma_pago'], PAYMENT_METHOD_TRANSFERENCIA),
            'l10n_mx_edi_payment_policy': cfdi['metodo_pago'] or 'PUE',
            'invoice_date': fecha, 'date': fecha, 'invoice_date_due': fecha,
        }
        if (cfdi['metodo_pago'] or 'PUE') == 'PUE':
            vals['invoice_payment_term_id'] = False
        try:
            inv_id = odoo.execute_kw('account.move', 'create', [vals])
        except OdooWriteUncertain as e:
            encontradas = buscar_por_ref(odoo, ref)
            if not encontradas:
                update_fila(db, rid, ST_ERROR, f"WRITE_UNCERTAIN en create y sin factura: {e}",
                            sumar_retry=True)
                return 'error'
            inv_id = encontradas[0]['id']
            stats['adoptadas_incierto'] += 1
        except xmlrpc.client.Fault as e:
            update_fila(db, rid, ST_ERROR, f"Odoo rechazo el create: {e.faultString[:500]}",
                        sumar_retry=True)
            return 'error'

    # --- 6. Nombre, base, CxC, UUID, IVA, XML, post ---
    # Nombre = serie-folio del CFDI (FI-4252) en el diario WALM: no consume la
    # secuencia INV (Finanzas, C3/D5; mismo patron que MELI-xxxxx y AB-xxxx).
    serie_folio = f"{cfdi['serie'] or ''}-{cfdi['folio'] or ''}".strip('-')
    if cfdi['serie'] and cfdi['folio']:
        try:
            actual = odoo.execute_kw('account.move', 'read', [[inv_id]],
                                     {'fields': ['name', 'state', 'journal_id']})[0]
            otro_diario = actual['journal_id'] and actual['journal_id'][0] != catalogos['diario_walmart']
            if actual['state'] == 'draft' and (otro_diario or actual['name'] != serie_folio):
                ctx = {'context': {'tracking_disable': True}}
                if otro_diario:   # borrador retomado de una corrida anterior en INV
                    odoo.execute_kw('account.move', 'write', [[inv_id], {'name': '/'}], ctx)
                odoo.execute_kw('account.move', 'write',
                                [[inv_id], {'journal_id': catalogos['diario_walmart'], 'name': serie_folio}], ctx)
        except OdooWriteUncertain as e:
            update_fila(db, rid, ST_ERROR, f"WRITE_UNCERTAIN al poner el nombre {serie_folio} (factura "
                        f"{inv_id} en borrador; la siguiente corrida la retoma): {e}", sumar_retry=True)
            return 'error'
        except xmlrpc.client.Fault as e:
            update_fila(db, rid, ST_ERROR, f"Odoo rechazo el nombre {serie_folio} (factura {inv_id} en "
                        f"borrador): {e.faultString[:400]}", sumar_retry=True)
            return 'error'
    try:
        ajuste_base = cuadrar_base_con_cfdi(odoo, inv_id, cfdi, etiqueta)
        narration = (f"CFDI autofacturador Walmart {cfdi['serie'] or ''}-{cfdi['folio'] or ''} "
                     f"| UUID {uuid}").strip()
        fijar_cxc(odoo, inv_id, catalogos['cuenta_cxc'],
                  {'l10n_mx_edi_cfdi_uuid': uuid, 'narration': narration})
        ajuste_iva = cuadrar_iva_con_cfdi(odoo, inv_id, cfdi, etiqueta)
        if ajuste_iva:
            fijar_cxc(odoo, inv_id, catalogos['cuenta_cxc'])

        adjunto = odoo.execute_kw('ir.attachment', 'search',
                                  [[('res_model', '=', 'account.move'), ('res_id', '=', inv_id),
                                    ('name', '=', f"{uuid}.xml")]], {'limit': 1})
        if not adjunto:
            att_id = odoo.execute_kw('ir.attachment', 'create', [{
                'name': f"{uuid}.xml", 'datas': fila['xml_data'],
                'res_model': 'account.move', 'res_id': inv_id}])
            try:
                odoo.execute_kw('account.edi.document', 'create', [{
                    'move_id': inv_id, 'edi_format_id': 2, 'attachment_id': att_id,
                    'state': 'sent'}])
            except Exception:
                pass   # no bloqueante (mismo trato que Mercado Libre)
    except OdooWriteUncertain as e:
        update_fila(db, rid, ST_ERROR, f"WRITE_UNCERTAIN antes de publicar (factura {inv_id} en "
                    f"borrador; la siguiente corrida la retoma): {e}", sumar_retry=True)
        return 'error'

    try:
        odoo.execute_kw('account.move', 'action_post', [[inv_id]])
    except OdooWriteUncertain as e:
        log.warning(f"{etiqueta}: post incierto, se verifica leyendo: {e}")
    except xmlrpc.client.Fault as e:
        update_fila(db, rid, ST_ERROR, f"Fallo el post: {e.faultString[:500]}", sumar_retry=True)
        return 'error'

    inv = odoo.execute_kw('account.move', 'read', [[inv_id]],
                          {'fields': ['name', 'state', 'amount_total', 'invoice_date',
                                      'l10n_mx_edi_cfdi_uuid', 'ref', 'payment_state']})[0]
    if inv['state'] != 'posted':
        update_fila(db, rid, ST_ERROR, f"La factura {inv_id} quedo en {inv['state']}",
                    sumar_retry=True)
        return 'error'

    conc = conciliar_salida(odoo, so, inv_id, catalogos['cuenta_merc_no_enviada'])
    stats[f"205_{conc['estado']}"] += 1
    postear_chatter(odoo, inv_id, [
        'Factura creada por Data via API: replica del CFDI del autofacturador de Walmart (no se timbra).',
        f"customer_order_id {pedido} | purchase_order_id {raw.get('purchase_order_id')} | "
        f"linea {linea_wm.get('iderp')} | {linea_wm.get('fulfillment_type')}",
        f"UUID {uuid} | serie-folio {cfdi['serie'] or ''}-{cfdi['folio'] or ''} | "
        f"receptor {cfdi['receptor_rfc']}",
        f"Mercancias facturadas - no enviadas ({ACCOUNT_MERC_NO_ENVIADA}): {conc['estado']} {conc['detalle']}",
    ] + ([aviso_linea] if aviso_linea else []))

    dif = round(a_float(inv['amount_total']) - cfdi['total'], 2)
    update_fila(db, rid, ST_WM_LINKED, aviso_linea, cfdi_uuid=uuid,
                facturas=[resumen_factura(dict(inv, id=inv_id))],
                extra_json={'total_odoo': a_float(inv['amount_total']), 'total_cfdi': cfdi['total'],
                            'diferencia': dif, 'ajuste_base': ajuste_base, 'ajuste_iva': ajuste_iva,
                            'conciliacion_205': conc['estado']})
    log.info(f"{etiqueta}: {inv['name']} | Odoo {a_float(inv['amount_total']):,.2f} "
             f"CFDI {cfdi['total']:,.2f} dif {dif:+.2f}")
    return 'creada'


def main():
    banner('03 - REPLICA DE CFDI DE WALMART (PULL)')
    validar_entorno()
    db = get_db_connection()
    reset_stuck_processing(db)
    filas = fetch_pendientes(db)
    log.info(f"Pendientes con XML: {len(filas)} | tope: {MAX_FACTURAS or 'sin tope'}")
    if not filas:
        return

    odoo = OdooModelProxy()
    catalogos = cargar_catalogos(odoo)
    stats, cache_clientes = Counter(), {}
    creadas = 0
    for fila in filas:
        if MAX_FACTURAS and creadas >= MAX_FACTURAS:
            log.info(f"Tope de {MAX_FACTURAS} facturas alcanzado; el resto queda para la siguiente corrida.")
            break
        if not claim(db, fila['id'], ST_PENDING_WM_LINK):
            continue
        try:
            resultado = procesar(odoo, db, fila, catalogos, cache_clientes, stats)
        except Exception as e:
            log.exception(f"{fila['mkp_order_id']}: error inesperado")
            update_fila(db, fila['id'], ST_ERROR, f"Inesperado: {str(e)[:500]}", sumar_retry=True)
            resultado = 'error'
        stats[resultado] += 1
        if resultado == 'creada':
            creadas += 1
    db.close()

    for k, v in sorted(stats.items()):
        log.info(f"  {k:<22} {v:>6}")
    if (stats['doble_cfdi'] or stats['error'] or stats['no_vigente'] or stats['linea_revisar']
            or stats['adoptada_revisar']):
        notify_slack(f"Walmart 03 (pull): creadas={stats['creada']} adoptadas={stats['adoptada']} "
                     f"DOBLE_CFDI={stats['doble_cfdi']} errores={stats['error']} "
                     f"TOTAL_DIFF={stats['total_diff']} no_vigentes={stats['no_vigente']} "
                     f"linea_a_revisar={stats['linea_revisar']} adoptadas_a_corregir={stats['adoptada_revisar']}")


if __name__ == '__main__':
    configurar_log()
    main()
