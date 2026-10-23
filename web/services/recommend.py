"""web.services.recommend —— 今日推荐单与战绩的只读视图（建议 4）。

与 `web/services/clv.py` 同一套访问约定：本文件**顶层不 import psycopg / scripts**
（T5 免 psycopg 环境），只在函数内延迟 import `store.pg`；模块级自举一次 `scripts/`。

⚠️ **时区纪律**：`analysis.ev_report.made_at` 是 `timestamptz`，
「今天」必须按 **BJT** 判定（`made_at AT TIME ZONE 'Asia/Shanghai'`）——
直接写 `made_at::date` 会按**会话时区**（生产 PG 常为 UTC）算 ⇒ 差一天
（这与建议 3 的 `kickoff_bj` 时区坑同源，见路线图 §7 第 12 条）。

读不出来一律**降级为空**（页面显示「暂无推荐单」，不 5xx、不编数）。

⚠️ **P1（2026-10-23）**：「读失败」与「本来就没有」必须**分开表达**。
旧实现两种情况都返回空列表 / `n_settled=0` ⇒ 页面把
「数据库连不上」印成「你还没跑脚本」+「已结算 0 注」（实测：读失败时页面在说谎）。
⇒ 现在读失败一律带 `degraded=True` 标志（照抄 `offer_freshness()` 的既有范式），
页面据此印「⚠️ 读取失败」而不是「没有数据」。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("web.recommend")

# T3 启动链 `PYTHONPATH=. uvicorn web.api:app` 不含 scripts/：自举一次（追加到末尾）。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)


def _bjt_today() -> str:
    """今天（BJT 日历日）。用 core.bjt 唯一真源，与引擎侧同一口径。"""
    from core.bjt import now_bjt

    return now_bjt().date().isoformat()


_PICKS_SQL = """
SELECT report_id, made_at, match_id, option_code, sp_decide, p_model, ev, kelly,
       legs, note, match_date, league_key
  FROM analysis.ev_report
 WHERE (made_at AT TIME ZONE 'Asia/Shanghai')::date = %s::date
 ORDER BY ev DESC NULLS LAST, report_id
"""

_RECORD_SQL = """
SELECT count(*), count(*) FILTER (WHERE hit), coalesce(sum(stake), 0), coalesce(sum(payout), 0)
  FROM analysis.rec_settle
"""

_BY_DAY_SQL = """
SELECT (settled_at AT TIME ZONE 'Asia/Shanghai')::date AS d,
       count(*), count(*) FILTER (WHERE hit), coalesce(sum(stake), 0), coalesce(sum(payout), 0)
  FROM analysis.rec_settle
 GROUP BY 1 ORDER BY 1 DESC LIMIT %s
"""

_DETAIL_SQL = """
SELECT match_id, option_code, league_key, match_date, sp_decide, sp_settle, hit, stake, payout, pnl
  FROM analysis.rec_settle ORDER BY match_date DESC NULLS LAST, match_id DESC LIMIT %s
