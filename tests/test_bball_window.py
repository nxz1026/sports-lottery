"""R40-2: scripts/bball/run.py::_window 3 cases direct + D-33 lock.

篮球数据窗口串 helper 0 direct tests; run() 集成层仅做 `_window(now)` 调用,
helper 本身未锁。函数: `(now) -> "{YYYYMMDD}-{YYYYMMDD}"`,
两个日期分别是 now 转 BJT 当日 + 次日, 跨日时 last day 应自动 +1。

helper 含一条关键契约 (D-33):
- 严格 17 字符 `YYYYMMDD-YYYYMMDD` (8 + 1 + 8), 后段总是 from-date + 1 天
- now 时区无关: 不管传入 UTC 还是 BJT, 都先 astimezone(BJT) 再 .strftime
  BJT = UTC+8, 故 23:30 BJT 是次日跨日边界, 16:00 UTC 是当日 BJT 00:00
- 全部分支: now 类型 datetime (需有 tzinfo); 缺 tzinfo 时 astimezone(BJT)
  在 Python 3 假定 naive → 用本地时区, Windows 本地时区可能是 CST +0800

3 tests (EASY lockdown):
- BJT 23:30 输入 -> 跨日: from-date = 当日, to-date = 次日
- UTC 16:00 输入 (BJT 00:00 当日或次日) -> 仍按 BJT 拼日期
- D-33 严格 17 字符 + 8+1+8 分隔锁
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bball import run as bball_run  # noqa: E402

BJT = timezone(timedelta(hours=8))


# ──────────────────────── BJT input ────────────────────────


def test_window_d33_bjt_late_evening_crosses_day():
    """R40-2 D-33: BJT 23:30 输入 -> 自动跨日, 后段 = from + 1 day。"""
    now = datetime(2026, 7, 15, 23, 30, tzinfo=BJT)
    out = bball_run._window(now)
    # 2026-07-15 -> 2026-07-16
    assert out == "20260715-20260716"


# ──────────────────────── UTC input ────────────────────────


def test_window_utc_input_converted_to_bjt():
    """R40-2: UTC 16:00 输入 -> astimezone(BJT) 仍是当日, 后段 = +1 day。

    2026-07-15 16:00 UTC = 2026-07-16 00:00 BJT -> from=20260716, to=20260717
    """
    now = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    out = bball_run._window(now)
    assert out == "20260716-20260717"


# ──────────────────────── 严格格式锁 ────────────────────────


def test_window_d33_strict_17_chars_format():
    """R40-2 D-33: 输出严格 17 字符 8+1+8 (YYYYMMDD-YYYYMMDD)。

    锁定 regex pattern, 防止分界符 / 字段长度悄悄漂移。
    """
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # BJT 2026-09-28 20:00
    out = bball_run._window(now)
    assert len(out) == 17
    assert re.match(r"^\d{8}-\d{8}$", out)
    # 后段必须 = from + 1 天 (转换 datetime 校验)
    from_str = out[:8]
    to_str = out[9:]
    from_dt = datetime.strptime(from_str, "%Y%m%d")
    to_dt = datetime.strptime(to_str, "%Y%m%d")
    assert to_dt - from_dt == timedelta(days=1)
