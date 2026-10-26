"""web.auth — 单账号会话认证。

- POST /api/v1/login  : 校验账号（恒定时间比较）→ 建会话 → Set-Cookie。
- POST /api/v1/logout : 主动失效会话。
- GET  /api/v1/me     : 当前会话信息。
- require_auth        : FastAPI Depends，供 M2/M3 业务路由复用。

登录限速：按客户端 IP 记连续失败次数；失败 >= LOGIN_MAX_FAILURES
后进入 LOGIN_LOCKOUT_SECONDS 锁定窗口，窗口内一律 429。

⚠️ **P2-C（2026-10-23）两处口径订正**：
  ① **计数跨重启存活**：失败计数/锁定窗口落 SQLite（``web.session_store`` 的
     ``login_failures`` 表）⇒ 运维重启服务不再把暴力破解窗口清零（计数**单调**）。
     进程内存只是热路径缓存；**恢复点放在限速闸门**（``_check_lockout``）——
     登录流程必定先过闸门 ⇒ 重启后的第一次尝试就会把计数接上。
  ② **cookie 的 Secure 跟着实际对外协议走**：不再只看静态 ``USE_HTTPS``
     （生产 .env 是 USE_HTTPS=0，而公网入口是 https ⇒ cookie 一直没带 Secure）。
     详见 ``_cookie_secure`` 的降级口径。
"""
from __future__ import annotations

import hmac
import threading
import time

from fastapi import Request, Response
from fastapi.routing import APIRouter

from web import config, session_store
from web.errors import ApiError

router = APIRouter(prefix="/api/v1", tags=["auth"])

