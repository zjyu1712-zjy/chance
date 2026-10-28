"""Optional integration test. Requires an explicitly selected disposable local DB."""
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


@unittest.skipUnless(os.getenv('CHANCE_TEST_DATABASE_URL'), 'No disposable PostgreSQL configured')
class PostgresTest(unittest.TestCase):
    def test_authenticated_bridge_persists_and_isolates_visitors(self):
        url = os.environ['CHANCE_TEST_DATABASE_URL']
        self.assertIn(urlparse(url).hostname, ('127.0.0.1', 'localhost', 'db'), 'Only local disposable databases are allowed')
        from fastapi.testclient import TestClient
        from sqlalchemy import text
        from portable.app import create_app
        import visitor_api as bridge
        owners = [hashlib.sha256(('chance-test-' + uuid.uuid4().hex).encode()).hexdigest() for _ in range(2)]
        env = {'PGDATABASE_URL':url, 'COZE_WORKSPACE_PATH':str(ROOT), 'VISITOR_DEPLOYMENT_ENABLED':'1', 'MENGNIU_ALLOW_MODEL_CALLS':'0', 'MENGNIU_AGENT_API_TOKEN':'test-only-not-live'}
        with patch.dict(os.environ, env):
            try:
                with TestClient(create_app()) as client:
                    def post(owner, operation, payload=None):
                        self.assertNotEqual(operation, 'analyze')
                        response = client.post('/visitor/v1/dispatch', headers={'Authorization':'Bearer test-only-not-live', 'X-Mengniu-Visitor':owner}, json={'operation':operation,'payload':payload or {}})
                        self.assertEqual(response.status_code, 200, response.text)
                        return response.json()
                    health = client.get('/visitor/v1/health', headers={'Authorization':'Bearer test-only-not-live'})
                    self.assertEqual(health.status_code, 200)
                    self.assertFalse(health.json()['model_called'])
                    a = post(owners[0], 'start', {'mode':'消费者捕手','simulation':True,'scope':'仅模拟'})
                    self.assertEqual(a['task_id'], 'T-001')
                    a = post(owners[0], 'input', {'raw_text':'数据库技术自测，不是消费者证据。','simulation':True,'material_kind':'simulation'})
                    b = post(owners[1], 'start', {'mode':'热点捕手'})
                    self.assertEqual(b['inputs'], [])
                # A new ASGI application reads the same PostgreSQL state.
                with TestClient(create_app()) as client:
                    restored = client.post('/visitor/v1/dispatch', headers={'Authorization':'Bearer test-only-not-live','X-Mengniu-Visitor':owners[0]}, json={'operation':'state'}).json()
                    self.assertEqual(restored['inputs'], a['inputs'])
                row, token = bridge.lock_session(owners[0], 'input')
                with self.assertRaises(Exception) as busy:
                    bridge.lock_session(owners[0], 'start')
                self.assertEqual(busy.exception.status_code, 409)
                # Existing input immutability must reject an attempt to erase it.
                changed = dict(row['bundle'])
                changed['T-001/inputs.json'] = []
                with self.assertRaises(Exception) as rejected:
                    bridge.finish(owners[0], token, {'bundle':changed,'result':None}, 'input')
                self.assertEqual(rejected.exception.status_code, 409)
                bridge.release(owners[0], token, 'technical test finished')
            finally:
                with bridge.engine().begin() as conn:
                    for owner in owners:
                        conn.execute(text('DELETE FROM mengniu_visitor_sessions_v1 WHERE owner_id=:owner'), {'owner':owner})


if __name__ == '__main__':
    unittest.main()
