"""test_jc_daily_image.py：/api/jc/daily-image 端点契约（football/basketball + 无赛事分支）。

修复：2026-09-21 五大联赛周一无在售赛事时，端点之前返回 available=True rows=[]，
前端画布仅 404px 高，体验如"空白"。与 NBA 休赛处理对称：rows=[] 时返回
available=False + 明确 note，前端 dailyimage.html 已有空态卡片。
"""
from __future__ import annotations

import importlib
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import jc as jc_router  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", "/tmp/jcdaily-sessions.db")
    monkeypatch.setenv("AUTH_USERNAME", "unit-jc")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-pass")
    import web.config as config
    import web.session_store
    import web.auth
    for m in (config, web.session_store, web.auth):
        importlib.reload(m)
    from web.api import create_app
    return TestClient(create_app())


def _login(c):
    r = c.post("/api/v1/login", json={"username": "unit-jc", "password": "unit-pass"})
    assert r.status_code == 200


def test_football_no_matches_returns_available_false(monkeypatch, client):
    """五大联赛今日无在售：rows=[] 时必须 available=False（与 NBA 休赛对称）。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [])
    monkeypatch.setattr("web.services.team_match.build_rows", lambda fs, pl: [])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is False
    assert d["rows"] == []
    assert "无在售" in d["note"]
    assert d["stats"]["on_sale"] == 0
    assert d["stats"]["recommend"] == 0


def test_football_with_matches_returns_available_true(monkeypatch, client):
    """有赛事：rows 非空 + available=True + on_sale/match 数对得上。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": "1", "play_type": "had"}])
    # build_rows 输出的 league_cn 是全名（_BIG5 用的是官方全名，如"英格兰超级联赛"）
    monkeypatch.setattr("web.services.team_match.build_rows",
                        lambda fs, pl: [{"league_cn": "英格兰超级联赛", "matched": True,
                                         "home_cn": "A", "away_cn": "B"}])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 19))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is True
    assert len(d["rows"]) == 1
    assert d["stats"]["on_sale"] == 1
    assert d["stats"]["recommend"] == 1


def test_basketball_off_season_returns_available_false(monkeypatch, client):
    """NBA 休赛期：已有 available=False 逻辑不变（回归测试）。"""
    monkeypatch.setattr("web.services.team_match.nba_rows", lambda preds: [])
    monkeypatch.setattr("web.routers.jc._nba_available", lambda preds, today: False)
    _login(client)
    d = client.get("/api/jc/daily-image?sport=basketball").json()
    assert d["available"] is False
    assert "NBA" in d["note"]