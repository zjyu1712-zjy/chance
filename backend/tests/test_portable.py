"""No model or external service is used in these tests."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


class PortableEntryTest(unittest.TestCase):
    def test_visitor_routes_require_server_token_and_admin_routes_are_absent(self):
        self.assertTrue((ROOT / 'portable/app.py').exists(), 'Missing standalone authenticated entry point')
        from fastapi.testclient import TestClient
        from portable.app import create_app
        # Disable visitor routes: auth must still reject before touching storage.
        with patch.dict(os.environ, {'MENGNIU_AGENT_API_TOKEN': 'test-only-token-not-live', 'VISITOR_DEPLOYMENT_ENABLED': '0', 'MENGNIU_ALLOW_MODEL_CALLS':'0'}):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get('/healthz').json()['model_called'], False)
                self.assertEqual(client.get('/visitor/v1/health').status_code, 401)
                self.assertEqual(client.get('/visitor/v1/health', headers={'Authorization':'Bearer wrong'}).status_code, 401)
                self.assertEqual(client.get('/visitor/v1/health', headers={'Authorization':'Bearer test-only-token-not-live'}).status_code, 503)
                self.assertEqual(client.post('/visitor/v1/dispatch', json={'operation':'analyze'}, headers={'Authorization':'Bearer test-only-token-not-live'}).status_code, 403)
                for path in ['/preview/api/state', '/run', '/stream_run', '/docs']:
                    self.assertEqual(client.get(path).status_code, 404)

    def test_missing_token_refuses_to_start(self):
        self.assertTrue((ROOT / 'portable/app.py').exists(), 'Missing standalone authenticated entry point')
        from fastapi.testclient import TestClient
        from portable.app import create_app
        with patch.dict(os.environ, {'MENGNIU_AGENT_API_TOKEN': '', 'VISITOR_DEPLOYMENT_ENABLED': '0'}):
            with self.assertRaisesRegex(RuntimeError, 'MENGNIU_AGENT_API_TOKEN'):
                with TestClient(create_app()):
                    pass


if __name__ == '__main__':
    unittest.main()
