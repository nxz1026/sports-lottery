"""P0-DASH1a：看板 store 层只读视图（league_ro 仅 SELECT；任何异常都降级为 []）。

口径：
  · fixtures_on：jc_match × 各玩法最新一版 jc_offer 快照（lateral distinct on）；不给 day 取最新 business_date；
  · issues：jc_issue left join jc_issue_draw（开奖侧可能尚无数据，空即正确）；
  · backtest_summary：analysis.backtest_market 是逐选项明细（175200 行），本层按基线口径聚合：
    先按 (场, 玩法) 对全部选项加总平方误差（多分类 Brier），再对场取平均。

⚠️ 时区口径（2026-09-17 修正）：
  fact.jc_match.kickoff_bj / fact.jc_issue.sale_begin|sale_end|draw_at / fact.jc_offer.odds_update
  都是 `timestamp without time zone`，**列里存的已经是北京时间**（schema 命名即 bj）。
  因此只能直接 to_char，绝不能写 `at time zone 'Asia/Shanghai'` —— 那会把无时区的墙上时间
  先当成北京时间转成 timestamptz，再由 to_char 按会话时区（本机 = Etc/UTC）渲染，结果早 8 小时
  （实测 kickoff_bj 2026-09-18 18:30 被渲染成 "09-18 10:30"，影响全部 51 场）。
  对照：fact.jc_offer.snap_ts / jc_odds_history.update_ts 是 `timestamptz`，那才是真 UTC 瞬时值。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.log import logger
from market.devig import safe_devig
from psycopg.rows import dict_row

from store import pg


def _nums(o: dict) -> dict:
    """h/d/a 温和提取：不存在或非数 → None。"""
    out = {}
    for k in ("h", "d", "a"):
        try:
            v = float(o[k])
        except (KeyError, TypeError, ValueError):
            v = None
        out[k] = v
    return out


def _implied(nums: dict) -> dict:
    """盘口赔率 → 归一化隐含概率（3 位小数）。

    P5-2 迁移：lenient 过滤非法项 + 单边退化逻辑收口到 ``market.devig.safe_devig``。
    本函数只负责 3 位小数 round（clv_baseline 走 ``safe_devig`` 不 round）。

    退化情形（仅 1 项有效）保持旧行为：返 ``{key: 1.0}``（单边胜率 100%），
    与历史 ABI 兼容。
    """
    probs = safe_devig({k: v for k, v in nums.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)},
                       allow_single=True)
    return {k: round(probs[k], 3) for k in probs} if probs else {}

_FIX_SQL = """select m.match_id, m.match_num, m.league_cn, m.home_cn, m.away_cn, m.match_status,
        to_char(m.kickoff_bj, 'MM-DD HH24:MI') as kickoff_bj,
        o.play_type, o.snap_ts, o.goal_line, o.options
   from fact.jc_match m
   left join lateral (
         select distinct on (q.match_id, q.play_type) q.play_type, q.snap_ts, q.goal_line, q.options
           from fact.jc_offer q
          where q.match_id = m.match_id
          order by q.match_id, q.play_type, q.snap_ts desc
   ) o on true
  where 1 = 1 {DAY}
  order by m.kickoff_bj nulls last, m.match_id, o.play_type"""
_DAY_BY = "and m.business_date = %(day)s"
_DAY_LATEST = "and m.business_date = (select max(business_date) from fact.jc_match)"

_ISSUE_SQL = """select i.game_num, i.issue_no, i.game_name, i.sale_begin, i.sale_end, i.draw_at, i.n_matches,
        (i.raw_head ->> 'head_source') as head_source,
        d.draw_result, d.pool_after, d.sales, d.pool_after_rj, d.sales_rj, d.is_delay
   from fact.jc_issue i
   left join fact.jc_issue_draw d on d.game_num = i.game_num and d.issue_no = i.issue_no
  order by i.draw_at desc nulls last, i.game_num, i.issue_no
  limit %(limit)s"""

_BT_SQL = """select play_type, count(*) as n_fp,
       round(avg(bs)::numeric, 4) as brier,
       round(avg(us)::numeric, 4) as brier_uniform,
       round(avg(ll)::numeric, 4) as log_loss,
       round(avg(hit::int)::numeric, 4) as acc,
       round(avg(hit::int)::numeric, 4) as hit_rate,
       count(*) filter (where if_clv) as clv_filled
  from (select play_type, fixture_id,
               sum(power(p_pred - outcome, 2)) as bs,
                -ln(greatest(max(p_pred) filter (where outcome = 1), 1e-6)) as ll,
               sum(power((1.0 / cnt) - outcome, 2)) as us,
               bool_or(outcome = 1 and p_pred = mx) as hit,
               count(clv) > 0 as if_clv
          from (select b.*, count(*) over (partition by b.fixture_id, b.play_type) as cnt,
                       max(b.p_pred) over (partition by b.fixture_id, b.play_type) as mx
                  from analysis.backtest_market b) x
         group by play_type, fixture_id) y
 group by play_type
 order by play_type"""


_LOTTERY_SQL = """select game_num, game_name, issue_no, draw_date, status,
                         numbers_raw, pool, equipment_count
                    from (select *, row_number() over (partition by game_num
                                                       order by issue_no desc) as rn
                            from fact.lottery_draw) t
                   where rn <= %(per_type)s
                   order by game_num, issue_no desc"""

_ISSUE_MATCH_SQL = """select i.game_num, i.issue_no, m.seq, m.gm_match_id, m.league_cn,
        m.home_cn, m.away_cn, m.start_date, m.is_drawn, m.cz_score, m.official_result
   from fact.jc_issue i
   join fact.jc_issue_match m on m.game_num = i.game_num and m.issue_no = i.issue_no
  where i.game_num = %(game_num)s and i.issue_no = %(issue_no)s
  order by m.seq"""


def _fetch(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """单次只读查询：ro 连接 SELECT-only；任何异常 → 记日志返回 []（页面降级但不 5xx）。"""
    try:
        with pg.read_conn("ro") as conn:
            with conn.cursor() as cur:
                cur.row_factory = dict_row
                cur.execute(sql, params or {})
                return [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        logger.exception("jc_view 只读查询失败，降级返回空: %s", sql.splitlines()[0])
        logger.error("jc_view 面板降级 sql=%s err=%s", sql.splitlines()[0], type(exc).__name__)
        return []


# R18 #6 (重提 R17 #6)：latest business_date 60s LRU 缓存。
# 原设计用 SQL 子查询 ``(select max(business_date) from fact.jc_match)`` 嵌入
# 主 SQL，每次 fixtures_on / jc_movement 触发 2~4 次 max 子查询。看板页面每晚刷新
# 5~10 分钟一次，60s 缓存命中率 ≥ 80%，省 ~5×min 子查询/worker。
_LATEST_BD_CACHE: dict = {"value": None, "expires_at": 0.0}
_LATEST_BD_TTL = 60.0  # seconds


def _latest_business_date() -> str | None:
    """返回 fact.jc_match 的最新 business_date（ISO 'YYYY-MM-DD' 字符串）。

    60s LRU 缓存（仅进程内，PG max 子查询本身 sub-ms 但省 5x 重复 IO + round-trip）。
    失败时返回 None（调用方退化为空查询 = 0 行）。
    """
    import time as _time
    now = _time.monotonic()
    cached = _LATEST_BD_CACHE
    if cached["value"] is not None and now < cached["expires_at"]:
        return cached["value"]
    try:
        with pg.read_conn("ro") as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT max(business_date)::text FROM fact.jc_match")
                row = cur.fetchone()
        if row and row[0]:
            value = str(row[0])
            cached["value"] = value
            cached["expires_at"] = now + _LATEST_BD_TTL
            return value
    except Exception:
        pass
    return None


def fixtures_on(day: str | None) -> list[dict[str, Any]]:
    """场次 × 玩法一行（一场 5 行）；盘口取各玩法最新一版；day 缺省 → 最新 business_date。"""
    if day is not None:
        try:
            date.fromisoformat(day)
        except ValueError:
            return []
        return _fetch(_FIX_SQL.format(DAY=_DAY_BY), {"day": day})
    # R18 #6: 用缓存的最新 business_date 替换 max 子查询（60s LRU）
    latest = _latest_business_date()
    if not latest:
        return []
    return _fetch(_FIX_SQL.format(DAY=_DAY_BY), {"day": latest})


def issues(limit: int = 20) -> list[dict[str, Any]]:
    """传统足彩期次 + 开奖；limit 由调用方保证 ≤200，本层仅钳制到 [1, 200]。"""
    limit = max(1, min(int(limit), 200))
    return _fetch(_ISSUE_SQL, {"limit": limit})


def issue_matches(game_num: str = "90", issue_no: str | None = None) -> list[dict[str, Any]]:
    """某一传统足彩期的逐场对阵（fact.jc_issue_match 一式）；issue_no 缺省 → 最新一期。

    返回 [{game_num, issue_no, seq, gm_match_id, league_cn, home_cn, away_cn, start_date,
           is_drawn, cz_score, official_result}]；无数据返回 []。
    """
    if issue_no is None:
        row = _fetch("select i.issue_no from fact.jc_issue i where i.game_num = %(g)s "
                     "order by i.draw_at desc nulls last, i.issue_no desc limit 1", {"g": game_num})
        if not row:
            return []
        issue_no = row[0]["issue_no"]
    return _fetch(_ISSUE_MATCH_SQL, {"game_num": game_num, "issue_no": issue_no})


def backtest_summary() -> list[dict[str, Any]]:
    """多分类 Brier / argmax 命中率的基线口径聚合，每玩法一行（与已发布基线一致）。"""
    return _fetch(_BT_SQL)


def lottery_draws(per_type: int = 20) -> list[dict[str, Any]]:
    """各彩种最近 per_type 期开奖，一行 = 一个彩种的一期。

    彩种来自采集端 lottery_draw 主题：超级大乐透(85)、排列3(35)、排列5(350133)、
    7星彩(04)；解析层无白名单，新增彩种只需采集端补采。draw_date 是 date 类型，
    不带时区，无需 to_char 转换（对比 fact.jc_match.kickoff_bj 的北京时间墙钟规则）。
    """
    per_type = max(1, min(int(per_type), 100))
    return _fetch(_LOTTERY_SQL, {"per_type": per_type})


_MOV_SQL_DAY = """
   select m.match_num, m.league_cn, m.home_cn, m.away_cn,
          to_char(m.kickoff_bj, 'MM-DD HH24:MI') as kickoff_bj,
          o.play_type, o.snap_ts, o.goal_line, o.options
     from fact.jc_offer o
     join fact.jc_match m using (match_id)
    where m.business_date = %(day)s
    order by m.match_num, o.play_type, o.snap_ts, o.match_id"""

_MOV_SQL_LATEST = _MOV_SQL_DAY.replace(
    "m.business_date = %(day)s",
    "m.business_date = (select max(business_date) from fact.jc_match)")

# 建议 5 ④：窗口口径（**可选参数**，不改变老端点的默认行为）——
# 「只看最新一期」⇒ 刚开盘的场次只有 1 个快照版本 ⇒ 永远算不出 delta
# （实测：今日在售 11 场每场 versions=1）。窗口口径让新开盘场次随采集累积。
_MOV_SQL_WINDOW = """
   select m.match_num, m.league_cn, m.home_cn, m.away_cn,
          to_char(m.kickoff_bj, 'MM-DD HH24:MI') as kickoff_bj,
          o.play_type, o.snap_ts, o.goal_line, o.options
     from fact.jc_offer o
     join fact.jc_match m using (match_id)
    where m.business_date >= (select max(business_date) from fact.jc_match) - %(window_days)s::int
    order by m.match_num, o.play_type, o.snap_ts, o.match_id"""


def _has_full_had(options: Any) -> bool:
    """该版快照的 had 三项是否齐全（>1 且可转 float）。

    ⚠️ 实测：406 场「版本数 ≥ 5」里 **405 场的第 1 版 `options` 是 `{}`**
    （竞彩刚开盘时 had 还没值）⇒ 「开盘价」若取第一版快照，delta 会全是 None。
    """
    nums = _nums(options or {})
    return all(v is not None and v > 1 for v in nums.values())


def _has_any_side(options: Any) -> bool:
    """该版快照是否**至少有一侧**有合法赔率（> 1）。

    用途：全场**没有**一版三项齐全的 had 时的退路 —— 单边有效的版本仍可当开盘价
    （老口径：单边隐含概率 100%，与历史 ABI 兼容；缺的侧照旧是 None、不参与 delta）。
    """
    nums = _nums(options or {})
    return any(v is not None and v > 1 for v in nums.values())


def jc_movement(day: str | None = None, window_days: int | None = None) -> list[dict[str, Any]]:
    """两时点盘口快照链：每场每玩法「开盘(first) vs 临场(last)」赔率与隐含概率变动。

    fact.jc_offer 由 10 分钟采集周期累积多时点 snap_ts（开盘/临场都在内）。
    返回每场每玩法一条：开盘/临场时点、逐侧赔率、delta、归一化隐含概率。
    day 缺省 → 最新 business_date；``window_days`` 给定时 → 最近 N 期（**可选，不改老行为**）。
    只读，异常降级 []。

    ⚠️ 建议 5 ③（修 bug）：「开盘(first)」取**第一版三项齐全的 had**，不是「第一版快照」
    —— 实测 405/406 场的第一版 `options` 是 `{}` ⇒ 原实现让 delta 全是 None
    （这就是 `/api/jc/movement` 长期显示「有快照却无变动」的真正原因）。
    """
    if day:
        rows = _fetch(_MOV_SQL_DAY, {"day": day})
    elif window_days:
        wd = max(1, min(int(window_days), 30))
        rows = _fetch(_MOV_SQL_WINDOW, {"window_days": wd})
    else:
        # R18 #6: latest business_date 走 60s LRU 缓存（同 fixtures_on）
        latest = _latest_business_date()
        if not latest:
            return []
        rows = _fetch(_MOV_SQL_DAY, {"day": latest})
    groups: dict = {}

    for r in rows:
        key = (r["match_num"], r["play_type"])
        g = groups.setdefault(key, {"base": r, "snaps": []})
        g["snaps"].append(r)

    out: list[dict[str, Any]] = []
    for (mn, pt), g in groups.items():
        snaps = g["snaps"]
        first = next((s for s in snaps if _has_full_had(s.get("options"))), None)
        if first is None:
            # 全场**没有**一版三项齐全的 had ⇒ 退而取第一版「有有效值」的快照
            # （老口径：单边有效也出行，缺的侧照旧 None、不参与 delta —— 与历史 ABI 兼容）
            first = next((s for s in snaps if _has_any_side(s.get("options"))), None)
        if first is None:
            continue      # 连一个有效赔率都没有 ⇒ 给不出开盘价（不编数）
        last = snaps[-1]
        fo, lo = _nums(first["options"] or {}), _nums(last["options"] or {})
        delta = {k: (round(lo[k] - fo[k], 3) if (fo[k] and lo[k]) else None) for k in fo}
        out.append({
            "match_num": mn, "play_type": pt,
            "league_cn": first.get("league_cn"), "home": first.get("home_cn"),
            "away": first.get("away_cn"), "kickoff": first.get("kickoff_bj"),
            "goal_line": last.get("goal_line"),
            "opening_ts": first["snap_ts"].isoformat(),
            "closing_ts": last["snap_ts"].isoformat(),
            "opening_odds": fo, "closing_odds": lo, "delta": delta,
            "opening_implied": _implied(fo), "closing_implied": _implied(lo),
        })
    return out
