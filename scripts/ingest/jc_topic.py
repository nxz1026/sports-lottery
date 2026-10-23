"""契约 v1.1 topic → 落库分派：WRITE（竞彩足球双表）走 jc_write；jclq_result 与 ISSUE 三个写指令 topic
共用 _load_saved（逐条 savepoint，坏行只回滚自己）；jc_odds_history 逐行 upsert；其余 skip。

⚠️ **P2-C（2026-10-23）ops.ingest_log 的 rows_in / rows_ups 口径**（本模块是 jc 装载链的**唯一定义处**）：

  · ``rows_in``  = **读入行数**：本文件里被读到、并进入装载流程的**信封行**数
                   （空行不计；读不出来 / 被隔离 ⇒ 记 0）。
  · ``rows_ups`` = **落库行数**：upsert 语句实际写入（INSERT 或 UPDATE）的行数合计。
                   ⚠️ **口径按生产者不同，必须分开看（不许混算）**：
                     - 本模块（jc 装载链）：落库 = **新增 + 更新**；**幂等跳过记 0**；
                     - ``scripts/ingest/collector_pull.py``（stg 装载链）：
                       ``ON CONFLICT DO NOTHING`` 的 rowcount ⇒ **只有新增**，冲突跳过记 0。
                     ⇒ 同一个列在两条链上含义不同（新增 vs 新增+更新）—— 这是**已登记**的口径差异，
                       消费方必须按 topic/链路分开解读（接口层见 ``web/routers/jc.py`` 的 ops 视图）。
  · **未落库行数** = ``rows_in - rows_ups - 拒行数``，分两类（接口层要能分开）：
                     - **幂等跳过**：读到了，但 upsert 冲突未改（重复装载同一批）；
                     - **设计跳过**：该 topic 不在装载范围（本模块 ``else`` 分支，如 ``jclq_offer``）。
  · ⚠️ **例外** ``jc_odds_history`` 是**展开口径**：一个信封可展开成 N 条历史行
    ⇒ ``rows_ups`` 可能 **> rows_in** ⇒ 该 topic **不许**用 ``rows_in - rows_ups`` 对账。
"""
import json
from pathlib import Path

from core.log import logger
from psycopg import errors as pg_errors
from psycopg.types.json import Json, Jsonb
from store.parse_collector import parse_line, parse_lines_all

from ingest import jbq_result_write, jc_issue_write, jc_odds_write, jc_write
from ingest.jc_read import UnreadableFileError, read_lines

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

# ⚠️ **P2-C（2026-10-23）隔离事件入台账**（幂等）：
#   坏文件/异形文件被隔离时，原来台账**零痕迹** ⇒ 页面上看不到「有 N 个文件被隔离」
#   ⇒ 等于**静默丢数据**（生产实测：隔离区 29 个文件，19 个连一行记录都没有）。
#   ⇒ 隔离事件写一行 ``ops.ingest_log``：``ok=false`` + ``rejected=[{"reason": ...}]``
#      （状态表达「**已隔离**」而不是「成功」）。
#   ⇒ **幂等**：「保留窗口(7 天)内同文件同原因已记过账」⇒ 不重复写
#      （否则每 10 分钟重试一次就多一行 ⇒ 又是「每天 1 万行」）。
#   ⇒ 前 6 个占位符与普通台账写入**同序**（topic, src_file, rows_in, rows_ups, rejected, ok），
#      后面 3 个只服务于去重判定。
_QUARANTINE_SQL = (
    "insert into ops.ingest_log (topic,src_file,rows_in,rows_ups,rejected,ok) "
    "select %s,%s,%s,%s,%s,%s "
    "where not exists (select 1 from ops.ingest_log l "
    "where l.topic = %s and l.src_file = %s and l.ok = false "
    "and l.at > now() - interval '7 days' and l.rejected @> %s)"
)


def record_quarantine(cur, topic: str, src_file: str, reason: str = "quarantined",
                      detail: str = "") -> None:
    """把「文件被隔离」写进 ops 台账（**幂等**：重试不重复记账）。

    ⚠️ **P2-C（2026-10-23）**：隔离事件原来**零痕迹** ⇒ 用户在页面上看不到
    「有 N 个文件被隔离」⇒ 静默丢数据。
    ⇒ 状态用 ``ok=false`` + ``rejected=[{"reason": ...}]`` 表达「**已隔离**」。
    ⇒ **不写** ``ops.file_arrival``（那个文件**没有**被装载 ⇒ 不能留下「已装载」的凭据，
      否则装载器会永久跳过它 ⇒ 数据静默缺失）。
    """
    notes = [{"reason": reason}]
    if detail:
        notes[0]["detail"] = str(detail)[:200]
    cur.execute(_QUARANTINE_SQL,
                (topic, src_file, 0, 0, Json(notes), False,
                 topic, src_file, Jsonb([{"reason": reason}])))


