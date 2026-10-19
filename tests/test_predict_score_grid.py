"""R33-1: scripts/core/predictor.py::_score_grid 5 测直测。

现状：P0-2 9×9 泊松比分网格 0 直测；是用户可见的核心输出。
两条独立分支：DC 模型（Dixon-Coles 闭式解）vs 独立泊松 9×9 枚举，
每条 BTTS/O2.5 计算方式一致但输入分布不同。

返回 5-tuple: (predicted_score, top3, btts_prob, over_25_prob, all_scores)。

本轮补 5 测：
- DC 分支（典型 λ=1.5, 1.2, dc=True, ρ=-0.1）→ predicted/top3/btts/over 结构
- 非 DC 分支（相同 λ, dc=False）→ 9×9 枚举 + sort desc
- 5-tuple shape 锁定（恒为 5）
- DC 高分差（λ=3.0, 0.5）→ over_25_prob > 0.5（高分差常见 O2.5）
- 非 DC 极小 λ（0.01, 0.01）→ 仍返有效 top3（D-14 候选 #3 验证：all_scores
  不会空、(0,0) 概率恒 ≥0.001 不过滤；锁定现状）
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
        "score_grid_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _score_grid ────────────────────────


def test_score_grid_dc_branch_basic(predictor_mod):
    """R33-1：DC 分支 (λ=1.5, 1.2, ρ=-0.1) → 5-tuple 结构 + 1-1 预测。"""
    pred, top3, btts, over, all_s = predictor_mod._score_grid(
        lambda_home=1.5, lambda_away=1.2,
        use_dixon_coles=True, dc_rho=-0.1)
    # predicted_score 字符串 "h-a"
    assert isinstance(pred, str) and "-" in pred
    h, a = pred.split("-")
    assert int(h) >= 0 and int(a) >= 0
    # top3 长度 ≤ 3，每个元素 (h, a, prob)
    assert 1 <= len(top3) <= 3
    for s in top3:
        assert len(s) == 3 and isinstance(s[2], float)
    # btts/over 概率 ∈ [0, 1]
    assert 0.0 <= btts <= 1.0
    assert 0.0 <= over <= 1.0
    # all_scores 非空
    assert len(all_s) >= 1


def test_score_grid_non_dc_branch_enumerates_grid(predictor_mod):
    """R33-1：非 DC 分支 (λ=1.5, 1.2, dc=False) → 9×9 网格 + sort desc。

    81 个网格点过滤 p<0.001 后剩 ~30+；top3[0] 概率最高。
    """
    pred, top3, btts, over, all_s = predictor_mod._score_grid(
        lambda_home=1.5, lambda_away=1.2,
        use_dixon_coles=False, dc_rho=0.0)
    # 至少 10+ 个概率≥0.001 的比分
    assert len(all_s) >= 10
    # 排序降序
    for i in range(len(all_s) - 1):
        assert all_s[i][2] >= all_s[i + 1][2]
    # top3[0] = all_s[0]
    assert top3[0][:2] == all_s[0][:2]
    # predicted_score 由 top3[0] 拼接
    assert pred == f"{top3[0][0]}-{top3[0][1]}"


def test_score_grid_return_shape_5tuple_locked(predictor_mod):
    """R33-1：返回值 shape 锁定 — 任何参数下恒为 5-tuple (pred, top3, btts, over, all_s)。"""
    for params in [
        (1.5, 1.2, True, -0.1),
        (1.5, 1.2, False, 0.0),
        (3.0, 0.5, True, -0.1),
        (0.01, 0.01, False, 0.0),
    ]:
        out = predictor_mod._score_grid(*params)
        assert isinstance(out, tuple) and len(out) == 5
        assert isinstance(out[0], str)  # predicted_score
        assert isinstance(out[1], list)  # top3
        assert isinstance(out[2], (int, float))  # btts（DC 极端时 sum=0 是 int）
        assert isinstance(out[3], (int, float))  # over_25
        assert isinstance(out[4], list)  # all_scores


def test_score_grid_high_diff_dc_over_25_high(predictor_mod):
    """R33-1：DC 高分差 (λ=3.0, 0.5) → over_25_prob > 0.5（主场强队 3 球期望）。"""
    pred, top3, btts, over, all_s = predictor_mod._score_grid(
        lambda_home=3.0, lambda_away=0.5,
        use_dixon_coles=True, dc_rho=-0.1)
    # 高分差 → 进 3 球期望高 → O2.5 显著
    assert over > 0.5
    # top3[0] 主场进球数应 ≥ 2
    assert top3[0][0] >= 2


def test_score_grid_tiny_lambda_non_dc_returns_valid_top3(predictor_mod):
    """R33-1：非 DC 极小 λ (0.01, 0.01) → (0,0) 概率恒 ≥0.001 不过滤。

    D-14 候选 #3 验证：原假设 lambda_home=lambda_away=极小时 all_scores 可能
    为空触发 IndexError。实测：poisson_pmf(0, 0.01) = e^-0.01 ≈ 0.99；
    p_00 = 0.99 × 0.99 ≈ 0.98 永远 ≥ 0.001 过滤线 → all_scores 至少含 (0,0,0.98)，
    top3[0]="0-0"。本测锁定现状防 lambda_lower_bound 改动时回归。
    """
    pred, top3, btts, over, all_s = predictor_mod._score_grid(
        lambda_home=0.01, lambda_away=0.01,
        use_dixon_coles=False, dc_rho=0.0)
    # (0,0) 概率显著 → 预测 0-0
    assert pred == "0-0"
    # all_scores 至少 1 个
    assert len(all_s) >= 1
    # (0,0) 在最前
    assert all_s[0][:2] == (0, 0)
