"""R49-5: scripts/core/data/fetch.py::fetch_api_football 4 cases direct + D-91 lock.

fetch_api_football (L268-L316, 49 行) 从 API-Football 获取数据 (含赔率):
1) api_key = env API_FOOTBALL_KEY
2) fixture_league_id = config.get('api_football_id')
3) fixture_league_id 缺 -> fetch_espn fallback (L277-278)
4) 解析 dates_str -> formatted_date (YYYY-MM-DD) + season
5) headers 含 x-apisports-key
6) fixtures_url 拼装 (https://v3.football.api-sports.io/fixtures?date=...)
7) _retry_request + _update_rate_limit_from_response + _validate_api_response
8) 客户端按 league_id 过滤
9) _build_odds_lookup + convert_api_football_to_espn_format
10) except: logger.warning + raise (L314-316, 无 fetch_espn fallback)

R36-5/6/7 + R45-2/3 D-19/20/21/63/64/73 + R48-3 + R49-2/3/4 D-85/88/89/90
已锁 _retry_request / _update_rate_limit_from_response / _build_odds_lookup
/ convert_api_football_to_espn_format / _map_api_football_status /
_build_espn_event / convert_football_data_to_espn_format / fetch_espn /
fetch_football_data, 但 fetch_api_football 真函数自身核心契约
(URL 拼装 + x-apisports-key 头 + 客户端 league 过滤 + api_football_id
缺 fallback + except raise) 0 直测。

D-91 锁定 4 契约:
- D-91-1: 主流程 happy - URL 含 date + x-apisports-key 头 + 返 convert 结果
- D-91-2: api_football_id 缺 -> 走 fetch_espn fallback
- D-91-3: 客户端 league 过滤 (fixtures 含多 league, 只返 fixture_league_id)
- D-91-4: _retry_request 抛错 -> except raise (无 fetch_espn fallback)

依赖 stub:
- _retry_request (monkeypatch 返受控 data)
- _update_rate_limit_from_response (no-op)
- _build_odds_lookup (stub 返 {})
- convert_api_football_to_espn_format (stub 返受控 events)
- _last_resp_headers 模块级 (设空 dict)
- os.environ (API_FOOTBALL_KEY)
- urllib.request.Request (捕获 url + headers)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def fetch_mod(monkeypatch):
    """Spec-load fetch.py + 默认 stub + 清 env var。"""
    spec = importlib.util.spec_from_file_location(
        "fetch_api_football_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 默认 stub
    monkeypatch.setattr(mod, "_retry_request",
                        lambda req, max_retries=3, timeout=None: {"response": []})
    monkeypatch.setattr(mod, "_update_rate_limit_from_response", lambda hdrs: None)
    monkeypatch.setattr(mod, "_build_odds_lookup",
                        lambda league_id, season, date, api_key, headers: {})
    monkeypatch.setattr(mod, "convert_api_football_to_espn_format",
                        lambda data, config, odds: [{"espn": "stub"}])
    # _last_resp_headers 模块级变量 (L301 引用)
    mod._last_resp_headers = {}
    # env
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    return mod


def _capture_request(monkeypatch, mod):
    """monkeypatch urllib.request.Request 捕获构造参数。"""
    captured = {"url": None, "headers": None}

    def fake_request(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        class _R:
            pass
        r = _R()
        r.full_url = url
        return r

    monkeypatch.setattr(mod.urllib.request, "Request", fake_request)
    return captured


# ──────────────────────── D-91 四契约 ────────────────────────


def test_fetch_api_football_d91_1_happy_path_with_url_and_apikey_header(fetch_mod, monkeypatch):
    """R49-5 D-91-1: 主流程 happy - URL 含 date + x-apisports-key 头 + 返 convert 结果。

    dates_str='20260715-20260720' -> formatted_date='2026-07-15'。
    api_key 存在 -> headers['x-apisports-key'] = api_key。
    返 convert_api_football_to_espn_format stub 结果。
    """
    mod = fetch_mod
    monkeypatch.setenv("API_FOOTBALL_KEY", "af-test-key")
    captured = _capture_request(monkeypatch, mod)

    config = {"api_football_id": 39, "name": "epl"}
    out = mod.fetch_api_football("20260715-20260720", config)
    # URL 含 date=2026-07-15
    assert "https://v3.football.api-sports.io/fixtures" in captured["url"]
    assert "date=2026-07-15" in captured["url"]
    # x-apisports-key 头
    assert captured["headers"].get("x-apisports-key") == "af-test-key"
    # 返 convert stub 结果
    assert out == [{"espn": "stub"}]


def test_fetch_api_football_d91_2_missing_api_football_id_falls_back_to_espn(fetch_mod, monkeypatch):
    """R49-5 D-91-2: api_football_id 缺 -> 走 fetch_espn fallback (L277-278)。

    config 缺 api_football_id -> logger.warning + fetch_espn(dates_str, espn_slug)。
    """
    mod = fetch_mod
    espn_called = {"dates": None, "slug": None}

    def fake_espn(dates_str, league_slug="epl"):
        espn_called["dates"] = dates_str
        espn_called["slug"] = league_slug
        return [{"espn_fallback": True}]

    monkeypatch.setattr(mod, "fetch_espn", fake_espn)

    config = {"name": "epl", "espn_slug": "eng.1"}  # 无 api_football_id
    out = mod.fetch_api_football("20260715-20260720", config)
    # 走 fetch_espn
    assert out == [{"espn_fallback": True}]
    assert espn_called["dates"] == "20260715-20260720"
    assert espn_called["slug"] == "eng.1"  # 从 config 取


def test_fetch_api_football_d91_3_client_side_league_filtering(fetch_mod, monkeypatch):
    """R49-5 D-91-3: 客户端 league 过滤 - fixtures 含多 league, 只保留 fixture_league_id。

    L305-307: 客户端 `[f for f in all if f.get('league', {}).get('id') == fixture_league_id]`。
    3 fixtures, 2 个 league=39, 1 个 league=140 -> filtered 仅 2 个。
    """
    mod = fetch_mod
    monkeypatch.setenv("API_FOOTBALL_KEY", "af-key")

    # _retry_request 返 3 fixtures (2 个 epl=39, 1 个 laliga=140)
    fixtures_resp = {"response": [
        {"fixture": {"id": 1}, "league": {"id": 39}},
        {"fixture": {"id": 2}, "league": {"id": 140}},
        {"fixture": {"id": 3}, "league": {"id": 39}},
    ]}
    monkeypatch.setattr(mod, "_retry_request",
                        lambda req, max_retries=3, timeout=None: fixtures_resp)

    # convert stub 返过滤后 fixtures 数 (供断言)
    captured_filtered = {}

    def fake_convert(data, config, odds):
        captured_filtered["count"] = len(data["response"])
        captured_filtered["ids"] = [f["fixture"]["id"] for f in data["response"]]
        return [{"stub": True}]

    monkeypatch.setattr(mod, "convert_api_football_to_espn_format", fake_convert)

    config = {"api_football_id": 39, "name": "epl"}
    mod.fetch_api_football("20260715-20260720", config)
    # filtered 仅 2 个 (id=1, 3), id=2 (laliga=140) 被过滤
    assert captured_filtered["count"] == 2
    assert set(captured_filtered["ids"]) == {1, 3}


def test_fetch_api_football_d91_4_retry_request_raises_no_fallback(fetch_mod, monkeypatch):
    """R49-5 D-91-4: _retry_request 抛错 -> except raise (L314-316, 无 fetch_espn fallback)。

    LOCK: except 路径只 logger.warning + raise, 不调 fetch_espn (区别于
    api_football_id 缺场景 L277)。
    """
    mod = fetch_mod
    monkeypatch.setenv("API_FOOTBALL_KEY", "af-key")
    espn_called = {"count": 0}

    def fake_espn(*args, **kwargs):
        espn_called["count"] += 1
        return [{"espn_fallback": True}]

    def fake_retry(*args, **kwargs):
        raise ConnectionError("api-football rate limited")

    monkeypatch.setattr(mod, "fetch_espn", fake_espn)
    monkeypatch.setattr(mod, "_retry_request", fake_retry)

    config = {"api_football_id": 39, "name": "epl"}
    with pytest.raises(ConnectionError, match="rate limited"):
        mod.fetch_api_football("20260715-20260720", config)
    # espn fallback 不被调 (区别于 D-91-2 api_football_id 缺场景)
    assert espn_called["count"] == 0