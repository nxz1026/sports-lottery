"""P0-COLLECT2i 真包 JSONL → fact.jc_match/jc_offer/jc_result + ops 留痕；批↔文件按文件名时间戳窗口归属；同窗口多文件逐个装载（C 条修复）。"""
import argparse
from contextlib import closing
from pathlib import Path

from core.log import logger
from ingest.jc_read import OPTIONAL, files_for_batch, iter_markers
from ingest.jc_topic import load_topic
from store import pg

TOPICS = ("jczq_offer", "jczq_result", "jc_issue", "jc_issue_result",
          "lottery_draw", "jclq_offer", "jclq_result")
TABLES = ("jc_match", "jc_offer", "jc_result", "jc_issue", "jc_issue_draw",
          "jc_issue_match", "jc_issue_prize", "lottery_draw")
_TABLE_SQL = {name: f"SELECT count(*) FROM fact.{name}" for name in TABLES}


def load_batch(conn, root: Path, marker: Path) -> dict:
    topics, errors = [], []
    try:
        with conn.cursor() as cur:
            for topic, cands in files_for_batch(root, marker, TOPICS + OPTIONAL).items():
                for path, state in cands or ([] if topic in OPTIONAL else [(None, "missing")]):
                    topics.append(load_topic(cur, root, marker, topic, path, state))
        conn.commit()
    except Exception as e:
        conn.rollback()
        errors.append(f"{marker.name}:{type(e).__name__}:{e}")
    return {"marker": marker.name, "topics": topics, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--batch")
    args = ap.parse_args(argv)
    root = Path(args.dir)
    with closing(pg.connect("ing")) as conn:
        for m in iter_markers(root, args.batch):
            r = load_batch(conn, root, m)
            logger.info("batch %s ups=%d errors=%s", r["marker"],
                        sum(t["ups"] for t in r["topics"]), r["errors"])
        seen = {p for m in iter_markers(root, args.batch)
                for cs in files_for_batch(root, m, TOPICS + OPTIONAL).values() for p, _ in cs or [] if p}
        orph = [p for t in TOPICS + OPTIONAL for p in sorted((root / t).glob("*.jsonl")) if p not in seen]
        try:
            with conn.cursor() as cur:
                for p in orph: load_topic(cur, root, Path("orphan"), p.parent.name, p, "orphan")
            conn.commit()
        except Exception:
            conn.rollback()
        logger.info("orphan 装载 n=%d 文件=%s", len(orph),
                    ",".join(p.name for p in orph[:5]) + ("…" if len(orph) > 5 else "") or "-")
        with conn.cursor() as cur:
            vals = [cur.execute(_TABLE_SQL[t]).fetchone()[0] for t in TABLES]
        conn.commit()
        logger.info("count jc_match=%d jc_offer=%d jc_result=%d jc_issue=%d jc_issue_draw=%d "
                    "jc_issue_match=%d jc_issue_prize=%d lottery_draw=%d", *vals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
