"""
16_traslado_amzb.py -- USO UNICO. Pasa al diario AMZB los CFDI de Amazon
Business que 01 registro en Customer Invoices (INV/).

Jero, hoja de ruta del 24-nov, punto 1: "Registrar los CFDI de Amazon Business
en el diario AMZB con su serie y folio, y trasladar lo ya cargado". Desde este
release 01 crea los nuevos directo en AMZB; este script mueve los anteriores.

Que hace con cada factura (de una en una):
  1. Guarda los cobros conciliados con su CxC.
  2. La regresa a borrador (button_draft desconcilia los cobros).
  3. Cambia el diario a AMZB y el nombre a serie-folio del CFDI de Amazon
     (AB-1245). Odoo solo deja cambiar el diario de algo que ya se publico si
     el nombre pasa antes por '/'.
  4. La vuelve a publicar y concilia los mismos cobros.
  5. Relee: diario, nombre, estado y estado de pago tienen que quedar bien.

No se toca el UUID, los importes, las cuentas ni la fecha. Nada se timbra: el
CFDI es el de Amazon. INV/ queda con huecos de numeracion (los folios que se
van a AMZB).

Candados:
  * Simulacion por defecto. Escribe solo con --aplicar.
  * No toca facturas con fecha en periodo cerrado (lock date): las reporta.
  * Si una factura falla a la mitad, se detiene y dice en que paso quedo.
  * --limite N para avanzar por tramos.

Uso (desde esta carpeta):
  $env:AMZ_ENTORNO='staging'; python 16_traslado_amzb.py              # simulacion
  $env:AMZ_ENTORNO='staging'; python 16_traslado_amzb.py --aplicar --limite 3
Deja un CSV en lote_auditoria_nov/traslado_amzb_<entorno>_<fecha>.csv
"""
import os
import sys
import csv
import argparse
import logging
import xmlrpc.client
from datetime import datetime

from _amz_common import (ENTORNO, OdooModelProxy, get_db_connection, validar_entorno,
                         ejecutar_sin_retorno, OdooWriteUncertain, normalizar_uuid)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('16_traslado_amzb')

DIARIO = os.getenv('AMZ_B2B_JOURNAL_CODE', 'AMZB').strip()
CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lote_auditoria_nov')


def cfdis_de_amazon(db):
    """{UUID en mayusculas: 'serie-folio'} de los CFDI de ingreso de Amazon."""
    cur = db.cursor(dictionary=True)
    cur.execute("""SELECT uuid, series, invoice FROM somos_reyes.amz_b2b_invoices
                   WHERE receipt_type = 'I' AND uuid IS NOT NULL""")
    salida = {}
    for r in cur.fetchall():
        u = normalizar_uuid(r['uuid'])
        if u and r['series'] and r['invoice']:
            salida[u] = f"{str(r['series']).strip()}-{str(r['invoice']).strip()}"
    cur.close()
    return salida


def candado(odoo):
    co = odoo.execute_kw('res.company', 'search_read', [[]],
                         {'fields': ['fiscalyear_lock_date', 'sale_lock_date', 'hard_lock_date'],
                          'limit': 1})[0]
    fechas = [str(co[k]) for k in ('fiscalyear_lock_date', 'sale_lock_date', 'hard_lock_date')
              if co.get(k)]
    return max(fechas) if fechas else ''


def candidatas(odoo, diario_id, uuids):
    """Facturas publicadas con UUID de Amazon que no estan en AMZB."""
    lista = list(uuids)
    encontradas = []
    for i in range(0, len(lista), 300):
        tramo = lista[i:i + 300]
        tramo = tramo + [u.lower() for u in tramo]
        encontradas += odoo.execute_kw(
            'account.move', 'search_read',
            [[('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
              ('journal_id', '!=', diario_id), ('l10n_mx_edi_cfdi_uuid', 'in', tramo)]],
            {'fields': ['id', 'name', 'invoice_origin', 'invoice_date', 'date', 'journal_id',
                        'amount_total', 'payment_state', 'l10n_mx_edi_cfdi_uuid', 'team_id']})
    return sorted(encontradas, key=lambda f: (str(f['invoice_date']), f['id']))


def conciliaciones(odoo, inv_id):
    """{cuenta: ([lineas de la factura], [contrapartes conciliadas])}.

    Todas las cuentas, no solo la CxC: button_draft desconcilia tambien la
    205.06.003 contra la salida (conciliar_salida de 01). Una cuenta puede
    tener varias lineas (una factura con dos productos: INV/2026/178650), y
    las del costo se recrean al publicar: se concilia por cuenta, no por id."""
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', inv_id)]],
                             {'fields': ['id', 'account_id', 'matched_debit_ids', 'matched_credit_ids']})
    propias = {l['id'] for l in lineas}
    salida = {}
    for l in lineas:
        parciales = l['matched_debit_ids'] + l['matched_credit_ids']
        contrapartes = set()
        if parciales:
            for p in odoo.execute_kw('account.partial.reconcile', 'read', [parciales],
                                     {'fields': ['debit_move_id', 'credit_move_id']}):
                for k in ('debit_move_id', 'credit_move_id'):
                    if p[k] and p[k][0] not in propias:
                        contrapartes.add(p[k][0])
        ids, cps = salida.get(l['account_id'][0], ([], set()))
        salida[l['account_id'][0]] = (ids + [l['id']], cps | contrapartes)
    return {k: (ids, sorted(cps)) for k, (ids, cps) in salida.items()}


