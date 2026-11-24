"""
_fenix_api.py
-------------
Cliente de la API_SELLERS de Fenix Invoice (la plataforma de facturacion de
Walmart). Manual: "Fenix API.pdf" (V.09.04.25) y la especificacion OpenAPI en
<FENIX_API_URL>/apisellers-docs.

Autenticacion: Basic (usuario y contrasena de un usuario con ROLE_EMPRESA_API).
El servidor es HTTP sin TLS: asi lo publica Walmart/Fenix. Credenciales en el
.env: FENIX_WALMART_USER, FENIX_WALMART_PASSWORD.

Solo se usan metodos de LECTURA que no cambian estado en Fenix:
  GET /precfdi/listRecibidosDelSeller          pre-CFDI que ya tienen su CFDI
  GET /precfdi/listRecibidosDelSellerConError  pre-CFDI que tuvieron error al timbrar
  GET /precfdi/listNuevos                      pre-CFDI sin atender
  GET /api/v1/invoicedetail/bloqueados         lineas detenidas por falta de serie/lote/pedimento
Los descargar* marcan la peticion como descargada y no traen nada en el modo
EN_AUTOMATICO de la cuenta: no se usan.

Lo que se midio el 23-nov-2026 (regla 6):
  * Un pre-CFDI es una PETICION DE FACTURA del cliente en Walmart, por linea
    (iderp = <orden>-<linea>, igual que FTM.so_prime_line_sub_line_no).
  * La API NO devuelve el UUID ni el estado del CFDI timbrado.
  * listRecibidos* ignoran el rango de fechas: regresan siempre todo el
    conjunto (1,258 recibidos y 209 con error, de mayo a noviembre). Las fechas
    deben ir como 'yyyy-MM-ddTHH:mm:ss' o el servidor responde 500.
  * invoiceno empieza casi siempre con la hora UTC de la peticion
    (AAAAMMDDhhmmss). Fenix timbra en tandas cada ~30 min: el XML trae una
    Fecha (hora de CDMX) 2-11 min despues de esa hora menos 6 h.
"""

import os
import re
import json
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE_URL = os.getenv('FENIX_API_URL', 'http://34.69.53.221:9301').rstrip('/')
TIMEOUT = int(os.getenv('FENIX_TIMEOUT_SEG', '120'))
# listRecibidos* exige el rango aunque lo ignore.
RANGO = {'inicio': '2020-01-01T00:00:00', 'fin': '2099-12-31T23:59:59'}

_TS = re.compile(r'^(20\d{12})')


class ErrorFenix(Exception):
    pass


def credenciales_configuradas():
    return bool(os.getenv('FENIX_WALMART_USER') and os.getenv('FENIX_WALMART_PASSWORD'))


def _auth():
    u, p = os.getenv('FENIX_WALMART_USER', ''), os.getenv('FENIX_WALMART_PASSWORD', '')
    if not (u and p):
        raise ErrorFenix('Faltan FENIX_WALMART_USER / FENIX_WALMART_PASSWORD.')
    return 'Basic ' + base64.b64encode(f"{u}:{p}".encode('utf-8')).decode('ascii')


def _get(path, params=None, intentos=3):
    """GET con reintento: son lecturas, repetirlas no cambia nada."""
    url = BASE_URL + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'Authorization': _auth()})
    ultimo = None
    for i in range(intentos):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            cuerpo = e.read()[:300].decode('utf-8', 'replace')
            # 404 "Sin resultados" es una lista vacia, no un error.
            if e.code == 404 and 'Sin resultados' in cuerpo:
                return []
            if e.code in (401, 403):
                raise ErrorFenix(f"{path}: credenciales rechazadas ({e.code}).")
            ultimo = f"{path}: HTTP {e.code} {cuerpo}"
        except Exception as e:
            ultimo = f"{path}: {e}"
        time.sleep(5 * (i + 1))
    raise ErrorFenix(ultimo)


def listar_recibidos():
    return _get('/precfdi/listRecibidosDelSeller', RANGO)


def listar_con_error():
    return _get('/precfdi/listRecibidosDelSellerConError', RANGO)


def listar_nuevos():
    return _get('/precfdi/listNuevos')


def listar_bloqueados():
    return _get('/api/v1/invoicedetail/bloqueados')


def peticion_cdmx(invoiceno):
    """Hora de la peticion en CDMX a partir del invoiceno, o None si no la trae
    (algunos invoiceno son la orden o '<orden>-<linea>')."""
    m = _TS.match(invoiceno or '')
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y%m%d%H%M%S') - timedelta(hours=6)
    except ValueError:
        return None
