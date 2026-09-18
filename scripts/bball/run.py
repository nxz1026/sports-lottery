"""NBA 预测 CLI 与预测文件输出。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bball.config_bball import LEAGUE
    from bball.elo_bball import load_ratings, save_ratings, update_ratings
    from bball.odds_api import (OddsApiError, fetch_completed_games, fetch_odds_games,
                                filter_window_games, parse_odds)
    from bball.predictor import predict_game
else:
    from .config_bball import LEAGUE
    from .elo_bball import load_ratings, save_ratings, update_ratings
    from .odds_api import (OddsApiError, fetch_completed_games, fetch_odds_games,
                           filter_window_games, parse_odds)
    from .predictor import predict_game

from core.config import PREDICTIONS_DIR
from core.i18n import to_cn

LOGGER = logging.getLogger("bball.run")
BJT = timezone(timedelta(hours=8))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBA 预测 CLI")
    parser.add_argument("--backtest", type=int, default=0, metavar="N")
    parser.add_argument("--ahead-days", type=int, default=1, metavar="N")
    parser.add_argument("--leagues", default="nba")
    return parser


def _display_name(name: str) -> str:
    translated = to_cn(name)
    if translated != name:
        return translated
    return {
        "Atlanta Hawks": "亚特兰大老鹰", "Boston Celtics": "波士顿凯尔特人",
        "Brooklyn Nets": "布鲁克林篮网", "Charlotte Hornets": "夏洛特黄蜂",
        "Chicago Bulls": "芝加哥公牛", "Cleveland Cavaliers": "克利夫兰骑士",
        "Dallas Mavericks": "达拉斯独行侠", "Denver Nuggets": "丹佛掘金",
        "Detroit Pistons": "底特律活塞", "Golden State Warriors": "金州勇士",
        "Houston Rockets": "休斯顿火箭", "Indiana Pacers": "印第安纳步行者",
        "LA Clippers": "洛杉矶快船", "Los Angeles Lakers": "洛杉矶湖人",
        "Memphis Grizzlies": "孟菲斯灰熊", "Miami Heat": "迈阿密热火",
        "Milwaukee Bucks": "密尔沃基雄鹿", "Minnesota Timberwolves": "明尼苏达森林狼",
        "New Orleans Pelicans": "新奥尔良鹈鹕", "New York Knicks": "纽约尼克斯",
        "Oklahoma City Thunder": "俄克拉荷马雷霆", "Orlando Magic": "奥兰多魔术",
        "Philadelphia 76ers": "费城76人", "Phoenix Suns": "菲尼克斯太阳",
        "Portland Trail Blazers": "波特兰开拓者", "Sacramento Kings": "萨克拉门托国王",
        "San Antonio Spurs": "圣安东尼奥马刺", "Toronto Raptors": "多伦多猛龙",
        "Utah Jazz": "犹他爵士", "Washington Wizards": "华盛顿奇才",
    }.get(name, name)


def _kickoff_date(game: dict[str, Any], now: datetime) -> str:
    raw = str(game.get("commence_time", ""))
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return moment.astimezone(BJT).date().isoformat()
    except ValueError:
        return now.astimezone(BJT).date().isoformat()


def _prediction(game: dict[str, Any], ratings: dict[str, float], now: datetime) -> dict:
    home, away = game.get("home_team", ""), game.get("away_team", "")
    odds_home, _odds_away, spread, total = parse_odds(game, home)
    result = predict_game(home, away, ratings, spread, total, odds_home, None)
    result["home"] = _display_name(home)
    result["away"] = _display_name(away)
    result["match"] = f"{result['home']} vs {result['away']}"
    # 英文原名是 The Odds API 的原始标识符，必须保留：AI 分数的稳定主键是
    # league|home_en|away_en（见 ai/feedback_loop.py::score_key）。中文名只用于显示。
    result["home_en"] = home
    result["away_en"] = away
    result["kickoff_date"] = _kickoff_date(game, now)
    result["spread_pred"] = result.get("spread_prediction")
    result["total_pred"] = result.get("total_prediction")
    return result


def _past_detail(game: dict[str, Any], prediction: dict) -> dict:
    scores = game.get("scores", {})
    home, away = game.get("home_team", ""), game.get("away_team", "")
    hs = scores.get(home) if isinstance(scores, dict) else None
    aws = scores.get(away) if isinstance(scores, dict) else None
    score = f"{hs}-{aws}" if hs is not None and aws is not None else ""
    detail = dict(prediction)
    if hs is not None and aws is not None:
        detail["actual_winner"] = prediction["home"] if hs > aws else prediction["away"]
    detail["actual_score"] = score
    return detail


def _save(output: dict, now: datetime) -> Path:
    """落盘篮球预测。

    文件名必须带联赛后缀：时间戳只有小时精度（%Y-%m-%d_%H），而篮球与足球预测写的是
    同一个 PREDICTIONS_DIR —— 不带后缀时同小时内先后跑的两者会互相覆盖，一方数据静默
    丢失（足球侧 scripts/predict.py 的 _save_output 有同样的缺陷，已一并修复）。
    归并侧 store.latest_by_league() 读 JSON 里的 league 字段、不解析文件名。
    """
    league = output.get("league") if isinstance(output.get("league"), str) else ""
    suffix = f"_{league}" if league else ""
    path = PREDICTIONS_DIR / f"prediction_{now.strftime('%Y-%m-%d_%H')}{suffix}.json"
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _window(now: datetime) -> str:
    return f"{now.astimezone(BJT):%Y%m%d}-{(now.astimezone(BJT) + timedelta(days=1)):%Y%m%d}"


def run(args: argparse.Namespace) -> dict:
    key = os.environ.get("ODDS_API_KEY", "")
    if not key:
        LOGGER.error("ODDS_API_KEY 未设置")
        raise SystemExit(2)
    now = datetime.now(timezone.utc)
    ratings = load_ratings()
    if args.backtest:
        completed = fetch_completed_games(key, LEAGUE["odds_sport"], args.backtest)
        if not completed:
            LOGGER.warning("odds api free tier 无历史比分，休赛期请用 --ahead-days 90 前瞻揭幕战")
        update_ratings(ratings, completed)
        save_ratings(ratings)
        details = [_past_detail(g, _prediction(g, ratings, now)) for g in completed]
        output = {"league": "nba", "sport": "basketball", "generated_at": now.astimezone(BJT).isoformat(),
                  "data_window": _window(now), "predictions": [], "past_matches": details,
                  "past_games_detail": details}
    else:
        games = fetch_odds_games(key, LEAGUE["odds_sport"], args.ahead_days)
        _past, future = filter_window_games(games, ahead_hours=args.ahead_days * 24)
        completed = fetch_completed_games(key, LEAGUE["odds_sport"], 3)
        update_ratings(ratings, completed)
        save_ratings(ratings)
        predictions = [_prediction(g, ratings, now) for g in future if g.get("home_team") and g.get("away_team")]
        output = {"league": "nba", "sport": "basketball", "generated_at": now.astimezone(BJT).isoformat(),
                  "data_window": _window(now), "predictions": predictions}
    _save(output, now.astimezone(BJT))
    return output


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        result = run(_parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OddsApiError, json.JSONDecodeError, ValueError) as error:
        LOGGER.error("NBA 预测失败: %s", error)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
