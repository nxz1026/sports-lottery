"""R34-3: scripts/core/predictor.py::_weighted_direction_probs 5 测直测。

现状：P0-2 统一权重包装层 0 直测；唯一调用 _assemble_result。
返回 5-tuple: (sm_capped, onside_weight, home_prob, draw_prob_calc, away_prob)。

sm 钳位 ±spread_movement_cap（=0.15）；onside_weight 由 (1-MW) × (1-EW if elo else 1.0)
推算（MW=0.45 MARKET_ODDS_WEIGHT）；委托 _unified_strength_probs（R33-4 已锁）。

本轮补 5 测：
- sm > cap → sm_capped = cap；sm < -cap → sm_capped = -cap
- onside_weight 无 ELO → (1-MW) * 1.0 = 0.55
- onside_weight 有 ELO → (1-MW) * (1-EW) = 0.451
- 5-tuple shape 锁定 + home/draw/away 来自 _unified_strength_probs 委托
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def predictor_mod():
    spec = importlib.util.spec_from_file_location(
        "weighted_direction_probs_test",
        REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(mod, **overrides):
    """公共调用：默认值覆盖"""
    kwargs = dict(
        hp=0.5, dp=0.25, ap=0.25,
        home_onside=0.6, away_onside=0.4,
        sm=0.0, elo_ratings=None,
        elo_home_expected=None, elo_away_expected=None,
        ELO_WEIGHT=0.18, dc_val=1.0, calibration_offset=None,
    )
    kwargs.update(overrides)
    return mod._weighted_direction_probs(**kwargs)


# ──────────────────────── _weighted_direction_probs ────────────────────────


def test_weighted_direction_probs_sm_clamps_to_positive_cap(predictor_mod):
    """R34-3：sm=100 → sm_capped = spread_movement_cap (0.15)。"""
    out = _call(predictor_mod, sm=100.0)
    sm_capped, onside_weight, *_ = out
    assert sm_capped == predictor_mod.THRESHOLDS["spread_movement_cap"]
    assert sm_capped == 0.15


def test_weighted_direction_probs_sm_clamps_to_negative_cap(predictor_mod):
    """R34-3：sm=-100 → sm_capped = -spread_movement_cap (-0.15)。"""
    out = _call(predictor_mod, sm=-100.0)
    sm_capped, onside_weight, *_ = out
    assert sm_capped == -predictor_mod.THRESHOLDS["spread_movement_cap"]
    assert sm_capped == -0.15


def test_weighted_direction_probs_onside_weight_without_elo(predictor_mod):
    """R34-3：elo_ratings=None → onside_weight = (1 - MARKET_ODDS_WEIGHT) * 1.0 = 0.55。"""
    out = _call(predictor_mod, elo_ratings=None)
    sm_capped, onside_weight, *_ = out
    expected = (1 - predictor_mod.MARKET_ODDS_WEIGHT) * 1.0
    assert onside_weight == expected
    assert onside_weight == 0.55


def test_weighted_direction_probs_onside_weight_with_elo(predictor_mod):
    """R34-3：elo_ratings 真值 → onside_weight = (1 - MW) * (1 - ELO_WEIGHT) = 0.451。

    (1 - 0.45) * (1 - 0.18) = 0.55 * 0.82 = 0.451
    """
    out = _call(predictor_mod, elo_ratings={"A": 1500},
                elo_home_expected=0.6, elo_away_expected=0.4)
    sm_capped, onside_weight, *_ = out
    expected = (1 - predictor_mod.MARKET_ODDS_WEIGHT) * (1 - 0.18)
    assert onside_weight == expected
    assert abs(onside_weight - 0.451) < 1e-9


def test_weighted_direction_probs_return_shape_5tuple_and_delegates(predictor_mod):
    """R34-3：5-tuple shape 锁定 + home/draw/away 来自 _unified_strength_probs 委托。

    等同 RNG output：`sm_capped, onside_weight, home_prob, draw_prob_calc, away_prob`
    """
    out = _call(predictor_mod)
    assert len(out) == 5
    sm_capped, onside_weight, home_p, draw_p, away_p = out
    # home/draw/away 概率 sum=1（来自 _unified_strength_probs，R33-4 已锁）
    assert abs((home_p + draw_p + away_p) - 1.0) < 1e-9
    # 类型
    assert isinstance(sm_capped, float)
    assert isinstance(onside_weight, float)
    for p in (home_p, draw_p, away_p):
        assert isinstance(p, float)
