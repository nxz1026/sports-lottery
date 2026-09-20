"""web.api — FastAPI 应用工厂与开发入口。

- GET  /health    免鉴权健康检查
- 静态挂载 /login（static/login.html）；未登录 GET / → 302 /login
- 注册 auth 路由与统一异常处理
- python web/api.py 启动开发服务器（host/port 读 env，默认仅本机）
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from web import config
from web import errors
from web.auth import router as auth_router
try:
    from web.routers.ai import router as ai_router
except Exception:  # AI 扩展模块损坏不得拖垮 app 启动（WO-M3 验收点 C）
    errors.logger.exception("AI 路由导入失败，已降级跳过")
    ai_router = None
from web.routers.jobs import router as jobs_router
from web.routers.jc import router as jc_router, v1_router as jc_v1_router
from web.routers.jc_ops import router as jc_ops_router
from web.routers.predictions import router as predictions_router
from web.routers.sources import router as sources_router
from web.lifecycle import lifespan

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_TITLE = "league-predict Web Dashboard"
APP_VERSION = "0.1.0"

# scripts/（core 纯函数，如 core.backtest.league_accuracy）供 web 复用：
# 追加到 sys.path 末尾（不前置），避免 scripts/ 下同名包遮蔽 web/、store/。
import sys as _sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.append(_SCRIPTS_DIR)


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    errors.register(app)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "time_utc_epoch": time.time(),
            "config": config.env_summary(),
        }

    @app.get("/")
    def index(request: Request):
        from web.auth import COOKIE_NAME
        from web import session_store
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or not session_store.validate_token(token):
            return RedirectResponse(url="/login", status_code=302)
        # M4：已登录进入单文件 SPA。
        return RedirectResponse(url="/static/index.html", status_code=302)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/login")
    def login_page():
        return RedirectResponse(url="/static/login.html", status_code=302)

    app.include_router(auth_router)
    app.include_router(predictions_router)
    app.include_router(sources_router)
    app.include_router(jobs_router)
    app.include_router(jc_router)
    app.include_router(jc_v1_router)
    app.include_router(jc_ops_router)
    if ai_router is not None:
        app.include_router(ai_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
