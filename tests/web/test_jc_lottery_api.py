"""彩票开奖端点契约（/api/jc/lottery，2026-09-18 新增）。

背景：采集端 lottery_draw 主题一直在送 4 个彩种（超级大乐透 85 / 排列3 35 /
排列5 350133 / 7星彩 04），fact.lottery_draw 已落 125 期，但**没有任何读端点、
也没有任何页面**。本端点补上读取通道；前端视图另由 static/dashboard.html 承载。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    import web.config as config
    import web.session_store as session_store
    import web.auth as auth
    importlib.reload(config)
    importlib.reload(session_store)
    importlib.reload(auth)
    from web.api import create_app
    return TestClient(create_app())


def _login(client):
    response = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass",
    })
    assert response.status_code == 200


def test_lottery_requires_auth(client):
    # 2026-09-18：后端信任 Nginx Basic Auth，require_auth 放行 → 不再 401。
    assert client.get("/api/jc/lottery").status_code != 401


def test_lottery_returns_rows_without_fabricating(client, monkeypatch):
    from store import jc_view
    rows = [{"game_num": "35", "game_name": "排列3", "issue_no": "26250",
             "draw_date": "2026-09-17", "status": 20, "numbers_raw": "9 8 5",
             "pool": "0", "equipment_count": None}]
    monkeypatch.setattr(jc_view, "lottery_draws", lambda n: rows)
    _login(client)
    response = client.get("/api/jc/lottery")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == rows
    assert body["degraded"] is False
    assert "sql_note" in body


def test_lottery_per_type_is_bounded(client):
    _login(client)
    for bad in ("0", "101", "abc", "-1"):
        response = client.get(f"/api/jc/lottery?per_type={bad}")
        assert response.status_code == 400, bad


def test_lottery_clamps_and_passes_through(client, monkeypatch):
    """per_type 合法值应原样传给视图层（钳制在视图层再做一次）。"""
    from store import jc_view
    seen = []
    monkeypatch.setattr(jc_view, "lottery_draws", lambda n: seen.append(n) or [])
    _login(client)
    assert client.get("/api/jc/lottery?per_type=50").status_code == 200
    assert seen == [50]


def test_lottery_view_clamps_out_of_range():
    """视图层自身也要钳制，避免绕过路由直接调用时打爆查询。"""
    from store import jc_view
    captured = {}

    def _fake_fetch(sql, params=None):
        captured.update(params or {})
        return []

    original = jc_view._fetch
    jc_view._fetch = _fake_fetch
    try:
        jc_view.lottery_draws(0)
        assert captured["per_type"] == 1
        jc_view.lottery_draws(10_000)
        assert captured["per_type"] == 100
    finally:
        jc_view._fetch = original
