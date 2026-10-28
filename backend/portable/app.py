"""Authenticated visitor-only entry, reusing the unchanged Coze business code."""
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('COZE_WORKSPACE_PATH', str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from visitor_api import router, migrate


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        if not os.getenv('MENGNIU_AGENT_API_TOKEN', '').strip():
            raise RuntimeError('Set MENGNIU_AGENT_API_TOKEN through the server environment')
        if os.getenv('VISITOR_DEPLOYMENT_ENABLED') == '1':
            if not os.getenv('PGDATABASE_URL', '').strip():
                raise RuntimeError('Set PGDATABASE_URL to a new, dedicated PostgreSQL database')
            migrate()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith('/visitor/'):
            expected = os.getenv('MENGNIU_AGENT_API_TOKEN', '')
            supplied = request.headers.get('Authorization', '')
            if not expected or not hmac.compare_digest(supplied.encode(), ('Bearer ' + expected).encode()):
                return JSONResponse({'detail': '需要服务端访问凭证'}, status_code=401)
            if request.method == 'POST' and request.url.path == '/visitor/v1/dispatch':
                raw = await request.body()
                if len(raw) > 250000:
                    return JSONResponse({'detail': '资料过长'}, status_code=413)
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    return JSONResponse({'detail': '请求格式错误'}, status_code=400)
                if data.get('operation') == 'analyze' and os.getenv('MENGNIU_ALLOW_MODEL_CALLS') != '1':
                    return JSONResponse({'detail': '当前为无模型验收模式，未发起分析'}, status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    def healthz():
        # Process liveness only. Authenticated /visitor/v1/health checks DB + rules.
        return {'ready': True, 'service': 'mengniu-agent-standalone', 'model_called': False}

    app.include_router(router)
    return app


app = create_app()