def reconciliar(odoo, inv_id, antes):
    """Vuelve a conciliar lo que estaba conciliado antes del borrador.
    Devuelve cuantas cuentas se reconciliaron."""
    ahora = conciliaciones(odoo, inv_id)
    hechas = 0
    for cuenta, (_, contrapartes) in antes.items():
        if not contrapartes or cuenta not in ahora:
            continue
        lineas, ya = ahora[cuenta]
        faltan = [c for c in contrapartes if c not in ya]
        if not faltan:
            continue
        vivas = odoo.execute_kw('account.move.line', 'search',
                                [[('id', 'in', faltan), ('reconciled', '=', False)]])
        abiertas = odoo.execute_kw('account.move.line', 'search',
                                   [[('id', 'in', lineas), ('reconciled', '=', False)]])
        if vivas and abiertas:
            ejecutar_sin_retorno(odoo, 'account.move.line', 'reconcile', abiertas + vivas)
            hechas += 1
    return hechas


def foto_iva_cxc(odoo, inv_id):
    """IVA y CxC de la factura publicada: lo que 01 ajusto a mano.

    01 iguala el IVA al CFDI de Amazon editando la linea de impuesto
    (cuadrar_con_cfdi) y pone la CxC de Amazon. En borrador Odoo recalcula el
    impuesto y se pierde el centavo (AB-1043, 25-nov: 2,926.55 -> 2,926.56)."""
    lineas = odoo.execute_kw('account.move.line', 'search_read',
                             [[('move_id', '=', inv_id), ('display_type', 'in', ['tax', 'payment_term'])]],
                             {'fields': ['display_type', 'account_id', 'balance']})
    iva = [l for l in lineas if l['display_type'] == 'tax']
    cxc = [l for l in lineas if l['display_type'] == 'payment_term']
    return {'iva': round(iva[0]['balance'], 2) if len(iva) == 1 else None,
            'cxc': cxc[0]['account_id'][0] if len(cxc) == 1 else None}


def restaurar_iva_cxc(odoo, inv_id, foto):
    """En borrador: regresa el IVA y la CxC a la foto. Devuelve lo que toco."""
    tocado = []
    ahora = odoo.execute_kw('account.move.line', 'search_read',
                            [[('move_id', '=', inv_id), ('display_type', 'in', ['tax', 'payment_term'])]],
                            {'fields': ['id', 'display_type', 'account_id', 'balance']})
    iva = [l for l in ahora if l['display_type'] == 'tax']
    if foto['iva'] is not None and len(iva) == 1 and round(iva[0]['balance'], 2) != foto['iva']:
        odoo.execute_kw('account.move', 'write',
                        [[inv_id], {'line_ids': [(1, iva[0]['id'], {
                            'amount_currency': foto['iva'], 'balance': foto['iva']})]}],
                        {'context': {'tracking_disable': True}})
        tocado.append(f"IVA {iva[0]['balance']:.2f}->{foto['iva']:.2f}")
    cxc = odoo.execute_kw('account.move.line', 'search_read',
                          [[('move_id', '=', inv_id), ('display_type', '=', 'payment_term')]],
                          {'fields': ['id', 'account_id']})
    fuera = [(1, l['id'], {'account_id': foto['cxc']}) for l in cxc
             if foto['cxc'] and l['account_id'][0] != foto['cxc']]
    if fuera:
        odoo.execute_kw('account.move', 'write', [[inv_id], {'line_ids': fuera}],
                        {'context': {'tracking_disable': True}})
        tocado.append('CxC')
    return tocado


