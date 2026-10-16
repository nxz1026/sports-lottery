"""R33-3: scripts/core/predictor.py::_init_inputs 6 测直测。

现状：输入解包 + 联赛差异化 ρ + Onside 4 信号 0 直测；
P0-3 联赛差异化 ρ + D-14 `or` 吞 0 bug 锁定。

本轮补 6 测：
- weights=None → 默认 ONSIDE_WEIGHTS；fifa_rankings=None 时调 fetch_fifa_rankings
- dc_rho=None + "epl" → 走 LEAGUE_DC_RHO；"unknown" → 回退 DC_RHO
- dc_rho=0.1 显式传入 → 不读 LEAGUE_DC_RHO
- D-14 锁定：match={hp:0, dp:0, ap:0} → 全部变 0.5/0.25/0.25（`or` 吞 0 行为）
- 缺 home_en/away_en → 回退到 home/away
- onside 字典透传：mock compute_onside_signals 返固定值 → home_onside/away_onside 提取正确
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
def predictor_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "init_inputs_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 避免真实 fetch_fifa_rankings 触网/读盘
    monkeypatch.setattr(mod, "fetch_fifa_rankings",
                        lambda: {"Arsenal": 1, "Chelsea": 2})
    # 避免真实 compute_onside_signals 触发 onside 链
    monkeypatch.setattr(mod, "compute_onside_signals",
                        lambda h, a, fr, hc, elo_ratings=None: {
                            "home": {"onside_score": 0.7},
                            "away": {"onside_score": 0.3}})
    return mod


BASE_MATCH = {"home": "Arsenal", "away": "Chelsea",
              "home_true_prob": 0.6, "draw_true_prob": 0.2, "away_true_prob": 0.2}


# ──────────────────────── _init_inputs ────────────────────────


def test_init_inputs_default_weights_and_league_rho(predictor_mod):
    """R33-3：weights=None → ONSIDE_WEIGHTS；dc_rho=None + "epl" → LEAGUE_DC_RHO["epl"]。

    ONSIDE_WEIGHTS = {fifa_ranking:0.25, league_footprint:0.2, host_advantage:0.15,
                      confederation:0.2}（scripts/core/constants.py）。
    """
    out = predictor_mod._init_inputs(
        match=BASE_MATCH, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="epl", dc_rho=None)
    weights, dc_rho, hp, dp, ap = out[:5]
    # 默认 weights = ONSIDE_WEIGHTS（4 个键）
    assert weights == predictor_mod.ONSIDE_WEIGHTS
    # EPL 联赛 ρ
    assert dc_rho == predictor_mod.LEAGUE_DC_RHO["epl"]
    # market 三元透传
    assert (hp, dp, ap) == (0.6, 0.2, 0.2)


def test_init_inputs_unknown_league_falls_back_to_dc_rho(predictor_mod):
    """R33-3：league_key="unknown"（不在 LEAGUE_DC_RHO）→ 回退 DC_RHO。"""
    out = predictor_mod._init_inputs(
        match=BASE_MATCH, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="unknown", dc_rho=None)
    _, dc_rho, *_ = out
    assert dc_rho == predictor_mod.DC_RHO


def test_init_inputs_explicit_dc_rho_overrides_league(predictor_mod):
    """R33-3：dc_rho=0.1 显式传入 → 不读 LEAGUE_DC_RHO；保持 0.1。"""
    out = predictor_mod._init_inputs(
        match=BASE_MATCH, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="epl", dc_rho=0.1)
    _, dc_rho, *_ = out
    assert dc_rho == 0.1


def test_init_inputs_d14_or_swallows_zero(predictor_mod):
    """R33-3 D-14 bug-shaped 锁定：match={hp:0, dp:0, ap:0} → 全部变 0.5/0.25/0.25。

    `or` 把合法输入 0 误吞为默认值（0.5/0.25/0.25）。生产数据 hp>0 不触发，
    但 calibration 边界场景可能传 0（缺真实数据 fallback）。锁定实际行为，
    防 silent 切换为 `if x is None: x = default` 时未覆盖旧调用点。
    """
    m_zero = {"home": "Arsenal", "away": "Chelsea",
              "home_true_prob": 0, "draw_true_prob": 0, "away_true_prob": 0}
    out = predictor_mod._init_inputs(
        match=m_zero, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="epl", dc_rho=None)
    _, _, hp, dp, ap, *_ = out
    assert hp == 0.5
    assert dp == 0.25
    assert ap == 0.25


def test_init_inputs_home_en_falls_back_to_home(predictor_mod):
    """R33-3：缺 home_en/away_en → 回退到 home/away（match 中英字段兼容）。"""
    m_no_en = {"home": "Arsenal", "away": "Chelsea",
               "home_true_prob": 0.5, "draw_true_prob": 0.25, "away_true_prob": 0.25}
    out = predictor_mod._init_inputs(
        match=m_no_en, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="epl", dc_rho=None)
    weights, dc_rho, hp, dp, ap, hfs, afs, hrs, ars, sm, home_en, away_en, *_ = out
    assert home_en == "Arsenal"
    assert away_en == "Chelsea"


def test_init_inputs_onside_extracted_from_compute(predictor_mod):
    """R33-3：compute_onside_signals 返固定值 → home_onside/away_onside 提取正确。

    mock compute_onside_signals 返 {"home":{"onside_score":0.7},"away":{"onside_score":0.3}}。
    """
    out = predictor_mod._init_inputs(
        match=BASE_MATCH, weights=None, fifa_rankings=None,
        host_country=None, elo_ratings=None,
        league_key="epl", dc_rho=None)
    weights, dc_rho, hp, dp, ap, hfs, afs, hrs, ars, sm, home_en, away_en, onside, home_onside, away_onside = out
    # onside 字典透传
    assert onside["home"]["onside_score"] == 0.7
    assert onside["away"]["onside_score"] == 0.3
    # home_onside/away_onside 提取正确
    assert home_onside == 0.7
    assert away_onside == 0.3