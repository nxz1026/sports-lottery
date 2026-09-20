"""篮球引擎离线单元测试。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from scripts.bball import config_bball, elo_bball, odds_api
from scripts.bball.predictor import predict_game


def test_elo_constants_exist_with_numeric_values() -> None:
    assert isinstance(config_bball.ELO, dict)
    assert {"K", "HOME_ADV", "INITIAL", "REGRESSION", "SCALE"} <= config_bball.ELO.keys()
    assert all(isinstance(value, (int, float)) for value in config_bball.ELO.values())


def test_predict_constants_exist_with_numeric_values() -> None:
    assert isinstance(config_bball.PREDICT, dict)
    expected = {"LEAGUE_PACE", "AVG_PPG", "HOME_SCORE_ADV", "ELO_TO_POINTS"}
    assert expected <= config_bball.PREDICT.keys()
    assert all(isinstance(value, (int, float)) for value in config_bball.PREDICT.values())


def test_parse_odds_returns_home_away_spread_total() -> None:
    game = {
        "bookmakers": [{"markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Away", "price": 2.4}, {"name": "Home", "price": 1.6},
            ]},
            {"key": "spreads", "outcomes": [
                {"name": "Home", "point": -4.5}, {"name": "Away", "point": 4.5},
            ]},
            {"key": "totals", "outcomes": [
                {"name": "Over", "point": 221.5}, {"name": "Under", "point": 221.5},
            ]},
        ]}],
    }
    assert odds_api.parse_odds(game, "Home") == (1.6, 2.4, -4.5, 221.5)


def test_parse_odds_returns_none_for_missing_markets() -> None:
    assert odds_api.parse_odds({"bookmakers": [{"markets": []}]}, "Home") == (None, None, None, None)


def test_parse_odds_tolerates_malformed_market() -> None:
    malformed = {"bookmakers": [{"markets": [{"key": "unknown"}, {}]}]}
    assert odds_api.parse_odds(malformed, "Home") == (None, None, None, None)


def _game_at(moment: datetime, name: str) -> dict:
    return {"id": name, "commence_time": moment.isoformat()}


def test_filter_window_splits_past_and_future() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    games = [_game_at(now - timedelta(seconds=1), "past"), _game_at(now + timedelta(hours=1), "future")]
    past, future = odds_api.filter_window_games(games, now=now, ahead_hours=24)
    assert [game["id"] for game in past] == ["past"]
    assert [game["id"] for game in future] == ["future"]


def test_filter_window_includes_now_and_window_boundary() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    games = [_game_at(now, "now"), _game_at(now + timedelta(hours=2), "edge"), _game_at(now + timedelta(hours=2, seconds=1), "late")]
    _, future = odds_api.filter_window_games(games, now=now, ahead_hours=2)
    assert [game["id"] for game in future] == ["now", "edge"]


def test_filter_window_empty_list() -> None:
    assert odds_api.filter_window_games([], now=datetime.now(timezone.utc), ahead_hours=24) == ([], [])


def test_expectation_is_symmetric_at_equal_ratings() -> None:
    assert elo_bball.expectation(1500, 1500) == 0.5


def test_expectation_increases_with_home_rating() -> None:
    assert elo_bball.expectation(1600, 1500) > elo_bball.expectation(1500, 1500)


def test_load_ratings_seeds_and_persists(tmp_path, monkeypatch) -> None:
    elo_file = tmp_path / "ratings.json"
    seed_file = tmp_path / "teams.json"
    seed_file.write_text('{"Home": 1500, "Away": 1500}', encoding="utf-8")
    monkeypatch.setattr(elo_bball, "ELO_FILE", elo_file)
    monkeypatch.setattr(elo_bball, "TEAMS_SEED_FILE", seed_file)
    ratings = elo_bball.load_ratings()
    assert ratings == {"Home": 1500.0, "Away": 1500.0}
    assert elo_file.exists()


def test_update_ratings_winner_gains_loser_loses() -> None:
    ratings = {"Home": 1500.0, "Away": 1500.0}
    count = elo_bball.update_ratings([ratings][0], [{"home_team": "Home", "away_team": "Away", "scores": {"Home": 110, "Away": 100}}])
    expected = elo_bball.expectation(1500 + config_bball.ELO["HOME_ADV"], 1500)
    assert count == 1
    assert ratings["Home"] == 1500 + config_bball.ELO["K"] * (1 - expected)
    assert ratings["Away"] == 1500 - config_bball.ELO["K"] * (1 - expected)


def test_update_ratings_uses_configured_k(monkeypatch) -> None:
    ratings = {"Home": 1500.0, "Away": 1500.0}
    monkeypatch.setitem(config_bball.ELO, "K", 64)
    elo_bball.update_ratings(ratings, [{"home_team": "Home", "away_team": "Away", "scores": {"Home": 110, "Away": 100}}])
    expected = elo_bball.expectation(1500 + config_bball.ELO["HOME_ADV"], 1500)
    assert ratings["Home"] == 1500 + 64 * (1 - expected)


def test_predict_game_high_elo_home_predicts_home_win() -> None:
    result = predict_game("Home", "Away", {"Home": 1700, "Away": 1400}, None, None, None, None)
    assert result["direction"] == "Home 胜"


def test_predict_game_handles_missing_market_fields() -> None:
    result = predict_game("Home", "Away", {}, None, None, None, None)
    # 让分/大小分是模型投影（数值），不依赖官方盘口线，任何场次都有值
    assert isinstance(result["spread_prediction"], (int, float))
    assert isinstance(result["total_prediction"], (int, float))
    assert result["total_prediction"] > 0


def test_predict_game_score_has_integer_pair_format() -> None:
    result = predict_game("Home", "Away", {"Home": 1600, "Away": 1500}, None, None, None, None)
    assert re.fullmatch(r"\d+-\d+", result["predicted_score"])


def test_predict_game_win_probability_is_bounded() -> None:
    result = predict_game("Home", "Away", {"Home": 1600, "Away": 1500}, None, None, None, None)
    assert 0 <= result["win_prob"] <= 1
