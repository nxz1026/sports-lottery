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

⚠️ **P2-N（2026-10-23）荐单门槛口径标注**：推荐端门槛 2026-10-23 起是
**EV ≥ 12.9%**（用户批准），而生成端**只做 upsert、从不清理**当日早先批次
留下的行 ⇒ 库里会同时存在「旧门槛批次的行」和「现行门槛批次的行」。
⇒ 页面若照旧把它们都当「荐单」展示，就与面板上写的「门槛 EV ≥ 12.9%」
**自相矛盾**（页面在说假话）。
⇒ 现在：① 每条 pick 带 `meets_threshold` / `threshold_label`（不达标的逐条标注）；
② 战绩/命中率统计带 `n_below_threshold` / `threshold_note`（标明「混合口径」）。
⇒ **只标注，不删历史行**（历史记录不可篡改）。
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

# ⚠️ **P2-N（2026-10-23）**：已结算的荐单里，有多少条**不满足现行门槛**
#   （join 回 `analysis.ev_report` 拿当时的 EV）⇒ 用来标出「混合口径」。
_BELOW_THRESHOLD_SQL = """
SELECT count(*)
  FROM analysis.rec_settle s
  JOIN analysis.ev_report p ON p.report_id = s.pick_id
 WHERE p.ev IS NOT NULL AND p.ev < %s
"""


def _meets_threshold(ev: float | None) -> bool | None:
    """这条荐单是否满足**现行**门槛（``None`` = EV 缺失 ⇒ 无法判定）。

    ⚠️ EV 缺失时**不许**当成「达标」——「没数据」说成「达标」同样是假话。
    """
    if ev is None:
        return None
    from core.betting_policy import MIN_EV

    return ev >= MIN_EV


def _below_threshold_label() -> str:
    """不满足现行门槛的荐单，印给用户的标注（**纯文本**，前端用 ``esc()`` 渲染）。

    ⚠️ **P2-N（2026-10-23）**：这不是「模型算错了」，而是**批次残留** ——
    生成端只做 upsert（``ON CONFLICT DO UPDATE``）、**从不清理**当日早先批次
    留下的行 ⇒ 门槛提高后那些行会继续躺在表里 ⇒ 页面若照旧把它们当「荐单」
    展示，就与面板上写的「门槛 EV ≥ 12.9%」自相矛盾（页面在说假话）。
    ⇒ 现行门槛下**不可能**产出 EV 低于门槛的行（生成端 ``MIN_EV`` 直接过滤）
    ⇒ 不达标的行**必然**是更早批次的残留 ⇒ 如实这么写。

    ⚠️ 文案里**不写旧门槛的数字**（早期门槛更低，但把两个数字摆在一起容易
    被读成「现行门槛 = 那个数」）⇒ 旧门槛只留在代码/文档层（``EV_PREV_MIN_EV``）。
    """
    from core.betting_policy import EV_THRESHOLD_TEXT

    return (f"不满足现行门槛（{EV_THRESHOLD_TEXT}）：本条来自更早的荐单批次，"
            "不代表现行门槛下的推荐。")


def _record_threshold_note(n_below: int, n_total: int = 0) -> str:
    """战绩/命中率面板的**门槛口径说明**（用户可见，纯文本）。

    ⇒ 战绩里的荐单跨了两套门槛（旧的更低、现行 = EV ≥ 12.9%）⇒ 命中率/ROI
    是**混合口径**，必须写出来，否则用户会把它当成现行门槛的表现。
    """
    from core.betting_policy import EV_THRESHOLD_TEXT

    if n_below <= 0:
        return f"战绩口径：已结算的荐单全部满足现行门槛（{EV_THRESHOLD_TEXT}）。"
    return (f"战绩口径：已结算的 {n_total} 条荐单里有 {n_below} 条"
            f"不满足现行门槛（{EV_THRESHOLD_TEXT}）—— 来自更早的荐单批次 ⇒ "
            "命中率/ROI 是混合口径，不能直接当作现行门槛的表现。")


