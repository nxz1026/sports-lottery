"""``core.observability`` events.jsonl size-based rotation — Round 10 H8。

覆盖：
- 默认无 ``events_max_bytes`` → 不轮转（向后兼容 Round 9 之前行为）
- 设了 ``events_max_bytes`` 后，写入超阈值 → 触发轮转
- 备份文件命名 ``events.jsonl.{1..N}.bak``，超出 backup_count 被丢
- 跨 process_name 后缀互不影响（rotation 只动当前 _EVENTS_JSONL）
- 写多次跨多轮边界 → 备份计数 ≤ backup_count
- 轮转后当前 events.jsonl 是新文件（空），后续写入不丢
"""
from __future__ import annotations

import json
import sys
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


def _read_jsonl_lines(path: Path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ─────────────────────── 默认无轮转 ──────────────────────────


class TestNoRotationByDefault:
    """向后兼容：未传 events_max_bytes → 不轮转，写无上限。"""

    def test_default_no_rotation_when_size_grows(self, tmp_path):
        obs.setup(dir=tmp_path / "logs")
        # 写 100 行 + 一行 huge payload（模拟 1MB），不轮转
        for i in range(100):
            obs.log_event("test_event", i=i, payload="x" * 100)
        events_path = obs.events_jsonl_path()
        # 当前文件含全部 100 行
        lines = _read_jsonl_lines(events_path)
        assert len(lines) == 100
        # 不应存在任何 .bak 文件
        bak_files = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert bak_files == [], f"无轮转配置下不应有 .bak 文件: {bak_files}"


# ─────────────────────── 启轮转 ──────────────────────────


class TestRotationTriggers:
    """events_max_bytes 给值后，写入超阈值触发 rotate。"""

    def test_rotates_when_size_exceeds_threshold(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", events_max_bytes=200, events_backup_count=3)
        events_path = obs.events_jsonl_path()

        # 写 ~150 字节的一行 → 不到 200，不轮转
        obs.log_event("small", payload="x" * 100)
        lines = _read_jsonl_lines(events_path)
        assert len(lines) == 1
        bak = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert len(bak) == 0

        # 再写一行 ~150 字节 → 总 ~300 超 200，触发轮转
        obs.log_event("large", payload="x" * 100)
        # 写后当前 events.jsonl 应有 1 行（刚 rotate 完再 append 一条）
        lines_now = _read_jsonl_lines(events_path)
        assert len(lines_now) == 1
        assert lines_now[0]["kind"] == "large"
        # 旧的 .1.bak 应该存在且含第一条
        bak1 = events_path.parent / f"{events_path.name}.1.bak"
        assert bak1.exists()
        bak1_lines = _read_jsonl_lines(bak1)
        assert len(bak1_lines) == 1
        assert bak1_lines[0]["kind"] == "small"

    def test_multiple_rotations_promote_bak_files(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", events_max_bytes=200, events_backup_count=3)
        events_path = obs.events_jsonl_path()

        # 写 5 轮 → 每次都触发轮转
        for round_idx in range(5):
            obs.log_event("first", round=round_idx, payload="x" * 100)
            obs.log_event("second", round=round_idx, payload="x" * 100)

        # 当前 events.jsonl 应有最近一轮的 second
        current = _read_jsonl_lines(events_path)
        assert len(current) == 1
        assert current[0]["kind"] == "second"
        assert current[0]["round"] == 4

        # 应有 .1.bak .. .3.bak（不超过 backup_count=3）
        for i in range(1, 4):
            assert (events_path.parent / f"{events_path.name}.{i}.bak").exists(), (
                f".{i}.bak 应存在"
            )
        # .4.bak 不应存在（被丢）
        assert not (events_path.parent / f"{events_path.name}.4.bak").exists()

    def test_rotation_count_limited_to_backup_count(self, tmp_path):
        """写 10 轮 → backup 数严格 ≤ backup_count（默认 5）。"""
        obs.setup(dir=tmp_path / "logs", events_max_bytes=200)  # 默认 backup_count=5
        events_path = obs.events_jsonl_path()

        for round_idx in range(10):
            obs.log_event("a", round=round_idx, payload="x" * 100)
            obs.log_event("b", round=round_idx, payload="x" * 100)

        bak_files = sorted(events_path.parent.glob(f"{events_path.name}.*.bak"))
        # backup_count 默认 5 → 至多 5 个 .bak
        assert len(bak_files) <= 5, f"备份数 ≤ backup_count=5，实际 {len(bak_files)}"

    def test_zero_size_file_does_not_create_bak(self, tmp_path):
        """空 events.jsonl 触发的轮转不应创建 .bak（避免每次启动空 rotate）。"""
        obs.setup(dir=tmp_path / "logs", events_max_bytes=100, events_backup_count=3)
        events_path = obs.events_jsonl_path()
        # 不写，只是空文件存在
        bak = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert bak == []
        # 写一行 → 不超阈值
        obs.log_event("init", n=1)
        bak = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert bak == []


# ─────────────────────── 进程隔离 ──────────────────────────


class TestRotationScopedToProcessFile:
    """H8 轮转只动当前 _EVENTS_JSONL；不同 process_name 后缀互不影响。

    校验关键前提：H9 已经按 process_name 分文件，H8 轮转不会跨进程误伤
    老文件（每个进程有自己的 events.{name}.jsonl）。
    """

    def test_predict_rotation_does_not_touch_web(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", process_name="predict",
                  events_max_bytes=200, events_backup_count=3)
        predict_path = obs.events_jsonl_path()

        for _ in range(5):
            obs.log_event("predict_evt", payload="x" * 100)

        # predict 文件轮转过；web 文件不应存在
        assert predict_path.exists()
        web_path = tmp_path / "logs" / "events.web.jsonl"
        assert not web_path.exists(), "H8 轮转不应创建未启用的 process_name 文件"


# ─────────────────────── log_event 边界 ──────────────────────────


class TestRotationSafety:
    """OSError / 不存在 / 0 阈值 → 静默降级。"""

    def test_disabled_when_max_bytes_zero(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", events_max_bytes=0)
        events_path = obs.events_jsonl_path()

        # 写很多行不轮转
        for _ in range(50):
            obs.log_event("event", payload="x" * 500)

        bak = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert bak == [], "max_bytes=0 应禁用轮转"

    def test_threshold_smaller_than_first_write(self, tmp_path):
        """第一次写入超阈值、且 current 已是空文件 → 不创建 .bak（边界）。

        设计：``_maybe_rotate_events`` 在 ``current == 0`` 时不搬空文件
        （避免每次启动空 rotate）；写后当前 events.jsonl 仍是一行新事件。
        """
        obs.setup(dir=tmp_path / "logs", events_max_bytes=50, events_backup_count=3)
        events_path = obs.events_jsonl_path()

        # 一行 ~150 字节 >> 50
        obs.log_event("huge", payload="x" * 150)
        current_lines = _read_jsonl_lines(events_path)
        assert len(current_lines) == 1
        assert current_lines[0]["kind"] == "huge"
        # 不应创建 .bak（current 为空时不强搬空文件）
        bak = list(events_path.parent.glob(f"{events_path.name}.*.bak"))
        assert bak == [], f"空文件 + 单行写入 → 不创建 .bak，实际 {bak}"
        # 不抛 OSError
