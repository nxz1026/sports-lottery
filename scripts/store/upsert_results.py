"""raw 落地块 → fact.fixture_result（每源一行，PK(fixture_id,source)；两源谁也不覆盖谁）。

AF 行：fixture_id 就是 AF 权威 id，直接落。FD 行：先过 align 对齐，命中才写到**对齐后的 AF fixture_id**；
未命中一律不落 fact（FD-only 场次既不建 fixture 也不写赛果），改为往 ops.ingest_log 追加一条 topic='fd_align'、
ok=false 的留痕（rows_in/rows_ups + rejected 明细 = FD id + 两队原名 + utcDate + reason）—— fact/ref 没有
DELETE、插错只能用超级用户洗，所以 rejected 是未对齐场次唯一的人工复核出口。
沿用 upsert_fixtures 的 league_maps/read_blocks/af_anchors：表→源名→解析函数与"哪行算锚点"都只有一份定义。
fixture 缺失（league_key 未知等）时 SELECT ... WHERE EXISTS 直接不落行 → 外键不破。事务归调用方，本模块不 commit。
"""
from __future__ import annotations

from typing import Any

from core.log import logger
from psycopg import Connection
from psycopg.types.json import Jsonb

from store import align
from store.parse_api import af_fixtures, fd_fixtures
from store.upsert_fixtures import af_anchors, league_maps, read_blocks

AF_SOURCE, FD_SOURCE = "api_football", "football_data"
RESULT_SQL = (
    "INSERT INTO fact.fixture_result (fixture_id, source, ft_h, ft_a, ht_h, ht_a, elapsed_ht, status,"
    " confirmed_at, raw) SELECT %(fid)s, %(src)s, %(ft_h)s, %(ft_a)s, %(ht_h)s, %(ht_a)s, %(elapsed)s,"
    " %(status)s, now(), %(raw)s WHERE EXISTS (SELECT 1 FROM fact.fixture WHERE fixture_id = %(fid)s)"
    " ON CONFLICT (fixture_id, source) DO UPDATE SET ft_h = EXCLUDED.ft_h, ft_a = EXCLUDED.ft_a,"
    " ht_h = EXCLUDED.ht_h, ht_a = EXCLUDED.ht_a, elapsed_ht = EXCLUDED.elapsed_ht,"
    " status = EXCLUDED.status, confirmed_at = EXCLUDED.confirmed_at, raw = EXCLUDED.raw")
# 未对齐 FD 的留痕落点：ops.ingest_log 是 append-only 日志，league_ing 只有 INSERT（无 DELETE，改不了）
LOG_SQL = ("INSERT INTO ops.ingest_log (topic, src_file, rows_in, rows_ups, rejected, ok)"
           " VALUES ('fd_align', 'raw.fd_raw', %s, %s, %s, false)")


def _params(row: dict, source: str, fixture_id: int) -> dict[str, Any]:
    return {"fid": fixture_id, "src": source, "ft_h": row["ft_h"], "ft_a": row["ft_a"], "ht_h": row["ht_h"],
            "ht_a": row["ht_a"], "elapsed": row["elapsed_ht"], "status": row["status"],
            "raw": Jsonb(row["raw_node"])}


def _reject(row: dict, reason: str) -> dict[str, Any]:
    """未对齐 FD 行的明细：源 id + 两队原名 + 开球时刻 + reason，人工复核时不必回头翻 raw 块。"""
    return {"fd_id": row["fixture_id"], "home": row["home_name"], "away": row["away_name"],
            "utc_date": row["kickoff_at"], "reason": reason}


def _af_rows(cur, bodies: list[dict], leagues: dict) -> dict[str, Any]:
    """AF 块 → fact.fixture_result（fixture_id 就是 AF id）：返回 {rows, results, orphans}。"""
    stats: dict[str, Any] = {"rows": 0, "results": 0, "orphans": 0}
    for body in bodies:
        for row in af_fixtures(body, leagues):
            stats["rows"] += 1
            cur.execute(RESULT_SQL, _params(row, AF_SOURCE, row["fixture_id"]))
            stats["results" if cur.rowcount else "orphans"] += 1
    return stats


def _fd_rows(cur, bodies: list[dict], leagues: dict, index: dict, names: dict) -> dict[str, Any]:
    """FD 块 → 对齐命中写落在 AF fixture_id 下，未命中只进 rejected（fact 一行不落）。"""
    stats: dict[str, Any] = {"rows": 0, "results": 0, "orphans": 0, "unmatched": 0, "rejected": []}
    for body in bodies:
        for row in fd_fixtures(body, leagues):
            stats["rows"] += 1
            fixture_id, reason = align.match_fd(row, index, names)
            if reason not in align.OK_REASONS:  # ok_synonym（走专名同义表）也算命中
                stats["unmatched"] += 1
                stats["rejected"].append(_reject(row, reason))
                continue
            cur.execute(RESULT_SQL, _params(row, FD_SOURCE, int(fixture_id)))
            stats["results" if cur.rowcount else "orphans"] += 1
    return stats


def run(conn: Connection, params_hashes: list[str] | None = None) -> dict[str, Any]:
    """raw 的 200 块 → fact.fixture_result；未对齐 FD 明细写 ops.ingest_log（无未对齐则不留痕）。"""
    af_map, fd_map = league_maps(conn)
    _, _, _, index, names = af_anchors(conn, params_hashes)
    with conn.cursor() as cur:
        af_stats = _af_rows(cur, read_blocks(conn, "af_raw", params_hashes), af_map)
        fd_stats = _fd_rows(cur, read_blocks(conn, "fd_raw", params_hashes), fd_map, index, names)
        if fd_stats["rejected"]:
            cur.execute(LOG_SQL, (fd_stats["rows"], fd_stats["results"], Jsonb(fd_stats["rejected"])))
    conflicts = conn.execute("SELECT count(*) FROM fact.v_result_conflict").fetchone()[0]
    logger.warning(f"[fact.fixture_result] AF {af_stats['results']} 行 + FD 对齐 {fd_stats['results']} 行落库；"
                   f"{af_stats['orphans'] + fd_stats['orphans']} 行因 fixture 缺失未落；FD 未对齐"
                   f" {fd_stats['unmatched']} 行 → ops.ingest_log；两源分歧 {conflicts} 场")
    return {"rows": af_stats["rows"] + fd_stats["rows"], "af_results": af_stats["results"],
            "fd_results": fd_stats["results"], "results": af_stats["results"] + fd_stats["results"],
            "orphans": af_stats["orphans"] + fd_stats["orphans"], "unmatched": fd_stats["unmatched"],
            # 与 upsert_fixtures.run 同名同义：两个聚合器的同一量只用一个对外名字（测试不再被迫读库）
            "fd_unmatched": fd_stats["unmatched"], "rejected": fd_stats["rejected"], "conflicts": conflicts}
