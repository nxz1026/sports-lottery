"""R46-3: web/services/health.py::_probe_predictions_fresh 5 cases direct + D-70 lock.

_probe_predictions_fresh (L42-L71, 30 行) 是 /health 探活关键: 最新 prediction
文件 mtime < max_age_hours。R44 P1-4 修复时 test_health.py 加了 1 测
(test_unit_probe_predictions_handles_missing_dir), 但 happy path + stale +
多文件取最新 仍 0 直测。

D-70 锁定 5 契约:
- D-70-1: 目录不存在 -> ok=True + note="predictions/ not yet provisioned" (首次启动免阻塞)
- D-70-2: 目录存在但空 -> ok=True + note="no prediction files yet"
- D-70-3: 目录 + 文件 fresh (< max_age_hours) -> ok=True + freshest_age_hours
- D-70-4: 目录 + 文件 stale (> max_age_hours) -> ok=False + freshest_age_hours
- D-70-5: 多文件取 mtime 最新 (max over files, 非 dir 顺序)

依赖: 无, 纯函数 (time.time 走真实时间)。
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def health_mod():
    """Spec-load web/services/health.py。"""
    spec = importlib.util.spec_from_file_location(
        "health_probe_predictions_fresh_test",
        REPO_ROOT / "web" / "services" / "health.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-70 五契约 ────────────────────────


def test_probe_predictions_fresh_d70_1_missing_dir_returns_ok_with_note(health_mod, tmp_path):
    """R46-3 D-70-1: 目录不存在 -> ok=True + note (首次启动免阻塞)。

    L48-L53: not d.is_dir() -> ok=True + "predictions/ not yet provisioned"。
    """
    nonexistent = tmp_path / "predictions_nope"
    r = health_mod._probe_predictions_fresh(nonexistent)
    assert r["ok"] is True
    assert "not yet provisioned" in r["note"]
    assert r["freshest_age_hours"] is None


def test_probe_predictions_fresh_d70_2_empty_dir_returns_ok_with_note(health_mod, tmp_path):
    """R46-3 D-70-2: 目录存在但空 -> ok=True + note (无文件免阻塞)。

    L55-L60: files 为空 -> ok=True + "no prediction files yet"。
    """
    empty = tmp_path / "empty_predictions"
    empty.mkdir()
    r = health_mod._probe_predictions_fresh(empty)
    assert r["ok"] is True
    assert "no prediction files yet" in r["note"]
    assert r["freshest_age_hours"] is None


def test_probe_predictions_fresh_d70_3_fresh_file_returns_ok(health_mod, tmp_path):
    """R46-3 D-70-3: 目录 + 文件 fresh (< max_age_hours) -> ok=True。

    1 个文件 mtime=1h 前, max_age=24 -> age=1 < 24 -> ok=True。
    """
    pd = tmp_path / "predictions"
    pd.mkdir()
    f = pd / "today.json"
    f.write_text('{"predictions": []}', encoding="utf-8")
    os_utime_safe(f, age_seconds=3600)  # 1h 前

    r = health_mod._probe_predictions_fresh(pd, max_age_hours=24)
    assert r["ok"] is True
    assert r["freshest_file"] == "today.json"
    assert r["freshest_age_hours"] < 24
    assert 0 < r["freshest_age_hours"]  # 1h 左右
    assert r["max_age_hours"] == 24


def test_probe_predictions_fresh_d70_4_stale_file_returns_false(health_mod, tmp_path):
    """R46-3 D-70-4: 目录 + 文件 stale (> max_age_hours) -> ok=False。

    1 个文件 mtime=25h 前, max_age=24 -> age=25 > 24 -> ok=False (引擎卡住告警)。
    """
    pd = tmp_path / "predictions"
    pd.mkdir()
    f = pd / "yesterday.json"
    f.write_text('{"predictions": []}', encoding="utf-8")
    os_utime_safe(f, age_seconds=25 * 3600)  # 25h 前

    r = health_mod._probe_predictions_fresh(pd, max_age_hours=24)
    assert r["ok"] is False
    assert r["freshest_file"] == "yesterday.json"
    assert r["freshest_age_hours"] >= 24
    assert r["max_age_hours"] == 24


def test_probe_predictions_fresh_d70_5_picks_freshest_by_mtime(health_mod, tmp_path):
    """R46-3 D-70-5: 多文件取 mtime 最新 (max over files, 非 dir 顺序)。

    3 文件: a=20h 前, b=1h 前, c=10h 前 -> freshest=b.json (age 1h)。
    创建顺序故意放 c 最后, 验证 L61 `max(files, key=lambda p: p.stat().st_mtime)`
    准确取 mtime 最大者, 不依赖 dir 顺序或创建顺序。
    """
    pd = tmp_path / "predictions"
    pd.mkdir()
    # 故意非按时间顺序创建
    a = pd / "old.json"
    a.write_text('{}', encoding="utf-8")
    os_utime_safe(a, age_seconds=20 * 3600)

    c = pd / "mid.json"
    c.write_text('{}', encoding="utf-8")
    os_utime_safe(c, age_seconds=10 * 3600)

    b = pd / "fresh.json"
    b.write_text('{}', encoding="utf-8")
    os_utime_safe(b, age_seconds=3600)  # 1h 前 (最新)

    r = health_mod._probe_predictions_fresh(pd, max_age_hours=24)
    assert r["ok"] is True  # 1h < 24
    assert r["freshest_file"] == "fresh.json"  # b 不是 a 或 c
    assert r["freshest_age_hours"] < 2  # 1h 左右


# ──────────────────────── helper ────────────────────────


def os_utime_safe(path: Path, age_seconds: float):
    """设置文件 mtime 为 (now - age_seconds),跨平台用 time.time()。"""
    target = time.time() - age_seconds
    import os
    os.utime(path, (target, target))