"""R47-1: scripts/core/data/convert.py::convert_football_data_to_espn_format 5 cases direct + D-73 lock.

convert_football_data_to_espn_format (L165-L206, 42 行) 是 football-data.org
-> ESPN 转换的 orchestrator。与 R45-2 D-63 (API-Football) 对称, 但输入格式
不同 (status 短码 / score 嵌套 fullTime / t3 abbr) 且无赔率。

D-58/D-63/D-64 已锁 _map_api_football_status / convert_api_football_to_espn_format
/ _build_espn_event, 但 football-data.org orchestrator 自身核心契约
(status 映射 8 短码 + 缺省值 + t3 abbr + 0-0 默认) 0 直测。

D-73 锁定 5 契约:
- D-73-1: 主流程 happy (FINISHED + score=2-1 + 全字段) -> STATUS_FULL_TIME + completed=True
- D-73-2: SCHEDULED + score 缺省 -> STATUS_SCHEDULED + 0/0
- D-73-3: 未知 status -> fallback STATUS_SCHEDULED
- D-73-4: 缺 homeTeam/awayTeam 字段 -> "Unknown" 兜底 (不会 TypeError 因 get({}, 't3'))
- D-73-5: t3 abbr 缺 -> "UNK" 兜底 (与 homeTeam 都缺时一致)

依赖 stub:
- _build_espn_event 真跑 (D-64 已锁, 复用 R45-3 套路)
- 纯函数, 无外部依赖
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
    """Spec-load convert.py (复用 R45-2 套路)。"""
    spec = importlib.util.spec_from_file_location(
        "convert_football_data_to_espn_format_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sample_match(*, status: str = "FINISHED", home: str = "Arsenal",
                  away: str = "Chelsea", home_goals: int = 2,
                  away_goals: int = 1, home_t3: str = "ARS",
                  away_t3: str = "CHE", include_score: bool = True,
                  utc_date: str = "2026-07-15T15:00:00Z") -> dict:
    """构造一个 football-data.org style match dict。"""
    m = {
        "homeTeam": {"name": home, "t3": home_t3},
        "awayTeam": {"name": away, "t3": away_t3},
        "status": status,
        "utcDate": utc_date,
    }
    if include_score:
        m["score"] = {"fullTime": {"home": home_goals, "away": away_goals}}
    return m


# ──────────────────────── D-73 五契约 ────────────────────────


def test_football_data_orchestrator_d73_1_happy_path_full_match(convert_mod):
    """R47-1 D-73-1: 主流程 happy - FINISHED + score 2-1 + 全字段。

    match FINISHED + score.fullTime(2,1) -> STATUS_FULL_TIME + completed=True
    + home_score='2'/away_score='1' + abbr ARS/CHE。
    """
    events = convert_mod.convert_football_data_to_espn_format(
        data={"matches": [_sample_match()]},
        config={},
    )
    assert len(events) == 1
    e = events[0]
    comp = e["competitions"][0]
    assert e["name"] == "Chelsea at Arsenal"  # 'away at home' 格式
    assert e["date"] == "2026-07-15T15:00:00Z"
    assert comp["status"]["type"]["name"] == "STATUS_FULL_TIME"
    assert comp["status"]["type"]["completed"] is True
    competitors = comp["competitors"]
    scores = {c["homeAway"]: c["score"] for c in competitors}
    assert scores["home"] == "2"
    assert scores["away"] == "1"
    abbrs = {c["homeAway"]: c["team"]["abbreviation"] for c in competitors}
    assert abbrs["home"] == "ARS"
    assert abbrs["away"] == "CHE"
    # football-data.org orchestrator 无赔率, odds=[]
    assert comp["odds"] == []


def test_football_data_orchestrator_d73_2_scheduled_no_score_defaults_zero(convert_mod):
    """R47-1 D-73-2: SCHEDULED + score 缺省 -> STATUS_SCHEDULED + 0/0。

    SCHEDULED 比赛无 score 字段, _build_espn_event home_score='0' (default)
    + status=STATUS_SCHEDULED + completed=False。
    """
    events = convert_mod.convert_football_data_to_espn_format(
        data={"matches": [_sample_match(status="SCHEDULED", include_score=False)]},
        config={},
    )
    assert len(events) == 1
    comp = events[0]["competitions"][0]
    assert comp["status"]["type"]["name"] == "STATUS_SCHEDULED"
    assert comp["status"]["type"]["completed"] is False
    competitors = comp["competitors"]
    scores = {c["homeAway"]: c["score"] for c in competitors}
    assert scores["home"] == "0"
    assert scores["away"] == "0"


def test_football_data_orchestrator_d73_3_unknown_status_falls_back_to_scheduled(convert_mod):
    """R47-1 D-73-3: 未知 status -> fallback STATUS_SCHEDULED。

    status_map.get('GARBAGE', 'STATUS_SCHEDULED') 兜底。
    """
    events = convert_mod.convert_football_data_to_espn_format(
        data={"matches": [_sample_match(status="GARBAGE_STATE")],
              "config": {}},
        config={},
    )
    assert len(events) == 1
    comp = events[0]["competitions"][0]
    assert comp["status"]["type"]["name"] == "STATUS_SCHEDULED"
    assert comp["status"]["type"]["completed"] is False


def test_football_data_orchestrator_d73_4_missing_team_defaults_to_unknown(convert_mod):
    """R47-1 D-73-4: 缺 homeTeam/awayTeam 字段 -> 'Unknown' 兜底。

    L171 `match.get('homeTeam', {}).get('name', 'Unknown')` 安全链: 缺 homeTeam
    返 {} -> .get('name', 'Unknown') 返 'Unknown'。不会 TypeError。
    """
    events = convert_mod.convert_football_data_to_espn_format(
        data={"matches": [{"status": "FINISHED", "utcDate": "2026-07-15T15:00:00Z",
                            "score": {"fullTime": {"home": 1, "away": 0}}}]},
        # homeTeam/awayTeam 完全缺失
        config={},
    )
    assert len(events) == 1
    e = events[0]
    competitors = e["competitions"][0]["competitors"]
    home_comp = next(c for c in competitors if c["homeAway"] == "home")
    away_comp = next(c for c in competitors if c["homeAway"] == "away")
    assert home_comp["team"]["displayName"] == "Unknown"
    assert away_comp["team"]["displayName"] == "Unknown"
    # abbr 也走 UNK 兜底
    assert home_comp["team"]["abbreviation"] == "UNK"
    assert away_comp["team"]["abbreviation"] == "UNK"


def test_football_data_orchestrator_d73_5_empty_matches_returns_empty_list(convert_mod):
    """R47-1 D-73-5: data 缺 'matches' 键 -> 返 [] (空降级, 与 R45-2 D-63-5 对称)。

    `data.get('matches', [])` -> [] -> for loop 0 次 -> 返 []。
    """
    events = convert_mod.convert_football_data_to_espn_format(
        data={},  # 缺 'matches'
        config={},
    )
    assert events == []