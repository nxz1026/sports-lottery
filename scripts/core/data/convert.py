"""Data format converters: football-data.org and API-Football to ESPN format."""

from __future__ import annotations

from core.data.parse import decimal_to_american


def _decimal_to_implied(decimal_str: str | None) -> float | None:
    """decimal odds → implied probability (1/decimal)"""
    if decimal_str is None:
        return None
    try:
        d = float(decimal_str)
        return 1.0 / d if d > 0 else None
    except (ValueError, TypeError):
        return None


def _build_moneyline_odds(match_winner: dict | None, home_team: str, away_team: str) -> dict:
    """Match Winner (1X2) bet → {details, drawOdds, homeML, awayML}."""
    if not match_winner:
        return {}

    values = match_winner.get("values", [])
    home_ml = next((v for v in values if v.get("value") == "Home"), None)
    draw_ml = next((v for v in values if v.get("value") == "Draw"), None)
    away_ml = next((v for v in values if v.get("value") == "Away"), None)

    home_dec = home_ml.get("odd") if home_ml else None
    draw_dec = draw_ml.get("odd") if draw_ml else None
    away_dec = away_ml.get("odd") if away_ml else None

    home_american = decimal_to_american(home_dec)
    draw_american = decimal_to_american(draw_dec)
    away_american = decimal_to_american(away_dec)

    # details = team with lower decimal odds (favorite)
    home_imp = _decimal_to_implied(home_dec) or 0
    away_imp = _decimal_to_implied(away_dec) or 0
    if home_imp >= away_imp and home_american:
        details = f"{home_team} {home_american}"
    elif away_american:
        details = f"{away_team} {away_american}"
    else:
        details = ""

    return {
        "details": details,
        "drawOdds": {"moneyLine": draw_american} if draw_american else {},
        "homeML": home_american,
        "awayML": away_american,
    }


def _build_spread_total_odds(handicap: dict | None, over_under: dict | None, home_team: str, away_team: str) -> dict:
    """Handicap → pointSpread and Goals Over/Under → total bet dicts."""
    odds_entry: dict = {}

    if handicap:
        values = handicap.get("values", [])
        for v in values:
            val = v.get("value", "")
            odd = v.get("odd", "")
            american = decimal_to_american(odd)
            spread_line = val.split(" ")[-1] if " " in val else val
            if home_team in val:
                side_key = "home"
            elif away_team in val:
                side_key = "away"
            else:
                continue
            point_spread = odds_entry.setdefault("pointSpread", {})
            point_spread[side_key] = {
                "open": {"line": spread_line, "odds": american or ""},
                "close": {"line": spread_line, "odds": american or ""},
            }

    if over_under:
        values = over_under.get("values", [])
        for v in values:
            val = v.get("value", "")
            odd = v.get("odd", "")
            american = decimal_to_american(odd)
            line_total = val.split(" ")[-1] if " " in val else "2.5"
            if val.startswith("Over"):
                ou_key = "over"
            elif val.startswith("Under"):
                ou_key = "under"
            else:
                continue
            total = odds_entry.setdefault("total", {})
            total[ou_key] = {
                "open": {"line": line_total, "odds": american or ""},
                "close": {"line": line_total, "odds": american or ""},
            }

    return odds_entry


def _build_odds_from_bookmaker(bookmakers: list, home_team: str, away_team: str) -> dict:
    """Convert API-Football bookmaker bets → ESPN-style odds struct."""
    if not bookmakers:
        return {}

    # Take the first bookmaker with relevant bets
    bets = bookmakers[0].get("bets", [])

    # Find Match Winner (1X2)
    match_winner = next((b for b in bets if b.get("name") == "Match Winner"), None)
    handicap = next((b for b in bets if b.get("name") in ("Handicap", "Asian Handicap", "Point Spread")), None)
    over_under = next((b for b in bets if b.get("name") == "Goals Over/Under"), None)

    odds_entry = {}
    odds_entry.update(_build_moneyline_odds(match_winner, home_team, away_team))
    odds_entry.update(_build_spread_total_odds(handicap, over_under, home_team, away_team))

    return odds_entry


def _build_espn_event(
    name: str,
    date: str,
    home_team: str,
    away_team: str,
    home_abbr: str,
    away_abbr: str,
    home_score: str,
    away_score: str,
    status: str,
    completed: bool,
    odds: list,
) -> dict:
    """Build one ESPN-style event dict from resolved fields."""
    return {
        "name": name,
        "date": date,
        "competitions": [{
            "status": {
                "type": {
                    "name": status,
                    "completed": completed,
                }
            },
            "competitors": [
                {
                    "homeAway": "home",
                    "team": {"displayName": home_team, "abbreviation": home_abbr},
                    "score": home_score,
                    "form": "",
                    "records": [],
                },
                {
                    "homeAway": "away",
                    "team": {"displayName": away_team, "abbreviation": away_abbr},
                    "score": away_score,
                    "form": "",
                    "records": [],
                },
            ],
            "odds": odds,
        }]
    }


