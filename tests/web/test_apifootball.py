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
