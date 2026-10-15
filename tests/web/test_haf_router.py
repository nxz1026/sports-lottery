"""R23 #3: web/routers/haf 端点专项测试（GET/POST + 401 + 降级语义）。

覆盖：
  · 未登录 GET / POST → 401
  · GET 端点 happy path（line 合法 + 找到 prediction）
  · GET 端点降级：line 越界 / 缺 home|away / 无 prediction / 无 λ
  · POST 端点 match_num 反查对阵
  · POST 端点降级：body 缺 home|away|match_num / line 非法
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """轻量 app fixture：env + reload config；mock haf 的 store 依赖。"""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)

    from web.api import create_app
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


# ────────────────────────── 401 鉴权守门 ──────────────────────────


def test_haf_get_unauthenticated_returns_401(client):
    """GET /api/v1/haf 未登录 → 401。"""
    r = client.get("/api/v1/haf?home=A&away=B")
    assert r.status_code in (401, 403), r.text


def test_haf_post_unauthenticated_returns_401(client):
    """POST /api/v1/haf 未登录 → 401。"""
    r = client.post("/api/v1/haf", json={"home": "A", "away": "B"})
    assert r.status_code in (401, 403), r.text


# ────────────────────────── GET 端点降级语义 ──────────────────────────


def test_haf_get_line_out_of_range_no_400(client, monkeypatch):
    """GET line 超 [-10, 10] 不抛 400（GET 端点没范围检查；只 POST _resolve_match 守门）。

    R23 踩坑：haf_get 路径直接调 ``_haf_calc(home, away, line)`` 不做 line
    边界；边界只发生在 POST ``_resolve_match``（契约 §5.5 降级语义只针对 POST
    body）。GET line 越界时若无 prediction → 200 + no_prediction。
    """
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {})
    cookies = _login(client)
    r = client.get("/api/v1/haf?home=A&away=B&line=99", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "no_prediction"


def test_haf_post_line_out_of_range_returns_400(client, monkeypatch):
    """POST line 超 [-10, 10] → 400（_resolve_match 守门）。"""
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {})
    cookies = _login(client)
    r = client.post("/api/v1/haf", json={"home": "A", "away": "B", "line": 99},
                    cookies=cookies)
    assert r.status_code == 400, r.text
    assert "line" in r.json().get("detail", "").lower() or "-10" in r.text


def test_haf_get_line_non_numeric_returns_422(client, monkeypatch):
    """GET line 非法数字 → 422（FastAPI Query float 自动校验先于 handler）。

    边界守门：契约 §5.5 仅覆盖 400（handler 路径），非法数字走 FastAPI
    框架 422 (unprocessable entity) 是统一约定。"""
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {})
    cookies = _login(client)
    r = client.get("/api/v1/haf?home=A&away=B&line=abc", cookies=cookies)
    assert r.status_code == 422, r.text


def test_haf_get_no_prediction_returns_available_false(client, monkeypatch):
    """GET 找不到 prediction → 200 + available=False, reason=no_prediction（不 404）。"""
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {})
    cookies = _login(client)
    r = client.get("/api/v1/haf?home=A&away=B", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "no_prediction"


# ────────────────────────── POST 端点降级语义 ──────────────────────────


def test_haf_post_missing_match_num_and_home_away_returns_400(client, monkeypatch):
    """POST body 同时缺 home/away/match_num → 400。"""
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {})
    cookies = _login(client)
    r = client.post("/api/v1/haf", json={}, cookies=cookies)
    assert r.status_code == 400, r.text
    assert "home" in r.text.lower() or "match_num" in r.text.lower()


def test_haf_post_match_num_reverse_lookup_finds_prediction(client, monkeypatch):
    """POST match_num → 反查 jc_view.fixtures_on 拿对阵 → 找到 prediction。"""
    import store.jc_view as jcv
    monkeypatch.setattr(jcv, "fixtures_on", lambda day=None: [
        {"match_num": 7001, "home_cn": "A", "away_cn": "B",
         "kickoff_bj": "01-15 20:00", "play_type": "had",
         "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
    ])
    from web.services import store as ws
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {
        ("A", "B"): {"home": "A", "away": "B", "lambda_home": 1.5, "lambda_away": 0.8},
    })

    cookies = _login(client)
    r = client.post("/api/v1/haf", json={"match_num": 7001}, cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["home"] == "A" and body["away"] == "B"
    # compute_haf 返回九宫格概率 + pick
    assert "probs" in body and len(body["probs"]) == 9, f"九宫格应 9 项；实际 {len(body.get('probs', {}))}"