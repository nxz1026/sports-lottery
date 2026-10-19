"""League（联赛）蒙特卡洛：剩余赛程 + 初始积分榜播种模拟单赛季。

源自 ``scripts/core/model/monte_carlo.py`` 的 2026-09-18 拆分（D1）。
原文件现在仅保留共享的 simulate_match_dc / _simulate_round / monte_carlo_champion
等入口与跨赛季公共工具；联赛语义（剩余赛程 + 积分榜）独立到本文件，
淘汰赛（小组赛 + 16 强对阵）独立到 monte_carlo_worldcup.py。

公共契约：
- build_league_standings(finished_matches) → 初始积分榜
- derive_team_strengths(finished_matches) → 每队进攻/防守/主客场 λ
- simulate_league(fixtures, team_strengths, rho, initial_standings=None) → 冠军 + final_standings

调用方（scripts/predict.py）已统一从 core.model.monte_carlo 引用；为保
持兼容，原模块对这些名字做 re-export。
"""
from __future__ import annotations

from typing import Any

from core.model.monte_carlo import simulate_match_dc


def build_league_standings(
    finished_matches: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """从已结束比赛推导当前积分榜（points/gf/ga/gd）。

    赛季中期做夺冠模拟必须播种既有积分：``simulate_league`` 只从传入的
    fixtures 建表，若只喂剩余赛程，所有球队都从 0 分起步，冠军概率是错的。
    """
    standings: dict[str, dict[str, int]] = {}
    for m in finished_matches:
        home = m.get("home") or ""
        away = m.get("away") or ""
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if not home or not away or hg is None or ag is None:
            continue
        for t in (home, away):
            if t not in standings:
                standings[t] = {"points": 0, "gf": 0, "ga": 0, "gd": 0}
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
    return standings


def derive_team_strengths(
    finished_matches: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """从已结束比赛推导每队的进攻/防守强度与主客场期望进球。

    乘性模型：``lambda_home = 联赛主场均进球 × 主队进攻 × 客队防守``。
    样本不足（联赛总进球为 0）时返回空 dict，调用方回退到默认强度。
    """
    played: dict[str, int] = {}
    scored: dict[str, int] = {}
    conceded: dict[str, int] = {}
    home_goals = away_goals = n_matches = 0

    for m in finished_matches:
        home = m.get("home") or ""
        away = m.get("away") or ""
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if not home or not away or hg is None or ag is None:
            continue
        n_matches += 1
        home_goals += hg
        away_goals += ag
        played[home] = played.get(home, 0) + 1
        played[away] = played.get(away, 0) + 1
        scored[home] = scored.get(home, 0) + hg
        scored[away] = scored.get(away, 0) + ag
        conceded[home] = conceded.get(home, 0) + ag
        conceded[away] = conceded.get(away, 0) + hg

    if n_matches == 0 or (home_goals + away_goals) == 0:
        return {}

    league_home_avg = home_goals / n_matches
    league_away_avg = away_goals / n_matches
    league_avg = (home_goals + away_goals) / (2 * n_matches)

    strengths: dict[str, dict[str, float]] = {}
    for team, n in played.items():
        if n == 0 or league_avg <= 0:
            continue
        attack = (scored.get(team, 0) / n) / league_avg
        defence = (conceded.get(team, 0) / n) / league_avg
        # 强度做温和收缩，避免小样本把极端值放大（n 越小越靠近 1.0）
        shrink = n / (n + 6.0)
        attack = 1.0 + (attack - 1.0) * shrink
        defence = 1.0 + (defence - 1.0) * shrink
        strengths[team] = {
            "attack": round(attack, 4),
            "defence": round(defence, 4),
            "lambda_home": round(league_home_avg * attack * defence, 4),
            "lambda_away": round(league_away_avg * attack * defence, 4),
            "matches": n,
        }
    return strengths


def simulate_league(
    fixtures: list[dict[str, Any]],
    team_strengths: dict[str, dict[str, float]],
    rho: float,
    initial_standings: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """模拟联赛剩余赛程，返回冠军。

    ``initial_standings`` 为已赛部分的积分（见 ``build_league_standings``），
    省略时全部球队从 0 分起步（等价于整季从头模拟）。
    """
    standings: dict[str, dict[str, int]] = {
        t: dict(v) for t, v in (initial_standings or {}).items()
    }

    for f in fixtures:
        home = f["home"]
        away = f["away"]

        if home not in standings:
            standings[home] = {"points": 0, "gf": 0, "ga": 0, "gd": 0}
        if away not in standings:
            standings[away] = {"points": 0, "gf": 0, "ga": 0, "gd": 0}

        # 有推导强度时用「联赛基准 × 主队进攻 × 客队防守」，否则回退默认
        sh = team_strengths.get(home) or {}
        sa = team_strengths.get(away) or {}
        if sh.get("attack") is not None and sa.get("defence") is not None:
            lh = sh.get("lambda_home", 1.5) * sa["defence"]
        else:
            lh = sh.get("lambda_home", 1.5)
        if sa.get("attack") is not None and sh.get("defence") is not None:
            la = sa.get("lambda_away", 1.2) * sh["defence"]
        else:
            la = sa.get("lambda_away", 1.2)

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

    champion = sorted_teams[0] if sorted_teams else None

    # 联赛是单循环积分制，没有"晋级轮次"概念。此前这里硬编码
    # team_rounds[team] = ["season"]，导致 API 的 round_reach_probs.season
    # 对每支球队恒为 1.0（3000/3000 次模拟都"到达 season"），语义空洞。
    # 轮次晋级仅对锦标赛（world_cup 路径 _simulate_group_stage）有意义。
    team_rounds: dict[str, list[str]] = {}

    return {
        "champion": champion,
        "team_rounds": team_rounds,
        "final_standings": [
            {"team": t, "points": standings[t]["points"],
             "gd": standings[t]["gd"], "gf": standings[t]["gf"]}
            for t in sorted_teams
        ],
    }


__all__ = [
    "build_league_standings",
    "derive_team_strengths",
    "simulate_league",
]
