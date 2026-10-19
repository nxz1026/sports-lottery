"""P1-5 验收测试：_ACC_CACHE 负缓存不被写 / 失败 TTL 短。

`_live_accuracy` 在 store 异常时不应被 10 分钟长 TTL 缓存 None，
否则 store 恢复后命中率看板最坏滞后 10 分钟（P1-5）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_fail_result_within_30s_recompute_too_short_to_skip():
    """失败一次后立即重调（< 30s 内）：仍命中负缓存，避免每次请求都重扫。

    每个 _live_accuracy 内部调用 league_accuracy_web 2 次（7d + 30d 窗口）。
    """
    from web.routers import predictions

    predictions._ACC_CACHE.clear()
    inner_calls = {"n": 0}

    def fake_acc(league, days=7):
        inner_calls["n"] += 1
        return None  # 模拟无样本

    with patch.object(predictions, "_ACC_CACHE", {}):
        with patch("web.services.accuracy.league_accuracy_web", side_effect=fake_acc):
            # 第一次调用：失败（内部 2 次 inner）
            assert predictions._live_accuracy("epl") is None
            assert inner_calls["n"] == 2
            # 立即再次（< 30s）：负缓存 30s TTL 内命中（避免每次请求重扫）
            assert predictions._live_accuracy("epl") is None
            assert inner_calls["n"] == 2, (
                f"30s TTL 内的失败应被命中，避免每次请求重扫 store；got {inner_calls['n']}"
            )


def test_fail_result_after_30s_recomputes():
    """失败一次后 31s 再调：30s TTL 已过，应重试（修复点：旧版 600s 锁死）。

    验证 P1-5 修复：旧代码失败结果被 600s 长 TTL 锁住，store 恢复后最坏
    滞后 10 分钟；新版 FAIL TTL = 30s，store 恢复后下次请求就追上。
    """
    from web.routers import predictions

    predictions._ACC_CACHE.clear()
    inner_calls = {"n": 0}
    base = time.time()

    def fake_acc(league, days=7):
        inner_calls["n"] += 1
        return None

    with patch.object(predictions, "_ACC_CACHE", {}):
        with patch("web.services.accuracy.league_accuracy_web", side_effect=fake_acc):
            with patch("time.time", side_effect=lambda: base):
                assert predictions._live_accuracy("epl") is None
                assert inner_calls["n"] == 2
            # 31s 后：负缓存 30s TTL 已过，应重试
            with patch("time.time", side_effect=lambda: base + 31):
                assert predictions._live_accuracy("epl") is None
                assert inner_calls["n"] == 4, (
                    f"P1-5 修复点：失败 31s 后必须重试；got {inner_calls['n']}"
                )


def test_ok_result_is_long_cached():
    """成功一次后，10 分钟内不重试（仅初次 2 次 inner 调用）。"""
    from web.routers import predictions

    predictions._ACC_CACHE.clear()
    inner_calls = {"n": 0}

    def fake_acc(league, days=7):
        inner_calls["n"] += 1
        return {"hit": 5, "total": 10}

    with patch.object(predictions, "_ACC_CACHE", {}):
        with patch("web.services.accuracy.league_accuracy_web", side_effect=fake_acc):
            r1 = predictions._live_accuracy("epl")
            r2 = predictions._live_accuracy("epl")
            assert r1 == r2 == {"7d": {"hit": 5, "total": 10}, "30d": {"hit": 5, "total": 10}}
            assert inner_calls["n"] == 2, (
                f"成功结果应被长缓存，600s 内不重试（初次 2 次内调用），got {inner_calls['n']}"
            )


def test_fail_to_ok_recovery_within_30s():
    """失败 → 模拟时间推进 31s → store 恢复 → 应返回真实结果（30s TTL 已过）。"""
    from web.routers import predictions

    predictions._ACC_CACHE.clear()
    base = time.time()

    def make_time(offset):
        return lambda: base + offset

    with patch.object(predictions, "_ACC_CACHE", {}):
        with patch("web.services.accuracy.league_accuracy_web", return_value=None):
            with patch("time.time", side_effect=make_time(0)):
                assert predictions._live_accuracy("epl") is None
        # 31s 后：负缓存 30s TTL 已过；store 恢复
        with patch("web.services.accuracy.league_accuracy_web",
                   return_value={"hit": 8, "total": 10}):
            with patch("time.time", side_effect=make_time(31)):
                r = predictions._live_accuracy("epl")
                assert r == {"7d": {"hit": 8, "total": 10}, "30d": {"hit": 8, "total": 10}}, (
                        f"30s 后 store 恢复应立即返回真实命中率，got {r}"
                )


def test_ok_result_kept_within_long_ttl():
    """成功结果 31s 后仍命中缓存（_ACC_TTL_OK = 600）。"""
    from web.routers import predictions

    predictions._ACC_CACHE.clear()
    base = time.time()

    with patch.object(predictions, "_ACC_CACHE", {}):
        with patch("web.services.accuracy.league_accuracy_web",
                   return_value={"hit": 5, "total": 10}):
            with patch("time.time", side_effect=lambda: base):
                r1 = predictions._live_accuracy("epl")
            # 31s 后再读
            with patch("time.time", side_effect=lambda: base + 31):
                r2 = predictions._live_accuracy("epl")
                assert r1 == r2, "成功结果在 600s TTL 内应被缓存（31s < 600s）"


def test_fail_ttl_shorter_than_ok_ttl():
    """_ACC_TTL_FAIL 必须严格小于 _ACC_TTL_OK（否则与成功路径无差异）。"""
    from web.routers import predictions

    assert predictions._ACC_TTL_FAIL < predictions._ACC_TTL_OK, (
        f"负缓存 TTL 应短于成功 TTL（{predictions._ACC_TTL_FAIL} < {predictions._ACC_TTL_OK}）"
    )


def test_exception_is_logged_and_degrades_to_none(caplog):
    """异常被 log 后降级为 None（绝不编数）。"""
    import logging

    from web.routers import predictions

    predictions._ACC_CACHE.clear()

    def boom(league, days=7):
        raise RuntimeError("store 损坏")

    with patch.object(predictions, "_ACC_CACHE", {}):
        with caplog.at_level(logging.WARNING, logger="web.predictions"):
            with patch("web.services.accuracy.league_accuracy_web", side_effect=boom):
                assert predictions._live_accuracy("epl") is None
        assert any("store 损坏" in r.getMessage() for r in caplog.records), (
            f"异常未入日志：{[r.getMessage() for r in caplog.records]}"
        )
