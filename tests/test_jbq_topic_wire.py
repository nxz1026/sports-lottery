"""P0-COLLECT2lqP-jbq-wire2：jc_topic.load_topic 接通 jclq_result → jbq_result_write。
纯假 cursor/monkeypatch，不碰 DB/网络。验证：writer 被调、ValueError 回滚并记 rej、jclq_offer 仍 skip。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ingest import jc_topic  # noqa: E402


class FakeCur:
    def __init__(self):
        self.sql, self.rowcount = [], 1

    def execute(self, q, p=None):
        self.sql.append((q, p))


@pytest.fixture
def wire(monkeypatch):
    calls, cur = {}, FakeCur()
    monkeypatch.setattr(jc_topic, "read_lines", lambda p: list(calls["lines"]))
    # P0-COLLECT2 续：_load_saved 改用 parse_lines_all 拉平；legacy parse_line 仍保留供旧测试用。
    monkeypatch.setattr(jc_topic, "parse_lines_all",
                        lambda env: [calls["parse"].pop(0)] if calls["parse"] else [None])
    monkeypatch.setattr(jc_topic.jbq_result_write, "upsert_jbq_result_instruction",
                        lambda c, ins, h, f: calls.setdefault("writer", []).append((ins, h, f)) or 1)
    calls["mp"] = monkeypatch
    return cur, calls


def _run(cur, calls, topic, lines, parse):
    calls.update(lines=lines, parse=list(parse))
    path = Path(__file__)
    return jc_topic.load_topic(cur, path.parent, path, topic, path, "jsonl")


def test_jclq_result_calls_writer(wire):
    cur, calls = wire
    ins = {"table": "fact.jbq_result", "pk": {"match_id": "x"}, "row": {"match_id": "x"}}
    result = _run(cur, calls, "jclq_result", [{"src_hash": "h1"}], [ins])
    assert calls["writer"] == [(ins, "h1", f"jclq_result/{Path(__file__).name}")]
    assert cur.sql[:2] == [("savepoint jbq", None), ("release savepoint jbq", None)]
    assert result["ups"] == 1


def test_valueerror_rollback_and_reject(wire):
    cur, calls = wire

    def boom(c, ins, h, f):
        if h == "h1":
            raise ValueError("未知写指令目标表")
        return 1

    monkeypatch = calls["mp"]
    monkeypatch.setattr(jc_topic.jbq_result_write, "upsert_jbq_result_instruction", boom)
    ins = {"table": "fact.no", "pk": {"match_id": "x"}, "row": {"match_id": "x"}}
    result = _run(cur, calls, "jclq_result", [{"src_hash": "h1"}, {"src_hash": "h2"}], [ins, ins])
    assert cur.sql[:4] == [("savepoint jbq", None), ("rollback to savepoint jbq", None),
                           ("savepoint jbq", None), ("release savepoint jbq", None)]
    assert cur.sql[-1][1][4].obj == [{"line": 1, "reason": "fact.no:未知写指令目标表"}]
    assert cur.sql[-1][1][5] is False
    assert result["ups"] == 1


def test_parse_none_rejected(wire):
    cur, calls = wire
    result = _run(cur, calls, "jclq_result", [{"src_hash": "h1"}], [None])
    assert cur.sql[-1][1][4].obj == [{"line": 1, "reason": "parse_none"}]
    assert result["ups"] == 0
    assert "writer" not in calls


def test_jclq_offer_still_skipped(wire):
    cur, calls = wire
    r = _run(cur, calls, "jclq_offer", [{"src_hash": "h1"}], [None])
    assert "writer" not in calls
    assert r["ups"] == 0
    assert calls["parse"] == [None]
    assert cur.sql[-1][1][4] is None
