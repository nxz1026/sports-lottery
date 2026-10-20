"""P0-COLLECT2i 真包 JSONL → fact.jc_match/jc_offer/jc_result + ops 留痕；批↔文件按文件名时间戳窗口归属；同窗口多文件逐个装载（C 条修复）。

R48（生产实测根因，三条叠加）：
  ① **无增量水位**：``iter_markers`` 返回**全部** ``.done``（实测已 2601 个，09-15 → 10-19），
     每轮从最老的一个**全量重跑**；老批次的批↔文件归属解析要走 manifest/哈希消歧 + 时间戳窗口
     兜底，单轮耗时涨到超过 wrapper 的 ``timeout 420`` ⇒ 每轮都被砍在队尾前 ⇒
     **队尾新批次永远补不上**（实测 fact.jc_offer 最新 snap_ts 落后 17h，缺口随积压自我放大）。
  ② **孤儿阶段每轮重装 15,816 个旧文件**：orphan 扫描只判"是否被某 marker 的窗口覆盖"，
     不判"是否已装载" ⇒ 老文件每轮全量重装（98% 冗余 IO 的大头）。
  ③ **撞唯一键即废整批**（见 ingest/jc_issue_write.py::_insert_prize 的 R48 docstring）。

本轮修法（零数据移动、幂等由 src_hash 保证）：
  · **水位**：``ops.ingest_marker`` 记已处理完的 marker ⇒ 每轮只处理新到港的批次；
  · **已装载跳过**：``ops.file_arrival`` 记已装载文件 ⇒ orphan 阶段不再重装老文件；
  · **孤儿前置**：孤儿（可能是老数据）先装、marker 批（更新的数据）后装 ⇒ is_current 指针单调向新。

R48b（部署后生产实测追加）：
  ④ **orphan 阶段整段是一个事务 + 裸 ``except Exception: conn.rollback()``** ⇒ 第一个坏文件
     （上游撕裂 JSON，实测 11+ 个）就把**整段 1239 个文件**的写入全部回滚，而且异常被吞掉
     （日志只剩 "orphan 装载 n=1239"，看不出任何异常）⇒ 那批文件每轮白跑白丢、永远进不了
     ``ops.file_arrival`` ⇒ ④ 修的「已装载跳过」根本收敛不了（每轮还是 1239 个）。
     修法：隔离粒度降到**文件**（``_load_file`` 保存点）—— 坏文件自己回滚、同段好文件照常落库；
     失败清单打进 warning（不再静默）。
"""
import argparse
from contextlib import closing
from pathlib import Path

from core.log import logger
from store import pg

from ingest.jc_read import OPTIONAL, files_for_batch, iter_markers
from ingest.jc_topic import load_topic

TOPICS = ("jczq_offer", "jczq_result", "jc_issue", "jc_issue_result",
          "lottery_draw", "jclq_offer", "jclq_result")
TABLES = ("jc_match", "jc_offer", "jc_result", "jc_issue", "jc_issue_draw",
          "jc_issue_match", "jc_issue_prize", "lottery_draw")
_TABLE_SQL = {name: f"SELECT count(*) FROM fact.{name}" for name in TABLES}

# 旧台账（ops.ingest_log / ops.file_arrival / ops.quota_ledger）见 docs/db/infra_p0_2_schemas.sql
_WATERMARK_SQL = "select marker from ops.ingest_marker where ok"
_LOADED_FILES_SQL = "select topic, src_file from ops.file_arrival"
_MARKER_UPSERT_SQL = (
    "insert into ops.ingest_marker (marker, ups, ok) values (%s, %s, %s) "
    "on conflict (marker) do update set ups = excluded.ups, ok = excluded.ok, "
    "processed_at = now()"
)


_SAVEPOINT = ("savepoint jc_file", "release savepoint jc_file",
              "rollback to savepoint jc_file")


def _load_file(cur, root: Path, marker: Path, topic: str, path, state: str):
    """装载一个文件；失败只回滚**这一个文件**（保存点隔离），不掀翻同批其它文件。

    R48b（生产实测）：原来「整段一个事务 + 裸 ``except Exception: conn.rollback()``」
    ⇒ orphan 阶段第一个坏文件（上游撕裂 JSON）就把**上千个文件**的写入全部回滚，
    而且异常被吞掉（日志只剩 "orphan 装载 n=1239"，看不出任何异常）⇒ 那批文件每轮
    白跑白丢、永远进不了 ops.file_arrival ⇒ R48 的「已装载跳过」永远收敛不了。
    这里把隔离粒度降到**文件**：坏文件自己回滚，同批好文件照常落库。
    """
    try:
        cur.execute(_SAVEPOINT[0])
        r = load_topic(cur, root, marker, topic, path, state)
        cur.execute(_SAVEPOINT[1])
        return r, None
    except Exception as e:
        cur.execute(_SAVEPOINT[2])
        return None, f"{type(e).__name__}:{str(e)[:160]}"


def load_batch(conn, root: Path, marker: Path) -> dict:
    topics, errors = [], []
    try:
        with conn.cursor() as cur:
            for topic, cands in files_for_batch(root, marker, TOPICS + OPTIONAL).items():
                for path, state in cands or ([] if topic in OPTIONAL else [(None, "missing")]):
                    r, err = _load_file(cur, root, marker, topic, path, state)
                    if r is None:
                        errors.append(f"{marker.name}:{topic}:{err}")
                    else:
                        topics.append(r)
        conn.commit()
    except Exception as e:
        conn.rollback()
        errors.append(f"{marker.name}:{type(e).__name__}:{e}")
    return {"marker": marker.name, "topics": topics, "errors": errors}


