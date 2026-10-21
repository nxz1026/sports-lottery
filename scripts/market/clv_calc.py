"""建议 3 子项 2：CLV（Closing Line Value）计算 —— 模型概率 vs 竞彩收盘去水概率。

CLV = ln(p_model / p_close)。为什么它是主指标：命中率是**结算后**的结果（n 小时不可信，
路线图 §1.2），而 CLV 是**结算前**就能算的「比市场早拿到多少」—— 它衡量的是决策质量，不是运气。

收盘价来源：`fact.jc_odds_history`（契约 v1.4 盘口全历史，每行一个版本，`seq_no` 从 0 递增）。
  - 上游 `is_close` **恒 False**（`ingest/jc_odds_write.py` 明写「本层不猜封盘」），
    所以收盘由本层判定 = **该场 had 的最后一版**（max(seq_no)）；
  - 实测（2026-10-21，402 场）：**全部 402 场的 had 最后一版都早于开球**，平均提前 **9.52 小时**
    （范围 0.04 ~ 59.48 小时）⇒ 它确实是「开球前最后一版」，不是开球后的终盘。本层仍再校验一次
    「早于开球」，不靠运气。
  - ⚠️ **时区陷阱（实测踩过）**：`fact.jc_match.kickoff_bj` 是 `timestamp **without** time zone`
    （存的是 BJT 墙上时钟），而 `fact.jc_odds_history.update_ts` 是 `timestamptz`。
    在 SQL 里直接比这两个列，PostgreSQL 会按**会话时区**解释 `kickoff_bj`：
    会话是 UTC 时会把 BJT 当成 UTC ⇒ 领先开球的时间**凭空多出 8 小时**
    （曾据此写下「平均提前 17.52 小时」，正确值是 **9.52 小时**）。
    本模块在 Python 侧显式把 `kickoff_bj` 当作 BJT（`replace(tzinfo=BJT)`）再比较，不依赖会话时区。

`p_model` 取**模型自己的**输出 `probs.h/d/a`（缺失时回退 `reasoning_factors.*_prob_weighted`），
**绝不**取顶层 `*_true_prob`（那是 `predictor.py` 从 `match` 透传的**市场先验** = ESPN 赔率去水）
或 `*_implied` —— 拿市场概率当模型概率算 CLV 是循环论证（见 `model/archive.py::_prob` 的说明）。

⚠️ 关联不上的场次**不硬凑**（错配会污染 CLV 均值）⇒ 宁可该场没有 CLV。
⚠️ `p_model = 0` 或 `p_close = 0` 时 `market.clv.clv()` 会抛 ValueError（这是对的：
   `-inf` 不许混进账本）⇒ 本层**跳过并计数**，不静默。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from core.bjt import BJT
from core.log import logger
from psycopg import Connection
from psycopg.types.json import Jsonb
from store import pg

from market.clv import clv
from market.devig import safe_devig

# 竞彩 had 的选项码 → 模型概率的键（与 `analysis.clv_match.option_code` 的取值域一致）
_OPTION_CODES = ("h", "d", "a")

# 归档行 → 候选 CLV 场次（只要关联上竞彩 match_id、且概率齐）
# 注意：**同一场会有多份归档**（每天跑一次预测 ⇒ 每天一份）⇒ 不能任意取一条，
# 必须取「收盘前最后一次模型意见」—— 这个选择在本模块用 Python 做（见 compute_clv），
# 因为「收盘时刻」要先查出来才知道哪些归档行算数。
_SRC_SQL = """
SELECT am.archive_id, am.match_key, am.match_date, ar.league_key, am.jc_match_id,
       am.p_home, am.p_draw, am.p_away, ar.run_at, m.kickoff_bj
FROM model.pred_archive_match am
JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
LEFT JOIN fact.jc_match m ON m.match_id = am.jc_match_id
WHERE am.jc_match_id IS NOT NULL
  AND am.p_home IS NOT NULL AND am.p_draw IS NOT NULL AND am.p_away IS NOT NULL
  AND (%s::date IS NULL OR am.match_date >= %s::date)
  AND (%s::date IS NULL OR am.match_date <= %s::date)
