"""P1-1 验收测试：代理头白名单 + X-Forwarded-For 受控采纳。

修复点：旧 _client_ip 直接用 request.client.host，反代后恒为 127.0.0.1
→ 凭据爆破限速失效（_failures[127.0.0.1] 全局单条）。

修复：仅当直连 IP 在 config.TRUSTED_PROXY_IPS 白名单内时采纳代理头；
否则忽略代理头，规避伪造。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_request(client_host: str, headers: dict | None = None):
    """构造 mock FastAPI Request。"""
    from unittest.mock import MagicMock
    req = MagicMock()
    req.client.host = client_host
    req.headers = headers or {}
    return req


def test_untrusted_direct_ignores_proxy_headers():
    """直连 IP 不在白名单 → 完全不读 X-Forwarded-For / X-Real-IP。

    模拟外部客户端直连后端（绕过 nginx），伪造 XFF 头不能改变记录的 IP。
    """
    from web import auth, config

    req = _make_request(
        "203.0.113.50",  # RFC 5737 doc IP，假设不在白名单
        {"x-forwarded-for": "1.2.3.4", "x-real-ip": "9.9.9.9"},
    )
    ip = auth._client_ip(req)
    assert ip == "203.0.113.50", f"应忽略代理头，got {ip}"


def test_trusted_direct_reads_xff_first_hop():
    """直连 IP 是 nginx（127.0.0.1）+ 有 XFF → 取首跳（原始客户端）。"""
    from web import auth, config

    req = _make_request(
        "127.0.0.1",
        {"x-forwarded-for": "203.0.113.50, 10.0.0.1, 127.0.0.1"},
    )
    ip = auth._client_ip(req)
    assert ip == "203.0.113.50", f"应取 XFF 首跳，got {ip}"


def test_trusted_direct_reads_x_real_ip_when_no_xff():
    """直连 IP 是 nginx + 无 XFF + 有 X-Real-IP → 取 X-Real-IP。"""
    from web import auth, config

    req = _make_request(
        "127.0.0.1",
        {"x-real-ip": "203.0.113.60"},
    )
    ip = auth._client_ip(req)
    assert ip == "203.0.113.60", f"应取 X-Real-IP，got {ip}"


def test_trusted_direct_falls_back_to_direct_when_no_headers():
    """直连 IP 是 nginx 但无代理头 → 退化为 direct（不异常）。"""
    from web import auth

    req = _make_request("127.0.0.1", {})
    ip = auth._client_ip(req)
    assert ip == "127.0.0.1"


def test_trusted_direct_xff_empty_value_falls_back():
    """XFF 为空字符串 / 仅空白 → 退化为 X-Real-IP 或 direct。"""
    from web import auth

    req = _make_request("127.0.0.1", {"x-forwarded-for": "   , , "})
    ip = auth._client_ip(req)
    # XFF 全空 → 走 X-Real-IP / direct
    assert ip == "127.0.0.1"


def test_trusted_proxy_ips_default_includes_loopback():
    """config 默认白名单包含 127.0.0.1（本地 nginx）+ ::1（IPv6 loopback）。"""
    from web import config

    assert "127.0.0.1" in config.TRUSTED_PROXY_IPS
    assert "::1" in config.TRUSTED_PROXY_IPS


def test_xff_with_spaces_and_uppercase_headers():
    """FastAPI Headers 自动小写；本测试也覆盖多空格。"""
    from web import auth

    req = _make_request(
        "127.0.0.1",
        {"x-forwarded-for": "  203.0.113.99  ,  10.0.0.1  "},
    )
    ip = auth._client_ip(req)
    assert ip == "203.0.113.99"


def test_login_rate_limit_uses_real_ip_when_behind_proxy():
    """端到端：登录失败 5 次应锁定真实客户端 IP（不是 nginx IP）。

    mock 配置 + TestClient + 5 次错误登录后第 6 次应 429。
    """
    from unittest.mock import patch
    from fastapi.testclient import TestClient

    from web import auth, config, errors
    from web.auth import router as auth_router

    # 用 m1 的 app fixture 不依赖：临时建一个 app
    from fastapi import FastAPI
    app = FastAPI()
    errors.register(app)  # 让 ApiError 转 401/429 JSON 响应
    app.include_router(auth_router)

    # 清失败记录
    with auth._failures_lock:
        auth._failures.clear()

    with patch.object(config, "TRUSTED_PROXY_IPS", ("127.0.0.1",)):
        # TestClient 默认 client=("testclient") 不在白名单，无法测反代路径；
        # 显式设为 127.0.0.1 模拟 nginx 反代直连。
        client = TestClient(app, client=("127.0.0.1", 5000))
        # 5 次错误密码 + 真实 IP 在 XFF
        bad_headers = {
            "x-forwarded-for": "203.0.113.50",
        }
        for i in range(config.LOGIN_MAX_FAILURES):
            r = client.post(
                "/api/v1/login",
                json={"username": config.AUTH_USERNAME, "password": "wrong"},
                headers=bad_headers,
            )
            # 前 5 次应是 401（凭据错），第 6 次应是 429（锁定）
        # 第 6 次：被锁定
        r = client.post(
            "/api/v1/login",
            json={"username": config.AUTH_USERNAME, "password": "wrong"},
            headers=bad_headers,
        )
    assert r.status_code == 429, (
        f"反代后凭据爆破应按真实客户端 IP 锁定；got {r.status_code} {r.text}"
    )
    # 失败计数应记到 203.0.113.50，不是 127.0.0.1
    assert "203.0.113.50" in auth._failures, (
        f"失败应被记到 XFF 首跳 IP；failures={list(auth._failures.keys())}"
    )
    assert "127.0.0.1" not in auth._failures