"""建议 5 子项 ②：赔率异动检测 → `analysis.odds_move`（**相邻快照跳变**口径）。

⚠️ **为什么不用「开盘 vs 临场」的首末差**（生产实测，2026-10-21）：

| 口径 | 样本 | ≥5% 命中率 | 评价 |
|---|---|---|---|
| 首末差（开盘 vs 临场） | 406 场（版本数 ≥ 5） | **77%**（314 场） | **纯噪音** —— 几乎每场都「异动」 |
| **相邻快照跳变（4~20 分钟）** | **23,955** 个相邻对 | **0.405%**（97 次） | 可用 ⇒ ≈**3.2 组/天**（按场次聚合的推送粒度） |

⇒ 默认阈值 **5%**（可配 3%，命中率 1.21% ⇒ ≈9.7 组/天，噪音偏大）。

⚠️ **窗口为什么是 4~20 分钟、不是「恰好 10 分钟」**（生产实测，2026-10-21）：
采集节奏**并不恒定**（实测间隔有 1/3/4/5/7/8/9/10/20/23/60/97/120 分钟）。
若把窗口卡死在 540~660 秒 ⇒ **324 场里只有 176 场进得了窗口**（漏 45%），
而且「恰好 10 分钟」这个前提本身就不成立。
按间隔分桶看 ≥5% 命中率（30 天）：

| 间隔桶 | 相邻对 | ≥5% 命中率 | 判定 |
|---|---|---|---|
| < 4 分钟 | 2,399 | 0.167% | 采集过密（多半是重扫）⇒ 排除 |
| **4~20 分钟** | **23,955** | **0.405%** | ✅ **真变盘区间**（覆盖全部 324 场） |
| 20~60 分钟 | 1,793 | 1.004% | 跨批次 ⇒ 开始混进假异动 |
| > 60 分钟 | 1,824 | **9.868%** | **隔夜/重开重定价 ⇒ 纯假异动**（24 倍） |

⇒ 窗口取 **240~1200 秒**：短于 4 分钟是重扫、长于 20 分钟开始进假异动。

**「开盘价」口径的坑（实测 406 场里 405 场中招）**：`fact.jc_offer` 最早一版的
`options` 常常是 `{}`（竞彩刚开盘时 had 还没值）⇒ 取「第一版快照」算 delta 会全是 NULL。
本模块只取**三项齐全**的版本做相邻对（`options->>'h'/'d'/'a'` 都非空）⇒ 405/406 场可算。

**口径纪律**：
- 落表**每一侧**（h/d/a 各一行，超过阈值才落）⇒ 页面能看出「哪一侧动了」；
  **推送按场次聚合成一条** ⇒ 不刷屏。
- 异动**落表才是资产**（前后两个 `snap_ts` + 前后 SP 都在表里 ⇒ 可复算），
  推送只是呈现；**未配 webhook ⇒ `pushed` 恒为 false，绝不谎报「已推」**。
- ⚠️ **异动是「信息」，不是「信号」**：盘口跳变大 ≠ 模型有价值（路线图 §6.6.5.1：
  本模型概率是市场锚定的，竞彩 had 还有 ~25~33% 的水位）⇒ 告警文案必须带上这句。

用法：
    PYTHONPATH=scripts python scripts/market/odds_move.py                 # 检测 + 落表 + 推送
    PYTHONPATH=scripts python scripts/market/odds_move.py --dry           # 只报计划
    PYTHONPATH=scripts python scripts/market/odds_move.py --threshold 3    # 放宽阈值
    PYTHONPATH=scripts python scripts/market/odds_move.py --no-push        # 只落表不推
"""
from __future__ import annotations

import argparse
import math
from datetime import timedelta
from typing import Any

from core.bjt import now_bjt
from core.log import logger
from psycopg import Connection
from store import pg

# 默认阈值（%）：相邻跳变口径下实测命中 0.405% ⇒ ≈3.2 组/天（按场次聚合）
DEFAULT_THRESHOLD_PCT = 5.0
# 相邻对的时间间隔窗（秒）：**4~20 分钟**
# ⚠️ 采集节奏并不恒定（实测 1/3/4/5/7/8/9/10/20/23/60/97/120 分钟都有）⇒
#    卡死「540~660 秒」会漏掉 324 场里的 148 场（45%）。
#    短于 4 分钟是重扫（≥5% 命中 0.167%）；长于 20 分钟开始混进假异动
#    （20~60 分钟 1.004% / >60 分钟 9.868% ⇒ 隔夜重开重定价）。
MIN_GAP_SEC = 240
MAX_GAP_SEC = 1200
# 检测窗口（小时）
DEFAULT_LOOKBACK_HOURS = 24.0

