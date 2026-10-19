"""Monte Carlo 共享层：单场 DC 采样 + 单轮分发 + 顶层 champion 入口 + 收敛性 SE。

2026-09-18 D1 拆分：本模块只保留与"联赛 / 锦标赛"语义无关的共享部分；
联赛语义（剩余赛程 + 积分榜播种）迁到 ``monte_carlo_league.py``，
锦标赛语义（小组赛 + 标准 16 强对阵 + 退化配对）迁到 ``monte_carlo_worldcup.py``。

调用方（``scripts/predict.py``、``tests/test_p0_window_mc_stablekey.py``、
``scripts/core/__init__.py``、``scripts/core/model/__init__.py``）继续从
``core.model.monte_carlo`` 引用；本文件末尾对 league / worldcup 子模块的
公共函数做 re-export 以保持向后兼容。
"""
from __future__ import annotations

import math
from typing import Any

from core.log import logger


def simulate_match_dc(lambda_h: float, lambda_a: float, rho: float = 0.2) -> tuple[int, int]:
    """用 Dixon-Coles 联合泊松模拟单场比分"""
    from core.config import MAX_GOALS_MC
    from core.model.poisson import dixon_coles_pmf

    _max_g = MAX_GOALS_MC  # 统一范围常量（与 poisson.py dixon_coles_match_probs 一致）
    probs: list[tuple[int, int, float]] = []
    for h in range(_max_g):
        for a in range(_max_g):
            p = dixon_coles_pmf(h, a, lambda_h, lambda_a, rho)
            probs.append((h, a, p))

    total_p = sum(p[2] for p in probs)
    if total_p > 0:
        probs = [(h, a, p / total_p) for h, a, p in probs]

    import random
    r = random.random()
    cumulative = 0.0
    for h, a, p in probs:
        cumulative += p
        if r <= cumulative:
            return h, a

    return 0, 0


def _simulate_round(
    fixtures: list[dict[str, Any]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    tournament_type: str,
    sim: int,
    n_simulations: int,
    champion_counts: dict[str, int],
    round_reach_counts: dict[str, dict[str, int]],
    initial_standings: dict[str, dict[str, int]] | None = None,
) -> None:
    """模拟一轮锦标赛，原地聚合冠军与晋级轮次计数（随机数消耗顺序不变）。"""
    if sim % 2000 == 0 and sim > 0:
        logger.info(f"  Simulation {sim}/{n_simulations}...")

    # 局部 import 避免顶层耦合 + 给两份子模块清晰的拆分面
    if tournament_type == "world_cup":
        from core.model.monte_carlo_worldcup import simulate_world_cup
        result = simulate_world_cup(fixtures, team_strengths, rho)
    else:
        from core.model.monte_carlo_league import simulate_league
        result = simulate_league(fixtures, team_strengths, rho, initial_standings)

    champion = result.get("champion")
    if champion:
        champion_counts[champion] = champion_counts.get(champion, 0) + 1

    for team, rounds in result.get("team_rounds", {}).items():
        for round_name in rounds:
            if round_name not in round_reach_counts[team]:
                round_reach_counts[team][round_name] = 0
            round_reach_counts[team][round_name] += 1


def _compute_se(champion_counts: dict[str, int], n_simulations: int) -> dict[str, Any]:
    """计算每个队伍冠军概率的标准误（P2-C）。

    SE = sqrt(p_hat * (1 - p_hat) / n)
    用于判断模拟次数是否足够（SE < 0.01 为理想精度）。
    """
    diagnostics: dict[str, Any] = {}
    for team, count in champion_counts.items():
        p = count / n_simulations
        se = math.sqrt(p * (1 - p) / n_simulations)
        diagnostics[team] = {
            "se": round(se, 4),
            "ci_95_low": round(max(0, p - 1.96 * se), 4),
            "ci_95_high": round(min(1, p + 1.96 * se), 4),
        }
    return diagnostics


def monte_carlo_champion(
    fixtures: list[dict[str, Any]],
    team_strengths: dict[str, dict[str, float]],
    n_simulations: int = 10000,
    rho: float = 0.2,
    tournament_type: str = "world_cup",
    initial_standings: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    logger.info(f"Starting Monte Carlo simulation: {n_simulations} iterations, type={tournament_type}")

    champion_counts: dict[str, int] = {}
    round_reach_counts: dict[str, dict[str, int]] = {}

    all_teams: set[str] = set()
    for f in fixtures:
        all_teams.add(f.get("home", ""))
        all_teams.add(f.get("away", ""))
    # 播种积分榜里的球队即使不在剩余赛程中也要进榜（否则概率分母漏队）
    for t in (initial_standings or {}):
        all_teams.add(t)

    for team in all_teams:
        champion_counts[team] = 0
        round_reach_counts[team] = {}

    for sim in range(n_simulations):
        _simulate_round(fixtures, team_strengths, rho, tournament_type, sim, n_simulations,
                        champion_counts, round_reach_counts, initial_standings)

    # 保留 0 概率球队：冠军页要展示完整参赛队伍，而不是只列"模拟中赢过"的
    # （此前 `if count > 0` 会把弱队整个滤掉，英超 20 队只显示 17 队）
    champion_probs = {team: round(count / n_simulations, 4)
                      for team, count in champion_counts.items()}
    champion_probs = dict(sorted(champion_probs.items(), key=lambda x: -x[1]))

    round_reach_probs: dict[str, dict[str, float]] = {}
    for team, rounds in round_reach_counts.items():
        for round_name, count in rounds.items():
            if round_name not in round_reach_probs:
                round_reach_probs[round_name] = {}
            round_reach_probs[round_name][team] = round(count / n_simulations, 4)

    for round_name in round_reach_probs:
        round_reach_probs[round_name] = dict(
            sorted(round_reach_probs[round_name].items(), key=lambda x: -x[1])
        )

    return {
        "champion_probs": champion_probs,
        "round_reach_probs": round_reach_probs,
        "simulation_count": n_simulations,
        "model": "dixon_coles",
        "rho": rho,
        # P2-C 新增: 收敛性诊断 — 标准误估计
        "convergence_diagnostics": _compute_se(champion_counts, n_simulations),
    }


# ── 向后兼容 re-export：拆分前的调用方仍可直接 ``from core.model.monte_carlo import X`` ──
from core.model.monte_carlo_league import (  # noqa: E402,F401
    build_league_standings,
    derive_team_strengths,
    simulate_league,
)
from core.model.monte_carlo_worldcup import simulate_world_cup  # noqa: E402,F401

__all__ = [
    "simulate_match_dc",
    "monte_carlo_champion",
    "build_league_standings",
    "derive_team_strengths",
    "simulate_league",
    "simulate_world_cup",
]
