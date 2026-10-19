"""R44-1: web/routers/predictions.py::_bball_extras 5 cases direct + D-56 lock.

篮球 extras 抽取 helper 0 直测; _prediction_summary 间接调用, 但 helper
自身契约 (5 字段 + sport="basketball" 标签) 未锁。

helper 含三条契约 (D-56):
- 5 字段透传: spread_pred / total_pred / win_prob / kickoff_date / sport
- 4 字段默认 "" (doc 缺时兜底)
- sport 恒等于 "basketball" (不分 league 标识)

5 tests (EASY):
- 完整 doc: 4 fields 透传 + sport="basketball"
- 缺 4 字段: 全部默认 "" + sport="basketball"
- sport 字段不被覆盖 (即使 doc 含 sport)
- 空 doc: 5 keys 全默认 (sport 仍 "basketball")
- D-56 字段顺序保持 (spread_pred 先, sport 最后, 可观测 UI 排版顺序)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers.predictions import _bball_extras

# ──────────────────────── D-56 三契约 ────────────────────────


def test_bball_extras_d56_full_doc_passthrough():
    """R44-1 D-56: 完整 doc -> 5 字段全透传。"""
    doc = {
        "spread_pred": -3.5, "total_pred": 215.5,
        "win_prob": 0.65, "kickoff_date": "2026-10-15",
    }
    out = _bball_extras(doc)
    assert out == {
        "spread_pred": -3.5, "total_pred": 215.5,
        "win_prob": 0.65, "kickoff_date": "2026-10-15",
        "sport": "basketball",
    }


def test_bball_extras_d56_missing_fields_default_empty():
    """R44-1 D-56: doc 缺 4 字段 -> 默认 "" (不抛穿)。"""
    out = _bball_extras({})
    assert out == {
        "spread_pred": "", "total_pred": "",
        "win_prob": "", "kickoff_date": "",
        "sport": "basketball",
    }


def test_bball_extras_d56_sport_always_basketball():
    """R44-1 D-56: sport 字段恒 = "basketball", 即使 doc 含 sport 也不被覆盖。"""
    # 即使 doc 给 sport="football", helper 强制写 "basketball"
    doc = {"sport": "football", "spread_pred": -1.0}
    out = _bball_extras(doc)
    assert out["sport"] == "basketball"


def test_bball_extras_d56_empty_doc_returns_5_keys():
    """R44-1 D-56: 完全空 doc -> 仍返 5 字段 (sport 不缺省)。"""
    out = _bball_extras({})
    assert set(out.keys()) == {"spread_pred", "total_pred", "win_prob",
                              "kickoff_date", "sport"}
    assert len(out) == 5


def test_bball_extras_d56_field_order_observable():
    """R44-1 D-56: 字段顺序保持 (spread_pred 先, sport 最后), UI 排版可观测。

    Python 3.7+ dict 保 insertion order; 锁 helper 输出顺序, 防止后人加
    键时插到中间破坏 UI 模板。
    """
    out = _bball_extras({"spread_pred": -3.5})
    keys = list(out.keys())
    assert keys == ["spread_pred", "total_pred", "win_prob",
                     "kickoff_date", "sport"]
