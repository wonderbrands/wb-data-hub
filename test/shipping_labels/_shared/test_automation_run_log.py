"""
Pruebas locales (sin base real, sin Kestra) del registro de corridas en
tools.automation_run_log (_shared/_02_automation_run_log.py) y de los conteos
que suma cada script de guías.

Correr desde la raíz del repo:  python test/shipping_labels/_shared/test_automation_run_log.py -v
Todo lo externo (mysql.connector, MySQLdb, Odoo, TikTok, Mirakl) es falso.
"""
import importlib
import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
LABELS = os.path.join(REPO, 'processes', 'wonderbrands', 'shipping_labels')
SHARED = os.path.join(LABELS, '_shared')
for p in (SHARED, os.path.join(LABELS, 'mercadolibre'), os.path.join(LABELS, 'tiktok'),
          os.path.join(LABELS, 'coppel')):
    if p not in sys.path:
        sys.path.insert(0, p)

# Ninguna variable real de BD: si algo intentara conectarse de verdad, fallaría.
for k in ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'KESTRA_EXECUTION_ID'):
    os.environ.pop(k, None)


# =============================================================================
# mysql.connector falso
# =============================================================================
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if self.conn.fail_execute:
            raise Exception("1146 (42S02): Table 'tools.automation_run_log' doesn't exist")
        self.conn.executed.append((' '.join(sql.split()), params))

    def fetchone(self):
        return (FAKE.open_run,) if FAKE.open_run else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, fail_execute=False):
        self.fail_execute = fail_execute
        self.executed = []
        self.commits = 0
        self.closed = False

    def cursor(self, *a, **k):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class FakeConnector:
    """Hace de módulo mysql.connector; guarda todas las conexiones abiertas."""
    def __init__(self):
        self.conns = []
        self.fail_connect = False
        self.fail_execute = False
        self.last_kwargs = None
        self.open_run = None

    def connect(self, **kwargs):
        self.last_kwargs = kwargs
        if self.fail_connect:
            raise Exception("2003: Can't connect to MySQL server")
        c = FakeConn(fail_execute=self.fail_execute)
        self.conns.append(c)
        return c

    def statements(self):
        return [s for c in self.conns for s in c.executed]


FAKE = FakeConnector()
mysql_pkg = types.ModuleType('mysql')
mysql_pkg.connector = FAKE
sys.modules['mysql'] = mysql_pkg
sys.modules['mysql.connector'] = FAKE
sys.modules.setdefault('PyPDF2', types.ModuleType('PyPDF2'))

import MySQLdb.cursors  # noqa: E402,F401  (en producción lo carga connect())
import _02_automation_run_log as rl  # noqa: E402


def reset_fake():
    FAKE.conns.clear()
    FAKE.fail_connect = False
    FAKE.fail_execute = False
    FAKE.open_run = None


