"""P2-11 镜像对账：clv_baseline._clv / _devig_home vs market.clv.clv /
market.devig.devig（同一公式，scripts 内部去重后）。

clv_baseline 是 lenient 包装（非法 → None），market.clv / market.devig 是
strict 实现（非法 → ValueError）。本测试断言：

1. 有效输入：clv_baseline 与 market.* 在数学上完全相等
2. 非法输入：clv_baseline 返 None，market.* 抛 ValueError（语义对称）
3. 行级：clv_baseline.run 产出的 CLV / p_close 与直接调 market.* 算出来逐字段相等

clv_baseline 的实现现已经委托给 market.*，本测试守住"委托链数学一致"；
若任一端改了 log / devig 公式（忘记同步），CI 即刻捕获。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# 典型 NBA h2h 赔率（双方向去水用）
_ODDS_FIXTURES = [
    # (label, home, away, expected_p_home)
    ("均衡 2.0/2.0", 2.0, 2.0, 0.5),
    ("主胜 1.8/2.05", 1.8, 2.05, 0.5325),
    # (1/1.95) / (1/1.95 + 1/1.85) = 0.51282 / 1.05332 = 0.48684
    ("客胜 1.95/1.85", 1.95, 1.85, 0.48684),
    # (1/1.1) / (1/1.1 + 1/8.0) = 0.90909 / 1.03409 = 0.87907
    ("低赔主胜 1.1/8.0", 1.1, 8.0, 0.87907),
]


def test_devig_home_matches_market_devig_when_valid():
    """有效赔率：clv_baseline._devig_home 与 market.devig.devig 数值一致。"""
    from clv_baseline import _devig_home
    from market.devig import devig

    for label, h, a, expected in _ODDS_FIXTURES:
        baseline = _devig_home(h, a)
        scripts = devig({"home": h, "away": a})["home"]
        assert baseline == pytest.approx(scripts, abs=1e-9), (
            f"{label}: baseline={baseline} scripts={scripts}"
        )
        # 与手算期望也对上
        assert baseline == pytest.approx(expected, abs=1e-4), (
            f"{label}: expected={expected} got={baseline}"
        )


def test_devig_home_none_matches_market_devig_valueerror():
    """非法赔率：clv_baseline 返 None，market.devig 抛 ValueError。"""
    from clv_baseline import _devig_home
    from market.devig import devig

    # odds <= 1
    assert _devig_home(1.0, 2.0) is None
    with pytest.raises(ValueError):
        devig({"home": 1.0, "away": 2.0})

    # odds None / 非数
    assert _devig_home(None, 2.0) is None
    with pytest.raises((ValueError, TypeError)):
        devig({"home": None, "away": 2.0})

    # odds 字符串
    assert _devig_home("x", 2.0) is None
    with pytest.raises((ValueError, TypeError)):
        devig({"home": "x", "away": 2.0})


def test_clv_matches_market_clv_when_valid():
    """有效 (0,1) 开区间：clv_baseline._clv 与 market.clv.clv 数学一致。"""
    from clv_baseline import _clv
    from market.clv import clv

    pairs = [
        (0.6, 0.4),
        (0.3, 0.5),
        (0.55, 0.45),
        (0.7, 0.3),
        (0.5, 0.5),  # 平
    ]
    for pm, pc in pairs:
        baseline = _clv(pm, pc)
        scripts = clv(pm, pc)
        assert baseline == pytest.approx(scripts, abs=1e-12), (
            f"({pm},{pc}): baseline={baseline} scripts={scripts}"
        )


def test_clv_boundary_zero_or_one_returns_none():
    """边界 0 / 1：clv_baseline 返 None（开区间语义守住）。"""
    from clv_baseline import _clv

    assert _clv(1.0, 0.5) is None
    assert _clv(0.5, 1.0) is None
    assert _clv(0.0, 0.5) is None
    assert _clv(0.5, 0.0) is None


def test_clv_non_numeric_returns_none():
    """非数值 / None：clv_baseline 返 None（market.clv 抛 ValueError / TypeError）。"""
    from clv_baseline import _clv
    from market.clv import clv

    assert _clv("x", 0.5) is None
    assert _clv(0.5, None) is None
    assert _clv(None, None) is None
    # market.clv 抛同类异常（语义对称）
    with pytest.raises((ValueError, TypeError)):
        clv("x", 0.5)


def test_run_report_clv_values_consistent_with_market_clv(monkeypatch):
    """端到端：run() 产出的每行 ``clv`` 与"重算一遍 market.clv.clv"逐字段相等。"""
    import clv_baseline as mod
    from market.clv import clv

    fake_games = [
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics",
         "commence_time": "2026-10-20T19:00:00Z",
         "bookmakers": [{"key": "x", "markets": [
             {"key": "h2h", "outcomes": [{"name": "Atlanta Hawks", "price": 2.0},
                                          {"name": "Boston Celtics", "price": 1.8}]}]}]},
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

    assert len(rep["rows"]) == 1
    row = rep["rows"][0]
    # row["clv"] 是 round(4)，重算一遍 market.clv.clv 也 round(4) 应一致
    expected = round(clv(row["p_model"], row["p_close"]), 4)
    assert row["clv"] == expected, f"row.clv={row['clv']} expected={expected}"
    # mean_clv = sum / len（valid_clv）
    valid = [r for r in rep["rows"] if r["clv"] is not None]
    expected_mean = round(sum(r["clv"] for r in valid) / len(valid), 4)
    assert rep["mean_clv"] == expected_mean