def _ops(cur, topic: str, rel: str, size: int | None, n: int, ups: int,
         rej: list, gap: bool, done: bool, *, ok: bool | None = None,
         notes: list | None = None, file_arrival: bool = True) -> None:
    """写台账（`ops.file_arrival` + `ops.ingest_log`）。

    ⚠️ **P1（2026-10-23）铁律**：**不许写「0 行 / 0 拒绝 / ok=true」** ——
    那等于说「这个文件我读过了，里面就是空的，没问题」，
    而真实情况往往是**根本没读成**（权限 / IO）⇒ **数据被静默丢弃，台账还说成功**
    （生产实测：permission-denied 44 次，每次都记 ok=true rows_in=0）。

    ⇒ `ok` 显式化：
      - `ok=None`（默认）= 旧口径「有行级拒绝 ⇒ False，否则 True」；
      - 调用方可显式给 `ok=False`（读不出来 / 没装成）**并**用 `notes` 写明原因；
      - `file_arrival=False` ⇒ **不写**「已装载」台账 ⇒ 该文件仍会被重试
        （`jc_load._orphan_files` 按 `ops.file_arrival` 判「已装载过」）。

    ⚠️ **P2-C（2026-10-23）口径**（定义见模块 docstring）：
      - ``rows_in``  = **读入行数**（`n`：本文件读到的信封行数，空行不计）；
      - ``rows_ups`` = **落库行数**（`ups`：upsert 实际写入的行数；幂等跳过记 0）。
      ⇒ 两者**不可相减当「拒行数」**：未落库行数里既有幂等跳过、也有设计跳过，
        而 ``jc_odds_history`` 还是展开口径（rows_ups 可能 > rows_in）。
    """
    if file_arrival:
        cur.execute("insert into ops.file_arrival (topic,src_file,bytes,rows,done_marker) values "
                    "(%s,%s,%s,%s,%s) on conflict (topic,src_file) do update set bytes=excluded.bytes,"
                    "rows=excluded.rows,done_marker=excluded.done_marker",
                    (topic, rel, size, n, done))
    rejected = list(rej)
    if notes:
        rejected.extend(notes)
    if ok is None:
        ok = bool(not rej or gap)
    cur.execute("insert into ops.ingest_log (topic,src_file,rows_in,rows_ups,rejected,ok) "
                "values (%s,%s,%s,%s,%s,%s)",
                (topic, rel, n, ups, Json(rejected) if rejected else None, ok))


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
            except pg_errors.IntegrityError as e:
                # R48：约束冲突（如唯一键撞车）也是**单行**问题，不是整批问题。
                # 原实现只 catch ValueError ⇒ IntegrityError 直接掀翻整个批事务
                # （实测 2026-10-01 起 70% 批次 rollback ⇒ 队尾新批次永远落不了库）。
                # 这里同样走保存点回滚：只回滚这一行，记进 rejected，批次继续。
                cur.execute(_SAVEPOINT_SQL[sp]["rollback to"])
                reason = f"{ins['table']}:{type(e).__name__}:{str(e)[:80]}"
                rej.append({"line": i, "reason": reason})
                logger.warning("%s topic=%s 文件=%s %s=%s err=%s",
                               kind, topic, rel, label, ins.get("pk"), reason)
    return ups


