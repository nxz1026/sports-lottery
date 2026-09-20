"""新鲜度监控：基于 ops.topics 的 topic 最近到达时间检测超阈值。

数据源复用 /api/jc/ops 已有逻辑（store.ops），本层只做"超阈值判定"+"日志输出"。
不查 DB、不改表、不依赖任何 core 模块。

约定：阈值默认 24h（按分钟计 1440）。调用方可传 threshold_hours 自定义。
"""
from __future__ import annotations

from typing import Iterable


DEFAULT_THRESHOLD_HOURS = 24


def evaluate_topics(
    topics: Iterable[dict],
    threshold_hours: float = DEFAULT_THRESHOLD_HOURS,
) -> dict:
    """对 ops.topics 列表逐行判定是否超阈值。

    入参每行至少有：topic (str), min_since_latest (number, 分钟)。
    返回 {"threshold_hours": ..., "ok": bool, "stale": [...], "all": [...]}
    - stale 仅超阈值的项（每项含 topic、minutes、hours）
    - all 全部项（便于 UI 展示）
    - ok: 无 stale 时 True
    """
    threshold_minutes = threshold_hours * 60
    all_rows = []
    stale_rows = []
    for t in topics or []:
        name = str(t.get("topic") or "")
        try:
            minutes = float(t.get("min_since_latest") or 0.0)
        except (TypeError, ValueError):
            minutes = 0.0
        hours = round(minutes / 60, 2)
        row = {"topic": name, "minutes": minutes, "hours": hours,
               "latest_arrival": t.get("latest_arrival")}
        all_rows.append(row)
        if minutes >= threshold_minutes:
            stale_rows.append(row)
    stale_rows.sort(key=lambda r: -r["minutes"])
    return {
        "threshold_hours": threshold_hours,
        "ok": len(stale_rows) == 0,
        "stale": stale_rows,
        "all": all_rows,
    }


def format_alert_lines(result: dict) -> list[str]:
    """把 evaluate_topics 结果格式化为日志告警行（每条 stale 一行）。"""
    if result["ok"]:
        return []
    th = result["threshold_hours"]
    out = []
    for r in result["stale"]:
        out.append(
            f"[FRESHNESS] stale topic={r['topic']} "
            f"minutes={r['minutes']} hours={r['hours']} "
            f"latest_arrival={r['latest_arrival']} threshold_h={th}"
        )
    return out