# 竞彩 had 的三项
_SIDES = ("h", "d", "a")
_SIDE_CN = {"h": "主胜", "d": "平", "a": "客胜"}


def _to_float(v: Any) -> float | None:
    """竞彩 SP 是**字符串**（'1.86'）⇒ 统一转 float；非法/≤1 ⇒ None（不猜、不用半个选项）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 1.0 else None


def jump_pct(sp_prev: float | None, sp_curr: float | None) -> float | None:
    """相邻两版的 SP 跳变幅度（**绝对百分数**）。

    任一缺失、或 **≤ 1**（非法赔率，竞彩 SP 恒 > 1）⇒ None（不拿半个选项算跳变）。
    """
    if sp_prev is None or sp_curr is None or sp_prev <= 1.0 or sp_curr <= 1.0:
        return None
    return abs(sp_curr / sp_prev - 1.0) * 100.0


# 相邻对：只取「~10 分钟间隔」且**前后两版三项齐全**的行
_PAIRS_SQL = """
WITH s AS (
  SELECT m.match_id, m.league_cn, m.kickoff_bj,
         o.snap_ts, o.options,
         lag(o.snap_ts) OVER (PARTITION BY m.match_id ORDER BY o.snap_ts) AS prev_ts,
         lag(o.options)  OVER (PARTITION BY m.match_id ORDER BY o.snap_ts) AS prev_opts
    FROM fact.jc_offer o
    JOIN fact.jc_match m ON m.match_id = o.match_id
   WHERE o.play_type = 'had'
     AND o.snap_ts >= %s
     AND (o.options->>'h') IS NOT NULL
     AND (o.options->>'d') IS NOT NULL
     AND (o.options->>'a') IS NOT NULL
)
SELECT match_id, league_cn, kickoff_bj, prev_ts, snap_ts, prev_opts, options
  FROM s
 WHERE prev_opts IS NOT NULL
   AND (prev_opts->>'h') IS NOT NULL
   AND (prev_opts->>'d') IS NOT NULL
   AND (prev_opts->>'a') IS NOT NULL
   AND EXTRACT(EPOCH FROM (snap_ts - prev_ts)) BETWEEN %s AND %s
   AND snap_ts >= %s
 ORDER BY match_id, snap_ts
"""

_INSERT_SQL = """
INSERT INTO analysis.odds_move (match_id, play_type, option_code, prev_ts, curr_ts,
                                 sp_prev, sp_curr, move_pct, threshold_pct,
                                 league_cn, match_date)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (match_id, play_type, option_code, prev_ts, curr_ts) DO UPDATE SET
  sp_prev = EXCLUDED.sp_prev, sp_curr = EXCLUDED.sp_curr, move_pct = EXCLUDED.move_pct,
  threshold_pct = EXCLUDED.threshold_pct, league_cn = EXCLUDED.league_cn,
  match_date = EXCLUDED.match_date, updated_at = now()
"""


def detect_moves(conn: Connection, *, threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                 lookback_hours: float = DEFAULT_LOOKBACK_HOURS) -> tuple[list[dict[str, Any]], int]:
    """检测相邻快照跳变，返回 ``(moves, n_pairs)``。

    ``moves`` 每个元素是**一个侧**的一次跳变（已过阈值）；``n_pairs`` 是检查过的相邻对数。
    """
    # 窗口起点往前回退一个间隔 ⇒ 跨窗口边界的相邻对也不漏
    start = now_bjt() - timedelta(hours=max(1.0, lookback_hours), minutes=15)
    rows = conn.execute(_PAIRS_SQL, (start, MIN_GAP_SEC, MAX_GAP_SEC, start)).fetchall()
    moves: list[dict[str, Any]] = []
    for r in rows:
        match_id, league_cn, kickoff_bj, prev_ts, curr_ts, prev_opts, curr_opts = r
        prev_opts = prev_opts or {}
        curr_opts = curr_opts or {}
        match_date = kickoff_bj.date().isoformat() if kickoff_bj is not None else None
        for code in _SIDES:
            sp_prev = _to_float(prev_opts.get(code))
            sp_curr = _to_float(curr_opts.get(code))
            pct = jump_pct(sp_prev, sp_curr)
            if pct is None or pct < threshold_pct:
                continue
            moves.append({
                "match_id": int(match_id),
                "option_code": code,
                "prev_ts": prev_ts,
                "curr_ts": curr_ts,
                "sp_prev": sp_prev,
                "sp_curr": sp_curr,
                "move_pct": round(pct, 4),
                "threshold_pct": threshold_pct,
                "league_cn": league_cn,
                "match_date": match_date,
            })
    return moves, len(rows)


def write_moves(conn: Connection, moves: list[dict[str, Any]]) -> int:
    """落 `analysis.odds_move`（幂等：同场同侧同前后版本只一行）。返回写入条数。"""
    n = 0
    for m in moves:
        conn.execute(_INSERT_SQL, (
            m["match_id"], "had", m["option_code"], m["prev_ts"], m["curr_ts"],
            m["sp_prev"], m["sp_curr"], m["move_pct"], m["threshold_pct"],
            m.get("league_cn"), m.get("match_date"),
        ))
        n += 1
    return n


def group_by_match(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按场次聚合成「一场一条」，供推送用（避免一场三点刷屏）。"""
    out: dict[int, dict[str, Any]] = {}
    for m in moves:
        g = out.setdefault(m["match_id"], {
            "match_id": m["match_id"],
            "league_cn": m.get("league_cn"),
            "match_date": m.get("match_date"),
            "prev_ts": m["prev_ts"],
            "curr_ts": m["curr_ts"],
            "sides": [],
        })
        g["sides"].append(m)
        # 时间窗取并集（同一场的多个侧可能跨了不同的相邻对）
        if m["curr_ts"] > g["curr_ts"]:
            g["curr_ts"] = m["curr_ts"]
        if m["prev_ts"] < g["prev_ts"]:
            g["prev_ts"] = m["prev_ts"]
    for g in out.values():
        g["sides"].sort(key=lambda x: -x["move_pct"])
    return list(out.values())