def load_topic(cur, root: Path, marker: Path, topic: str, path: Path | None, state: str) -> dict:
    # ⚠️ **2026-10-23 订正（gaps 口径）**：新增 state="absent" ——
    #   「**该批次的设计里本就不含这个 topic**」（回补批 / 单 topic 推送 / 手动验证批）。
    #   它**不是缺档**，所以：**不写 ops.file_arrival**（否则台账被假行灌满）、
    #   **不记 gap**（否则 gaps 虚高）、**也不写 ops.ingest_log**（没有装载发生过）。
    #   ⇒ 直接返回，不落任何痕迹。
    # ⚠️ **返回值的键集合必须与其它路径完全一致**（``{"topic","lines","ups","gap"}``）——
    #   既有契约由 ``tests/test_jc_topic_issue.py`` 断言（``set(ret) == {...}``）⇒
    #   这里**不许**加 "absent" / "missing_file" 之类的额外键（那会掀翻既有断言，
    #   2026-10-23 在 Oracle 上实测踩到）。
    if state == "absent":
        return {"topic": topic, "lines": 0, "ups": 0, "gap": False}

    rel = f"{topic}/{path.name}" if path else f"{marker.stem}/{topic}#MISSING"

    # ⚠️ **P2-C（2026-10-23）异形文件名 ⇒ 文件级隔离**：
    #   ``state="badname"`` 由 ``ingest.jc_read._window`` 给出（文件名的批次时间戳
    #   推不出来：非法字符 / 超长 / 非法时间戳）。
    #   旧实现：``_stamp`` 直接抛 AttributeError ⇒ **掀翻整批** ⇒ marker 永久
    #   ``ok=false`` ⇒ 每 10 分钟重装整批（每天凭空 1 万行 ingest_log）。
    #   ⇒ 现在：**该文件记失败 + 原因（入台账，状态=已隔离）**，**其余文件继续**，
    #     且**不掀翻整批**（批次能标 ok=true ⇒ 停止重复重装）。
    if state == "badname":
        logger.warning("异形文件名（已隔离）topic=%s 文件=%s ⇒ 记 ok=false、不掀翻整批",
                       topic, rel)
        record_quarantine(cur, topic, rel, "bad-filename",
                          "文件名里推不出批次时间戳（异形名）")
        return {"topic": topic, "lines": 0, "ups": 0, "gap": False}

    # ⚠️ **2026-10-23 修一个真缺陷（第三个）**：``state == "rejected"``
    #   （清单**列了**这个文件、但**行数与声明对不上**）原来会走到
    #   ``read_lines(path)`` ⇒ 读到的是**好行**（行数对不上≠读不出来），
    #   于是**照常装载**（旧行为，其实是对的：ticket 2q3b 明写 rejected → done=true）。
    #   但**文件真的不存在**（manifest-file-missing，被隔离/被清理/半传丢文件）时，
    #   ``read_lines`` 会抛 **FileNotFoundError 掀翻整批** ⇒ 该批 marker 永久 ok=false
    #   ⇒ 水位失效 ⇒ **每 10 分钟重装整批** ⇒ 实测每天凭空写 1 万行重复
    #   ``ops.ingest_log``（且无信息增量）。
    # ⇒ 现在：**文件不存在 ⇒ 记 gap（真缺档）+ 留痕（ingest_log ok=false），
    #   但不掀翻整批** —— 一个文件丢了不该让同批其它文件永远重跑。
    #   ⚠️ 与 ticket 2q3b 的差别只在「文件不存在」这一支：
    #   rejected（行数不符、文件在）仍照常装载 ⇒ done=true，行为不变。
    if path is not None and not path.exists():
        logger.warning("清单列了但文件不存在 topic=%s 文件=%s ⇒ 记缺档、不掀翻整批",
                       topic, rel)
        cur.execute(
            "insert into ops.ingest_log (topic,src_file,rows_in,rows_ups,rejected,ok) "
            "values (%s,%s,%s,%s,%s,%s)",
            (topic, rel, 0, 0, Json([{"reason": "manifest-file-missing"}]), False))
        # 真缺档：写 file_arrival（带 #MISSING 后缀）⇒ gaps 计数 +1（**这是真缺档**）
        cur.execute(
            "insert into ops.file_arrival (topic,src_file,bytes,rows,done_marker) "
            "values (%s,%s,%s,%s,%s) on conflict (topic,src_file) do nothing",
            (topic, f"{marker.stem}/{topic}#MISSING", 0, 0, False))
        return {"topic": topic, "lines": 0, "ups": 0, "gap": True}

    # ⚠️ **P1（2026-10-23）修一个真回归**：这里原来写的是
    #   ``state in ("jsonl", "orphan")`` —— **把 "rejected" 挡掉了** ⇒
    #   「清单列了、文件在、但行数与声明不符」的文件**根本没被读** ⇒
    #   n=0 / ups=0 / rej=[] ⇒ `_ops` 记成 **ok=true, rows_in=0** ⇒
    #   **文件里的数据被静默丢弃，而台账说「装成功」**。
    #   （而上面那段注释自己写的是「rejected 仍照常装载」—— **注释与代码打架**。）
    # ⇒ 现在 **rejected 也要读盘**（ticket P0-COLLECT2q3b 契约：行数不符≠读不出来），
    #   并把「manifest 行数不符」作为**注记**写进 ingest_log（可见但不算行级拒绝）。
    notes: list = []
    if state == "rejected":
        notes.append({"reason": "manifest-mismatch",
                      "detail": "清单声明行数与实际不符 ⇒ 已照常装载（ticket 2q3b）"})
    try:
        lines = read_lines(path) if path and state in ("jsonl", "orphan", "rejected") else []
    except UnreadableFileError as e:
        # ⚠️ **P1（2026-10-23）**：读不出来（权限 / IO）**不是「0 行已装载」**。
        # ⇒ 记 ok=false + 原因；**不写 file_arrival** ⇒ 该文件仍会被重试
        #   （`jc_load._orphan_files` 按 file_arrival 判「已装载过」）；
        # ⇒ 也**不掀翻整批**（同 ticket 2q3b：一个文件坏了不该让同批永远重跑）。
        # ⚠️ **P2-C（2026-10-23）**：改走 ``record_quarantine`` ⇒ 台账**幂等**
        #   （同一个文件每 10 分钟重试一次 ⇒ 只记**第一**次，不再每天凭空多行）。
        logger.warning("文件读不出来 topic=%s 文件=%s ⇒ 记 ok=false、不写已装载台账",
                       topic, rel)
        record_quarantine(cur, topic, rel, "permission-denied", str(e))
        return {"topic": topic, "lines": 0, "ups": 0, "gap": False}
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # ⚠️ **P2-C（2026-10-23）内容损坏（撕裂 JSON / 非 UTF-8）⇒ 文件级隔离**：
        #   旧实现：异常冒到 ``jc_load._load_file`` ⇒ 整批回滚 ⇒ marker 永久
        #   ``ok=false`` ⇒ 每 10 分钟重装整批 ⇒ 每天凭空 1 万行 ingest_log。
        #   ⇒ 现在：**该文件记失败 + 原因（入台账，状态=已隔离）**，**不掀翻整批**。
        logger.warning("文件内容损坏 topic=%s 文件=%s ⇒ 记 ok=false、不掀翻整批",
                       topic, rel)
        record_quarantine(cur, topic, rel, "bad-json",
                          f"{type(e).__name__}: {str(e)[:180]}")
        return {"topic": topic, "lines": 0, "ups": 0, "gap": False}
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
    _ops(cur, topic, rel, path.stat().st_size if path else 0, n, ups, rej, gap, done,
         notes=notes)
    return {"topic": topic, "lines": n, "ups": ups, "gap": gap}


