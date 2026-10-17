"""R51-1+2: web/auth.py 5 个 0 直测 helper 9 测 + D-97 / D-98 双契约锁。

R50 末已锁 D-92 ~ D-96 (compute_haf / freshness / market_projection /
live_accuracy / validated_model_probs + prediction_summary), 但 web.auth
5 个私有 helper 完全 0 直测, 只有 HTTP 集成层间接覆盖:

- _check_lockout     (L55-63): 锁定窗口内 raise 429 ApiError
- _record_failure    (L66-84): 累计失败, 达阈值设 lockout_until + count 清零
- _clear_failures    (L87-89): login 成功清空 ip 记录
- _set_session_cookie(L92-100): httponly/samesite/secure/TTL 设置
- _current_token     (L127-131): valid cookie 返 token, 否则 401

D-97 锁 _check_lockout + _record_failure + _clear_failures 三契约:
- D-97-1: _check_lockout 锁定窗口内 raise ApiError(rate_limited, http=429)
- D-97-2: _check_lockout 锁定窗口外 / 无记录 不 raise
- D-97-3: _record_failure 单 IP 累计 +1, 未达阈值 lockout_until 仍 0
- D-97-4: _record_failure 达阈值后 lockout_until > now + count 归零
- D-97-5: _clear_failures 真从字典移除 ip 记录

D-98 锁 _set_session_cookie + _current_token 两契约:
- D-98-1: _set_session_cookie 设置 httponly/samesite=lax/COOKIE_NAME=lp_session/
          max_age=SESSION_TTL_SECONDS/secure 跟随 config.USE_HTTPS
- D-98-2: _set_session_cookie USE_HTTPS=False 时 secure=False
- D-98-3: _current_token 有 valid cookie 返 token
- D-98-4: _current_token 无 cookie / validate_token=False 时 raise 401

策略:
- spec-load web.auth 到隔离 namespace (避免污染 module-global _failures)
- 不替换 time.time(), 直接构造 _failures dict 场景
- stub web.config.{LOGIN_MAX_FAILURES,LOGIN_LOCKOUT_SECONDS,USE_HTTPS,
  SESSION_TTL_SECONDS} + session_store.{create_session,validate_token} —
  session_store 涉及 sqlite3, 不真起 DB.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def auth_mod(monkeypatch):
    """Spec-load web.auth 到隔离 namespace + stub config/session_store.

    - LOGIN_MAX_FAILURES=3 (短阈值便于单测 4 测覆盖完整失败链路)
    - LOGIN_LOCKOUT_SECONDS=60 (短窗口但绝对正向)
    - SESSION_TTL_SECONDS=43200 (匹配 config 默认)
    - USE_HTTPS 默认 True, _set_session_cookie USE_HTTPS=False 时显式 stub
    - session_store.create_session 返受控 token 'fake-token-test'
    - session_store.validate_token 仅 'valid-tok' 通过
    """
    spec = importlib.util.spec_from_file_location(
        "web_auth_test",
        REPO_ROOT / "web" / "auth.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 短阈值: 3 次失败立即触发
    monkeypatch.setattr("web.config.LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr("web.config.LOGIN_LOCKOUT_SECONDS", 60)
    monkeypatch.setattr("web.config.SESSION_TTL_SECONDS", 43200)
    monkeypatch.setattr("web.config.USE_HTTPS", True)

    # session_store stub: 不真起 sqlite3, 受控返 token / validate
    import web.session_store as ss_mod  # noqa: PLC0415 - fixture 本地 import
    monkeypatch.setattr(ss_mod, "create_session", lambda: "fake-token-test")
    monkeypatch.setattr(ss_mod, "validate_token", lambda t: t == "valid-tok")
    monkeypatch.setattr(ss_mod, "delete_session", lambda t: None)

    # 清空跨测残留 (spec-load 已经隔离, 但保险)
    mod._failures.clear()

    return mod


# ──────────────────────── D-97: 5 测 ────────────────────────


def test_d97_1_check_lockout_raises_when_in_window(auth_mod):
    """D-97-1: _check_lockout 在锁定窗口内 raise ApiError(rate_limited, http=429).

    构造 ip="x"的 _failures 项含 lockout_until = now + 1000 (未来 1k 秒),
    _check_lockout 必须 raise 且 code="rate_limited" / http_status=429.
    """
    from web.errors import ApiError  # noqa: PLC0415 - 真实 Exception, 直接验证

    now = time.time()
    auth_mod._failures["x"] = {
        "count": 3, "lockout_until": now + 1000, "last_failure_at": now,
    }

    with pytest.raises(ApiError) as exc_info:
        auth_mod._check_lockout("x")
    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.http_status == 429


def test_d97_2_check_lockout_no_raise_when_out_of_window(auth_mod):
    """D-97-2: 锁定窗口外 (lockout_until <= now) 或无记录 不 raise.

    - 场景 a: lockout_until 在过去 (now - 100), 过期锁定不 raise
    - 场景 b: 无 ip 记录 (全新 ip), 不 raise
    """
    now = time.time()
    # 过期锁定
    auth_mod._failures["stale"] = {
        "count": 3, "lockout_until": now - 100, "last_failure_at": now - 700,
    }
    auth_mod._check_lockout("stale")  # 不 raise 即过

    # 全新 ip 不在 _failures 里
    auth_mod._check_lockout("brand-new-ip")  # 不 raise 即过


def test_d97_3_record_failure_single_inc_no_lockout(auth_mod):
    """D-97-3: _record_failure 单 IP 累计 +1, 未达 LOGIN_MAX_FAILURES 时 lockout_until 仍 0.

    连续 _record_failure(ip) 两次 (LOGIN_MAX_FAILURES=3, 差 1 次):
    - count: 0 -> 1 -> 2
    - lockout_until: 保持 0.0 (未触发阈值)
    - last_failure_at: 递增
    """
    auth_mod._record_failure("alpha")
    rec = auth_mod._failures["alpha"]
    assert rec["count"] == 1
    assert rec["lockout_until"] == 0.0
    assert rec["last_failure_at"] <= time.time()

    auth_mod._record_failure("alpha")
    rec = auth_mod._failures["alpha"]
    assert rec["count"] == 2
    assert rec["lockout_until"] == 0.0


def test_d97_4_record_failure_triggers_lockout_and_resets_count(auth_mod):
    """D-97-4: _record_failure 累计到 LOGIN_MAX_FAILURES(=3) 时:
    - lockout_until = now + LOGIN_LOCKOUT_SECONDS (未来)
    - count 归零 (下一次失败从 1 开始)
    """
    before = time.time()
    auth_mod._record_failure("beta")
    auth_mod._record_failure("beta")
    auth_mod._record_failure("beta")  # 第 3 次触发阈值

    rec = auth_mod._failures["beta"]
    # lockout_until 必在未来 (now + 60s)
    assert rec["lockout_until"] > before + 50
    assert rec["lockout_until"] <= before + 65
    # count 归零: 下一次失败会从 1 开始
    assert rec["count"] == 0


def test_d97_5_clear_failures_removes_ip_record(auth_mod):
    """D-97-5: _clear_failures 真从 _failures dict 移除 ip 记录.

    记录 _record_failure 后 _clear_failures, _failures["ip"] 必须不存在.
    """
    auth_mod._record_failure("clear-me")
    assert "clear-me" in auth_mod._failures

    auth_mod._clear_failures("clear-me")
    assert "clear-me" not in auth_mod._failures

    # 不存在的 ip 不报错
    auth_mod._clear_failures("never-existed")  # 不 raise 即过


# ──────────────────────── D-98: 4 测 ────────────────────────


class _CookieSpyResponse:
    """Mock FastAPI Response, 只截 set_cookie 调用."""

    def __init__(self):
        self.calls: list[dict] = []

    def set_cookie(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})


def test_d98_1_set_session_cookie_uses_strict_attrs(auth_mod):
    """D-98-1: _set_session_cookie 设置 httponly=True/samesite='lax'/secure=True
    (USE_HTTPS=True 默认)/max_age=SESSION_TTL_SECONDS/COOKIE_NAME='lp_session'.

    用 CookieSpyResponse 截 set_cookie 调用, 验证所有 5 个字段.
    """
    resp = _CookieSpyResponse()
    auth_mod._set_session_cookie(resp, "tok-123")

    assert len(resp.calls) == 1
    call = resp.calls[0]
    kwargs = call["kwargs"]
    # 位置参数第 1 个是 cookie name (set_cookie 的 key 是位置或 kw)
    assert call["args"][0] == auth_mod.COOKIE_NAME
    assert call["args"][0] == "lp_session"
    assert call["args"][1] == "tok-123"
    assert kwargs.get("httponly") is True
    assert kwargs.get("samesite") == "lax"
    assert kwargs.get("secure") is True
    assert kwargs.get("max_age") == 43200


def test_d98_2_set_session_cookie_secure_false_when_http(auth_mod, monkeypatch):
    """D-98-2: USE_HTTPS=False 时 secure=False (HTTP 环境不发 Secure cookie)."""
    monkeypatch.setattr("web.config.USE_HTTPS", False)

    resp = _CookieSpyResponse()
    auth_mod._set_session_cookie(resp, "tok-no-secure")

    assert len(resp.calls) == 1
    kwargs = resp.calls[0]["kwargs"]
    assert kwargs.get("secure") is False
    # httponly / samesite 不受 USE_HTTPS 影响
    assert kwargs.get("httponly") is True
    assert kwargs.get("samesite") == "lax"


def test_d98_3_current_token_returns_token_for_valid_cookie(auth_mod):
    """D-98-3: _current_token: request.cookies 含 'valid-tok' 且 validate_token 返 True 时
    返 token (= 'valid-tok').
    """
    req = SimpleNamespace(cookies={"lp_session": "valid-tok"})
    out = auth_mod._current_token(req)
    assert out == "valid-tok"


def test_d98_4_current_token_raises_401_on_missing_or_invalid_cookie(auth_mod):
    """D-98-4: 无 cookie / validate_token 返 False 时 raise ApiError(unauthorized, 401).

    - 场景 a: cookies dict 不含 lp_session
    - 场景 b: lp_session 在但 validate_token('garbage') 返 False
    """
    from web.errors import ApiError  # noqa: PLC0415 - 真实 Exception

    # 场景 a: 无 cookie
    req_empty = SimpleNamespace(cookies={})
    with pytest.raises(ApiError) as exc_a:
        auth_mod._current_token(req_empty)
    assert exc_a.value.code == "unauthorized"
    assert exc_a.value.http_status == 401

    # 场景 b: cookie 在但 validate_token 返 False (fixture 只让 'valid-tok' 通过)
    req_bad = SimpleNamespace(cookies={"lp_session": "garbage-tok"})
    with pytest.raises(ApiError) as exc_b:
        auth_mod._current_token(req_bad)
    assert exc_b.value.code == "unauthorized"
    assert exc_b.value.http_status == 401
