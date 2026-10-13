"""web/routers/jc_ops 端到端测试：/api/jc/ops + /api/jc/freshness。

背景：web/routers/jc_ops.py 当前 41% 覆盖。P3-5 已守底层
``store.jc_ops_view.ops_snapshot()``（100% 覆盖），但 router 层端点
封装（鉴权 / degraded 计算 / freshness 阈值钳制）完全无测。

覆盖点：
  · /ops 端点鉴权 / 响应封装（rows / ok / sql_note / degraded）
  · /freshness 端点鉴权 / threshold_hours 钳制 [0.5, 168] / 异常降级

依赖 mock：jc_ops_view.ops_snapshot + web.services.freshness.evaluate_topics
+ format_alert_lines（避免走真 DB 与真 fresh 评估逻辑）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """env + reload + mock store 层 + 应用实例化。"""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    # 注：不 setenv TRUSTED_PROXY_IPS，保持 web.config 默认 tuple（含 127.0.0.1 + ::1）
    # 防止污染后续 test_client_ip.py::test_trusted_proxy_ips_default_includes_loopback

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)
    import web.api as api
    importlib.reload(api)

    # mock jc_ops_view.ops_snapshot 避免依赖 DSN
    import store.jc_ops_view as jcov
    monkeypatch.setattr(jcov, "ops_snapshot",
                        lambda: {"teams": [], "topics": [],
                                 "quota": [], "rejects": [],
                                 "ok_teams": True, "ok_topics": True,
                                 "ok_quota": True, "ok_rejects": True})
    # mock freshness 评估避免走真时间解析
    import web.services.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "evaluate_topics",
                        lambda topics, threshold_hours=24.0: {
                            "ok": True, "threshold_hours": threshold_hours,
                            "stale": [], "all": topics,
                        })
    monkeypatch.setattr(freshness_mod, "format_alert_lines",
                        lambda result: ["test alert line"])

    return api.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


# ────────────────────────── /api/jc/ops ──────────────────────────


def test_ops_unauthenticated_returns_401(client):
    """未登录 → 401（require_auth 守护）。"""
    r = client.get("/api/jc/ops")
    assert r.status_code in (401, 403), r.text


def test_ops_returns_full_structure(client, monkeypatch):
    """鉴权 + mock 全部 ok → 200 + rows/ok/sql_note/degraded 全有。"""
    import store.jc_ops_view as jcov
    monkeypatch.setattr(jcov, "ops_snapshot", lambda: {
        "teams": [{"league": "意甲", "n": 10}],
        "topics": [{"topic": "lottery_draw"}],
        "quota": [{"source": "api_football", "remaining": 90}],
        "rejects": [],
        "ok_teams": True, "ok_topics": True,
        "ok_quota": True, "ok_rejects": True,
    })
    cookies = _login(client)
    r = client.get("/api/jc/ops", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rows" in body
    assert "ok" in body
    assert "sql_note" in body
    assert "degraded" in body
    # rows 四键齐全
    assert set(body["rows"].keys()) == {"teams", "topics", "quota", "rejects"}
    assert set(body["ok"].keys()) == {"teams", "topics", "quota", "rejects"}
    # 全 ok → degraded=False
    assert body["degraded"] is False
    # sql_note 非空字符串
    assert isinstance(body["sql_note"], str) and len(body["sql_note"]) > 0


def test_ops_degraded_when_any_section_fails(client, monkeypatch):
    """任一段 ok=False → degraded=True。"""
    import store.jc_ops_view as jcov
    monkeypatch.setattr(jcov, "ops_snapshot", lambda: {
        "teams": [], "topics": [], "quota": [], "rejects": [],
        "ok_teams": True, "ok_topics": False,  # topics 异常
        "ok_quota": True, "ok_rejects": True,
    })
    cookies = _login(client)
    r = client.get("/api/jc/ops", cookies=cookies)
    body = r.json()
    assert body["ok"]["topics"] is False
    assert body["degraded"] is True


def test_ops_handles_missing_ok_keys_gracefully(client, monkeypatch):
    """snap 缺 ok_<key>（jcov 异常路径下可能发生）→ bool() 默认 False。"""
    import store.jc_ops_view as jcov
    monkeypatch.setattr(jcov, "ops_snapshot", lambda: {
        "teams": [], "topics": [],
        "quota": [], "rejects": [],
        # 故意缺 ok_<key>
    })
    cookies = _login(client)
    r = client.get("/api/jc/ops", cookies=cookies)
    body = r.json()
    # 所有 ok 默认 False
    assert all(v is False for v in body["ok"].values())
    assert body["degraded"] is True


# ────────────────────────── /api/jc/freshness ──────────────────────────


def test_freshness_unauthenticated_returns_401(client):
    """未登录 → 401。"""
    r = client.get("/api/jc/freshness")
    assert r.status_code in (401, 403), r.text


def test_freshness_default_threshold_24h(client):
    """threshold_hours 默认 24。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["threshold_hours"] == 24.0
    assert "alerts" in body


def test_freshness_clamps_threshold_to_min_half_hour(client):
    """threshold_hours < 0.5 → 钳制到 0.5。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness?threshold_hours=0.1", cookies=cookies)
    body = r.json()
    assert body["threshold_hours"] == 0.5


def test_freshness_clamps_threshold_to_max_168h(client):
    """threshold_hours > 168 → 钳制到 168（一周）。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness?threshold_hours=1000", cookies=cookies)
    body = r.json()
    assert body["threshold_hours"] == 168.0


def test_freshness_non_numeric_threshold_returns_422(client):
    """threshold_hours 非数字 → FastAPI Pydantic 422（路由前解析失败，handler 兜底不到）。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness?threshold_hours=abc", cookies=cookies)
    assert r.status_code == 422, r.text


def test_freshness_string_numeric_threshold_is_cast(client):
    """str 数字 threshold_hours → float（接口容错）。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness?threshold_hours=12", cookies=cookies)
    body = r.json()
    assert body["threshold_hours"] == 12.0


def test_freshness_handles_snapshot_exception(client, monkeypatch):
    """ops_snapshot 抛错 → topics 空 + logger.warning，200 仍返。"""
    import store.jc_ops_view as jcov

    def boom():
        raise RuntimeError("ops snapshot failed")

    monkeypatch.setattr(jcov, "ops_snapshot", boom)
    import web.services.freshness as freshness_mod
    monkeypatch.setattr(freshness_mod, "evaluate_topics",
                        lambda topics, threshold_hours=24.0: {
                            "ok": False, "threshold_hours": threshold_hours,
                            "stale": [], "all": [],
                        })
    cookies = _login(client)
    r = client.get("/api/jc/freshness", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    # 异常降级：topics 空、ok=False
    assert body["all"] == [] or body.get("ok") is False


def test_freshness_returns_alerts_lines(client):
    """alerts 字段 = format_alert_lines(result) 列表。"""
    cookies = _login(client)
    r = client.get("/api/jc/freshness", cookies=cookies)
    body = r.json()
    assert body["alerts"] == ["test alert line"]
