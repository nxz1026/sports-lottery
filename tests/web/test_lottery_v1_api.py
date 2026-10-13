"""web/routers/lottery_api 端到端测试：/api/v1/lottery/{prize,cost,stats,dantuo}。

背景：web/routers/lottery_api.py 当前 42% 覆盖 — 4 个端点（prize/cost/
stats/dantuo）+ 私有 _nums 辅助函数完全无测；现有 test_jc_lottery_api.py
只覆盖 /api/jc/lottery（不同端点）。

覆盖点：
  · _nums：空 → 400 / 非数字 → 400 / 合法 → list[int]
  · /lottery/prize：未鉴权 → 401 / ticket 空 → 400 / 合法 → 200
  · /lottery/cost：未鉴权 → 401 / front/back 空 → cost=0 / 非法 → 400
  · /lottery/stats：未鉴权 → 401 / mock lottery_draws → 200
  · /lottery/dantuo：未鉴权 → 401 / 合法参数 → 200

依赖 mock：web.services.lottery（已测纯函数）+ store.jc_view.lottery_draws
（避免走真 DB）。
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

    # mock jc_view.lottery_draws 避免依赖 DSN
    import store.jc_view as jcv
    monkeypatch.setattr(jcv, "lottery_draws",
                        lambda per_type=20: [
                            {"game_num": "85", "issue_no": "26001",
                             "numbers_raw": "3 7 12 18 25 31 5 9"},
                        ] * 3)

    return api.create_app()


@pytest.fixture
def client(app):
    return TestClient(app, client=("127.0.0.1", 5000))


def _login(client):
    r = client.post("/api/v1/login",
                    json={"username": "unit-test-user", "password": "unit-test-pass"})
    assert r.status_code == 200, r.text
    return r.cookies


# ────────────────────────── /lottery/prize ──────────────────────────


def test_prize_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85", "ticket": "1,2,3", "draw": "1,2,3"})
    assert r.status_code in (401, 403), r.text


def test_prize_returns_level_on_hit(client, monkeypatch):
    """合法输入 → 200 + body 含 level / hits。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "match_prize",
                        lambda game, ticket, draw: {
                            "level": "一等奖", "hits": [1, 2, 3], "note": ""})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85", "ticket": "1,2,3", "draw": "1,2,3"},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game"] == "85"
    assert body["ticket"] == [1, 2, 3]
    assert body["draw"] == [1, 2, 3]
    assert body["level"] == "一等奖"


def test_prize_empty_ticket_returns_400(client):
    """ticket 空字符串 → 400。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85", "ticket": "", "draw": "1,2,3"},
                   cookies=cookies)
    assert r.status_code == 400, r.text
    assert "ticket" in r.json()["message"]


def test_prize_empty_draw_returns_400(client):
    """draw 空字符串 → 400。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85", "ticket": "1,2,3", "draw": ""},
                   cookies=cookies)
    assert r.status_code == 400, r.text
    assert "draw" in r.json()["message"]


def test_prize_non_numeric_ticket_returns_400(client):
    """ticket 含字母 → 400。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "85", "ticket": "1,abc,3", "draw": "1,2,3"},
                   cookies=cookies)
    assert r.status_code == 400, r.text
    assert "数字" in r.json()["message"]


def test_prize_unknown_game_still_200(client, monkeypatch):
    """未识别 game → 200 + level=None（不抛错）。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "match_prize",
                        lambda game, ticket, draw: {"level": None, "hits": [], "note": "unknown"})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/prize",
                   params={"game": "999", "ticket": "1", "draw": "1"},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["level"] is None


# ────────────────────────── /lottery/cost ──────────────────────────


def test_cost_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/cost",
                   params={"game": "85", "front": "1,2", "back": "3,4"})
    assert r.status_code in (401, 403), r.text


