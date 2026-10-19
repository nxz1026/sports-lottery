"""R45-4: scripts/core/model/monte_carlo.py::_simulate_round 5 cases direct + D-65 lock.

_simulate_round (L47-L105, 32 行 + 进度日志 6 行) 是 Monte Carlo 单轮模拟分发器:
- 进度日志: sim % 2000 == 0 and sim > 0 -> logger.info
- 路由分发: tournament_type=='world_cup' -> simulate_world_cup; else -> simulate_league
- 冠军聚合: champion_counts[champion] += 1 (None/空 -> 跳过)
- 轮次聚合: round_reach_counts[team][round_name] 默认 0 后 +1

R37-1 D-22/D-23 锁 _compute_se, R44 已通过 test_p0_window_mc_stablekey.py 间接断言
monte_carlo_champion 顶层入口, 但 _simulate_round 自身核心契约 (路由分发 + champion
聚合 + team_rounds 默认初始化 + 进度日志) 0 直测。

D-65 锁定 5 契约:
- D-65-1: world_cup 路由 + champion 累加 + team_rounds 累加
- D-65-2: 默认 league 路由 (tournament_type != 'world_cup')
- D-65-3: champion=None -> 不累加 (champion_counts 保持原值)
- D-65-4: team_rounds round_name 默认初始化 (round_reach_counts[team] 不含 round_name)
- D-65-5: 进度日志触发 — sim=2000 + sim>0 走 logger.info (用 monkeypatch spy)

依赖 stub:
- core.model.monte_carlo_worldcup.simulate_world_cup (monkeypatch)
- core.model.monte_carlo_league.simulate_league (monkeypatch)
- 提前 import 子模块 (避免 _simulate_round 内 lazy import 抓不到 stub)
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
    """Spec-load scripts/core/model/monte_carlo.py (避免 scripts.core.__init__ 链)。

    该文件末尾有 re-export, 必须先成功加载核心模块才能 monkeypatch 子模块。
    """
    spec = importlib.util.spec_from_file_location(
        "monte_carlo_simulate_round_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-65 fixture helpers ────────────────────────


def _stub_worldcup(mc_mod, monkeypatch, return_value: dict):
    """Mock simulate_world_cup 返受控结果。"""
    from core.model import monte_carlo_worldcup as wc_mod
    monkeypatch.setattr(wc_mod, "simulate_world_cup",
                        lambda f, ts, rho: return_value)


def _stub_league(mc_mod, monkeypatch, return_value: dict):
    """Mock simulate_league 返受控结果。"""
    from core.model import monte_carlo_league as lg_mod
    monkeypatch.setattr(lg_mod, "simulate_league",
                        lambda f, ts, rho, initial: return_value)


# ──────────────────────── D-65 五契约 ────────────────────────


def test_simulate_round_d65_1_world_cup_route_aggregates_champion_and_rounds(mc_mod, monkeypatch):
    """R45-4 D-65-1: world_cup 路由 + champion 累加 + team_rounds 累加。

    tournament_type='world_cup' -> 调 simulate_world_cup -> 返 {champion, team_rounds},
    champion_counts['Brazil']: 0 -> 1, round_reach_counts['Brazil']['QF']: 0 -> 1。
    """
    _stub_worldcup(mc_mod, monkeypatch, {
        "champion": "Brazil",
        "team_rounds": {"Brazil": ["QF", "SF"]},
    })
    champion_counts = {"Brazil": 0, "Argentina": 0}
    round_reach_counts = {"Brazil": {}, "Argentina": {}}

    mc_mod._simulate_round(
        fixtures=[{"home": "Brazil", "away": "Argentina"}],
        team_strengths={"Brazil": {"attack": 1.5}, "Argentina": {"attack": 1.3}},
        rho=0.2,
        tournament_type="world_cup",
        sim=1,
        n_simulations=100,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert champion_counts["Brazil"] == 1
    assert champion_counts["Argentina"] == 0
    assert round_reach_counts["Brazil"]["QF"] == 1
    assert round_reach_counts["Brazil"]["SF"] == 1


def test_simulate_round_d65_2_default_league_route(mc_mod, monkeypatch):
    """R45-4 D-65-2: 默认 league 路由 (tournament_type != 'world_cup')。

    tournament_type='epl' (或任何非 'world_cup') -> 调 simulate_league。
    """
    _stub_league(mc_mod, monkeypatch, {
        "champion": "Arsenal",
        "team_rounds": {"Arsenal": ["Round_38"]},
    })
    champion_counts = {"Arsenal": 0}
    round_reach_counts = {"Arsenal": {}}

    mc_mod._simulate_round(
        fixtures=[{"home": "Arsenal", "away": "Chelsea"}],
        team_strengths={"Arsenal": {"attack": 1.5}},
        rho=0.2,
        tournament_type="epl",  # 非 world_cup -> league 路由
        sim=1,
        n_simulations=100,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert champion_counts["Arsenal"] == 1
    assert round_reach_counts["Arsenal"]["Round_38"] == 1


def test_simulate_round_d65_3_champion_none_no_aggregation(mc_mod, monkeypatch):
    """R45-4 D-65-3: champion=None/空 -> 不累加 (保持原值)。

    simulate_world_cup 返 {"champion": None, "team_rounds": {}}, _simulate_round L71
    `if champion:` 假 -> 跳过累加。champion_counts[team] 保持 0。
    """
    _stub_worldcup(mc_mod, monkeypatch, {
        "champion": None,
        "team_rounds": {},
    })
    champion_counts = {"Brazil": 5, "Argentina": 3}  # 预填非零
    round_reach_counts = {"Brazil": {}, "Argentina": {}}

    mc_mod._simulate_round(
        fixtures=[{"home": "Brazil", "away": "Argentina"}],
        team_strengths={"Brazil": {"attack": 1.5}},
        rho=0.2,
        tournament_type="world_cup",
        sim=1,
        n_simulations=100,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert champion_counts["Brazil"] == 5  # 不变
    assert champion_counts["Argentina"] == 3  # 不变


def test_simulate_round_d65_4_round_name_default_initialization(mc_mod, monkeypatch):
    """R45-4 D-65-4: round_reach_counts[team] 不含 round_name -> 默认 0 后 +1。

    team_rounds: {"Spain": ["QF"]}, round_reach_counts["Spain"] = {} -> QF 默认 0 -> +1 = 1。
    """
    _stub_worldcup(mc_mod, monkeypatch, {
        "champion": "Spain",
        "team_rounds": {"Spain": ["QF"]},
    })
    champion_counts = {"Spain": 0}
    round_reach_counts = {"Spain": {}}  # 预填空 dict 触发 L76 默认初始化

    mc_mod._simulate_round(
        fixtures=[{"home": "Spain", "away": "France"}],
        team_strengths={"Spain": {"attack": 1.5}},
        rho=0.2,
        tournament_type="world_cup",
        sim=1,
        n_simulations=100,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert round_reach_counts["Spain"]["QF"] == 1  # 默认 0 -> +1 = 1


def test_simulate_round_d65_5_progress_log_at_sim_2000(mc_mod, monkeypatch):
    """R45-4 D-65-5: 进度日志触发 — sim=2000 + sim>0 -> logger.info 被调。

    L59 `if sim % 2000 == 0 and sim > 0:` 触发 logger.info。
    spy core.log.logger.info 验证调用次数。
    """
    from core.log import logger as core_logger

    _stub_worldcup(mc_mod, monkeypatch, {
        "champion": "Brazil",
        "team_rounds": {},
    })
    # spy logger.info
    info_calls = []
    original_info = core_logger.info
    monkeypatch.setattr(core_logger, "info",
                        lambda msg, *a, **kw: info_calls.append(msg))

    champion_counts = {"Brazil": 0}
    round_reach_counts = {"Brazil": {}}

    # sim=2000 触发进度日志 (2000 % 2000 == 0 and 2000 > 0)
    mc_mod._simulate_round(
        fixtures=[{"home": "Brazil", "away": "Argentina"}],
        team_strengths={"Brazil": {"attack": 1.5}},
        rho=0.2,
        tournament_type="world_cup",
        sim=2000,
        n_simulations=10000,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert any("Simulation 2000" in str(m) for m in info_calls)

    # sim=1 不触发 (1 % 2000 != 0)
    info_calls.clear()
    mc_mod._simulate_round(
        fixtures=[{"home": "Brazil", "away": "Argentina"}],
        team_strengths={"Brazil": {"attack": 1.5}},
        rho=0.2,
        tournament_type="world_cup",
        sim=1,
        n_simulations=10000,
        champion_counts=champion_counts,
        round_reach_counts=round_reach_counts,
    )
    assert not any("Simulation" in str(m) for m in info_calls)
