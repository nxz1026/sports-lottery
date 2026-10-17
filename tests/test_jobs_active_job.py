"""R41-6: web/services/jobs.py::active_job 6 cases direct + D-43 orphan-recovery lock.

活跃任务查找 + 孤儿回收 helper 0 direct tests; 路由层 GET /api/v1/jobs
内部消费, 但 active_job 自身契约 (terminal skip / running 超时回收 /
queued 300s 回收 / 跨进程序列返回首个) 未锁。helper bug 会让 stale
running/queued 任务永久占位, 全站新触发 409。

helper 含六条契约 (D-43):
1. terminal status ({done, failed, timeout}) 跳过 -> 不参与决策
2. running + now - base > timeout*2 -> _spin_state 标 failed + 跳过
3. queued + now - created_at > 300s -> 标 failed + 跳过
4. 正常 queued (未到 300s) 或 running (未到 timeout*2) -> 返该 dict
5. list_jobs 全 terminal 或空 [] -> None
6. 跨进程序列 (多个混合) -> 返回首个非 terminal 非孤儿 (按 list_jobs mtime desc)

5 tests (HIGH) + 1 跨进程序列:
- 全 terminal -> None
- 正常 queued (created_at=now-10) -> return
- running orphan (base=now-10000) -> _spin_state(failed, "orphan recovered") + skip
- queued orphan (created_at=now-1000) -> _spin_state(failed, "orphan queued expired") + skip
- 空 list -> None
- 混合序列 [terminal, running, queued] -> 返首个非孤儿的 running
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jobs_mod(tmp_path, monkeypatch):
    """加载 web/services/jobs.py + JOBS_DIR 重定向到 tmp + 收集 _spin_state calls。"""
    from web import config as web_config
    monkeypatch.setattr(web_config, "JOBS_DIR", tmp_path / "jobs")
    spec = importlib.util.spec_from_file_location(
        "jobs_active_job_test",
        REPO_ROOT / "web" / "services" / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def spin_calls(monkeypatch, jobs_mod):
    """Mock _spin_state 收集调用, 不实际写盘。"""
    calls = []

    def fake_spin(jid, status, finished_at=None, error=None):
        calls.append({"jid": jid, "status": status, "error": error})

    monkeypatch.setattr(jobs_mod, "_spin_state", fake_spin)
    return calls


# ──────────────────────── D-43 六契约 ────────────────────────


def test_active_job_d43_empty_list_returns_none(jobs_mod, monkeypatch):
    """R41-6 D-43: list_jobs 返 [] -> 返 None (无需 spin_state)。

    锁定 base case: 全空 → None, 不抛错。
    """
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: [])
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: 1_000_000.0)
    out = jobs_mod.active_job()
    assert out is None


def test_active_job_d43_all_terminal_returns_none(jobs_mod, monkeypatch):
    """R41-6 D-43: 全 done/failed/timeout -> 跳过所有 -> 返 None。

    锁定 TERMINAL skip 行为: 终态不参与 active 决策。
    """
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: [
        {"id": "j1", "status": "done", "created_at": 0.0},
        {"id": "j2", "status": "failed", "created_at": 0.0},
        {"id": "j3", "status": "timeout", "created_at": 0.0},
    ])
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: 1_000_000.0)
    out = jobs_mod.active_job()
    assert out is None


def test_active_job_d43_normal_queued_returns(jobs_mod, monkeypatch, spin_calls):
    """R41-6 D-43: 正常 queued (created_at=now-10s) -> 返该 row, 不标 failed。"""
    now = 1_000_000.0
    q_row = {"id": "jq", "status": "queued", "created_at": now - 10.0}
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: [q_row])
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: now)

    out = jobs_mod.active_job()
    assert out is q_row
    assert spin_calls == []  # 未触发标 failed


def test_active_job_d43_running_orphan_marks_failed_and_skips(
    jobs_mod, monkeypatch, spin_calls,
):
    """R41-6 D-43: running + base 远早于 timeout*2 -> _spin_state(failed, "orphan recovered") + 跳过。

    锁定回收语义: 孤儿必须自愈为 failed, 不能让 stale running 占位。
    """
    now = 1_000_000.0
    timeout = jobs_mod.config.PREDICT_TIMEOUT_SECONDS  # 默认 2700s 等
    # base = started_at = now - timeout*2 - 1 (已超时)
    r_row = {
        "id": "jr", "status": "running",
        "started_at": now - (timeout * 2 + 1),
    }
    # list_jobs 还返一个 normal queued, 验证 runner orphan 后被跳过, return 第二个
    q_row = {"id": "jq", "status": "queued", "created_at": now - 5.0}
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: [r_row, q_row])
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: now)

    out = jobs_mod.active_job()
    assert out is q_row  # 跳过 orphan running, 返后续 queued
    # orphan 被标 failed
    assert len(spin_calls) == 1
    assert spin_calls[0] == {"jid": "jr", "status": "failed",
                              "error": "orphan recovered"}


def test_active_job_d43_queued_orphan_marks_failed(
    jobs_mod, monkeypatch, spin_calls,
):
    """R41-6 D-43: queued + created_at 早于 300s -> _spin_state(failed, "orphan queued expired") + 跳过。"""
    now = 1_000_000.0
    q_row = {"id": "jq", "status": "queued", "created_at": now - 301.0}
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: [q_row])
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: now)

    out = jobs_mod.active_job()
    assert out is None  # 孤儿被跳过, 无其它 -> None
    assert len(spin_calls) == 1
    assert spin_calls[0]["status"] == "failed"
    assert spin_calls[0]["error"] == "orphan queued expired"


def test_active_job_d43_cross_row_returns_first_non_orphan(
    jobs_mod, monkeypatch, spin_calls,
):
    """R41-6 D-43: 跨进程序列 [terminal, running-orphan, queued-normal] ->
    跳过终态和孤儿, 返首个非孤儿 (running orphan + queued normal 都在,
    按 src 返回 queued normal 因为 orphan 先被标记+跳) 。

    锁定 list_jobs mtime desc 顺序下 helper 仍正确选首个可用 row。
    """
    now = 1_000_000.0
    timeout = jobs_mod.config.PREDICT_TIMEOUT_SECONDS
    rows = [
        {"id": "t", "status": "done", "created_at": now - 60},       # terminal skip
        {"id": "ro", "status": "running",
         "started_at": now - (timeout * 2 + 5)},                       # running orphan
        {"id": "qn", "status": "queued", "created_at": now - 5},      # normal queued
    ]
    monkeypatch.setattr(jobs_mod, "list_jobs", lambda limit=20: rows)
    monkeypatch.setattr(jobs_mod, "_now_epoch", lambda: now)

    out = jobs_mod.active_job()
    assert out is rows[2]  # qn
    # running orphan 被标 failed (queued normal 不动)
    assert len(spin_calls) == 1
    assert spin_calls[0]["jid"] == "ro"