ORDER BY am.jc_match_id, ar.run_at DESC
"""

# 每场 had 的最后一版（= 收盘候选）
_CLOSE_SQL = """
SELECT DISTINCT ON (match_id) match_id, seq_no, update_ts, odds
FROM fact.jc_odds_history
WHERE play_type = 'had' AND match_id = ANY(%s)
ORDER BY match_id, seq_no DESC
"""

_INSERT_MATCH_SQL = """
INSERT INTO analysis.clv_match (match_id, clv_date, league_key, archive_id, play_type, option_code,
                                 p_model, p_close, clv, odds_close, close_ts)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (match_id, play_type, option_code) DO UPDATE SET
  clv_date = EXCLUDED.clv_date, league_key = EXCLUDED.league_key, archive_id = EXCLUDED.archive_id,
  p_model = EXCLUDED.p_model, p_close = EXCLUDED.p_close, clv = EXCLUDED.clv,
  odds_close = EXCLUDED.odds_close, close_ts = EXCLUDED.close_ts, updated_at = now()
"""

# 日汇总：同一 (日期, 联赛) 覆盖多场比赛 ⇒ 用 INSERT ... SELECT 一次算完（不在 Python 里手搓分位数）
_DAILY_SQL = """
INSERT INTO analysis.clv_daily (clv_date, league_key, n_matches, n_options, mean_clv,
                                median_clv, min_clv, max_clv, updated_at)
SELECT clv_date, league_key,
       count(DISTINCT match_id), count(*),
       avg(clv), percentile_cont(0.5) WITHIN GROUP (ORDER BY clv),
       min(clv), max(clv), now()
FROM analysis.clv_match
WHERE (%s::date IS NULL OR clv_date >= %s::date)
  AND (%s::date IS NULL OR clv_date <= %s::date)
GROUP BY clv_date, league_key
ON CONFLICT (clv_date, league_key) DO UPDATE SET
  n_matches = EXCLUDED.n_matches, n_options = EXCLUDED.n_options, mean_clv = EXCLUDED.mean_clv,
  median_clv = EXCLUDED.median_clv, min_clv = EXCLUDED.min_clv, max_clv = EXCLUDED.max_clv,
  updated_at = now()
"""


def _to_float(v: Any) -> float | None:
    """竞彩赔率是**字符串**（'1.86'）⇒ 统一转 float，失败返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 1.0 else None


def closing_probs(conn: Connection, match_id: int) -> tuple[dict[str, float], Any, dict] | None:
    """该场的**收盘**去水概率 + 收盘时刻 + 收盘赔率原文。

    收盘 = `fact.jc_odds_history` 里该场 had 的 max(seq_no)。
    赔率三项（h/d/a）必须齐全且 >1，否则返回 None（**不猜、不用半个选项**）。
    """
    row = conn.execute(
        "SELECT DISTINCT ON (match_id) match_id, seq_no, update_ts, odds FROM fact.jc_odds_history "
        "WHERE play_type='had' AND match_id=%s ORDER BY match_id, seq_no DESC", (match_id,)).fetchone()
    if not row:
        return None
    odds = row[3] or {}
    h, d, a = _to_float(odds.get("h")), _to_float(odds.get("d")), _to_float(odds.get("a"))
    if h is None or d is None or a is None:
        return None
    probs = safe_devig({"h": h, "d": d, "a": a})
    if not probs or len(probs) < 2:
        return None
    return probs, row[2], odds


