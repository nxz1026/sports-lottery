"""raw 落地块 → ref.team + fact.fixture：**只有 AF 建 fixture 行**，FD 只把 id 合进 source_ids（P0-FACT1 曾把 3504
条 FD 场次各插一行 ⇒ 同一批比赛两份副本、回测样本翻倍，已清洗；fact/ref 无 DELETE ⇒ 宁可不插不可插错）。FD 行过
align 对齐（(league_key, 开球时刻) 精确等值 + 队名规范化）命中才 UPDATE source_ids，未命中只计数（明细由
upsert_results 落 ops.ingest_log.rejected）；只认 200，403 绝不当数据。**认队按源 id 不按名字**（规则与 SQL 见
store.team_identity）：AF 跨季改拼写不再裂行、name_cn 只做展示名。事务归调用方。"""
from __future__ import annotations

from typing import Any

from core.log import logger
from psycopg import Connection, sql
from psycopg.types.json import Jsonb

from store import align
from store.parse_api import af_fixtures, fd_fixtures
from store.team_identity import merge_fd_teams, resolve_team

BLOCK_TABLES = ("af_raw", "fd_raw")  # raw 两块：AF 是权威锚，FD 只做 id 合并
FIXTURE_SQL = (
    "INSERT INTO fact.fixture (fixture_id, league_key, season, round, kickoff_at, home_team_id, away_team_id,"
    " status, venue, source_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (fixture_id) DO UPDATE SET league_key = EXCLUDED.league_key, season = EXCLUDED.season,"
    " round = EXCLUDED.round, kickoff_at = EXCLUDED.kickoff_at, home_team_id = EXCLUDED.home_team_id,"
    " away_team_id = EXCLUDED.away_team_id, venue = COALESCE(EXCLUDED.venue, fact.fixture.venue),"
    " status = CASE WHEN EXCLUDED.status = 'unknown' THEN fact.fixture.status ELSE EXCLUDED.status END,"
    " source_ids = fact.fixture.source_ids || EXCLUDED.source_ids, updated_at = now()")
FD_ID_SQL = ("UPDATE fact.fixture SET source_ids = source_ids || jsonb_build_object('football_data', %s::bigint),"
             " updated_at = now() WHERE fixture_id = %s")


def league_maps(conn: Connection) -> tuple[dict[int, str], dict[str, str]]:
    """ref.league.ids → ({AF league id: key}, {FD competition code: key})；联赛真相只在 DB，不复制第二份。"""
    rows = conn.execute("SELECT league_key, ids->>'api_football', ids->>'football_data' FROM ref.league").fetchall()
    return ({int(af): key for key, af, _ in rows if af}, {fd: key for key, _, fd in rows if fd})


def read_blocks(conn: Connection, table: str, params_hashes: list[str] | None = None) -> list[dict]:
    """取该块表 http_status=200 的 body；params_hashes 非空时只取这些块（重跑/测试用）。"""
    if table not in BLOCK_TABLES: raise ValueError(f"unknown raw block table {table!r}; expected af_raw/fd_raw")
    tail = " AND params_hash = ANY(%s)" if params_hashes else ""
    pattern = "SELECT body FROM raw.{} WHERE http_status = 200" + tail + " ORDER BY id"
    query = sql.SQL(pattern).format(sql.Identifier(table))
    return [body for (body,) in conn.execute(query, (params_hashes,) if params_hashes else ()).fetchall()]


def af_anchors(conn: Connection, params_hashes: list[str] | None = None):
    """AF 200 块 → (FD 联赛映射, 能落库的行, 被筛掉的行, align 索引, {fid: (主队, 客队)})；"能落库"= league_key
    在 ref.league 且 season 不缺 —— 判据只此一份，FD 也只许对齐到能落库的行。
    """
    af_map, fd_map = league_maps(conn)
    known = set(af_map.values()) | set(fd_map.values())
    rows = [row for body in read_blocks(conn, "af_raw", params_hashes) for row in af_fixtures(body, af_map)]
    kept = [row for row in rows if row["league_key"] in known and row["season"] is not None]
    dropped = [row for row in rows if row["league_key"] not in known or row["season"] is None]
    return fd_map, kept, dropped, align.af_index(kept), align.af_names(kept)


def _store(cur, row: dict) -> None:
    """一行 AF：两队认队（af_id 优先）→ fact.fixture upsert（source_ids 只写 AF 自己的 id）。"""
    home, away = row["af_team_id"] or (None, None)
    cur.execute(FIXTURE_SQL, (row["fixture_id"], row["league_key"], row["season"], row["round"], row["kickoff_at"],
                              resolve_team(cur, row["home_name"], home), resolve_team(cur, row["away_name"], away),
                              row["status"], row["venue"], Jsonb({"api_football": row["fixture_id"]})))


def _merge_fd(cur, row: dict, index: dict, names: dict) -> tuple[bool, int]:
    """FD 行：命中就把 football_data id 并进该 AF fixture 的 source_ids 与其两队 aliases；返回 (是否真合并,
    有几个 FD 队 id 被**别的行**占着没能并入 —— 跨键重复行（同俱乐部两行）的信号）。"""
    fixture_id, reason = align.match_fd(row, index, names)
    if reason not in align.OK_REASONS:  # ok_synonym（走专名同义表）也算命中
        return False, 0
    cur.execute(FD_ID_SQL, (row["fixture_id"], int(fixture_id)))
    if not cur.rowcount:
        return False, 0
    return True, merge_fd_teams(cur, int(fixture_id), row["fd_team_id"])


def run(conn: Connection, params_hashes: list[str] | None = None) -> dict[str, Any]:
    """raw 的 200 块 → AF 落 ref.team/fact.fixture、FD 合并 source_ids；返回逐项统计 dict。"""
    fd_map, kept, dropped, index, names = af_anchors(conn, params_hashes)
    stats: dict[str, Any] = {"rows": len(kept) + len(dropped), "fd_merged": 0, "fd_unmatched": 0, "fd_id_taken": 0}
    with conn.cursor() as cur:
        for row in kept:
            _store(cur, row)
        for body in read_blocks(conn, "fd_raw", params_hashes):
            for row in fd_fixtures(body, fd_map):
                stats["rows"] += 1
                merged, taken = _merge_fd(cur, row, index, names)
                stats["fd_merged" if merged else "fd_unmatched"] += 1
                stats["fd_id_taken"] += taken
    unknown = sorted(map(str, {row["league_key"] for row in dropped}))
    if dropped:
        logger.warning(f"[fact.fixture] 跳过 {len(dropped)} 行：league_key 不在 ref.league 或 season 缺失 {unknown}")
    if stats["fd_id_taken"]:
        logger.warning(f"[ref.team] {stats['fd_id_taken']} 个 FD 队 id 已被**别的行**占着 ⇒ 未并入：同一家俱乐部在"
                       " ref.team 有两个行（一行只有 af_id、一行只有 fd_id），属 DDL 侧去重，代码不猜")
    logger.info(f"[fact.fixture] AF 解析 {len(kept) + len(dropped)} 行 → 写入 {len(kept)} 行；FD 对齐合并"
                f" {stats['fd_merged']} 场、未对齐 {stats['fd_unmatched']} 场（不建 fixture 行）")
    return {**stats, "fixtures": len(kept), "skipped": len(dropped), "unknown_leagues": unknown}
