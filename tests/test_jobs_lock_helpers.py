"""R26 #2: web/services/jobs.py 锁三重 (_exclusive_lock / _pid_alive / _drop_stale_lock) 直测。

explore 实测：5 个 helper 0 直测；test_m3_jobs.py 把 _exclusive_lock 用作实现
细节，test_apifootball_quota_concurrency.py 在 _guard_quota 内部使用。锁是
Windows race 的核心保险。

本轮补 9 测，覆盖：
- _exclusive_lock 3：happy path / LockTimeout 超时 / finally unlink 三次重试
- _pid_alive 2：自身 PID 短路 / POSIX 死 PID 兜底
- _drop_stale_lock 4：非数字 PID / 同进程短 mtime 保留 / 同进程长 mtime 强删 /
  跨进程 _pid_alive False + mtime > 120s 删 / 跨进程 _pid_alive True 保留
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.services import jobs as jobs_mod
from web.errors import LockTimeout


# ──────────────────────── _exclusive_lock ────────────────────────


def test_exclusive_lock_happy_path_creates_and_unlinks(tmp_path):
    """R26 #2：acquire 成功 → 锁文件含 pid；with 退出 → 锁文件消失。"""
    lock_path = tmp_path / "test.lock"
    with jobs_mod._exclusive_lock(lock_path, timeout=1.0):
        # 锁文件存在且含 self pid
        assert lock_path.exists()
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
    # 退出后锁文件消失
    assert not lock_path.exists()


def test_exclusive_lock_second_acquire_raises_lock_timeout(tmp_path):
    """R26 #2：第一把未释放就 acquire 第二把 → 超时后 raise LockTimeout。"""
    lock_path = tmp_path / "test.lock"

    @contextmanager
    def hold_lock():
        with jobs_mod._exclusive_lock(lock_path, timeout=10):
            yield

    with hold_lock():
        # 锁仍被 first context 持有；第二把应在超时内 raise
        with pytest.raises(LockTimeout):
            with jobs_mod._exclusive_lock(lock_path, timeout=0.2):
                pass
        # 锁仍存在（first 未释放）
        assert lock_path.exists()
    # first 退出 → 锁消失
    assert not lock_path.exists()


def test_exclusive_lock_unlink_retries_on_oserror(tmp_path, monkeypatch, caplog):
    """R26 #2：finally 段 os.unlink 前两次 raise OSError，第三次成功 → lock 消失。"""
    lock_path = tmp_path / "retry.lock"
    # 先正常 acquire，再用 monkeypatch 替换 os.unlink 验证重试
    real_unlink = os.unlink
    calls = {"n": 0}

    def fake_unlink(p):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(32, "another process is using this file")
        return real_unlink(p)

    monkeypatch.setattr(jobs_mod.os, "unlink", fake_unlink)

    with caplog.at_level("WARNING"):
        with jobs_mod._exclusive_lock(lock_path, timeout=1):
            pass
    # 第三次 unlink 才成功；前两次 raise 但 finally 仍继续重试
    assert calls["n"] == 3
    assert not lock_path.exists()


# ──────────────────────── _pid_alive ────────────────────────


def test_pid_alive_self_returns_true():
    """R26 #2：自身 PID 短路返回 True（避免 Windows os.kill 自身 PID 挂死）。"""
    assert jobs_mod._pid_alive(os.getpid()) is True


def test_pid_alive_posix_dead_pid_returns_false(monkeypatch):
    """R26 #2：POSIX os.kill 抛 ProcessLookupError → 死 PID 返 False。"""
    if os.name == "nt":
        pytest.skip("POSIX only test on non-Windows")
    # 强制造一个不存在的 pid
    fake_pid = 99999999

    def fake_kill(pid, sig):
        raise ProcessLookupError(f"no such pid: {pid}")

    monkeypatch.setattr(jobs_mod.os, "kill", fake_kill)
    assert jobs_mod._pid_alive(fake_pid) is False


# ──────────────────────── _drop_stale_lock ────────────────────────


def test_drop_stale_lock_non_numeric_pid_silently_returns(tmp_path, caplog):
    """R26 #2：锁文件内容非数字 → 静默 return，不抛穿。"""
    lock_path = tmp_path / "abc.lock"
    lock_path.write_text("not_a_pid", encoding="utf-8")
    with caplog.at_level("WARNING"):
        jobs_mod._drop_stale_lock(lock_path)
    # 锁仍存在（未删）
    assert lock_path.exists()


def test_drop_stale_lock_same_process_recent_mtime_keeps_lock(tmp_path):
    """R26 #2：同进程 pid == os.getpid() + mtime < 5s → 不删。"""
    lock_path = tmp_path / "self.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    # mtime now (刚写) → age ≈ 0s < 5s → 不删
    jobs_mod._drop_stale_lock(lock_path)
    assert lock_path.exists()


def test_drop_stale_lock_same_process_old_mtime_force_unlinks(tmp_path):
    """R26 #2：同进程 + mtime > 5s（线程崩死残留）→ 强删锁。"""
    lock_path = tmp_path / "self_old.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    # mtime backdate 到 30s 前
    target_mtime = time.time() - 30
    os.utime(lock_path, (target_mtime, target_mtime))

    jobs_mod._drop_stale_lock(lock_path)
    # 同进程路径走 unlink（不管 Windows OSError silently pass）
    assert not lock_path.exists()


def test_drop_stale_lock_cross_process_dead_pid_unlinks(tmp_path, monkeypatch, caplog):
    """R26 #2：跨进程 _pid_alive False + mtime > 120s → 删 + log warning。"""
    lock_path = tmp_path / "dead.lock"
    # 写一个非自身 PID（绕过 self 分支）
    fake_dead_pid = max(1, os.getpid() - 1)
    if fake_dead_pid == os.getpid():
        fake_dead_pid = 12345
    lock_path.write_text(str(fake_dead_pid), encoding="utf-8")
    # mtime backdate 到 200s 前
    target_mtime = time.time() - 200
    os.utime(lock_path, (target_mtime, target_mtime))

    # monkeypatch _pid_alive 假装 dead
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: False)

    with caplog.at_level("WARNING"):
        jobs_mod._drop_stale_lock(lock_path)
    assert not lock_path.exists()
    assert any("删除 stale 锁" in r.message for r in caplog.records)


def test_drop_stale_lock_cross_process_alive_pid_keeps_lock(tmp_path, monkeypatch):
    """R26 #2：跨进程 _pid_alive True（活进程持有）→ 不删。"""
    lock_path = tmp_path / "alive.lock"
    fake_alive_pid = 12345
    lock_path.write_text(str(fake_alive_pid), encoding="utf-8")
    # mtime backdate 也覆盖 120s 阈值；但 _pid_alive True 应使其保留
    target_mtime = time.time() - 300
    os.utime(lock_path, (target_mtime, target_mtime))

    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: True)

    jobs_mod._drop_stale_lock(lock_path)
    # 活进程持有 → 不删
    assert lock_path.exists()
