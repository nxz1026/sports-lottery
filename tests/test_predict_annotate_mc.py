"""predict.py _annotate_monte_carlo 关键路径回归测试。

关注点：
- run_monte_carlo=False → 直接 None（不浪费函数调用）
- predictions 为空 → None
- _season_mc_inputs 抛异常时降级到窗口内赛程（不阻断预测）
- 整季赛程空 → 退回窗口内；窗口内也空 → None（不返回半成品 dict）
- 蒙特卡洛真跑一轮（n_simulations 调小）能产出 champion_probs

超时的最小回归：注释里点明「3 篇」中提到的 P0-2 修复
（整季赛程 + 积分榜播种；窗口内 6 场兜底）的兜底逻辑。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from predict import _annotate_monte_carlo  # noqa: E402


def _stub_mc(return_value: dict) -> object:
    """构建一个 mock，替代 monte_carlo_champion 返回固定值。"""
    return patch("predict.monte_carlo_champion", return_value=return_value)


def test_returns_none_when_monte_carlo_disabled() -> None:
    """run_monte_carlo=False → 不调用任何东西，直接 None。"""
    assert _annotate_monte_carlo(
        predictions=[{"home": "A", "away": "B"}],
        run_monte_carlo=False, n_simulations=10, fitted_rho=0.2,
        tournament_type="league", league_key="epl",
    ) is None


def test_returns_none_when_predictions_empty() -> None:
    """predictions 为空 → None（无论 run_monte_carlo 是 True/False）。"""
    assert _annotate_monte_carlo(
        predictions=[], run_monte_carlo=True, n_simulations=10,
        fitted_rho=0.2, tournament_type="league", league_key="epl",
    ) is None


def test_season_mc_inputs_exception_falls_back_to_window_fixtures() -> None:
    """_season_mc_inputs 抛异常 → 降级到窗口内赛程，冠军榜仍出（仅语义弱）。"""
    preds = [
        {"home": "Arsenal", "away": "Chelsea",
         "lambda_home": 1.7, "lambda_away": 1.1},
        {"home": "Liverpool", "away": "Man City",
         "lambda_home": 1.5, "lambda_away": 1.4},
    ]
    fake_mc = {"champion_probs": {"Arsenal": 0.4, "Chelsea": 0.2,
                                  "Liverpool": 0.3, "Man City": 0.1}}
    with patch("predict._season_mc_inputs", side_effect=RuntimeError("network down")), \
         _stub_mc(fake_mc):
        result = _annotate_monte_carlo(
            predictions=preds, run_monte_carlo=True, n_simulations=10,
            fitted_rho=0.2, tournament_type="league", league_key="epl",
        )
    assert result == fake_mc  # 降级但出数


def test_season_mc_inputs_empty_list_falls_back_to_window_fixtures() -> None:
    """_season_mc_inputs 返回 ([], {}, {}) 时降级窗口内。"""
    preds = [{"home": "A", "away": "B", "lambda_home": 1.4, "lambda_away": 1.2}]
    fake_mc = {"champion_probs": {"A": 0.5, "B": 0.5}}
    with patch("predict._season_mc_inputs", return_value=([], {}, {})), \
         _stub_mc(fake_mc):
        result = _annotate_monte_carlo(
            predictions=preds, run_monte_carlo=True, n_simulations=10,
            fitted_rho=0.2, tournament_type="league", league_key="epl",
        )
    assert result == fake_mc


def test_returns_none_when_no_fixtures_at_all() -> None:
    """整季空 + 窗口内也空 → None（不返回半成品 dict）。"""
    preds = []  # 窗口内为空
    with patch("predict._season_mc_inputs", return_value=([], {}, {})):
        result = _annotate_monte_carlo(
            predictions=preds, run_monte_carlo=True, n_simulations=10,
            fitted_rho=0.2, tournament_type="league", league_key="epl",
        )
    assert result is None


def test_real_monte_carlo_one_round_small_sim() -> None:
    """真跑一轮（n_simulations=20，lambda 固定）应产出 champion_probs。

    验证降级路径下窗口内赛程能真实被蒙特卡洛消费（不调 mock），
    防御 _build_fixtures_from_predictions 接口变更导致静默走形。
    """
    preds = [
        {"home": "Arsenal", "away": "Chelsea",
         "lambda_home": 1.5, "lambda_away": 1.2},
        {"home": "Liverpool", "away": "Man City",
         "lambda_home": 1.4, "lambda_away": 1.3},
    ]
    with patch("predict._season_mc_inputs", side_effect=RuntimeError("skip")):
        result = _annotate_monte_carlo(
            predictions=preds, run_monte_carlo=True, n_simulations=20,
            fitted_rho=0.2, tournament_type="league", league_key="epl",
        )
    assert result is not None
    probs = result["champion_probs"]
    # 4 支球队必须都出现（即使有 0 概率 — P0-2 修复"保留 0 概率球队"）
    assert set(probs.keys()) == {"Arsenal", "Chelsea", "Liverpool", "Man City"}
    # 总和约等于 1（蒙特卡洛归一化）
    assert abs(sum(probs.values()) - 1.0) < 1e-6