"""契约 v1.1 topic → 落库分派：WRITE（竞彩足球双表）走 jc_write；jclq_result 与 ISSUE 三个写指令 topic
共用 _load_saved（逐条 savepoint，坏行只回滚自己）；jc_odds_history 逐行 upsert；其余 skip。"""
from pathlib import Path

from core.log import logger
from ingest import jbq_result_write, jc_issue_write, jc_odds_write, jc_write
from ingest.jc_read import read_lines
from psycopg.types.json import Json
from store.parse_collector import parse_line, parse_lines_all

WRITE = ("jczq_offer", "jczq_result")
ISSUE = ("jc_issue", "jc_issue_result", "lottery_draw")
BASKETBALL_RESULTS = ("jclq_result",)
_BBALL_LANE = ("jbq", "jbq-reject", "pk")   # 篮球结果通道：保存点名 / 拒绝日志前缀 / 主键标签
_ISSUE_LANE = ("iw", "issue-reject", "期")  # 三个写指令 topic 通道：同上
_SAVEPOINT_SQL = {
    name: {
        "savepoint": f"savepoint {name}",
        "release": f"release savepoint {name}",
        "rollback to": f"rollback to savepoint {name}",
    }
    for name in ("jbq", "iw")
}


def _ops(cur, topic: str, rel: str, size: int | None, n: int, ups: int,
         rej: list, gap: bool, done: bool) -> None:
    cur.execute("insert into ops.file_arrival (topic,src_file,bytes,rows,done_marker) values "
                "(%s,%s,%s,%s,%s) on conflict (topic,src_file) do update set bytes=excluded.bytes,"
                "rows=excluded.rows,done_marker=excluded.done_marker",
                (topic, rel, size, n, done))
    cur.execute("insert into ops.ingest_log (topic,src_file,rows_in,rows_ups,rejected,ok) "
                "values (%s,%s,%s,%s,%s,%s)",
                (topic, rel, n, ups, Json(rej) if rej else None, not rej or gap))


def _load_saved(cur, topic: str, rel: str, lines: list, rej: list, writer) -> int:
    """逐条写指令：savepoint 隔离；parse_line None 与 writer ValueError 都记 rej，不静默丢行。
    P0-COLLECT2：jc_issue / jc_issue_result 一行 envelope 可产生多条指令（parent + N children），
    用 parse_lines_all 拉平；children 写失败但 parent 成功 ⇒ 父行已落，rej 单独记行内 index；不断父事务。"""
    sp, kind, label = _BBALL_LANE if topic in BASKETBALL_RESULTS else _ISSUE_LANE
    ups = 0
    for i, env in enumerate(lines, 1):
        all_ins = parse_lines_all(env)
        if all_ins == [None]:
            rej.append({"line": i, "reason": "parse_none"})
            continue
        for j, ins in enumerate(all_ins):
            if ins is None:
                rej.append({"line": i, "reason": f"parse_none[child {j}]"})
                continue
            try:
                cur.execute(_SAVEPOINT_SQL[sp]["savepoint"])
                ups += writer(cur, ins, env.get("src_hash") or "", rel)
                cur.execute(_SAVEPOINT_SQL[sp]["release"])
            except ValueError as e:
                cur.execute(_SAVEPOINT_SQL[sp]["rollback to"])
                rej.append({"line": i, "reason": f"{ins['table']}:{str(e)[:80]}"})
                logger.warning("%s topic=%s 文件=%s %s=%s err=%s",
                               kind, topic, rel, label, ins.get("pk"), str(e)[:80])
    return ups


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
    elif topic in BASKETBALL_RESULTS:
        ups += _load_saved(cur, topic, rel, lines, rej, jbq_result_write.upsert_jbq_result_instruction)
    elif topic in ISSUE:
        ups += _load_saved(cur, topic, rel, lines, rej, jc_issue_write.upsert_issue_instruction)
    elif topic == "jc_odds_history":
        for env in lines:
            ups += jc_odds_write.upsert_from_env(cur, {**env, "src_file": rel})
    else:
        logger.info("skip %s n=%d (2e 范围)", topic, n)
    gap, done = state == "missing", state != "missing" and state != "orphan"
    _ops(cur, topic, rel, path.stat().st_size if path else 0, n, ups, rej, gap, done)
    return {"topic": topic, "lines": n, "ups": ups, "gap": gap}
