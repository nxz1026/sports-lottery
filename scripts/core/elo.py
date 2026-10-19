"""ELO Dynamic Rating System (P4-1).

场级更新的 ELO 评分系统，替代静态 FIFA 月级排名。
支持主客场调整、目标得分期望（GDA）、多联赛适配。
P0-D 升级: 增加 ELO ratings 持久化（save/load），避免每次冷启动。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from core.log import logger

# ── ELO 常量 ──
K_FACTOR: float = 20.0           # 基础 K 值（可按联赛/赛事类型调）
HOME_ADVANTAGE_ELO: float = 100.0  # 主场加成（ELO 分）
DEFAULT_ELO: float = 1500.0      # 新球队默认 ELO
SCALE_FACTOR: float = 400.0      # ELO 公式分母

# ════════════════════════════════════════════════
# P0-D 新增: ELO 持久化路径
# 每次 process_match_result 批量更新后应调用 save_elo_ratings()
# 下次运行时用 load_elo_ratings() 恢复，避免从 FIFA 冷启动
# ════════════════════════════════════════════════
_ELO_STATE_DIR = Path(__file__).resolve().parent.parent.parent / "references"
_ELO_STATE_FILE = _ELO_STATE_DIR / ".elo_ratings.json"


def expected_score(elo_a: float, elo_b: float, home_adv: bool = False) -> float:
    """计算 A 队对 B 队的期望得分。

    Args:
        elo_a: A 队当前 ELO
        elo_b: B 队当前 ELO
        home_adv: 是否 A 队为主队（+100 加成）

    Returns:
        A 队期望得分 [0, 1]
    """
    effective_a = elo_a + (HOME_ADVANTAGE_ELO if home_adv else 0)
    return 1.0 / (1.0 + 10 ** ((elo_b - effective_a) / SCALE_FACTOR))


def update_elo(
    elo_a: float,
    elo_b: float,
    actual_score_a: float,
    k: float = K_FACTOR,
    home_adv: bool = False,
) -> tuple[float, float]:
    """根据比赛结果更新双方 ELO。

    Args:
        elo_a: A 阵赛前 ELO
        elo_b: B 阵赛前 ELO
        actual_score_a: A 队实际得分（胜=1, 平=0.5, 负=0）
        k: K 因子
        home_adv: A 队是否为主队

    Returns:
        (new_elo_a, new_elo_b)
    """
    exp_a = expected_score(elo_a, elo_b, home_adv)
    exp_b = 1.0 - exp_a
    actual_b = 1.0 - actual_score_a

    new_a = elo_a + k * (actual_score_a - exp_a)
    new_b = elo_b + k * (actual_b - exp_b)
    return round(new_a, 1), round(new_b, 1)


def goal_difference_adjustment(
    goal_diff: int,
    base_k: float = K_FACTOR,
) -> float:
    """根据净胜球数动态调整 K 值。

    大比分胜利获得更多 ELO 收益。
    """
    if goal_diff <= 0:
        return base_k
    elif goal_diff == 1:
        return base_k * 1.0
    elif goal_diff == 2:
        return base_k * 1.5
    else:
        return base_k * (1.75 + 0.125 * (goal_diff - 3))


def _elo_apply_result(
    home_team: str,
    away_team: str,
    home_goals: int,
    away_goals: int,
    ratings: dict[str, float],
    k: float,
) -> tuple[float, float, float, float, float]:
    """推导单场结果的双队 ELO 更新，结果就地写回 ratings。

    Returns:
        元组 (elo_h, elo_a, new_h, new_a, adj_k)：更新前双方分数、更新后分数与所用 K 值。
    """
    elo_h = ratings.get(home_team, DEFAULT_ELO)
    elo_a = ratings.get(away_team, DEFAULT_ELO)

    if home_goals > away_goals:
        score_h = 1.0
    elif home_goals < away_goals:
        score_h = 0.0
    else:
        score_h = 0.5

    goal_diff = abs(home_goals - away_goals)
    adj_k = goal_difference_adjustment(goal_diff, k)

    new_h, new_a = update_elo(elo_h, elo_a, score_h, adj_k, home_adv=True)

    ratings[home_team] = new_h
    ratings[away_team] = new_a
    return elo_h, elo_a, new_h, new_a, adj_k


def process_match_result(
    home_team: str,
    away_team: str,
    home_goals: int,
    away_goals: int,
    ratings: dict[str, float],
    k: float = K_FACTOR,
) -> dict[str, Any]:
    """处理单场比赛结果，更新 ELO 评分表。

    Args:
        home_team: 主队名
        away_team: 客队名
        home_goals: 主队进球
        away_goals: 客队进球
        ratings: 当前 {team_name: elo} 字典（会被就地修改）
        k: 基础 K 因子

    Returns:
        变更详情字典
    """
    elo_h, elo_a, new_h, new_a, adj_k = _elo_apply_result(
        home_team, away_team, home_goals, away_goals, ratings, k)

    change = {
        "match": f"{home_team} {home_goals}-{away_goals} {away_team}",
        "home_elo_before": elo_h,
        "away_elo_before": elo_a,
        "home_elo_after": new_h,
        "away_elo_after": new_a,
        "home_elo_change": round(new_h - elo_h, 1),
        "away_elo_change": round(new_a - elo_a, 1),
        "k_used": round(adj_k, 1),
    }
    logger.debug(f"ELO update: {change['match']} → H{change['home_elo_change']:+.0f} A{change['away_elo_change']:+.0f}")
    return change


def elo_to_lambda_scale(elo: float, min_lam: float = 0.8, max_lam: float = 3.0) -> float:
    """将 ELO 评分映射到泊松 λ 参数范围。

    使用 sigmoid 映射：ELO 范围 [1000, 2200] → λ [min_lam, max_lam]
    """
    center = 1600.0
    width = 400.0
    ratio = 1.0 / (1.0 + math.exp(-(elo - center) / width))
    return min_lam + ratio * (max_lam - min_lam)


def init_ratings_from_fifa(fifa_rankings: dict[str, int]) -> dict[str, float]:
    """从 FIFA 排名初始化 ELO 评分表。

    前 10 名 ≈ 1800-2000，中间 ≈ 1500-1700，末尾 ≈ 1200-1400。
    """
    ratings: dict[str, float] = {}
    for team, rank in fifa_rankings.items():
        # 线性映射：rank 1 → 1950, rank 200 → 1250
        elo = 1950 - min(rank, 200) * 3.5
        elo = max(1200.0, min(2050.0, elo))
        ratings[team] = round(elo, 1)
    logger.info(f"Initialized ELO ratings for {len(ratings)} teams from FIFA rankings")
    return ratings


# ════════════════════════════════════════════════
# P0-D 新增: ELO ratings 持久化 (save / load)
# 解决每次从 FIFA 冷启动导致动态评分失效的问题
# ════════════════════════════════════════════════


def save_elo_ratings(ratings: dict[str, float]) -> None:
    """持久化 ELO 评分表到 JSON 文件。

    在批量 process_match_result() 后调用，确保下次运行可恢复最新状态。
    """
    try:
        _ELO_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ratings": {k: v for k, v in sorted(ratings.items(), key=lambda x: -x[1])},
            "_saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "_team_count": len(ratings),
        }
        with open(_ELO_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved ELO ratings for {len(ratings)} teams to {_ELO_STATE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save ELO ratings: {e}")


def load_elo_ratings() -> dict[str, float] | None:
    """从 JSON 文件加载上一次的 ELO 评分表。

    Returns:
        {team_name: elo} 字典，若文件不存在或读取失败则返回 None。
    """
    import json as _json
    try:
        if not _ELO_STATE_FILE.exists():
            logger.debug("No persisted ELO state file found, will initialize from FIFA")
            return None
        with open(_ELO_STATE_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        ratings = data.get("ratings", {})
        if not ratings or not isinstance(ratings, dict):
            return None
        # 验证数据完整性：所有值应为数值
        validated = {k: float(v) for k, v in ratings.items() if isinstance(v, (int, float))}
        saved_at = data.get("_saved_at", "?")
        logger.info(f"Loaded ELO ratings for {len(validated)} teams from {_ELO_STATE_FILE} (saved={saved_at})")
        return validated
    except (_json.JSONDecodeError, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load ELO ratings from {_ELO_STATE_FILE}: {e}")
        return None


def get_or_init_elo_ratings(fifa_rankings: dict[str, int] | None = None,
                             force_refresh: bool = False) -> dict[str, float]:
    """获取 ELO 评分表：优先加载持久化，否则从 FIFA 初始化。

    Args:
        fifa_rankings: FIFA 排名字典（冷启动时需要）
        force_refresh: 强制忽略缓存，重新从 FIFA 初始化

    Returns:
        {team_name: elo} 字典
    """
    if not force_refresh:
        cached = load_elo_ratings()
        if cached and len(cached) > 10:
            return cached

    if fifa_rankings is None:
        from core.rankings import fetch_fifa_rankings
        fifa_rankings = fetch_fifa_rankings()

    ratings = init_ratings_from_fifa(fifa_rankings)
    # 立即保存初始状态，避免下次再冷启动
    save_elo_ratings(ratings)
    return ratings