def gap_label() -> str:
    """相邻对窗口的人话标签（**从常量推导**，避免文案与口径脱节）。"""
    return f"{MIN_GAP_SEC // 60}~{MAX_GAP_SEC // 60} 分钟"


def format_alert(groups: list[dict[str, Any]], threshold_pct: float,
                 window_label: str | None = None) -> str:
    """告警文案（飞书 text）。

    ⚠️ 必须带上「异动是信息不是信号」这句 —— 路线图 §6.6.5.1 实测：模型概率与竞彩盘口
    不同源，竞彩 had 还有 ~25~33% 的水位 ⇒ 盘口跳变大 **不构成下注理由**。
    """
    label = window_label or gap_label()
    lines = [f"【赔率异动告警】阈值 {threshold_pct:g}%（相邻 {label} 跳变）"]
    for g in groups:
        lines.append("")
        lines.append(f"· {g.get('league_cn') or '—'} · {g.get('match_date') or '—'} · 场次 {g['match_id']}")
        for s in g["sides"]:
            arrow = "↓" if s["sp_curr"] < s["sp_prev"] else "↑"
            lines.append(
                f"    {_SIDE_CN.get(s['option_code'], s['option_code'])} "
                f"{s['sp_prev']:.2f} → {s['sp_curr']:.2f}（{arrow}{s['move_pct']:.1f}%）")
        lines.append(f"    快照 {g['prev_ts']} → {g['curr_ts']}")
    lines.append("")
    lines.append("⚠️ 盘口异动是「信息」，不是下注理由："
                 "本模型概率与竞彩盘口不同源（市场锚定），且竞彩 had 有约 25~33% 的水位。")
    return "\n".join(lines)


def push_groups(groups: list[dict[str, Any]], threshold_pct: float) -> dict[str, Any]:
    """推送告警到飞书。**未配 webhook ⇒ 跳过并如实返回 pushed=False**（不报错、不谎报）。"""
    if not groups:
        return {"pushed": False, "reason": "no_moves"}
    try:
        from market.notify import push_feishu
    except Exception as exc:  # noqa: BLE001 - 推送不可用不能阻断检测
        return {"pushed": False, "reason": f"notify_unavailable: {type(exc).__name__}"}
    text = format_alert(groups, threshold_pct, window_label=gap_label())
    try:
        res = push_feishu(text)
    except Exception as exc:  # noqa: BLE001 - 推送失败不阻断落库
        return {"pushed": False, "reason": f"push_failed: {type(exc).__name__}"}
    return {"pushed": bool(res.get("ok")), "detail": res}


