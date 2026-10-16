"""R38-3: scripts/core/data/parse.py::_extract_event_basics 7 tests direct (Top 2).

ESPN event basic field parser 0 direct tests; R38-4 _build_match_record
predecessor. Covers name parsing + missing field fallbacks + competitors
fallback + status fields.

7 branches:
- ESPN "A at B" -> name = to_cn(home) vs to_cn(away)
- Missing "at" -> to_cn(name)
- Missing competitions -> fallback [{}] still returns record (bug-shaped)
- date_str unparseable -> kickoff = now_utc, time_to ~ 0
- Missing home competitor -> home_name="?", home_score="0"
- Missing away competitor -> away_name="?", away_score="0"
- Status field: status + completed
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def parse_mod():
    spec = importlib.util.spec_from_file_location(
        "parse_extract_event_basics_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def now_utc():
    return datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _home_away_comps(home_name="Liverpool", away_name="ManU",
                    home_score="2", away_score="1"):
    return [{"competitors": [
        {"homeAway": "home",
         "team": {"displayName": home_name, "abbreviation": "LIV"},
         "score": home_score, "form": "WWD", "records": [{"wins": 3}]},
        {"homeAway": "away",
         "team": {"displayName": away_name, "abbreviation": "MUN"},
         "score": away_score, "form": "LDL", "records": []},
    ]}]


def test_extract_event_basics_at_separator(parse_mod, now_utc):
    """R38-3: ESPN "A at B" -> name = to_cn(home) vs to_cn(away)."""
    ev = {
        "name": "Manchester United at Liverpool",
        "competitions": _home_away_comps(),
    }
    out = parse_mod._extract_event_basics(ev, now_utc)
    # 验证 name 含 " vs " 分隔符
    assert " vs " in out["name"]
    # i18n.to_cn 翻译："Manchester United" → "曼联"，"Liverpool" → "利物浦"
    # 用 bytes 比较避免控制台编码错位
    name_bytes = out["name"].encode("utf-8")
    assert ("曼联" in name_bytes.decode("utf-8")
            or "Manchester United" in name_bytes.decode("utf-8"))
    assert ("利物浦" in name_bytes.decode("utf-8")
            or "Liverpool" in name_bytes.decode("utf-8"))


def test_extract_event_basics_no_at_separator(parse_mod, now_utc):
    """R38-3: name no " at " -> to_cn(name)."""
    ev = {"name": "Single Team Event", "competitions": _home_away_comps()}
    out = parse_mod._extract_event_basics(ev, now_utc)
    # "Single Team Event" 不是英文队名 → to_cn 返原串
    assert "Single Team Event" in out["name"]


def test_extract_event_basics_missing_competitions_returns_fallback(parse_mod, now_utc):
    """R38-3: missing competitions -> fallback [{}] still returns record (D-24).

    `comps = ev.get("competitions", [{}])` fallback，触发不到 `len == 0` 早退。
    """
    ev = {"name": "No Comps Event"}
    out = parse_mod._extract_event_basics(ev, now_utc)
    assert out is not None  # 不返 None（bug-shaped 行为）
    assert out["home_name"] == "?"
    assert out["away_name"] == "?"
    assert out["home_score"] == "0"
    assert out["away_score"] == "0"


def test_extract_event_basics_bad_date_uses_now_utc(parse_mod, now_utc):
    """R38-3: date_str unparseable -> kickoff = now_utc, time_to ~ 0."""
    ev = {
        "name": "X at Y",
        "date": "not_a_date",
        "competitions": _home_away_comps(),
    }
    out = parse_mod._extract_event_basics(ev, now_utc)
    assert abs(out["time_to"]) < 1e-6
    assert out["date_str"] == "not_a_date"


def test_extract_event_basics_missing_home_competitor(parse_mod, now_utc):
    """R38-3: missing home competitor -> home_name="?", home_score="0"."""
    ev = {
        "name": "X at Y",
        "competitions": [{"competitors": [
            {"homeAway": "away",
             "team": {"displayName": "Y", "abbreviation": "Y"}},
        ]}],
    }
    out = parse_mod._extract_event_basics(ev, now_utc)
    assert out["home_name"] == "?"
    assert out["home_score"] == "0"
    assert out["away_name"] != "?"


def test_extract_event_basics_missing_away_competitor(parse_mod, now_utc):
    """R38-3: missing away competitor -> away_name="?", away_score="0"."""
    ev = {
        "name": "X at Y",
        "competitions": [{"competitors": [
            {"homeAway": "home",
             "team": {"displayName": "X", "abbreviation": "X"}},
        ]}],
    }
    out = parse_mod._extract_event_basics(ev, now_utc)
    assert out["away_name"] == "?"
    assert out["away_score"] == "0"
    assert out["home_name"] != "?"


def test_extract_event_basics_status_and_completed_fields(parse_mod, now_utc):
    """R38-3: status + completed extracted from competitions[0].status.type."""
    ev = {
        "name": "X at Y",
        "competitions": [{
            "status": {"type": {"name": "STATUS_FULL_TIME", "completed": True}},
            "competitors": [],
        }],
    }
    out = parse_mod._extract_event_basics(ev, now_utc)
    assert out["status"] == "STATUS_FULL_TIME"
    assert out["completed"] is True