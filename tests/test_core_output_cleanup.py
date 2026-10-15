"""R24 #3: core/output.cleanup_old_files 端到端测试。

cleanup_old_files 仅集成于 retrain 调度（无单元测），边界未验证：
  · 两目录不存在 → 返回 0，不抛穿
  · 跨天文件按 mtime cutoff 过滤（旧的删、新的留）
  · 子目录不被当文件删（只 iterdir + is_file 守门）
  · days=0 / days 极大两类边界
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def output_mod(tmp_path, monkeypatch):
    """加载 output.py 为唯一 module name，避免脚本运行时污染（与 R23 #4 同模式）。"""
    spec = importlib.util.spec_from_file_location(
        "output_mod_cleanup_test", REPO_ROOT / "scripts" / "core" / "output.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # from core.config import PREDICTIONS_DIR/RESULTS_DIR 是 from-import 别名；
    # 在此 module name 下绑定独立引用，必须 monkeypatch 本 module 属性才生效。
    pred_dir = tmp_path / "predictions"
    res_dir = tmp_path / "results"
    pred_dir.mkdir()
    res_dir.mkdir()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", pred_dir)
    monkeypatch.setattr(mod, "RESULTS_DIR", res_dir)
    return mod, pred_dir, res_dir


def _touch(path: Path, mtime_offset_days: float) -> None:
    """创建文件并设置 mtime = now - offset_days * 86400。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    target_mtime = time.time() - mtime_offset_days * 86400
    os.utime(path, (target_mtime, target_mtime))


def test_cleanup_returns_zero_when_both_dirs_missing(tmp_path):
    """R24 #3：predictions/ + results/ 都不存在 → 返回 0，不抛穿。"""
    # 用全新 tmp_path（空目录），让 output_mod 的 PREDICTIONS_DIR/RESULTS_DIR
    # 都指向 tmp_path/{not_existing_a,not_existing_b}
    spec = importlib.util.spec_from_file_location(
        "output_mod_cleanup_missing", REPO_ROOT / "scripts" / "core" / "output.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.PREDICTIONS_DIR = tmp_path / "nope_a"
    mod.RESULTS_DIR = tmp_path / "nope_b"

    assert mod.cleanup_old_files(days=7) == 0


def test_cleanup_removes_old_keeps_recent_predictions(output_mod):
    """R24 #3：predictions/ 中混合新旧文件，仅 mtime < cutoff 的被删。"""
    mod, pred_dir, res_dir = output_mod
    # 旧文件：8 天前（mtime < cutoff=now-7天 → 删）
    _touch(pred_dir / "prediction_2026-09-01_10.json", mtime_offset_days=8)
    # 边界内：3 天前（mtime > cutoff → 保留）
    _touch(pred_dir / "prediction_2026-09-15_15.json", mtime_offset_days=3)
    # 新文件：1 天前（保留）
    _touch(pred_dir / "prediction_2026-10-10_10.json", mtime_offset_days=1)
    # 旁挂一份 results/ 新文件
    _touch(res_dir / "result_2026-10-12.json", mtime_offset_days=1)
    # 旁挂一份 results/ 旧文件
    _touch(res_dir / "result_2026-08-12.json", mtime_offset_days=30)

    removed = mod.cleanup_old_files(days=7)
    # predictions 旧 1 删 + results 旧 1 删 = 2
    assert removed == 2
    remaining = sorted(p.name for p in pred_dir.iterdir())
    assert remaining == ["prediction_2026-09-15_15.json",
                         "prediction_2026-10-10_10.json"]
    res_remaining = sorted(p.name for p in res_dir.iterdir())
    assert res_remaining == ["result_2026-10-12.json"]


def test_cleanup_skips_subdirectories(output_mod):
    """R24 #3：子目录（含 .gitkeep / 子文件系统）不应被当文件删。"""
    mod, pred_dir, res_dir = output_mod
    # 子目录里放文件让其 mtime 老旧；cleanup 只看 iterdir top-level is_file
    sub = pred_dir / "subdir"
    sub.mkdir()
    _touch(sub / "old_in_subdir.json", mtime_offset_days=100)
    # 顶层旧文件应被删
    _touch(pred_dir / "prediction_2026-01-01_00.json", mtime_offset_days=100)
    # 顶层新文件保留
    _touch(pred_dir / "prediction_2026-10-13_00.json", mtime_offset_days=0)

    removed = mod.cleanup_old_files(days=7)
    # 子目录不被删；只看顶层
    assert removed == 1
    assert (pred_dir / "subdir").is_dir()
    assert (sub / "old_in_subdir.json").exists()
    # 删的是顶层旧文件
    assert not (pred_dir / "prediction_2026-01-01_00.json").exists()
    assert (pred_dir / "prediction_2026-10-13_00.json").exists()


def test_cleanup_days_zero_removes_everything(output_mod):
    """R24 #3：days=0（cutoff=now）→ mtime < cutoff 的全删；显式 backdate 避免 mtime 漂移。"""
    mod, pred_dir, res_dir = output_mod
    # 全部 mtime 后退 60s（远大于文件系统时间戳精度 ~1s），必 < now cutoff
    for name in ("prediction_a.json", "prediction_b.json", "result_a.json"):
        target = (pred_dir if "prediction" in name else res_dir) / name
        _touch(target, mtime_offset_days=60 / 86400)
    # 增加一份 mtime 推进到 1 小时后的"未来"文件 → mtime > cutoff 保留
    future = pred_dir / "future.json"
    future.write_text("{}", encoding="utf-8")
    future_mtime = time.time() + 3600
    os.utime(future, (future_mtime, future_mtime))

    removed = mod.cleanup_old_files(days=0)
    # 旧的三个全删；future 因 mtime 推进至未来，cutoff=now 时不被删
    assert removed == 3
    assert (pred_dir / "future.json").exists()


def test_cleanup_skips_when_dir_exists_but_empty(output_mod):
    """R24 #3：两目录都存在但空 → removed=0。"""
    mod, pred_dir, res_dir = output_mod
    # 目录已由 fixture 创建，iterdir 为空
    assert mod.cleanup_old_files(days=7) == 0
    assert list(pred_dir.iterdir()) == []
    assert list(res_dir.iterdir()) == []