def trasladar(odoo, diario_id, f, nombre, foto=None):
    """Devuelve (estado, detalle). Se detiene en el primer paso que falla.
    nombre=None: no cambia diario ni nombre (modo --corregir); foto: el IVA y
    la CxC que debe quedar (por defecto, los de la factura antes de tocarla)."""
    paso = 'leer conciliaciones'
    try:
        antes = conciliaciones(odoo, f['id'])
        conciliadas = {c for c, (_, cp) in antes.items() if cp}
        foto = foto or foto_iva_cxc(odoo, f['id'])
        paso = 'regresar a borrador'
        ejecutar_sin_retorno(odoo, 'account.move', 'button_draft', [f['id']])
        if nombre is not None:
            paso = 'cambiar diario'
            odoo.execute_kw('account.move', 'write',
                            [[f['id']], {'name': '/', 'journal_id': diario_id}],
                            {'context': {'tracking_disable': True}})
            paso = 'poner nombre'
            odoo.execute_kw('account.move', 'write', [[f['id']], {'name': nombre}],
                            {'context': {'tracking_disable': True}})
        paso = 'restaurar IVA y CxC'
        restaurar_iva_cxc(odoo, f['id'], foto)
        paso = 'publicar'
        odoo.execute_kw('account.move', 'action_post', [[f['id']]])
        paso = 'volver a conciliar'
        rehechas = reconciliar(odoo, f['id'], antes)
        paso = 'verificar'
        despues = conciliaciones(odoo, f['id'])
        perdidas = [c for c in conciliadas if not (despues.get(c) or (None, []))[1]]
        r = odoo.execute_kw('account.move', 'read', [[f['id']]],
                            {'fields': ['name', 'state', 'journal_id', 'payment_state',
                                        'amount_total', 'l10n_mx_edi_cfdi_uuid']})[0]
        problemas = []
        if r['state'] != 'posted':
            problemas.append(f"estado {r['state']}")
        if r['journal_id'][0] != diario_id:
            problemas.append(f"diario {r['journal_id'][1]}")
        if nombre is not None and r['name'] != nombre:
            problemas.append(f"nombre {r['name']}")
        if r['payment_state'] != f['payment_state']:
            problemas.append(f"pago {f['payment_state']} -> {r['payment_state']}")
        if abs(r['amount_total'] - f['amount_total']) > 0.005:
            problemas.append(f"total {f['amount_total']} -> {r['amount_total']}")
        if normalizar_uuid(r['l10n_mx_edi_cfdi_uuid']) != normalizar_uuid(f['l10n_mx_edi_cfdi_uuid']):
            problemas.append('UUID cambio')
        if perdidas:
            problemas.append(f"{len(perdidas)} cuenta(s) quedaron sin conciliar")
        if problemas:
            return 'REVISAR', '; '.join(problemas)
        return 'TRASLADADA', (f"{len(conciliadas)} cuenta(s) conciliada(s) antes; "
                              f"{rehechas} se volvieron a conciliar")
    except (xmlrpc.client.Fault, OdooWriteUncertain, Exception) as e:
        return 'FALLO', f"en '{paso}': {str(e)[:300]}"