# =============================================================================
# 1. Módulo compartido
# =============================================================================
class TestHelper(unittest.TestCase):
    def setUp(self):
        reset_fake()

    def test_start_inserta_running_en_utc(self):
        run_id = rl.start('coppel_flex_labels', execution_ref='exec123')
        sql, params = FAKE.statements()[0]
        self.assertIn('INSERT INTO tools.automation_run_log', sql)
        self.assertIn("UTC_TIMESTAMP(), 'RUNNING'", sql)
        self.assertIn('ON DUPLICATE KEY UPDATE', sql)
        self.assertEqual(params, (run_id, 'coppel_flex_labels', 'kestra', 'exec123'))
        self.assertEqual(len(run_id), 36)
        self.assertEqual(FAKE.conns[0].commits, 1)
        self.assertTrue(FAKE.conns[0].closed)
        self.assertEqual(FAKE.last_kwargs.get('connection_timeout'), 10)

    def test_run_id_aleatorio_sin_execution_ref(self):
        self.assertNotEqual(rl.make_run_id('ml_shipping_labels'), rl.make_run_id('ml_shipping_labels'))

    def test_find_open_run(self):
        FAKE.open_run = 'abc-123'
        self.assertEqual(rl.find_open_run('ml_shipping_labels'), 'abc-123')
        sql, params = FAKE.statements()[-1]
        self.assertIn("status = 'RUNNING'", sql)
        self.assertEqual(params, ('ml_shipping_labels', 90))
        FAKE.open_run = None
        self.assertIsNone(rl.find_open_run('ml_shipping_labels'))

    def test_find_open_run_sin_conexion_no_rompe(self):
        FAKE.fail_connect = True
        self.assertIsNone(rl.find_open_run('ml_shipping_labels'))

    def test_finish_update_con_conteos_y_json(self):
        ok = rl.finish('rid', 'SUCCESS', orders_seen=10, orders_ok=7, orders_blocked=3,
                       failed_shops=['Neon'], summary={'x': 1})
        self.assertTrue(ok)
        sql, params = FAKE.statements()[0]
        self.assertIn('UPDATE tools.automation_run_log', sql)
        self.assertIn('finished_at = UTC_TIMESTAMP()', sql)
        self.assertIn("THEN 'PARTIAL'", sql)
        self.assertIn('JSON_MERGE_PATCH', sql)
        self.assertEqual(params['run_id'], 'rid')
        self.assertEqual(params['status'], 'SUCCESS')
        self.assertEqual(params['orders_seen'], 10)
        self.assertEqual(json.loads(params['failed_shops']), ['Neon'])
        self.assertEqual(json.loads(params['summary']), {'x': 1})

    def test_update_no_cierra(self):
        rl.update('rid', orders_unregistered=4, summary={'sla_fallidas': 4})
        sql, params = FAKE.statements()[0]
        self.assertNotIn('finished_at', sql)
        self.assertNotIn('status =', sql)
        self.assertEqual(params['orders_unregistered'], 4)

    def test_tabla_inexistente_no_rompe(self):
        FAKE.fail_execute = True
        with self.assertLogs(level='WARNING') as cm:
            run_id = rl.start('coppel_flex_labels', execution_ref='e')
            ok = rl.finish(run_id, 'SUCCESS', orders_seen=1)
        self.assertTrue(run_id)
        self.assertFalse(ok)
        self.assertTrue(any("doesn't exist" in m for m in cm.output))
        self.assertTrue(all(c.closed for c in FAKE.conns))

    def test_sin_conexion_no_rompe(self):
        FAKE.fail_connect = True
        with self.assertLogs(level='WARNING'):
            with rl.track_run('amz_selfship_labels') as run:
                rl.add(run, 'orders_seen', 3)
        self.assertEqual(run['orders_seen'], 3)

    def test_sin_paquete_mysql_no_rompe(self):
        with mock.patch.dict(sys.modules, {'mysql.connector': None}):
            with self.assertLogs(level='WARNING'):
                self.assertTrue(rl.start('x'))

    def test_track_run_exito(self):
        with rl.track_run('coppel_flex_labels', execution_ref='e1') as run:
            rl.add(run, 'orders_seen', 5)
            rl.add(run, 'orders_ok', 4)
            rl.add(run, 'orders_blocked')
            rl.count(run, 'Sin_cobertura')
        stmts = FAKE.statements()
        self.assertEqual(len(stmts), 2)
        params = stmts[1][1]
        self.assertEqual(params['status'], 'SUCCESS')
        self.assertEqual((params['orders_seen'], params['orders_ok'], params['orders_blocked']), (5, 4, 1))
        self.assertIsNone(params['failed_shops'])
        self.assertEqual(json.loads(params['summary']), {'Sin_cobertura': 1})

    def test_track_run_excepcion_queda_failed_y_se_relanza(self):
        with self.assertRaises(ValueError):
            with rl.track_run('amz_selfship_labels', execution_ref='e2') as run:
                rl.add(run, 'orders_seen', 2)
                raise ValueError('boom')
        params = FAKE.statements()[-1][1]
        self.assertEqual(params['status'], 'FAILED')
        self.assertEqual(params['error_message'], 'ValueError: boom')
        self.assertEqual(params['orders_seen'], 2)

    def test_track_run_sys_exit(self):
        with self.assertRaises(SystemExit):
            with rl.track_run('a', execution_ref='e3'):
                sys.exit(1)
        self.assertEqual(FAKE.statements()[-1][1]['status'], 'FAILED')
        reset_fake()
        with self.assertRaises(SystemExit):
            with rl.track_run('a', execution_ref='e4'):
                sys.exit(0)
        self.assertEqual(FAKE.statements()[-1][1]['status'], 'SUCCESS')

    def test_track_run_no_final_deja_running(self):
        with rl.track_run('ml_shipping_labels', execution_ref='e5', final=False) as run:
            rl.add(run, 'orders_unregistered', 2)
        sql, params = FAKE.statements()[-1]
        self.assertNotIn('finished_at', sql)
        self.assertEqual(params['orders_unregistered'], 2)
        # ...pero si falla, sí la cierra como FAILED
        reset_fake()
        with self.assertRaises(RuntimeError):
            with rl.track_run('ml_shipping_labels', execution_ref='e6', final=False):
                raise RuntimeError('x')
        sql, params = FAKE.statements()[-1]
        self.assertIn('finished_at', sql)
        self.assertEqual(params['status'], 'FAILED')

    def test_status_explicito(self):
        with rl.track_run('coppel_flex_labels', execution_ref='e7') as run:
            run['status'] = 'PARTIAL'
        self.assertEqual(FAKE.statements()[-1][1]['status'], 'PARTIAL')