"""


def _read_picks(day: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    """读今日推荐单。返回 ``(rows, degraded)``。

    ``degraded=True`` ⇒ **读失败**（不是「今天没有荐单」）。两者必须分开表达。
    """
    try:
        from store import pg

        with pg.read_conn("ro") as conn:
            rows = conn.execute(_PICKS_SQL, (day or _bjt_today(),)).fetchall()
        out = []
        for r in rows:
            legs = r[8] or []
            leg = legs[0] if legs else {}
            out.append({
                "report_id": int(r[0]),
                "made_at": str(r[1]),
                "match_id": int(r[2]),
                "option_code": r[3],
                "sp": float(r[4]) if r[4] is not None else None,
                "p_model": float(r[5]) if r[5] is not None else None,
                "ev": float(r[6]) if r[6] is not None else None,
                "kelly": float(r[7]) if r[7] is not None else None,
                "home_cn": leg.get("home_cn"),
                "away_cn": leg.get("away_cn"),
                "all_ev": leg.get("all_ev"),
                "sp_snapshot_ts": leg.get("sp_snapshot_ts"),
                "match_date": str(r[10]) if r[10] else None,
                "league_key": r[11],
                "note": r[9],
            })
        return out, False
    except Exception as exc:  # noqa: BLE001 - 只读视图，任何异常降级为空
        logger.warning("web.recommend 读今日推荐单失败（降级为空，degraded=True）：%s", exc)
        return [], True


def today_picks(day: str | None = None) -> list[dict[str, Any]]:
    """今日推荐单（BJT 日口径）。读取失败 → []（降级，不编数）。

    ⚠️ 只看这个返回值**分不出**「读失败」与「今天没荐单」——
    需要区分的调用方请用 `today_picks_status()`。
    """
    rows, _degraded = _read_picks(day)
    return rows


def today_picks_status(day: str | None = None) -> dict[str, Any]:
    """今日推荐单 + **degraded 标志**（True = 读失败，不是「没有」）。"""
    rows, degraded = _read_picks(day)
    return {"picks": rows, "degraded": degraded}


def record(min_n: int = 5) -> dict[str, Any]:
    """战绩汇总（n 不足不给 ROI —— 与建议 3 同一纪律：宁可空白不误导）。

    ⚠️ P1（2026-10-23）：返回值带 ``degraded`` 标志 —— **读失败**（`degraded=True`）
    与「确实一注都没结算」（`degraded=False`）必须分开表达；旧实现两种都返回
    `n_settled=0` ⇒ 页面把「连不上库」印成「已结算 0 注」。
    """
    try:
        from store import pg

        with pg.read_conn("ro") as conn:
            row = conn.execute(_RECORD_SQL).fetchone()
            n = int(row[0] or 0)
            n_hit = int(row[1] or 0)
            staked = float(row[2] or 0.0)
            returned = float(row[3] or 0.0)
            by_day = conn.execute(_BY_DAY_SQL, (30,)).fetchall()
            detail = conn.execute(_DETAIL_SQL, (50,)).fetchall()
        roi = round((returned - staked) / staked, 6) if staked > 0 else None
        days = []
        for d in by_day:
            dn = int(d[1] or 0)
            d_staked = float(d[3] or 0.0)
            d_returned = float(d[4] or 0.0)
            days.append({
                "date": str(d[0]), "n": dn, "hits": int(d[2] or 0),
                "staked": round(d_staked, 2), "returned": round(d_returned, 2),
                "roi": round((d_returned - d_staked) / d_staked, 6)
                if (d_staked > 0 and dn >= min_n) else None,
                "insufficient": dn < min_n,
            })
        return {
            "n_settled": n, "n_hit": n_hit, "n_miss": n - n_hit,
            "staked": round(staked, 2), "returned": round(returned, 2),
            "pnl": round(returned - staked, 2),
            "roi": roi if n >= min_n else None,
            "insufficient": n < min_n,
            "min_n": min_n,
            "by_day": days,
            "detail": [{
                "match_id": int(x[0]), "option_code": x[1], "league_key": x[2],
                "match_date": str(x[3]) if x[3] else None,
                "sp_decide": float(x[4]) if x[4] is not None else None,
                "sp_settle": float(x[5]) if x[5] is not None else None,
                "hit": x[6], "stake": float(x[7]), "payout": float(x[8]), "pnl": float(x[9]),
            } for x in detail],
            "degraded": False,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("web.recommend 读战绩失败（降级为空，degraded=True）：%s", exc)
        return {"n_settled": 0, "n_hit": 0, "n_miss": 0, "staked": 0.0, "returned": 0.0,
                "pnl": 0.0, "roi": None, "insufficient": True, "min_n": min_n,
                "by_day": [], "detail": [], "degraded": True}


def offer_freshness(threshold_hours: float | None = None) -> dict[str, Any]:
    """定价赔率快照的新鲜度（页面要**显式显示**，别让用户以为盘口是实时的）。

    **唯一真源 = `scripts/decision/recommend.py::offer_freshness`**（函数内延迟 import；
    web 层顶层不得 import scripts.*）⇒ 页面显示与生成端守门是同一张表、同一阈值，
    不会出现「页面说新鲜、生成端说过期」的两套口径。

    读失败 → degraded=True、字段为 None，**不编数**。
    """
    out: dict[str, Any] = {
        "threshold_hours": None, "latest_snap": None, "age_hours": None,
        "stale": None, "arrival_ts": None, "arrival_age_hours": None,
        "arrival_stale": None, "degraded": False,
    }
    try:
        from decision.recommend import DEFAULT_MAX_OFFER_AGE_HOURS
        from decision.recommend import offer_freshness as _of
        from store import pg

        th = DEFAULT_MAX_OFFER_AGE_HOURS if threshold_hours is None else threshold_hours
        with pg.read_conn("ro") as conn:
            out.update(_of(conn, th))
    except Exception as exc:  # noqa: BLE001 - 只读视图，任何异常降级
        logger.warning("web.recommend 读赔率新鲜度失败（降级）：%s", exc)
        out["degraded"] = True
    return out


def snapshot() -> dict[str, Any]:
    """看板一次拿全：今日推荐单 + 战绩 + 定价赔率新鲜度。

    ⚠️ P1（2026-10-23）：``picks_degraded`` / ``record.degraded`` 标志 ——
    **读失败**与**本来就没有**必须分开表达（页面据此印「⚠️ 读取失败」而不是
    「你还没跑脚本」/「已结算 0 注」）。
    """
    st = today_picks_status()
    rec = record()
    return {
        "date": _bjt_today(),
        "picks": st["picks"],
        "n_picks": len(st["picks"]),
        "picks_degraded": st["degraded"],
        "record": rec,
        "offer_freshness": offer_freshness(),
        "pricing_note": ("定价 = 竞彩官方 SP（决策时刻那一版）；结算 = 竞彩官方结算 SP。"
                         "⚠️ 本模型概率为「市场锚定」，EV 含 ESPN↔竞彩 跨市场定价差，"
                         "不等于模型有同等真实优势。"
                         "⚠️ 竞彩 had 三侧含水（**生产实测 2026-10-23：当日三项隐含概率和 − 1 = 12.9%**；"
                         "等额三侧投注每元回报 ≈ 0.89 元 ⇒ 水位 ≈ 11~13%），"
                         "单边投注要先扛掉这笔水 ⇒ EV 必须**不低于水位**才谈得上盈利"
                         "（推荐端阈值已对齐为 EV ≥ 12.9%）。"),
        "calendar_note": ("推荐单是**手动生成**的（scripts/decision/recommend.py）："
                          "生产上没有任何 cron / systemd 跑它。"
                          "采集端 2026-10-22 起也改成手动（国内机 scripts\\refresh_now.bat 一键跑完"
                          "「采集 → 推送 → 远端装载 → 出荐单」）。"
                          "所以「今日推荐单」为空 = 还没跑，不是故障；"
                          "要看实时盘口，请先跑一次采集刷新。"),
    }
