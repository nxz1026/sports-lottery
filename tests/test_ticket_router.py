"""test_ticket_router.py：/api/v1/ticket/sfc 路由（TestClient + importlib.reload 处理 auth + monkeypatch store）。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import ticket as ticket_router  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", "/tmp/ticket-sessions.db")
    monkeypatch.setenv("AUTH_USERNAME", "unit-ticket")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-pass")
    import web.config as config
    import web.session_store
    import web.auth
    importlib.reload(config)
    importlib.reload(web.session_store)
    importlib.reload(web.auth)

    # 假 jc_view.issue_matches：14 场；奇偶场主客交错，保证每场都能匹配到预测
    fake_matches = [{
        "game_num": "90", "issue_no": "26T01", "seq": i + 1, "gm_match_id": 100 + i,
        "league_cn": "英超", "home_cn": (f"主{i}" if i % 2 == 0 else f"客{i}"),
        "away_cn": (f"客{i}" if i % 2 == 0 else f"主{i}"),
        "start_date": None, "is_drawn": (i == 0), "cz_score": None, "official_result": None,
    } for i in range(14)]
    monkeypatch.setattr("store.jc_view.issue_matches", lambda g, i: fake_matches)

    # 每场都有预测 entry
    entries = [{"home": f"主{i}", "away": f"客{i}",
                "reasoning_factors": {"home_ml_true_prob": 0.6 if i % 2 == 0 else 0.2,
                                      "draw_true_prob": 0.3,
                                      "away_ml_true_prob": 0.1 if i % 2 == 0 else 0.5}}
               for i in range(14)]
    monkeypatch.setattr(ticket_router, "_all_pred_entries", lambda: entries)

    from web.api import create_app
    from fastapi.testclient import TestClient
    return TestClient(create_app())


def _login(c):
    r = c.post("/api/v1/login", json={"username": "unit-ticket", "password": "unit-pass"})
    assert r.status_code == 200


def test_ticket_route_returns_picks_and_renjiu(client):
    _login(client)
    r = client.post("/api/v1/ticket/sfc", json={"choose": 9, "issue_no": "26T01"})
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["n_matches"] == 14
    assert d["n_with_probs"] == 14
    ren = d.get("renjiu")
    assert ren is not None
    assert ren["choose"] == 9
    assert ren["combinations"] == 2002
    assert len(ren["picked_seqs"]) == 9
    assert d["drawn_count"] == 1
    # 每场都给了 side（主胜或客胜）
    assert all(m["side"] in ("主胜", "客胜") for m in d["matches"])
    # 奇偶场三向和 ≈1（choose_top 用 prob 不能错乱归一）
    for m in d["matches"]:
        assert abs(sum(m["probs"].values()) - 1.0) < 1e-6


def test_ticket_route_get_and_post(client):
    _login(client)
    r = client.get("/api/v1/ticket/sfc", params={"choose": 9, "issue_no": "26T01"})
    assert r.status_code == 200
    assert r.json()["available"] is True


def test_ticket_route_choose_invalid(client):
    _login(client)
    assert client.post("/api/v1/ticket/sfc", json={"choose": 99}).status_code == 400
    assert client.post("/api/v1/ticket/sfc", json={"choose": 0}).status_code == 400


def test_model_three_ref_and_ml_fallback():
    assert ticket_router._model_three({"reasoning_factors": {}, "ml_proba": [0.6, 0.3, 0.1]}) \
        == {"主胜": 0.6, "平": 0.3, "客胜": 0.1}


def test_model_three_missing_and_bad():
    assert ticket_router._model_three({"reasoning_factors": {}, "ml_proba": []}) is None
    e = {"reasoning_factors": {"home_ml_true_prob": None, "draw_true_prob": None, "away_ml_true_prob": None},
         "ml_proba": []}
    assert ticket_router._model_three(e) is None


def test_no_matches_empty_degraded(monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", "/tmp/ticket2.db")
    monkeypatch.setenv("AUTH_USERNAME", "unit-ticket2")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-pass2")
    import web.config as config
    import web.session_store
    import web.auth
    importlib.reload(config)
    importlib.reload(web.session_store)
    importlib.reload(web.auth)
    monkeypatch.setattr("store.jc_view.issue_matches", lambda g, i: [])
    monkeypatch.setattr(ticket_router, "_all_pred_entries", lambda: [])
    from web.api import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.post("/api/v1/login", json={"username": "unit-ticket2", "password": "unit-pass2"})
    d = c.post("/api/v1/ticket/sfc", json={"choose": 9}).json()
    assert d["available"] is False
    assert d["reason"] == "no_issue_matches"