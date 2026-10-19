"""web.services.source_meta — sources/status 路由的纯辅助函数。

不放进 web/routers/sources.py 的原因：routes 模块 import fastapi 链路，
测试在 fastapi 缺失的本地环境无法 import。本文件零 web 框架依赖，
纯 stdlib + datetime，便于本地 pytest 覆盖。
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ``systemctl list-timers`` 行格式（systemd 252+）：
#   NEXT                        LEFT LAST                        PASSED UNIT                       ACTIVATES
#   Mon 2026-10-12 01:30:00 UTC  14h Sun 2026-10-11 01:30:01 UTC 9h ago league-daily-predict.timer league-daily-predict.service
# NEXT/LAST 各 4 token（Day YYYY-MM-DD HH:MM:SS TZ），LEFT/PASSED 各 1-2 token（"14h" / "1h 30min"）。
# UNIT/ACTIVATES 各 1 token，固定在行末。解析：先剥右侧 3 字段（PASSED/UNIT/ACTIVATES），
# 再切 LEFT 单 token，最后 NEXT/LAST 各自用剩余首末 4 token。
_TIMESTAMP_RE = re.compile(
    r"^(?P<day>[A-Z][a-z]{2})\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<tz>[A-Z]{2,4})$"
)
_BJT = timezone(timedelta(hours=8))


def _parse_ts_field(tokens: list[str]) -> datetime | None:
    """"Mon 2026-10-12 01:30:00 UTC" 这种 4 token 字段 → aware UTC datetime。"""
    if len(tokens) != 4:
        return None
    joined = " ".join(tokens)
    m = _TIMESTAMP_RE.match(joined)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group('date')} {m.group('time')} {m.group('tz')}",
                               "%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return None
    # %Z 在 Python 3.10+ 才识别 TZ 名缩写；显式用 UTC 表「systemctl 表中时间必带时区且左对齐」
    return dt.replace(tzinfo=timezone.utc)


def list_timer_runs() -> dict[str, dict]:
    """读 ``systemctl list-timers`` 行 → {unit_name: {next_utc, next_bjt, last_utc, last_bjt}}。

    行格式示例（systemd 252+，左对齐定宽）：
        NEXT                        LEFT LAST                        PASSED  UNIT                       ACTIVATES
        Mon 2026-10-12 01:30:00 UTC  14h Sun 2026-10-11 01:30:01 UTC  9h ago  league-daily-predict.timer league-daily-predict.service
        Tue 2026-10-13 01:30:00 UTC   1h Mon 2026-10-12 01:30:00 UTC  23h     league-daily-predict.timer league-daily-predict.service

    解析策略：从右剥 3 token（UNIT, ACTIVATES 之外 PASSED 末 token），向前试探是否
    "ago" / "left" / "in" 等副词触发 PASSED 2 token；LEFT 同理；最后剩 NEXT/LAST 各 4 token。
    非 systemd / 权限缺失 / hang 5s+ → 静默返回空 dict，绝不阻塞状态面。
    """
    out: dict[str, dict] = {}
    try:
        proc = subprocess.run(
            ["systemctl", "list-timers", "--no-pager"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.info("systemctl list-timers unavailable: %s", e)
        return out
    if proc.returncode != 0 or not proc.stdout:
        return out
    # systemd 在 "ago" / "left" 等副词前还有时间 token；其他情形 PASSED 单 token。
    # PASSED 长度只由末 token 是否为副词决定（见下方 passed_len）。
    _TIME_QUALIFIERS = ("ago", "left", "in")
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("NEXT") or "timers listed" in line:
            continue
        tokens = line.split()
        if len(tokens) < 10:
            continue
        unit = tokens[-2]
        if not unit.endswith(".timer"):
            continue
        # 列序固定（systemd 252+）：NEXT(4) LEFT(1~2) LAST(4) PASSED(1~2) UNIT(1) ACTIVATES(1)
        # 先判 PASSED 长度（紧邻 UNIT 之左）。只要 -3 是副词即认定 PASSED 占 2 token，
        # -4 是否匹配 time-suffix 不强求（systemd 不同版本 "1m" / "1min" / "0.5h" 都见过）。
        passed_len = 2 if tokens[-3] in _TIME_QUALIFIERS else 1
        passed_start = -(passed_len + 2)  # PASSED 起点（负索引，指向 '9h' / '14h'）
        # LAST 在 PASSED 之左，固定 4 token。
        last_end = passed_start  # LAST 终点（PASSED 起点）exclusive
        last_start = last_end - 4  # LAST 起点
        # LEFT 在 LAST 之左（NEXT 紧邻 LEFT 之右）。
        # 审计 R45：此处原有的 left_len / left_end_excl / _TIME_SUFFIXES 判定链计算后
        # 从未被消费（ruff F841）——LEFT 的合法性实际由下方 next_tokens/last_tokens
        # 的 len==4 校验兜住，已删除该死代码。
        next_tokens = tokens[:4]
        last_tokens = tokens[last_start:last_end]
        if len(next_tokens) != 4 or len(last_tokens) != 4:
            continue
        next_utc = _parse_ts_field(next_tokens)
        last_utc = _parse_ts_field(last_tokens)
        if next_utc is None or last_utc is None:
            continue
        next_bjt = next_utc.astimezone(_BJT)
        out[unit] = {
            "next_utc": next_utc.isoformat(),
            "next_bjt": next_bjt.strftime("%Y-%m-%d %H:%M:%S"),
            "last_utc": last_utc.isoformat(),
            "last_bjt": last_utc.astimezone(_BJT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    return out


def recent_job_stats(list_jobs) -> dict:
    """封装「取最近任务统计 + 最近一次失败」。

    list_jobs 参数化便于测试注入；生产侧传 ``web.services.jobs.list_jobs``。
    返回：``{by_status, last, last_failure, items}``。
    """
    rows = list_jobs(limit=50)
    counts: dict[str, int] = {}
    last: dict | None = None
    last_failure: dict | None = None
    for row in rows:
        st = row.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        if last is None:
            last = {
                "id": row.get("id"),
                "status": row.get("status"),
                "trigger": row.get("trigger"),
                "created_at": row.get("created_at"),
                "finished_at": row.get("finished_at"),
            }
        if last_failure is None and st == "failed":
            last_failure = {
                "id": row.get("id"),
                "script": row.get("script"),
                "trigger": row.get("trigger"),
                "created_at": row.get("created_at"),
                "finished_at": row.get("finished_at"),
                "exit_code": row.get("exit_code"),
                "error": (str(row.get("error"))[:120] if row.get("error") else None),
            }
    items = []
    for row in list_jobs(limit=10):
        items.append({
            "id": row.get("id"),
            "status": row.get("status"),
            "trigger": row.get("trigger"),
            "script": row.get("script"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "exit_code": row.get("exit_code"),
            "error": (str(row.get("error"))[:120] if row.get("error") else None),
        })
    return {"by_status": counts, "last": last, "last_failure": last_failure, "items": items}