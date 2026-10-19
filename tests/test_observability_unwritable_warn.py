"""Round 11 D.1 — observability 不可写时 startup 哨兵。

覆盖：
- ``log_event`` 在 events_jsonl_path()=None 时升级到 logger.warning（一次性）
- ``_ensure_default_events`` 在 lazy setup 失败时升级到 logger.warning
- 哨兵只触发一次；reset_for_test 后再触发一次
- runtime_log_path()=None 的同款哨兵（如有需要，可扩展）
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402


def _force_events_unwritable(monkeypatch):
    """强制 obs.events_jsonl_path() 永返 None（绕过 auto-install 副作用）。"""
    obs.reset_for_test()
    # 让 setup 强制失败 → _EVENTS_JSONL 永远 None
    monkeypatch.setattr(obs, "setup",
                        lambda **_kw: (_ for _ in ()).throw(
                            OSError("forced unreadable mount simulation")))
    # 同时清 auto-install 装挂（避免 core.log 顶层 auto 落真实路径）
    monkeypatch.setattr(obs, "_INSTALLED", True)
    monkeypatch.setattr(obs, "_LOGS_DIR", None)
    monkeypatch.setattr(obs, "_EVENTS_JSONL", None)
    monkeypatch.setattr(obs, "_RUNTIME_LOG", None)
    monkeypatch.setattr(obs, "_UNWRITABLE_EVENTS_WARNED", False)


class TestUnwritableSentinel:
    """D.1: events.jsonl 不可写时给运维一次性哨兵。"""

    def test_log_event_with_none_path_warns_once(self, caplog, monkeypatch):
        _force_events_unwritable(monkeypatch)
        assert obs.events_jsonl_path() is None
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            obs.log_event("test_evt", foo="bar")
            obs.log_event("test_evt", foo="baz")
            obs.log_event("test_evt", foo="qux")
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and "events.jsonl unavailable" in r.message]
        # 哨兵只触发一次（即使连发 3 次）—— 这是核心契约
        assert len(warnings) == 1, f"哨兵应只触发一次，实际 {len(warnings)} 条"
        # 警告内容：路径不可写 OR lazy setup 失败（_force_events_unwritable 让 lazy
        # 走失败路径，所以前者消息里含 "lazy setup failed"）。
        assert ("events_jsonl_path() is None" in warnings[0].message
                or "lazy setup failed" in warnings[0].message)

    def test_sentinel_resets_across_reset_for_test(self, caplog, monkeypatch):
        """``reset_for_test`` 应清旗子，让下一个测试可以再触发哨兵。"""
        _force_events_unwritable(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            obs.log_event("evt1", k=1)
            assert sum(1 for r in caplog.records
                       if "events.jsonl unavailable" in r.message) == 1
        # reset 后再试，旗子应被清
        obs.reset_for_test()
        monkeypatch.setattr(obs, "_INSTALLED", True)
        monkeypatch.setattr(obs, "_LOGS_DIR", None)
        monkeypatch.setattr(obs, "_EVENTS_JSONL", None)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            obs.log_event("evt2", k=2)
            assert sum(1 for r in caplog.records
                       if "events.jsonl unavailable" in r.message) == 1

    def test_silent_when_setup_ok(self, caplog, tmp_path):
        """正常 setup → ``log_event`` 不打哨兵。"""
        obs.setup(dir=tmp_path / "logs")
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            obs.log_event("ok_evt", n=1)
            obs.log_event("ok_evt", n=2)
        warns = [r for r in caplog.records
                 if "events.jsonl unavailable" in r.message]
        assert warns == [], f"正常 setup 不应打哨兵，实际 {len(warns)} 条"

    def test_lazy_setup_failure_warns_once(self, caplog, monkeypatch):
        """``_ensure_default_events`` lazy setup 失败 → logger.warning。"""
        # reset 后让 setup 强制失败
        obs.reset_for_test()

        def _explode_setup(**_):
            raise OSError("forced: readonly mount")

        monkeypatch.setattr(obs, "setup", _explode_setup)
        with caplog.at_level(logging.WARNING, logger=obs.logger.name):
            result = obs._ensure_default_events()
        assert result is None  # 静默返 None
        warns = [r for r in caplog.records
                 if "events.jsonl unavailable" in r.message]
        assert len(warns) >= 1
        assert "lazy setup failed" in warns[0].message
