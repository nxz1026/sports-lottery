"""web.services.clv — CLV 日汇总的只读视图（建议 3 子项 ②）。

与 `web/routers/jc.py` 同一套访问约定：本文件**顶层不 import psycopg / scripts**（T5 免 psycopg 环境），
只在函数内延迟 import `store.pg` 与 `market.clv_calc`；模块级自举一次 `scripts/` 到 sys.path。

读不出来一律**降级为空列表**（页面显示「暂无数据」，不 5xx、不编数）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("web.clv")

# T3 启动链 `PYTHONPATH=. uvicorn web.api:app` 不含 scripts/：自举一次，端点才可 import store/market。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def daily_rows(days: int = 30) -> list[dict[str, Any]]:
    """CLV 日汇总（按日期倒序）。读取失败 → 返回 []（降级，不编数）。"""
    try:
        from market import clv_calc
        from store import pg

        with pg.read_conn("ro") as conn:
            return clv_calc.daily_rows(conn, days=days)
    except Exception as exc:  # noqa: BLE001 - 只读视图，任何异常降级为空
        logger.warning("web.clv 读 CLV 日汇总失败（降级为空）：%s", exc)
        return []


def summary(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """看板用的整体摘要。

    口径纪律：``n_matches`` 不足时**不给均值**，只给 n（宁可空白不误导）。
    """
    rows = rows if rows is not None else daily_rows()
    if not rows:
        return {"n_days": 0, "n_matches": 0, "mean_clv": None, "insufficient": True}
    total_n = sum(int(r.get("n_matches") or 0) for r in rows)
    usable = [r for r in rows if not r.get("insufficient")]
    mean = None
    if usable:
        weighted = sum(float(r["mean_clv"]) * int(r["n_matches"])
                       for r in usable if r.get("mean_clv") is not None)
        weight = sum(int(r["n_matches"]) for r in usable if r.get("mean_clv") is not None)
        mean = round(weighted / weight, 4) if weight else None
    return {
        "n_days": len(rows),
        "n_matches": total_n,
        "mean_clv": mean,
        "insufficient": total_n < 5 or mean is None,
    }