def _picks_threshold_note(n_below: int, n_total: int = 0) -> str:
    """荐单面板的**门槛口径说明**（用户可见，纯文本）。"""
    from core.betting_policy import EV_THRESHOLD_TEXT

    if n_below <= 0:
        return ""
    return (f"⚠️ 本面板有 {n_below} 条荐单不满足现行门槛（{EV_THRESHOLD_TEXT}）："
            "它们是更早批次留下的行（同一比赛日多次跑批时旧行不会被清除），"
            "不代表现行门槛下的推荐。")


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
            ev = float(r[6]) if r[6] is not None else None
            # ⚠️ P2-N：逐条给「是否满足现行门槛」的判定 + 不达标的用户可见标注
            meets = _meets_threshold(ev)
            out.append({
                "report_id": int(r[0]),
                "made_at": str(r[1]),
                "match_id": int(r[2]),
                "option_code": r[3],
                "sp": float(r[4]) if r[4] is not None else None,
                "p_model": float(r[5]) if r[5] is not None else None,
                "ev": ev,
                "kelly": float(r[7]) if r[7] is not None else None,
                "home_cn": leg.get("home_cn"),
                "away_cn": leg.get("away_cn"),
                "all_ev": leg.get("all_ev"),
                "sp_snapshot_ts": leg.get("sp_snapshot_ts"),
                "match_date": str(r[10]) if r[10] else None,
                "league_key": r[11],
                "note": r[9],
                "meets_threshold": meets,
                "threshold_label": "" if meets is not False else _below_threshold_label(),
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
        from core.betting_policy import MIN_EV
        from store import pg

        with pg.read_conn("ro") as conn:
            row = conn.execute(_RECORD_SQL).fetchone()
            n = int(row[0] or 0)
            n_hit = int(row[1] or 0)
            staked = float(row[2] or 0.0)
            returned = float(row[3] or 0.0)
            by_day = conn.execute(_BY_DAY_SQL, (30,)).fetchall()
            detail = conn.execute(_DETAIL_SQL, (50,)).fetchall()
            # ⚠️ P2-N：战绩里有多少条**不满足现行门槛**（跨了两套门槛 ⇒ 混合口径）
            _below_row = conn.execute(_BELOW_THRESHOLD_SQL, (MIN_EV,)).fetchone()
            n_below = int((_below_row[0] if _below_row else 0) or 0)
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
            # ⚠️ P2-N：门槛口径（混合口径要写出来，不许让用户误读成现行门槛的表现）
            "n_below_threshold": n_below,
            "threshold_note": _record_threshold_note(n_below, n),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("web.recommend 读战绩失败（降级为空，degraded=True）：%s", exc)
        return {"n_settled": 0, "n_hit": 0, "n_miss": 0, "staked": 0.0, "returned": 0.0,
                "pnl": 0.0, "roi": None, "insufficient": True, "min_n": min_n,
                "by_day": [], "detail": [], "degraded": True,
                # ⚠️ 读失败 ⇒ 门槛口径**未知**（不许报 0，那是在编数）
                "n_below_threshold": None, "threshold_note": ""}


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


def _pricing_note() -> str:
    """荐单面板的**用户可见**定价说明。

    ⚠️ **P2-I（2026-10-23）**：这里的每个数字都**从唯一真源派生**
    （``core.betting_policy``），**不许手写** "12.9%" / "5%"：
    P2-H4 事故就是文案手打「阈值已对齐为 EV ≥ 12.9%」而代码是 5% ⇒
    **用户被告知一个不存在的策略**，而静态审计 / 单测 / 守门全都没抓到。
    ⇒ 派生 ⇒ 数值**不可能漂移**；再加通用守门（用户可见文本里的 EV 阈值 / 水位
    数字必须与常量一致）。
    """
    from core.betting_policy import (
        EV_APPROVAL_DATE,
        EV_EDGE_TEXT,
        EV_FACTOR_TEXT,
        EV_SNAPSHOT_TEXT,
        EV_THRESHOLD_TEXT,
        WATER_FACTOR_TEXT,
        WATER_LEVEL_TEXT,
        WATER_RANGE_TEXT,
        WATER_RETURN_TEXT,
    )

    # ⚠️ **P2-J（2026-10-23）**：这段是**纯文本**（前端用 `esc()` 渲染 ⇒ 不解析 Markdown）
    # ⇒ **不许**再写 `**粗体**`：星号会**原样漏给用户**（生产浏览器验收时当场抓到）。
    # ⇒ 强调一律用中文「」。
    # ⚠️ **P2-L（2026-10-23）阈值政策变更**：阈值从 5% 提到**实测水位 12.9%**
    #   （用户 2026-10-23 批准）⇒ 下面这句话必须改：
    #   旧文案说「不需要再拿水位当门槛」「阈值是政策值，不是水位」—— 阈值一旦等于水位，
    #   这两句就**互相矛盾**（也成了假话）⇒ 必须如实写「阈值 = 水位，但**比扛水更严**」。
    #   扛水的**数学门槛**仍是 EV 为正（p/q > m ≈ 1.129）；
    #   EV ≥ 12.9% 等价于 p/q ≥ 1.275（模型要比市场高出 27.5%）⇒ **更严一道**。
    # ⚠️ 文案纪律：① 不许出现 Markdown 标记（P2-J）；② 口径数字全部从常量派生；
    #   ③ 「水位」二字后面 12 个字符内**不许跟数字**（否则会被守门当成「手写水位」），
    #   所以日期一律放在「水位」**前面**。
    return ("定价 = 竞彩官方 SP（决策时刻那一版）；结算 = 竞彩官方结算 SP。"
            "⚠️ 本模型概率为「市场锚定」，EV 含 ESPN↔竞彩 跨市场定价差，"
            "不等于模型有同等真实优势。"
            f"⚠️ 竞彩 had 三侧含水（生产实测 2026-10-23：当日三项隐含概率和 − 1 = "
            f"{WATER_LEVEL_TEXT}；等额三侧投注每元回报 ≈ {WATER_RETURN_TEXT} 元 "
            f"⇒ 水位 ≈ {WATER_RANGE_TEXT}）。"
            "⇒ 单边投注「扛水」的数学门槛 = EV 为正"
            "（EV = p×SP − 1，p/q > "
            f"{WATER_FACTOR_TEXT} 时才为正）。"
            f"推荐端阈值 {EV_THRESHOLD_TEXT}（用户 {EV_APPROVAL_DATE} 批准：= 当前实测水位；"
            "等价于 p/q ≥ "
            f"{EV_FACTOR_TEXT}，即模型概率要比去水后的市场概率高出约 "
            f"{EV_EDGE_TEXT}，因此比「扛水」更严一道；"
            f"{EV_SNAPSHOT_TEXT}）。")


def snapshot() -> dict[str, Any]:
    """看板一次拿全：今日推荐单 + 战绩 + 定价赔率新鲜度。

    ⚠️ P1（2026-10-23）：``picks_degraded`` / ``record.degraded`` 标志 ——
    **读失败**与**本来就没有**必须分开表达（页面据此印「⚠️ 读取失败」而不是
    「你还没跑脚本」/「已结算 0 注」）。
    """
    st = today_picks_status()
    rec = record()
    # ⚠️ P2-N：荐单面板的门槛口径（逐条标注 + 面板级说明）
    n_below = sum(1 for p in st["picks"] if p.get("meets_threshold") is False)
    from core.betting_policy import EV_THRESHOLD_TEXT

    return {
        "date": _bjt_today(),
        "picks": st["picks"],
        "n_picks": len(st["picks"]),
        "picks_degraded": st["degraded"],
        "record": rec,
        "offer_freshness": offer_freshness(),
        "pricing_note": _pricing_note(),
        # ⚠️ 门槛数字**由后端给**，前端不许自己写死（写死 = 会漂移）
        "ev_threshold_text": EV_THRESHOLD_TEXT,
        "picks_n_below_threshold": n_below,
        "picks_threshold_note": _picks_threshold_note(n_below, len(st["picks"])),
        "calendar_note": ("推荐单是「手动生成」的（scripts/decision/recommend.py）："
                          "生产上没有任何 cron / systemd 跑它。"
                          "采集端 2026-10-22 起也改成手动（国内机 scripts\\refresh_now.bat 一键跑完"
                          "「采集 → 推送 → 远端装载 → 出荐单」）。"
                          "所以「今日推荐单」为空 = 还没跑，不是故障；"
                          "要看实时盘口，请先跑一次采集刷新。"),
    }
