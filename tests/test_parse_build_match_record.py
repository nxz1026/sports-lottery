"""R38-4: scripts/core/data/parse.py::_build_match_record 6 tests direct (Top 1) + D-27.

ESPN event → bucket dispatch + record builder. Pure function. 0 direct tests.

6 branches:
- D-27 past status enum: STATUS_FULL_TIME / STATUS_FINAL_PEN / STATUS_FINAL_ET all → "past"
- STATUS_SCHEDULED → "future"
- Other status (e.g., STATUS_IN_PROGRESS) → "in_progress" (default)
- score field: only populated for past status; "" for non-past
- odds_data_available bool: True if any of details/drawOdds/pointSpread/total present
- None propagation: chain when _extract_event_basics returns None
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


PAST_STATUSES = pytest.mark.parametrize(
    "status_name",
    ["STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_FINAL_ET"],
)


@pytest.fixture
def parse_mod():
    spec = importlib.util.spec_from_file_location(
        "parse_build_match_record_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def now_utc():
    return datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _ev(status_name, completed=True, with_odds=True, home_name="Liverpool",
        away_name="ManU", home_score="3", away_score="1"):
    """构造 ESPN 事件 fixture：含 status + competitors + (可选) odds。"""
    comp = {
        "status": {"type": {"name": status_name, "completed": completed}},
        "competitors": [
            {"homeAway": "home",
             "team": {"displayName": home_name, "abbreviation": "LIV"},
             "score": home_score, "form": "WWD",
             "records": [{"summary": "3-0-0"}]},
            {"homeAway": "away",
             "team": {"displayName": away_name, "abbreviation": "MUN"},
             "score": away_score, "form": "LDL", "records": []},
        ],
    }
    if with_odds:
        comp["odds"] = [{
            "details": "Liverpool -110",
            "drawOdds": {"moneyLine": "+250"},
            "pointSpread": {
                "home": {"close": {"line": "-1.5", "odds": "-110"}},
                "away": {"close": {"line": "+1.5", "odds": "-110"}},
            },
            "total": {
                "over": {"close": {"line": "2.5", "odds": "-110"}},
                "under": {"close": {"line": "2.5", "odds": "-110"}},
            },
        }]
    return {"name": f"{away_name} at {home_name}", "competitions": [comp]}


# ──────────────────────── D-27 status enum lock ────────────────────────


@PAST_STATUSES
def test_build_match_record_past_status_enum(parse_mod, now_utc, status_name):
    """R38-4 D-27: 3 past statuses all -> 'past' bucket.

    Lock _build_match_record 的 status enum dispatch:
    - STATUS_FULL_TIME  regular final
    - STATUS_FINAL_PEN  penalty shootout end
    - STATUS_FINAL_ET   extra-time end
    任一变更/拆解该枚举集合 -> 直接 fail。
    """
    bucket, rec = parse_mod._build_match_record(_ev(status_name), now_utc)
    assert bucket == "past"
    assert rec is not None


# ──────────────────────── future / in_progress branches ────────────────────────


def test_build_match_record_future_scheduled(parse_mod, now_utc):
    """R38-4: STATUS_SCHEDULED -> 'future' bucket, score ''. """
    bucket, rec = parse_mod._build_match_record(
        _ev("STATUS_SCHEDULED", completed=False), now_utc)
    assert bucket == "future"
    assert rec["score"] == ""


def test_build_match_record_in_progress_default(parse_mod, now_utc):
    """R38-4: 其他 status -> 'in_progress' bucket (default 分支兜底).

    else default branch covers live / halftime / postponed / 等未列出的 status。
    """
    bucket, rec = parse_mod._build_match_record(
        _ev("STATUS_IN_PROGRESS", completed=False), now_utc)
    assert bucket == "in_progress"
    assert rec["score"] == ""


# ──────────────────────── record fields ────────────────────────


def test_build_match_record_score_only_for_past(parse_mod, now_utc):
    """R38-4: score 仅 past 状态格式化；future/in_progress 空串。

    `score = f"{home}-{away}" if status in PAST else ""` 锁。
    """
    _, past = parse_mod._build_match_record(
        _ev("STATUS_FULL_TIME"), now_utc)
    assert past["score"] == "3-1"

    _, future = parse_mod._build_match_record(
        _ev("STATUS_SCHEDULED", completed=False), now_utc)
    assert future["score"] == ""

    _, live = parse_mod._build_match_record(
        _ev("STATUS_IN_PROGRESS", completed=False), now_utc)
    assert live["score"] == ""


def test_build_match_record_odds_data_available_flag(parse_mod, now_utc):
    """R38-4: odds_data_available = any(details/drawOdds/pointSpread/total) truthy.

    任一键存在即 True；odds 空 dict 全 None -> False。
    """
    # 含完整 odds -> True
    _, with_odds = parse_mod._build_match_record(
        _ev("STATUS_FULL_TIME"), now_utc)
    assert with_odds["odds_data_available"] is True

    # odds 完全空 -> False
    _, no_odds = parse_mod._build_match_record(
        _ev("STATUS_FULL_TIME", with_odds=False), now_utc)
    assert no_odds["odds_data_available"] is False


# ──────────────────────── None propagation ────────────────────────


def test_build_match_record_none_propagation(parse_mod, now_utc, monkeypatch):
    """R38-4: _extract_event_basics 返 None -> _build_match_record 返 (None, None).

    链式早退：parse_events 看到 None rec 跳过该事件。防御性代码虽当前
    实现在 _extract_event_basics 的 [{}] fallback 下永远不触发
    （comps 非空 -> 触发不到 `len == 0` 早退），但契约层保留。
    """
    monkeypatch.setattr(parse_mod, "_extract_event_basics",
                        lambda ev, now: None)
    bucket, rec = parse_mod._build_match_record(
        _ev("STATUS_FULL_TIME"), now_utc)
    assert bucket is None
    assert rec is None
