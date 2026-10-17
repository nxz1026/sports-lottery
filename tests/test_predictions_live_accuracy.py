"""R50-4: web/routers/predictions.py::_live_accuracy 4 cases direct + D-95 lock.

_live_accuracy (L232-L260, 29 行) 存量 accuracy_summary 缺失时, 实时对账:
1) cache 命中 (10min OK / 30s fail) -> 返 cache
2) league_accuracy_web 调 7d + 30d -> windows 字典
3) windows 空 -> None
4) 异常降级 -> None (logger.warning + 短缓存)

R44-5 D-60 + R46-1 D-68 已锁 accuracy._collect_league_data /
league_accuracy_web, 但 _live_accuracy 自身核心契约 (cache 命中 +
7d+30d 窗口 + 空返 None + 异常降级) 0 直测。

D-95 锁定 4 契约:
- D-95-1: 主流程 happy (7d+30d 都有结果) -> {"7d": ..., "30d": ...}
- D-95-2: 7d+30d 都空 -> None (L255 `windows or None`)
- D-95-3: cache 命中 (10min OK TTL 内) -> 不再调 league_accuracy_web
- D-95-4: league_accuracy_web 抛错 -> None (L256-258 except 降级)

依赖 stub:
- web.services.accuracy.league_accuracy_web (monkeypatch 返受控 dict 或抛错)
- _ACC_CACHE 清空防 fixture 污染
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
def preds_mod(monkeypatch):
    """Spec-load predictions.py + stub league_accuracy_web + 清 _ACC_CACHE。"""
    spec = importlib.util.spec_from_file_location(
        "predictions_live_accuracy_test",
        REPO_ROOT / "web" / "routers" / "predictions.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 清 cache
    mod._ACC_CACHE.clear()
    # 默认 stub: 返 None
    monkeypatch.setattr("web.services.accuracy.league_accuracy_web",
                        lambda league, days=7: None)
    return mod


# ──────────────────────── D-95 四契约 ────────────────────────


def test_live_accuracy_d95_1_happy_returns_7d_30d_windows(preds_mod, monkeypatch):
    """R50-4 D-95-1: 主流程 happy - 7d+30d 都有结果 -> {"7d": ..., "30d": ...}。

    L251-254 调 7d + 30d, 各自返 acc -> windows dict。
    """
    responses = {7: {"7d_acc": True}, 30: {"30d_acc": True}}

    def fake_acc(league, days=7):
        return responses[days]

    monkeypatch.setattr("web.services.accuracy.league_accuracy_web", fake_acc)
    out = preds_mod._live_accuracy("epl")
    assert out == {"7d": {"7d_acc": True}, "30d": {"30d_acc": True}}


def test_live_accuracy_d95_2_both_windows_empty_returns_none(preds_mod, monkeypatch):
    """R50-4 D-95-2: 7d+30d 都 None -> windows={} -> None (L255 `or None`)。"""
    # 默认 stub 返 None, 用 laliga 避免被其它 test cache 干扰
    out = preds_mod._live_accuracy("laliga")
    assert out is None
    # 任意一窗非空 -> windows 含那一窗 (用 bundesliga fresh cache)
    def fake_acc(league, days=7):
        return {"only_7d": True} if days == 7 else None
    monkeypatch.setattr("web.services.accuracy.league_accuracy_web", fake_acc)
    out_partial = preds_mod._live_accuracy("bundesliga")
    assert out_partial == {"7d": {"only_7d": True}}


def test_live_accuracy_d95_3_cache_hit_within_10min(preds_mod, monkeypatch):
    """R50-4 D-95-3: cache 命中 (10min OK TTL 内) -> 不再调 league_accuracy_web。

    L242-246 cache 命中 + within _ACC_TTL_OK -> 直接返 cached[1]。
    spy 调用次数验证只调 1 次 (第一次), 第二次返缓存。
    """
    call_count = {"i": 0}

    def fake_acc(league, days=7):
        call_count["i"] += 1
        return {"call": call_count["i"]}

    monkeypatch.setattr("web.services.accuracy.league_accuracy_web", fake_acc)

    # 第一次调: cache miss, 调 league_accuracy_web (7d + 30d 共 2 次)
    out1 = preds_mod._live_accuracy("seriea")  # fresh league
    assert out1 == {"7d": {"call": 1}, "30d": {"call": 2}}
    assert call_count["i"] == 2
    # 第二次调: cache hit (10min TTL 未到), 不再调 league_accuracy_web
    out2 = preds_mod._live_accuracy("seriea")
    # 返缓存的 windows (第一次结果)
    assert out2 == {"7d": {"call": 1}, "30d": {"call": 2}}
    assert call_count["i"] == 2  # 仍 2 次调用 (没新调)


def test_live_accuracy_d95_4_accuracy_raises_returns_none(preds_mod, monkeypatch):
    """R50-4 D-95-4: league_accuracy_web 抛错 -> None (L256-258 except 降级)。

    LOCK: 异常时 result=None + logger.warning + 仍 cache (短 30s)。
    """
    def boom(league, days=7):
        raise RuntimeError("db down")

    monkeypatch.setattr("web.services.accuracy.league_accuracy_web", boom)
    out = preds_mod._live_accuracy("epl")
    # 异常降级 -> None (不 5xx)
    assert out is None
    # cache 设了 (短 30s)
    assert "epl" in preds_mod._ACC_CACHE
    cached_ts, cached_result = preds_mod._ACC_CACHE["epl"]
    assert cached_result is None
    # timestamp 接近 now
    assert abs(cached_ts - time.time()) < 1.0