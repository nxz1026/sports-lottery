"""P0-DASH2c：竞彩看板「入库与对照」只读视图（league_ro 仅 SELECT；每查询独立降级）。

四段口径（全部与队长真盘核验一致，2026-09-16 复核）：
  · teams    按联赛聚合**全部**待售 business_date：总场次 / 两队都已对齐 / 两队都带 AF 历史。
             差值即「缺 af_id」——AF 免费档只到 2024 赛季，降级队无历史属预期，不是漏配。
             不只看 max(business_date)：清单跨 3 天，只看最新一天会漏掉 26 场。
  · topics   ops.file_arrival 按 topic：文件数 / 累计行数 / 正常批次 / 无主通道 / 最新到达 + 距今分钟；
             无主通道（done_marker=false）是 loader 每次 run 无条件全量重扫的产物，不是事故。
  · quota    ops.quota_ledger 当日各源 used / cap / 余量（空 = 当日未发请求，非降级）。
  · rejects  ops.ingest_log 近 24h：**只认 rejected jsonb 非空与 ok=false**。
             ⚠️ 不能用 rows_in - rows_ups：重放未变更、按设计跳过（jclq 2e 范围）都会让它变正，
             实测把 lottery_draw 算成 369360 行「被拒」，实为 0。空表 = 全绿，不是降级。
"""
from __future__ import annotations

from typing import Any

from core.log import logger
from psycopg.rows import dict_row

from store import pg

_TEAMS_SQL = """select m.league_cn as league,
        count(*) as n_matches,
        count(distinct m.business_date) as n_days,
        count(*) filter (where m.home_team_id is not null and m.away_team_id is not null) as n_aligned,
        count(*) filter (where m.home_team_id is not null and m.away_team_id is not null
                         and th.af_id is not null and ta.af_id is not null) as n_af_ready
   from fact.jc_match m
   left join ref.team th on th.team_id = m.home_team_id
   left join ref.team ta on ta.team_id = m.away_team_id
  where m.home_sporttery_id is not null or m.away_sporttery_id is not null
  group by m.league_cn
  order by n_aligned desc, n_matches desc, league"""

_TOPICS_SQL = """select topic,
        count(*) as n_files,
        coalesce(sum(rows), 0) as n_rows,
        count(*) filter (where done_marker) as n_marked,
        count(*) filter (where not done_marker) as n_orphan,
        max(arrived_at) as latest_arrival,
        date_part('minute', now() - max(arrived_at)) as min_since_latest
   from ops.file_arrival
  group by topic
  order by min_since_latest desc"""

_QUOTA_SQL = """select source, used, cap, cap - used as remaining
   from ops.quota_ledger
  where day = (now() AT TIME ZONE 'Asia/Shanghai')::date
  order by source"""

_REJECTS_SQL = """select topic,
        count(*) as n_runs,
        count(*) filter (where not ok) as n_failed,
        count(*) filter (where rejected is not null) as n_files_reject,
        coalesce(sum(case when rejected is not null then jsonb_array_length(rejected) else 0 end), 0)
            as n_reject_rows
   from ops.ingest_log
  where at > now() - interval '24 hours'
  group by topic
 having count(*) filter (where not ok) > 0
        or coalesce(sum(case when rejected is not null then jsonb_array_length(rejected) else 0 end), 0) > 0
  order by n_reject_rows desc, n_failed desc"""

_SQLS: dict[str, str] = {
    "teams": _TEAMS_SQL,
    "topics": _TOPICS_SQL,
    "quota": _QUOTA_SQL,
    "rejects": _REJECTS_SQL,
}


def _fetch(sql: str) -> tuple[list[dict[str, Any]], bool]:
    """单次只读查询：ro 连接 SELECT-only；异常 → (空表, False)，页面降级但不 5xx。"""
    try:
        with pg.read_conn("ro") as conn:
            with conn.cursor() as cur:
                cur.row_factory = dict_row
                cur.execute(sql)
                return [dict(row) for row in cur.fetchall()], True
    except Exception as exc:
        logger.error("jc_ops_view 降级 sql=%s err=%s", sql.splitlines()[0], type(exc).__name__)
        return [], False


def ops_snapshot() -> dict[str, Any]:
    """四段一次取齐；每段独立降级，任一段失败不影响其余，ok_* 标记分键可见。"""
    out: dict[str, Any] = {}
    for key, sql in _SQLS.items():
        rows, ok = _fetch(sql)
        out[key] = rows
        out["ok_" + key] = ok
    return out