def already_pushed(conn: Connection, moves: list[dict[str, Any]]) -> set[tuple]:
    """返回**已经推过**的事件键集合（场次, 侧, 前一版, 后一版）。

    ⚠️ **为什么必须查这一步**：检测窗口是 24h ⇒ 同一个异动会被**每一轮**重新检出。
    若不查，就会**每 10 分钟把同一条异动重推一遍** ⇒ 刷屏。
    落表侧靠 `ON CONFLICT` **不重置** `pushed` 来保证「已推」这件事不丢
    （§6.7.3 第 ② 条：没推的绝不能标已推；推过的也不该被重置成没推）。
    """
    if not moves:
        return set()
    keys = [(m["match_id"], m["option_code"], m["prev_ts"], m["curr_ts"]) for m in moves]
    rows = conn.execute(
        "SELECT match_id, option_code, prev_ts, curr_ts "
        "  FROM analysis.odds_move "
        " WHERE pushed "
        "   AND (match_id, option_code, prev_ts, curr_ts) IN "
        "       (SELECT * FROM unnest(%s::bigint[], %s::text[], %s::timestamptz[], %s::timestamptz[]))",
        ([k[0] for k in keys], [k[1] for k in keys],
         [k[2] for k in keys], [k[3] for k in keys])).fetchall()
    return {(r[0], r[1], r[2], r[3]) for r in rows}


def mark_pushed(conn: Connection, groups: list[dict[str, Any]]) -> int:
    """把**确实被推送**的那些侧逐条标成 ``pushed=true``（返回标记条数）。

    ⚠️ 必须**逐条精确标记**（场次 + 侧 + 前后两版）。
    原实现是 ``match_id = ANY(...) AND curr_ts = ANY(...)`` ⇒ **笛卡尔积**
    ⇒ 会把「同场但不同侧 / 不同相邻对」里**没被推送**的行也标成 ``pushed=true``
    ⇒ 正是「绝不谎报已推」这条纪律要防的事。
    """
    n = 0
    for g in groups:
        for s in g["sides"]:
            conn.execute(
                "UPDATE analysis.odds_move "
                "SET pushed = true, pushed_at = now(), updated_at = now() "
                "WHERE match_id = %s AND option_code = %s "
                "AND prev_ts = %s AND curr_ts = %s",
                (s["match_id"], s["option_code"], s["prev_ts"], s["curr_ts"]))
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="赔率异动检测（相邻快照跳变）→ analysis.odds_move")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                    help=f"跳变阈值（%%），默认 {DEFAULT_THRESHOLD_PCT:g}")
    ap.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS,
                    help="检测窗口（小时），默认 24")
    ap.add_argument("--no-push", action="store_true", help="只落表，不推送")
    ap.add_argument("--repush", action="store_true",
                    help="把**已推过**的也重推一次（默认只推没推过的，避免刷屏）")
    ap.add_argument("--dry", action="store_true", help="只报计划，不写库")
    args = ap.parse_args(argv)

    with pg.write_conn("app") as conn:
        moves, n_pairs = detect_moves(conn, threshold_pct=args.threshold,
                                      lookback_hours=args.lookback_hours)
        groups = group_by_match(moves)
        logger.info("odds_move: 相邻对 %d → 异动 %d 侧 / %d 场（阈值 %g%%）",
                    n_pairs, len(moves), len(groups), args.threshold)
        for m in moves:
            logger.info("  · 场次 %d %s %s %.2f→%.2f (%+.1f%%)",
                        m["match_id"], m.get("league_cn") or "-", m["option_code"],
                        m["sp_prev"], m["sp_curr"], m["move_pct"])
        if args.dry:
            logger.info("（只读模式：未写库、未推送）")
            return 0
        n_written = write_moves(conn, moves)
        # 只推**没推过**的（检测窗口 24h ⇒ 同一个异动每轮都会被重新检出；
        # 不去重就会每 10 分钟重推同一条 ⇒ 刷屏）。--repush 才无视去重重推。
        done = set() if args.repush else already_pushed(conn, moves)
        new_moves = [m for m in moves
                     if (m["match_id"], m["option_code"], m["prev_ts"], m["curr_ts"]) not in done]
        new_groups = group_by_match(new_moves)
        if done:
            logger.info("odds_move: 跳过已推过的 %d 侧（剩 %d 侧待推）", len(done), len(new_moves))
        pushed = {"pushed": False, "reason": "skipped_by_flag"}
        if not args.no_push and new_groups:
            pushed = push_groups(new_groups, args.threshold)
            if pushed.get("pushed"):
                # ⚠️ 必须**逐条精确标记**（场次+侧+前后两版）—— 见 mark_pushed 的注释。
                n_marked = mark_pushed(conn, new_groups)
                logger.info("odds_move: 标记 pushed %d 条", n_marked)
        elif not args.no_push:
            pushed = {"pushed": False, "reason": "no_new_moves"}
        logger.info("odds_move: 落表 %d 行 / 推送 %s", n_written, pushed)
    return 0


if __name__ == "__main__":
    # ⚠️ 这个入口不能少：少了它，直接跑本文件会**静默什么都不做**（退出码 0、零输出、零写入），
    # 看起来像「跑了但没数据」—— 建议 3 首跑就踩过这个坑。
    raise SystemExit(main())
