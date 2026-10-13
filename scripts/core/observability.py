"""``core.observability`` — 进程级日志 / 异常 / 结构化事件通道。

本模块在 ``core.log`` 的 stdlib ``logger`` 之上加 4 项能力，给 60 个 caller
**零迁移成本**的同时把"程序运行错误"与"结构化数据"落盘：

1. **持久化日志**（``runtime.log``，每日 1 份 + size-based 备份）
   —— 进程崩了之后仍可回看当时 stderr 写了什么；包括 ERROR/CRITICAL
2. **uncaught exception hook**（``sys.excepthook``）
   —— 任何未 try/except 的异常（KeyError/RuntimeError/...）都先写日志
   再走默认 Python 行为（traceback + exit code 1）
3. **结构化事件流**（``events.jsonl``，append-only）
   —— 供下游 ETL / ML 反馈用；每行一个 JSON object
4. **``log_error(exc, context)``** / ``log_event(kind, **fields)`` helper

设计要点：

- 不替换 stdlib logger：60 个文件 ``from core.log import logger`` 仍工作；
  这里只是给同一个 logger 追加 handler / 给进程加 hook
- 路径走 ``FOOTBALL_DIR / "logs"``（与 predictions/results 平级），纳入
  ``cleanup_old_files`` 范围
- 测试可注入临时目录：``setup(dir=tmp_path)`` 让 logger 把日志写到
  pytest 临时目录，不污染生产数据

P-6 (2026-10-13) 引入。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.log import logger

# ── 路径常量（模块级；test 可直接读）───────────────────────
_LOGS_DIR: Path | None = None
_RUNTIME_LOG: Path | None = None
_EVENTS_JSONL: Path | None = None
# H8: events.jsonl size 轮转配置；None 关闭，setup() 可设值开启。
_EVENTS_MAX_BYTES: int | None = None
_EVENTS_BACKUP_COUNT: int = 5
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def logs_dir() -> Path:
    """当前安装到的 logs 目录（默认 ``FOOTBALL_DIR / "logs"``）。"""
    if _LOGS_DIR is None:
        from core.constants import FOOTBALL_DIR
        return FOOTBALL_DIR / "logs"
    return _LOGS_DIR


def runtime_log_path() -> Path | None:
    """当前 ``runtime.log`` 路径；未安装时为 None。"""
    return _RUNTIME_LOG


def events_jsonl_path() -> Path | None:
    """当前 ``events.jsonl`` 路径；未安装时为 None。"""
    return _EVENTS_JSONL


# ── 进程级安装（幂等）─────────────────────────────────────
def setup(*, dir: Path | None = None, level: str | int | None = None,
          process_name: str | None = None,
          events_max_bytes: int | None = None,
          events_backup_count: int | None = None) -> Path:
    """安装文件 handler + JSONL handler + excepthook。返回 logs 目录。

    重复调用幂等：第一次安装后跳过。``dir`` 显式指定覆盖默认 ``FOOTBALL_DIR/logs``；
    ``level`` 默认读 ``LEAGUE_PREDICT_LOG_LEVEL`` env，缺省 ``INFO``。
    ``process_name`` 给文件加后缀（``events.{name}.jsonl`` / ``runtime.{name}.log``）
    让多进程（web uvicorn vs predict subprocess）不竞同一文件 —— 见 H9。
    """
    global _LOGS_DIR, _RUNTIME_LOG, _EVENTS_JSONL, _EVENTS_MAX_BYTES, _EVENTS_BACKUP_COUNT, _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return logs_dir()
        if dir is None:
            from core.constants import FOOTBALL_DIR
            dir = FOOTBALL_DIR / "logs"
        dir.mkdir(parents=True, exist_ok=True)
        _LOGS_DIR = dir
        # 文件名后缀：process_name=None → "runtime.log" / "events.jsonl"（保持旧契约）；
        # 设置后 → "runtime.{name}.log" / "events.{name}.jsonl"（多进程隔离）
        suffix = f".{process_name}" if process_name else ""
        _RUNTIME_LOG = dir / f"runtime{suffix}.log"
        _EVENTS_JSONL = dir / f"events{suffix}.jsonl"
        # events.jsonl size-based 轮转（H8）。None = 无轮转（默认/向后兼容）；
        # events_max_bytes 给值后按"写前 size + 行 > max"触发 rotate。
        # 单进程内 open+rename 是原子，跨进程已在 H9 用 process_name 隔离。
        _EVENTS_MAX_BYTES = events_max_bytes
        _EVENTS_BACKUP_COUNT = (
            events_backup_count if events_backup_count is not None else 5
        )
        # 确保两个文件存在（empty file）—— 监控 / 备份 / 读 events.jsonl 的下游
        # 工具不必先写一行才有路径
        _RUNTIME_LOG.touch(exist_ok=True)
        _EVENTS_JSONL.touch(exist_ok=True)

        # 解析 level
        if level is None:
            level = os.environ.get("LEAGUE_PREDICT_LOG_LEVEL", "INFO")
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)

        # ── file handler (RotatingFileHandler，10MB × 5 份) ──
        fh = logging.handlers.RotatingFileHandler(
            _RUNTIME_LOG,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)  # 文件最低门槛，让 DEBUG 也落地
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s"
        ))
        logger.addHandler(fh)
        logger.setLevel(level)  # 显式 setLevel（覆盖之前测试的 ERROR，避免 sticky）

        # ── excepthook（未捕获异常先写日志） ──
        _previous_excepthook = sys.excepthook

        def _excepthook(exc_type, exc_value, exc_tb):
            # 复刻 traceback 写日志再走默认 hook（默认行为：traceback + exit）
            try:
                tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
                logger.critical("UNCAUGHT EXCEPTION:\n%s", tb_text)
                for h in logger.handlers:
                    try:
                        h.flush()
                    except Exception:  # noqa: BLE001 — flush 失败不阻塞默认 hook
                        pass
                log_event("uncaught_exception",
                          exc_type=exc_type.__name__,
                          exc_repr=repr(exc_value),
                          traceback=tb_text)
            finally:
                _previous_excepthook(exc_type, exc_value, exc_tb)

        sys.excepthook = _excepthook
        # 暴露给测试用（单测想直接调我们的 hook 而不想让真的 default hook 跑）
        # 但要避免重新 setup 时丢失对真正 default 的引用
        globals()["sys_excepthook"] = _excepthook  # noqa: F841 — 测试用

        # ── Threading excepthook（线程内未捕获异常） ──
        _previous_threading_excepthook = threading.excepthook

        def _threading_excepthook(args):
            try:
                tb_text = "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback))
                logger.critical("UNCAUGHT THREAD EXCEPTION (%s):\n%s",
                                args.thread.name, tb_text)
                log_event("uncaught_thread_exception",
                          thread=args.thread.name,
                          exc_type=args.exc_type.__name__,
                          exc_repr=repr(args.exc_value),
                          traceback=tb_text)
            finally:
                _previous_threading_excepthook(args)

        threading.excepthook = _threading_excepthook
        # 暴露给测试用（同 sys_excepthook）
        globals()["threading_excepthook"] = _threading_excepthook  # noqa: F841 — 测试用

        _INSTALLED = True
        logger.info("observability installed (logs_dir=%s, level=%s, process=%s)",
                    _LOGS_DIR, logging.getLevelName(level),
                    process_name or "default")
        return _LOGS_DIR


def reset_for_test() -> None:
    """测试用：重置模块级状态、卸 handler、卸 excepthook。

    pytest fixture 调用以隔离测试间副作用。
    """
    global _LOGS_DIR, _RUNTIME_LOG, _EVENTS_JSONL, _EVENTS_MAX_BYTES, _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            return
        # 卸 handler（保留 stderr handler = setup_logger 已加的）
        for h in list(logger.handlers):
            if isinstance(h, logging.handlers.RotatingFileHandler):
                logger.removeHandler(h)
                h.close()
        # excepthook 已被覆盖到内部 closure；测试不重置 sys.excepthook
        # 因为生产 hook 仍是有用的
        _LOGS_DIR = _RUNTIME_LOG = _EVENTS_JSONL = None
        _EVENTS_MAX_BYTES = None
        _INSTALLED = False


# ── 便捷 helper ────────────────────────────────────────────
def log_error(exc: BaseException, *, context: str = "", **fields: Any) -> None:
    """记录异常：写 ``logger.exception`` + ``log_event("error", ...)``。

    ``context`` 是人类可读的场景名（如 ``"save_predictions"``）；
    ``fields`` 是结构化上下文（如 ``match_id=..., league="epl"``）。
    """
    logger.exception("error in %s: %s", context or "?", exc)
    log_event("error",
              context=context or None,
              exc_type=type(exc).__name__,
              exc_repr=repr(exc),
              **fields)


def log_event(kind: str, **fields: Any) -> None:
    """append-only 写一行 JSON 到 ``events.jsonl``（H8 后支持 size-based 轮转）。

    每行形如 ``{"ts": "2026-10-13T...", "kind": "prediction", "match_id": "...", ...}``。
    ``fields`` 必须 JSON-serializable（str / int / float / bool / None / list/dict of these）。
    """
    path = events_jsonl_path() or _ensure_default_events()
    if path is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "kind": kind,
        **{k: _jsonable(v) for k, v in fields.items()},
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    try:
        _maybe_rotate_events(path, len(line) + 1)  # +1 for newline
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("log_event write failed (%s): %s", path, e)


def _maybe_rotate_events(path: Path, incoming_bytes: int) -> None:
    """H8: 写前检查 events.jsonl 体积，超阈值触发轮转。

    备份文件命名：``events.jsonl.{1..N}.bak``；超出 backup_count 的最旧备份被丢。
    单进程内 open+rename 是原子的（Windows 上 rename 替代 POSIX rename）；多进程
    隔离通过 H9 ``process_name`` 后缀保证。
    """
    if _EVENTS_MAX_BYTES is None or _EVENTS_MAX_BYTES <= 0:
        return
    try:
        current = path.stat().st_size
    except OSError:
        return
    if current + incoming_bytes <= _EVENTS_MAX_BYTES:
        return
    keep = max(1, _EVENTS_BACKUP_COUNT)
    # 步骤 1：丢最旧的备份（如果已经在 keep 位置）
    oldest = path.with_name(f"{path.name}.{keep}.bak")
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            pass
    # 步骤 2：.N.bak 倒推为 .(N+1).bak（从 keep-1 推到 1）
    for i in range(keep - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}.bak")
        dst = path.with_name(f"{path.name}.{i + 1}.bak")
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                pass
    # 步骤 3：当前 events.jsonl → .1.bak（非空才轮转，空文件直接复用）
    if current > 0:
        try:
            path.rename(path.with_name(f"{path.name}.1.bak"))
        except OSError:
            pass
    try:
        path.touch(exist_ok=True)
    except OSError:
        pass


def _ensure_default_events() -> Path | None:
    """``log_event`` 可能在 ``setup()`` 之前被调用：尝试 lazy 初始化。

    若 ``FOOTBALL_DIR`` 不可写（pytest tmp_path 场景），返回 None 静默丢。
    """
    try:
        setup()
        return _EVENTS_JSONL
    except Exception as e:
        logger.debug("lazy observability setup failed: %s", e)
        return None


def _jsonable(v: Any) -> Any:
    """把任意值尽量压成 JSON-serializable（list/dict 递归；其他原样）。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)  # 兜底：path / exception / 等


# ── 读 events.jsonl 工具（供 ML 反馈 / 训练管线）────────────
def iter_events(
    *,
    kind: str | None = None,
    since_ts: str | None = None,
    limit: int | None = None,
    path: Path | None = None,
):
    """惰性读 ``events.jsonl``。``kind`` 过滤事件类型；``since_ts`` ISO 字符串下界。

    返回 dict 流（每行一个）；解析失败行记 logger.warning 跳过。
    """
    p = path or events_jsonl_path()
    if p is None or not p.exists():
        return
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("iter_events: bad json line skipped: %s", e)
                continue
            if kind and rec.get("kind") != kind:
                continue
            if since_ts and rec.get("ts", "") < since_ts:
                continue
            out.append(rec)
            if limit is not None and len(out) >= limit:
                break
    yield from out


__all__ = [
    "setup",
    "reset_for_test",
    "log_error",
    "log_event",
    "iter_events",
    "logs_dir",
    "runtime_log_path",
    "events_jsonl_path",
]
