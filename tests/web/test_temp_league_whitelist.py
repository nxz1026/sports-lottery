"""test_temp_league_whitelist.py：env LEAGUE_TEMP_LEAGUES + _big5_with_temp() helper。

Step 1 临时赛事白名单：硬编码五大联赛 + env CSV 附加层，端点过滤统一使用 helper。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.config import parse_league_temp  # noqa: E402


# ---------- parse_league_temp 单元测试 ----------

def test_parse_empty_returns_empty_tuple():
    assert parse_league_temp("") == ()
    assert parse_league_temp("   ") == ()


def test_parse_single():
    assert parse_league_temp("亚运会男足") == ("亚运会男足",)


def test_parse_csv_with_spaces_and_dedup():
    assert parse_league_temp("  亚运会男足 , 欧罗巴联赛 ,亚运会男足") == (
        "亚运会男足", "欧罗巴联赛",
    )


def test_parse_skips_empty_tokens():
    assert parse_league_temp(",,亚运,,,  ,欧联,") == ("亚运", "欧联")


# ---------- _big5_with_temp() 集成测试（re-import config + jc router）----------

def test_helper_includes_hardcoded_big5_when_env_empty(monkeypatch):
    """env 未配临时联赛时，helper 仅含硬编码五大联赛。
    注意：web.config 重载会 load_dotenv() 把 .env 重新读入 os.environ；
    所以设 "" (而非 delenv) 才能让 reload 后 LEAGUE_TEMP_LEAGUES 真的为空。
    """
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "")
    import web.config as config
    importlib.reload(config)
    from web.routers import jc as jc_router
    importlib.reload(jc_router)
    s = jc_router._big5_with_temp()
    assert "英格兰超级联赛" in s
    assert "西班牙甲级联赛" in s
    assert len(s) == 5  # 仅硬编码五大


def test_helper_unions_env_into_big5(monkeypatch):
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会男足, 亚运会女足 ,欧罗巴联赛")
    import web.config as config
    importlib.reload(config)
    from web.routers import jc as jc_router
    importlib.reload(jc_router)
    s = jc_router._big5_with_temp()
    # 硬编码五大联赛 + 3 个临时 = 8
    assert len(s) == 8
    assert "亚运会男足" in s and "亚运会女足" in s and "欧罗巴联赛" in s
    assert "英格兰超级联赛" in s  # 五大仍在


def test_helper_no_duplicate_when_env_repeats_big5(monkeypatch):
    """临时赛事里重复五大联赛名 → set 去重，长度仍为 5。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "英格兰超级联赛,亚运会男足,英格兰超级联赛")
    import web.config as config
    importlib.reload(config)
    from web.routers import jc as jc_router
    importlib.reload(jc_router)
    s = jc_router._big5_with_temp()
    assert len(s) == 6  # 5 五大 + 1 亚运（去重后）
    assert "亚运会男足" in s


def test_env_summary_exposes_league_temp(monkeypatch):
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会男足,亚运会女足")
    import web.config as config
    importlib.reload(config)
    s = config.env_summary()
    assert s["league_temp_leagues"] == ["亚运会男足", "亚运会女足"]


# ---------- 端点过滤：daily-image 临时赛事应进入 rows（main/temp 分组在 Step 2）----------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", "/tmp/jctemp-sessions.db")
    monkeypatch.setenv("AUTH_USERNAME", "unit-temp")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-pass")
    import web.config as config
    import web.session_store
    import web.auth
    for m in (config, web.session_store, web.auth):
        importlib.reload(m)
    from web.api import create_app
    from fastapi.testclient import TestClient
    return TestClient(create_app())


def _login(c):
    r = c.post("/api/v1/login", json={"username": "unit-temp", "password": "unit-pass"})
    assert r.status_code == 200


def test_daily_image_filter_includes_temp_league(monkeypatch, client):
    """env 配 LEAGUE_TEMP_LEAGUES=亚运会女足 → daily-image 过滤后应保留 1 行亚运会女足。"""
    monkeypatch.setenv("LEAGUE_TEMP_LEAGUES", "亚运会女足")
    import web.config as config
    import web.session_store, web.auth
    for m in (config, web.session_store, web.auth):
        importlib.reload(m)
    from web.routers import jc as jc_router
    importlib.reload(jc_router)

    fake_fixtures = [
        {"match_id": "1", "play_type": "had"},
        {"match_id": "2", "play_type": "had"},
    ]
    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: fake_fixtures)
    def fake_build(fs, pl):
        return [
            {"league_cn": "英格兰超级联赛", "matched": False},
            {"league_cn": "亚运会女足", "matched": False},
        ]
    monkeypatch.setattr("web.services.team_match.build_rows", fake_build)
    from datetime import date as _date
    monkeypatch.setattr("web.services.store.bjt_today", lambda: _date(2026, 9, 21))

    _login(client)
    d = client.get("/api/jc/daily-image?sport=football").json()
    # Step 1 仅扩白名单，过滤后 rows 应包含亚运会女足
    assert d["available"] is True
    leagues = [r["league_cn"] for r in d["rows"]]
    assert "亚运会女足" in leagues
    assert "英格兰超级联赛" in leagues
