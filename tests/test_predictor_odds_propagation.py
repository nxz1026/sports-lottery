"""P1-A10: 验证 predictor 把 ESPN 赔率字段（market/home_ml_implied/...）从 match 透传到 result。

根因：scripts/core/data/odds_enrich._apply_enrichment 就地写 ``match``，但
scripts/core/predictor._assemble_result 早期版本只拷 ``odds_data_available`` 标志，
不拷 ``market`` 等结构化字段；写盘 prediction_*.json 时 market 缺失 → web 层
``_market_projection`` 永远 ``status=missing`` → 今日推荐 / 串关工作台「赔率/价值不可用」。

修复：在 _assemble_result 中按 _ODDS_PROPAGATION_FIELDS 仅在值非空时透传，
避免污染 None / 空 dict。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

# 把仓库根 + scripts/ 加进 sys.path（与其他 tests/test_*.py 一致）
REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from core.predictor import _assemble_result, calculate_prediction  # noqa: E402


def _minimal_assemble_args(match=None):
    """构造一组最简 _assemble_result 参数；只关心透传语义，其余字段给合理 default。"""
    m = match if match is not None else {}
    return dict(
        match=m,
        direction="主队 胜",
        stars="★★★☆☆",
        confidence_raw=0.62,
        predicted_score="2-1",
        top3=[(2, 1, 0.15), (1, 1, 0.13), (2, 0, 0.11)],
        lambda_home=1.4,
        lambda_away=1.1,
        raw_home=1.35,
        raw_away=1.05,
        btts_prob=0.55,
        over_25_prob=0.48,
        ml_proba=None,
        home_prob=0.5,
        draw_prob_calc=0.28,
        away_prob=0.22,
        hfs=0.6, hrs=0.55, afs=0.45, ars=0.5, sm=0.0,
        home_onside=0.6, away_onside=0.4,
        elo_home_expected=None,
        elo_ratings=None,
        use_dixon_coles=False,
        dc_rho=None,
        league_key="epl",
        onside={},
        confidence_note=None,
        hp=0.5, dp=0.28, ap=0.22,
        # R13 #4：新增 3 个 _assemble_result 参数（home_en/away_en/host_country）——
        # 这些 odds 透传测试只关心 result["market"] / ["home_ml_implied"] 等字段是否
        # 透传，不动 reasoning_factors，给空值即可。
        home_en="", away_en="", host_country=None,
    )


# ── 直接调 _assemble_result 验证透传 ──────────────────────────────

def test_market_field_is_propagated_when_present():
    """match 里有完整 market dict 时，result 必须带上 market。"""
    match = {
        "odds_data_available": True,
        "market": {
            "source": "espn",
            "captured_at": "2026-10-11T09:00:00+00:00",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {
                "home": {"decimal_odds": 2.10},
                "draw": {"decimal_odds": 3.40},
                "away": {"decimal_odds": 3.60},
            },
        },
        "home_ml_implied": 0.4762,
        "draw_implied": 0.2941,
        "home_true_prob": 0.49,
        "draw_true_prob": 0.27,
        "away_true_prob": 0.24,
        "spread_home_line": "-0.5",
        "total_over_close": "2.5",
    }
    result = _assemble_result(**_minimal_assemble_args(match))
    assert "market" in result, "market 字段必须透传"
    assert result["market"]["source"] == "espn"
    assert result["market"]["selections"]["home"]["decimal_odds"] == 2.10
    assert result["home_ml_implied"] == 0.4762
    assert result["home_true_prob"] == 0.49
    assert result["draw_true_prob"] == 0.27
    assert result["away_true_prob"] == 0.24
    assert result["spread_home_line"] == "-0.5"
    assert result["total_over_close"] == "2.5"
    assert result["odds_data_available"] is True


def test_missing_market_is_not_propagated_as_empty_dict():
    """match 里 market 字段缺失（football-data 源 + 未富化）→ result 不应有 market。"""
    match = {"odds_data_available": False}
    result = _assemble_result(**_minimal_assemble_args(match))
    assert "market" not in result
    assert result["odds_data_available"] is False


def test_empty_values_are_not_propagated():
    """match 里 market 是空 dict 或 None / 空字符串 → 不写进 result（避免污染下游默认降级）。"""
    for empty in (None, {}, ""):
        match = {"odds_data_available": True, "market": empty, "home_ml_implied": ""}
        result = _assemble_result(**_minimal_assemble_args(match))
        assert "market" not in result, f"空 market={empty!r} 不应写入 result"
        assert "home_ml_implied" not in result, "空字符串 home_ml_implied 不应写入"


# ── 端到端：calculate_prediction 也带上 market ─────────────────────

@patch("core.predictor.compute_onside_signals")
def test_calculate_prediction_propagates_espn_fields(mock_onside):
    """通过 calculate_prediction 端到端验证：富化过的 match → result 含 market。"""
    mock_onside.return_value = {"home": {"onside_score": 0.65}, "away": {"onside_score": 0.45}}
    match = {
        "home": "Arsenal", "away": "Chelsea",
        "home_true_prob": 0.50, "draw_true_prob": 0.27, "away_true_prob": 0.23,
        "odds_data_available": True,
        "market": {
            "source": "espn",
            "captured_at": "2026-10-11T09:00:00+00:00",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {
                "home": {"decimal_odds": 1.95},
                "draw": {"decimal_odds": 3.50},
                "away": {"decimal_odds": 4.20},
            },
        },
        "home_ml_implied": 0.513, "draw_implied": 0.286,
        "home_true_prob_dup": 0.50,  # predictor 会覆盖
    }
    result = calculate_prediction(match, use_dixon_coles=False, league_key="epl")
    assert "market" in result
    assert result["market"]["source"] == "espn"
    assert result["market"]["selections"]["home"]["decimal_odds"] == 1.95


@patch("core.predictor.compute_onside_signals")
def test_calculate_prediction_without_odds_has_no_market(mock_onside):
    """football-data 源未富化的 match → calculate_prediction 输出也不该有 market。"""
    mock_onside.return_value = {"home": {"onside_score": 0.5}, "away": {"onside_score": 0.5}}
    match = {
        "home": "Arsenal", "away": "Chelsea",
        "home_true_prob": 0.45, "draw_true_prob": 0.27, "away_true_prob": 0.28,
        "odds_data_available": False,
    }
    result = calculate_prediction(match, use_dixon_coles=False, league_key="epl")
    assert "market" not in result