def _done_markers(cur) -> set[str]:
    """已成功处理过的 marker 名（水位）。表不存在时退化为"全量重跑"（旧库兼容）。"""
    try:
        cur.execute(_WATERMARK_SQL)
        return {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning("ingest_marker 台账不可用，退化为全量重跑: %s", e)
        conn = getattr(cur, "connection", None)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return set()


def _loaded_files(cur) -> set[tuple[str, str]]:
    """已装载过的 (topic, src_file) 集合 —— 用来跳过老文件（98% 冗余 IO 的大头）。"""
    try:
        cur.execute(_LOADED_FILES_SQL)
        return {(r[0], r[1]) for r in cur.fetchall()}
    except Exception as e:
        logger.warning("file_arrival 台账不可用，退化为全量装: %s", e)
        return set()


def _record_marker(conn, marker: Path, ups: int, ok: bool) -> None:
    """把处理结果写进水位台账（失败批次不标 ok ⇒ 下轮会重试）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(_MARKER_UPSERT_SQL, (marker.name, ups, ok))
        conn.commit()
    except Exception as e:
        logger.warning("写 ingest_marker 台账失败 %s: %s", marker.name, e)


def _pending_markers(root: Path, batch: str | None, done: set[str]) -> list[Path]:
    """水位之后的新批次（升序）。已处理的 marker 直接跳过 —— 这是 R48 的核心提速点。"""
    return [m for m in iter_markers(root, batch) if m.name not in done]


def _claimed_files(root: Path, markers: list[Path]) -> set[Path]:
    """pending marker 认领走的文件（孤儿判定用）。

    只解析**待处理**的 marker：全量解析正是 420s 超时的主因。
    已处理 marker 的文件早已落库（file_arrival 有记录），不会进孤儿集。
    """
    seen: set[Path] = set()
    for m in markers:
        try:
            for cs in files_for_batch(root, m, TOPICS + OPTIONAL).values():
                for p, _ in cs or []:
                    if p:
                        seen.add(p)
        except Exception as e:
            logger.warning("orphan-scan skipped: %s", e)
    return seen


def _orphan_files(root: Path, claimed: set[Path], loaded: set[tuple[str, str]]) -> list[Path]:
    """盘上有、但既没被待处理 marker 认领、也从未装载过的文件。

    R48：原实现只判"是否被任何 marker 认领"，于是 15,816 个老文件每轮全量重装。
    现在多一道"是否已装载"（ops.file_arrival）⇒ 老文件直接跳过。
    """
    orph: list[Path] = []
    try:
        for t in TOPICS + OPTIONAL:
            for p in sorted((root / t).glob("*.jsonl")):
                if p in claimed:
                    continue
                if (t, f"{t}/{p.name}") in loaded:
                    continue          # 已装载过 ⇒ 不重装
                orph.append(p)
    except Exception as e:
        logger.warning("orphan-glob skipped: %s", e)
    return orph


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--batch")
    args = ap.parse_args(argv)
    root = Path(args.dir)
    with closing(pg.connect("ing")) as conn:
        with conn.cursor() as cur:
            done = _done_markers(cur)
            loaded = _loaded_files(cur)
        pending = _pending_markers(root, args.batch, done)
        logger.info("水位：已处理 %d 个 marker，本轮待处理 %d 个", len(done), len(pending))

        # 孤儿先装：它们可能是老数据（marker 没到港的批次）。
        # 放在 marker 批之前 ⇒ is_current 指针单调向新（后装的更新批次会盖过旧版本）。
        claimed = _claimed_files(root, pending)
        orph = _orphan_files(root, claimed, loaded)
        bad: list[str] = []
        try:
            with conn.cursor() as cur:
                for p in orph:
                    r, err = _load_file(cur, root, Path("orphan"), p.parent.name, p, "orphan")
                    if r is None:
                        bad.append(f"{p.parent.name}/{p.name}:{err}")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("orphan 阶段整体失败: %s: %s", type(e).__name__, e)
        if bad:
            logger.warning("orphan 失败 n=%d（文件级隔离，其余文件照常落库）：%s",
                           len(bad), "; ".join(bad[:5]) + ("…" if len(bad) > 5 else ""))
        logger.info("orphan 装载 n=%d 失败=%d 文件=%s", len(orph), len(bad),
                    ",".join(p.name for p in orph[:5]) + ("…" if len(orph) > 5 else "") or "-")

        # marker 批：只处理水位之后的新批次（升序 ⇒ 越新越后写）。
        for m in pending:
            r = load_batch(conn, root, m)
            logger.info("batch %s ups=%d errors=%s", r["marker"],
                        sum(t["ups"] for t in r["topics"]), r["errors"])
            _record_marker(conn, m, sum(t["ups"] for t in r["topics"]), not r["errors"])

        with conn.cursor() as cur:
            vals = [cur.execute(_TABLE_SQL[t]).fetchone()[0] for t in TABLES]
        conn.commit()
        logger.info("count jc_match=%d jc_offer=%d jc_result=%d jc_issue=%d jc_issue_draw=%d "
                    "jc_issue_match=%d jc_issue_prize=%d lottery_draw=%d", *vals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
