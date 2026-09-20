"""篮球比赛预测纯逻辑。"""

import math

from .config_bball import ELO, PREDICT, TEAM_CN, TEAM_CN_ALIASES
from .elo_bball import expectation


def _to_cn(name: str) -> str:
    return TEAM_CN.get(name, TEAM_CN_ALIASES.get(name, name))


def _implied_probability(close_odds: float | None, implied_home: float | None) -> float | None:
    if implied_home is not None and 0 < implied_home < 1:
        return implied_home
    if close_odds is not None and close_odds > 0:
        return 1 / close_odds
    return None


def _score_projection(
    rh: float,
    ra: float,
    market_probability: float | None,
) -> tuple[float, float, float, float]:
    if market_probability is not None and 0 < market_probability < 1:
        logit = math.log(market_probability / (1 - market_probability))
        margin = max(-20, min(20, logit * 14))
    else:
        margin = (rh - ra) * PREDICT["ELO_TO_POINTS"] + PREDICT["HOME_SCORE_ADV"]
    home_score = PREDICT["AVG_PPG"] + margin / 2
    away_score = PREDICT["AVG_PPG"] - margin / 2
    return home_score, away_score, margin, home_score + away_score


def _bet_directions(
    home: str,
    away: str,
    win_prob: float,
    confidence: float,
    margin: float,
    total: float,
    spread_line: float | None,
    total_line: float | None,
) -> tuple[str, str, str | None, str | None]:
    winner = _to_cn(home) if win_prob > 0.5 else _to_cn(away)
    direction = f"{winner} 胜"
    stars = "3-star" if confidence >= 0.5 else "2-star" if confidence >= 0.3 else "1-star"
    spread = None
    if spread_line is not None and confidence >= PREDICT["SPREAD_CONFIDENCE"]:
        cover = "主" if margin > -spread_line else "客"
        spread = f"{cover}({margin + spread_line:+.1f})"
    total_pred = None
    if total_line is not None and confidence >= PREDICT["TOTAL_CONFIDENCE"]:
        side = "大" if total > total_line else "小"
        total_pred = f"{side}(+{abs(total - total_line):.1f})"
    return direction, stars, spread, total_pred


def predict_game(
    home: str,
    away: str,
    ratings: dict[str, float],
    spread_line: float | None,
    total_line: float | None,
    close_odds: float | None,
    implied_home: float | None,
) -> dict:
    """生成单场预测及盘口方向。"""
    rh = ratings.get(home, ELO["INITIAL"]) + ELO["HOME_ADV"]
    ra = ratings.get(away, ELO["INITIAL"])
    elo_probability = expectation(rh, ra)
    market_probability = _implied_probability(close_odds, implied_home)
    win_probability = (
        0.3 * elo_probability + 0.7 * market_probability
        if market_probability is not None else elo_probability
    )
    home_score, away_score, margin, total = _score_projection(rh, ra, market_probability)
    confidence = abs(win_probability - 0.5) * 2
    direction, stars, _spread_txt, _total_txt = _bet_directions(
        home, away, win_probability, confidence, margin, total, spread_line, total_line
    )
    odds_calibration = ""
    if implied_home is not None and 0 < implied_home < 1:
        odds_calibration = f"赔率赢率={implied_home:.1%} 边缘={win_probability - implied_home:+.1%}"
    return {
        "match": f"{_to_cn(home)} vs {_to_cn(away)}",
        "home": _to_cn(home), "away": _to_cn(away), "direction": direction,
        "stars": stars, "win_prob": round(win_probability, 3),
        "predicted_score": f"{home_score:.0f}-{away_score:.0f}",
        "predicted_margin": round(margin, 1),
        # 让分/大小分为模型投影（点）：不再依赖官方让分/大小线，休赛期也有值。
        # spread_prediction：模型净胜分（正=主让），total_prediction：双方预测总得分。
        "spread_prediction": round(margin, 1),
        "total_prediction": round(total, 1),
        "odds_calibration": odds_calibration,
    }
