#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=======================================================================
 billing_guard.py  --  Blindaje de concurrencia e idempotencia
=======================================================================

Modulo de apoyo para facturacion_automatica_1a1.py. Aporta cuatro piezas:

  1. GlobalLease   : exclusion mutua real entre PROCESOS (no entre
                     ejecuciones de Kestra), con TTL, heartbeat y fencing.
  2. OrderClaim    : maquina de estados por orden de venta. Garantiza que
                     una orden solo avanza una vez por el flujo
                     create -> post -> stamp, y permite reanudar.
  3. SafeOdooProxy : reintentos SOLO en metodos de lectura. Las escrituras
                     que fallan por red nunca se reintentan a ciegas: se
                     reconcilian por clave de idempotencia.
  4. find_invoices_by_origin : deteccion de duplicados que SI ve mas de
                     una factura (el bug del dict que oculto 549 casos).

Clave de idempotencia
---------------------
Se escribe en account.move.ref con el formato:

    AUTOINV:<order_name>

Ese campo hoy se manda vacio ('ref': ''), asi que esta libre. Sirve para
que, tras un 502 en el create, el script pueda PREGUNTARLE A ODOO si la
factura existe en vez de crear otra.

Endurecimiento opcional (ver odoo_idempotency_index.sql): un indice unico
parcial en PostgreSQL convierte la garantia de "por convencion" en
"imposible por construccion".
"""

import os
import time
import uuid
import socket
import logging
import threading
import xmlrpc.client

import mysql.connector
from mysql.connector import Error as MySQLError

log = logging.getLogger(__name__)

IDEMPOTENCY_PREFIX = 'AUTOINV:'


def make_idempotency_key(order_name):
    return f'{IDEMPOTENCY_PREFIX}{order_name}'


# =======================================================================
# 1. LEASE GLOBAL
# =======================================================================

class LeaseNotAcquired(Exception):
    """Otro proceso tiene el lease vigente. No se debe facturar."""


class LeaseLost(Exception):
    """Perdimos el lease mientras trabajabamos. Hay que abortar YA."""


class GlobalLease:
    """Exclusion mutua entre procesos, con expiracion automatica.

    Por que no basta con `concurrency: limit: 1` de Kestra:
      - Kestra libera el slot al marcar una ejecucion como KILLED, aunque
        el contenedor y el proceso Python sigan vivos unos segundos mas.
      - No cubre corridas manuales, otro entorno Kestra, ni backfills.
      - El lease protege el recurso real (Odoo), no la contabilidad del
        orquestador.

    Uso:
        with GlobalLease('facturacion_1a1') as lease:
            ...
            lease.check()      # fencing antes de cada escritura critica
    """

    def __init__(self, name, db_config, ttl_seconds=300, heartbeat_seconds=60,
                 holder_id=None):
        self.name = name
        self.db_config = db_config
        self.ttl = ttl_seconds
        self.heartbeat_interval = heartbeat_seconds
        self.holder_id = holder_id or (
            f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}')
        self._stop = threading.Event()
        self._thread = None
        self._lost = False

    # -- infraestructura ----------------------------------------------------
    def _conn(self):
        return mysql.connector.connect(**self.db_config)

    def ensure_schema(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS billing_process_lease (
            lease_name   VARCHAR(64)  NOT NULL PRIMARY KEY,
            holder_id    VARCHAR(160) NOT NULL,
            acquired_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            heartbeat_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at   TIMESTAMP    NOT NULL,
            info         VARCHAR(255) NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(ddl)
        conn.commit()
        cur.close()
        conn.close()

    # -- ciclo de vida ------------------------------------------------------
    def acquire(self):
        """Toma el lease si esta libre o vencido. Atomico via UPSERT."""
        self.ensure_schema()
        conn = self._conn()
        cur = conn.cursor()
        try:
            # Si la fila no existe -> se inserta.
            # Si existe y esta vencida -> se reasigna.
            # Si existe y esta vigente -> los IF() dejan los valores intactos.
            cur.execute(
                """
                INSERT INTO billing_process_lease
                       (lease_name, holder_id, acquired_at, heartbeat_at, expires_at, info)
                VALUES (%s, %s, NOW(), NOW(), NOW() + INTERVAL %s SECOND, %s)
                ON DUPLICATE KEY UPDATE
                    holder_id    = IF(expires_at < NOW(), VALUES(holder_id),    holder_id),
                    acquired_at  = IF(expires_at < NOW(), NOW(),                acquired_at),
                    heartbeat_at = IF(expires_at < NOW(), NOW(),                heartbeat_at),
                    info         = IF(expires_at < NOW(), VALUES(info),         info),
                    expires_at   = IF(expires_at < NOW(), VALUES(expires_at),   expires_at)
                """,
                (self.name, self.holder_id, self.ttl, f'pid={os.getpid()}'))
            conn.commit()

            cur.execute(
                'SELECT holder_id, heartbeat_at, expires_at '
                'FROM billing_process_lease WHERE lease_name = %s', (self.name,))
            holder, heartbeat, expires = cur.fetchone()
        finally:
            cur.close()
            conn.close()

        if holder != self.holder_id:
            raise LeaseNotAcquired(
                f'El lease "{self.name}" lo tiene {holder} '
                f'(ultimo heartbeat {heartbeat}, vence {expires}). '
                f'Este proceso no facturara.')

        log.info(f'Lease "{self.name}" adquirido por {self.holder_id} (TTL {self.ttl}s)')
        self._start_heartbeat()
        return self

    def renew(self):
        """Extiende el lease. Devuelve False si lo perdimos (fencing)."""
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute(
                'UPDATE billing_process_lease '
                '   SET heartbeat_at = NOW(), expires_at = NOW() + INTERVAL %s SECOND '
                ' WHERE lease_name = %s AND holder_id = %s',
                (self.ttl, self.name, self.holder_id))
            conn.commit()
            return cur.rowcount == 1
        finally:
            cur.close()
            conn.close()

    def release(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute('DELETE FROM billing_process_lease '
                        ' WHERE lease_name = %s AND holder_id = %s',
                        (self.name, self.holder_id))
            conn.commit()
            cur.close()
            conn.close()
            log.info(f'Lease "{self.name}" liberado.')
        except MySQLError as exc:
            log.warning(f'No se pudo liberar el lease (vencera solo en {self.ttl}s): {exc}')

    def check(self):
        """FENCING. Llamar antes de cada escritura critica en Odoo.

        Si el heartbeat fallo, este proceso ya no es el dueno y debe
        detenerse ANTES de crear otra factura.
        """
        if self._lost:
            raise LeaseLost(
                f'Se perdio el lease "{self.name}". Otro proceso pudo haberlo tomado. '
                f'Se aborta para no duplicar facturas.')

    def _start_heartbeat(self):
        def beat():
            while not self._stop.wait(self.heartbeat_interval):
                try:
                    if not self.renew():
                        self._lost = True
                        log.error(f'LEASE PERDIDO: "{self.name}" ya no pertenece a '
                                  f'{self.holder_id}. El proceso debe abortar.')
                        return
                except MySQLError as exc:
                    # Un fallo puntual de red no invalida el lease todavia;
                    # si persiste, expirara solo y check() lo detectara.
                    log.warning(f'Heartbeat del lease fallo: {exc}')

        self._thread = threading.Thread(target=beat, name='lease-heartbeat', daemon=True)
        self._thread.start()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# =======================================================================
# 2. MAQUINA DE ESTADOS POR ORDEN
# =======================================================================

CLAIM_DDL = """
CREATE TABLE IF NOT EXISTS billing_order_claim (
    odoo_order_id    INT          NOT NULL PRIMARY KEY,
    order_name       VARCHAR(64)  NOT NULL,
    idempotency_key  VARCHAR(160) NOT NULL,
    state            ENUM('CLAIMED','CREATED','POSTED','STAMPED','DONE',
                          'FAILED','QUARANTINE','NO_STOCK') NOT NULL,
    invoice_id       INT          NULL,
    invoice_name     VARCHAR(64)  NULL,
    holder_id        VARCHAR(160) NULL,
    lease_expires_at TIMESTAMP    NULL,
    attempts         INT          NOT NULL DEFAULT 0,
    last_error       TEXT         NULL,
    created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_idempotency (idempotency_key),
    KEY idx_state (state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Estados terminales: la orden no se vuelve a tocar.
TERMINAL_STATES = {'DONE', 'QUARANTINE'}
# Estados intermedios: hay trabajo a medias que hay que REANUDAR, no repetir.
RESUMABLE_STATES = {'CREATED', 'POSTED', 'STAMPED'}


class OrderClaim:
    """Reserva exclusiva y reanudable de una orden de venta.

    Reemplaza a insert_audit_record() como mecanismo de control. La
    auditoria sigue existiendo para reporteo; esto es para correctitud.

    Diferencia clave con reset_stuck_processing_records(): aqui NO se
    borran las reservas de otros procesos. Solo se recuperan las que
    tienen el lease vencido.
    """

    def __init__(self, db_config, holder_id, claim_ttl=1800):
        self.db_config = db_config
        self.holder_id = holder_id
        self.claim_ttl = claim_ttl
        self.ensure_schema()

    def _conn(self):
        return mysql.connector.connect(**self.db_config)

    def ensure_schema(self):
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(CLAIM_DDL)
        conn.commit()
        cur.close()
        conn.close()

    def claim(self, order_id, order_name):
        """Intenta reservar la orden.

        Devuelve (adquirida: bool, estado_previo: dict|None).
          - (True,  None)  -> orden nueva, procesar desde cero
          - (True,  {...}) -> reanudar: ya hay invoice_id creado/posteado
          - (False, {...}) -> no tocar (terminal, o reservada y viva)
        """
        key = make_idempotency_key(order_name)
        conn = self._conn()
        conn.autocommit = False
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute('SELECT * FROM billing_order_claim '
                        ' WHERE odoo_order_id = %s FOR UPDATE', (order_id,))
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    'INSERT INTO billing_order_claim '
                    '  (odoo_order_id, order_name, idempotency_key, state, '
                    '   holder_id, lease_expires_at, attempts) '
                    'VALUES (%s, %s, %s, %s, %s, NOW() + INTERVAL %s SECOND, 1)',
                    (order_id, order_name, key, 'CLAIMED', self.holder_id, self.claim_ttl))
                conn.commit()
                return True, None

            if row['state'] in TERMINAL_STATES:
                conn.commit()
                return False, row

            # Reserva viva de OTRO proceso -> respetarla.
            expires = row.get('lease_expires_at')
            still_alive = expires is not None and expires.timestamp() > time.time()
            if still_alive and row['holder_id'] != self.holder_id:
                conn.commit()
                return False, row

            # Reserva propia o vencida -> la tomamos y reanudamos.
            cur.execute(
                'UPDATE billing_order_claim '
                '   SET holder_id = %s, '
                '       lease_expires_at = NOW() + INTERVAL %s SECOND, '
                '       attempts = attempts + 1 '
                ' WHERE odoo_order_id = %s',
                (self.holder_id, self.claim_ttl, order_id))
            conn.commit()
            return True, row
        except MySQLError:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def advance(self, order_id, state, **fields):
        sets = ['state = %s']
        vals = [state]
        for col, val in fields.items():
            sets.append(f'{col} = %s')
            vals.append(val)
        vals.append(order_id)
        conn = self._conn()
        cur = conn.cursor()
        try:
            cur.execute(
                f"UPDATE billing_order_claim SET {', '.join(sets)} "
                f" WHERE odoo_order_id = %s", vals)
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def quarantine(self, order_id, reason):
        """Estado terminal para duplicados detectados u ordenes corruptas.

        Sin esto, una orden con qty_invoiced > qty_delivered se queda en
        'to invoice' para siempre y se reprocesa en cada corrida.
        """
        self.advance(order_id, 'QUARANTINE', last_error=reason[:2000])
        log.error(f'CUARENTENA orden {order_id}: {reason}')


# =======================================================================
# 3. PROXY RPC SEGURO
# =======================================================================

class UncertainWrite(Exception):
    """Escritura fallida por red: Odoo pudo haberla aplicado. No reintentar."""


class SafeOdooProxy:
    """Reintenta lecturas. Nunca reintenta escrituras a ciegas.

    El problema que resuelve: un 502 del reverse proxy o un timeout de
    socket NO informan si Odoo hizo commit. Odoo ejecuta el create en su
    propia transaccion y la confirma aunque el cliente ya no escuche. El
    reintento ciego produce una segunda factura identica.
    """

    IDEMPOTENT_METHODS = frozenset({
        'search', 'search_read', 'read', 'search_count', 'read_group',
        'fields_get', 'default_get', 'name_search', 'name_get',
    })

    NETWORK_ERRORS = (xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError,
                      socket.timeout, TimeoutError, OSError, ConnectionError)

    def __init__(self, url, db, user, pwd, timeout=300, max_retries=3):
        self.url, self.db, self.user, self.pwd = url, db, user, pwd
        self.timeout, self.max_retries = timeout, max_retries
        self.network_error_count = 0
        self.uncertain_write_count = 0
        self._connect()

    def _transport(self):
        class _T(xmlrpc.client.SafeTransport):
            def make_connection(_self, host):
                conn = super().make_connection(host)
                conn.timeout = self.timeout
                return conn
        return _T()

    def _connect(self):
        common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common',
                                           transport=self._transport())
        self.uid = common.authenticate(self.db, self.user, self.pwd, {})
        self.models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object',
                                                transport=self._transport())

    def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
        """Firma compatible con el proxy actual, para no tocar los llamados."""
        return self.call(model, method, args, kwargs)

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if method not in self.IDEMPOTENT_METHODS:
            return self._call_once_strict(model, method, args, kwargs)

        last = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.models.execute_kw(self.db, self.uid, self.pwd,
                                              model, method, args, kwargs)
            except xmlrpc.client.Fault:
                raise
            except self.NETWORK_ERRORS as exc:
                last = exc
                self.network_error_count += 1
                log.warning(f'Red en lectura {model}.{method} '
                            f'({attempt}/{self.max_retries}): {exc}')
                if attempt < self.max_retries:
                    time.sleep(2 * attempt)
                    self._connect()
        raise last

    def _call_once_strict(self, model, method, args, kwargs):
        try:
            return self.models.execute_kw(self.db, self.uid, self.pwd,
                                          model, method, args, kwargs)
        except xmlrpc.client.Fault:
            # Error de negocio: determinista, la transaccion hizo rollback.
            raise
        except self.NETWORK_ERRORS as exc:
            self.uncertain_write_count += 1
            self.network_error_count += 1
            raise UncertainWrite(
                f'{model}.{method} fallo por red ({exc}). Estado en Odoo '
                f'INDETERMINADO: reconciliar por clave de idempotencia.') from exc


