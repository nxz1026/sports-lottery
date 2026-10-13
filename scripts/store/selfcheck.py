"""自审读法：双源覆盖 / FD 对齐明细（两函数只发 SELECT；`_rows` 是 store 层唯一的游标
包装，重复一份必然漂移，故 import 而不抄）。`source_coverage`/`unmatched_fd` 的"最新
一条留痕"读法会被任何更新的生产留痕顶掉 ⇒ 调用方要么传 `src_file`/`fd_ids` 限定作用域，
要么自担（OMP-SKILL §9-33：读全局必须能给范围）。
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

from store.query import _rows


def source_coverage(conn: Connection) -> dict[str, Any]:
    """双源覆盖自审：AF 场数 / FD 场数 / 双源重叠场数 / 最近一次 FD 未对齐数（读 ops.ingest_log）。"""
    row = _rows(conn, """
        SELECT count(*) FILTER (WHERE source = 'api_football') AS af_rows,
               count(*) FILTER (WHERE source = 'football_data') AS fd_rows,
               count(DISTINCT fixture_id) FILTER (WHERE n_src = 2) AS both
          FROM (SELECT fixture_id, source, count(*) OVER (PARTITION BY fixture_id) AS n_src
                  FROM fact.fixture_result) s""")[0]
    last = _rows(conn, "SELECT coalesce(jsonb_array_length(rejected), 0) AS unmatched FROM ops.ingest_log"
                       " WHERE topic = 'fd_align' ORDER BY id DESC LIMIT 1")
    return {**row, "fd_unmatched": last[0]["unmatched"] if last else 0}


def unmatched_fd(conn: Connection, limit: int = 20, *, src_file: str | None = None,
                 fd_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """FD 对齐失败明细（ops.ingest_log.topic='fd_align'）的作用域读法，供人工复核；无留痕返回 []。

    src_file 等值过滤留痕来源（生产写 'raw.fd_raw'；调用方可传本批唯一标记）；fd_ids 只保留 rejected 里
    fd_id 落在该集合的条目（= **本批**哨兵 id，测试/回填自证用）。两者都给取交集；**都不给＝最新一条**
    （生产读法：会被任何更新的留痕顶掉，测试与 CLI 汇总不许依赖）。本层只 SELECT，不写 ops.ingest_log。
    """
    where, params = [], []
    where.append("topic = %s")
    params.append("fd_align")
    if src_file is not None:
        where.append("src_file = %s")
        params.append(src_file)
    latest = f"SELECT rejected FROM ops.ingest_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 1"
    if fd_ids is None:  # 生产读法：整条最新留痕
        rows = _rows(conn, latest, tuple(params))
        return list(rows[0]["rejected"] or [])[:limit] if rows else []
    entries = (f"SELECT e.entry FROM ({latest}) s, LATERAL jsonb_array_elements(s.rejected)"
               " WITH ORDINALITY AS e(entry, ord) WHERE e.entry->>'fd_id' = ANY(%s::text[])")
    return [row["entry"] for row in _rows(conn, entries + " ORDER BY e.ord LIMIT %s",
                                          (*params, [str(fid) for fid in fd_ids], limit))]
