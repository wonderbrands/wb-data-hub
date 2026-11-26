"""
02_wm_xml_loader.py
-------------------
Consigue el XML de los CFDI que emitio el autofacturador de Walmart y lo deja
en su fila de la tabla de control: PENDING_WM_XML -> PENDING_WM_LINK. Solo
escribe MySQL; no toca Odoo. Solo aplica a las ventas que el cliente
autofacturo en Walmart (~8%); el resto las timbra 04.

DOS FUENTES
  SAT (por defecto)  Web service de descarga masiva, con la e.firma de la
                     empresa (_sat_descarga.py).
  ZIP (respaldo)     WM_XML_PATH = carpeta o .zip con los XML que descarga
                     Finanzas. Si la variable viene llena, se usa el ZIP y no
                     se llama al SAT.
REPOSITORIO DE CFDI DE WALMART. Todo XML timbrado por Fenix (PAC
ICA050422AA6, serie FI, nuestro RFC, tipo I) se guarda como fila CFDI:<UUID>
con marketplace='WALMART_FENIX', este o no pendiente. Despues:
  * las filas PENDING_WM_XML toman su XML del repositorio por UUID;
  * ligar_cfdis() liga los CFDI sueltos con su linea (crawler si lo hay;
    sin el: camino rapido, folio con el pedido y tramos de folio contra las
    peticiones de la API de Fenix). El miner (segunda pasada en el flujo)
    convierte esas ligas en filas <orden>:<UUID>.
El resto de lo que emite la empresa (Mercado Libre, Amazon, publico general)
se descarta.

COMO SE PIDE AL SAT (asincrono: lo que se pide hoy se descarga en otra corrida)
  1. Avanzar lo pedido antes: verificar las solicitudes abiertas y descargar
     sus paquetes.
  2. Pedir por DIA: una solicitud de CFDI EMITIDOS, tipo I, vigentes, de
     00:00:00 a 23:59:59, para cada dia con
       - CFDI de Walmart con UUID conocido y sin XML (fecha del crawler), o
       - ordenes PENDING_WM_UUID: el dia de la peticion de factura (API de
         Fenix) y los WM_SAT_DIAS_TRAS_PETICION siguientes, y
       - BARRIDO: cada uno de los ultimos WM_SAT_DIAS_BARRIDO dias, haya o no
         pendientes. Asi todo CFDI de Fenix llega al repositorio al dia
         siguiente aunque la API no tenga su peticion (pasa: el pedido
         600000121084541 tiene CFDI vigente y no sale en ninguna lista de la
         API). 04 no timbra una orden si hay un CFDI de Walmart sin ligar que
         podria ser suyo.
     Solo dias ya cerrados (anteriores a hoy en CDMX).
     SEGUNDA DESCARGA (definitiva). El PAC tiene hasta 72 h para entregar un
     CFDI al SAT: lo que se pide al dia siguiente puede llegar incompleto. Una
     solicitud de un dia D hecha antes de D + WM_SAT_DIAS_DEFINITIVA (4) es
     PROVISIONAL; cuando se cumple ese plazo, el dia se vuelve a pedir una
     vez (la segunda y ultima que permite el SAT). Si la primera ya se hizo
     despues del plazo, es definitiva y no se repite. Los CFDI de Walmart que
     solo aparecen en la definitiva se avisan por Slack (llegaron tarde).
  3. Pedir por FOLIO lo que siga faltando despues de descargar su dia (por
     ejemplo, un CFDI timbrado cerca de medianoche con la fecha de Fenix en el
     dia vecino).

SEGUIMIENTO. Cada solicitud al SAT es una fila de la misma tabla de control,
marketplace = 'SAT_DESCARGA' (no se crea tabla nueva):
  mkp_order_id  EMITIDOS:<AAAA-MM-DD>  o  FOLIO:<uuid>
  status        SOLICITADA -> TERMINADA -> DESCARGADA
                o SIN_CFDI, RECHAZADA, VENCIDA, ERROR
  raw_json      id_solicitud, pedida_en, definitiva, paquetes, paquetes
                descargados, ultima verificacion; 'anteriores' guarda la
                solicitud provisional cuando el dia se vuelve a pedir
  retry_count   veces que se pidio. El SAT solo acepta 2 solicitudes con los
                mismos parametros "de por vida" (codigo 5002): no se pasa de 2.
La clave unica (marketplace, mkp_order_id) deja una fila por dia: a lo mas una
solicitud provisional y una definitiva.

Un error del SAT no detiene el flujo: se registra en la fila, se avisa por
Slack y 03/04 siguen.

Variables:
  WM_XML_PATH                ZIP o carpeta (respaldo). Vacio = SAT.
  SAT_FIEL_CER_PATH / _B64, SAT_FIEL_KEY_PATH / _B64, SAT_FIEL_PASSWORD
  WM_SAT_MAX_SOLICITUDES     solicitudes nuevas por dia en una corrida (10)
  WM_SAT_MAX_FOLIOS          solicitudes por folio en una corrida (20)
  WM_SAT_DIAS_TRAS_PETICION  dias despues de la peticion de factura que se piden (1)
  WM_SAT_DIAS_BARRIDO        ultimos dias cerrados que se piden siempre (7; 0 = sin barrido)
  WM_SAT_DIAS_DEFINITIVA     dias despues de D a partir de los que la solicitud de D es definitiva (4)
  WM_SAT_SOLO_DIAS           pruebas: lista AAAA-MM-DD separada por comas. Si viene llena, solo se
                             piden esos dias (sin barrido, sin pendientes, sin folios)
  WM_DRY_RUN=1              no escribe ni llama al SAT; dice que haria
"""