# =======================================================================
# 4. DETECCION DE DUPLICADOS (el bug del dict)
# =======================================================================

def find_invoices_by_origin(proxy, db, uid, pwd, order_names, extra_fields=None):
    """Devuelve {invoice_origin: [facturas...]} -- LISTA, no un solo valor.

    El codigo actual hace:
        {inv['invoice_origin']: inv for inv in data}
    Un dict no puede guardar dos valores con la misma llave: si una orden
    tiene dos facturas, una desaparece en silencio. Como account.move._order
    termina en 'id desc', sobrevive la mas ANTIGUA -- por eso el log siempre
    reportaba la primera factura y nunca la segunda.
    """
    fields = ['id', 'invoice_origin', 'name', 'state', 'ref',
              'l10n_mx_edi_cfdi_uuid', 'amount_total']
    if extra_fields:
        fields.extend(f for f in extra_fields if f not in fields)

    result = {}
    names = list(order_names)
    for i in range(0, len(names), 500):
        chunk = names[i:i + 500]
        data = proxy.call('account.move', 'search_read',
                          [[('invoice_origin', 'in', chunk),
                            ('move_type', '=', 'out_invoice'),
                            ('state', '!=', 'cancel')]],
                          {'fields': fields})
        for inv in data:
            if inv.get('invoice_origin'):
                result.setdefault(inv['invoice_origin'], []).append(inv)
    return result


