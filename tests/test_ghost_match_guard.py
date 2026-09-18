"""C-4 幽灵场次守卫：完赛仍被数据源标为未开赛的场次必须被剔除。

线上背景：西甲「莱万特 vs 毕尔巴鄂竞技」开球 2026-09-16T00:00Z，两天后
（09-18）仍出现在今日推荐视图（数据源状态滞后 / 改期未同步）。predict.py
新增 _drop_ghost_future：开球时刻距今超过 GHOST_MATCH_GRACE_HOURS（3h）的
「未来」场次判为幽灵并剔除，开球时间缺失或不可解析的场次保留（无时刻
可证伪，留给页面「时间待定」，不误删）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _run(cases: list[dict]) -> tuple[list, list]:
    from predict import _drop_ghost_future

    return _drop_ghost_future(cases, NOW)


def test_ghost_in_past_is_dropped():
    kept, dropped = _run([{"name": "ghost", "kickoff_utc": "2026-09-16T00:00:00Z"}])
    assert [m["name"] for m in kept] == []
    assert [m["name"] for m in dropped] == ["ghost"]


def test_within_grace_window_is_kept():
    kept, dropped = _run([{"name": "recent", "kickoff_utc": "2026-09-18T11:30:00Z"}])
    assert [m["name"] for m in kept] == ["recent"]
    assert dropped == []


def test_future_is_kept():
    kept, dropped = _run([{"name": "future", "kickoff_utc": "2026-09-18T15:00:00Z"}])
    assert [m["name"] for m in kept] == ["future"]
    assert dropped == []


def test_exact_grace_boundary_is_kept():
    """恰好 3h（未超过）保留；剔除条件是严格大于。"""
    kept, dropped = _run([{"name": "edge", "kickoff_utc": "2026-09-18T09:00:00Z"}])
    assert [m["name"] for m in kept] == ["edge"]
    assert dropped == []


def test_missing_or_bad_kickoff_is_kept():
    """无时刻不可证伪：保留（不误删），交由页面「时间待定」兜底。"""
    cases = [
        {"name": "empty", "kickoff_utc": ""},
        {"name": "absent"},
        {"name": "garbage", "kickoff_utc": "not-a-date"},
    ]
    kept, dropped = _run(cases)
    assert [m["name"] for m in kept] == ["empty", "absent", "garbage"]
    assert dropped == []


def test_mixed_batch():
    cases = [
        {"name": "ok-future", "kickoff_utc": "2026-09-19T00:00:00Z"},
        {"name": "ghost", "kickoff_utc": "2026-09-15T00:00:00Z"},
        {"name": "no-time", "kickoff_utc": ""},
        {"name": "ghost2", "kickoff_utc": "2026-09-17T10:00:00Z"},
    ]
    kept, dropped = _run(cases)
    assert [m["name"] for m in kept] == ["ok-future", "no-time"]
    assert [m["name"] for m in dropped] == ["ghost", "ghost2"]
