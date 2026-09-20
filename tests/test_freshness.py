from __future__ import annotations

from web.services import freshness as F


def test_evaluate_topics_empty():
    r = F.evaluate_topics([])
    assert r["ok"] is True
    assert r["stale"] == []
    assert r["all"] == []
    assert r["threshold_hours"] == F.DEFAULT_THRESHOLD_HOURS


def test_evaluate_topics_marks_stale_above_threshold():
    topics = [
        {"topic": "a", "min_since_latest": 30.0, "latest_arrival": "2026-09-20T01:00:00Z"},
        {"topic": "b", "min_since_latest": 1500.0, "latest_arrival": "2026-09-19T00:00:00Z"},  # 25h
        {"topic": "c", "min_since_latest": 60.0, "latest_arrival": "2026-09-20T00:00:00Z"},
    ]
    r = F.evaluate_topics(topics, threshold_hours=24.0)
    assert r["ok"] is False
    assert [x["topic"] for x in r["stale"]] == ["b"]
    assert r["stale"][0]["hours"] == 25.0
    assert r["threshold_hours"] == 24.0
    assert len(r["all"]) == 3


def test_evaluate_topics_custom_threshold():
    topics = [{"topic": "x", "min_since_latest": 120.0, "latest_arrival": None}]
    # 2h, 默认 24h → ok
    assert F.evaluate_topics(topics, threshold_hours=24.0)["ok"] is True
    # 2h, 阈值 1h → stale
    assert F.evaluate_topics(topics, threshold_hours=1.0)["ok"] is False


def test_evaluate_topics_tolerates_bad_minutes():
    topics = [{"topic": "y", "min_since_latest": None}, {"topic": "z"}]
    r = F.evaluate_topics(topics)
    assert len(r["all"]) == 2
    assert all(x["minutes"] == 0.0 for x in r["all"])


def test_evaluate_topics_stale_sorted_by_minutes_desc():
    topics = [
        {"topic": "lo", "min_since_latest": 1440.0, "latest_arrival": None},
        {"topic": "hi", "min_since_latest": 5000.0, "latest_arrival": None},
        {"topic": "mid", "min_since_latest": 2000.0, "latest_arrival": None},
    ]
    r = F.evaluate_topics(topics, threshold_hours=24.0)
    assert [x["topic"] for x in r["stale"]] == ["hi", "mid", "lo"]


def test_format_alert_lines_empty_when_ok():
    assert F.format_alert_lines(F.evaluate_topics([])) == []


def test_format_alert_lines_contains_topic_and_minutes():
    r = F.evaluate_topics(
        [{"topic": "t1", "min_since_latest": 2000.0, "latest_arrival": "X"}],
        threshold_hours=24.0,
    )
    lines = F.format_alert_lines(r)
    assert len(lines) == 1
    assert "stale topic=t1" in lines[0]
    assert "minutes=2000" in lines[0]
