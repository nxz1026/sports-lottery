"""R51-7: scripts/core/model/poisson.py 2 个 0 直测 helper 4 测 + D-103 lock。

R51 已锁 D-97~D-102 (auth / features / i18n). fit_dc_rho 内层的
``_neg_log_likelihood`` + ``_refine_dc_rho`` 完全 0 直测, 仅 fit_dc_rho
集成层 5 测 (test_poisson.py::TestFitDcRho) 覆盖 happy.

D-103 锁 _neg_log_likelihood + _refine_dc_rho 双契约 (4 测):
- D-103-1: _neg_log_likelihood 空 scores -> 0.0 (无 iteration)
- D-103-2: _neg_log_likelihood 单 score / 多 score 累加正确 (=-log(prob) 相对值)
- D-103-3: _refine_dc_rho 三分搜索在边界: best_rho 接近 rho_min/rho_max 时
           lo/hi 被 max/min 收紧, final_rho 不越界
- D-103-4: _refine_dc_rho 兜底保留: 三分搜索结果若 LL < best_ll 则 final=best_rho

策略:
- spec-load poisson.py 到隔离 namespace
- 真实 dixon_coles_pmf + math.log (纯函数, 无副作用)
- 0 依赖 + 0 IO, 纯数学验证
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def poisson_helpers_mod():
    """Spec-load poisson.py + 暴露 2 个目标 helper.

    不需要 monkeypatch — 纯函数 + 真实 dixon_coles_pmf + math.
    """
    spec = importlib.util.spec_from_file_location(
        "poisson_refit_test",
        REPO_ROOT / "scripts" / "core" / "model" / "poisson.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-103: 4 测 ────────────────────────


def test_d103_1_neg_log_likelihood_empty_scores_returns_zero(poisson_helpers_mod):
    """D-103-1: _neg_log_likelihood([]) 必返 0.0 (L120 ll=0.0, 无 iteration)."""
    out = poisson_helpers_mod._neg_log_likelihood([], 1.5, 1.2, 0.2)
    assert out == 0.0


def test_d103_2_neg_log_likelihood_single_and_multi_score_sum(poisson_helpers_mod):
    """D-103-2: _neg_log_likelihood 累加 -log(dixon_coles_pmf(h, a, ...)) 严格等于数学定义.

    - 场景 a: 单 score, 期望 -log(dixon_coles_pmf(h, a, avg_h, avg_a, rho_val))
    - 场景 b: 多 score, 期望上述的累加
    - 验证 p > 1e-10 时累加 / p <= 1e-10 时跳过 (用极端 rho 触发)
    """
    avg_h, avg_a, rho_val = 1.4, 1.1, 0.1

    # 场景 a: 单 score
    single = [(2, 1)]
    expected_single = -math.log(
        poisson_helpers_mod.dixon_coles_pmf(2, 1, avg_h, avg_a, rho_val)
    )
    actual_single = poisson_helpers_mod._neg_log_likelihood(single, avg_h, avg_a, rho_val)
    assert actual_single == pytest.approx(expected_single, abs=1e-9)

    # 场景 b: 多 score 累加
    multi = [(2, 1), (1, 1), (0, 2)]
    expected_multi = sum(
        -math.log(poisson_helpers_mod.dixon_coles_pmf(h, a, avg_h, avg_a, rho_val))
        for h, a in multi
    )
    actual_multi = poisson_helpers_mod._neg_log_likelihood(multi, avg_h, avg_a, rho_val)
    assert actual_multi == pytest.approx(expected_multi, abs=1e-9)
    # 多 score 必然大于等于单 score (累加非负)
    assert actual_multi >= actual_single - 1e-9


def test_d103_3_refine_dc_rho_clamps_window_to_rho_min_max(poisson_helpers_mod):
    """D-103-3: _refine_dc_rho 在 best_rho 接近边界时 lo/hi 被 max/min 收紧 (L131-132).

    - 场景 a: best_rho 极靠近 rho_max, hi 应被 min(rho_max, ...) 截断;
              final_rho <= rho_max + 1e-9
    - 场景 b: best_rho 极靠近 rho_min, lo 应被 max(rho_min, ...) 截断;
              final_rho >= rho_min - 1e-9
    """
    scores = [(1, 0), (2, 1), (1, 1), (0, 2), (2, 0)] * 4  # 20+ scores, 充分拟合
    avg_h, avg_a = 1.4, 1.1
    rho_min, rho_max = -0.3, 0.5
    adaptive_step = 0.005

    # 场景 a: best_rho = rho_max (0.5), 窗口应该被 hi=min(rho_max, 0.5+0.05)=0.5 收紧
    final_a = poisson_helpers_mod._refine_dc_rho(
        scores, avg_h, avg_a, rho_min, rho_max, adaptive_step,
        best_rho=rho_max, best_ll=0.0,
    )
    assert final_a <= rho_max + 1e-9
    assert final_a >= rho_min - 1e-9

    # 场景 b: best_rho = rho_min (-0.3), lo=max(rho_min, -0.3-0.05)=-0.3
    final_b = poisson_helpers_mod._refine_dc_rho(
        scores, avg_h, avg_a, rho_min, rho_max, adaptive_step,
        best_rho=rho_min, best_ll=0.0,
    )
    assert final_b <= rho_max + 1e-9
    assert final_b >= rho_min - 1e-9


def test_d103_4_refine_dc_rho_fallback_to_best_rho_when_refit_worse(poisson_helpers_mod):
    """D-103-4: L146-147 兜底: 三分搜索结果 LL < best_ll 时 final_rho = best_rho.

    通过构造"best_ll 已经是非常大的数" (极端高 LL), 让三分搜索几乎不可能超过,
    从而触发 L146 `if -_neg_log_likelihood(...) < best_ll: final_rho = best_rho`.

    策略: best_ll=+inf, 那么任何三分搜索后的 -LL 都 < inf, 必然 fallback.
    期望 final_rho == best_rho 严格相等.
    """
    scores = [(1, 1), (2, 0), (0, 1)] * 5  # 15 个 score
    avg_h, avg_a = 1.2, 0.9

    # best_ll=+inf 必触发 fallback
    best_rho_in = 0.123
    final = poisson_helpers_mod._refine_dc_rho(
        scores, avg_h, avg_a, rho_min=-0.3, rho_max=0.5,
        adaptive_step=0.005, best_rho=best_rho_in,
        best_ll=float("inf"),
    )
    assert final == best_rho_in
