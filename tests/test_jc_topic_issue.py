"""P0-COLLECT2eB 接线测试：三个写指令 topic（jc_issue / jc_issue_result / lottery_draw）经 load_topic
真包端到端落库：① 三表各自 count>0 且同批重放幂等（first_seen 不变、last_seen 前进）；
② 首条指令 ValueError ⇒ 逐条保存点隔离，好指令照常、ops.ingest_log 留痕 rejected；
③ 收尾回滚 ⇒ 另开 ro 连接三表 0 行（§9-83）。
夹具 = /srv/league-staging/incoming/cn-collector/** 真包（不 glob 顶层，目录权 711 只能精确进）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest import jc_issue_write, jc_topic  # noqa: E402
from store import pg  # noqa: E402

BASE = Path("/srv/league-staging/incoming/cn-collector")
FILES = {t: BASE / t / "2026-09-16T10-13Z__001.jsonl" for t in ("jc_issue", "jc_issue_result", "lottery_draw")}
TABLE = {"jc_issue": "fact.jc_issue", "jc_issue_result": "fact.jc_issue_draw", "lottery_draw": "fact.lottery_draw"}


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    try:
        yield connection  # 用例里不要再套 closing / with
    finally:
        connection.rollback()  # 库里最终 0 行的保证
        connection.close()


def _run(cur, topic: str) -> dict:
    return jc_topic.load_topic(cur, Path.cwd(), FILES[topic], topic, FILES[topic], "jsonl")


def _snap(cur) -> dict:
    return {t: cur.execute(f"select count(*), min(first_seen_at), max(last_seen_at) from {tab}").fetchone()
            for t, tab in TABLE.items()}


def test_e2e_real_batches_upsert_and_replay_idempotent(conn):
    cur = conn.cursor()
    for topic in FILES:
        ret = _run(cur, topic)
        assert set(ret) == {"topic", "lines", "ups", "gap"}
        assert ret["ups"] > 0 and ret["lines"] > 0
    snap1 = _snap(cur)
    for topic in FILES:
        assert snap1[topic][0] > 0  # 单表各自 0 行就该暴露（不求和）
    conn.rollback()  # 跨事务推进 now()：PG 同事务 now() 恒定，sleep 无效
    for topic in FILES:
        _run(cur, topic)
    snap2 = _snap(cur)
    for topic in FILES:
        assert snap2[topic][0] == snap1[topic][0]     # 行数不变
        assert snap2[topic][2] > snap1[topic][2]      # 跨事务 ⇒ last_seen_at 严格变大
        assert snap2[topic][1] == snap1[topic][1]      # first_seen_at 不变


def test_bad_instruction_rejected_by_savepoint_good_rows_survive(conn, monkeypatch):
    cur = conn.cursor()
    topic = "jc_issue"
    calls = {"n": 0}

    def f(cur2, ins, src_hash, src_file):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return 1

    monkeypatch.setattr(jc_issue_write, "upsert_issue_instruction", f)
    ret = _run(cur, topic)
    # P0-COLLECT2 续：jc_issue 一行 envelope ⇒ parent + N children，每条独立 savepoint；首条坏 ⇒ 总成功数
    # = 所有 envelope 展开后调用 writer 的总次数 - 1；ops.ingest_log.rejected 按 envelope line 1 记一条。
    assert ret["ups"] == calls["n"] - 1
    rej, ups, ok = cur.execute(
        "select rejected, rows_ups, ok from ops.ingest_log where topic=%s and src_file=%s "
        "order by id desc limit 1", [topic, f"{topic}/{FILES[topic].name}"]).fetchone()
    assert isinstance(rej, list) and len(rej) == 1
    assert rej[0]["line"] == 1 and "boom" in rej[0]["reason"]
    assert ups == ret["ups"]
    assert ok is False


def test_finale_all_rollback_no_traces():
    """回滚无残留 = 收尾行数与开测前快照逐表一致（**不再断言 0 行**：cron 已往这三表装真数据，
    2026-09-17 实测 jc_issue=8 / lottery_draw=120 ⇒ 绝对 0 行的断言在生产库上必然假失败）。"""
    with pg.read_conn("ro") as c:
        before = {t: c.execute(f"select count(*) from {tab}").fetchone()[0] for t, tab in TABLE.items()}
    conn = pg.connect("ing")  # 本用例自开自收：不走 conn fixture，否则拿不到自己的回滚边界
    try:
        cur = conn.cursor()
        _run(cur, "jc_issue")
        mid = {t: cur.execute(f"select count(*) from {tab}").fetchone()[0] for t, tab in TABLE.items()}
        conn.rollback()
    finally:
        conn.close()
    with pg.read_conn("ro") as c:
        after = {t: c.execute(f"select count(*) from {tab}").fetchone()[0] for t, tab in TABLE.items()}
    assert before == after  # 回滚生效 ⇒ 与开测前逐表一致（mid 只作旁证，upsert 可能等值）
