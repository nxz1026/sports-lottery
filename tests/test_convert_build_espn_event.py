"""R45-3: scripts/core/data/convert.py::_build_espn_event 5 cases direct + D-64 lock.

_build_espn_event (L120-L162, 43 行) 是 ESPN event dict 构建器,
返回固定 schema 包装 {name, date, competitions[{status.type.{name,completed},
competitors[home/away].{homeAway, team.{displayName, abbreviation}, score,
form='', records=[]}, odds}]}.

R44-3 D-58 锁 _map_api_football_status, R36-2 D-15 锁 _build_odds_from_bookmaker,
R45-2 D-63 锁 convert_api_football_to_espn_format orchestrator, 但 _build_espn_event
自身核心契约 (structure 完整 + competitors 顺序 + form/records 默认值 + odds 透传)
0 直测。

D-64 锁定 5 契约:
- D-64-1: 主流程 happy (全 11 字段透传 + structure 准确)
- D-64-2: 空 odds=[] 透传 (无赔率 fixture)
- D-64-3: completed=False 状态透传 (NS/PST/...)
- D-64-4: form='' + records=[] 默认值 (UI 渲染必需字段)
- D-64-5: 多 odds list 透传 (e.g., 2 个 bookmaker)

依赖: 无外部依赖, 纯函数。
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
    """Spec-load convert.py (复用 R35/R45-2 套路)。"""
    spec = importlib.util.spec_from_file_location(
        "convert_build_espn_event_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-64 五契约 ────────────────────────


def test_build_espn_event_d64_1_happy_path_full_structure(convert_mod):
    """R45-3 D-64-1: 主流程 happy — 全 11 字段透传 + structure 准确。

    event dict 结构: name/date/competitions[0].status.type.{name,completed}/
    competitors[2]{home/away}.team.{displayName,abbreviation}/score/form=''/records=[]
    /odds 各自透传。
    """
    event = convert_mod._build_espn_event(
        name="Chelsea at Arsenal",
        date="2026-07-15T15:00:00+00:00",
        home_team="Arsenal",
        away_team="Chelsea",
        home_abbr="ARS",
        away_abbr="CHE",
        home_score="2",
        away_score="1",
        status="STATUS_FULL_TIME",
        completed=True,
        odds=[{"moneyline": {"h": 1.85}}],
    )
    assert event["name"] == "Chelsea at Arsenal"
    assert event["date"] == "2026-07-15T15:00:00+00:00"
    comp = event["competitions"][0]
    assert comp["status"]["type"]["name"] == "STATUS_FULL_TIME"
    assert comp["status"]["type"]["completed"] is True
    assert len(comp["competitors"]) == 2
    assert comp["odds"] == [{"moneyline": {"h": 1.85}}]


def test_build_espn_event_d64_2_empty_odds_passthrough(convert_mod):
    """R45-3 D-64-2: 空 odds=[] 透传 (无赔率 fixture)。

    NS/PST 阶段无 odds 数据, odds=[] 透传到 event.odds=[]。
    """
    event = convert_mod._build_espn_event(
        name="PSG at Marseille",
        date="2026-07-16T20:00:00+00:00",
        home_team="Marseille",
        away_team="PSG",
        home_abbr="MAR",
        away_abbr="PSG",
        home_score="0",
        away_score="0",
        status="STATUS_SCHEDULED",
        completed=False,
        odds=[],
    )
    assert event["competitions"][0]["odds"] == []


def test_build_espn_event_d64_3_completed_false_passthrough(convert_mod):
    """R45-3 D-64-3: completed=False 透传 (NS/PST/未完赛状态)。

    verify completed=False 准确写入 status.type.completed。
    """
    event = convert_mod._build_espn_event(
        name="Test",
        date="2026-07-15T15:00:00+00:00",
        home_team="H",
        away_team="A",
        home_abbr="HOM",
        away_abbr="AWY",
        home_score="0",
        away_score="0",
        status="STATUS_IN_PROGRESS",  # 进行中
        completed=False,
        odds=[],
    )
    assert event["competitions"][0]["status"]["type"]["completed"] is False
    assert event["competitions"][0]["status"]["type"]["name"] == "STATUS_IN_PROGRESS"


def test_build_espn_event_d64_4_competitors_order_and_defaults(convert_mod):
    """R45-3 D-64-4: competitors 顺序 + form/records 默认值。

    homeAway='home' 在前, homeAway='away' 在后 (ESPN 规范)。
    form/records 默认 '' / [] 是 UI 渲染必需字段 (不传会让模板 KeyError)。
    """
    event = convert_mod._build_espn_event(
        name="H vs A",
        date="2026-07-15T15:00:00+00:00",
        home_team="Home Team",
        away_team="Away Team",
        home_abbr="HOM",
        away_abbr="AWY",
        home_score="1",
        away_score="0",
        status="STATUS_FULL_TIME",
        completed=True,
        odds=[],
    )
    competitors = event["competitions"][0]["competitors"]
    # 顺序: home 先, away 后
    assert competitors[0]["homeAway"] == "home"
    assert competitors[1]["homeAway"] == "away"
    # home competitor
    assert competitors[0]["team"] == {"displayName": "Home Team",
                                       "abbreviation": "HOM"}
    assert competitors[0]["score"] == "1"
    assert competitors[0]["form"] == ""  # 默认空串
    assert competitors[0]["records"] == []  # 默认空 list
    # away competitor
    assert competitors[1]["team"] == {"displayName": "Away Team",
                                       "abbreviation": "AWY"}
    assert competitors[1]["score"] == "0"
    assert competitors[1]["form"] == ""
    assert competitors[1]["records"] == []


def test_build_espn_event_d64_5_multiple_odds_passthrough(convert_mod):
    """R45-3 D-64-5: 多 odds list 透传 (e.g., 2 个 bookmaker)。

    odds 列表可含多个 bookmaker dict, _build_espn_event 整体透传 (不解析)。
    """
    event = convert_mod._build_espn_event(
        name="Multi Bookie",
        date="2026-07-15T15:00:00+00:00",
        home_team="H",
        away_team="A",
        home_abbr="HOM",
        away_abbr="AWY",
        home_score="0",
        away_score="0",
        status="STATUS_SCHEDULED",
        completed=False,
        odds=[
            {"bookmaker": "Pinnacle", "moneyline": {"h": 1.85}},
            {"bookmaker": "Bet365", "moneyline": {"h": 1.90}},
        ],
    )
    assert len(event["competitions"][0]["odds"]) == 2
    assert event["competitions"][0]["odds"][0]["bookmaker"] == "Pinnacle"
    assert event["competitions"][0]["odds"][1]["bookmaker"] == "Bet365"
