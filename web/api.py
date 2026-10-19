"""web.api — league-api 服务入口（FastAPI 应用工厂）。

- GET  /health    免鉴权健康检查
- 静态挂载 /static/（含 dashboard.html / dashboard-login.html / dailyimage.html）
- R47 恢复独立登录页：未登录 GET / → 302 /login → static/dashboard-login.html
  （P5 删除登录页的前提「鉴权由 Nginx Basic Auth 把关」不成立，见 index() 注释）。
- 注册 auth 路由与统一异常处理
- python web/api.py 启动开发服务器（host/port 读 env，默认仅本机）
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from web import config, errors
from web.auth import router as auth_router
from web.services import health as health_svc

try:
    from web.routers.ai import router as ai_router
except Exception:  # AI 扩展模块损坏不得拖垮 app 启动（WO-M3 验收点 C）
    errors.logger.exception("AI 路由导入失败，已降级跳过")
    ai_router = None
from web.lifecycle import lifespan
from web.routers.admin import router as admin_router  # R15b
from web.routers.combo import router as combo_router
from web.routers.haf import router as haf_router
from web.routers.jc import router as jc_router
from web.routers.jc import v1_router as jc_v1_router
from web.routers.jc_ops import router as jc_ops_router
from web.routers.jobs import router as jobs_router
from web.routers.lottery_api import router as lottery_api_router
from web.routers.predictions import router as predictions_router
from web.routers.sources import router as sources_router
from web.routers.ticket import router as ticket_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_TITLE = "league-api"
APP_VERSION = "0.1.0"

# scripts/（core 纯函数，如 core.backtest.league_accuracy）供 web 复用：
# 追加到 sys.path 末尾（不前置），避免 scripts/ 下同名包遮蔽 web/、store/。
import sys as _sys

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.append(_SCRIPTS_DIR)


# ── observability 装挂（Round 9 H3 + H9）────────────────────
# web 进程独立写 ``events.web.jsonl`` / ``runtime.web.log``，与 predict subprocess
# 共享 ``FOOTBALL_DIR/logs/`` 时不抢同一文件；uvicorn worker 线程异常不再
# 走默认 silent 丢，走 ``threading.excepthook`` 落 runtime.web.log + events。
# 装挂失败不阻塞 app（与 AI 路由降级同源语义 —— 错误日志已有，进程得有）。
try:
    from core import observability  # noqa: E402 — sys.path 已加

    # R13 #1：events.web.jsonl 启用 10MB×5 rotation（H8）。
    # web jobs spin_state + AI LLM 失败持续累积；无 rotation 单文件月级 GB。
    observability.setup(process_name="web", events_max_bytes=10 * 1024 * 1024,
                        events_backup_count=5)
except Exception as _obs_exc:  # noqa: BLE001 — 装挂失败仅记日志，进程继续
    errors.logger.warning("observability.setup(process_name='web') 失败：%s", _obs_exc)


def create_app() -> FastAPI:
    app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)
    errors.register(app)

    @app.get("/health")
    def health():
        """真依赖探活：sessions.db SQLite + 最新 prediction mtime。

        任一探活失败 → 503；body 携带各探活项明细，便于监控告警。
        """
        result = health_svc.check()
        result["config"] = config.env_summary()
        return JSONResponse(
            status_code=200 if result["ok"] else 503,
            content=result,
        )

    @app.get("/")
    def index(request: Request):
        """未登录 → 302 /login；已登录 → 302 dashboard.html。

        R47 修复：2026-10-18（P5 / 093dafa）曾以「鉴权由 Nginx Basic Auth 在反代层把关」
        为由删掉本路由的登录判定与 /login 路由。但生产 nginx 对 /dashboard/jc/ 是
        ``auth_basic off``（免凭据 200），而 ``require_auth`` 只认 ``lp_session`` cookie、
        不接受 Basic 头 —— 公网体彩看板因此失去了唯一的登录入口（页面 200、API 全 401）。
        实测口径（2026-10-19）：/dashboard/jc/ → 200、/dashboard/jc/api/v1/me → 401、
        /dashboard/jc/login → 404（登录页缺失）。
        """
        from web import session_store
        from web.auth import COOKIE_NAME

        token = request.cookies.get(COOKIE_NAME, "")
        if not token or not session_store.validate_token(token):
            return RedirectResponse(url="/login", status_code=302)
        # 已登录进入新版 SPA（dashboard.html，API 路径带 /dashboard/jc/ 前缀）。
        return RedirectResponse(url="/static/dashboard.html", status_code=302)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/login")
    def login_page():
        """独立登录页（R47 恢复）。

        页面：static/login.html —— 它会从当前 URL 推导 API 基址
        （含 /dashboard/jc/ 前缀时用之，否则用 /），因此后端直连与经 nginx 两种
        场景都能正确提交登录。公网 nginx ``location = /dashboard/jc/login`` 直接
        proxy 的是 static/dashboard-login.html（硬编码公网路径），两者等价。
        """
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
    app.include_router(combo_router)
    app.include_router(haf_router)
    app.include_router(lottery_api_router)
    app.include_router(ticket_router)
    app.include_router(admin_router)  # R15b
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
