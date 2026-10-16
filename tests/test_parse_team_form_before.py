"""R37-7: scripts/core/data/parse.py::_team_form_before 5 测直测 + D-26 锁定（Top 7）。

现状：球队「kickoff 前最近 n 场」W/D/L 序列函数 0 直测。

D-26 bug-shaped 锁定：`ko >= kickoff` 严格不等号 + 字典序比较（要求
所有 kickoff_utc 均为 ISO8601 字典序可比）。

本轮补 5 测：
- 5 场历史 → 全收（WWDWL）
- 7 场历史 n=3 → 仅最后 3 场
- D-26：含 `ko == kickoff` → 跳过（严格不等）
- 主队赢 vs 客队输 → 视角翻转
- 球队名不存在 → 返 []
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
def parse_mod():
    spec = importlib.util.spec_from_file_location(
        "parse_team_form_before_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _team_form_before ────────────────────────


def test_team_form_before_5_history_collects_all(parse_mod):
    """R37-7：5 场 Arsenal 历史（按时间升序）→ 全收 ['W', 'W', 'D', 'W', 'L']。

    顺序按 fixture 出现顺序（最早 → 最近）；results[-n:] 取末尾。
    """
    completed = [
        ("2026-07-01T10:00:00Z", "Arsenal", "A", 2, 1),  # W (主)
        ("2026-07-03T10:00:00Z", "B", "Arsenal", 0, 1),  # W (客)
        ("2026-07-05T10:00:00Z", "Arsenal", "C", 0, 0),  # D
        ("2026-07-07T10:00:00Z", "Arsenal", "D", 1, 0),  # W
        ("2026-07-09T10:00:00Z", "Arsenal", "E", 0, 2),  # L
    ]
    out = parse_mod._team_form_before(
        completed, "Arsenal", "2026-07-15T10:00:00Z", n=5)
    assert out == ["W", "W", "D", "W", "L"]


def test_team_form_before_n3_returns_last_three(parse_mod):
    """R37-7：7 场历史 n=3 → 仅最后 3 场。"""
    completed = [
        ("2026-07-01T10:00:00Z", "Arsenal", "A", 2, 1),  # W
        ("2026-07-03T10:00:00Z", "Arsenal", "B", 1, 0),  # W
        ("2026-07-05T10:00:00Z", "Arsenal", "C", 0, 0),  # D
        ("2026-07-07T10:00:00Z", "Arsenal", "D", 1, 0),  # W
        ("2026-07-09T10:00:00Z", "Arsenal", "E", 0, 2),  # L
    ]
    out = parse_mod._team_form_before(
        completed, "Arsenal", "2026-07-15T10:00:00Z", n=3)
    assert out == ["D", "W", "L"]


def test_team_form_before_d26_ko_equals_kickoff_skipped(parse_mod):
    """R37-7 D-26 锁定：`ko >= kickoff` 严格不等 → `ko == kickoff` 跳过。

    字典序比较（要求所有 kickoff_utc 均为 ISO8601 字典序可比）。
    """
    completed = [
        ("2026-07-15T10:00:00Z", "Arsenal", "X", 1, 0),  # ko == kickoff → 跳过
    ]
    out = parse_mod._team_form_before(
        completed, "Arsenal", "2026-07-15T10:00:00Z", n=5)
    assert out == []


def test_team_form_before_home_away_perspective_flipped(parse_mod):
    """R37-7：主队赢 h>a → W；客队赢 h<a → W（视角翻转）。"""
    completed = [
        # 主场赢
        ("2026-07-01T10:00:00Z", "Arsenal", "Chelsea", 2, 1),  # h>a → W (主)
        # 客场赢
        ("2026-07-03T10:00:00Z", "Chelsea", "Arsenal", 0, 1),  # h<a → W (客)
    ]
    out = parse_mod._team_form_before(
        completed, "Arsenal", "2026-07-15T10:00:00Z", n=5)
    assert out == ["W", "W"]


def test_team_form_before_team_not_found_returns_empty(parse_mod):
    """R37-7：球队名不存在 → 返 []（不入 results）。"""
    completed = [
        ("2026-07-01T10:00:00Z", "Arsenal", "Chelsea", 2, 1),
    ]
    out = parse_mod._team_form_before(
        completed, "Liverpool", "2026-07-15T10:00:00Z", n=5)
    assert out == []