# =============================================================================
# 2. Mercado Libre
# =============================================================================
class FakeMyDb:
    """MySQLdb falso: las escrituras sin commit se pierden al cerrar."""
    def __init__(self, orders):
        self.orders = orders
        self.pending = []
        self.committed = []

    def cursor(self, *a, **k):
        db = self

        class C:
            def execute(self, sql, params=None):
                s = ' '.join(sql.split())
                if s.startswith('SELECT token'):
                    self._r = [('tok',)]
                elif s.startswith('SELECT id, marketplace_reference'):
                    self._r = db.orders
                else:
                    db.pending.append((s, params))

            def fetchone(self):
                return self._r[0]

            def fetchall(self):
                return self._r
        return C()

    def commit(self):
        self.committed.extend(self.pending)
        self.pending = []

    def close(self):
        self.pending = []  # lo no commiteado se pierde


class FakeOdoo:
    """search_read de sale.order y search_count de PICK configurables por referencia."""
    def __init__(self, so_by_ref, picks_by_so):
        self.so_by_ref = so_by_ref
        self.picks_by_so = picks_by_so

    def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
        if model == 'sale.order' and method == 'search_read':
            ref = args[0][1][2]
            so = self.so_by_ref.get(ref)
            return [{'id': 1, 'name': so, 'data_carrier_selection_relational': False}] if so else []
        if model == 'stock.picking':
            so = args[0][0][2]
            return self.picks_by_so.get(so, 0)
        raise AssertionError(f'llamada inesperada a Odoo: {model}.{method}')


