"""Logging setup for the league-predict engine。

P-6 增强：本模块顶层 ``logger = setup_logger("predict")`` 之后，``core.observability.setup()``
会被自动调用以挂载：
- RotatingFileHandler（``FOOTBALL_DIR/logs/runtime.log``，10MB × 5 份）
- ``sys.excepthook`` / ``threading.excepthook``（未捕获异常先写日志）
- JSONL 结构化事件流（``FOOTBALL_DIR/logs/events.jsonl``）

进程崩溃后仍可从 ``runtime.log`` 回看 stderr 内容。
"""

from __future__ import annotations

import logging
import os


def setup_logger(name: str = "predict") -> logging.Logger:
    logger = logging.getLogger(name)
    # 支持环境变量 LEAGUE_PREDICT_LOG_LEVEL 配置日志级别
    level_str = os.environ.get("LEAGUE_PREDICT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logger.setLevel(level)
    # 清理旧 handler 防止 pytest 并行时累积
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter("[%(name)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


logger = setup_logger("predict")


# 顶层 ``import core.log`` 触发 observability 装挂（lazy — pytest tmp_path
# 场景下让 observability 自己用 FOOTBALL_DIR；测试 fixture 再调 reset_for_test 隔离）。
# process_name="predict" 让 predict subprocess 写 ``events.predict.jsonl``，与
# web 进程的 ``events.web.jsonl`` 不抢同一文件（Round 9 H9 多进程隔离）。
try:
    from core import observability  # noqa: F401 — 触发装挂
    # R13 #1：补 events.jsonl 10MB×5 rotation（对齐 runtime.log）——
    # features_snapshot 26 维 + 7 处 log_error + 5 联赛 × 多 league 持续累积，
    # 不限大小 → 月级 GB 级增长；启用 H8 rotation（observability.py:_maybe_rotate_events）
    observability.setup(process_name="predict", events_max_bytes=10 * 1024 * 1024,
                        events_backup_count=5)
except Exception:  # noqa: BLE001 — 装挂失败不应阻塞 logger 使用
    # 测试早期 / 异常环境下 skip；上层仍可显式调用 observability.setup
    pass


__all__ = ["setup_logger", "logger"]
