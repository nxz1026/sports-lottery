"""R37-6: scripts/core/data/parse.py::_collect_completed 5 测直测（Top 6）。

现状：状态/战绩回填链路预处理函数 0 直测；缺它则 `_team_form_before`
无法工作。football-data 数据源无 form 时靠此函数推导。

5 分支：
- 正常 "2-1" → 5-tuple
- "0-0" 平局 → 5-tuple
- score 缺 "-" → 跳过
- score 非整数 → 跳过
- 空 list → []
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
        "parse_collect_completed_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _collect_completed ────────────────────────


def test_collect_completed_normal_score(parse_mod):
    """R37-6：正常 "2-1" → 5-tuple 完整解析。"""
    out = parse_mod._collect_completed([
        {"kickoff_utc": "2026-07-15T10:00:00Z", "home_en": "Arsenal",
         "away_en": "Chelsea", "score": "2-1"},
    ])
    assert out == [("2026-07-15T10:00:00Z", "Arsenal", "Chelsea", 2, 1)]


def test_collect_completed_draw_score(parse_mod):
    """R37-6：平局 "0-0" → 5-tuple。"""
    out = parse_mod._collect_completed([
        {"kickoff_utc": "2026-07-15T12:00:00Z", "home_en": "Liverpool",
         "away_en": "ManU", "score": "0-0"},
    ])
    assert out == [("2026-07-15T12:00:00Z", "Liverpool", "ManU", 0, 0)]


def test_collect_completed_score_missing_dash_skipped(parse_mod):
    """R37-6：score 缺 "-" → 跳过（continue）。"""
    out = parse_mod._collect_completed([
        {"score": "no_dash"},
        {"score": "x"},
    ])
    assert out == []


def test_collect_completed_score_non_int_skipped(parse_mod):
    """R37-6：score 非整数 "a-b" → 跳过（ValueError 兜底）。"""
    out = parse_mod._collect_completed([
        {"score": "a-b"},
        {"score": "2-x"},
    ])
    assert out == []


def test_collect_completed_empty_list_returns_empty(parse_mod):
    """R37-6：空 list → []（不抛错）。"""
    assert parse_mod._collect_completed([]) == []
