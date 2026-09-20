"""test_clv_baseline.py：CLV 基线纯函数（_devig_home / _clv）。"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from clv_baseline import _clv, _devig_home  # noqa: E402


def test_devig_home_valid():
    # 1.8 / 2.05 → 主胜 0.5325
    p = _devig_home(1.8, 2.05)
    assert p is not None
    assert p == pytest.approx(0.5325, abs=1e-4)


def test_devig_home_invalid():
    assert _devig_home(None, 2.0) is None
    assert _devig_home(1.0, 2.0) is None  # 非 >1
    assert _devig_home("x", 2.0) is None


def test_clv_positive_when_model_likes_more():
    # p_model > p_close ⇒ CLV > 0
    assert _clv(0.6, 0.4) == pytest.approx(math.log(1.5), abs=1e-9)
    assert _clv(0.3, 0.5) == pytest.approx(math.log(0.6), abs=1e-9)  # 负


def test_clv_boundary_rejected():
    assert _clv(1.0, 0.5) is None  # p_model=1 边界拒绝
    assert _clv(0.0, 0.5) is None
    assert _clv(0.5, 1.0) is None
    assert _clv(0.5, None) is None
    assert _clv("x", 0.5) is None


def test_clv_generates_report_shape_with_fake_games(monkeypatch):
    """不联网：monkeypatch fetch 为假盘口 + load_ratings 空 → run 产出报告结构。"""
    import clv_baseline as mod

    fake_games = [
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics",
         "commence_time": "2026-10-20T19:00:00Z",
         "bookmakers": [{"key": "x", "markets": [
             {"key": "h2h", "outcomes": [{"name": "Atlanta Hawks", "price": 2.0},
                                          {"name": "Boston Celtics", "price": 1.8}]}]}]},
        {"home_team": "Chicago Bulls", "away_team": "Detroit Pistons",
         "commence_time": "2026-10-20T20:00:00Z",
         "bookmakers": [{"key": "x", "markets": [
             {"key": "h2h", "outcomes": [{"name": "Chicago Bulls", "price": 1.9},
                                          {"name": "Detroit Pistons", "price": 1.9}]}]}]},
    ]

    class FakePred:
        def __init__(self, prob):
            self.p = prob
        def get(self, k, d=None):
            return self.p if k == "win_prob" else d

    monkeypatch.setattr(mod, "fetch_odds_games", lambda *a, **k: fake_games)
    monkeypatch.setattr(mod, "load_ratings", lambda: {})
    monkeypatch.setattr(mod, "predict_game",
                        lambda h, a, r, s, t, o, x: FakePred(0.6))

    monkeypatch.setenv("ODDS_API_KEY", "test")
    rep = mod.run(ahead_days=5)
    assert rep["games_with_market"] == 2
    assert rep["games_with_model_and_market"] == 2
    assert rep["valid_clv"] == 2
    assert rep["mean_clv"] is not None
    assert rep["positive_clv_pct"] == 100.0
    assert len(rep["rows"]) == 2