class TestMercadoLibre(unittest.TestCase):
    def setUp(self):
        reset_fake()
        os.environ['ML_SELLER_ID'] = '160190870'
        for m in ('ml_sellers', 'ml_odoo_injection_labels', 'ml_mix_db_api_sla_orders'):
            sys.modules.pop(m, None)

    def tearDown(self):
        os.environ.pop('ML_SELLER_ID', None)

    def test_automation_id_por_seller(self):
        import ml_sellers
        self.assertEqual(ml_sellers.get_seller_config()['automation_id'], 'ml_oficiales_shipping_labels')
        os.environ['ML_SELLER_ID'] = '25523702'
        self.assertEqual(ml_sellers.get_seller_config()['automation_id'], 'ml_shipping_labels')

    def test_sla_fallido_cuenta_como_sin_registrar(self):
        sla = importlib.import_module('ml_mix_db_api_sla_orders')
        raw = [{'order_id': i, 'pack_id': None, 'o_status': 'paid', 'shipping_id': 1000 + i,
                's_status': 'ready_to_ship', 's_substatus': 'ready_to_print', 'l_type': 'cross_docking',
                't_method': None} for i in range(4)]

        class Db:
            def __init__(self):
                self.inserts = 0

            def cursor(self, *a, **k):
                db = self

                class C:
                    def execute(self, sql, params=None):
                        s = ' '.join(sql.split())
                        if 'FROM tools.ml_api_etl_orders' in s:
                            self._r = []
                        elif 'FROM somos_reyes.ml_order_update' in s:
                            self._r = raw
                        elif s.startswith('INSERT INTO tools.ml_api_etl_orders'):
                            db.inserts += 1

                    def fetchall(self):
                        return self._r
                return C()

            def commit(self):
                pass

        class Resp:
            def __init__(self, code):
                self.status_code = code

            def json(self):
                return {'expected_date': '2026-11-23T10:00:00-06:00'}

        class Session:
            def get(self, url, timeout=None):
                sid = int(url.split('/')[-2])
                if sid == 1001:
                    return Resp(500)            # SLA con error HTTP
                if sid == 1002:
                    raise TimeoutError('timeout')  # SLA con excepción
                return Resp(200)

        db = Db()
        run = rl.new_run()
        with mock.patch.object(sla, 'get_override_config',
                               return_value={'override_days': 0, 'lookback_days': 10, 'cutoff_time': 15}), \
                mock.patch.object(sla, 'get_requests_session', return_value=Session()), \
                self.assertLogs(level='WARNING'):
            sla.run_etl_hybrid(db, 'tok', run)
        self.assertEqual(db.inserts, 2)
        self.assertEqual(run['orders_unregistered'], 2)
        self.assertEqual(sorted(run['summary']['sla_fallidas_shipping_ids']), ['1001', '1002'])
        self.assertEqual(run['summary']['sla_fallidas'], 2)
        self.assertEqual(run['summary']['sla_registradas'], 2)

    def test_dos_pasos_escriben_la_misma_fila(self):
        with rl.track_run('ml_oficiales_shipping_labels', final=False) as r1:
            rl.add(r1, 'orders_unregistered', 3)
        FAKE.open_run = r1['run_id']   # lo que devolvería la base: la corrida RUNNING del paso 1
        with rl.track_run('ml_oficiales_shipping_labels', resume=True) as r2:
            rl.add(r2, 'orders_seen', 5)
        self.assertEqual(r1['run_id'], r2['run_id'])
        stmts = FAKE.statements()
        # paso 1: INSERT + UPDATE sin cerrar; paso 2: SELECT (busca la abierta) + UPDATE que cierra
        self.assertEqual([s.split()[0] for s, _ in stmts], ['INSERT', 'UPDATE', 'SELECT', 'UPDATE'])
        self.assertNotIn('finished_at', stmts[1][0])
        self.assertIn('finished_at', stmts[3][0])
        # El paso 2 no pisa orders_unregistered (None -> COALESCE conserva lo del paso 1)
        self.assertIsNone(stmts[3][1]['orders_unregistered'])

    def test_paso_2_sin_corrida_abierta_abre_una_nueva(self):
        with rl.track_run('ml_shipping_labels', resume=True) as r:
            pass
        stmts = FAKE.statements()
        self.assertEqual([s.split()[0] for s, _ in stmts], ['SELECT', 'INSERT', 'UPDATE'])
        self.assertIsNotNone(r['run_id'])