def corregir(odoo, diario_id, nombres):
    """--corregir AB-1043 ...: una factura ya trasladada cuyo total quedo
    distinto del CFDI de Amazon. Se regresa a borrador, el IVA se ajusta por la
    diferencia (como cuadrar_con_cfdi de 01) y se vuelve a publicar y conciliar."""
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    for nombre in nombres:
        f = odoo.execute_kw('account.move', 'search_read',
                            [[('name', '=', nombre), ('journal_id', '=', diario_id),
                              ('state', '=', 'posted')]],
                            {'fields': ['id', 'name', 'amount_total', 'payment_state',
                                        'l10n_mx_edi_cfdi_uuid']})
        if not f:
            log.error(f"{nombre}: no esta publicada en {DIARIO}.")
            continue
        f = f[0]
        uuid = normalizar_uuid(f['l10n_mx_edi_cfdi_uuid'])
        cur.execute("""SELECT total FROM somos_reyes.amz_b2b_invoices WHERE receipt_type = 'I'
                       AND UPPER(uuid) = %s LIMIT 1""", (uuid,))
        fila = cur.fetchone()
        if not fila:
            log.error(f"{nombre}: no encuentro el CFDI {uuid} en amz_b2b_invoices.")
            continue
        total_cfdi = round(float(fila['total']), 2)
        dif = round(total_cfdi - f['amount_total'], 2)
        if dif == 0 or abs(dif) > 0.05:
            log.info(f"{nombre}: total {f['amount_total']} vs CFDI {total_cfdi}: no se toca.")
            continue
        foto = foto_iva_cxc(odoo, f['id'])
        foto['iva'] = round(foto['iva'] - dif, 2)    # IVA es credito: balance negativo
        f['amount_total'] = total_cfdi                # lo que debe quedar
        estado, detalle = trasladar(odoo, diario_id, f, None, foto)
        log.info(f"{nombre}: {estado} {detalle}")
    cur.close()
    db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--aplicar', action='store_true', help='escribe en Odoo (sin esto, simula)')
    ap.add_argument('--limite', type=int, default=0, help='maximo de facturas a trasladar')
    ap.add_argument('--corregir', nargs='+', help='facturas de AMZB a igualar al total del CFDI')
    args = ap.parse_args()

    validar_entorno()
    log.info(f"16_traslado_amzb | entorno {ENTORNO} | {'APLICAR' if args.aplicar else 'SIMULACION'}")
    odoo = OdooModelProxy(timeout=300)
    diario = odoo.execute_kw('account.journal', 'search_read',
                             [[('code', '=', DIARIO), ('type', '=', 'sale')]],
                             {'fields': ['id', 'name'], 'limit': 1})
    if not diario:
        raise Exception(f"No existe el diario de ventas {DIARIO}.")
    diario_id = diario[0]['id']
    if args.corregir:
        if not args.aplicar:
            raise Exception("--corregir escribe: agregar --aplicar.")
        corregir(odoo, diario_id, args.corregir)
        return 0
    lock = candado(odoo)
    db = get_db_connection()
    uuids = cfdis_de_amazon(db)
    db.close()
    facturas = candidatas(odoo, diario_id, uuids)
    log.info(f"Diario {diario[0]['name']} (id {diario_id}) | candado {lock or 'sin candado'} | "
             f"{len(uuids)} CFDI de Amazon | {len(facturas)} factura(s) fuera de {DIARIO}")

    existentes = {m['name'] for m in odoo.execute_kw(
        'account.move', 'search_read', [[('journal_id', '=', diario_id)]], {'fields': ['name']})}
    filas, hechas = [], 0
    for f in facturas:
        nombre = uuids.get(normalizar_uuid(f['l10n_mx_edi_cfdi_uuid']))
        fila = {'factura': f['name'], 'so': f['invoice_origin'], 'fecha': f['invoice_date'],
                'fecha_contable': f['date'], 'total': f['amount_total'],
                'pago': f['payment_state'], 'uuid': f['l10n_mx_edi_cfdi_uuid'],
                'nombre_nuevo': nombre, 'estado': '', 'detalle': ''}
        if not nombre:
            fila['estado'], fila['detalle'] = 'SIN_FOLIO', 'el UUID no tiene serie-folio en amz_b2b_invoices'
        elif nombre in existentes:
            fila['estado'], fila['detalle'] = 'NOMBRE_OCUPADO', f"{nombre} ya existe en {DIARIO}"
        elif lock and str(f['date']) <= lock:
            fila['estado'], fila['detalle'] = 'PERIODO_CERRADO', f"fecha contable {f['date']} <= candado {lock}"
        elif not args.aplicar or (args.limite and hechas >= args.limite):
            fila['estado'] = 'SE_TRASLADARIA' if not args.aplicar else 'PENDIENTE (limite)'
        else:
            fila['estado'], fila['detalle'] = trasladar(odoo, diario_id, f, nombre)
            hechas += 1
            existentes.add(nombre)
            log.info(f"{f['name']} -> {nombre}: {fila['estado']} {fila['detalle']}")
            if fila['estado'] == 'FALLO':
                filas.append(fila)
                log.error(f"Se detiene: {f['name']} fallo {fila['detalle']}. Revisarla en Odoo "
                          f"(puede haber quedado en borrador o sin conciliar).")
                break
        filas.append(fila)

    os.makedirs(CARPETA, exist_ok=True)
    salida = os.path.join(CARPETA, f"traslado_amzb_{ENTORNO}_{datetime.now():%Y%m%d_%H%M}.csv")
    with open(salida, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0]) if filas else ['factura'])
        w.writeheader()
        w.writerows(filas)
    resumen = {}
    for fila in filas:
        resumen[fila['estado']] = resumen.get(fila['estado'], 0) + 1
    log.info(f"Resumen: {resumen}")
    log.info(f"CSV: {salida}")
    return 1 if resumen.get('FALLO') or resumen.get('REVISAR') else 0


if __name__ == '__main__':
    sys.exit(main())
