"""R49-2: scripts/core/data/fetch.py::fetch_events 4 cases direct + D-88 lock.

fetch_events (L109-L153, 45 行) 抽象数据源 router:
- 路由 espn -> fetch_espn (whole_season 警告)
- 路由 football-data -> fetch_football_data (whole_season 透传)
- 路由 api-football -> ThreadPoolExecutor 并行 api + espn, 主取 api, 失败 fallback espn
- 其它 -> default espn (whole_season 警告)

R36-5/6/7 D-19/D-20/D-21 + R48-3 D-85 已锁 _validate_api_response /
_update_rate_limit_from_response / _retry_request / _build_odds_lookup /
validate_api_keys, 但 fetch_events router 自身核心契约 (4 路由分发 +
api-football 并行 + fallback + whole_season 透传/警告) 0 直测。

D-88 锁定 4 契约:
- D-88-1: 路由 espn -> 调 fetch_espn (slug 来自 config, default epl)
- D-88-2: 路由 football-data -> 调 fetch_football_data (whole_season 透传)
- D-88-3: 路由 api-football + 主结果非空 -> 返 api 结果 (不调 espn fallback)
- D-88-4: 路由 api-football + 主结果空 -> 走 espn fallback

依赖 stub:
- fetch_espn / fetch_football_data / fetch_api_football monkeypatch 返受控
- LEAGUE_CONFIG 真跑 (锁了 epl/football-data/api-football 配置)
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
    """Spec-load fetch.py + stub 3 个下游 fetch。"""
    spec = importlib.util.spec_from_file_location(
        "fetch_events_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 默认 stub: 3 个 fetch 返 []
    monkeypatch.setattr(mod, "fetch_espn",
                        lambda dates_str, league_slug="epl": [])
    monkeypatch.setattr(mod, "fetch_football_data",
                        lambda dates_str, config, whole_season=False: [])
    monkeypatch.setattr(mod, "fetch_api_football",
                        lambda dates_str, config: [])
    return mod


# ──────────────────────── D-88 四契约 ────────────────────────


def test_fetch_events_d88_1_routes_to_espn(fetch_mod, monkeypatch):
    """R49-2 D-88-1: 路由 espn -> 调 fetch_espn + slug 来自 config (default epl)。"""
    received = {"dates": None, "slug": None}

    def fake_espn(dates_str, league_slug="epl"):
        received["dates"] = dates_str
        received["slug"] = league_slug
        return [{"espn_event": 1}]

    monkeypatch.setattr(fetch_mod, "fetch_espn", fake_espn)
    out = fetch_mod.fetch_events("20260715-20260720", league_key="epl",
                                    data_source="espn")
    assert out == [{"espn_event": 1}]
    assert received["dates"] == "20260715-20260720"
    # LEAGUE_CONFIG['epl']['espn_slug'] 真实值 - 验证传的是 config 里的 slug
    assert received["slug"] is not None


def test_fetch_events_d88_2_routes_to_football_data_with_whole_season(fetch_mod, monkeypatch):
    """R49-2 D-88-2: 路由 football-data -> 调 fetch_football_data + whole_season 透传。

    fetch_espn 不应被调, 验证 whole_season=True 透传。
    """
    espn_called = {"flag": False}
    monkeypatch.setattr(fetch_mod, "fetch_espn",
                        lambda *a, **kw: espn_called.update(flag=True) or [])

    received = {"dates": None, "config": None, "whole_season": None}

    def fake_fd(dates_str, config, whole_season=False):
        received["dates"] = dates_str
        received["config"] = config
        received["whole_season"] = whole_season
        return [{"football_data_event": 1}]

    monkeypatch.setattr(fetch_mod, "fetch_football_data", fake_fd)

    out = fetch_mod.fetch_events("20260715-20260720", league_key="epl",
                                    data_source="football-data",
                                    whole_season=True)
    assert out == [{"football_data_event": 1}]
    assert received["dates"] == "20260715-20260720"
    assert received["whole_season"] is True
    assert espn_called["flag"] is False  # espn 未被调


def test_fetch_events_d88_3_api_football_primary_returns_immediately(fetch_mod, monkeypatch):
    """R49-2 D-88-3: 路由 api-football + 主结果非空 -> 返 api 结果 (不调 espn fallback)。

    ThreadPoolExecutor 并行, api 先返 (mock 立即) + espn 仍并行但结果被忽略。
    """
    api_called = {"count": 0}
    espn_called = {"count": 0}

    def fake_api(dates_str, config):
        api_called["count"] += 1
        return [{"api_football_event": 1}]

    def fake_espn(dates_str, league_slug="epl"):
        espn_called["count"] += 1
        return [{"espn_event": 2}]

    monkeypatch.setattr(fetch_mod, "fetch_api_football", fake_api)
    monkeypatch.setattr(fetch_mod, "fetch_espn", fake_espn)

    out = fetch_mod.fetch_events("20260715-20260720", league_key="epl",
                                    data_source="api-football")
    # api 返回非空 -> 直接返 api 结果
    assert out == [{"api_football_event": 1}]
    assert api_called["count"] == 1
    # espn 也被并行调 (ThreadPoolExecutor) 但结果被忽略
    assert espn_called["count"] == 1


def test_fetch_events_d88_4_api_football_empty_falls_back_to_espn(fetch_mod, monkeypatch):
    """R49-2 D-88-4: 路由 api-football + 主结果空 -> 走 espn fallback。

    api_football 返 [] -> logger.info + 返 espn 结果。
    """
    def fake_api(dates_str, config):
        return []  # api 返回空

    def fake_espn(dates_str, league_slug="epl"):
        return [{"espn_event": "fallback"}]

    monkeypatch.setattr(fetch_mod, "fetch_api_football", fake_api)
    monkeypatch.setattr(fetch_mod, "fetch_espn", fake_espn)

    out = fetch_mod.fetch_events("20260715-20260720", league_key="epl",
                                    data_source="api-football")
    assert out == [{"espn_event": "fallback"}]
