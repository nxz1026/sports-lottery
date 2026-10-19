"""Onside 4+1 signal model: FIFA rankings, league footprint, host advantage, confederation strength."""

from __future__ import annotations

import math
from typing import Any

from core.config import CONFEDERATION_STRENGTH, COUNTRY_CONFEDERATION, ONSIDE_WEIGHTS, THRESHOLDS


def fifa_rank_to_score(rank: int | None, max_rank: int = 200) -> float:
    """FIFA 排名 → [0,1] 分数。

    P2-A 修复: 衰减系数从 3.0 提升到 4.5，
    使 top 10 与 rank 11-30 之间区分度更显著。
    新映射: rank 1→0.975, rank 10→0.80, rank 50→0.32, rank 100→0.08
    """
    if not rank or rank <= 0:
        return 0.5
    rank = min(rank, max_rank)
    # 更陡峭的衰减：top 20 占据大部分分数区间
    score = math.exp(-4.5 * (rank - 1) / max_rank)
    return max(0.0, min(1.0, score))


def get_confederation(country_name: str) -> str:
    return COUNTRY_CONFEDERATION.get(country_name, "UEFA")


def confederation_score(country_name: str) -> float:
    conf = get_confederation(country_name)
    return CONFEDERATION_STRENGTH.get(conf, 0.5)


def league_footprint_score(country_name: str, fifa_rank: int | None = None) -> float:
    if fifa_rank and fifa_rank > 0:
        if fifa_rank <= 10:
            return 1.0
        elif fifa_rank <= 30:
            return 0.8
        elif fifa_rank <= 60:
            return 0.6
        elif fifa_rank <= 100:
            return 0.4
        else:
            return 0.2
    conf = get_confederation(country_name)
    if conf in ("UEFA", "CONMEBOL"):
        return 0.7
    elif conf in ("AFC", "CAF", "CONCACAF"):
        return 0.5
    else:
        return 0.3


def host_advantage_score(country_name: str, host_country: str | None) -> float:
    if host_country and country_name == host_country:
        return 1.0
    return 0.5


def _elo_to_approx_rank(elo: float) -> int:
    """ELO 评分 → 近似 FIFA 排名（用于无 FIFA 排名的球队）。

    ELO 1500(默认) → rank 200, ELO 1800 → rank ~100, ELO 2100 → rank ~1
    """
    r = 200 - (elo - 1500.0) * (199.0 / 600.0)
    return max(1, int(round(r)))


def _onside_weighted_score(fifa_score: float, league_score: float, host_score: float, conf_score: float) -> float:
    """按 ONSIDE_WEIGHTS 加权求和单边 Onside 得分（与原 compute_onside_signals 内联计算一致）。"""
    w = ONSIDE_WEIGHTS
    return (
        fifa_score * w["fifa_ranking"]
        + league_score * w["league_footprint"]
        + host_score * w["host_advantage"]
        + conf_score * w["confederation"]
    )


def compute_onside_signals(home_team: str, away_team: str, fifa_rankings: dict[str, int], host_country: str | None = None, elo_ratings: dict[str, float] | None = None) -> dict[str, Any]:
    fifa_default = THRESHOLDS.get("fifa_rank_default", 200)
    home_rank = fifa_rankings.get(home_team, fifa_default)
    away_rank = fifa_rankings.get(away_team, fifa_default)

    # 俱乐部无 FIFA 排名时用 ELO 推算近似排名
    if home_rank == fifa_default and elo_ratings:
        home_elo = elo_ratings.get(home_team, 1500.0)
        home_rank = _elo_to_approx_rank(home_elo)
    if away_rank == fifa_default and elo_ratings:
        away_elo = elo_ratings.get(away_team, 1500.0)
        away_rank = _elo_to_approx_rank(away_elo)

    home_fifa = fifa_rank_to_score(home_rank)
    away_fifa = fifa_rank_to_score(away_rank)

    home_league = league_footprint_score(home_team, home_rank)
    away_league = league_footprint_score(away_team, away_rank)

    home_host = host_advantage_score(home_team, host_country)
    away_host = host_advantage_score(away_team, host_country)

    home_conf = confederation_score(home_team)
    away_conf = confederation_score(away_team)

    home_onside = _onside_weighted_score(home_fifa, home_league, home_host, home_conf)
    away_onside = _onside_weighted_score(away_fifa, away_league, away_host, away_conf)

    return {
        "home": {
            "fifa_rank": home_rank,
            "fifa_score": round(home_fifa, 3),
            "league_footprint": round(home_league, 3),
            "host_advantage": round(home_host, 3),
            "confederation": round(home_conf, 3),
            "onside_score": round(home_onside, 3),
        },
        "away": {
            "fifa_rank": away_rank,
            "fifa_score": round(away_fifa, 3),
            "league_footprint": round(away_league, 3),
            "host_advantage": round(away_host, 3),
            "confederation": round(away_conf, 3),
            "onside_score": round(away_onside, 3),
        }
    }
