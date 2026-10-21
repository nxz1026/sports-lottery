"""建议 4 子项 ③：用**竞彩官方赔率**结算推荐单 → `analysis.rec_settle` + 战绩。

结算口径（路线图 §6.6.2 第 ③ 条）：
- **结算价** = `fact.jc_result.sp_home/sp_draw/sp_away`（竞彩**官方公布**的结算 SP）。
  ⚠️ 结算 SP ≠ 决策 SP（竞彩每 10 分钟变盘）⇒ 两个都存，任何人可复算。
- **命中判定** = `fact.jc_result.win_flag`（H/D/A）；`win_flag` 为空时用比分 `ft_h/ft_a` 兜底，
  并在 `settle_source` 里记明用的是哪一种。
- **战绩统计用固定注额**（默认 2 元/注）—— 推荐单里的 Kelly 建议注额是「建议」，
  战绩要可比就必须用统一注额，否则 ROI 是「按当时的建议仓位回算」的假象。

口径纪律（与建议 3 同一套）：**n 不足不给 ROI**（宁可空白不误导）。

用法：
    PYTHONPATH=scripts python scripts/decision/settle.py                # 结算全部未结算的推荐单
    PYTHONPATH=scripts python scripts/decision/settle.py --dry          # 只报计划
"""
from __future__ import annotations

import argparse
from typing import Any

from core.log import logger
from psycopg import Connection
from store import pg

# 战绩统计用的固定注额（元/注）
DEFAULT_STAKE = 2.0

# n 少于这个数 ⇒ 不给 ROI（与建议 3 的 MIN_N_MATCHES 同一条纪律）
MIN_N_SETTLED = 5

# option_code → fact.jc_result 的 SP 列
_SP_COLUMN = {"h": "sp_home", "d": "sp_draw", "a": "sp_away"}
# option_code → win_flag 取值
_WIN_FLAG = {"h": "H", "d": "D", "a": "A"}

_PICKS_SQL = """
SELECT report_id, match_id, option_code, sp_decide, p_model, match_date, league_key
  FROM analysis.ev_report
 WHERE match_id IS NOT NULL AND option_code IS NOT NULL
 ORDER BY report_id
"""

_RESULTS_SQL = """
SELECT match_id, sp_home, sp_draw, sp_away, ft_h, ft_a, win_flag
  FROM fact.jc_result
 WHERE match_id = ANY(%s)
"""


def is_hit(option_code: str, win_flag: str | None, ft_h: Any, ft_a: Any) -> tuple[bool | None, str]:
    """命中判定：优先 ``win_flag``（官方标记），缺失时用比分。返回 ``(是否命中, 来源)``。

    两者都没有 ⇒ ``(None, 'none')``（**不判**：不许把「查不到」当成「没中」）。
    """
    if win_flag:
        flag = str(win_flag).strip().upper()
        expected = _WIN_FLAG.get(option_code)
        if expected and flag in ("H", "D", "A"):
            return flag == expected, "win_flag"
        return None, "none"
    if ft_h is not None and ft_a is not None:
        h, a = int(ft_h), int(ft_a)
        if option_code == "h":
            return h > a, "score"
        if option_code == "d":
            return h == a, "score"
        if option_code == "a":
            return h < a, "score"
    return None, "none"


def settle_pick(pick: dict[str, Any], result: dict[str, Any] | None,
                stake: float = DEFAULT_STAKE) -> dict[str, Any] | None:
    """结算一条推荐单；没有官方赛果 ⇒ None（不算「没中」）。"""
    if not result:
        return None
    option = pick["option_code"]
    sp_settle = result.get(_SP_COLUMN.get(option, ""))
    hit, source = is_hit(option, result.get("win_flag"), result.get("ft_h"), result.get("ft_a"))
    if hit is None:
        return None
    sp_settle_f = float(sp_settle) if sp_settle is not None else None
    payout = round(stake * sp_settle_f, 4) if (hit and sp_settle_f is not None) else 0.0
    return {
        "pick_id": pick.get("report_id"),
        "match_id": pick["match_id"],
        "option_code": option,
        "league_key": pick.get("league_key"),
        "match_date": pick.get("match_date"),
        "sp_decide": pick.get("sp_decide"),
        "sp_settle": sp_settle_f,
        "p_model": pick.get("p_model"),
        "hit": hit,
        "stake": stake,
        "payout": payout,
        "pnl": round(payout - stake, 4),
        "settle_source": source,
    }


