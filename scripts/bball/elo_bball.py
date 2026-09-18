"""篮球 ELO 评分持久化与更新。"""

import json
from json import JSONDecodeError

from .config_bball import ELO, ELO_FILE, TEAM_CN, TEAMS_SEED_FILE


def _seed_ratings() -> dict[str, float]:
    try:
        seed = json.loads(TEAMS_SEED_FILE.read_text(encoding="utf-8"))
    except (JSONDecodeError, OSError):
        seed = list(TEAM_CN)
    teams = seed.keys() if isinstance(seed, dict) else seed
    return {str(team): float(ELO["INITIAL"]) for team in teams if isinstance(team, str)}


def load_ratings() -> dict[str, float]:
    """读取评分；文件异常时用球队种子并持久化初始评分。"""
    try:
        ratings = json.loads(ELO_FILE.read_text(encoding="utf-8"))
        if isinstance(ratings, dict):
            return {str(team): float(value) for team, value in ratings.items()}
    except (JSONDecodeError, OSError, TypeError, ValueError):
        pass
    ratings = _seed_ratings()
    save_ratings(ratings)
    return ratings


def save_ratings(ratings: dict[str, float]) -> None:
    """保存球队 ELO 评分。"""
    ELO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ELO_FILE.write_text(json.dumps(ratings, indent=2), encoding="utf-8")


def expectation(rh: float, ra: float) -> float:
    """按 ELO 标准分母计算主队期望胜率。"""
    return 1 / (1 + 10 ** ((ra - rh) / ELO["SCALE"]))


def _update_match(ratings: dict[str, float], home: str, away: str, home_win: bool) -> None:
    k = ELO["K"]
    rh = ratings.get(home, ELO["INITIAL"]) + ELO["HOME_ADV"]
    ra = ratings.get(away, ELO["INITIAL"])
    expected = expectation(rh, ra)
    actual = 1.0 if home_win else 0.0
    ratings[home] = ratings.get(home, ELO["INITIAL"]) + k * (actual - expected)
    ratings[away] = ratings.get(away, ELO["INITIAL"]) + k * ((1 - actual) - (1 - expected))


def update_ratings(ratings: dict[str, float], completed: list[dict]) -> int:
    """根据已完赛比分逐场更新 ELO，返回更新场数。"""
    updated = 0
    for game in completed:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        scores = game.get("scores", {})
        if not isinstance(scores, dict):
            continue
        home_score = scores.get(home)
        away_score = scores.get(away)
        if home and away and home_score is not None and away_score is not None:
            _update_match(ratings, home, away, home_score > away_score)
            updated += 1
    return updated


def apply_regression(ratings: dict[str, float]) -> int:
    """让休赛期评分按固定回归系数靠近初始值。"""
    initial = ELO["INITIAL"]
    regression = ELO["REGRESSION"]
    for team in list(ratings):
        ratings[team] = initial + regression * (ratings[team] - initial)
    return len(ratings)