import io
import json
import os
import re
import sys
import base64
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from _wm_common import (configurar_log, log, banner, get_db_connection, leer_cfdi, notify_slack,
                        a_json, ahora_cdmx, a_datetime, TBL_BILLING, MARKETPLACE, RFC_EMISOR,
                        ST_PENDING_WM_XML, ST_PENDING_WM_LINK, ST_PENDING_WM_UUID, lotes,
                        MKP_FENIX, PRE_RECIBIDO, es_cfdi_walmart, guardar_cfdis_walmart, ligar_cfdis,
                        llenar_xml_desde_repositorio)
import _sat_descarga as sat

DRY_RUN = os.getenv('WM_DRY_RUN', '0').strip() in ('1', 'true', 'TRUE')
WM_XML_PATH = os.getenv('WM_XML_PATH', '').strip()
MAX_SOLICITUDES = int(os.getenv('WM_SAT_MAX_SOLICITUDES', '10'))
MAX_FOLIOS = int(os.getenv('WM_SAT_MAX_FOLIOS', '20'))
DIAS_TRAS_PETICION = int(os.getenv('WM_SAT_DIAS_TRAS_PETICION', '1'))
DIAS_BARRIDO = int(os.getenv('WM_SAT_DIAS_BARRIDO', '7'))
DIAS_DEFINITIVA = int(os.getenv('WM_SAT_DIAS_DEFINITIVA', '4'))
SOLO_DIAS = {d.strip() for d in os.getenv('WM_SAT_SOLO_DIAS', '').split(',') if d.strip()}

MKP_SAT = 'SAT_DESCARGA'
SAT_SOLICITADA, SAT_TERMINADA, SAT_DESCARGADA = 'SOLICITADA', 'TERMINADA', 'DESCARGADA'
SAT_SIN_CFDI, SAT_RECHAZADA, SAT_VENCIDA, SAT_ERROR = 'SIN_CFDI', 'RECHAZADA', 'VENCIDA', 'ERROR'
SAT_ABIERTAS = (SAT_SOLICITADA, SAT_TERMINADA)
SAT_REPETIBLES = (SAT_VENCIDA, SAT_ERROR, SAT_RECHAZADA)   # se pueden volver a pedir
MAX_PEDIDOS_MISMOS_PARAMETROS = 2

