"""web.auth — 单账号会话认证。

- POST /api/v1/login  : 校验账号（恒定时间比较）→ 建会话 → Set-Cookie。
- POST /api/v1/logout : 主动失效会话。
- GET  /api/v1/me     : 当前会话信息。
- require_auth        : FastAPI Depends，供 M2/M3 业务路由复用。

登录限速：进程内字典，按客户端 IP 记连续失败次数；失败 >= LOGIN_MAX_FAILURES
后进入 LOGIN_LOCKOUT_SECONDS 锁定窗口，窗口内一律 429。
"""
from __future__ import annotations

import hmac
import threading
import time

from fastapi import Request, Response
from fastapi.routing import APIRouter

from web import config
from web import session_store
from web.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["auth"])

COOKIE_NAME = "lp_session"
_FAILURE_TTL_SECONDS = config.LOGIN_LOCKOUT_SECONDS
_failures: dict[str, dict] = {}  # ip -> {"count": int, "lockout_until": float, "last_failure_at": float}
_failures_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # 信任代理头会引入伪造风险；默认用直连地址（本机部署场景足够）。
    return request.client.host if request.client else "unknown"


def _check_lockout(ip: str) -> None:
    with _failures_lock:
        rec = _failures.get(ip)
        if rec and rec["lockout_until"] > time.time():
            raise ApiError(
                "rate_limited",
                "登录失败次数过多，请稍后再试",
                http_status=429,
            )


def _record_failure(ip: str) -> None:
    global _failures
    now = time.time()
    with _failures_lock:
        # 清理过期锁定和长期没有新失败的普通记录，防止字典无界增长。
        def active(rec: dict) -> bool:
            lockout_until = rec.get("lockout_until", 0.0)
            last_failure_at = rec.get("last_failure_at", 0.0)
            if lockout_until > 0:
                return not (lockout_until < now and now - lockout_until > _FAILURE_TTL_SECONDS)
            return now - last_failure_at <= _FAILURE_TTL_SECONDS

        _failures = {k: rec for k, rec in _failures.items() if active(rec)}
        rec = _failures.setdefault(ip, {"count": 0, "lockout_until": 0.0})
        rec["count"] += 1
        rec["last_failure_at"] = now
        if rec["count"] >= config.LOGIN_MAX_FAILURES:
            rec["lockout_until"] = time.time() + config.LOGIN_LOCKOUT_SECONDS
            rec["count"] = 0


def _clear_failures(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=config.USE_HTTPS,
    )


@router.post("/login")
def login(request: Request, response: Response,
          body: dict | None = None) -> dict:
    """校验账号并签发会话 cookie。body: {username, password}。"""
    ip = _client_ip(request)
    _check_lockout(ip)
    body = body or {}
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    ok_user = hmac.compare_digest(
        username.encode("utf-8"), config.AUTH_USERNAME.encode("utf-8")
    )
    ok_pass = hmac.compare_digest(
        password.encode("utf-8"), config.AUTH_PASSWORD.encode("utf-8")
    )
    if not (ok_user and ok_pass):
        _record_failure(ip)
        raise ApiError("unauthorized", "用户名或密码错误", http_status=401)
    _clear_failures(ip)
    token = session_store.create_session()
    _set_session_cookie(response, token)
    return {"message": "ok", "expires_in": config.SESSION_TTL_SECONDS}


def _current_token(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or not session_store.validate_token(token):
        raise ApiError("unauthorized", "未登录或会话已过期", http_status=401)
    return token


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token = _current_token(request)
    session_store.delete_session(token)
    response.delete_cookie(COOKIE_NAME)
    return {"message": "ok"}


@router.get("/me")
def me(request: Request) -> dict:
    _current_token(request)
    return {"username": config.AUTH_USERNAME, "authenticated": True}


def require_auth(request: Request) -> None:
    """FastAPI 依赖：未登录抛 401（统一 JSON 错误）。"""
    _current_token(request)
