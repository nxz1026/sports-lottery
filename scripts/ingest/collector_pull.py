"""incoming/ 中带 .done 标记的文件 → 契约 v1.1 校验 → stg 装载 → ops 留痕 → 归档 done/（零拒绝）或 bad/（有拒绝；
league_ing 无 DELETE，坏样本必须留证）。归档：零拒绝 → 原件与标记都进 done/；有拒绝 → 原件进 bad/、标记仍进 done/
（标记语义=已处理）；无 .done 的数据文件不动（抗半包）；未知 topic 挪 bad/_unknown/；未知 topic 与装载失败留痕 ok=false。
逐文件独立事务、提交成功才归档（shutil.move 不随事务回滚；整批共用事务会让已落库的 stg/ops 回滚、原件却已进 done/），
main() 因此刻意用裸 pg.connect，_load 的断言锁死它（外层非 IDLE 即 RuntimeError）；run() 就绪 = 同名 .done 存在。
load_file：契约 v1.1 逐行校验后装载，非 JSON / 外壳八字段不合规 / src_hash 复算不符 ⇒ rejected（绝不入库）；
"""
from __future__ import annotations

import argparse
import shutil
from contextlib import closing, nullcontext
from pathlib import Path

from psycopg import Connection, pq, sql
from psycopg.types.json import Jsonb

from core.log import logger
from store import pg
from store.parse_collector import canonical_src_hash, row_error
from store.upsert_official import RAW_PREVIEW_CHARS, STG_TABLES

ROOT = Path("/srv/league-staging")
INCOMING, DONE, BAD = ROOT / "incoming", ROOT / "done", ROOT / "bad"  # 默认目录，函数/CLI 参数；测试一律传 tmp
INSERT_STG = "INSERT INTO stg.{} (line, src_hash, src_file) VALUES (%s, %s, %s) ON CONFLICT (src_hash) DO NOTHING"
LOG_SQL = "INSERT INTO ops.ingest_log (topic, src_file, rows_in, rows_ups, rejected, ok) VALUES (%s,%s,%s,%s,%s,%s)"
FILE_ARRIVAL_SQL = ("INSERT INTO ops.file_arrival (topic, src_file, bytes, rows, done_marker) VALUES (%s,%s,%s,%s,true)"
                    " ON CONFLICT (topic, src_file) DO UPDATE SET arrived_at=now(), bytes=EXCLUDED.bytes,"
                    " rows=EXCLUDED.rows, done_marker=true")

def _archive(data: Path, marker: Path, incoming: Path, done: Path, bad: Path, clean: bool) -> str:
    target = (done if clean else bad) / data.relative_to(incoming)
    for src, dst in ((data, target), (marker, done / marker.relative_to(incoming))):
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return str(target.parent)

def load_file(conn: Connection, path: str | Path, table: str) -> dict:
    if table not in STG_TABLES:  # 表名白名单：绝不拼出 stg 之外的 SQL
        raise ValueError(f"unknown stg table {table!r}")
    name, statement = Path(path).name, sql.SQL(INSERT_STG).format(sql.Identifier(table))
    rows_in, rows_ups, rejected = 0, 0, []
    try:
        with open(path, encoding="utf-8") as handle, conn.cursor() as cur:
            for lineno, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line:
                    continue
                rows_in += 1
                obj, reason = row_error(line, table)
                if reason:
                    rejected.append({"lineno": lineno, "reason": reason, "raw": line[:RAW_PREVIEW_CHARS]})
                else:
                    cur.execute(statement, (Jsonb(obj), canonical_src_hash(obj["payload"]), name))
                    rows_ups += cur.rowcount
    except PermissionError:
        logger.error("collector_pull: %s 权限拒绝", path)
        return {"rows_in": 0, "rows_ups": 0, "rejected_n": 1, "rejected": [{"reason": "permission-denied"}]}
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            rows_in += 1
            obj, reason = row_error(line, table)
            if reason:
                rejected.append({"lineno": lineno, "reason": reason, "raw": line[:RAW_PREVIEW_CHARS]})
            else:
                cur.execute(statement, (Jsonb(obj), canonical_src_hash(obj["payload"]), name))
                rows_ups += cur.rowcount
    return {"rows_in": rows_in, "rows_ups": rows_ups, "rejected_n": len(rejected), "rejected": rejected[:50]}

def _load(conn: Connection, data: Path, topic: str, incoming: Path, commit: bool, reason: str | None = None) -> dict:
    """单文件一个独立事务：stg 装载 + 两处留痕，提交成功才返回（失败整体回滚）；reason 非空则不装载、只写失败留痕。"""
    if commit and conn.pgconn.transaction_status != pq.TransactionStatus.IDLE:  # 合法环境只有 pg.connect 的裸连接
        raise RuntimeError("run(commit=True) 外层已有事务 → conn.transaction() 退化成 SAVEPOINT、提交不发生：归档后回滚即无痕丢数据")
    with conn.transaction() if commit else nullcontext(), conn.cursor() as cur:
        if reason:
            cur.execute(LOG_SQL, (topic, data.name, 0, 0, Jsonb([{"reason": reason}]), False))
            return {"rows_in": 0, "rows_ups": 0, "rejected_n": 1, "rejected": []}
        result = load_file(conn, data, topic)
        cur.execute(FILE_ARRIVAL_SQL, (topic, str(data.relative_to(incoming)), data.stat().st_size, result["rows_in"]))
        cur.execute(LOG_SQL, (topic, data.name, result["rows_in"], result["rows_ups"],
                              Jsonb(result["rejected"]), result["rejected_n"] == 0))
    return result

def _one(conn: Connection, marker: Path, incoming: Path, done: Path, bad: Path, commit: bool) -> dict:
    data, topic = marker.with_suffix(""), marker.parent.name  # topic = 数据文件所在目录名（incoming/<host>/<topic>）
    reason = "unknown topic" if topic not in STG_TABLES else None  # 未知 topic：不再每轮重复告警
    try:
        result = _load(conn, data, topic, incoming, commit, reason)
    except Exception as exc:  # 单文件失败不中断整批：留痕 ok=false → 原件进 bad/、标记进 done/
        logger.error("collector_pull: %s 未装载：%s", data.name, exc)
        result = _load(conn, data, topic, incoming, commit, reason=str(exc))
    roots = (bad / "_unknown", bad / "_unknown") if reason else (done, bad)
    archived = _archive(data, marker, incoming, *roots, result["rejected_n"] == 0)
    logger.info("collector_pull: %s in=%d up=%d rej=%d", data.name, result["rows_in"], result["rows_ups"],
                result["rejected_n"])
    return {**result, "topic": topic, "src_file": data.name, "archived_to": archived}

def run(conn: Connection, incoming: Path = INCOMING, done: Path = DONE, bad: Path = BAD,
        commit: bool = False) -> list[dict]:
    return [_one(conn, marker, incoming, done, bad, commit) for marker in
            sorted(m for m in incoming.rglob("*.done") if m.with_suffix("").is_file())]

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="incoming 中 .done 文件 → stg 装载 + ops 留痕 + 归档")
    for name, default in (("incoming", INCOMING), ("done", DONE), ("bad", BAD)):
        parser.add_argument(f"--{name}", type=Path, default=default)
    args = parser.parse_args(argv)
    with closing(pg.connect("ing")) as conn:  # 裸连接：外套事务会让逐文件提交退化成 SAVEPOINT
        logger.info("collector_pull: %d file(s) processed", len(run(conn, args.incoming, args.done, args.bad, True)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
