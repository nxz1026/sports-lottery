"""采集端「原子性」回归测试（2026-10-23 生产实测修）。

背景：2026-10-23 查远端装载日志时发现 6 条 `JSONDecodeError`，追下去是
**采集机本地写文件时的内容交织损坏**（不是传输问题：本地 sha == 远端 sha == 清单 sha）。

根因链（两处都是 TOCTOU）：

1. ``_acquire_singleton_lock``：``if lock.exists()`` → ``lock.write_text()`` 是两步 ⇒
   **同一秒启动的 N 个批次会同时判定"没有锁"**、各自写自己的 pid ⇒ N 个批次同时采集。
   真实触发：Boot+Logon 触发器把多个计划任务在同一刻拉起（实测 21:49 一次拉起 4 个）。
2. ``write_batch``：``while (…).exists(): batch += 1`` → ``open(path, "w")`` 也是两步 ⇒
   多个进程给同一 topic 选到**同一个文件名**、各自 ``open("w")``（截断）写同一路径
   ⇒ **文件内容互相交织、变成非法 JSON**，且坏文件留在盘上，每次重装重复报错。

⇒ 修法：两处都改用 **O_EXCL 独占创建**（``open(path, "x")``）：抢不到就换下一个。

本文件锁住三件事：
  ① ``write_batch`` 顺序调用序号递增、不覆盖历史批；
  ② ``write_batch`` **并发**调用不撞名，且每个文件都是**完整合法 JSON**（不交织）；
  ③ 单例锁**并发**时最多一个进程能拿到（用真进程测：线程共享 pid 会绕过存活判定）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = REPO_ROOT / "collector-cn" / "collector.py"

# ⚠️ 本文件**不做平台跳过**（2026-10-23 订正）：初版把两条锁测试标成
# 「仅 Windows 可测」，根因是存活判定只认 `ctypes.windll`。现在采集端存活判定
# 走 `_pid_alive()`（Windows 走 OpenProcess / POSIX 走 os.kill(pid, 0)）
# ⇒ Windows 与 Linux（Oracle）**两侧都能跑**，平台差异由生产代码自己吸收。

def _load_mod():
    spec = importlib.util.spec_from_file_location("collector_atomic_test", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mod_at(tmp_path, monkeypatch):
    mod = _load_mod()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


# ── ① write_batch：序号递增、不覆盖历史批 ──────────────────────────────

def test_write_batch_sequential_uses_increasing_index(tmp_path, monkeypatch):
    """同分钟连续两次 ⇒ `__001` / `__002`，不覆盖。"""
    mod = _mod_at(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "fname_ts", lambda: "2026-10-23T07-00Z")

    p1 = mod.write_batch("jczq_offer", [{"a": 1}])
    p2 = mod.write_batch("jczq_offer", [{"a": 2}])

    assert p1.name == "2026-10-23T07-00Z__001.jsonl"
    assert p2.name == "2026-10-23T07-00Z__002.jsonl"
    assert p1.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert p2.read_text(encoding="utf-8") == '{"a": 2}\n'


def test_write_batch_empty_uses_same_index_space(tmp_path, monkeypatch):
    """空批产 `.empty`，且不与同序号的 `.jsonl` 共用序号。"""
    mod = _mod_at(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "fname_ts", lambda: "2026-10-23T07-00Z")

    p1 = mod.write_batch("jczq_offer", [{"a": 1}])
    p2 = mod.write_batch("jczq_offer", [])

    assert p1.name.endswith("__001.jsonl")
    assert p2.name.endswith("__002.empty")
    assert p2.exists()


# ── ② write_batch：并发不撞名、内容不交织 ──────────────────────────────

def test_write_batch_concurrent_no_collision(tmp_path, monkeypatch):
    """8 线程同时写同一 topic ⇒ 8 个不同文件，且每个都是**完整合法 JSON**。

    旧实现（exists() → open("w")）在这里会：多个线程选到同一个序号 ⇒
    互相截断写入 ⇒ 文件内容交织 ⇒ 解析失败。**这就是生产上那 6 个坏文件的成因。**
    """
    import threading

    mod = _mod_at(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "fname_ts", lambda: "2026-10-23T07-00Z")

    n = 8
    rows = [{"kind": "line", "topic": "jczq_offer", "payload": {"i": i}} for i in range(n)]
    paths: list[Path] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def _worker():
        try:
            barrier.wait()          # 最大化并发窗口
            paths.append(mod.write_batch("jczq_offer", list(rows)))
        except BaseException as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"写文件抛异常: {errors}"
    assert len(paths) == n
    names = [p.name for p in paths]
    assert len(set(names)) == n, f"文件名撞了: {sorted(names)}"

    # 每个文件都必须能被逐行完整解析（不交织）
    for p in paths:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == n, f"{p.name} 行数 {len(lines)} != {n}（内容被交织/截断）"
        for ln in lines:
            json.loads(ln)


# ── ③ 单例锁：并发最多一个拿到 ────────────────────────────────────────

_CHILD = r"""
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("c", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["c"] = mod
spec.loader.exec_module(mod)
mod.ROOT = __import__("pathlib").Path(sys.argv[2])
log = sys.argv[3]          # **每进程一个日志文件**（共享 append 会有写丢失）
got = mod._acquire_singleton_lock()
with open(log, "a", encoding="utf-8") as f:
    f.write("ACQ %d %s\n" % (time.time(), got))
    f.flush()
    os.fsync(f.fileno())
if got:
    time.sleep(1.0)
    with open(log, "a", encoding="utf-8") as f:
        f.write("REL %d\n" % time.time())
        f.flush()
        os.fsync(f.fileno())
    mod._release_singleton_lock()
"""


def test_singleton_lock_concurrent_only_one_holder(tmp_path):
    """4 个**真进程**同时抢锁 ⇒ 持锁区间必须**互不重叠**（恰好一个赢家）。

    为什么用进程不用线程：锁的存活判定是「旧 pid != 我的 pid」⇒ 同进程多线程
    会共享 pid 而绕过判定，测不出真并发。
    """
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")

    procs = []
    for i in range(4):
        log = tmp_path / ("lock_%d.log" % i)
        procs.append((log, subprocess.Popen(
            [sys.executable, str(child), str(COLLECTOR), str(tmp_path), str(log)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
    for _log, p in procs:
        p.wait(timeout=60)

    events = []
    for log, _p in procs:
        for line in log.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            events.append((parts[0], float(parts[1]), parts[2] if len(parts) > 2 else ""))

    acqs = [e for e in events if e[0] == "ACQ"]
    assert len(acqs) == 4, f"子进程没都上报: {events}"
    win = [e for e in acqs if e[2] == "True"]
    assert len(win) == 1, f"应当恰好 1 个进程拿到锁，实际 {len(win)}: {acqs}"

    # 持锁区间互不重叠
    spans = []
    cur_start = None
    for tag, ts, _flag in sorted(events, key=lambda e: e[1]):
        if tag == "ACQ" and _flag == "True":
            cur_start = ts
        elif tag == "REL" and cur_start is not None:
            spans.append((cur_start, ts))
            cur_start = None
    assert len(spans) == 1, f"持锁区间应只有 1 段，实际 {len(spans)}: {spans}"


def test_singleton_lock_refuses_when_other_process_alive(tmp_path, monkeypatch):
    """锁被**另一个活进程**持有时必须返回 False（跨平台）。

    ⚠️ 2026-10-23 踩过：这条原本带 `skipif(not WINDOWS)`，因为当时存活判定
    只认 `ctypes.windll`（Windows 专有）⇒ Linux 上子进程直接 AttributeError 崩掉。
    现在存活判定走 `_pid_alive()`（Windows 走 OpenProcess / POSIX 走 os.kill(0)）
    ⇒ **全平台可测**，不再需要跳过。
    """
    mod = _mod_at(tmp_path, monkeypatch)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / ".collector.lock").write_text(str(holder.pid), encoding="utf-8")
        assert mod._acquire_singleton_lock() is False
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_singleton_lock_creates_lock_with_own_pid(tmp_path, monkeypatch):
    """首次拿锁 ⇒ 创建锁文件并写入自己的 pid。"""
    mod = _mod_at(tmp_path, monkeypatch)
    assert mod._acquire_singleton_lock() is True
    lock = tmp_path / ".collector.lock"
    assert lock.exists()
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    mod._release_singleton_lock()
    assert not lock.exists()


def test_singleton_lock_takes_over_dead_pid(tmp_path, monkeypatch):
    """旧 pid 已死（且进程对象已销毁）⇒ 接管（返回 True）。

    ⚠️ 关键细节：只 ``wait()`` 是不够的 —— Popen 还握着进程句柄 ⇒ 进程对象仍在
    ⇒ ``OpenProcess`` **依然成功** ⇒ 会被误判成「还活着」而拒绝接管。
    必须把句柄关掉，pid 才真正失效（这正是「wscript 被杀、python 僵尸已回收」的场景）。
    """
    mod = _mod_at(tmp_path, monkeypatch)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    pid = dead.pid
    try:
        dead._handle.Close()      # 释放最后一个句柄 ⇒ 进程对象销毁 ⇒ pid 失效
    except Exception:
        pass
    del dead

    (tmp_path / ".collector.lock").write_text(str(pid), encoding="utf-8")
    assert mod._acquire_singleton_lock() is True
    assert (tmp_path / ".collector.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