def _before_kickoff(close_ts: Any, kickoff_bj: Any) -> bool:
    """收盘时刻是否**早于**开球时刻（跨 naive/aware 安全）。

    ⚠️ `fact.jc_match.kickoff_bj` 是 `timestamp without time zone`（naive，存 BJT 墙上时钟），
    `fact.jc_odds_history.update_ts` 是 `timestamptz`（aware）。
    Python 里直接比这两个会抛 `TypeError: can't compare offset-naive and offset-aware datetimes`；
    而在 SQL 里比又会按会话时区解释 kickoff_bj（实测把领先时间算多 8 小时，见模块 docstring）。
    ⇒ 这里显式把 kickoff_bj 当作 BJT。

    任一为空 ⇒ 返回 True（**不拦**：缺数据时不该凭空否掉一场，交由调用方决定）。
    """
    if close_ts is None or kickoff_bj is None:
        return True
    ko = kickoff_bj if getattr(kickoff_bj, "tzinfo", None) is not None \
        else kickoff_bj.replace(tzinfo=BJT)
    return close_ts < ko


def compute_clv(conn: Connection, date_from: str | None = None,
                date_to: str | None = None) -> dict[str, Any]:
    """扫描归档行 → 算逐选项 CLV → 落 `analysis.clv_match` + `analysis.clv_daily`。

    返回真实计数（不编数）：``{"n_candidates", "n_matched", "n_skipped", "n_rows", "n_days"}``。
    """
    src = conn.execute(_SRC_SQL, (date_from, date_from, date_to, date_to)).fetchall()
    n_candidates = len(src)
    if not src:
        conn.execute(_DAILY_SQL, (date_from, date_from, date_to, date_to))
        return {"n_candidates": 0, "n_matched": 0, "n_skipped": 0, "n_rows": 0, "n_days": 0}

    # 收盘价批量取（一次查询，避免 N+1）
    match_ids = sorted({r[4] for r in src})
    closes: dict[int, tuple[dict[str, float], Any, dict]] = {}
    for row in conn.execute(_CLOSE_SQL, (match_ids,)).fetchall():
        odds = row[3] or {}
        h, d, a = _to_float(odds.get("h")), _to_float(odds.get("d")), _to_float(odds.get("a"))
        if h is None or d is None or a is None:
            continue
        probs = safe_devig({"h": h, "d": d, "a": a})
        if probs and len(probs) >= 2:
            closes[row[0]] = (probs, row[2], odds)

    # 同一场可能有多份归档（每天一次预测）⇒ 取「**收盘前最后一次**模型意见」。
    # SQL 已按 (match_id, run_at DESC) 排序 ⇒ 顺序扫描时第一条满足条件的即是所求。
    picked: dict[int, tuple] = {}
    for r in src:
        mid = r[4]
        if mid in picked:
            continue
        close = closes.get(mid)
        if close is None:
            continue
        close_ts = close[1]
        run_at = r[8]
        if close_ts is not None and run_at is not None and run_at > close_ts:
            continue   # 这份模型意见发生在收盘之后 ⇒ 不算数，往下一份找
        picked[mid] = r

    n_matched = n_skipped = n_rows = 0
    n_considered = len(picked)
    for mid, r in picked.items():
        (archive_id, match_key, match_date, league_key, _match_id,
         p_home, p_draw, p_away, _run_at, kickoff_bj) = r
        p_close, close_ts, odds_raw = closes[mid]
        # 收盘必须早于开球（实测 402/402 成立，这里再校验一次，不靠运气）。
        # ⚠️ `kickoff_bj` 是 naive（BJT 墙上时钟）、`close_ts` 是 aware ⇒ 必须显式处理，
        #    否则 Python 直接比会抛 TypeError（见 _before_kickoff 与模块 docstring 的时区陷阱）。
        if not _before_kickoff(close_ts, kickoff_bj):
            n_skipped += 1
            continue
        p_models = {"h": float(p_home), "d": float(p_draw), "a": float(p_away)}
        wrote = False
        for code in _OPTION_CODES:
            p_model = p_models[code]
            p_c = p_close.get(code)
            if p_c is None or p_c <= 0 or p_model <= 0:
                continue  # clv() 对 0 会抛（不许 -inf 进账本）⇒ 跳过该选项
            try:
                value = clv(p_model, p_c)
            except ValueError:
                continue
            conn.execute(_INSERT_MATCH_SQL, (
                mid, match_date, league_key, archive_id, "had", code,
                p_model, p_c, value, Jsonb(odds_raw), close_ts))
            n_rows += 1
            wrote = True
        if wrote:
            n_matched += 1
        else:
            n_skipped += 1
    # 没关联上收盘价、或所有归档行都晚于收盘的场次，也算跳过
    n_skipped += len({r[4] for r in src}) - n_considered

    conn.execute(_DAILY_SQL, (date_from, date_from, date_to, date_to))
    n_days = conn.execute(
        "SELECT count(*) FROM analysis.clv_daily WHERE (%s::date IS NULL OR clv_date >= %s::date)"
        " AND (%s::date IS NULL OR clv_date <= %s::date)",
        (date_from, date_from, date_to, date_to)).fetchone()[0]
    logger.info("clv: 候选 %d 场 → 可算 %d 场 / 跳过 %d 场 → 落 %d 行明细、%d 天汇总",
                n_candidates, n_matched, n_skipped, n_rows, n_days)
    return {"n_candidates": n_candidates, "n_matched": n_matched, "n_skipped": n_skipped,
            "n_rows": n_rows, "n_days": n_days}


