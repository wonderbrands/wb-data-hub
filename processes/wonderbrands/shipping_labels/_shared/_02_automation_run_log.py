"""
Módulo compartido para registrar cada corrida de las automatizaciones de guías
en la tabla `tools.automation_run_log`.

Para qué sirve: saber cuándo corrió cada flujo, cómo terminó y cuántos pedidos
vio / resolvió / dejó frenados, sin tener que entrar a Kestra. La tabla la lee
un monitor de automatizaciones (fuera de este repo).

Reglas:
  - Al arrancar se inserta la corrida con status='RUNNING'.
  - Al terminar se actualiza con finished_at, status y los conteos.
  - Status: RUNNING | SUCCESS | PARTIAL | FAILED. PARTIAL = terminó, pero hubo
    tiendas fallidas o pedidos sin registrar (`finish` convierte SUCCESS en
    PARTIAL solo si ve `orders_unregistered` > 0 o `failed_shops` no vacío).
  - Horas en UTC (UTC_TIMESTAMP() del servidor de BD).
  - NUNCA tumba la automatización: cualquier error al escribir el log (tabla
    inexistente, sin conexión, etc.) se atrapa y solo se emite un warning.
  - Este módulo NO crea la tabla.

Uso típico (context manager, recomendado):

    from _02_automation_run_log import track_run, add, count

    with track_run('coppel_flex_labels') as run:
        procesar_ordenes_coppel(run)       # el script va llenando `run`

    # dentro del script:
    add(run, 'orders_seen', len(orders))   # conteos de columnas
    add(run, 'orders_ok')
    count(run, 'mirakl_update_failed')     # conteos extra -> summary_json
    run['failed_shops'] = ['Neon']         # tiendas que no se pudieron procesar
    run['status'] = 'PARTIAL'              # opcional: forzar un status

  Si el bloque termina normal -> SUCCESS (o PARTIAL, ver arriba).
  Si lanza una excepción (o sys.exit con código != 0) -> FAILED con el error,
  y la excepción se vuelve a lanzar tal cual (el flujo falla como siempre).

Uso directo (sin context manager):

    run_id = start('amz_selfship_labels')
    ...
    finish(run_id, 'SUCCESS', orders_seen=10, orders_ok=8, orders_blocked=2)

Flujos de varios pasos (p. ej. Mercado Libre: un task de Kestra por paso):
  el primer paso usa `track_run(..., final=False)`: abre la corrida, guarda sus
  conteos y la deja en RUNNING. El último paso usa `track_run(..., resume=True)`:
  retoma la corrida RUNNING más reciente de esa misma automatización (abierta en
  los últimos 90 minutos) y la cierra. Si no encuentra ninguna, abre una nueva.
  Así no hace falta pasar nada entre tasks ni tocar el YAML de los flujos. Los
  conteos que un paso no informa (None) conservan lo que dejó el paso anterior,
  y summary_json se combina (JSON_MERGE_PATCH).

Variables de entorno:
    DB_HOST, DB_USER, DB_PASSWORD, DB_NAME  las mismas que ya usan los scripts.
                         El módulo abre y cierra su propia conexión corta en
                         cada escritura (no depende de la conexión del script).

DDL de referencia (la tabla se crea fuera de este repo, antes de usar esto):

    CREATE TABLE IF NOT EXISTS tools.automation_run_log (
      run_id              CHAR(36)     NOT NULL PRIMARY KEY,
      automation          VARCHAR(64)  NOT NULL,
      runner              VARCHAR(16)  NOT NULL,
      execution_ref       VARCHAR(128) NULL,
      started_at          DATETIME     NOT NULL,
      finished_at         DATETIME     NULL,
      status              VARCHAR(16)  NOT NULL,
      orders_seen         INT NULL,
      orders_ok           INT NULL,
      orders_blocked      INT NULL,
      orders_unregistered INT NULL,
      failed_shops        JSON NULL,
      error_message       TEXT NULL,
      summary_json        JSON NULL,
      KEY idx_automation_started (automation, started_at)
    );
"""

import json
import logging
import os
import uuid
from contextlib import contextmanager

logger = logging.getLogger()

TABLE = 'tools.automation_run_log'

# Namespace fijo para derivar el run_id desde (automation, execution_ref).
_RUN_ID_NAMESPACE = uuid.UUID('6f1c1d52-3b8e-4c43-9a51-2a7d0f6e8b10')

_MAX_ERROR_LEN = 4000


def _get_connection():
    """Conexión corta y propia (mismas variables de entorno que los scripts)."""
    import mysql.connector  # import local: si falta el paquete, solo se omite el log

    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        connection_timeout=10,
    )


