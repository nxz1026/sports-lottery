"""R29 #1: web/services/jobs.py::_spin_state 原子写 + LockTimeout + _write_state 直测。

R26 #2 已测 _exclusive_lock / _pid_alive / _drop_stale_lock 三层；_spin_state /
_write_state 7 处 caller 仍 0 直测。任何 status 漂移 / extra 丢键 / lock 静默漏
都会让 run_job 状态机漂移。

本轮补 6 测，覆盖：
- _spin_state happy path：_read_job 返现有 → status + extra 合并
- _spin_state _default_job fallback：job 文件缺失 → 兜底
- _spin_state extra merge：多 kwargs（finished_at / exit_code / error）
- _spin_state LockTimeout：second acquire 超时 raise
- _spin_state returns fresh copy：return 值的 dict 是新对象
- _write_state happy path：无锁写入 + extra merge
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.errors import LockTimeout
from web.services import jobs as jobs_mod

# ──────────────────────── _spin_state ────────────────────────


def test_spin_state_merges_with_existing_job(tmp_path, monkeypatch):
    """R29 #1：job file 已存在 → status + extra 合并写入；return 包含 merged dict。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "test001"
    job_path = tmp_path / "jobs" / f"{jid}.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text(json.dumps({
        "id": jid, "status": "queued", "trigger": "manual",
        "args": ["--league", "epl"], "created_at": 1000.0,
    }), encoding="utf-8")

    out = jobs_mod._spin_state(jid, "running", started_at=2000.0, pid=12345)
    assert out["status"] == "running"
    assert out["started_at"] == 2000.0
    assert out["pid"] == 12345
    # 保留原始字段
    assert out["trigger"] == "manual"
    assert out["args"] == ["--league", "epl"]

    # 写回的 file 应含新字段
    reread = json.loads(job_path.read_text(encoding="utf-8"))
    assert reread["status"] == "running"
    assert reread["started_at"] == 2000.0
    assert reread["pid"] == 12345


def test_spin_state_falls_back_to_default_job_when_no_file(tmp_path, monkeypatch):
    """R29 #1：job file 不存在 → 走 _default_job 兜底；返回 dict 含默认字段。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "fresh01"

    out = jobs_mod._spin_state(jid, "queued", trigger="timer")
    # _default_job 字段
    assert out["id"] == jid
    assert out["status"] == "queued"
    assert out["trigger"] == "timer"  # extra 覆盖默认的 "manual"
    assert out["script"] == "predict"
    assert out["args"] == []
    assert out["created_at"] is not None
    # 文件应被创建
    assert (tmp_path / "jobs" / f"{jid}.json").exists()


def test_spin_state_extra_kwargs_merged(tmp_path, monkeypatch):
    """R29 #1：extra 多 kwargs（finished_at / exit_code / error）全部进入返回 dict。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "merge01"
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jobs" / f"{jid}.json").write_text(
        json.dumps({"id": jid, "status": "running"}), encoding="utf-8")

    out = jobs_mod._spin_state(jid, "finished",
                                finished_at=3000.5, exit_code=0, error=None)
    assert out["finished_at"] == 3000.5
    assert out["exit_code"] == 0
    assert out["error"] is None


def test_spin_state_lock_contention_raises_locktimeout(tmp_path, monkeypatch):
    """R29 #1：state.lock 被外部持有时 _spin_state 第二次调用 5s 超时 → LockTimeout。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "lock01"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    # 预创建 job file（_spin_state 需要 _read_job 找到）
    (jobs_dir / f"{jid}.json").write_text(
        json.dumps({"id": jid, "status": "queued"}), encoding="utf-8")

    lock_path = jobs_dir / f"{jid}.state.lock"

    with jobs_mod._exclusive_lock(lock_path, timeout=5):
        # lock 已持，二次 acquire 在 0.3s 内必 raise
        with pytest.raises(LockTimeout):
            jobs_mod._spin_state(jid, "running", started_at=1000.0)


def test_spin_state_returns_fresh_copy(tmp_path, monkeypatch):
    """R29 #1：return 值的 dict 是新对象（与 from disk 隔离，caller 改 return 不影响 file）。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "copy01"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{jid}.json").write_text(
        json.dumps({"id": jid, "status": "queued"}), encoding="utf-8")

    out = jobs_mod._spin_state(jid, "running")
    out["status"] = "MUTATED"  # caller 改 return

    # 重读 disk 应仍为 running
    reread = json.loads((jobs_dir / f"{jid}.json").read_text(encoding="utf-8"))
    assert reread["status"] == "running"


# ──────────────────────── _write_state ────────────────────────


def test_write_state_no_lock_acquire(tmp_path, monkeypatch):
    """R29 #1：_write_state 不取 lock（直接写），extra merge 与 _spin_state 行为对齐。"""
    monkeypatch.setattr(jobs_mod.config, "JOBS_DIR", tmp_path / "jobs")
    jid = "w01"
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{jid}.json").write_text(
        json.dumps({"id": jid, "status": "queued"}), encoding="utf-8")

    out = jobs_mod._write_state(jid, "running", started_at=5000.0)
    assert out["status"] == "running"
    assert out["started_at"] == 5000.0

    # 同样 fallback：file 缺失 → _default_job
    out2 = jobs_mod._write_state("w02", "finished", exit_code=1)
    assert out2["script"] == "predict"
    assert out2["status"] == "finished"
    assert out2["exit_code"] == 1
