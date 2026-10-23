"""web/routers/ai 端到端测试：/api/v1/ai/{status,details,analyze,daily,ranking}。

背景：web/routers/ai.py 当前 38% 覆盖 — 5 个 AI 富化端点 +
_parse_day 私有辅助完全无测；现有 test_ai_report.py 只覆盖
service 层 ai.ai_daily_report / ai.ai_ranking 纯函数。

覆盖点：
  · /ai/status：未鉴权 → 401 / 正常 → 200
  · /ai/details：未鉴权 → 401 / 正常 → 200
  · /ai/analyze：date 校验 + 文件不存在 + 读取异常 + 正常 JSON
  · /ai/daily：date 校验 + 降级语义（invalid_date）
  · /ai/ranking：limit 范围 + date 校验
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date
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

    # 默认 mock ai service 让端点返回可控响应
    import web.services.ai as ai_svc
    monkeypatch.setattr(ai_svc, "ai_status",
                        lambda: {"available": True, "scores_count": 10})
    monkeypatch.setattr(ai_svc, "ai_details",
                        lambda: {"available": True, "items": []})
    monkeypatch.setattr(ai_svc, "ai_daily_report",
                        lambda d: {"available": True, "date": d.isoformat(),
                                   "items": [], "summary": {}})
    monkeypatch.setattr(ai_svc, "ai_ranking",
                        lambda d, limit: {"available": True, "date": d.isoformat(),
                                          "hot": [], "cold": [],
                                          "matched_count": 0})

    # 默认 mock _parse_day 走固定日期（避免依赖 BJT 时区）
    import web.routers.ai as ai_router
    monkeypatch.setattr(ai_router, "_parse_day",
                        lambda v: date(2026, 1, 1) if v is None else date.fromisoformat(v))

    return api.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


# ────────────────────────── /ai/status ──────────────────────────


def test_status_unauthenticated_returns_401(client):
    r = client.get("/api/v1/ai/status")
    assert r.status_code in (401, 403), r.text


def test_status_returns_ai_state(client):
    cookies = _login(client)
    r = client.get("/api/v1/ai/status", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["scores_count"] == 10


# ────────────────────────── /ai/details ──────────────────────────


def test_details_unauthenticated_returns_401(client):
    r = client.get("/api/v1/ai/details")
    assert r.status_code in (401, 403), r.text


def test_details_returns_items(client):
    cookies = _login(client)
    r = client.get("/api/v1/ai/details", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert "items" in body


# ────────────────────────── /ai/analyze ──────────────────────────


def test_analyze_unauthenticated_returns_401(client):
    r = client.get("/api/v1/ai/analyze")
    assert r.status_code in (401, 403), r.text


def test_analyze_invalid_date_returns_available_false(client):
    """date 非 ISO → available=False + invalid_date note。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/analyze?date=not-a-date", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["note"] == "invalid_date"
    assert "classes" in body  # 必有空壳结构


