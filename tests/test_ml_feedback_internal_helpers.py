"""R34-5: ml_feedback._result_label + _safe_log_event + retrain_baseline._normalize_result_label 8 测。

现状：
- ml_feedback._result_label (line 117-122) — events.jsonl outcome 契约入口，0 直测
- ml_feedback._safe_log_event (line 125-130) — 写 events.jsonl OSError 吞，0 直测
- retrain_baseline._normalize_result_label (line 66-73) — H/D/A 归一映射，0 直测

D 类契约：
- _safe_log_event 只 catch OSError；TypeError/ValueError 等仍上抛（D 类契约钉死）。

本轮补 8 测：
- _result_label home > away → "home_win"
- _result_label home < away → "away_win"
- _result_label home == away → "draw"
- _safe_log_event 吞 OSError + logger.warning
- _safe_log_event 非 OSError（TypeError）仍上抛
- _normalize_result_label 透传 H/D/A（旧 schema）
- _normalize_result_label 映射 home_win/away_win/draw → H/A/D
- _normalize_result_label 未识别 → None
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
def ml_feedback_mod():
    spec = importlib.util.spec_from_file_location(
        "ml_feedback_internal_test",
        REPO_ROOT / "scripts" / "core" / "ml_feedback.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def retrain_baseline_mod():
    spec = importlib.util.spec_from_file_location(
        "retrain_baseline_internal_test",
        REPO_ROOT / "scripts" / "core" / "retrain_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── ml_feedback._result_label ────────────────────────


def test_ml_feedback_result_label_home_win(ml_feedback_mod):
    """R34-5：home > away → "home_win"（events.jsonl outcome 契约）。"""
    assert ml_feedback_mod._result_label(2, 1) == "home_win"


def test_ml_feedback_result_label_away_win(ml_feedback_mod):
    """R34-5：home < away → "away_win"。"""
    assert ml_feedback_mod._result_label(1, 2) == "away_win"


def test_ml_feedback_result_label_draw(ml_feedback_mod):
    """R34-5：home == away → "draw"（含 0-0）。"""
    assert ml_feedback_mod._result_label(1, 1) == "draw"
    assert ml_feedback_mod._result_label(0, 0) == "draw"


# ──────────────────────── ml_feedback._safe_log_event ────────────────────────


def test_ml_feedback_safe_log_event_swallows_oserror(ml_feedback_mod, monkeypatch, caplog):
    """R34-5：_obs.log_event 抛 OSError → 吞 + logger.warning（D 类契约）。"""
    def boom_log_event(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(ml_feedback_mod._obs, "log_event", boom_log_event)

    with caplog.at_level("WARNING"):
        # 不抛异常
        ml_feedback_mod._safe_log_event("test_kind", foo="bar")
    # warning 日志
    assert any(
        "ml_feedback log_event" in r.message and "test_kind" in r.message
        for r in caplog.records
    ), f"missing warning, got: {[r.message for r in caplog.records]}"


def test_ml_feedback_safe_log_event_propagates_typeerror(ml_feedback_mod, monkeypatch):
    """R34-5：_obs.log_event 抛 TypeError（非 OSError）→ 仍上抛（D 类契约钉死）。

    只 catch OSError 是有意设计；其他异常让上层知道。锁定防 silent 切换为
    `except Exception` 时未覆盖旧调用点。
    """
    def boom_log_event(*args, **kwargs):
        raise TypeError("invalid arg")
    monkeypatch.setattr(ml_feedback_mod._obs, "log_event", boom_log_event)

    with pytest.raises(TypeError):
        ml_feedback_mod._safe_log_event("test_kind")


# ──────────────────────── retrain_baseline._normalize_result_label ────────────────────────


def test_retrain_normalize_passes_through_H_D_A(retrain_baseline_mod):
    """R34-5：H/D/A 旧 schema 透传（`raw in _RESULT_TO_INDEX` 直接返 raw）。"""
    assert retrain_baseline_mod._normalize_result_label("H") == "H"
    assert retrain_baseline_mod._normalize_result_label("D") == "D"
    assert retrain_baseline_mod._normalize_result_label("A") == "A"


def test_retrain_normalize_maps_home_away_draw(retrain_baseline_mod):
    """R34-5：ml_feedback schema home_win/away_win/draw → H/A/D。"""
    assert retrain_baseline_mod._normalize_result_label("home_win") == "H"
    assert retrain_baseline_mod._normalize_result_label("away_win") == "A"
    assert retrain_baseline_mod._normalize_result_label("draw") == "D"


def test_retrain_normalize_unknown_returns_none(retrain_baseline_mod):
    """R34-5：未识别字符串 → None（评估时跳过）。"""
    assert retrain_baseline_mod._normalize_result_label("unknown") is None
    assert retrain_baseline_mod._normalize_result_label("") is None
    assert retrain_baseline_mod._normalize_result_label("HOME_WIN") is None  # 大小写敏感
