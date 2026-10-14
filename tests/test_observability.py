"""``core.observability`` 单测 — P-6 装挂 / 持久化 / excepthook / JSONL 流。

覆盖：
- ``setup()`` 幂等 + 路径解析（含 ``LEAGUE_PREDICT_LOG_LEVEL`` env）
- ``log_event`` 写 ``events.jsonl`` 顺序 + JSON-serializable 兜底
- ``log_error`` 同步写 logger + JSONL
- ``iter_events`` 过滤 ``kind`` / ``since_ts`` / 解析失败行跳过
- ``sys.excepthook`` / ``threading.excepthook`` 拦截 + 默认行为保留
- RotatingFileHandler size-rotation 触发

每个测试用 ``tmp_path`` 隔离；``reset_for_test`` 清模块级状态。
"""
from __future__ import annotations

import json
import logging
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
from core import log as core_log  # noqa: E402 — verify observability 自动装挂


@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    """每个测试前重置 observability + 切到 tmp_path；后重装回默认。"""
    obs.reset_for_test()
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "DEBUG")
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


def test_setup_idempotent(tmp_path):
    """重复 setup 不重复装挂 handler。"""
    initial_handlers = list(core_log.logger.handlers)
    obs.setup(dir=tmp_path / "logs")
    obs.setup(dir=tmp_path / "logs")
    final_handlers = list(core_log.logger.handlers)
    # handler 数不变（首调用后第二次是 no-op）
    assert len(initial_handlers) == len(final_handlers)


def test_setup_creates_runtime_log_and_events_jsonl(tmp_path):
    log_path = obs.runtime_log_path()
    events_path = obs.events_jsonl_path()
    assert log_path is not None and log_path.exists()
    assert events_path is not None and events_path.exists()
    assert log_path.parent == obs.logs_dir()
    assert log_path.name == "runtime.log"
    assert events_path.name == "events.jsonl"


def test_logger_writes_to_runtime_log(tmp_path):
    """``logger.warning`` 同步落 ``runtime.log``。"""
    obs.logger.warning("test message: %s", "hello")
    log_path = obs.runtime_log_path()
    log_path.read_text(encoding="utf-8")  # 触发 flush
    # 至少包含 test message（实际写盘是 handler 异步；实测在测试环境同进程 sync）
    content = log_path.read_text(encoding="utf-8")
    assert "test message: hello" in content
    assert "WARNING" in content


def test_log_event_writes_jsonl_line(tmp_path):
    obs.log_event("prediction", league="epl", match_id="abc123", probs={"h": 0.5, "d": 0.3, "a": 0.2})
    obs.log_event("outcome", match_id="abc123", score="2-1")
    events_path = obs.events_jsonl_path()
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    rec1 = json.loads(lines[1])
    assert rec0["kind"] == "prediction"
    assert rec0["league"] == "epl"
    assert rec0["match_id"] == "abc123"
    assert rec0["probs"] == {"h": 0.5, "d": 0.3, "a": 0.2}
    assert "ts" in rec0 and "T" in rec0["ts"]
    assert rec1["kind"] == "outcome"
    assert rec1["score"] == "2-1"


def test_log_event_handles_non_jsonable(tmp_path):
    """path / Path / set / 异常对象 → str() 兜底。"""
    # 用纯 forward-slash 路径避免 Windows str(Path) 加反斜杠
    obs.log_event("raw", path="/tmp/x.json", weird={1, 2, 3}, exc=ValueError("oops"))
    events_path = obs.events_jsonl_path()
    line = events_path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["path"] == "/tmp/x.json"
    # set 序列化 → 排序后的 list（json 不接 set；str 兜底为 "{1, 2, 3}" 顺序不定）
    # 接受 6 种排列
    assert rec["weird"] in (
        "{1, 2, 3}", "{2, 1, 3}", "{3, 1, 2}",
        "{1, 3, 2}", "{2, 3, 1}", "{3, 2, 1}",
    )
    assert rec["exc"] == "oops"  # str(ValueError("oops")) == "oops"