def _execute(sql, params, action):
    """Ejecuta una escritura en la tabla de corridas. Nunca lanza excepción."""
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        logger.warning(f"[{TABLE}] No se pudo registrar la corrida ({action}): {e}. "
                       f"La automatización sigue normal.")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def find_open_run(automation, max_age_minutes=90):
    """
    Devuelve el run_id de la corrida RUNNING más reciente de `automation`
    abierta en los últimos `max_age_minutes`, o None. Nunca lanza excepción.
    """
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT run_id FROM {TABLE}
            WHERE automation = %s
              AND status = 'RUNNING'
              AND finished_at IS NULL
              AND started_at >= UTC_TIMESTAMP() - INTERVAL %s MINUTE
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (automation, int(max_age_minutes)),
        )
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"[{TABLE}] No se pudo buscar la corrida abierta de {automation}: {e}. "
                       f"Se abre una nueva.")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def make_run_id(automation, execution_ref=None):
    """
    Con execution_ref (id de ejecución de Kestra) el run_id es determinístico:
    todos los pasos y reintentos de una misma ejecución escriben la misma fila.
    Sin execution_ref (corrida local) es un uuid4 aleatorio.
    """
    if execution_ref:
        return str(uuid.uuid5(_RUN_ID_NAMESPACE, f"{automation}|{execution_ref}"))
    return str(uuid.uuid4())


def start(automation, runner='kestra', execution_ref=None):
    """
    Registra el inicio de una corrida (status='RUNNING') y devuelve su run_id.

    Si la fila ya existe (otro paso o un reintento de la misma ejecución de
    Kestra) se vuelve a poner en RUNNING sin tocar started_at.
    Siempre devuelve un run_id, aunque el INSERT falle.
    """
    run_id = make_run_id(automation, execution_ref)

    sql = f"""
        INSERT INTO {TABLE}
            (run_id, automation, runner, execution_ref, started_at, status)
        VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), 'RUNNING')
        ON DUPLICATE KEY UPDATE
            status = 'RUNNING',
            finished_at = NULL,
            error_message = NULL
    """
    if _execute(sql, (run_id, automation, runner, execution_ref), 'start'):
        logger.info(f"[{TABLE}] Corrida iniciada: automation={automation} run_id={run_id} "
                    f"execution_ref={execution_ref}")
    return run_id