def reconcile_by_idempotency_key(proxy, order_name):
    """Tras un UncertainWrite en el create: preguntarle a Odoo si existe.

    Devuelve la factura ya creada, o None. Esto sustituye al reintento
    ciego y a la re-validacion previa al create.
    """
    key = make_idempotency_key(order_name)
    found = proxy.call('account.move', 'search_read',
                       [[('ref', '=', key),
                         ('move_type', '=', 'out_invoice'),
                         ('state', '!=', 'cancel')]],
                       {'fields': ['id', 'name', 'state', 'l10n_mx_edi_cfdi_uuid'],
                        'order': 'id asc'})
    if not found:
        # Respaldo por si la factura se creo antes de adoptar la convencion ref.
        found = proxy.call('account.move', 'search_read',
                           [[('invoice_origin', '=', order_name),
                             ('move_type', '=', 'out_invoice'),
                             ('state', '!=', 'cancel')]],
                           {'fields': ['id', 'name', 'state', 'l10n_mx_edi_cfdi_uuid'],
                            'order': 'id asc'})
    if len(found) > 1:
        log.error(f'DUPLICADO DETECTADO en reconciliacion de {order_name}: '
                  f'{[f.get("name") for f in found]}')
    return found[0] if found else None


# =======================================================================
# ALERTA
# =======================================================================

def notify_slack(webhook_url, text):
    """Alerta desde el script. El webhook del YAML solo dispara si el
    pipeline falla, y las corridas que duplicaron 549 facturas terminaron
    con exit code 0."""
    if not webhook_url:
        return
    import json
    import urllib.request
    try:
        req = urllib.request.Request(
            webhook_url, data=json.dumps({'text': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        log.warning(f'No se pudo enviar alerta a Slack: {exc}')
