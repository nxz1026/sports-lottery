"""P0-JCSPLIT 纯搬家：jc_load.py 的"每 topic 怎么落库"（_ops/load_topic）原样移入；零行为变化。"""
from pathlib import Path

from core.log import logger
from ingest import jc_issue_write, jc_odds_write, jc_write
from ingest.jc_read import read_lines
from psycopg.types.json import Json
from store.parse_collector import parse_line

WRITE = ("jczq_offer", "jczq_result")
ISSUE = ("jc_issue", "jc_issue_result", "lottery_draw")


def _ops(cur, topic: str, rel: str, size: int | None, n: int, ups: int,
         rej: list, gap: bool, done: bool) -> None:
    cur.execute("insert into ops.file_arrival (topic,src_file,bytes,rows,done_marker) values "
                "(%s,%s,%s,%s,%s) on conflict (topic,src_file) do update set bytes=excluded.bytes,"
                "rows=excluded.rows,done_marker=excluded.done_marker",
                (topic, rel, size, n, done))
    cur.execute("insert into ops.ingest_log (topic,src_file,rows_in,rows_ups,rejected,ok) "
                "values (%s,%s,%s,%s,%s,%s)",
                (topic, rel, n, ups, Json(rej) if rej else None, not rej or gap))


def load_topic(cur, root: Path, marker: Path, topic: str, path: Path | None, state: str) -> dict:
    rel = f"{topic}/{path.name}" if path else f"{marker.stem}/{topic}#MISSING"
    lines = read_lines(path) if path and state in ("jsonl", "orphan") else []
    n, ups, rej = len(lines), 0, []
    if topic in WRITE:
        for i, env in enumerate(lines, 1):
            p = parse_line(env)
            if p is None:
                rej.append({"line": i, "reason": "parse_none"})
                continue
            snap, src = env["snap_ts"], {"src_hash": env["src_hash"], "src_file": rel}
            if topic == "jczq_offer":
                # P0-JCTEAM1 热修：原先硬传 None,None ⇒ 每轮 cron 都把 home/away_team_id 覆盖回 NULL，
                # 球队对照回填（docs/db/seed_jc_team_pool_v2.sql）每 10 分钟被冲掉一次。
                # jc_write.resolve_jc_teams 早已写好「sporttery id → ref.team(team_id)」解析却零调用方（死代码）。
                home, away = jc_write.resolve_jc_teams(cur, p["match"].get("home_sporttery_id"),
                                                       p["match"].get("away_sporttery_id"))
                jc_write.upsert_jc_match(cur, p["match"], snap, src, home, away)
                ups += jc_write.upsert_jc_offer(cur, p["row"], snap, src)
            else:
                ups += jc_write.upsert_jc_result(cur, p["row"], snap, src)
    elif topic in ISSUE:
        for i, env in enumerate(lines, 1):
            ins = parse_line(env)
            if ins is None:
                continue
            try:
                cur.execute("savepoint iw")
                ups += jc_issue_write.upsert_issue_instruction(cur, ins, env.get("src_hash") or "", rel)
                cur.execute("release savepoint iw")
            except ValueError as e:
                cur.execute("rollback to savepoint iw")
                rej.append({"line": i, "reason": str(e)})
                logger.warning("issue-reject topic=%s 文件=%s 期=%s err=%s",
                               topic, rel, (ins or {}).get("pk"), str(e)[:80])
    elif topic == "jc_odds_history":
        for env in lines:
            env["src_file"] = rel
            ups += jc_odds_write.upsert_from_env(cur, env)
    else:
        logger.info("skip %s n=%d (2e 范围)", topic, n)
    gap, done = state == "missing", state != "missing" and state != "orphan"
    _ops(cur, topic, rel, path.stat().st_size if path else 0, n, ups, rej, gap, done)
    return {"topic": topic, "lines": n, "ups": ups, "gap": gap}
