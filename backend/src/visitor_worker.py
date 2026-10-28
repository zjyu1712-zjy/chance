"""Run the existing application in a disposable visitor workspace.

No original opportunity directory is mounted or copied. This entry point is
invoked only by visitor_api.py with a server-owned, persisted visitor snapshot.
"""
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ALLOWED_FILE = re.compile(r"(?:_active_task\.json|T-\d{3,}/(?:cards|inputs|runlog|exclusions|task_meta|visitor_replies)\.json)\Z")


def checked_bundle(bundle):
    if not isinstance(bundle, dict) or len(bundle) > 120:
        raise ValueError("访客任务快照格式错误或已达任务数量上限")
    if len(json.dumps(bundle, ensure_ascii=False).encode()) > 8_000_000:
        raise ValueError("访客资料总量超过 8MB，请导出后开始新的访客会话")
    for name in bundle:
        if not ALLOWED_FILE.fullmatch(name):
            raise ValueError("非法任务文件名")
    return bundle


async def operate(workspace, command):
    # All imports that resolve WORKSPACE must happen after the environment is set.
    import webapp_preview as ui
    from storage.opportunity_store import get_opportunity_store
    store = get_opportunity_store()
    op = command['operation']
    p = command.get('payload') or {}
    if not isinstance(p, dict):
        raise ValueError('请求内容须为对象')
    if op == 'health':
        rules = Path(workspace, 'assets/rules/05_智能体运行提示词_v1.4.txt')
        return {'rules_version': 'v1.4', 'rules_sha256': hashlib.sha256(rules.read_bytes()).hexdigest(),
                'materials': ui._materials().overview(), 'model_called': False}
    if op == 'state':
        result = ui._overview()
    elif op == 'start':
        if len(ui._task_dirs()) >= 15:
            raise ValueError('本访客已达 15 个任务上限，请先导出已有任务')
        result = await ui.api_start(p)
    elif op == 'input':
        raw = p.get('raw_text', '')
        if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= 50000:
            raise ValueError('每份资料请控制在 1—50000 字符')
        if len(store.list_inputs()) >= 20:
            raise ValueError('本任务最多登记 20 份资料')
        result = await ui.api_input(p)
    elif op == 'switch_mode':
        result = await ui.api_switch_mode(p)
    elif op == 'activate':
        if p.get('task_id') not in ui._task_dirs():
            raise ValueError('该任务不属于当前访客')
        result = await ui.api_activate(p)
    elif op == 'versions':
        return await ui.api_card_versions(p.get('card_id', 'OC-001'))
    elif op == 'export':
        return {'text': (await ui.api_export()).body.decode('utf-8')}
    elif op == 'export_card':
        return {'text': (await ui.api_export_card(p.get('card_id', 'OC-001'))).body.decode('utf-8')}
    elif op == 'exclude':
        result = await ui.api_exclude(p)
    elif op == 'restore':
        result = await ui.api_restore(p)
    elif op == 'model_reply':
        act = store.get_active_task() or {}
        path = Path(workspace, 'assets/opportunity', act.get('task_id', '_none'), 'visitor_replies.json')
        rows = json.loads(path.read_text()) if path.exists() else []
        matches = [r for r in rows if str(r.get('version')) == str(p.get('version')) and r.get('card_id') == p.get('card_id', 'OC-001')]
        return matches[-1] if matches else {'exists': False}
    elif op == 'analyze':
        if not store.get_active_task() or not store.list_inputs():
            raise ValueError('请先开始任务并登记资料')
        # Visitor short-term checkpoints never use the original shared DB schema.
        from langgraph.checkpoint.memory import MemorySaver
        import storage.memory.memory_saver as mm
        mm.get_memory_saver = lambda: MemorySaver()
        from agents.agent import build_agent
        ui.set_graph_provider(lambda: build_agent())
        result = await ui.api_analyze({})
        tid = (store.get_active_task() or {}).get('task_id')
        reply = result.get('last_ai_text')
        if tid and reply:
            path = Path(workspace, 'assets/opportunity', tid, 'visitor_replies.json')
            replies = json.loads(path.read_text()) if path.exists() else []
            card = result.get('latest_card') or {}
            replies.append({'exists': True, 'content': reply, 'task_id': tid,
                            'card_id': card.get('card_id', ''), 'version': card.get('version'),
                            'note': '访客任务真实模型回复；保存失败时仍保留回复，不视为机会卡保存成功。',
                            'acquired_from': '本轮实际模型返回', 'related_log': command.get('run_id', ''),
                            'saved_new': bool((result.get('run') or {}).get('saved_new'))})
            path.write_text(json.dumps(replies, ensure_ascii=False), encoding='utf-8')
    else:
        raise ValueError('不支持的访客操作')
    return result


def main():
    original = Path(os.environ.get('COZE_WORKSPACE_PATH', '/workspace/projects')).resolve()
    command = json.load(sys.stdin)
    initial = checked_bundle(command.get('bundle', {}))
    with tempfile.TemporaryDirectory(prefix='mengniu-visitor-') as temp:
        root = Path(temp)
        # Copies of read-only configuration and business references only.
        for relative in ('config', 'assets/materials', 'assets/rules'):
            source = original / relative
            if not source.is_dir():
                raise ValueError('缺少运行所需的只读资料目录：' + relative)
            shutil.copytree(source, root / relative)
        data_dir = root / 'assets/opportunity'
        data_dir.mkdir(parents=True)
        for name, content in initial.items():
            path = data_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content, ensure_ascii=False), encoding='utf-8')
        os.environ['COZE_WORKSPACE_PATH'] = str(root)
        sys.path.insert(0, str(original / 'src'))
        error = None
        result = None
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = asyncio.run(operate(root, command))
        except Exception as exc:
            error = str(getattr(exc, 'detail', '') or exc)
        snapshot = {str(p.relative_to(data_dir)): json.loads(p.read_text()) for p in data_dir.rglob('*.json') if ALLOWED_FILE.fullmatch(str(p.relative_to(data_dir)))}
        checked_bundle(snapshot)
        json.dump({'result': result, 'bundle': snapshot, 'error': error}, sys.stdout, ensure_ascii=False)


if __name__ == '__main__':
    main()