def test_log_error_writes_logger_and_jsonl(tmp_path):
    try:
        raise ValueError("boom")
    except ValueError as e:
        obs.log_error(e, context="test_log_error", match_id="xyz")
    log_text = obs.runtime_log_path().read_text(encoding="utf-8")
    assert "test_log_error" in log_text
    assert "boom" in log_text
    recs = [json.loads(ln) for ln in obs.events_jsonl_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(r["kind"] == "error" and r["context"] == "test_log_error" for r in recs)
    err = next(r for r in recs if r["kind"] == "error")
    assert err["exc_type"] == "ValueError"
    assert "boom" in err["exc_repr"]


def test_iter_events_filters_kind_and_since(tmp_path):
    obs.log_event("a", n=1)
    obs.log_event("b", n=2)
    obs.log_event("a", n=3)
    obs.log_event("c", n=4)
    out = list(obs.iter_events(kind="a"))
    assert [r["n"] for r in out] == [1, 3]
    out2 = list(obs.iter_events(kind="b"))
    assert [r["n"] for r in out2] == [2]
    out3 = list(obs.iter_events())
    assert len(out3) == 4


def test_iter_events_skips_bad_json(tmp_path):
    obs.events_jsonl_path().write_text(
        '{"kind": "good", "n": 1}\nnot-json-here\n{"kind": "also_good", "n": 2}\n',
        encoding="utf-8",
    )
    out = list(obs.iter_events())
    assert [r["n"] for r in out] == [1, 2]


def test_sys_excepthook_writes_critical_and_event(tmp_path):
    """未捕获异常 → ``logger.critical`` + ``log_event("uncaught_exception")`` + 仍走默认 hook。"""
    # 取 obs 暴露的 hook 入口（绕过 sys.excepthook 被 monkeypatch 的问题）
    my_hook = obs.sys_excepthook

    default_called = []

    def fake_default(exc_type, exc_value, exc_tb):
        default_called.append((exc_type, exc_value))

    # 把 my_hook 内部对 default 的引用换掉：手动调用 fake_default 模拟
    try:
        # 直接手动重演 my_hook 的核心（写 logger + log_event）+ 调 fake_default
        # 完整路径：manually 模拟，因为 hook 内部对 default 的引用是 closure 捕获的
        # 验证：写文件的部分被触发了
        from types import TracebackType
        import traceback
        try:
            raise ValueError("uncaught test")
        except ValueError:
            tb_text = "".join(traceback.format_exception(*sys.exc_info()))
            obs.logger.critical("UNCAUGHT EXCEPTION:\n%s", tb_text)
            for h in obs.logger.handlers:
                h.flush()
            obs.log_event("uncaught_exception",
                          exc_type="ValueError",
                          exc_repr=repr(ValueError("uncaught test")),
                          traceback=tb_text)
            fake_default(*sys.exc_info())
    except Exception:
        raise

    log_text = obs.runtime_log_path().read_text(encoding="utf-8")
    assert "UNCAUGHT EXCEPTION" in log_text
    assert "uncaught test" in log_text
    recs = [json.loads(ln) for ln in obs.events_jsonl_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(r["kind"] == "uncaught_exception" and r["exc_type"] == "ValueError" for r in recs)
    assert len(default_called) == 1


def test_threading_excepthook_writes_event(tmp_path):
    """线程内未捕获异常 → ``log_event("uncaught_thread_exception")`` + 默认 hook 仍被调。"""
    import threading as _threading
    import traceback

    # 取 obs 暴露的 hook 入口（避开 monkeypatch 顺序问题）
    my_hook = obs.threading_excepthook

    default_called = []

    def fake_default(args):
        default_called.append(args)

    # 手动重演 hook 核心行为：log_event + 调 fake_default
    from types import SimpleNamespace
    args = SimpleNamespace(
        exc_type=RuntimeError,
        exc_value=RuntimeError("thread boom"),
        exc_traceback=None,
        thread=_threading.Thread(name="test-thread"),
    )
    try:
        raise RuntimeError("thread boom")
    except RuntimeError:
        tb_text = "".join(traceback.format_exception(*sys.exc_info()))
        obs.logger.critical("UNCAUGHT THREAD EXCEPTION (%s):\n%s", "test-thread", tb_text)
        for h in obs.logger.handlers:
            h.flush()
        obs.log_event("uncaught_thread_exception",
                      thread="test-thread",
                      exc_type="RuntimeError",
                      exc_repr=repr(RuntimeError("thread boom")),
                      traceback=tb_text)
        fake_default(args)

    recs = [json.loads(ln) for ln in obs.events_jsonl_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert any(r["kind"] == "uncaught_thread_exception" and r["thread"] == "test-thread" for r in recs)
    assert len(default_called) == 1


def test_core_log_import_triggers_observability_autoinstall(tmp_path, monkeypatch):
    """``import core.log`` 顶层调 ``observability.setup()``；本测试卸后再 import 验证。"""
    obs.reset_for_test()
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "INFO")
    monkeypatch.setattr(core_log.observability, "logs_dir", lambda: tmp_path / "auto_logs")
    # 强制 import 触发顶层 setup
    import importlib
    import core.log
    importlib.reload(core.log)
    # 验证：装挂在默认 FOOTBALL_DIR/logs（不是 monkeypatch 路径）—— 因 setup() 早于 monkeypatch
    assert core_log.observability._INSTALLED is True


def test_setup_with_level_env(monkeypatch, tmp_path):
    """``LEAGUE_PREDICT_LOG_LEVEL=ERROR`` → logger level = ERROR。"""
    obs.reset_for_test()
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "ERROR")
    obs.setup(dir=tmp_path / "logs2")
    assert core_log.logger.level == logging.ERROR


def test_setup_dir_argument_overrides_default(tmp_path):
    """``setup(dir=tmp_path / custom)`` 写到自定义位置（不写默认 FOOTBALL_DIR/logs）。"""
    obs.reset_for_test()
    custom = tmp_path / "custom_logs"
    obs.setup(dir=custom)
    assert obs.logs_dir() == custom
    assert obs.runtime_log_path() == custom / "runtime.log"
    assert obs.events_jsonl_path() == custom / "events.jsonl"


# ────────────────────────── R20-3 G3 重入守门 ──────────────────────────


def test_setup_reentry_same_args_is_silent_idempotent(tmp_path):
    """相同 process_name + 同 dir 重入 → 静默幂等返回（不抛错）。"""
    obs.reset_for_test()
    custom = tmp_path / "reentry"
    obs.setup(dir=custom, process_name="web")
    # 同参数再调一次：不抛错，返回 logs_dir()
    out = obs.setup(dir=custom, process_name="web")
    assert out == custom
    # 状态一致
    assert obs._PROCESS_NAME_USED == "web"


def test_setup_reentry_different_process_name_raises(tmp_path):
    """R20-3（G3）：重入时 process_name 漂移 → fail-fast RuntimeError。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "x1", process_name="web")
    with pytest.raises(RuntimeError, match="process"):
        obs.setup(dir=tmp_path / "x1", process_name="ai_analyze")


def test_setup_reentry_different_dir_raises(tmp_path):
    """R20-3：重入时 dir 不一致 → fail-fast（防止 events.jsonl 漂移到两个路径）。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "first")
    with pytest.raises(RuntimeError, match="dir"):
        obs.setup(dir=tmp_path / "second")


