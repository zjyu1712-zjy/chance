"""Production-only visitor bridge. Archived tasks are never opened for writing."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(prefix='/visitor/v1', tags=['isolated-visitors'])
READ_OPS = {'state', 'versions', 'export', 'export_card', 'model_reply', 'health'}
WRITE_OPS = {'start', 'input', 'switch_mode', 'activate', 'exclude', 'restore', 'analyze'}
TABLE = 'mengniu_visitor_sessions_v1'


def enabled():
    if os.getenv('VISITOR_DEPLOYMENT_ENABLED') != '1':
        raise HTTPException(503, '访客生产入口尚未启用')


def engine():
    from storage.database.db import get_engine
    return get_engine()


def migrate():
    # Dedicated table; no writes to existing data, memory tables or case files.
    with engine().begin() as conn:
        conn.execute(text('''CREATE TABLE IF NOT EXISTS mengniu_visitor_sessions_v1 (
            owner_id varchar(64) PRIMARY KEY,
            bundle jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_result jsonb,
            revision integer NOT NULL DEFAULT 0,
            busy_token varchar(40), busy_since timestamptz,
            day_key varchar(10), day_runs integer NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now()
        )'''))


def lock_session(owner, operation):
    today = datetime.now(timezone.utc).date().isoformat()
    token = uuid.uuid4().hex
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO mengniu_visitor_sessions_v1(owner_id) VALUES (:owner) ON CONFLICT DO NOTHING"), {'owner': owner})
        row = dict(conn.execute(text('SELECT *, EXTRACT(EPOCH FROM (now()-busy_since)) AS busy_age FROM mengniu_visitor_sessions_v1 WHERE owner_id=:owner FOR UPDATE'), {'owner': owner}).mappings().one())
        if row['busy_token'] and (row['busy_age'] or 0) < 720:
            if operation in READ_OPS:
                return row, None
            raise HTTPException(409, '当前访客任务正在处理，请等待；不会重复提交分析')
        if operation in READ_OPS:
            return row, None
        runs = row['day_runs'] if row['day_key'] == today else 0
        if operation == 'analyze' and runs >= 10:
            raise HTTPException(429, '当前访客今天已运行 10 次分析，请明天继续。已有结果仍可查看和导出。')
        conn.execute(text('UPDATE mengniu_visitor_sessions_v1 SET busy_token=:token,busy_since=now(),day_key=:day,day_runs=:runs WHERE owner_id=:owner'),
                     {'owner': owner, 'token': token, 'day': today, 'runs': runs + (1 if operation == 'analyze' else 0)})
    return row, token


def finish(owner, token, output, operation):
    with engine().begin() as conn:
        row = conn.execute(text('SELECT bundle FROM mengniu_visitor_sessions_v1 WHERE owner_id=:owner AND busy_token=:token FOR UPDATE'), {'owner': owner, 'token': token}).mappings().first()
        if row is None:
            raise HTTPException(409, '保存锁已变化，未覆盖其他请求结果')
        old = row['bundle'] or {}
        new = output['bundle']
        # A model cannot overwrite/delete a prior version, even inside its sandbox.
        for name, contents in old.items():
            if name.endswith('/cards.json'):
                actual = new.get(name, [])
                if not isinstance(actual, list) or actual[:len(contents)] != contents:
                    raise HTTPException(409, '历史版本保护检查失败，已拒绝覆盖原记录')
            if name.endswith('/inputs.json'):
                actual = new.get(name, [])
                if actual[:len(contents)] != contents:
                    raise HTTPException(409, '原输入保护检查失败，已拒绝覆盖原记录')
        params = {'owner': owner, 'token': token, 'bundle': json.dumps(new, ensure_ascii=False),
                  'result': json.dumps(output.get('result'), ensure_ascii=False)}
        result_sql = ',last_result=CAST(:result AS jsonb)' if operation == 'analyze' else ''
        conn.execute(text('UPDATE mengniu_visitor_sessions_v1 SET bundle=CAST(:bundle AS jsonb),revision=revision+1,busy_token=NULL,busy_since=NULL,updated_at=now()'+result_sql+' WHERE owner_id=:owner AND busy_token=:token'), params)


def release(owner, token, message):
    if not token:
        return
    with engine().begin() as conn:
        conn.execute(text("UPDATE mengniu_visitor_sessions_v1 SET busy_token=NULL,busy_since=NULL,last_result=CAST(:r AS jsonb),updated_at=now() WHERE owner_id=:owner AND busy_token=:token"),
                     {'owner': owner, 'token': token, 'r': json.dumps({'analyze_error': message, 'run': {'saved_new': False, 'status': 'error'}}, ensure_ascii=False)})


async def isolated(command, timeout):
    worker = str(Path(__file__).with_name('visitor_worker.py'))
    proc = await asyncio.create_subprocess_exec(sys.executable, worker, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(command, ensure_ascii=False).encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(504, '分析达到本次时间上限，已停止且不会自动重试；原输入与已保存版本保留')
    if proc.returncode:
        # Do not leak runtime credentials or traceback through public responses.
        raise HTTPException(502, '独立运行进程失败；原输入与已保存版本保留，请联系项目管理员')
    try:
        return json.loads(out)
    except Exception:
        raise HTTPException(502, '独立运行返回格式异常，未冒充保存成功')


@router.get('/health')
async def health():
    enabled()
    with engine().connect() as conn:
        conn.execute(text('SELECT 1 FROM mengniu_visitor_sessions_v1 LIMIT 1'))
    out = await isolated({'operation': 'health', 'bundle': {}}, 40)
    if out.get('error'):
        raise HTTPException(503, out['error'])
    return {'ready': True, 'storage': 'isolated PostgreSQL visitor table', **out['result']}


@router.post('/dispatch')
async def dispatch(request: Request):
    enabled()
    owner = request.headers.get('X-Mengniu-Visitor', '')
    if not re.fullmatch(r'[a-f0-9]{64}', owner):
        raise HTTPException(401, '缺少有效访客身份')
    raw = await request.body()
    if len(raw) > 250000:
        raise HTTPException(413, '资料过长，请分批提交')
    try:
        req = json.loads(raw)
    except Exception:
        raise HTTPException(400, '请求格式错误')
    op = req.get('operation')
    if op not in READ_OPS | WRITE_OPS:
        raise HTTPException(400, '不支持的操作')
    row, token = await asyncio.to_thread(lock_session, owner, op)
    try:
        out = await isolated({'operation': op, 'payload': req.get('payload') or {}, 'bundle': row['bundle'] or {}, 'run_id': token}, 600 if op == 'analyze' else 40)
        if token:
            await asyncio.to_thread(finish, owner, token, out, op)
        if out.get('error'):
            raise HTTPException(400, out['error'])
        result = out['result']
        if op == 'state' and isinstance(result, dict):
            last = row.get('last_result') or {}
            if last.get('task_id') == result.get('task_id'):
                for k in ('last_ai_text', 'analyze_error', 'run'):
                    if k in last:
                        result[k] = last[k]
            result['visitor_busy'] = bool(row['busy_token']) and (row['busy_age'] or 0) < 720
            result['visitor_saved_at'] = str(row.get('updated_at') or '')
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except Exception as exc:
        if token:
            await asyncio.to_thread(release, owner, token, str(getattr(exc, 'detail', '') or '访客请求失败'))
        raise


if __name__ == '__main__':
    migrate()
    print('Visitor schema ready; model calls: 0')
