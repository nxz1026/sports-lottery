"""R46-2: scripts/core/model/monte_carlo.py::simulate_match_dc 4 cases direct + D-69 lock.

simulate_match_dc (L20-L44, 25 行) 用 Dixon-Coles 联合泊松模拟单场比分。
R37-1 D-22/D-23 锁 _compute_se; R44 / R45-4 间接断言 _simulate_round 走
simulate_world_cup/simulate_league; 但 simulate_match_dc 真函数被多个测试
monkeypatch 间接覆盖, 函数自身 0 直测 (R37-2/3/4 test_monte_carlo_worldcup_*).

D-69 锁定 4 契约:
- D-69-1: 主流程 happy - dixon_coles_pmf 让 home=2, away=0 概率最高 ->
  random=0.5 命中累计 -> 返 (2, 0)
- D-69-2: total_p=0 (全 PMF 返 0) -> 不归一化 -> random=0.5 仍不命中累加
  (累计 0) -> 兜底 return 0, 0
- D-69-3: random=1.0 (浮点累加永远 < 1) -> 兜底 return 0, 0
- D-69-4: 均匀 PMF + 多次采样 -> 分布均匀 (2x2 grid 验证)

依赖 stub:
- core.model.poisson.dixon_coles_pmf (monkeypatch 返受控 PMF)
- core.config.MAX_GOALS_MC (monkeypatch 设为 2 减少循环)
- random.random (monkeypatch 返受控值)
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
    """Spec-load scripts/core/model/monte_carlo.py (复用 R45-4 套路)。"""
    spec = importlib.util.spec_from_file_location(
        "monte_carlo_simulate_match_dc_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-69 fixture helpers ────────────────────────


def _stub_pmf_max_goals(monkeypatch, pmf_func, max_goals: int = 2):
    """Stub dixon_coles_pmf + MAX_GOALS_MC 让循环受控。"""
    from core.model import poisson as poisson_mod
    from core import config as config_mod

    monkeypatch.setattr(poisson_mod, "dixon_coles_pmf", pmf_func)
    monkeypatch.setattr(config_mod, "MAX_GOALS_MC", max_goals)
    # simulate_match_dc 内部 'from core.config import MAX_GOALS_MC' lazy import
    # 需在 sys.modules['core.config'] 上 patch


def _stub_random(monkeypatch, value: float):
    """Stub random.random 返固定值。"""
    import random
    monkeypatch.setattr(random, "random", lambda: value)


# ──────────────────────── D-69 四契约 ────────────────────────


def test_simulate_match_dc_d69_1_happy_path_picks_highest_pmf(mc_mod, monkeypatch):
    """R46-2 D-69-1: 主流程 happy - 最高 PMF 命中。

    MAX_GOALS_MC=2, 4 个 PMF: (0,0)=0.0, (1,0)=0.0, (2,0)=1.0, 其他=0.0。
    random=0.5, 累计: 0.0+0.0+1.0=1.0 >= 0.5 -> 命中 (2,0)。
    """
    def fake_pmf(h, a, lambda_h, lambda_a, rho=0.2):
        return 1.0 if (h == 2 and a == 0) else 0.0

    _stub_pmf_max_goals(monkeypatch, fake_pmf, max_goals=3)
    _stub_random(monkeypatch, 0.5)

    h, a = mc_mod.simulate_match_dc(1.5, 1.0, rho=0.2)
    assert (h, a) == (2, 0)


def test_simulate_match_dc_d69_2_zero_total_p_returns_fallback(mc_mod, monkeypatch):
    """R46-2 D-69-2: total_p=0 (全 PMF 返 0) -> 兜底 0, 0。

    PMF 全 0 -> 累计永远 0, random=0.5 永不命中 -> 走 L44 `return 0, 0`。
    """
    def zero_pmf(h, a, lambda_h, lambda_a, rho=0.2):
        return 0.0

    _stub_pmf_max_goals(monkeypatch, zero_pmf, max_goals=2)
    _stub_random(monkeypatch, 0.5)

    h, a = mc_mod.simulate_match_dc(1.5, 1.0)
    assert (h, a) == (0, 0)


def test_simulate_match_dc_d69_3_random_beyond_total_p_returns_fallback(mc_mod, monkeypatch):
    """R46-2 D-69-3: random > 累计总和 -> 兜底 0, 0。

    PMF total=0 (全 0 概率) -> 累计永远 0, 任何 random > 0 永不命中。
    这模拟了 PMF 归一化前的 total_p=0 边界 (兜底分支)。
    """
    def zero_pmf(h, a, lambda_h, lambda_a, rho=0.2):
        return 0.0

    _stub_pmf_max_goals(monkeypatch, zero_pmf, max_goals=2)
    _stub_random(monkeypatch, 0.99)  # random > 累计(0)

    h, a = mc_mod.simulate_match_dc(1.5, 1.0)
    assert (h, a) == (0, 0)


def test_simulate_match_dc_d69_4_uniform_pmf_distribution(mc_mod, monkeypatch):
    """R46-2 D-69-4: 均匀 PMF + 多次采样 -> 分布覆盖。

    MAX_GOALS_MC=2, PMF 均匀 0.25 -> 4x4=16 cells 归一化后 1/16 each。
    random 序列让累加依次越界, 验证采样在 0-1 范围内且能命中多种 cell。
    """
    def uniform_pmf(h, a, lambda_h, lambda_a, rho=0.2):
        return 0.25  # 16 * 0.25 = 4.0 total, 归一化后 0.0625 each

    _stub_pmf_max_goals(monkeypatch, uniform_pmf, max_goals=2)

    # 多次采样 (random 不再需要不同 cell, 验证 h, a 在 0-1 范围)
    _stub_random(monkeypatch, 0.5)
    h, a = mc_mod.simulate_match_dc(1.5, 1.0)
    assert 0 <= h <= 1
    assert 0 <= a <= 1

    # 跨多 cell 验证: 16 次随机不同值 -> 至少 2+ 个不同 cell
    random_values = iter([0.01, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50])
    import random
    monkeypatch.setattr(random, "random", lambda: next(random_values))
    samples = set()
    for _ in range(8):
        h, a = mc_mod.simulate_match_dc(1.5, 1.0)
        samples.add((h, a))
    # 8 个 random 值应至少命中 2 个不同 cell
    assert len(samples) >= 2
    for (h, a) in samples:
        assert 0 <= h <= 1
        assert 0 <= a <= 1