# ── CLI：把「已被运维隔离的文件」补记进 ops 台账 ─────────────────────────
#
# ⚠️ **P2-C（2026-10-23）**：`docs/infra/jc-staging-retention.sh` 的「坏文件隔离」
#   步骤只做文件移动、**不写台账** ⇒ 被隔离的文件在 ops 台账里**零痕迹**
#   （生产实测：隔离区 29 个文件，19 个连一行记录都没有）⇒ 页面/接口上看不出来。
# ⇒ 这里给运维一个**幂等**的记账入口（该脚本属别的 worker 的文件，本轮只提供入口 + 接线建议）：
#
#   PYTHONPATH=scripts python -m ingest.jc_topic --record-quarantine \
#       --src-file jczq_offer/2026-10-23T10-00Z__001.jsonl --reason quarantined
#
#   · ``--src-file`` 用**相对落港根**的路径（``<topic>/<文件名>``）⇒ topic 自动取第一段；
#   · 幂等：保留窗口内同文件同原因已记过账 ⇒ 不重复写（可安全重跑）。

def _record_quarantine_cli(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="把已被隔离的文件记进 ops.ingest_log（幂等）")
    ap.add_argument("--src-file", required=True,
                    help="相对落港根的路径，形如 <topic>/<文件名>")
    ap.add_argument("--reason", default="quarantined", help="隔离原因（默认 quarantined）")
    ap.add_argument("--detail", default="", help="补充说明（可空）")
    args = ap.parse_args(argv)

    rel = str(args.src_file).lstrip("/")
    topic = rel.split("/", 1)[0] if "/" in rel else "unknown"
    from store import pg

    with pg.write_conn("ing") as conn:
        with conn.cursor() as cur:
            record_quarantine(cur, topic, rel, args.reason, args.detail)
    logger.info("已记隔离台账 topic=%s src_file=%s reason=%s", topic, rel, args.reason)
    return 0


if __name__ == "__main__":  # pragma: no cover - 运维脚本入口
    raise SystemExit(_record_quarantine_cli())