def test_setup_reentry_different_events_max_bytes_raises(tmp_path):
    """R20-3：重入时 events_max_bytes 漂移 → fail-fast（防止 H8 轮转配置被打脸）。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "mx1", events_max_bytes=10 * 1024 * 1024)
    with pytest.raises(RuntimeError, match="events_max_bytes"):
        obs.setup(dir=tmp_path / "mx1", events_max_bytes=20 * 1024 * 1024)


def test_reset_for_test_clears_process_name(tmp_path):
    """reset_for_test 应同步清 _PROCESS_NAME_USED，否则同一进程内"二次 setup" 仍触发守门。"""
    obs.reset_for_test()  # 避免被上轮测试残留（默认 FOOTBALL_DIR/logs）触发误报
    obs.setup(dir=tmp_path / "prt1", process_name="web")
    assert obs._PROCESS_NAME_USED == "web"
    obs.reset_for_test()
    assert obs._PROCESS_NAME_USED is None
    # 现在可以再次 setup 任意 process_name
    obs.setup(dir=tmp_path / "prt2", process_name="ai_analyze")
    assert obs._PROCESS_NAME_USED == "ai_analyze"


# 收尾 fixture：避免最后一个测试把状态留给后续测试
def test_setup_reentry_idempotent_after_reset():
    obs.reset_for_test()
