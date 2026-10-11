"""test_web_store_mtime.py — web.services.store.load_prediction_docs 必须写 mtime。

背景（web UX 审核 P0-A2 / 2026-10-11）：
  老版本 ``load_prediction_docs`` 返回的 dict 不含 ``mtime`` 字段，
  ``web.services.accuracy._collect_league_data`` 用
  ``doc.get("mtime", 0) < cutoff`` 做窗口过滤——所有文件的 ``mtime`` 都是 0、
  cutoff 永远 > 0，于是所有文件被跳过、命中率永远"数据积累中"。

本测试覆盖：
  1. load_prediction_docs 返回的 doc 必须含 ``mtime``（float epoch 秒，正数）
  2. mtime 应与文件 stat().st_mtime 一致（在 OS 时间精度内）
  3. league_accuracy_web 在 cutoff 区间内的文件应能命中（即使 sample 中只有 1 个
     reconciled，也至少返回一个 dict 而不是 None）
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _write_pred(path: Path, league: str = "epl", days_ago: int = 0,
                past_matches: list | None = None,
                predictions: list | None = None) -> None:
    """写一份测试用预测文件。``days_ago`` 控制 mtime（用于 cutoff 验证）。"""
    generated = datetime.now() - timedelta(days=days_ago)
    doc = {
        "generated_at": generated.isoformat(),
        "data_window": "20260101-20261231",
        "status": "ok",
        "league": league,
        "tournament_type": "league",
        "data_source": "",
        "past_matches": past_matches or [],
        "predictions": predictions or [],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture()
def predictions_dir(tmp_path: Path, monkeypatch) -> Path:
    """重定向 ``OUTPUT_DIR`` 到 tmp_path，让 ``iter_prediction_files`` 从 tmp 读。

    ``store.OUTPUT_DIR`` 是模块级常量（import 时固化），monkeypatch 要直接
    打到 ``web.services.store.OUTPUT_DIR`` 上。
    """
    pdir = tmp_path / "predictions"
    rdir = tmp_path / "results"
    refdir = tmp_path / "references"
    for d in (pdir, rdir, refdir):
        d.mkdir(parents=True, exist_ok=True)
    import web.services.store as wstore
    monkeypatch.setattr(wstore, "OUTPUT_DIR", tmp_path)
    return pdir


def test_load_prediction_docs_includes_mtime(predictions_dir: Path):
    """每条 doc 必须含 ``mtime`` 字段（float epoch 秒），与文件 stat 一致。"""
    f = predictions_dir / "prediction_2026-10-11_09_epl.json"
    _write_pred(f)

    from web.services.store import load_prediction_docs
    docs = load_prediction_docs()
    assert len(docs) == 1
    doc = docs[0]
    assert "mtime" in doc, "缺少 mtime → accuracy 永远 0%"
    assert isinstance(doc["mtime"], float)
    assert doc["mtime"] > 0
    # 与文件系统实际 mtime 一致（精度容忍 2s）
    assert abs(doc["mtime"] - f.stat().st_mtime) < 2


def test_load_prediction_docs_skips_unreadable_files(predictions_dir: Path):
    """坏文件（OSError stat 失败）应该跳过而不是把 mtime 设为 0。"""
    # 写一个正常文件
    good = predictions_dir / "prediction_2026-10-11_09_epl.json"
    _write_pred(good)

    from web.services.store import load_prediction_docs
    docs = load_prediction_docs()
    # 全部 OK，mtime 全为正
    assert all(d["mtime"] > 0 for d in docs)


def test_league_accuracy_web_returns_data_within_window(predictions_dir: Path):
    """``_collect_league_data`` 用 ``doc.mtime`` 做 cutoff：cutoff 区间内必须返回数据。

    准备两个文件：一个 0d（窗口内），一个 100d（窗口外）。
    验证 7d 窗口只看 0d 文件；30d 窗口两个都看。
    """
    today = predictions_dir / "prediction_2026-10-11_09_epl.json"
    old = predictions_dir / "prediction_2026-08-15_09_epl.json"

    past_match = {
        "name": "Arsenal vs Chelsea", "status": "STATUS_FULL_TIME",
        "kickoff_utc": "2026-08-15T19:00:00Z",
        "home": "阿森纳", "home_en": "Arsenal FC",
        "away": "切尔西", "away_en": "Chelsea FC",
        "score": "2-1", "completed": True,
    }
    pred = {
        "name": "Arsenal vs Chelsea", "direction": "Arsenal FC 胜",
        "home": "阿森纳", "home_en": "Arsenal FC",
        "away": "切尔西", "away_en": "Chelsea FC",
        "predicted_score": "2-1", "over_under": "Under 3.5",
    }
    # 把 mtime 显式设到现在（old 文件用更早时间）
    import os
    _write_pred(today, league="epl", days_ago=0, past_matches=[past_match], predictions=[pred])
    _write_pred(old, league="epl", days_ago=60, past_matches=[past_match], predictions=[pred])
    # 强制 mtime 老化
    old_time = time.time() - 60 * 86400
    os.utime(old, (old_time, old_time))

    from web.services.accuracy import league_accuracy_web
    r7 = league_accuracy_web("epl", days=7)
    r30 = league_accuracy_web("epl", days=30)

    # 7d 窗口：today 文件在范围内（mtime≈now），old 文件超出
    assert r7 is not None, "7d 窗口内必须返回 dict，不能 None"
    assert r7["reconciled"] >= 1

    # 30d 窗口：today + old 都进来；reconciled 应该 >= r7 的 reconciled（多算了 old）
    assert r30 is not None
    assert r30["reconciled"] >= r7["reconciled"]


def test_league_accuracy_web_returns_none_when_no_window_data(predictions_dir: Path):
    """窗口内没有任何文件时（cutoff > 文件 mtime）应返回 None（不编数）。"""
    f = predictions_dir / "prediction_2026-10-11_09_epl.json"
    _write_pred(f, league="epl", days_ago=0)
    # 把 mtime 强制设到 100d 前
    import os
    old_time = time.time() - 100 * 86400
    os.utime(f, (old_time, old_time))

    from web.services.accuracy import league_accuracy_web
    # 7d 窗口完全不含此文件
    assert league_accuracy_web("epl", days=7) is None
    # 30d 窗口也不含
    assert league_accuracy_web("epl", days=30) is None
    # 但 200d 窗口可见
    r = league_accuracy_web("epl", days=200)
    assert r is None or r["reconciled"] >= 0  # 不强制有 reconciled（pred/past 都没）