def test_analyze_file_not_exists_returns_available_false(client, tmp_path):
    """日期合法但文件不存在 → available=False + **中文**用户态 note（P2-A7）。

    ⚠️ 2026-10-23 契约订正：原断言是 ``"no analysis" in body["note"]``（英文文案）。
    P2-A7 已把后端 note 改成中文用户态说明（英文看不懂 + 原文案把内部接口路径
    ``POST /api/v1/jobs/ai-analyze`` 甩给终端用户）⇒ 这里改成断言**新契约**：
    ① 中文说明；② 有 ``reason="not_generated"``；③ note 里**不许**出现内部接口路径。
    """

    import web.services.ai_analyze as aa_mod
    with patch.object(aa_mod, "_output_path", return_value=tmp_path / "missing.json"):
        cookies = _login(client)
        r = client.get("/api/v1/ai/analyze?date=2026-01-01", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "not_generated"
    note = body["note"]
    assert "尚未生成" in note
    # 不许把内部 API 路径甩给终端用户
    assert "/api/" not in note
    assert "no analysis" not in note


def test_analyze_read_failure_returns_available_false(client, tmp_path):
    """JSON 解析失败 → available=False + **中文** note + reason=read_failed。

    ⚠️ 2026-10-23 契约订正：原文案是 ``f"read_failed: {e}"`` ⇒ 英文 + **可能带服务器
    绝对路径**（PermissionError / IsADirectoryError 的 str() 含完整路径）。
    ⇒ 现在 note 是中文用户态说明，异常细节只进服务端日志；``reason`` 承载机器可读原因。
    """
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    import web.services.ai_analyze as aa_mod
    with patch.object(aa_mod, "_output_path", return_value=bad):
        cookies = _login(client)
        r = client.get("/api/v1/ai/analyze?date=2026-01-01", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "read_failed"
    note = body["note"]
    assert "读取失败" in note
    # 绝对路径不许出现在用户可见文案里
    assert str(tmp_path) not in note
    assert "/" not in note


def test_analyze_valid_json_returns_full_response(client, tmp_path):
    """合法 JSON 文件 → available=True + classes / generated_at。"""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "date": "2026-01-01",
        "generated_at": "2026-01-01T12:00:00+08:00",
        "classes": {"per_match": [{"match": "A vs B", "analysis": "x"}],
                    "banker": "A", "lottery": []},
        "warning": [],
    }), encoding="utf-8")
    import web.services.ai_analyze as aa_mod
    with patch.object(aa_mod, "_output_path", return_value=good):
        cookies = _login(client)
        r = client.get("/api/v1/ai/analyze?date=2026-01-01", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["date"] == "2026-01-01"
    assert "per_match" in body["classes"]


def test_analyze_default_today_date(client, tmp_path):
    """date 缺省 → 走 _today_bjt()。"""
    import web.services.ai_analyze as aa_mod
    today = aa_mod._today_bjt().isoformat()
    today_path = tmp_path / f"{today}.json"
    today_path.write_text(json.dumps({"date": today, "classes": {}, "warning": []}),
                            encoding="utf-8")
    with patch.object(aa_mod, "_output_path", return_value=today_path):
        cookies = _login(client)
        r = client.get("/api/v1/ai/analyze", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    # 可能 available=True 或 False 取决于今天日期文件存在
    assert "available" in body


# ────────────────────────── /ai/daily ──────────────────────────


def test_daily_unauthenticated_returns_401(client):
    r = client.get("/api/v1/ai/daily")
    assert r.status_code in (401, 403), r.text


def test_daily_default_returns_ai_daily_report(client):
    """date 缺省 → ai_daily_report(today)。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/daily", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True


def test_daily_invalid_date_returns_200_with_degradation(client):
    """date 非法 → ValueError → 200 + available=False（不 500）。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/daily?date=bad", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "invalid_date"


# ────────────────────────── /ai/ranking ──────────────────────────


def test_ranking_unauthenticated_returns_401(client):
    r = client.get("/api/v1/ai/ranking")
    assert r.status_code in (401, 403), r.text


def test_ranking_default_limit_10(client):
    """limit 缺省 = 10（Query ge=1 le=50）。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/ranking", cookies=cookies)
    assert r.status_code == 200, r.text


def test_ranking_limit_above_50_returns_422(client):
    """limit > 50 → FastAPI 422。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/ranking?limit=100", cookies=cookies)
    assert r.status_code == 422, r.text


def test_ranking_limit_below_1_returns_422(client):
    """limit < 1 → FastAPI 422。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/ranking?limit=0", cookies=cookies)
    assert r.status_code == 422, r.text


def test_ranking_invalid_date_returns_degradation(client):
    """date 非法 → 200 + available=False（不 500）。"""
    cookies = _login(client)
    r = client.get("/api/v1/ai/ranking?date=bad&limit=10", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "invalid_date"
    assert body["matched_count"] == 0
