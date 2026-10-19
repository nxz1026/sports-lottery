"""World Cup / 锦标赛蒙特卡洛：小组赛 + 16 强 + 8 强 + 半决赛 + 决赛。

源自 ``scripts/core/model/monte_carlo.py`` 的 2026-09-18 拆分（D1）。
原文件现在仅保留共享入口；锦标赛语义（小组分组 + 标准 16 强对阵 +
退化到顺序配对的兜底）独立到本文件，联赛语义在 monte_carlo_league.py。

公共契约：
- simulate_world_cup(fixtures, team_strengths, rho) → champion + team_rounds

淘汰赛按标准 World Cup 对阵表（8 组 × 4 队，A1vB2 / C1vD2 ...）。组数不足
8 组（种子不足 16 强）时退回顺序配对 _simulate_knockout_sequential，
不返回 None 冠军（修复：此前该分支恒不可达）。
"""
from __future__ import annotations

import random
from typing import Any

from core.log import logger
from core.model.monte_carlo import simulate_match_dc


def _simulate_group_stage(
    fixtures: list[dict[str, Any]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """模拟小组赛：分组、逐场比分、排名，返回 (group_standings, team_rounds)。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    knockout: list[dict[str, Any]] = []

    for f in fixtures:
        stage = f.get("stage", "group")
        if stage == "group":
            group_name = f.get("group", "A")
            if group_name not in groups:
                groups[group_name] = []
            groups[group_name].append(f)
        else:
            knockout.append(f)

    group_standings: dict[str, list[str]] = {}
    team_rounds: dict[str, list[str]] = {}

    for group_name, group_fixtures in groups.items():
        group_standings[group_name] = _simulate_group(group_fixtures, team_strengths, rho, team_rounds)

    return group_standings, team_rounds


def _simulate_group(
    group_fixtures: list[dict[str, Any]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    team_rounds: dict[str, list[str]],
) -> list[str]:
    """模拟单个小组全部比赛，记录轮次，返回组内排名（晋级取前 2）。"""
    teams_in_group: set[str] = set()
    for f in group_fixtures:
        teams_in_group.add(f["home"])
        teams_in_group.add(f["away"])

    standings: dict[str, dict[str, int]] = {team: {"points": 0, "gf": 0, "ga": 0, "gd": 0} for team in teams_in_group}

    for f in group_fixtures:
        home = f["home"]
        away = f["away"]

        lh = team_strengths.get(home, {}).get("lambda_home", 1.5)
        la = team_strengths.get(away, {}).get("lambda_away", 1.2)

        hg, ag = simulate_match_dc(lh, la, rho)

        standings[home]["gf"] += hg
        standings[home]["ga"] += ag
        standings[home]["gd"] += hg - ag
        standings[away]["gf"] += ag
        standings[away]["ga"] += hg
        standings[away]["gd"] += ag - hg

        if hg > ag:
            standings[home]["points"] += 3
        elif hg == ag:
            standings[home]["points"] += 1
            standings[away]["points"] += 1
        else:
            standings[away]["points"] += 3

    sorted_teams = sorted(standings.keys(),
                          key=lambda t: (standings[t]["points"], standings[t]["gd"], standings[t]["gf"]),
                          reverse=True)

    _record_group_rounds(team_rounds, teams_in_group, sorted_teams)

    return sorted_teams


def _record_group_rounds(
    team_rounds: dict[str, list[str]],
    teams_in_group: set[str],
    sorted_teams: list[str],
) -> None:
    """记录小组赛与晋级 16 强轮次（原地写回 team_rounds）。"""
    for team in teams_in_group:
        if team not in team_rounds:
            team_rounds[team] = []
        team_rounds[team].append("group_stage")

    advanced = sorted_teams[:2]
    for team in advanced:
        if team not in team_rounds:
            team_rounds[team] = []
        team_rounds[team].append("round_of_16")


def _build_r16_matchups(seeded: list[str]) -> list[tuple[str, str]]:
    """按标准 16 强对阵表配对种子队伍。"""
    # ── 标准 World Cup 淘汰赛对阵 ──
    # 8 组 (A-H)，每组前2名晋级，16强对阵：
    # A1 v B2, C1 v D2, E1 v F2, G1 v H2,
    # B1 v A2, D1 v C2, F1 v E2, H1 v G2
    _KNOCKOUT_PAIRING = [
        (0, 1),  # A1 vs B2
        (2, 3),  # C1 vs D2
        (4, 5),  # E1 vs F2
        (6, 7),  # G1 vs H2
        (1, 0),  # B1 vs A2
        (3, 2),  # D1 vs C2
        (5, 4),  # F1 vs E2
        (7, 6),  # H1 vs G2
    ]

    r16_matchups: list[tuple[str, str]] = []
    for first_idx, second_idx in _KNOCKOUT_PAIRING:
        # first_idx: 组号（0=A,1=B...），取该组第1名
        # second_idx: 组号，取该组第2名
        t1 = seeded[first_idx * 2]      # 组 first_idx 的第1名
        t2 = seeded[second_idx * 2 + 1]  # 组 second_idx 的第2名
        r16_matchups.append((t1, t2))

    return r16_matchups


def _assemble_knockout(
    group_standings: dict[str, list[str]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    team_rounds: dict[str, list[str]],
) -> list[str]:
    """装配淘汰赛种子并按对阵模拟，返回剩余队伍列表（冠军为首）。"""
    remaining_teams: list[str] = []

    # 构建淘汰赛队伍列表：[A1, A2, B1, B2, C1, C2, ...]
    group_order = sorted(group_standings.keys())
    seeded: list[str] = []
    for gn in group_order:
        advanced = group_standings[gn][:2]
        seeded.extend(advanced)

    if len(seeded) >= 16:
        remaining_teams = _simulate_knockout_bracket(_build_r16_matchups(seeded), team_strengths, rho, team_rounds)
    elif len(seeded) >= 2:
        # 组数不足 8 组（种子不足 16 强）时，退化为顺序配对：
        # 汇总所有组的晋级队，按顺序两两淘汰（修复：此前 remaining_teams
        # 永远为空列表，该分支恒不可达，冠军静默为 None）
        for group_name in sorted(group_standings.keys()):
            advanced = group_standings[group_name][:2]
            remaining_teams.extend(advanced)
        remaining_teams = _simulate_knockout_sequential(remaining_teams, team_strengths, rho, team_rounds)
    else:
        remaining_teams = []
        logger.warning("simulate_world_cup: 晋级队伍不足 2 支，无法模拟淘汰赛，冠军为 None")

    return remaining_teams


def simulate_world_cup(fixtures: list[dict[str, Any]], team_strengths: dict[str, dict[str, float]], rho: float) -> dict[str, Any]:
    group_standings, team_rounds = _simulate_group_stage(fixtures, team_strengths, rho)
    remaining_teams = _assemble_knockout(group_standings, team_strengths, rho, team_rounds)

    champion = remaining_teams[0] if remaining_teams else None

    return {
        "champion": champion,
        "team_rounds": team_rounds,
    }


def _simulate_knockout_bracket(
    matchups: list[tuple[str, str]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    team_rounds: dict[str, list[str]],
) -> list[str]:
    """按给定对阵表模拟淘汰赛，返回冠军队伍列表。"""
    round_names = {16: "round_of_16", 8: "quarter_final", 4: "semi_final", 2: "final"}

    current_matchups = matchups
    while len(current_matchups) >= 1:
        n_teams = len(current_matchups) * 2
        round_name = round_names.get(n_teams, f"r{n_teams}")
        next_matchups: list[tuple[str, str]] = []

        for t1, t2 in current_matchups:
            lh = team_strengths.get(t1, {}).get("lambda_home", 1.5)
            la = team_strengths.get(t2, {}).get("lambda_away", 1.2)
            hg, ag = simulate_match_dc(lh, la, rho)

            if hg == ag:
                winner = t1 if random.random() < 0.5 else t2
            elif hg > ag:
                winner = t1
            else:
                winner = t2

            if winner not in team_rounds:
                team_rounds[winner] = []
            team_rounds[winner].append(round_name)

            # 将胜者配对进入下一轮
            if not next_matchups or len(next_matchups[-1]) == 2:
                next_matchups.append((winner,))
            else:
                next_matchups[-1] = (next_matchups[-1][0], winner)

        # 如果只剩1场（决赛），冠军已决出
        if len(current_matchups) == 1:
            return [current_matchups[0][0] if isinstance(current_matchups[0], tuple) and len(current_matchups[0]) == 1 else winner]

        current_matchups = next_matchups

    return []


def _simulate_knockout_sequential(
    remaining_teams: list[str],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    team_rounds: dict[str, list[str]],
) -> list[str]:
    """顺序配对模拟淘汰赛（组数不足8组时的退化方案）。"""
    while len(remaining_teams) > 1:
        next_round_teams: list[str] = []
        round_name = f"r{len(remaining_teams)}"
        for i in range(0, len(remaining_teams), 2):
            if i + 1 < len(remaining_teams):
                t1 = remaining_teams[i]
                t2 = remaining_teams[i + 1]

                lh = team_strengths.get(t1, {}).get("lambda_home", 1.5)
                la = team_strengths.get(t2, {}).get("lambda_away", 1.2)

                hg, ag = simulate_match_dc(lh, la, rho)

                if hg == ag:
                    winner = t1 if random.random() < 0.5 else t2
                elif hg > ag:
                    winner = t1
                else:
                    winner = t2

                next_round_teams.append(winner)

                if winner not in team_rounds:
                    team_rounds[winner] = []
                team_rounds[winner].append(round_name)
            else:
                next_round_teams.append(remaining_teams[i])

        remaining_teams = next_round_teams

    return remaining_teams


__all__ = [
    "simulate_world_cup",
    "_simulate_group_stage",
    "_simulate_group",
    "_record_group_rounds",
    "_build_r16_matchups",
    "_assemble_knockout",
    "_simulate_knockout_bracket",
    "_simulate_knockout_sequential",
]
