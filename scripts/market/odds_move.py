"""建议 5 子项 ②：赔率异动检测 → `analysis.odds_move`（**相邻快照跳变**口径）。

⚠️ **为什么不用「开盘 vs 临场」的首末差**（生产实测，2026-10-21）：

| 口径 | 样本 | ≥5% 命中率 | 评价 |
|---|---|---|---|
| 首末差（开盘 vs 临场） | 406 场（版本数 ≥ 5） | **77%**（314 场） | **纯噪音** —— 几乎每场都「异动」 |
| **相邻 ~10 分钟跳变** | **21,620** 个相邻对 | **0.40%**（86 次） | 可用 ⇒ 144 个采集周期 × 0.40% ≈ **0.6 次/天** |

⇒ 默认阈值 **5%**（可配 3%，命中率 1.23% ⇒ ≈1.8 次/天）。

**相邻对只取间隔 ~10 分钟的**（540~660 秒）：跨批次/跨天的自然间隔（1/7/20/60/120 分钟）
不是「10 分钟内的变盘」⇒ 排除，否则会混进「隔夜重开」这类假异动。

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

# 默认阈值（%）：相邻跳变口径下实测命中 0.40% ⇒ ≈0.6 次/天
DEFAULT_THRESHOLD_PCT = 5.0
# 相邻对的时间间隔窗（秒）：只认「~10 分钟内的变盘」
MIN_GAP_SEC = 540
MAX_GAP_SEC = 660
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
RETURNING move_id
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


def format_alert(groups: list[dict[str, Any]], threshold_pct: float) -> str:
    """告警文案（飞书 text）。

    ⚠️ 必须带上「异动是信息不是信号」这句 —— 路线图 §6.6.5.1 实测：模型概率与竞彩盘口
    不同源，竞彩 had 还有 ~25~33% 的水位 ⇒ 盘口跳变大 **不构成下注理由**。
    """
    lines = [f"【赔率异动告警】阈值 {threshold_pct:g}%（相邻 ~10 分钟跳变）"]
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
    text = format_alert(groups, threshold_pct)
    try:
        res = push_feishu(text)
    except Exception as exc:  # noqa: BLE001 - 推送失败不阻断落库
        return {"pushed": False, "reason": f"push_failed: {type(exc).__name__}"}
    return {"pushed": bool(res.get("ok")), "detail": res}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="赔率异动检测（相邻快照跳变）→ analysis.odds_move")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PCT,
                    help=f"跳变阈值（%%），默认 {DEFAULT_THRESHOLD_PCT:g}")
    ap.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS,
                    help="检测窗口（小时），默认 24")
    ap.add_argument("--no-push", action="store_true", help="只落表，不推送")
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
        pushed = {"pushed": False, "reason": "skipped_by_flag"}
        if not args.no_push and groups:
            pushed = push_groups(groups, args.threshold)
            if pushed.get("pushed"):
                conn.execute(
                    "UPDATE analysis.odds_move SET pushed = true, pushed_at = now(), updated_at = now() "
                    "WHERE match_id = ANY(%s) AND curr_ts = ANY(%s)",
                    (list({g["match_id"] for g in groups}),
                     list({g["curr_ts"] for g in groups})))
        logger.info("odds_move: 落表 %d 行 / 推送 %s", n_written, pushed)
    return 0


if __name__ == "__main__":
    # ⚠️ 这个入口不能少：少了它，直接跑本文件会**静默什么都不做**（退出码 0、零输出、零写入），
    # 看起来像「跑了但没数据」—— 建议 3 首跑就踩过这个坑。
    raise SystemExit(main())
