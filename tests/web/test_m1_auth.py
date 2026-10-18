"""M1 验收测试：会话认证闭环（验收点 B/C 全矩阵）。

fixture 值一律 unit-test- 前缀 dummy；测试使用独立临时 SQLite 库，
与开发/生产 .env 完全隔离。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 让 `web` 包可导入（tests/web 下运行时 sys.path 需含仓库根）。
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """临时 env + 临时会话库 + 应用实例（每测试全新）。"""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "43200")
    monkeypatch.setenv("LOGIN_MAX_FAILURES", "5")
    monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "600")

    # 重新加载 config 使 env 生效（模块级常量）。
    import importlib
    import web.config as config
    import web.session_store as session_store
    import web.auth as auth

    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    auth = importlib.reload(auth)

    from web.api import create_app

    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _login(client, username="unit-test-user", password="unit-test-pass"):
    return client.post(
        "/api/v1/login",
        json={"username": username, "password": password},
    )


# --- 验收点 B：免鉴权路径 --------------------------------------------------

def test_health_no_auth(client, monkeypatch):
    """免鉴权：mock 探活为 OK，避免依赖真实 predictions/ 目录状态。"""
    from web.services import health as health_svc

    monkeypatch.setattr(health_svc, "check", lambda: {
        "ok": True,
        "time_utc_epoch": 123.0,
        "checks": {"sessions_db": {"ok": True, "path": "/tmp/s.db"},
                   "predictions_fresh": {"ok": True, "freshest_age_hours": 1}},
    })
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "time_utc_epoch" in body
    assert "checks" in body


def test_root_redirects_to_dashboard_when_anonymous(client):
    """P5：登录页下线后，GET / 直接 302 到 dashboard.html。

    鉴权由 Nginx Basic Auth 在反代层把关；web 进程只管渲染 SPA，
    不再持有「未登录跳登录页」的概念。
    """
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/static/dashboard.html"


def test_login_route_removed(client):
    """P5：/login 路由已移除（页面下线，避免运维误以为还存在）。"""
    res = client.get("/login", follow_redirects=False)
    assert res.status_code == 404


def test_static_login_html_removed(client):
    """P5：static/login.html 已下线 → 静态挂载返回 404。"""
    res = client.get("/static/login.html")
    assert res.status_code == 404


def test_me_unauthorized_uniform_error(client):
    res = client.get("/api/v1/me")
    assert res.status_code == 401
    body = res.json()
    assert body["code"] == "unauthorized"
    assert "message" in body


def test_unknown_route_uniform_404(client):
    res = client.get("/api/v1/nonexistent")
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "http_error"
    assert "message" in body


# --- 验收点 C：登录闭环 ----------------------------------------------------

def test_login_success_sets_http_only_cookie(client):
    res = _login(client)
    assert res.status_code == 200
    cookie = res.headers.get("set-cookie", "")
    assert "lp_session=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower() or "SameSite=Lax" in cookie
    assert "Secure" not in cookie  # 默认 USE_HTTPS=0


def test_login_success_then_me_ok(client):
    res = _login(client)
    assert res.status_code == 200
    token = res.cookies.get("lp_session")
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["username"] == "unit-test-user"
    assert token  # cookie 确实下发


def test_login_wrong_password_no_cookie(client):
    res = client.post(
        "/api/v1/login",
        json={"username": "unit-test-user", "password": "unit-test-wrong"},
    )
    assert res.status_code == 401
    assert "set-cookie" not in res.headers
    body = res.json()
    assert body["code"] == "unauthorized"


def test_login_wrong_username_no_cookie(client):
    res = client.post(
        "/api/v1/login",
        json={"username": "unit-test-nobody", "password": "unit-test-pass"},
    )
    assert res.status_code == 401
    assert "set-cookie" not in res.headers


def test_login_non_ascii_credentials_no_typeerror(client, monkeypatch):
    """中文账号 + 错误非 ASCII 密码必须 401；回归 hmac.compare_digest
    对非 ASCII str 抛 TypeError 导致 500 的缺陷（WO-M7b1）。"""
    import web.config as config

    monkeypatch.setattr(config, "AUTH_USERNAME", "管理员")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "口令123")
    res = client.post(
        "/api/v1/login",
        json={"username": "管理员", "password": "错误口令"},
    )
    assert res.status_code == 401
    body = res.json()
    assert body["code"] == "unauthorized"


def test_login_five_failures_then_rate_limited(client):
    for _ in range(5):
        res = client.post(
            "/api/v1/login",
            json={"username": "unit-test-user", "password": "unit-test-wrong"},
        )
        assert res.status_code == 401
    res = client.post(
        "/api/v1/login",
        json={"username": "unit-test-user", "password": "unit-test-pass"},
    )
    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"


def test_logout_then_me_401(client):
    _login(client)
    assert client.get("/api/v1/me").status_code == 200
    res = client.post("/api/v1/logout")
    assert res.status_code == 200
    assert client.get("/api/v1/me").status_code == 401


def test_logout_without_login_401(client):
    res = client.post("/api/v1/logout")
    assert res.status_code == 401


def test_me_with_garbage_token_401(client):
    client.cookies.set("lp_session", "unit-test-garbage-token")
    assert client.get("/api/v1/me").status_code == 401


# --- 会话过期（UTC epoch 纪律）--------------------------------------------

def test_session_expiry_time_is_utc_epoch(app, tmp_path):
    """过期时间必须存 UTC epoch 秒；写一条短 TTL 会话后立即失效。"""
    import sqlite3
    import time as _time

    import web.config as config
    import web.session_store as store

    ttl = 1
    token = store.create_session(ttl_seconds=ttl)
    now = _time.time()
    assert store.validate_token(token) is True

    with sqlite3.connect(config.SESSION_DB_PATH) as conn:
        row = conn.execute(
            "SELECT created, expires FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    # created/expires 是数值 epoch（非字符串/非本地时间）。
    assert isinstance(row[0], (int, float)) and isinstance(row[1], (int, float))
    assert row[0] <= now < row[1]
    assert row[1] - row[0] == ttl

    _time.sleep(ttl + 0.1)
    assert store.validate_token(token) is False