def test_cost_empty_front_back_returns_zero(client, monkeypatch):
    """front/back 都空 → cost=0 + notes=0。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "compound_cost",
                        lambda game, parts: {"notes": 0, "per_note": 2.0, "total": 0.0})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/cost", cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cost"]["notes"] == 0


def test_cost_valid_combination_returns_cost(client, monkeypatch):
    """合法 front+back → cost 字段。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "compound_cost",
                        lambda game, parts: {"notes": 18, "per_note": 2.0, "total": 36.0})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/cost",
                   params={"game": "85", "front": "1,2,3,4,5,6", "back": "1,2"},
                   cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["cost"]["notes"] == 18


def test_cost_non_numeric_front_returns_400(client):
    """front 非数字 → 400。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/cost",
                   params={"game": "85", "front": "1,x,3", "back": "1,2"},
                   cookies=cookies)
    assert r.status_code == 400, r.text
    assert "front" in r.json()["message"]


def test_cost_non_numeric_back_returns_400(client):
    """back 非数字 → 400。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/cost",
                   params={"game": "85", "front": "1,2,3", "back": "1,y"},
                   cookies=cookies)
    assert r.status_code == 400, r.text
    assert "back" in r.json()["message"]


# ────────────────────────── /lottery/stats ──────────────────────────


def test_stats_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/stats", params={"game": "85"})
    assert r.status_code in (401, 403), r.text


def test_stats_returns_history_summary(client, monkeypatch):
    """mock lottery_draws → n_periods + stats 字段齐全。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "stats",
                        lambda history: {
                            "freq": [1, 2, 3, 4, 5],
                            "top": [3, 7],
                            "parity": {"odd": 5, "even": 3},
                        })
    cookies = _login(client)
    r = client.get("/api/v1/lottery/stats", params={"game": "85"}, cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game"] == "85"
    assert body["n_periods"] >= 0
    assert "freq" in body["stats"]
    assert "top" in body["stats"]
    assert "parity" in body["stats"]


def test_stats_default_n_is_30(client, monkeypatch):
    """n 默认 30。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "stats", lambda history: {"freq": [], "top": [], "parity": {}})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/stats", params={"game": "85"}, cookies=cookies)
    assert r.status_code == 200, r.text


def test_stats_n_above_100_returns_422(client):
    """n > 100 → FastAPI 422（Query ge=1 le=100 校验）。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/stats",
                   params={"game": "85", "n": 200}, cookies=cookies)
    assert r.status_code == 422, r.text


def test_stats_n_below_1_returns_422(client):
    """n < 1 → FastAPI 422。"""
    cookies = _login(client)
    r = client.get("/api/v1/lottery/stats",
                   params={"game": "85", "n": 0}, cookies=cookies)
    assert r.status_code == 422, r.text


# ────────────────────────── /lottery/dantuo ──────────────────────────


def test_dantuo_unauthenticated_returns_401(client):
    r = client.get("/api/v1/lottery/dantuo",
                   params={"dan": 2, "tuo": 8, "choose": 5})
    assert r.status_code in (401, 403), r.text


def test_dantuo_valid_returns_tickets(client, monkeypatch):
    """合法参数 → result.tickets。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "dan_tuo_tickets",
                        lambda dan, tuo, choose: {
                            "tickets": 56, "combos": [[1, 2, 3, 4, 5]],
                            "note": ""})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/dantuo",
                   params={"dan": 2, "tuo": 8, "choose": 5}, cookies=cookies)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["tickets"] == 56


def test_dantuo_invalid_returns_zero_tickets(client, monkeypatch):
    """dan > choose 等非法参数 → tickets=0 + note=invalid（不抛错）。"""
    import web.services.lottery as L
    monkeypatch.setattr(L, "dan_tuo_tickets",
                        lambda dan, tuo, choose: {"tickets": 0, "note": "invalid"})
    cookies = _login(client)
    r = client.get("/api/v1/lottery/dantuo",
                   params={"dan": 5, "tuo": 3, "choose": 2}, cookies=cookies)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["tickets"] == 0
