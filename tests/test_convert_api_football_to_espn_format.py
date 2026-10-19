"""R45-2: scripts/core/data/convert.py::convert_api_football_to_espn_format 5 cases direct + D-63 lock.

convert_api_football_to_espn_format (L236-L272, 37 行) 是 API-Football -> ESPN
转换的 orchestrator。R44-3 D-58 锁了 _map_api_football_status 短码映射,
R36-2 D-15 锁了 _build_odds_from_bookmaker, 但 orchestrator 自身核心契约
(odds_lookup 装载 + 空降级 + score 缺省 + name 格式) 0 直测。

D-63 锁定 5 契约:
- D-63-1: 主流程 happy (full fixture + odds_lookup 命中 + _build_odds 装配)
- D-63-2: odds_lookup=None -> odds=[] (无赔率数据降级)
- D-63-3: odds_lookup 不含 fixture_id -> odds=[] (跨 fixture 不污染)
- D-63-4: goals 缺失 -> home_score/away_score 默认 '0'
- D-63-5: data 缺 'response' 键 -> 返 [] (数据为空降级)

依赖:
- _build_odds_from_bookmaker monkeypatch (避免 R36-2 已测细节影响 D-63 断言)
- _map_api_football_status 真跑 (D-58 已锁, 间接断言 status 透传)
- _build_espn_event 真跑 (R45-3 D-64 待测, 本次仅断言 orchestrator 编排)
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
def convert_mod():
    """Spec-load scripts/core/data/convert.py 避免触发 scripts.core.__init__ 链。"""
    spec = importlib.util.spec_from_file_location(
        "convert_api_football_to_espn_format_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-63 fixture helpers ────────────────────────


def _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, return_value: dict):
    """Mock _build_odds_from_bookmaker 返可控 dict (隔离 R36-2 已测 helper)。"""
    monkeypatch.setattr(convert_mod, "_build_odds_from_bookmaker",
                        lambda bookmakers, home_team, away_team: return_value)


def _sample_fixture(fid: int = 1001, *, home: str = "Arsenal",
                    away: str = "Chelsea", status_short: str = "NS",
                    home_goals=None, away_goals=None) -> dict:
    """构造一个 API-Football style fixture dict。"""
    goals = {}
    if home_goals is not None:
        goals["home"] = home_goals
    if away_goals is not None:
        goals["away"] = away_goals
    return {
        "fixture": {"id": fid, "date": "2026-07-15T15:00:00+00:00",
                    "status": {"short": status_short}},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": goals,
    }


# ──────────────────────── D-63 五契约 ────────────────────────


def test_api_football_orchestrator_d63_1_happy_path_with_odds(convert_mod, monkeypatch):
    """R45-2 D-63-1: 主流程 — full fixture + odds_lookup 命中 + _build_odds 装配。

    1 fixture (id=1001) + odds_lookup 含 1001 -> _build_odds_from_bookmaker 被调,
    odds_entry = {moneyline: {...}} 传入 _build_espn_event.
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {"moneyline": {"h": 1.85}})
    events = convert_mod.convert_api_football_to_espn_format(
        data={"response": [_sample_fixture(fid=1001, status_short="NS")]},
        config={},
        odds_lookup={1001: [{"bookmaker": "Pinnacle"}]},
    )
    assert len(events) == 1
    e = events[0]
    comp = e["competitions"][0]
    assert comp["odds"] == [{"moneyline": {"h": 1.85}}]  # odds_entry 包成 list
    assert e["name"] == "Chelsea at Arsenal"  # 'away at home' 格式
    assert comp["status"]["type"]["name"] == "STATUS_SCHEDULED"  # NS -> D-58
    assert comp["status"]["type"]["completed"] is False
    home_comp = next(c for c in comp["competitors"] if c["homeAway"] == "home")
    assert home_comp["team"]["displayName"] == "Arsenal"
    assert home_comp["team"]["abbreviation"] == "ARS"  # 前 3 字符大写
    away_comp = next(c for c in comp["competitors"] if c["homeAway"] == "away")
    assert away_comp["team"]["displayName"] == "Chelsea"
    assert away_comp["team"]["abbreviation"] == "CHE"


