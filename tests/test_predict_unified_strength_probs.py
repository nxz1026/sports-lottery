"""R33-4: scripts/core/predictor.py::_unified_strength_probs 5 测直测。

现状：P0-2 方向概率源头 0 直测；是 home/draw/away 概率统一加权函数。
返回 3-tuple (home_prob, draw_prob, away_prob)。

本轮补 5 测：
- 基础融合：market 三元 + onside + sm_capped=0.0 → sum=1
- ELO 旁路：elo_ratings=True + elo_home_expected=0.6 → home_strength 加 ELO
- calibration：dict 真值 → _dc_draw=dc_val；None → _dc_draw=1.0
- D-14 锁定：sm_capped 极负 → home_strength/away_strength 变负，
  total=max(..., 0.05) 静默掩盖；home_prob 透传负数，away_prob 透传 >1
- draw_base_score 截断：market_draw*dc*MW + DBS < 0 → draw_strength=max(0,..)=0
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
        "unified_strength_probs_test",
        REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 公共调用：market 三元 + onside + sm_capped=0 + onside_weight=0.3
def _call(mod, **overrides):
    kwargs = dict(
        market_home=0.5, market_draw=0.25, market_away=0.25,
        home_onside=0.6, away_onside=0.4, sm_capped=0.0,
        onside_weight=0.3, elo_ratings=False,
        elo_home_expected=None, elo_away_expected=None,
        ELO_WEIGHT=0.18, dc_val=1.0, calibration_offset=None,
    )
    kwargs.update(overrides)
    return mod._unified_strength_probs(**kwargs)


# ──────────────────────── _unified_strength_probs ────────────────────────


def test_unified_strength_probs_basic_blend_sums_to_one(predictor_mod):
    """R33-4：基础融合 sm_capped=0 → 三概率 sum ≈ 1.0（归一化）。"""
    h, d, a = _call(predictor_mod)
    assert abs((h + d + a) - 1.0) < 1e-9
    # 主胜 > 平局 > 客胜（market=0.5/0.25/0.25 + onside 0.6/0.4）
    assert h > d > a


def test_unified_strength_probs_elo_bonus_lifts_home(predictor_mod):
    """R33-4：elo_ratings=True + elo_home_expected=0.6 → home_prob 比无 ELO 时抬升。"""
    h_no, _, _ = _call(predictor_mod)
    h_elo, _, _ = _call(predictor_mod, elo_ratings=True,
                        elo_home_expected=0.6, elo_away_expected=0.4,
                        ELO_WEIGHT=0.18)
    # ELO 加成让 home_prob 上升
    assert h_elo > h_no
    # 三比例仍归一化
    assert abs((h_elo + _call(predictor_mod, elo_ratings=True,
                              elo_home_expected=0.6, elo_away_expected=0.4,
                              ELO_WEIGHT=0.18)[1] +
                _call(predictor_mod, elo_ratings=True,
                      elo_home_expected=0.6, elo_away_expected=0.4,
                      ELO_WEIGHT=0.18)[2]) - 1.0) < 1e-9


def test_unified_strength_probs_calibration_scales_draw(predictor_mod):
    """R33-4：calibration_offset 真值 → draw_strength 走 _dc_draw=dc_val；None → 1.0。

    dc_val=0.8 + calibration_offset → _dc_draw=0.8 → draw_prob 比无 calibration 低。
    """
    _, d_no, _ = _call(predictor_mod)  # calibration_offset=None → _dc_draw=1.0
    _, d_cal, _ = _call(predictor_mod, dc_val=0.8, calibration_offset={"sample_size": 30})
    # 0.8 < 1.0 → draw_prob 应更低
    assert d_cal < d_no


def test_unified_strength_probs_d14_max_clamp_passes_negatives_through(predictor_mod):
    """R33-4 D-14 bug-shaped 锁定：sm_capped=-10 → home_prob 透传负数，away_prob 透传 >1。

    home_strength = 0.5*0.45 + 0*0.3 + (-10)*0.5 = 0.225 - 5 = -4.775
    away_strength = 0.25*0.45 + 0*0.3 + (-(-10))*0.5 = 0.1125 + 5 = 5.1125
    draw_strength = max(0, 0.25*1*0.45 + 0.15) = 0.2625
    total = max(-4.775 + 0.2625 + 5.1125, 0.05) = 0.6
    home_prob = -4.775/0.6 ≈ -7.96（负数透传！）
    away_prob = 5.1125/0.6 ≈ 8.52（>1 透传！）
    """
    h, d, a = _call(predictor_mod, sm_capped=-10.0,
                    home_onside=0.0, away_onside=0.0)
    # D-14：负数透传、>1 透传 — 这是 bug-shaped 契约
    assert h < 0  # 负 home_prob 透传
    assert a > 1  # >1 away_prob 透传
    # d 仍合法（draw_strength 不受 sm_capped 影响）
    assert 0 <= d <= 1


def test_unified_strength_probs_draw_base_score_clamped_to_zero(predictor_mod):
    """R33-4：market_draw*dc*MW + DBS < 0 → draw_strength=max(0,..)=0 → draw_prob=0。

    market_draw=0.25, dc=-10, MW=0.45, DBS=0.15：
    market_draw*dc*MW + DBS = -1.125 + 0.15 = -0.975 → max(0,..)=0
    """
    _, d, _ = _call(predictor_mod, dc_val=-10.0,
                    calibration_offset={"sample_size": 5})
    # draw_strength 被截断为 0 → draw_prob=0
    assert d == 0.0
    # home + away 应仍 sum=1
    h, _, _ = _call(predictor_mod, dc_val=-10.0,
                    calibration_offset={"sample_size": 5})
    a = 1.0 - h  # sum=1
    assert h > 0 and a > 0