COOKIE_NAME = "lp_session"
_FAILURE_TTL_SECONDS = config.LOGIN_LOCKOUT_SECONDS
_failures: dict[str, dict] = {}  # ip -> {"count": int, "lockout_until": float, "last_failure_at": float}
_failures_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取客户端 IP，区分直连 vs 反代。

    仅当直连 IP 在 ``config.TRUSTED_PROXY_IPS`` 白名单内时，才采纳
    ``X-Forwarded-For`` 首跳 / ``X-Real-IP``；否则完全忽略代理头（防伪造）。
    直连 IP 不在白名单时仍按 request.client.host 计费，避免外部客户端直连
    后端绕过 nginx 限速。
    """
    direct = request.client.host if request.client else "unknown"
    if direct not in config.TRUSTED_PROXY_IPS:
        return direct
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # X-Forwarded-For: client, proxy1, proxy2 → 取首项（原始客户端）
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return direct


def _request_is_https(request: Request | None) -> bool:
    """本次请求的**对外协议**是不是 https。

    判定顺序：
      ① 直连 TLS（``request.url.scheme == "https"``）；
      ② **受信反代**透传的 ``X-Forwarded-Proto: https``
         （只在直连 IP 属于 ``config.TRUSTED_PROXY_IPS`` 时采信 —— 与
         ``_client_ip`` 同一套防伪造口径：外部客户端可以随便伪造这个头）。
    """
    if request is None:
        return False
    url = getattr(request, "url", None)
    if url is not None and getattr(url, "scheme", "") == "https":
        return True
    client = getattr(request, "client", None)
    direct = client.host if client else "unknown"
    if direct in config.TRUSTED_PROXY_IPS:
        xfp = request.headers.get("x-forwarded-proto", "")
        first = xfp.split(",", 1)[0].strip().lower()
        if first == "https":
            return True
    return False


def _cookie_secure(request: Request | None = None) -> bool:
    """会话 cookie 是否带 ``Secure``。

    ⚠️ **P2-C（2026-10-23）**：旧实现是 ``secure=config.USE_HTTPS``
    ⇒ 生产 ``.env`` 里 ``USE_HTTPS=0``（nginx→后端走 http）⇒ cookie **没有 Secure**
    ⇒ 用户先访问 ``http://host/...`` 时，会话 token 会在**明文请求**里过网
    （之后才被 301 到 https）。

    ⇒ 现在 ``Secure`` 跟着**实际对外协议**走：
      - ``USE_HTTPS=1``（运维显式声明「站点是 https」）⇒ 一律带 Secure；
      - 本次请求本身是 https（直连 TLS，或**受信反代**的 X-Forwarded-Proto）⇒ 带 Secure；
      - 其它（**本地 http 开发/自测**）⇒ **不带** Secure ——
        这是**明确的降级口径**：浏览器拒收带 Secure 的 http cookie ⇒
        本地会直接登不上（不是遗漏，是取舍）。

    ⚠️ 部署提醒：生产 nginx 必须透传 ``X-Forwarded-Proto $scheme``；
    若没透传，则要么补上，要么把生产 ``.env`` 的 ``USE_HTTPS`` 置 1。
    """
    if config.USE_HTTPS:
        return True
    return _request_is_https(request)


def _restore_failure(ip: str) -> dict | None:
    """从落盘的 ``login_failures`` 恢复该 IP 的失败状态（**重启后**的唯一真相）。

    只在「进程内存里没有该 IP 的记录」时调用（= 刚重启，或内存记录已被清理）。
    恢复条件：记录不陈旧（``last_failure_at`` 在 TTL 内）**或** 锁定窗口仍然有效
    ⇒ 锁定窗口必须跨重启生效（否则重启一次 = 暴力破解窗口重开一次）。
    """
    rec = session_store.load_login_failure(ip)
    if rec is None:
        return None
    now = time.time()
    stale = now - rec.get("last_failure_at", 0.0) > _FAILURE_TTL_SECONDS
    locked = rec.get("lockout_until", 0.0) > now
    if stale and not locked:
        return None
    with _failures_lock:
        if ip not in _failures:
            _failures[ip] = dict(rec)
    return rec


def _check_lockout(ip: str) -> None:
    """锁定窗口内 raise 429。

    ⚠️ **P2-C（2026-10-23）**：限速计数原来只在**进程内存**里 ⇒ 服务一重启就清零
    ⇒ 运维每重启一次，暴力破解窗口就被重新打开一次。
    ⇒ 内存里没有该 IP 时**回落到落盘状态**（``login_failures`` 表）：
    登录流程必定先过这道闸门 ⇒ 重启后的第一次尝试就把计数接上（**单调**）。
    """
    with _failures_lock:
        rec = _failures.get(ip)
    if rec is None:
        rec = _restore_failure(ip)
    if rec and rec.get("lockout_until", 0.0) > time.time():
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
        snapshot = dict(rec)
    # ⚠️ 落盘（跨重启存活）；写失败只记日志，不许把登录打成 500。
    session_store.record_login_failure(
        ip, snapshot["count"], snapshot["lockout_until"], snapshot["last_failure_at"]
    )


def _clear_failures(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)
    # 落盘的那份也要清（登录成功 ⇒ 计数归零，且不会在下次重启后复活）。
    session_store.clear_login_failure(ip)


def _set_session_cookie(response: Response, token: str,
                         secure: bool | None = None) -> None:
    """下发会话 cookie。

    ``secure=None`` ⇒ 退回 ``config.USE_HTTPS``（**不**看请求），
    供不持有 request 的调用方/单测使用；登录路径一律显式传
    ``_cookie_secure(request)`` 的结果（跟着实际对外协议走）。
    """
    if secure is None:
        secure = config.USE_HTTPS
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
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
    _set_session_cookie(response, token, secure=_cookie_secure(request))
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
    """FastAPI 依赖：未登录抛 401（统一 JSON 错误）。

    体彩 /jc/ 与 Nginx Basic Auth 隔离（nginx 对该路径 auth_basic off），
    本依赖是体彩项目的独立认证（session cookie）。Nginx 账号只保护其它业务。
    """
    _current_token(request)
