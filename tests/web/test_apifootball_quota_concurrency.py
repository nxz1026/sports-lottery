"""P2-8 验收测试：API-Football _guard_quota 跨进程配额原子性。

修复点：旧 _guard_quota 用 threading.Lock + 读改写 quota.json，
多 uvicorn worker 无法跨进程同步。两个 worker 同时读 daily=99，
各自 +1 后 tmp.replace → 后写者覆盖前写者，每日最多超限 (workers × 1) 次。

修复：复用 web.services.jobs._exclusive_lock（O_CREAT|O_EXCL 文件锁）。
本测试用多线程模拟多 worker 并发（同一进程下文件锁同样能拦截
竞争，确保即使真正的多 worker 部署也安全）。
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_concurrent_guard_quota_does_not_exceed_daily_limit(monkeypatch, tmp_path):
    """并发 N 次 _guard_quota，quota.json.daily 必须 == N（无丢失）。"""
    from web.services import apifootball

    # 用足够高的 daily limit 让所有线程都能通过；验证点是 daily 计数
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_DAILY_LIMIT", 10000)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_MINUTE_LIMIT", 10000)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_KEY", "test-key")

    # 清掉 stale lock
    quota_path = tmp_path / "quota.json"
    lock_path = quota_path.with_suffix(".lock")
    if lock_path.exists():
        lock_path.unlink()

    service = apifootball.ApiFootballService(
        transport=lambda *a, **kw: b"{}",
        cache_dir=tmp_path,
    )

    n = 50  # 50 线程并发 guard_quota

    def call(_idx):
        # _guard_quota 内部已持有 _exclusive_lock（与 production 一致）。
        # 不要在测试里再 wrap 一次同一个锁路径 → FileExistsError 死锁。
        service._guard_quota()

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(call, i) for i in range(n)]
        for f in futures:
            f.result(timeout=20)

    # daily 必须 == n（无丢失，无超限）
    state = json.loads(quota_path.read_text(encoding="utf-8"))
    assert int(state["daily"]) == n, (
        f"P2-8: 并发 {n} 次后 quota.daily={state['daily']}，应为 {n}（超/丢失 = 锁失效）"
    )
    assert len(state["requests"]) == n


def test_guard_quota_daily_limit_strictly_respected_under_concurrency(monkeypatch, tmp_path):
    """daily limit = 5，并发 20 次 → 5 次成功 + 15 次抛 quota exhausted。"""
    from web.services import apifootball

    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_DAILY_LIMIT", 5)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_MINUTE_LIMIT", 10000)
    monkeypatch.setattr(apifootball.config, "API_FOOTBALL_KEY", "test-key")

    # 清掉前序测试遗留的 quota.json / lock（pytest tmp_path 是 function-scoped，
    # 但同一模块内连跑时若锁残留会跨用例污染）。
    quota_path = tmp_path / "quota.json"
    if quota_path.exists():
        quota_path.unlink()
    lock_path = quota_path.with_suffix(".lock")
    if lock_path.exists():
        lock_path.unlink()

    service = apifootball.ApiFootballService(
        transport=lambda *a, **kw: b"{}",
        cache_dir=tmp_path,
    )

    successes = []
    quota_errors = []

    def call(idx):
        try:
            service._guard_quota()
            successes.append(idx)
        except apifootball.ApiFootballQuotaError:
            quota_errors.append(idx)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(call, i) for i in range(20)]
        for f in futures:
            f.result(timeout=15)

    assert len(successes) == 5, (
        f"limit=5 时 20 并发应 5 成功 15 失败；got {len(successes)} successes / {len(quota_errors)} errors"
    )
    assert len(quota_errors) == 15
    state = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert int(state["daily"]) == 5
