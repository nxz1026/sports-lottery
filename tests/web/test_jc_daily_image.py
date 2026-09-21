"""test_jc_daily_image.py：/api/jc/daily-image 端点契约。

- Step 1：五大联赛周一无在售时 available=False + 明确 note（与 NBA 休赛对称）。
- Step 2：rows 分组 main_rows / temp_rows，stats 带 main_/temp_ 子计数；临时赛事仅盘口层。
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


# ---------- Step 1：available=False 分支（五大联赛无在售）----------

def test_football_no_matches_returns_available_false(monkeypatch, client):
    """五大联赛今日无在售：rows=[] 时必须 available=False（与 NBA 休赛对称）。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [])
    monkeypatch.setattr("web.services.team_match.build_rows", lambda fs, pl: [])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is False
    assert d["rows"] == []
    assert d["main_rows"] == []
    assert d["temp_rows"] == []
    assert "无在售" in d["note"]
    assert d["stats"]["main_on_sale"] == 0
    assert d["stats"]["main_recommend"] == 0
    assert d["stats"]["temp_on_sale"] == 0


def test_football_with_only_big5_returns_available_true(monkeypatch, client):
    """只有五大联赛：rows=main_rows，temp_rows=[], available=True。"""
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": "1", "play_type": "had"}])
    monkeypatch.setattr("web.services.team_match.build_rows",
                        lambda fs, pl: [{"league_cn": "英格兰超级联赛", "matched": True,
                                         "home_cn": "A", "away_cn": "B"}])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 19))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is True
    assert len(d["rows"]) == 1
    assert len(d["main_rows"]) == 1
    assert d["temp_rows"] == []
    assert d["stats"]["main_on_sale"] == 1
    assert d["stats"]["main_recommend"] == 1
    assert d["stats"]["temp_on_sale"] == 0


# ---------- Step 2：临时赛事分组 ----------

def test_temp_league_only_main_empty_note_with_temp_count(monkeypatch, client):
    """五大联赛空 + 临时赛事有：available=True + note 提示 + 分组正确。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会女足")
    import web.config as config
    import web.session_store, web.auth
    for m in (config, web.session_store, web.auth):
        importlib.reload(m)
    from web.routers import jc as jc_router2
    importlib.reload(jc_router2)

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": "1", "play_type": "had"}])
    monkeypatch.setattr("web.services.team_match.build_rows",
                        lambda fs, pl: [{"league_cn": "亚运会女足", "matched": False,
                                         "home_cn": "中国女足", "away_cn": "菲律宾女足"}])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is True
    assert d["main_rows"] == []
    assert len(d["temp_rows"]) == 1
    assert d["temp_rows"][0]["league_cn"] == "亚运会女足"
    assert d["stats"]["main_on_sale"] == 0
    assert d["stats"]["temp_on_sale"] == 1
    # note 必须提示五大联赛空 + 临时赛事数量
    assert d["note"] is not None
    assert "五大联赛无在售" in d["note"]
    assert "1 场临时赛事" in d["note"]
    assert "亚运会女足" in d["note"]


def test_temp_and_big5_split_into_groups(monkeypatch, client):
    """五大 + 临时同在：rows 是 main+temp 拼接、但 main_rows / temp_rows 已分组。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会女足,亚运会男足")
    import web.config as config
    import web.session_store, web.auth
    for m in (config, web.session_store, web.auth):
        importlib.reload(m)
    from web.routers import jc as jc_router3
    importlib.reload(jc_router3)

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [{"match_id": str(i), "play_type": "had"} for i in range(3)])
    monkeypatch.setattr("web.services.team_match.build_rows",
                        lambda fs, pl: [
                            {"league_cn": "英格兰超级联赛", "matched": True, "home_cn": "A", "away_cn": "B"},
                            {"league_cn": "亚运会女足", "matched": False, "home_cn": "X", "away_cn": "Y"},
                            {"league_cn": "亚运会男足", "matched": False, "home_cn": "P", "away_cn": "Q"},
                        ])
    monkeypatch.setattr("web.services.store.bjt_today", lambda: date(2026, 9, 21))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    assert d["available"] is True
    # 三大联赛 + 2 临时赛事都进 rows（兜底兼容旧前端）
    assert len(d["rows"]) == 3
    # main / temp 分组
    assert len(d["main_rows"]) == 1
    assert d["main_rows"][0]["league_cn"] == "英格兰超级联赛"
    assert len(d["temp_rows"]) == 2
    temp_lgs = {r["league_cn"] for r in d["temp_rows"]}
    assert temp_lgs == {"亚运会女足", "亚运会男足"}
    # note 应为 None（五大联赛有赛事，正常展示）
    assert d["note"] is None
    # 临时赛事无 matched，所以 main_recommend=1 temp_recommend 字段不在 stats（只 main）
    assert d["stats"]["main_recommend"] == 1
    assert d["stats"]["temp_on_sale"] == 2


# ---------- NBA 回归 ----------

def test_basketball_off_season_returns_available_false(monkeypatch, client):
    """NBA 休赛期：已有 available=False 逻辑不变（回归测试）。"""
    monkeypatch.setattr("web.services.team_match.nba_rows", lambda preds: [])
    monkeypatch.setattr("web.routers.jc._nba_available", lambda preds, today: False)
    _login(client)
    d = client.get("/api/jc/daily-image?sport=basketball").json()
    assert d["available"] is False
    assert "NBA" in d["note"]
