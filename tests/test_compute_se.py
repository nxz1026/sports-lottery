"""R37-1: scripts/core/model/monte_carlo.py::_compute_se 5 测直测 + D-22/D-23 锁定（Top 1）。

现状：冠军概率标准误计算函数 0 直测；挂在 API `convergence_diagnostics`
字段给前端用户，SE/CI 数值错位直接影响诊断可信度。

公式：`SE = sqrt(p * (1 - p) / n)`，ci_95 = [max(0, p-1.96*se), min(1, p+1.96*se)]
round 4 位。

D-22 bug-shaped 锁定：空 champion_counts 静默返 {} 而非抛错，掩盖上游
`all_teams` 空集 bug（隐式契约：上游总应先播种）。

D-23 bug-shaped 锁定：n_simulations=0 直接抛 ZeroDivisionError，当前
无守卫；若 predict.py 误传 0 会 crash 整个 MC 循环。

本轮补 5 测：
- 典型值：3 队 5000/3000/2000 → SE/CI 校验
- p=0 极端（count=0）→ se=0, ci=[0,0]
- p=1 极端（count=n）→ se=0, ci=[1,1]
- D-22：空 champion_counts → 静默返 {}（锁现状）
- D-23：n=0 → ZeroDivisionError 直接抛（锁现状无守卫）
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
def mc_mod():
    spec = importlib.util.spec_from_file_location(
        "compute_se_test", REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _compute_se ────────────────────────


def test_compute_se_typical_values(mc_mod):
    """R37-1：3 队不同 count → SE/CI 公式校验。

    Arsenal count=5000/10000 → p=0.5，SE=sqrt(0.5*0.5/10000)=sqrt(0.000025)=0.005
    ci=[max(0, 0.5-0.0098), min(1, 0.5+0.0098)]=[0.4902, 0.5098]
    """
    out = mc_mod._compute_se(
        {"Arsenal": 5000, "Chelsea": 3000, "Liverpool": 2000}, 10000)
    assert out["Arsenal"]["se"] == 0.005
    assert out["Arsenal"]["ci_95_low"] == 0.4902
    assert out["Arsenal"]["ci_95_high"] == 0.5098
    # Chelsea p=0.3, SE=sqrt(0.21/10000)=sqrt(0.000021)≈0.0046
    assert abs(out["Chelsea"]["se"] - 0.0046) < 1e-4
    # Liverpool p=0.2, SE=sqrt(0.16/10000)=0.004
    assert out["Liverpool"]["se"] == 0.004


def test_compute_se_extreme_p_zero(mc_mod):
    """R37-1：count=0（队从未夺冠）→ p=0 → se=0, ci=[0,0]。

    p=0 → SE = sqrt(0) = 0；ci_95_low = max(0, 0-0) = 0；ci_95_high = min(1, 0+0) = 0。
    """
    out = mc_mod._compute_se({"NeverWin": 0, "Sometimes": 100}, 1000)
    assert out["NeverWin"]["se"] == 0.0
    assert out["NeverWin"]["ci_95_low"] == 0.0
    assert out["NeverWin"]["ci_95_high"] == 0.0
    # Sometimes p=0.1 → SE > 0
    assert out["Sometimes"]["se"] > 0


def test_compute_se_extreme_p_one(mc_mod):
    """R37-1：count=n（队每次都夺冠）→ p=1 → se=0, ci=[1,1]（round 4 位钳位）。

    p=1 → SE = sqrt(0) = 0；ci_95_high = min(1, 1+0) = 1.0。
    """
    out = mc_mod._compute_se({"God": 100}, 100)
    assert out["God"]["se"] == 0.0
    assert out["God"]["ci_95_low"] == 1.0
    assert out["God"]["ci_95_high"] == 1  # round(1.0, 4) = 1


def test_compute_se_d22_empty_champion_counts_silently_returns_empty(mc_mod):
    """R37-1 D-22 锁定：空 champion_counts → 静默返 {}（不抛错）。

    隐式契约：上游 `monte_carlo_champion` 应保证 `all_teams` 已播种；
    静默返 {} 是现状。锁定防 silent 切换为抛 ValueError 时回归。
    """
    assert mc_mod._compute_se({}, 1000) == {}


def test_compute_se_d23_zero_simulations_raises_zero_division(mc_mod):
    """R37-1 D-23 锁定：n_simulations=0 → ZeroDivisionError 直接抛（无守卫）。

    p = count / n_simulations 时除零。当前实现无 `n >= 1` 守卫；
    若 predict.py 误传 0 会 crash 整个 MC 循环。锁定无守卫现状，
    防未来加守卫时未覆盖旧调用点。
    """
    with pytest.raises(ZeroDivisionError):
        mc_mod._compute_se({"Arsenal": 0}, 0)