def convert_football_data_to_espn_format(data: dict, config: dict) -> list[dict]:
    """将 football-data.org 格式转换为 ESPN 兼容格式"""
    events = []
    matches = data.get("matches", [])

    for match in matches:
        home_team = match.get("homeTeam", {}).get("name", "Unknown")
        away_team = match.get("awayTeam", {}).get("name", "Unknown")

        status_map = {
            "SCHEDULED": "STATUS_SCHEDULED",
            "LIVE": "STATUS_IN_PROGRESS",
            "IN_PLAY": "STATUS_IN_PROGRESS",
            "PAUSED": "STATUS_IN_PROGRESS",
            "FINISHED": "STATUS_FULL_TIME",
            "POSTPONED": "STATUS_SCHEDULED",
            "SUSPENDED": "STATUS_SCHEDULED",
            "CANCELLED": "STATUS_SCHEDULED",
        }

        status = status_map.get(match.get("status", ""), "STATUS_SCHEDULED")
        completed = match.get("status") == "FINISHED"

        score_data = match.get("score", {}).get("fullTime", {})
        home_score = str(score_data.get("home", 0) or 0)
        away_score = str(score_data.get("away", 0) or 0)

        events.append(_build_espn_event(
            name=f"{away_team} at {home_team}",
            date=match.get("utcDate", ""),
            home_team=home_team,
            away_team=away_team,
            home_abbr=match.get("homeTeam", {}).get("t3", "UNK"),
            away_abbr=match.get("awayTeam", {}).get("t3", "UNK"),
            home_score=home_score,
            away_score=away_score,
            status=status,
            completed=completed,
            odds=[],
        ))

    return events


def _map_api_football_status(fixture: dict) -> tuple[str, bool]:
    """API-Football status short → (ESPN status, completed)."""
    status_short = fixture.get("fixture", {}).get("status", {}).get("short", "NS")
    status_map = {
        "NS": "STATUS_SCHEDULED",
        "1H": "STATUS_IN_PROGRESS",
        "HT": "STATUS_IN_PROGRESS",
        "2H": "STATUS_IN_PROGRESS",
        "ET": "STATUS_IN_PROGRESS",
        "P": "STATUS_IN_PROGRESS",
        "FT": "STATUS_FULL_TIME",
        "AET": "STATUS_FINAL_ET",
        "PEN": "STATUS_FINAL_PEN",
        "SUSP": "STATUS_SCHEDULED",
        "INT": "STATUS_SCHEDULED",
        "PST": "STATUS_SCHEDULED",
        "CANC": "STATUS_SCHEDULED",
        "ABD": "STATUS_SCHEDULED",
        "AWD": "STATUS_FULL_TIME",
        "WO": "STATUS_FULL_TIME",
    }

    status = status_map.get(status_short, "STATUS_SCHEDULED")
    completed = status in ("STATUS_FULL_TIME", "STATUS_FINAL_ET", "STATUS_FINAL_PEN")
    return status, completed


def convert_api_football_to_espn_format(data: dict, config: dict, odds_lookup: dict[int, list] | None = None) -> list[dict]:
    """将 API-Football 格式转换为 ESPN 兼容格式（含赔率）。"""
    events = []
    fixtures = data.get("response", [])

    for fixture in fixtures:
        fixture_id = fixture.get("fixture", {}).get("id")
        home_team = fixture.get("teams", {}).get("home", {}).get("name", "Unknown")
        away_team = fixture.get("teams", {}).get("away", {}).get("name", "Unknown")

        status, completed = _map_api_football_status(fixture)

        goals = fixture.get("goals", {})
        home_score = str(goals.get("home", 0) or 0)
        away_score = str(goals.get("away", 0) or 0)

        # ── Build ESPN-style odds from API-Football odds lookup ──
        odds_entry = {}
        if odds_lookup and fixture_id and fixture_id in odds_lookup:
            bookmakers = odds_lookup[fixture_id]
            odds_entry = _build_odds_from_bookmaker(bookmakers, home_team, away_team)

        events.append(_build_espn_event(
            name=f"{away_team} at {home_team}",
            date=fixture.get("fixture", {}).get("date", ""),
            home_team=home_team,
            away_team=away_team,
            home_abbr=home_team[:3].upper(),
            away_abbr=away_team[:3].upper(),
            home_score=home_score,
            away_score=away_score,
            status=status,
            completed=completed,
            odds=[odds_entry] if odds_entry else [],
        ))

    return events
