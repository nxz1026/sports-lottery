from datetime import date

from web.services import ai


def test_daily_exact_match_and_league(monkeypatch):
    monkeypatch.setattr(ai.store, "latest_by_league", lambda: {
        "epl": {"data": {"data_window": "20260101-20260101", "predictions": [
            {"match": "A vs B", "home": "A"}, {"match": "C vs D", "home": "C"}]}},
    })
    monkeypatch.setattr(ai, "_load_ai_scores", lambda: {
        "A vs B": {"league": "epl", "ai_score": 80},
        "C vs D": {"league": "laliga", "ai_score": 99},
    })
    out = ai.ai_daily_report(date(2026, 1, 1))
    assert out["summary"] == {"prediction_count": 2, "ai_matched_count": 1, "unmatched_count": 1,
                               "leagues": {"epl": {"prediction_count": 2, "ai_matched_count": 1}}}
    assert out["items"][1]["ai_matched"] is False


def test_ranking_excludes_non_numeric(monkeypatch):
    monkeypatch.setattr(ai, "ai_daily_report", lambda day: {"available": True, "date": day.isoformat(), "items": [
        {"match": "a", "league": "epl", "ai_matched": True, "ai_score": 20},
        {"match": "b", "league": "epl", "ai_matched": True, "ai_score": "bad"},
        {"match": "c", "league": "epl", "ai_matched": True, "ai_score": 90},
    ]})
    out = ai.ai_ranking(date(2026, 1, 1), 1)
    assert out["matched_count"] == 2
    assert out["hot"][0]["match"] == "c"
    assert out["cold"][0]["match"] == "a"
