"""Exercise existing persistence bundles in fresh temp workspaces, without LLMs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


def worker(bundle, operation, payload=None):
    assert operation != 'analyze', 'These tests never request model analysis'
    env = {**os.environ, 'COZE_WORKSPACE_PATH': str(ROOT)}
    for key in ('COZE_WORKLOAD_IDENTITY_API_KEY', 'OPENAI_API_KEY', 'PGDATABASE_URL'):
        env.pop(key, None)
    p = subprocess.run([sys.executable, str(ROOT / 'src/visitor_worker.py')],
                       input=json.dumps({'operation':operation, 'bundle':bundle, 'payload':payload or {}}),
                       text=True, capture_output=True, timeout=30, env=env)
    if p.returncode:
        raise AssertionError(p.stderr[-1200:])
    return json.loads(p.stdout)


class WorkerTest(unittest.TestCase):
    def test_rules_references_and_isolation_survive_separate_processes(self):
        before = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'assets').rglob('*') if p.is_file()}
        health = worker({}, 'health')
        self.assertIsNone(health['error'])
        self.assertEqual(health['result']['rules_version'], 'v1.4')
        self.assertFalse(health['result']['model_called'])
        a = worker({}, 'start', {'mode':'消费者捕手', 'scope':'仅模拟', 'simulation':True})
        self.assertIsNone(a['error'])
        self.assertEqual(a['result']['task_id'], 'T-001')
        a = worker(a['bundle'], 'input', {'raw_text':'技术保存自测，不是消费者证据。', 'material_kind':'simulation', 'simulation':True})
        self.assertIsNone(a['error'])
        reloaded = worker(a['bundle'], 'state')
        self.assertEqual(reloaded['result']['inputs'], a['result']['inputs'])
        b = worker({}, 'start', {'mode':'热点捕手'})
        self.assertEqual(b['result']['inputs'], [])
        self.assertTrue(worker(b['bundle'], 'activate', {'task_id':'T-016'})['error'])
        a2 = worker(a['bundle'], 'start', {'mode':'企业灵感捕手'})
        self.assertEqual(a2['result']['task_id'], 'T-002')
        self.assertEqual(a2['result']['inputs'], [])
        self.assertEqual(a2['bundle']['T-001/inputs.json'], a['bundle']['T-001/inputs.json'])
        switched = worker(a2['bundle'], 'switch_mode', {'mode':'市场缺口捕手'})
        self.assertEqual(switched['result']['task_id'], 'T-002')
        self.assertEqual(switched['result']['active_task']['mode'], '市场缺口捕手')
        self.assertEqual(len(switched['result']['materials']['sources']), 9)
        self.assertEqual(before, {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'assets').rglob('*') if p.is_file()})
        self.assertFalse((ROOT / 'assets/opportunity').exists())

    def test_bundle_rejects_path_traversal_and_arbitrary_files(self):
        from visitor_worker import checked_bundle
        for name in ('../secrets.json', '/tmp/cards.json', 'T-001/config.json'):
            with self.assertRaises(ValueError):
                checked_bundle({name: {}})

    def test_agent_graph_can_be_built_without_inference_or_database(self):
        from langgraph.checkpoint.memory import MemorySaver
        # Building the actual graph is not model inference. Block connections anyway.
        with patch.dict(os.environ, {'COZE_WORKSPACE_PATH':str(ROOT), 'COZE_WORKLOAD_IDENTITY_API_KEY':'test-only-not-live', 'COZE_INTEGRATION_MODEL_BASE_URL':'http://127.0.0.1:9/v1'}):
            with patch('socket.socket.connect', side_effect=AssertionError('Network is forbidden during graph construction')):
                with patch('storage.memory.memory_saver.get_memory_saver', return_value=MemorySaver()):
                    from agents.agent import build_agent
                    graph = build_agent()
                    self.assertIn('model', graph.get_graph().nodes)
                    from tools.material_tools import ALL_TOOLS
                    self.assertEqual(len(ALL_TOOLS), 14)


if __name__ == '__main__':
    unittest.main()