MIN_N_MATCHES = 5   # n 少于这个数 ⇒ 不展示 CLV 均值（宁可空白不误导，与命中率同一条纪律）


def daily_rows(conn: Connection, days: int = 30) -> list[dict[str, Any]]:
    """日汇总（给 web 层读）。按日期倒序。

    每行带 ``n_matches`` 与 ``insufficient`` 标记：``n_matches < MIN_N_MATCHES`` 时
    ``insufficient=True`` ⇒ 调用方**不展示均值**（宁可空白不误导，
    与路线图 §1.2「n=18 的命中率不可信」同一条纪律）。
    """
    cutoff = (datetime.now(BJT).date() - timedelta(days=max(1, days))).isoformat()
    rows = conn.execute(
        "SELECT clv_date, league_key, n_matches, n_options, mean_clv, median_clv, min_clv, max_clv"
        " FROM analysis.clv_daily WHERE clv_date >= %s::date"
        " ORDER BY clv_date DESC, league_key", (cutoff,)).fetchall()
    out = []
    for r in rows:
        n_matches = r[2]
        out.append({
            "date": str(r[0]), "league_key": r[1], "n_matches": n_matches, "n_options": r[3],
            "mean_clv": float(r[4]) if r[4] is not None else None,
            "median_clv": float(r[5]) if r[5] is not None else None,
            "min_clv": float(r[6]) if r[6] is not None else None,
            "max_clv": float(r[7]) if r[7] is not None else None,
            "insufficient": n_matches < MIN_N_MATCHES,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI：算 CLV 并打印日汇总。

    用法：
        PYTHONPATH=scripts python scripts/market/clv_calc.py                 # 全量重算
        PYTHONPATH=scripts python scripts/market/clv_calc.py --from 2026-10-01 --to 2026-10-20
    """
    import argparse

    ap = argparse.ArgumentParser(description="CLV 计算：模型概率 vs 竞彩收盘去水概率")
    ap.add_argument("--from", dest="date_from", help="起始日 YYYY-MM-DD（缺省 = 全部）")
    ap.add_argument("--to", dest="date_to", help="结束日 YYYY-MM-DD（缺省 = 全部）")
    args = ap.parse_args(argv)

    with pg.write_conn("app") as conn:
        stats = compute_clv(conn, date_from=args.date_from, date_to=args.date_to)
        rows = daily_rows(conn, days=3650)
    logger.info("clv: %s", stats)
    for r in rows[:30]:
        shown = "—(样本不足)" if r["insufficient"] else f"{r['mean_clv']:+.4f}"
        print(f"{r['date']}  {r['league_key']:<12} n={r['n_matches']:<4} 均值={shown}")
    return 0
