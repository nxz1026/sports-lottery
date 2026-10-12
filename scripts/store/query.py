"""fact/ref 只读接口（下游 app/ro 角色用；本层只发 SELECT，不做任何写）。用途：票面/模型取数
（fixtures_between、fixture_with_result）、翻译补全待办（unmatched_teams）、认队体检（team_identity_audit）、
回测汇总（backtest_report，读 analysis/model）；conn 由调用方给（store.pg.connect("app"/"ro")）。
自审读法（回填自检/双源覆盖/FD 对齐明细）另见 store/selfcheck.py。
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from store.team_identity import AUDIT_SQL

FIXTURE_COLS = ("fixture_id, league_key, season, round, kickoff_at, home_team_id, away_team_id, status, venue")
RESULT_COLS = "source, ft_h, ft_a, ht_h, ht_a, elapsed_ht, status, confirmed_at"
UNTRANSLATED = "[一-鿿]"  # name_cn 里一个 CJK 都没有 ⇒ 仍是原名（to_cn 未命中），供后续翻译补全


def _rows(conn: Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fixtures_between(conn: Connection, start, end, league_key: str | None = None) -> list[dict[str, Any]]:
    """[start, end) 内的赛程（含未开赛）；league_key=None 时跨联赛。"""
    query = f"SELECT {FIXTURE_COLS} FROM fact.fixture WHERE kickoff_at >= %s AND kickoff_at < %s"
    params: list[Any] = [start, end]
    if league_key:
        query, params = query + " AND league_key = %s", [*params, league_key]
    return _rows(conn, query + " ORDER BY kickoff_at, fixture_id", tuple(params))


def fixture_with_result(conn: Connection, fixture_id: int) -> dict[str, Any] | None:
    """单场：fixture 字段 + "results" 列表（每源一行，含半场）；查无此场返回 None。"""
    fixture = _rows(conn, f"SELECT {FIXTURE_COLS} FROM fact.fixture WHERE fixture_id = %s", (fixture_id,))
    if not fixture:
        return None
    results = _rows(conn, f"SELECT {RESULT_COLS} FROM fact.fixture_result WHERE fixture_id = %s ORDER BY source",
                    (fixture_id,))
    return {**fixture[0], "results": results}


def unmatched_teams(conn: Connection) -> list[dict[str, Any]]:
    """name_cn 至今无中文（= to_cn 未命中）的队伍，供后续翻译补全；只读，不改 ref.team。"""
    return _rows(conn, "SELECT team_id, name_cn, aliases FROM ref.team "
                       "WHERE name_cn !~ %s ORDER BY team_id",
                   (UNTRANSLATED,))


def team_identity_audit(conn: Connection) -> dict[str, Any]:
    """认队体检（只读）：dup_af/dup_fd 是按 (sport, 源 id) 分组的裂行数，**必须恒 0**（team_af_key/team_fd_key
    两个 partial unique 兜底）；orphan=未被任何 fact.fixture 引用的队，可 >0。SQL 恒量在 store.team_identity。"""
    return _rows(conn, AUDIT_SQL)[0]


REPORT_SQL = """
WITH s AS (SELECT b.src_run, b.play_type, b.fixture_id, b.p_pred::float8 AS p, b.outcome, f.league_key,
                  CASE WHEN b.play_type = 'jqc' THEN left(b.option_code, 1) ELSE '' END AS side,
                  coalesce((pf.features -> 'fallback') <> '{"home": "fit", "away": "fit"}'::jsonb,
                           false) AS fb
             FROM analysis.backtest_market b
             JOIN model.pred_fixture pf ON pf.run_id = b.src_run AND pf.fixture_id = b.fixture_id
             JOIN fact.fixture f ON f.fixture_id = b.fixture_id
            WHERE (%s::bigint IS NULL OR b.src_run = %s) AND (%s::text IS NULL OR f.league_key = %s)),
     g AS (SELECT src_run, play_type, fixture_id, side, fb,
                  sum((p - outcome) ^ 2) AS brier,                       -- 多类 Brier：含未中奖项
                  -ln(greatest(max(p) FILTER (WHERE outcome = 1), 1e-6)) AS log_loss,
                  (array_agg(outcome ORDER BY p DESC))[1]::float8 AS hit
             FROM s GROUP BY 1, 2, 3, 4, 5)
SELECT src_run, play_type, fb AS fallback, count(DISTINCT fixture_id) AS n_fixtures,
       avg(brier)::float8 AS brier_mean, avg(log_loss)::float8 AS log_loss_mean,
       avg(hit)::float8 AS hit_rate
  FROM g GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""


def backtest_report(conn: Connection, *, src_run: int | None = None,
                    league_key: str | None = None) -> list[dict[str, Any]]:
    """回测汇总（只读）：按 (run, 玩法, fallback 分组) 出 n_fixtures/brier_mean/log_loss_mean/hit_rate。

    fallback 分组 = `pred_fixture.features->'fallback'` 不是"两队都 fit"（真升班马 vs 拟合队）—— 兜底场若
    明显差于拟合场，说明"联盟平均先验"该换成合并多季训练。jqc 每场两个侧别各自计分（两侧互斥的 4 档），
    故 mean 是"侧别组均值"、n_fixtures 仍按场计。本 SQL 是 model/backtest.py::score_options 的同口径孪生
    （store 不反向依赖 model），eps 1e-6 两边同值，由 tests/test_model_backtest.py 逐值对账防漂移。
    两参数缺省＝全量（§9-33：读全局必须能给范围）；空库/只有兜底场都照常返回（SQL 聚合，无除零）。
    """
    return _rows(conn, REPORT_SQL, (src_run, src_run, league_key, league_key))
