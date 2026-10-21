"""建议 5 子项 ①：错位**三分量** —— 决策时错位 / 当前错位 / 盘口漂移。

⚠️ **为什么要拆**（路线图 §6.7.2 ①）：现在的错位榜（`GET /api/jc/gap`）算的是

    gap = TV/2( 官方隐含概率@**此刻** , 模型 p@**09:30** )

⇒ 把两件不同的事混在了一起：
- **真实错位** = 模型在**决策时刻**对盘口的判断偏差（决策当时就算出来了，**是固定的**）；
- **盘口漂移** = 决策之后盘口自己走了多少（**随时间变**）。

⇒ 混在一起的数字既不是「模型的错」，也不是「价格的错」，**没法解释、也没法校准**
（这正是建议 4 实测出的负 ROI —— EV 里混着跨市场定价差 —— 无法归因的根因）。

本模块给出三个量（同一场、同一份模型概率 `p_model`）：

| 量 | 定义 | 性质 |
|---|---|---|
| `gap_decide` | TV/2(决策时盘口去水概率, p_model) | **固定**（历史量，决策时就算出来了） |
| `gap_now` | TV/2(当前盘口去水概率, p_model) | **滚动**（随盘口变） |
| `drift` | 当前去水概率 − 决策时去水概率（逐侧，pp） | **滚动**（盘口漂了多少） |

**「决策时盘口」怎么取**：`fact.jc_offer` 里 `snap_ts <= 归档 run_at` 的**最后一版三项齐全的 had**
⇒ 与建议 4 的定价口径**完全一致**（同一条纪律：宁可这一场没有，也不拿半个选项硬凑）。
⚠️ 必须取「**三项齐全**」的那一版：实测 406 场里 **405 场的第 1 版 `options` 是 `{}`**
（竞彩刚开盘时 had 还没值）⇒ 沿用「第一版快照」会算不出任何东西。

**`p_model` 取模型自己的归档概率**（`model.pred_archive_match.p_home/p_draw/p_away`，
`jc_match_id` **精确匹配**）⇒ 比现有错位榜的「按名 join 预测文件」更可靠，
且**不用**顶层 `*_true_prob`（那是市场先验，用它算错位是循环论证 —— 与建议 3 同款纪律）。

**去水口径**：`market.devig.safe_devig`（与 CLV 计算同款）⇒ 错位是「去水后」比，
不受竞彩水位影响（⚠️ 水位本身是建议 4 的结论：单边投注要扛 ~25~33%）。

**不落表**：这三个量随盘口滚动变，落表会与「实时」打架 ⇒ **查询时实时算**
（`analysis.odds_move` 才是落表的资产 —— 异动事件）。
"""
from __future__ import annotations

from typing import Any

from core.log import logger
from psycopg import Connection
from store import pg

# 竞彩 had 三项 ↔ 模型概率键 / 推荐侧中文
_SIDES = ("h", "d", "a")
_SIDE_CN = {"h": "主胜", "d": "平", "a": "客胜"}


def _to_float(v: Any) -> float | None:
    """竞彩 SP 是字符串（'1.86'）⇒ 转 float；非法/≤1 ⇒ None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 1.0 else None


def tv2(p_official: dict[str, float], p_model: dict[str, float]) -> float:
    """总变差距离的一半（与现有错位榜同款口径）：``sum(|官方 − 模型|) / 2``。"""
    total = 0.0
    for code in _SIDES:
        a = p_official.get(code)
        b = p_model.get(code)
        if a is None or b is None:
            continue
        total += abs(float(a) - float(b))
    return total / 2.0


def devig(options: dict[str, Any] | None) -> dict[str, float] | None:
    """竞彩 had 三项 → 去水概率；三项不齐/非法 ⇒ None（**不猜、不用半个选项**）。"""
    options = options or {}
    raw = {code: _to_float(options.get(code)) for code in _SIDES}
    if any(v is None for v in raw.values()):
        return None
    from market.devig import safe_devig

    probs = safe_devig({k: v for k, v in raw.items() if v is not None})
    if not probs or len(probs) < 2:
        return None
    return {code: float(probs[code]) for code in _SIDES if code in probs}


# 每场取「最新一次归档」+ 决策时盘口 + 当前盘口
_GAPS_SQL = """
SELECT src.jc_match_id, m.match_num, src.p_home, src.p_draw, src.p_away, src.match_date,
       src.league_key, src.run_at, src.archive_id,
       d.options AS decide_opts, d.snap_ts AS decide_ts,
       n.options AS now_opts, n.snap_ts AS now_ts
  FROM (
    SELECT DISTINCT ON (am.jc_match_id)
           am.jc_match_id, am.p_home, am.p_draw, am.p_away, am.match_date,
           ar.league_key, ar.run_at, ar.archive_id
      FROM model.pred_archive_match am
      JOIN model.pred_archive ar ON ar.archive_id = am.archive_id
     WHERE am.jc_match_id IS NOT NULL
       AND am.p_home IS NOT NULL AND am.p_draw IS NOT NULL AND am.p_away IS NOT NULL
     ORDER BY am.jc_match_id, ar.run_at DESC
  ) src
  JOIN fact.jc_match m ON m.match_id = src.jc_match_id
  LEFT JOIN LATERAL (
    SELECT o.options, o.snap_ts FROM fact.jc_offer o
     WHERE o.match_id = src.jc_match_id AND o.play_type = 'had'
       AND o.snap_ts <= src.run_at
       AND (o.options->>'h') IS NOT NULL
       AND (o.options->>'d') IS NOT NULL
       AND (o.options->>'a') IS NOT NULL
     ORDER BY o.snap_ts DESC LIMIT 1
  ) d ON TRUE
  LEFT JOIN LATERAL (
    SELECT o.options, o.snap_ts FROM fact.jc_offer o
     WHERE o.match_id = src.jc_match_id AND o.play_type = 'had'
       AND (o.options->>'h') IS NOT NULL
       AND (o.options->>'d') IS NOT NULL
       AND (o.options->>'a') IS NOT NULL
     ORDER BY o.snap_ts DESC LIMIT 1
  ) n ON TRUE
 WHERE (%s::int[] IS NULL OR m.match_num = ANY(%s::int[]))
 ORDER BY src.jc_match_id
