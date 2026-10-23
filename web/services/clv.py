"""web.services.clv — CLV 日汇总的只读视图（建议 3 子项 ②）。

与 `web/routers/jc.py` 同一套访问约定：本文件**顶层不 import psycopg / scripts**（T5 免 psycopg 环境），
只在函数内延迟 import `store.pg` 与 `market.clv_calc`；模块级自举一次 `scripts/` 到 sys.path。

读不出来一律**降级为空列表**（页面显示「暂无数据」，不 5xx、不编数）。

⚠️ **P1（2026-10-23）**：「读失败」与「本来就没有」必须**分开表达**。
旧实现读失败也返回 `[]` ⇒ 页面把「数据库连不上」印成「暂无 CLV 数据…成对样本不存在」
（实测：读失败时页面在说谎，而且说的是一个**误导性的原因**）。
⇒ 现在读失败一律带 `degraded=True` 标志（照抄 `recommend.offer_freshness()` 的既有范式）。
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


def _read_rows(days: int = 30) -> tuple[list[dict[str, Any]], bool]:
    """读 CLV 日汇总。返回 ``(rows, degraded)``；degraded=True ⇒ 读失败（不是「没有」）。"""
    try:
        from market import clv_calc
        from store import pg

        with pg.read_conn("ro") as conn:
            return clv_calc.daily_rows(conn, days=days), False
    except Exception as exc:  # noqa: BLE001 - 只读视图，任何异常降级为空
        logger.warning("web.clv 读 CLV 日汇总失败（降级为空，degraded=True）：%s", exc)
        return [], True


def daily_rows(days: int = 30) -> list[dict[str, Any]]:
    """CLV 日汇总（按日期倒序）。读取失败 → 返回 []（降级，不编数）。

    ⚠️ 只看这个返回值**分不出**「读失败」与「没有样本」——
    需要区分的调用方请用 `daily_rows_status()`。
    """
    rows, _degraded = _read_rows(days)
    return rows


def daily_rows_status(days: int = 30) -> dict[str, Any]:
    """CLV 日汇总 + **degraded 标志**（True = 读失败，不是「没有样本」）。"""
    rows, degraded = _read_rows(days)
    return {"rows": rows, "degraded": degraded}


def summary(rows: list[dict[str, Any]] | None = None,
            degraded: bool = False) -> dict[str, Any]:
    """看板用的整体摘要。

    口径纪律：``n_matches`` 不足时**不给均值**，只给 n（宁可空白不误导）。

    ⚠️ **P2（2026-10-23）：「天数」与「行数」是两个量，不能混用。**
    旧实现把 ``len(rows)`` 当成「天数」⇒ 生产实测「31 行」被印成「31 天」，
    而这 31 行其实是 **9 个不同日期 × 若干联赛**（行 = （日期 × 联赛）单元）。
    ⇒ 现在：
      · ``n_days``  = **去重后的日期数**（真的是「天」）；
      · ``n_rows``  = 行数（（日期 × 联赛）单元数，新增键，语义见下）。
    页面要同时显示时用「N 天 · M 行」，不许再把行数印成天数。

    ⚠️ **判不足要看「总场次」，不能看「逐行标记」**（2026-10-21 实测修正）：
    逐日（×联赛）单元的 n 普遍只有 1~4 ⇒ 每行都带 ``insufficient=True``；
    若拿这个逐行标记去过滤整体聚合，**整体均值会被一起抹掉**（明明总样本 71 场），
    看板就永远是一片空白。
    ⇒ 整体口径：按**总场次**判不足（< MIN_N_MATCHES 才不给均值），
    均值用各行 ``mean_clv`` 按 ``n_matches`` 加权还原。

    ⚠️ P1（2026-10-23）：``degraded`` 标志随摘要一起返回（读失败 ≠ 没有样本）。
    """
    if rows is None:
        st = daily_rows_status()
        rows = st["rows"]
        degraded = degraded or st["degraded"]
    if not rows:
        return {"n_days": 0, "n_rows": 0, "n_matches": 0, "mean_clv": None,
                "insufficient": True, "degraded": degraded}
    from market.clv_calc import MIN_N_MATCHES

    total_n = sum(int(r.get("n_matches") or 0) for r in rows)
    usable = [r for r in rows if r.get("mean_clv") is not None]
    mean = None
    if usable:
        weighted = sum(float(r["mean_clv"]) * int(r["n_matches"]) for r in usable)
        weight = sum(int(r["n_matches"]) for r in usable)
        mean = round(weighted / weight, 4) if weight else None
    # 天数 = 去重后的日期数（不是行数！）；行数 = （日期 × 联赛）单元数。
    days = {str(r.get("date")) for r in rows if r.get("date")}
    return {
        "n_days": len(days),
        "n_rows": len(rows),
        "n_matches": total_n,
        "mean_clv": mean,
        "insufficient": total_n < MIN_N_MATCHES or mean is None,
        "degraded": degraded,
    }
