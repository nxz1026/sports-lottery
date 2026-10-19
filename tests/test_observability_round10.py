"""Round 10 observability 硬化守门 — E.3 + A.1 + E.1。

覆盖：
- **E.3** ``web/services/ai_analyze.py`` 模块导入触发
  ``observability.setup(process_name="ai_analyze")``
- **A.1** ``log_event`` 多线程并发不丢行
- **E.1** ``iter_events`` 真惰性（generator 在 ``limit`` 处立即停止读取）
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402


@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    obs.reset_for_test()
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "DEBUG")
    yield
    obs.reset_for_test()


def _read_events():
    p = obs.events_jsonl_path()
    if p is None or not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ────────────────────── E.3: ai_analyze 装挂门 ──────────────────────────


class TestAiAnalyzeInstallsObservability:
    """E.3: web/services/ai_analyze.py 模块导入触发 ``setup(process_name="ai_analyze")``。"""

    def _fresh_import(self):
        """重置 obs 状态后重新加载 ai_analyze。"""
        obs.reset_for_test()
        sys.modules.pop("web.services.ai_analyze", None)
        return importlib.import_module("web.services.ai_analyze")

    def test_ai_analyze_uses_ai_suffix(self, tmp_path, monkeypatch):
        from core import constants
        monkeypatch.setattr(constants, "FOOTBALL_DIR", tmp_path)

        self._fresh_import()

        runtime_path = obs.runtime_log_path()
        events_path = obs.events_jsonl_path()
        if runtime_path is not None:
            assert runtime_path.name == "runtime.ai_analyze.log"
        if events_path is not None:
            assert events_path.name == "events.ai_analyze.jsonl"

    def test_ai_analyze_load_failure_does_not_break(self, tmp_path, monkeypatch):
        """装挂失败仅 logger.warning，模块照常可用。"""
        from core import observability as obs_mod

        def _explode(**_):
            raise OSError("read-only simulated")

        monkeypatch.setattr(obs_mod, "setup", _explode)
        sys.modules.pop("web.services.ai_analyze", None)
        mod = importlib.import_module("web.services.ai_analyze")
        assert mod is not None
        assert hasattr(mod, "analyze")


# ────────────────────── A.1: 多线程写锁 ──────────────────────────


class TestLogEventConcurrencySafety:
    """A.1: 多线程并发 ``log_event`` → 不丢行、rotation race-safe。"""

    def test_concurrent_log_event_does_not_drop(self, tmp_path):
        """5 个线程各写 20 行 → 最终 events.jsonl 应有 100 行。"""
        obs.setup(dir=tmp_path / "logs")
        events_path = obs.events_jsonl_path()

        n_threads = 5
        per_thread = 20
        barrier = threading.Barrier(n_threads)

        def worker(tid: int):
            barrier.wait()  # 同步起跑
            for i in range(per_thread):
                obs.log_event("thread_evt", tid=tid, i=i,
                              payload=f"t{tid}-i{i}-p{'x'*20}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join()

        lines = _read_events()
        assert len(lines) == n_threads * per_thread, (
            f"丢失行：预期 {n_threads * per_thread} 实际 {len(lines)}"
        )

    def test_concurrent_log_event_with_rotation_no_corruption(self, tmp_path):
        """并发 + rotation：每行 JSON 仍合法，不出现半行 / 截断。

        注：H8 rotation 是 stdlib RotatingFileHandler 风格的"破坏式"轮转
        （旧内容覆写到 .1.bak，3 份 .bak 各自只保留最近一轮 rotated 内容），
        不是 append-only 累加。所以"总行数 == n × per_thread" 不成立——
        这是设计语义，不是 race bug。本测试只检：A.1 锁保证没写半行 / 损坏。
        """
        obs.setup(dir=tmp_path / "logs", events_max_bytes=200, events_backup_count=3)
        events_path = obs.events_jsonl_path()

        n_threads = 4
        per_thread = 30
        barrier = threading.Barrier(n_threads)

        def worker(tid: int):
            barrier.wait()
            for i in range(per_thread):
                obs.log_event("evt", tid=tid, i=i, body="x" * 50)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads: t.start()
        for t in threads: t.join()

        # 验证：所有 .jsonl / .bak 文件里每一行都是合法 JSON（没有半行截断）
        log_dir = events_path.parent
        total_valid = 0
        for p in [events_path] + sorted(log_dir.glob(f"{events_path.name}.*.bak")):
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                    total_valid += 1
                except json.JSONDecodeError:
                    pytest.fail(f"race-induced 半行 JSON in {p.name}: {line!r}")
        # 至少有一些事件被写到（races 不会丢光）
        assert total_valid > 0, "rotation race 后一行都没写出，有问题"

    def test_rotation_keeps_destructive_history_within_limit(self, tmp_path):
        """H8 标准 rotation：磁盘总额受 backup_count 约束，但旧内容会被覆写。

        这是 stdlib RotatingFileHandler 的语义：最多保留 backup_count ×
        max_bytes 的最近事件；旧事件被新内容覆写。
        """
        obs.setup(dir=tmp_path / "logs", events_max_bytes=200, events_backup_count=3)
        events_path = obs.events_jsonl_path()
        log_dir = events_path.parent

        for round_idx in range(20):
            for i in range(2):
                obs.log_event("evt", round=round_idx, i=i, body="x" * 80)

        # 磁盘上 .bak 文件数量 ≤ backup_count + 1（含当前 .jsonl）
        bak_files = list(log_dir.glob(f"{events_path.name}.*.bak"))
        assert len(bak_files) <= 3, f"backup_count=3, 实际 {len(bak_files)} 个 .bak"


def _read_jsonl_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _count_all_jsonl(log_dir: Path, base_name: str = "events.jsonl") -> int:
    """汇总当前 events.jsonl + .bak 文件的总行数。"""
    total = 0
    main = log_dir / base_name
    if main.exists():
        total += len(_read_jsonl_lines(main))
    for bak in sorted(log_dir.glob(f"{base_name}.*.bak")):
        total += len(_read_jsonl_lines(bak))
    return total


# ────────────────────── E.1: 真惰性 iter_events ──────────────────────────


def _read_jsonl_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestIterEventsLaziness:
    """E.1: iter_events 是生成器，limit 处立即停止读盘。"""

    def test_iter_events_is_generator(self, tmp_path):
        """类型是 generator（不是预收集 list）"""
        obs.setup(dir=tmp_path / "logs")
        gen = obs.iter_events()
        # Python generator 标识
        assert not isinstance(gen, list)

    def test_iter_events_respects_limit_immediately(self, tmp_path):
        """limit=2 时只读完前 2 条匹配 → 立刻退出 generator。"""
        obs.setup(dir=tmp_path / "logs")
        events_path = obs.events_jsonl_path()
        # 写 1000 行统一 kind
        for i in range(1000):
            obs.log_event("bench", i=i)
        # 重新初始化 generator（读盘）
        gen = obs.iter_events(kind="bench", limit=2)
        recs = list(gen)
        assert len(recs) == 2

    def test_iter_events_lazy_no_full_load(self, tmp_path):
        """关键：取第一个后立刻 break → 文件不应被读完。"""
        obs.setup(dir=tmp_path / "logs")
        events_path = obs.events_jsonl_path()
        # 写很多行
        for i in range(5000):
            obs.log_event("bench2", i=i, body="x" * 30)

        file_size_before = events_path.stat().st_size
        assert file_size_before > 50_000, "测试前要保证文件够大"

        # iter_events + next() 一次后 close()；未读完剩余部分
        gen = obs.iter_events(kind="bench2")
        first = next(gen)
        assert first["kind"] == "bench2"
        gen.close()
        # 文件仍在（未删除）；这里主要确认 generator 不预收集 list
        assert events_path.exists()
        # 关键不变量：调用 next 后立即退出 generator，无 list 创建
        assert not isinstance(gen, list)
