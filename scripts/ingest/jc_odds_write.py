"""契约 v1.4 盘口全历史写手（P0-COLLECT3b2）：fact.jc_odds_history 逐版 upsert。
调用方持有连接与事务；PG 事务内一条 SQL 失败即 aborted 整事务 ⇒ 逐行裸 SQL savepoint
（psycopg3 无 savepoint 关键字参数）：坏行只回滚到 savepoint，
外层事务与已成功的行存活，单条坏行不炸整批（每天 144 趟）。"""
from __future__ import annotations

from core.log import logger
from psycopg.types.json import Json
from store.parse_odds_hist import parse_env

_SQL = ("insert into fact.jc_odds_history "
        "(match_id,play_type,update_ts,seq_no,goal_line,goal_line_value,odds,is_first,is_close,league_id,"
        "src_hash,src_file) values "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " on conflict (match_id,play_type,update_ts) do update set "
        "seq_no=excluded.seq_no, goal_line=excluded.goal_line, goal_line_value=excluded.goal_line_value, "
        "odds=excluded.odds, is_first=excluded.is_first, is_close=excluded.is_close, "
        "league_id=excluded.league_id, src_hash=excluded.src_hash, src_file=excluded.src_file, "
        "last_seen_at=now()")


def upsert_jc_odds_history(cur, rows: list[dict], src: dict) -> int:
    """逐行 upsert：返回成功行数合计；坏行记一条 logger.warning('odds-hist-reject …') 后继续下一行。
    first_seen_at 不碰（首次插入靠 DDL default now()，重复装载只刷 last_seen_at）；
    is_close 原样透传（上游恒 False，本层不猜封盘）。"""
    n = 0
    for r in rows:
        params = [r["match_id"], r["play_type"], r["update_ts"], r["seq_no"], r["goal_line"],
                  r["goal_line_value"], Json(r["odds"]), r["is_first"], r["is_close"], r["league_id"],
                  src.get("src_hash", ""), src.get("src_file", "")]
        try:
            cur.execute("savepoint rh")
            cur.execute(_SQL, params)
            n += cur.rowcount or 0
            cur.execute("release savepoint rh")
        except Exception as exc:
            cur.execute("rollback to savepoint rh")
            logger.warning("odds-hist-reject match_id=%s play_type=%s: %s",
                           r.get("match_id"), r.get("play_type"), exc)
    return n


def upsert_from_env(cur, env: dict) -> int:
    """信封（契约 v1.4，topic jc_odds_history）⇒ parse_env 后转调 upsert_jc_odds_history；
    src 取自信封（src_hash 外壳键；src_file 外壳没有 ⇒ ''，装载层 jc_load 补文件路径）。"""
    rows = parse_env(env)
    if not rows:
        return 0
    src = {"src_hash": env.get("src_hash", ""), "src_file": env.get("src_file", "")}
    return upsert_jc_odds_history(cur, rows, src)
