"""fact/ref 只读接口（下游 app/ro 角色用；本层只发 SELECT，不做任何写）。目前仅承担
``backtest_report`` 一份分析层只读接口（读 analysis/model）；conn 由调用方给
（``store.pg.connect("app"/"ro")``）。
自审读法（双源覆盖/FD 对齐明细）见 ``store/selfcheck.py``。
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row


def _rows(conn: Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


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
