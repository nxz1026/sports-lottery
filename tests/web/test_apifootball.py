import json

import pytest

from web.services import apifootball


def _enable(monkeypatch):
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_ENRICH_ENABLED", True)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_KEY", "test-key")
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_CACHE_TTL_SECONDS", 900)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_DAILY_LIMIT", 100)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_MINUTE_LIMIT", 10)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_TIMEOUT_SECONDS", 3)


def test_disabled_is_safe_and_no_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_ENRICH_ENABLED", False)
    calls = []
    service = apifootball.ApiFootballService(transport=lambda *args: calls.append(args), cache_dir=tmp_path)
    with pytest.raises(apifootball.ApiFootballError):
        service.injuries(1)
    assert not calls


def test_injuries_normalized_and_cached(monkeypatch, tmp_path):
    _enable(monkeypatch)
    calls = []

    def transport(request, timeout):
        calls.append((request.full_url, timeout))
        return json.dumps({"response": [{"player": {"id": 7}, "team": {"id": 50}, "type": "Missing", "reason": "ankle"}]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path)
    assert service.injuries(123)[0]["player"]["id"] == 7
    assert service.injuries(123)[0]["reason"] == "ankle"
    assert len(calls) == 1
    assert "fixture=123" in calls[0][0]


def test_lineups_h2h_and_minute_quota(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_MINUTE_LIMIT", 1)
    now = [1000.0]

    def transport(request, timeout):
        return json.dumps({"response": [{"team": {"id": 50}, "formation": "4-3-3"}]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path, now=lambda: now[0])
    assert service.lineups(12)[0]["formation"] == "4-3-3"
    with pytest.raises(apifootball.ApiFootballQuotaError):
        service.h2h(50, 42)


def test_h2h_never_sends_last_and_limits_client_side(monkeypatch, tmp_path):
    """Free plans reject ``last`` outright, so the limit must stay local."""
    _enable(monkeypatch)
    urls = []

    def transport(request, timeout):
        urls.append(request.full_url)
        return json.dumps({"response": [
            {"fixture": {"id": 1, "date": "2021-10-16T16:30:00+00:00"}},
            {"fixture": {"id": 2, "date": "2024-03-02T15:00:00+00:00"}},
            {"fixture": {"id": 3, "date": "2013-01-27T12:00:00+00:00"}},
        ]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path)
    rows = service.h2h(55, 49, last=2)
    assert "last" not in urls[0]
    assert [r["fixture"]["id"] for r in rows] == [2, 1]


def test_h2h_sorts_newest_first_without_last(monkeypatch, tmp_path):
    _enable(monkeypatch)

    def transport(request, timeout):
        return json.dumps({"response": [
            {"fixture": {"id": 1, "date": "2013-01-27T12:00:00+00:00"}},
            {"fixture": {"id": 2, "date": "2027-05-30T15:00:00+00:00"}},
            {"fixture": {"id": 3, "date": "2021-10-16T16:30:00+00:00"}},
        ]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path)
    assert [r["fixture"]["id"] for r in service.h2h(55, 49)] == [2, 3, 1]


def test_h2h_tolerates_missing_or_invalid_dates(monkeypatch, tmp_path):
    _enable(monkeypatch)

    def transport(request, timeout):
        return json.dumps({"response": [
            {"fixture": {"id": 1}},
            {"fixture": {"id": 2, "date": "not-a-date"}},
            {"fixture": {"id": 3, "date": "2024-03-02T15:00:00+00:00"}},
        ]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path)
    rows = service.h2h(55, 49, last=1)
    assert [r["fixture"]["id"] for r in rows] == [3]


def test_injuries_reads_type_and_reason_nested_under_player(monkeypatch, tmp_path):
    """真实 provider 把 type/reason 嵌在 player 下（行级只有 fixture/league/team/player）。

    旧代码只读行级字段 → 实测 24 行全部 type/reason=None，伤停类型与原因被静默丢弃。
    """
    _enable(monkeypatch)

    def transport(request, timeout):
        return json.dumps({"response": [
            {"player": {"id": 19495, "name": "N. Collins",
                        "type": "Missing Fixture", "reason": "Injury"},
             "team": {"id": 55}},
        ]}).encode()

    service = apifootball.ApiFootballService(transport=transport, cache_dir=tmp_path)
    row = service.injuries(1557408)[0]
    assert row["type"] == "Missing Fixture"
    assert row["reason"] == "Injury"
