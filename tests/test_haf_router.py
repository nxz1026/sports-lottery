from __future__ import annotations

import pytest

from web.routers import haf as haf_router


def _fake_pred():
    return {
        "home": "佛罗伦萨", "away": "那不勒斯",
        "lambda_home": 1.09, "lambda_away": 0.99,
    }


def test_haf_calc_found(monkeypatch):
    monkeypatch.setattr(haf_router, "_find_prediction", lambda h, a: _fake_pred())
    r = haf_router._haf_calc("佛罗伦萨", "那不勒斯", 0)
    assert r["available"] is True
    assert set(r["probs"]) >= {"胜胜", "平平", "负负"}
    assert r["pick"] in r["probs"]
    assert abs(sum(r["probs"].values()) - 1.0) < 1e-3


def test_haf_calc_not_found(monkeypatch):
    monkeypatch.setattr(haf_router, "_find_prediction", lambda h, a: None)
    r = haf_router._haf_calc("X", "Y", 0)
    assert r["available"] is False
    assert r["reason"] == "no_prediction"


def test_haf_calc_bad_lambda(monkeypatch):
    bad = {"home": "A", "away": "B", "lambda_home": None, "lambda_away": 1.0}
    monkeypatch.setattr(haf_router, "_find_prediction", lambda h, a: bad)
    r = haf_router._haf_calc("A", "B", 0)
    assert r["available"] is False
    assert r["reason"] == "bad_lambda"


def test_resolve_match_line_validation():
    with pytest.raises(Exception):
        haf_router._resolve_match({"home": "A", "away": "B", "line": 99})
    with pytest.raises(Exception):
        haf_router._resolve_match({"home": "A", "away": "B", "line": "x"})
    assert haf_router._resolve_match({"home": "A", "away": "B"}) == ("A", "B", 0.0)