def test_api_football_orchestrator_d63_2_no_odds_lookup(convert_mod, monkeypatch):
    """R45-2 D-63-2: odds_lookup=None -> odds=[] (无赔率数据降级)。

    odds_entry 默认 {}, odds=[odds_entry] if odds_entry else [] -> odds=[]。
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {"moneyline": {"h": 1.85}})  # 不应被调
    events = convert_mod.convert_api_football_to_espn_format(
        data={"response": [_sample_fixture()]},
        config={},
        odds_lookup=None,  # 关键: None
    )
    assert len(events) == 1
    assert events[0]["competitions"][0]["odds"] == []


def test_api_football_orchestrator_d63_3_odds_lookup_miss(convert_mod, monkeypatch):
    """R45-2 D-63-3: odds_lookup 不含 fixture_id -> odds=[] (跨 fixture 不污染)。

    fixture_id=2002 但 odds_lookup={1001: ...}, 不命中 -> odds_entry={} -> odds=[]。
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {"moneyline": {"h": 1.85}})
    events = convert_mod.convert_api_football_to_espn_format(
        data={"response": [_sample_fixture(fid=2002)]},
        config={},
        odds_lookup={1001: [{"bookmaker": "Pinnacle"}]},  # 1001 不含 2002
    )
    assert len(events) == 1
    assert events[0]["competitions"][0]["odds"] == []


def test_api_football_orchestrator_d63_4_missing_goals_defaults_to_zero(convert_mod, monkeypatch):
    """R45-2 D-63-4: goals 缺失 -> home_score/away_score 默认 '0'。

    `str(goals.get('home', 0) or 0)` -> None or 0 = 0 -> '0'.
    NS (未开赛) fixture 通常无 goals, 走默认 0 路径。
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {})
    events = convert_mod.convert_api_football_to_espn_format(
        data={"response": [_sample_fixture(status_short="NS", home_goals=None,
                                            away_goals=None)]},
        config={},
        odds_lookup=None,
    )
    assert len(events) == 1
    competitors = events[0]["competitions"][0]["competitors"]
    scores = {c["homeAway"]: c["score"] for c in competitors}
    assert scores["home"] == "0"
    assert scores["away"] == "0"


def test_api_football_orchestrator_d63_5_empty_response_returns_empty_list(convert_mod, monkeypatch):
    """R45-2 D-63-5: data 缺 'response' 键 -> 返 [] (数据为空降级)。

    `data.get('response', [])` -> [] -> for loop 0 次 -> 返 [].
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {})
    events = convert_mod.convert_api_football_to_espn_format(
        data={},  # 缺 'response'
        config={},
        odds_lookup=None,
    )
    assert events == []


def test_api_football_orchestrator_d63_6_status_completed_for_full_time(convert_mod, monkeypatch):
    """R45-2 D-63-6: 状态透传 — 'FT' -> STATUS_FULL_TIME + completed=True (D-58 集成)。

    verify orchestrator 把 _map_api_football_status 真结果透传给 _build_espn_event。
    """
    _stub_build_odds_from_bookmaker(convert_mod, monkeypatch, {})
    events = convert_mod.convert_api_football_to_espn_format(
        data={"response": [_sample_fixture(status_short="FT",
                                            home_goals=2, away_goals=1)]},
        config={},
        odds_lookup=None,
    )
    assert len(events) == 1
    comp = events[0]["competitions"][0]
    assert comp["status"]["type"]["name"] == "STATUS_FULL_TIME"
    assert comp["status"]["type"]["completed"] is True
    competitors = comp["competitors"]
    scores = {c["homeAway"]: c["score"] for c in competitors}
    assert scores["home"] == "2"
    assert scores["away"] == "1"