"""


def compute_gaps(conn: Connection, *, league_key: str | None = None,
                 match_nums: list[int] | None = None) -> list[dict[str, Any]]:
    """算每场的错位三分量（**实时**，不落表）。

    缺「决策时盘口」⇒ ``gap_decide=None``、``drift_pp=None``（**不编数**）；
    缺「当前盘口」⇒ 该场不返回（连当前错位都算不出来）。

    ``match_nums`` 给了就只看这些场次（web 层只查榜上那几场 ⇒ 不做全表扫描）。
    """
    rows = conn.execute(_GAPS_SQL, (match_nums, match_nums)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        (jc_match_id, match_num, p_home, p_draw, p_away, match_date, league, run_at, archive_id,
         decide_opts, decide_ts, now_opts, now_ts) = r
        if league_key and league != league_key:
            continue
        p_now = devig(now_opts)
        if p_now is None:
            continue                      # 连当前价都没有 ⇒ 这一场给不出任何错位
        p_model = {"h": float(p_home), "d": float(p_draw), "a": float(p_away)}
        p_decide = devig(decide_opts)
        gap_now = tv2(p_now, p_model)
        gap_decide = tv2(p_decide, p_model) if p_decide is not None else None
        drift = None
        if p_decide is not None:
            drift = {code: round((p_now[code] - p_decide[code]) * 100.0, 4) for code in _SIDES}
        worst = max(_SIDES, key=lambda c: abs(p_now[c] - p_model[c]))
        out.append({
            "match_id": int(jc_match_id),
            "match_num": match_num,
            "league_key": league,
            "match_date": str(match_date) if match_date else None,
            "archive_id": archive_id,
            "run_at": run_at,
            "p_model": {k: round(v, 6) for k, v in p_model.items()},
            "p_decide": ({k: round(v, 6) for k, v in p_decide.items()} if p_decide else None),
            "p_now": {k: round(v, 6) for k, v in p_now.items()},
            "gap_decide": round(gap_decide, 6) if gap_decide is not None else None,
            "gap_now": round(gap_now, 6),
            "drift_pp": drift,
            "decide_ts": decide_ts,
            "now_ts": now_ts,
            "worst_side": _SIDE_CN[worst],
        })
    return out


def summarize(gaps: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总（**n 不足不给均值** —— 与建议 3/4 同一纪律：宁可空白不误导）。"""
    n = len(gaps)
    n_decide = sum(1 for g in gaps if g.get("gap_decide") is not None)
    return {
        "n_matches": n,
        "n_with_decide": n_decide,
        "n_gap_now": n,
        "insufficient": n < 5,
        "min_n": 5,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI：打印错位三分量（调试 / 验收用）。"""
    import argparse

    ap = argparse.ArgumentParser(description="错位三分量：决策时错位 / 当前错位 / 盘口漂移")
    ap.add_argument("--league", help="只打印某个联赛（如 epl / ucl）")
    args = ap.parse_args(argv)

    with pg.read_conn("ro") as conn:
        gaps = compute_gaps(conn, league_key=args.league)
    logger.info("gap_calc: %s", summarize(gaps))
    for g in gaps[:30]:
        logger.info("  · %s 场次 %d gap_decide=%s gap_now=%.4f drift=%s",
                    g.get("league_key") or "-", g["match_id"],
                    ("%.4f" % g["gap_decide"]) if g["gap_decide"] is not None else "—",
                    g["gap_now"], g.get("drift_pp"))
    return 0


if __name__ == "__main__":
    # ⚠️ 入口不能少：少了它，直接跑本文件会静默什么都不做（建议 3 首跑踩过这个坑）。
    raise SystemExit(main())
