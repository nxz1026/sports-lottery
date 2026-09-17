"""P0-COLLECT2lqP 写手测试：upsert_jbq_result_instruction 纯假游标，不开连接不落库（§9-83）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ingest import jbq_result_write  # noqa: E402

INS = {"table": "fact.jbq_result", "pk": {"match_id": "301"}, "notes": [],
       "row": {"match_id": "301", "final_score": "88-92", "status": "sold",
               "mnl_result_status": "lose", "mnl_odds": 2.85, "raw_blocks": {"a": [1]}}}


class FakeCur:
    """记录 execute 入参；故意不带 commit 属性（写手不得碰事务）。"""

    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.calls = []

    def execute(self, query, vals):
        self.calls.append((query, vals))


def _sql_text(query) -> str:
    return query.as_string(None)


def test_ok_returns_rowcount_and_binds_json():
    cur = FakeCur(rowcount=1)
    assert jbq_result_write.upsert_jbq_result_instruction(cur, INS, "sh1", "f.jsonl") == 1
    (q, vals), = cur.calls
    txt = _sql_text(q)
    assert "insert into" in txt and "on conflict" in txt and "do update set" in txt
    assert '"fact"."jbq_result"' in txt
    assert "last_seen_at = now()" in txt
    assert "first_seen_at" not in txt
    json_wrapped = [v for v in vals if isinstance(v, Json)]
    assert len(json_wrapped) == 1
    assert json_wrapped[0].obj == {"a": [1]}
    assert vals[-2:] == ["sh1", "f.jsonl"]
    assert vals.count("301") == 1  # 主键只绑一次（values 列，不重复进 SET）


def test_rowcount_zero_returns_zero():
    cur = FakeCur(rowcount=0)
    assert jbq_result_write.upsert_jbq_result_instruction(cur, INS, "s", "f") == 0


def test_rowcount_none_returns_zero():
    assert jbq_result_write.upsert_jbq_result_instruction(FakeCur(None), INS, "s", "f") == 0


def test_unknown_table_valueerror():
    with pytest.raises(ValueError):
        jbq_result_write.upsert_jbq_result_instruction(
            FakeCur(), {"table": "fact.nope", "pk": {}, "row": {}}, "s", "f")


def test_extra_column_valueerror():
    bad = {"table": "fact.jbq_result", "pk": {"match_id": "1"},
           "row": {"match_id": "1", "bogus_col": 1}}
    with pytest.raises(ValueError):
        jbq_result_write.upsert_jbq_result_instruction(FakeCur(), bad, "s", "f")


def test_missing_pk_valueerror():
    bad = {"table": "fact.jbq_result", "pk": {}, "row": {"final_score": "1-0"}}
    with pytest.raises(ValueError):
        jbq_result_write.upsert_jbq_result_instruction(FakeCur(), bad, "s", "f")


def test_empty_string_normalized_to_none():
    ins = {"table": "fact.jbq_result", "pk": {"match_id": "9"},
           "row": {"match_id": "9", "hdc_odds": ""}}
    cur = FakeCur()
    jbq_result_write.upsert_jbq_result_instruction(cur, ins, "s", "f")
    assert cur.calls[0][1] == ["9", None, "s", "f"]


def test_works_without_commit_attribute():
    cur = FakeCur()
    assert not hasattr(cur, "commit")
    assert jbq_result_write.upsert_jbq_result_instruction(cur, INS, "s", "f") == 1