_UPSERT_SQL = """
INSERT INTO analysis.rec_settle (pick_id, match_id, option_code, league_key, match_date,
                                 sp_decide, sp_settle, p_model, hit, stake, payout, pnl,
                                 settle_source, settled_at, updated_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), now())
ON CONFLICT (match_id, option_code) DO UPDATE SET
  pick_id = EXCLUDED.pick_id, league_key = EXCLUDED.league_key, match_date = EXCLUDED.match_date,
  sp_decide = EXCLUDED.sp_decide, sp_settle = EXCLUDED.sp_settle, p_model = EXCLUDED.p_model,
  hit = EXCLUDED.hit, stake = EXCLUDED.stake, payout = EXCLUDED.payout, pnl = EXCLUDED.pnl,
  settle_source = EXCLUDED.settle_source, updated_at = now()
RETURNING settle_id
"""


def settle_all(conn: Connection, *, stake: float = DEFAULT_STAKE) -> dict[str, Any]:
    """结算所有可结算的推荐单（幂等）。"""
    picks = []
    for r in conn.execute(_PICKS_SQL).fetchall():
        picks.append({
            "report_id": int(r[0]), "match_id": int(r[1]), "option_code": r[2],
            "sp_decide": float(r[3]) if r[3] is not None else None,
            "p_model": float(r[4]) if r[4] is not None else None,
            "match_date": r[5], "league_key": r[6],
        })
    if not picks:
        return {"n_picks": 0, "n_settled": 0, "n_pending": 0, "n_hit": 0, "roi": None}
    mids = sorted({p["match_id"] for p in picks})
    results = {int(r[0]): {"sp_home": r[1], "sp_draw": r[2], "sp_away": r[3],
                           "ft_h": r[4], "ft_a": r[5], "win_flag": r[6]}
               for r in conn.execute(_RESULTS_SQL, (mids,)).fetchall()}

    n_settled = n_hit = n_pending = 0
    for p in picks:
        s = settle_pick(p, results.get(p["match_id"]), stake=stake)
        if s is None:
            n_pending += 1
            continue
        conn.execute(_UPSERT_SQL, (
            s["pick_id"], s["match_id"], s["option_code"], s["league_key"], s["match_date"],
            s["sp_decide"], s["sp_settle"], s["p_model"], s["hit"], s["stake"],
            s["payout"], s["pnl"], s["settle_source"],
        ))
        n_settled += 1
        if s["hit"]:
            n_hit += 1
    return {"n_picks": len(picks), "n_settled": n_settled, "n_pending": n_pending,
            "n_hit": n_hit, "roi": roi_of(conn, stake=stake)}


def roi_of(conn: Connection, *, stake: float = DEFAULT_STAKE) -> dict[str, Any]:
    """战绩汇总：命中数 / 注数 / 回报 / ROI。**n 不足不给 ROI**。"""
    row = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE hit), coalesce(sum(stake), 0), "
        "       coalesce(sum(payout), 0) FROM analysis.rec_settle").fetchone()
    n = int(row[0] or 0)
    n_hit = int(row[1] or 0)
    staked = float(row[2] or 0.0)
    returned = float(row[3] or 0.0)
    roi = round((returned - staked) / staked, 6) if staked > 0 else None
    return {
        "n_settled": n,
        "n_hit": n_hit,
        "n_miss": n - n_hit,
        "staked": round(staked, 2),
        "returned": round(returned, 2),
        "pnl": round(returned - staked, 2),
        "roi": roi if n >= MIN_N_SETTLED else None,
        "insufficient": n < MIN_N_SETTLED,
        "min_n": MIN_N_SETTLED,
        "stake_per_pick": stake,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用竞彩官方赔率结算推荐单（建议 4 子项 ③）")
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE, help="战绩统计注额（元/注）")
    ap.add_argument("--dry", action="store_true", help="只报计划，不写库")
    args = ap.parse_args(argv)

    with pg.write_conn("app") as conn:
        if args.dry:
            n = conn.execute("SELECT count(*) FROM analysis.ev_report "
                             "WHERE match_id IS NOT NULL").fetchone()[0]
            logger.info("（只读模式）当前推荐单 %s 条，未写库", n)
            return 0
        stats = settle_all(conn, stake=args.stake)
        logger.info("settle: %s", stats)
        for r in conn.execute(
                "SELECT match_date, league_key, option_code, sp_decide, sp_settle, hit, pnl "
                "  FROM analysis.rec_settle ORDER BY match_date DESC LIMIT 10").fetchall():
            logger.info("  · %s [%s] %s 决策 SP=%s 结算 SP=%s 命中=%s 盈亏=%s",
                        r[0], r[1], r[2], r[3], r[4], r[5], r[6])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
