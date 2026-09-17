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

from psycopg.rows import dict_row

from core.log import logger
from store import pg

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


def fixtures_on(day: str | None) -> list[dict[str, Any]]:
    """场次 × 玩法一行（一场 5 行）；盘口取各玩法最新一版；day 缺省 → 最新 business_date。"""
    if day is not None:
        try:
            date.fromisoformat(day)
        except ValueError:
            return []
        return _fetch(_FIX_SQL.format(DAY=_DAY_BY), {"day": day})
    return _fetch(_FIX_SQL.format(DAY=_DAY_LATEST))


def issues(limit: int = 20) -> list[dict[str, Any]]:
    """传统足彩期次 + 开奖；limit 由调用方保证 ≤200，本层仅钳制到 [1, 200]。"""
    limit = max(1, min(int(limit), 200))
    return _fetch(_ISSUE_SQL, {"limit": limit})


def backtest_summary() -> list[dict[str, Any]]:
    """多分类 Brier / argmax 命中率的基线口径聚合，每玩法一行（与已发布基线一致）。"""
    return _fetch(_BT_SQL)