def _to_json(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _write_counts(run_id, status, final, orders_seen, orders_ok, orders_blocked,
                  orders_unregistered, failed_shops, error_message, summary):
    """UPDATE compartido por `finish` (final=True) y `update` (final=False)."""
    if not run_id:
        return False

    if error_message is not None:
        error_message = str(error_message)[:_MAX_ERROR_LEN]

    params = {
        'run_id': run_id,
        'status': status,
        'orders_seen': orders_seen,
        'orders_ok': orders_ok,
        'orders_blocked': orders_blocked,
        'orders_unregistered': orders_unregistered,
        'failed_shops': _to_json(failed_shops),
        'error_message': error_message,
        'summary': _to_json(summary),
    }

    # Los conteos en None conservan el valor que ya tenía la fila (útil cuando
    # varios pasos de un mismo flujo escriben la misma corrida).
    # La expresión de status va primero y solo lee parámetros + valores
    # previos de la fila, así no depende del orden de evaluación del UPDATE.
    status_sql = """
            status = CASE
                WHEN %(status)s = 'SUCCESS' AND (
                    COALESCE(%(orders_unregistered)s, orders_unregistered, 0) > 0
                    OR JSON_LENGTH(COALESCE(%(failed_shops)s, failed_shops, '[]')) > 0
                ) THEN 'PARTIAL'
                ELSE %(status)s
            END,
            finished_at = UTC_TIMESTAMP(),""" if final else ""

    sql = f"""
        UPDATE {TABLE}
        SET {status_sql}
            orders_seen = COALESCE(%(orders_seen)s, orders_seen),
            orders_ok = COALESCE(%(orders_ok)s, orders_ok),
            orders_blocked = COALESCE(%(orders_blocked)s, orders_blocked),
            orders_unregistered = COALESCE(%(orders_unregistered)s, orders_unregistered),
            failed_shops = COALESCE(%(failed_shops)s, failed_shops),
            error_message = COALESCE(%(error_message)s, error_message),
            summary_json = IF(%(summary)s IS NULL, summary_json,
                              JSON_MERGE_PATCH(COALESCE(summary_json, JSON_OBJECT()), %(summary)s))
        WHERE run_id = %(run_id)s
    """
    return _execute(sql, params, 'finish' if final else 'update')


def finish(run_id, status, orders_seen=None, orders_ok=None, orders_blocked=None,
           orders_unregistered=None, failed_shops=None, error_message=None, summary=None):
    """
    Cierra la corrida: finished_at = UTC_TIMESTAMP(), status y conteos.

    status        : 'SUCCESS' | 'PARTIAL' | 'FAILED'. Un SUCCESS con pedidos sin
                    registrar o tiendas fallidas se guarda como PARTIAL.
    failed_shops  : lista de nombres de tienda/cuenta (se guarda como JSON).
    summary       : dict con conteos propios de la automatización (JSON).
    Nunca lanza excepción.
    """
    ok = _write_counts(run_id, status, True, orders_seen, orders_ok, orders_blocked,
                       orders_unregistered, failed_shops, error_message, summary)
    if ok:
        logger.info(f"[{TABLE}] Corrida cerrada: run_id={run_id} status={status} "
                    f"vistos={orders_seen} ok={orders_ok} frenados={orders_blocked} "
                    f"sin_registrar={orders_unregistered}")
    return ok


def update(run_id, orders_seen=None, orders_ok=None, orders_blocked=None,
           orders_unregistered=None, failed_shops=None, error_message=None, summary=None):
    """
    Guarda conteos parciales SIN cerrar la corrida (sigue en RUNNING).
    Lo usa el primer paso de un flujo de varios pasos. Nunca lanza excepción.
    """
    return _write_counts(run_id, None, False, orders_seen, orders_ok, orders_blocked,
                         orders_unregistered, failed_shops, error_message, summary)


# =============================================================================
# CONTEXT MANAGER
# =============================================================================
def new_run():
    """Contadores de una corrida. El script los llena con `add` y `count`."""
    return {
        'run_id': None,
        'status': None,              # None -> SUCCESS al terminar (o PARTIAL)
        'orders_seen': None,
        'orders_ok': None,
        'orders_blocked': None,
        'orders_unregistered': None,
        'failed_shops': None,        # lista; solo la usan flujos con tiendas
        'error_message': None,
        'summary': {},
    }


def add(run, key, n=1):
    """Suma n a una columna de conteo (orders_seen, orders_ok, ...)."""
    if run is None:
        return
    run[key] = (run.get(key) or 0) + n


def count(run, name, n=1):
    """Suma n a un conteo extra que va en summary_json."""
    if run is None:
        return
    summary = run.setdefault('summary', {})
    summary[name] = (summary.get(name) or 0) + n


def _close_from_run(run, status, error_message=None, final=True):
    kwargs = dict(
        orders_seen=run.get('orders_seen'),
        orders_ok=run.get('orders_ok'),
        orders_blocked=run.get('orders_blocked'),
        orders_unregistered=run.get('orders_unregistered'),
        # Lista vacía = se revisaron las tiendas y ninguna falló; None = no aplica.
        failed_shops=run.get('failed_shops'),
        error_message=error_message or run.get('error_message'),
        summary=run.get('summary') or None,
    )
    if final:
        finish(run['run_id'], status, **kwargs)
    else:
        update(run['run_id'], **kwargs)


@contextmanager
def track_run(automation, runner='kestra', execution_ref=None, final=True, resume=False):
    """
    Registra la corrida alrededor de un bloque de código.

    final=False: al terminar bien NO cierra la corrida (queda RUNNING para que
    el siguiente paso del flujo la cierre); si falla, la cierra como FAILED.
    resume=True: retoma la corrida RUNNING que dejó el paso anterior del mismo
    flujo (ver find_open_run); si no hay, abre una nueva.
    """
    run = new_run()
    try:
        open_run_id = find_open_run(automation) if resume else None
        if open_run_id:
            run['run_id'] = open_run_id
            logger.info(f"[{TABLE}] Se retoma la corrida abierta: automation={automation} "
                        f"run_id={open_run_id}")
        else:
            run['run_id'] = start(automation, runner=runner, execution_ref=execution_ref)
    except Exception as e:  # defensa extra: start ya atrapa todo
        logger.warning(f"[{TABLE}] No se pudo iniciar el registro de la corrida: {e}")

    try:
        yield run
    except SystemExit as e:
        if e.code in (None, 0):
            _safe_close(run, run.get('status') or 'SUCCESS', final=final)
        else:
            _safe_close(run, 'FAILED', error_message=run.get('error_message') or f"sys.exit({e.code})")
        raise
    except BaseException as e:
        _safe_close(run, 'FAILED', error_message=f"{type(e).__name__}: {e}")
        raise
    else:
        status = run.get('status') or 'SUCCESS'
        # Un status explícito FAILED/PARTIAL siempre cierra la corrida.
        _safe_close(run, status, final=final or status == 'FAILED')


def _safe_close(run, status, error_message=None, final=True):
    try:
        _close_from_run(run, status, error_message=error_message, final=final)
    except Exception as e:
        logger.warning(f"[{TABLE}] No se pudo cerrar el registro de la corrida: {e}")
