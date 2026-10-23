"""Logging setup for the league-predict engine。

P-6 增强：本模块顶层 ``logger = setup_logger("predict")`` 之后，``core.observability.setup()``
会被自动调用以挂载：
- RotatingFileHandler（``FOOTBALL_DIR/logs/runtime.log``，10MB × 5 份）
- ``sys.excepthook`` / ``threading.excepthook``（未捕获异常先写日志）
- JSONL 结构化事件流（``FOOTBALL_DIR/logs/events.jsonl``）

进程崩溃后仍可从 ``runtime.log`` 回看 stderr 内容。

⚠️ **P1（2026-10-23）**：观测文件名后缀由 **env ``LEAGUE_PROCESS_NAME``** 驱动，
**不许**在这里写死。

事故：web 进程里 `core/__init__.py` → `core.predictor` → **本模块** 先跑，
于是 `observability.setup(process_name="predict")` 先装上了；
`web/api.py` 随后再 `setup(process_name="web")` ⇒ **参数不一致 ⇒ RuntimeError**
（`observability.setup` 的重入守门）⇒ 被 api.py 的 except 吞掉 ⇒
**生产上根本没有 `events.web.jsonl` / `runtime.web.log`**，
web 进程把事件写进了 `events.predict.jsonl`（与 predict 子进程**抢同一个文件**，
H9 跨进程隔离形同虚设），而 `web/api.py` 的注释却在宣称「web 进程独立写」。

修法（不是「允许重入切换」——那会让 ai_analyze 被 web 进程 import 时切走，更坏）：
**进程名由 env 决定，各入口在 import core 之前 setdefault**：
- 引擎 predict 进程：不设 ⇒ 默认 "predict"；
- web 进程（`web/api.py`）：`LEAGUE_PROCESS_NAME=web`；
- ai_analyze 子进程：`LEAGUE_PROCESS_NAME=ai_analyze`；
- ai_enrich 子进程：`LEAGUE_PROCESS_NAME=ai_enrich`。
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


# 观测文件名的进程后缀（events.{name}.jsonl / runtime.{name}.log）。
# 由 env 驱动（见模块 docstring 的事故复盘）：**每个进程入口**在 import core 之前
# setdefault 自己的名字 ⇒ 本模块是**第一个**执行 setup 的，所以它读到的 env 就是对的。
# ⚠️ 不要改成「允许重入切换」：ai_analyze 在 web 进程内被 import 时会被切走，反而更坏。
PROCESS_NAME = os.environ.get("LEAGUE_PROCESS_NAME") or "predict"


# 顶层 ``import core.log`` 触发 observability 装挂（lazy — pytest tmp_path
# 场景下让 observability 自己用 FOOTBALL_DIR；测试 fixture 再调 reset_for_test 隔离）。
# process_name 让各进程写各自的 events 文件（Round 9 H9 多进程隔离）：
# predict / web / ai_analyze / ai_enrich 各一份，互不抢文件。
try:
    from core import observability  # noqa: F401 — 触发装挂
    # R13 #1：补 events.jsonl 10MB×5 rotation（对齐 runtime.log）——
    # features_snapshot 26 维 + 7 处 log_error + 5 联赛 × 多 league 持续累积，
    # 不限大小 → 月级 GB 级增长；启用 H8 rotation（observability.py:_maybe_rotate_events）
    observability.setup(process_name=PROCESS_NAME, events_max_bytes=10 * 1024 * 1024,
                        events_backup_count=5)
except Exception:  # noqa: BLE001 — 装挂失败不应阻塞 logger 使用
    # 测试早期 / 异常环境下 skip；上层仍可显式调用 observability.setup
    pass


__all__ = ["PROCESS_NAME", "setup_logger", "logger"]