# =============================================================================
# 3. TikTok Bulky
# =============================================================================
class TestTikTok(unittest.TestCase):
    def setUp(self):
        reset_fake()

    def test_tienda_sin_token_va_a_failed_shops(self):
        tk = importlib.import_module('tiktok_bulky_fulfillment')
        tk.RUN_FAILED_SHOPS.clear()
        tk.RUN_MANUAL_REASONS.clear()
        counters = {'found': 0, 'processed': 0, 'manual': 0, 'skipped': 0, 'shops_failed': 0}
        with mock.patch.object(tk, 'get_valid_access_token', return_value=None):
            tk.process_shop({'seller_name': 'Neon'}, None, None, None, None, None, counters)
        self.assertEqual(counters['shops_failed'], 1)
        self.assertEqual(tk.RUN_FAILED_SHOPS, ['Neon'])

        tk.RUN_MANUAL_REASONS['POST_LABEL_UPDATE_FAILED'] = 2
        with rl.track_run('tiktok_bulky_labels', execution_ref='e-tk') as run:
            counters.update(found=7, processed=4, manual=3)
            tk.fill_run_log(run, counters)
        params = FAKE.statements()[-1][1]
        self.assertEqual(json.loads(params['failed_shops']), ['Neon'])
        self.assertEqual(params['status'], 'SUCCESS')  # el SQL la convierte en PARTIAL (failed_shops no vacío)
        summary = json.loads(params['summary'])
        self.assertEqual(summary['post_label_update_failed'], 2)
        self.assertEqual((params['orders_seen'], params['orders_ok'], params['orders_blocked']), (7, 4, 3))

    def test_register_manual_cuenta_motivo(self):
        tk = importlib.import_module('tiktok_bulky_fulfillment')
        tk.RUN_MANUAL_REASONS.clear()
        ctx = {'order_id': '1', 'seller_name': 'Neon', 'items': []}
        with mock.patch.object(tk, 'upsert_sheet_row'), mock.patch.object(tk, 'get_attempt_number', return_value=1), \
                mock.patch.object(tk.VOBO, 'mark'), mock.patch.object(tk.VOBO, 'annotate'):
            tk.register_manual(None, {}, ctx, 'POST_LABEL_UPDATE_FAILED', 'x', write_db=False)
        self.assertEqual(tk.RUN_MANUAL_REASONS, {'POST_LABEL_UPDATE_FAILED': 1})


# =============================================================================
# 4. Coppel: fallas al actualizar Mirakl / Odoo devuelven False
# =============================================================================
class TestCoppel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        os.chdir(os.path.join(LABELS, 'coppel'))
        try:
            cls.cp = importlib.import_module('_02_coppel_labels_flex')
        finally:
            os.chdir(cwd)

    def test_mirakl_st01_con_errores_devuelve_false(self):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {'shipment_errors': [{'e': 1}], 'shipment_success': []}
        order = {'order_id': 'O1', 'order_lines': [{'offer_sku': 'A', 'quantity': 1}]}
        labels = [{'offer_sku': 'A', 'tracking_number': 'T1', 'provider': 'FEDEX'}]
        with mock.patch.object(self.cp.requests, 'post', return_value=R()):
            self.assertFalse(self.cp.post_shipments_to_mirakl({}, order, labels, {}))

    def test_mirakl_st01_ok_devuelve_true(self):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {'shipment_errors': [], 'shipment_success': [{}]}
        order = {'order_id': 'O1', 'order_lines': [{'offer_sku': 'A', 'quantity': 1}]}
        labels = [{'offer_sku': 'A', 'tracking_number': 'T1', 'provider': 'FEDEX'}]
        with mock.patch.object(self.cp.requests, 'post', return_value=R()):
            self.assertTrue(self.cp.post_shipments_to_mirakl({}, order, labels, {}))

    def test_mirakl_or74_excepcion_devuelve_false(self):
        labels = [{'tracking_number': 'T1', 'zpl': '^XA^XZ'}]
        with mock.patch.object(self.cp.requests, 'post', side_effect=Exception('502')):
            self.assertFalse(self.cp.upload_documents_to_mirakl({'Authorization': 'x'}, 'O1', labels))

    def test_odoo_adjunto_falla_devuelve_false(self):
        class M:
            def execute_kw(self, *a, **k):
                raise Exception('Odoo caído')
        labels = [{'tracking_number': 'T1', 'zpl': '^XA^XZ'}]
        self.assertFalse(self.cp.consolidate_and_attach_labels_odoo(M(), 'db', 1, 'pw', 5, labels, 'SO1'))

    def test_odoo_adjunto_ok_devuelve_true(self):
        class M:
            def execute_kw(self, *a, **k):
                return 99
        labels = [{'tracking_number': 'T1', 'zpl': '^XA^XZ'}]
        self.assertTrue(self.cp.consolidate_and_attach_labels_odoo(M(), 'db', 1, 'pw', 5, labels, 'SO1'))


if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