# Un CFDI pesa ~5-10 KB. Algo mucho mayor no es lo que esperamos.
MAX_BYTES_XML = 2 * 1024 * 1024


# =============================================================================
# APLICAR XML A LA TABLA DE CONTROL (comun a SAT y ZIP)
# =============================================================================

def xml_de_zip(contenido_o_ruta):
    """(nombre, bytes) de cada .xml de un ZIP (bytes o ruta), en memoria."""
    origen = io.BytesIO(contenido_o_ruta) if isinstance(contenido_o_ruta, bytes) else contenido_o_ruta
    with zipfile.ZipFile(origen) as zf:
        for info in zf.infolist():
            if info.filename.lower().endswith('.xml') and info.file_size <= MAX_BYTES_XML:
                yield os.path.basename(info.filename), zf.read(info)


PATRON_UUID = re.compile(r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')


def xml_de_carpeta(ruta, solo_uuids=None):
    """La carpeta de Finanzas trae TODO lo emitido por la empresa en el mes
    (53,380 XML en octubre). Si el nombre del archivo empieza con un UUID
    (asi los nombra el SAT: '<uuid>@...xml') y no esta en solo_uuids, ni se
    abre. Los que no siguen ese patron se leen siempre."""
    for raiz, _, archivos in os.walk(ruta):
        for nombre in archivos:
            p = os.path.join(raiz, nombre)
            m = PATRON_UUID.match(nombre)
            if solo_uuids is not None and m and m.group(0).upper() not in solo_uuids:
                continue
            if nombre.lower().endswith('.xml') and os.path.getsize(p) <= MAX_BYTES_XML:
                with open(p, 'rb') as fh:
                    yield nombre, fh.read()


def uuids_en_repositorio(db, uuids):
    """Cuales de estos UUID ya tienen fila CFDI:<UUID> en el repositorio."""
    existentes = set()
    cur = db.cursor()
    for chunk in lotes(sorted(uuids), 500):
        cur.execute(f"""SELECT cfdi_uuid FROM {TBL_BILLING}
                        WHERE marketplace = %s AND mkp_order_id IN ({','.join(['%s'] * len(chunk))})""",
                    tuple([MKP_FENIX] + [f"CFDI:{u}" for u in chunk]))
        existentes.update(u.upper() for (u,) in cur.fetchall() if u)
    cur.close()
    return existentes


def aplicar_xmls(db, xmls, stats, nuevos_walmart=None):
    """Guarda en el repositorio cada CFDI timbrado por Fenix y pone el XML en
    su fila PENDING_WM_XML si la hay. Devuelve los UUID cargados a filas.
    nuevos_walmart (lista): recibe los UUID de Walmart que no estaban en el
    repositorio."""
    validos, repositorio = {}, []
    for nombre, contenido in xmls:
        stats['xml_leidos'] += 1
        try:
            cfdi = leer_cfdi(contenido)
        except Exception as e:
            stats['xml_ilegibles'] += 1
            log.warning(f"{nombre}: no es un CFDI legible ({e})")
            continue
        if cfdi['emisor_rfc'] != RFC_EMISOR or cfdi['tipo'] != 'I':
            stats['xml_no_aplican'] += 1
            continue
        if es_cfdi_walmart(cfdi) and cfdi['uuid'] not in validos:
            repositorio.append((contenido, cfdi))
        validos.setdefault(cfdi['uuid'], contenido)
    stats['xml_de_walmart_leidos'] += len(repositorio)
    ya = uuids_en_repositorio(db, {c['uuid'].upper() for _, c in repositorio})
    nuevos = sorted({c['uuid'].upper() for _, c in repositorio} - ya)
    stats['xml_de_walmart_nuevos_al_repositorio'] += len(nuevos)
    if nuevos_walmart is not None:
        nuevos_walmart.extend(nuevos)
    guardar_cfdis_walmart(db, repositorio, dry_run=DRY_RUN)

    cur = db.cursor()
    pendientes = {}
    for chunk in lotes(list(validos), 500):
        cur.execute(f"""SELECT id, cfdi_uuid FROM {TBL_BILLING}
                        WHERE marketplace = %s AND status = %s
                          AND cfdi_uuid IN ({','.join(['%s'] * len(chunk))})""",
                    tuple([MARKETPLACE, ST_PENDING_WM_XML] + chunk))
        pendientes.update({u.upper(): rid for rid, u in cur.fetchall()})

    cargados = set()
    for uuid, rid in pendientes.items():
        cargados.add(uuid)
        if DRY_RUN:
            continue
        cur.execute(f"""UPDATE {TBL_BILLING}
                        SET xml_data = %s, status = %s, processed_at = NOW(), error_log = NULL
                        WHERE id = %s AND status = %s""",
                    (base64.b64encode(validos[uuid]).decode('ascii'), ST_PENDING_WM_LINK,
                     rid, ST_PENDING_WM_XML))
    if not DRY_RUN:
        db.commit()
    cur.close()
    stats['xml_cargados'] += len(cargados)
    stats['xml_sin_fila_pendiente'] += len(validos) - len(cargados)
    return cargados


# =============================================================================
# SEGUIMIENTO DE SOLICITUDES AL SAT
# =============================================================================

def leer_solicitudes(db):
    cur = db.cursor(dictionary=True)
    cur.execute(f"""SELECT id, mkp_order_id, status, raw_json, retry_count FROM {TBL_BILLING}
                    WHERE marketplace = %s""", (MKP_SAT,))
    filas = {}
    for r in cur.fetchall():
        r['raw'] = json.loads(r['raw_json']) if isinstance(r['raw_json'], str) else (r['raw_json'] or {})
        filas[r['mkp_order_id']] = r
    cur.close()
    return filas


def guardar_solicitud(db, clave, status, raw, error_log=None, nueva_peticion=False):
    """Inserta o actualiza la fila de seguimiento. nueva_peticion suma 1 a
    retry_count (veces que se pidio al SAT)."""
    if DRY_RUN:
        return
    cur = db.cursor()
    cur.execute(f"""
        INSERT INTO {TBL_BILLING} (marketplace, mkp_order_id, raw_json, status, error_log,
                                   retry_count, retry_count_loader, processed_at)
        VALUES (%s, %s, %s, %s, %s, %s, 0, NOW())
        ON DUPLICATE KEY UPDATE raw_json = VALUES(raw_json), status = VALUES(status),
            error_log = VALUES(error_log), processed_at = NOW(),
            retry_count = retry_count + %s
    """, (MKP_SAT, clave, a_json(raw), status, error_log,
          1 if nueva_peticion else 0, 1 if nueva_peticion else 0))
    db.commit()
    cur.close()


def pendientes_por_dia(db):
    """{dia: {uuid,...}} de los CFDI de Walmart que esperan su XML."""
    cur = db.cursor()
    cur.execute(f"""SELECT cfdi_uuid, JSON_UNQUOTE(JSON_EXTRACT(raw_json, '$.fenix.fecha_creacion'))
                    FROM {TBL_BILLING} WHERE marketplace = %s AND status = %s""",
                (MARKETPLACE, ST_PENDING_WM_XML))
    por_dia = {}
    for uuid, fecha in cur.fetchall():
        if uuid and fecha and fecha != 'null':
            por_dia.setdefault(fecha[:10], set()).add(uuid.upper())
    cur.close()
    return por_dia


def dias_por_peticion(db):
    """Dias a pedir para las ordenes PENDING_WM_UUID: el de cada peticion de
    factura RECIBIDA (API de Fenix) y los DIAS_TRAS_PETICION siguientes."""
    cur = db.cursor()
    cur.execute(f"SELECT mkp_order_id FROM {TBL_BILLING} WHERE marketplace = %s AND status = %s",
                (MARKETPLACE, ST_PENDING_WM_UUID))
    pedidos = {r[0] for r in cur.fetchall()}
    dias = set()
    if pedidos:
        cur.execute(f"""SELECT raw_json FROM {TBL_BILLING}
                        WHERE marketplace = %s AND mkp_order_id LIKE 'PRECFDI:%%' AND status = %s""",
                    (MKP_FENIX, PRE_RECIBIDO))
        for (raw,) in cur.fetchall():
            raw = json.loads(raw) if isinstance(raw, (str, bytes)) else (raw or {})
            if raw.get('orderno') in pedidos and raw.get('peticion_cdmx'):
                d = a_datetime(raw['peticion_cdmx'])
                for i in range(DIAS_TRAS_PETICION + 1):
                    dias.add(f"{d + timedelta(days=i):%Y-%m-%d}")
    cur.close()
    return dias


def llenar_desde_repositorio(db, stats):
    """Las filas PENDING_WM_XML cuyo UUID ya esta en el repositorio toman de ahi su XML."""
    stats['xml_desde_repositorio'] += llenar_xml_desde_repositorio(db, dry_run=DRY_RUN)


def dia_definitivo(dia):
    """Primer dia (AAAA-MM-DD) en que una solicitud del dia `dia` ya es definitiva."""
    return f"{datetime.strptime(dia, '%Y-%m-%d') + timedelta(days=DIAS_DEFINITIVA):%Y-%m-%d}"


def es_definitiva(clave, fila):
    """Una solicitud por dia es definitiva si se hizo en o despues de D + DIAS_DEFINITIVA.
    Las de folio siempre lo son. Sin fecha de solicitud se toma como provisional."""
    if not clave.startswith('EMITIDOS:'):
        return True
    pedida = (fila['raw'] or {}).get('pedida_en') or ''
    return bool(pedida) and pedida[:10] >= dia_definitivo(clave.split(':', 1)[1])


def falta_definitiva(clave, fila):
    """Dia ya descargado con una solicitud provisional, al que ya le toca la
    definitiva y al que el SAT todavia le acepta una solicitud."""
    return (clave.startswith('EMITIDOS:') and fila is not None
            and fila['status'] in (SAT_DESCARGADA, SAT_SIN_CFDI)
            and (fila['retry_count'] or 0) < MAX_PEDIDOS_MISMOS_PARAMETROS
            and not es_definitiva(clave, fila)
            and f"{ahora_cdmx():%Y-%m-%d}" >= dia_definitivo(clave.split(':', 1)[1]))


def puede_pedirse(fila, clave=''):
    if fila is None:
        return True
    if fila['status'] in SAT_REPETIBLES and (fila['retry_count'] or 0) < MAX_PEDIDOS_MISMOS_PARAMETROS:
        return True
    return falta_definitiva(clave, fila)


# =============================================================================
# PASOS
# =============================================================================

def avanzar_abiertas(db, sesion, solicitudes, stats):
    for clave, fila in solicitudes.items():
        if fila['status'] not in SAT_ABIERTAS:
            continue
        raw = fila['raw']
        try:
            if fila['status'] == SAT_SOLICITADA:
                v = sat.verificar(sesion.fiel, sesion.token(), raw['id_solicitud'])
                raw['verificacion'] = v
                stats[f"verificacion_{v['estado_txt']}"] += 1
                if v['estado'] in (1, 2):
                    guardar_solicitud(db, clave, SAT_SOLICITADA, raw)
                    continue
                if v['estado'] == 3 and v['paquetes']:
                    raw['paquetes'] = v['paquetes']
                    raw.setdefault('descargados', [])
                    fila['status'] = SAT_TERMINADA
                    guardar_solicitud(db, clave, SAT_TERMINADA, raw)
                elif v['estado'] == 3 or v['codigo_estado'] == '5004':
                    fila['status'] = SAT_SIN_CFDI
                    guardar_solicitud(db, clave, SAT_SIN_CFDI, raw, v['mensaje'])
                    continue
                else:
                    nuevo = {4: SAT_ERROR, 5: SAT_RECHAZADA, 6: SAT_VENCIDA}.get(v['estado'], SAT_ERROR)
                    if raw.get('anteriores'):
                        # Fallo la definitiva: el dia conserva lo que bajo la provisional.
                        raw['definitiva_fallo'] = f"{nuevo}: {v['codigo_estado']} {v['mensaje']}"
                        nuevo = raw['anteriores'][-1]['status']
                    fila['status'] = nuevo
                    guardar_solicitud(db, clave, nuevo, raw,
                                      f"{v['codigo_estado']} {v['mensaje']}")
                    continue

            # TERMINADA: bajar los paquetes que falten. Se guarda despues de
            # cada uno: el SAT limita cuantas veces se descarga un paquete.
            for paquete in raw.get('paquetes', []):
                if paquete in raw.get('descargados', []):
                    continue
                cod, mensaje, datos = sat.descargar(sesion.fiel, sesion.token(), paquete)
                if not datos:
                    raw.setdefault('errores_descarga', []).append(f"{paquete}: {cod} {mensaje}")
                    guardar_solicitud(db, clave, SAT_TERMINADA, raw, f"{cod} {mensaje}")
                    stats['paquetes_con_error'] += 1
                    continue
                nuevos = []
                cargados = aplicar_xmls(db, xml_de_zip(datos), stats, nuevos)
                raw.setdefault('descargados', []).append(paquete)
                raw.setdefault('uuids_cargados', []).extend(sorted(cargados))
                raw.setdefault('walmart_nuevos', []).extend(nuevos)
                stats['paquetes_descargados'] += 1
                guardar_solicitud(db, clave, SAT_TERMINADA, raw)
            if set(raw.get('paquetes', [])) <= set(raw.get('descargados', [])):
                fila['status'] = SAT_DESCARGADA
                guardar_solicitud(db, clave, SAT_DESCARGADA, raw)
                # Segunda descarga de un dia: lo de Walmart que no trajo la
                # provisional llego tarde al SAT. Si la orden ya se timbro a
                # publico general, 01 la marcara DOBLE_CFDI al ligarla.
                if raw.get('anteriores') and raw.get('walmart_nuevos'):
                    stats['walmart_llegaron_tarde'] += len(raw['walmart_nuevos'])
                    log.warning(f"{clave}: {len(raw['walmart_nuevos'])} CFDI de Walmart solo aparecieron "
                                f"en la descarga definitiva: {', '.join(raw['walmart_nuevos'][:10])}")
                    notify_slack(f"Walmart 02: {len(raw['walmart_nuevos'])} CFDI de Walmart del "
                                 f"{clave.split(':', 1)[1]} llegaron tarde al SAT (no estaban en la primera "
                                 f"descarga). Revisar que su orden no se haya timbrado a publico general.")
        except sat.ErrorSAT as e:
            stats['errores_sat'] += 1
            log.error(f"{clave}: {e}")
            guardar_solicitud(db, clave, fila['status'], raw, str(e)[:900])


def pedir(db, sesion, clave, fila, funcion, stats):
    """Hace una solicitud nueva y la registra. Si es la segunda (definitiva) de
    un dia ya descargado y falla, el dia se queda como estaba: lo descargado
    sigue valiendo y no se vuelve a intentar (ya se gastaron las 2)."""
    segunda = fila is not None and fila['status'] in (SAT_DESCARGADA, SAT_SIN_CFDI)
    if DRY_RUN:
        log.info(f"  [DRY RUN] pediria {clave}{' (definitiva)' if segunda else ''}")
        stats['solicitudes_nuevas'] += 1
        return

    def fallo(raw_error, mensaje):
        if segunda:
            previo = dict(fila['raw'] or {})
            previo['definitiva_fallo'] = {'pedida_en': f"{ahora_cdmx():%Y-%m-%d %H:%M}", 'error': mensaje}
            guardar_solicitud(db, clave, fila['status'], previo, mensaje, nueva_peticion=True)
        else:
            guardar_solicitud(db, clave, SAT_ERROR, raw_error, mensaje, nueva_peticion=True)

    try:
        r = funcion()
    except sat.ErrorSAT as e:
        stats['errores_sat'] += 1
        log.error(f"{clave}: {e}")
        fallo({'error': str(e)[:900]}, str(e)[:900])
        return
    raw = {'id_solicitud': r['id_solicitud'], 'cod_estatus': r['cod_estatus'],
           'mensaje': r['mensaje'], 'pedida_en': f"{ahora_cdmx():%Y-%m-%d %H:%M}"}
    raw['definitiva'] = es_definitiva(clave, {'raw': raw})
    if r['cod_estatus'] == '5000' and r['id_solicitud']:
        if segunda:
            # Se conserva el resumen de la solicitud provisional.
            previo = fila['raw'] or {}
            raw['anteriores'] = previo.get('anteriores', []) + [{
                'id_solicitud': previo.get('id_solicitud'), 'pedida_en': previo.get('pedida_en'),
                'status': fila['status'], 'paquetes': previo.get('paquetes', []),
                'walmart_nuevos': len(previo.get('walmart_nuevos', []))}]
            stats['solicitudes_definitivas'] += 1
        guardar_solicitud(db, clave, SAT_SOLICITADA, raw, nueva_peticion=True)
        stats['solicitudes_nuevas'] += 1
    else:
        fallo(raw, f"{r['cod_estatus']} {r['mensaje']}")
        stats['solicitudes_rechazadas'] += 1
        log.warning(f"{clave}: el SAT no acepto la solicitud: {r['cod_estatus']} {r['mensaje']}")


def pedir_dias(db, sesion, solicitudes, dias, stats):
    hoy = f"{ahora_cdmx():%Y-%m-%d}"
    nuevas = 0
    for dia in sorted(dias):
        if dia >= hoy or nuevas >= MAX_SOLICITUDES:
            continue
        clave = f"EMITIDOS:{dia}"
        if not puede_pedirse(solicitudes.get(clave), clave):
            continue
        pedir(db, sesion, clave, solicitudes.get(clave),
              lambda d=dia: sat.solicitar_emitidos(sesion.fiel, sesion.token(), f"{d}T00:00:00",
                                                   f"{d}T23:59:59", tipo_comprobante='I',
                                                   estado_comprobante='Vigente'), stats)
        nuevas += 1


def pedir_folios(db, sesion, solicitudes, por_dia, stats):
    """UUID que siguen sin XML aunque su dia ya se descargo de forma
    definitiva (antes de eso todavia pueden llegar en la segunda descarga)."""
    nuevas = 0
    for dia, uuids in sorted(por_dia.items()):
        clave_dia = f"EMITIDOS:{dia}"
        fila_dia = solicitudes.get(clave_dia)
        if not fila_dia or fila_dia['status'] not in (SAT_DESCARGADA, SAT_SIN_CFDI):
            continue
        if (not es_definitiva(clave_dia, fila_dia)
                and (fila_dia['retry_count'] or 0) < MAX_PEDIDOS_MISMOS_PARAMETROS):
            continue
        for uuid in sorted(uuids):
            if nuevas >= MAX_FOLIOS:
                return
            clave = f"FOLIO:{uuid}"
            if not puede_pedirse(solicitudes.get(clave)):
                continue
            pedir(db, sesion, clave, solicitudes.get(clave),
                  lambda u=uuid: sat.solicitar_folio(sesion.fiel, sesion.token(), u), stats)
            nuevas += 1


def modo_sat(db, stats):
    if not sat.fiel_configurada():
        if not DRY_RUN:
            log.warning('Sin e.firma configurada (SAT_FIEL_*): no se descarga nada del SAT en esta corrida.')
            return
        log.warning('DRY RUN sin e.firma: solo se calcula que se pediria.')
        sesion = None
    else:
        fiel = sat.cargar_fiel()
        if fiel['rfc'] != RFC_EMISOR:
            raise sat.ErrorSAT(f"La e.firma es del RFC {fiel['rfc']}, no de {RFC_EMISOR}.")
        log.info(f"e.firma {fiel['rfc']} vigente hasta {fiel['vence']:%Y-%m-%d}")
        if fiel['vence'] - timedelta(days=30) < datetime.now(timezone.utc):
            notify_slack(f"La e.firma de {fiel['rfc']} vence el {fiel['vence']:%Y-%m-%d}: renovarla y "
                         f"actualizar los secrets de Kestra.")
        sesion = sat.SesionSAT(fiel)

    solicitudes = leer_solicitudes(db)
    if not DRY_RUN:
        avanzar_abiertas(db, sesion, solicitudes, stats)
    if SOLO_DIAS:
        # Pruebas: solo los dias pedidos; nada de barrido, pendientes ni folios.
        log.warning(f"WM_SAT_SOLO_DIAS: solo se piden {sorted(SOLO_DIAS)}")
        pedir_dias(db, sesion, solicitudes, SOLO_DIAS, stats)
        return
    por_dia = pendientes_por_dia(db)
    dias_pet = dias_por_peticion(db)
    hoy = ahora_cdmx()
    barrido = {f"{hoy - timedelta(days=i):%Y-%m-%d}" for i in range(1, DIAS_BARRIDO + 1)}
    # Dias con solicitud provisional a los que ya les toca la definitiva,
    # esten o no en el barrido (por ejemplo, si el flujo no corrio unos dias).
    definitivas = {c.split(':', 1)[1] for c, f in solicitudes.items() if falta_definitiva(c, f)}
    log.info(f"CFDI de Walmart sin XML: {sum(len(v) for v in por_dia.values())} en {len(por_dia)} dia(s) | "
             f"dias por peticiones sin ligar (PENDING_WM_UUID): {len(dias_pet)} | barrido: {len(barrido)} dia(s) | "
             f"segunda descarga pendiente: {len(definitivas)} dia(s)")
    # Primero el barrido y las definitivas (protegen a 04), luego lo demas.
    pedir_dias(db, sesion, solicitudes, barrido | definitivas, stats)
    solicitudes = leer_solicitudes(db) if not DRY_RUN else solicitudes
    pedir_dias(db, sesion, solicitudes, set(por_dia) | dias_pet, stats)
    pedir_folios(db, sesion, solicitudes, por_dia, stats)


def main():
    fuente = f"ZIP {WM_XML_PATH}" if WM_XML_PATH else 'SAT (descarga masiva)'
    banner(f"02 - XML DE LOS CFDI DE WALMART | fuente: {fuente}{' | DRY RUN' if DRY_RUN else ''}")
    db = get_db_connection()
    stats = Counter()
    try:
        llenar_desde_repositorio(db, stats)
        if WM_XML_PATH:
            if not os.path.exists(WM_XML_PATH):
                raise Exception(f"No existe WM_XML_PATH={WM_XML_PATH}")
            pendientes = {u for us in pendientes_por_dia(db).values() for u in us}
            log.info(f"CFDI de Walmart esperando XML: {len(pendientes)}")
            # Se leen todos los XML (no solo los pendientes): los de Walmart
            # van al repositorio para ligarlos.
            origen = xml_de_carpeta(WM_XML_PATH) if os.path.isdir(WM_XML_PATH) else xml_de_zip(WM_XML_PATH)
            aplicar_xmls(db, origen, stats)
        else:
            modo_sat(db, stats)
        ligas = ligar_cfdis(db, dry_run=DRY_RUN)
        for k, v in ligas.items():
            stats[f"repositorio_{k}"] += v
    except Exception as e:
        # No se corta el flujo: 03 replica lo que ya tiene XML y 04 emite lo suyo.
        log.exception('02 fallo')
        notify_slack(f"Walmart 02 (XML del SAT) fallo: {str(e)[:300]}")
    finally:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {TBL_BILLING} WHERE marketplace = %s AND status = %s",
                    (MARKETPLACE, ST_PENDING_WM_XML))
        faltan = cur.fetchone()[0]
        cur.close()
        db.close()
    for k, v in sorted(stats.items()):
        log.info(f"  {k:<28} {v:>6}")
    log.info(f"CFDI de Walmart que siguen sin XML: {faltan}")


if __name__ == '__main__':
    configurar_log()
    main()
