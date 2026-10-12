"""P2-18 _upsert_helper 单元测试：守住"三道闸校验 + SQL 模板 + bind_values"。

子任务审计遗留 #1：``scripts/ingest/_upsert_helper`` 从 jc_issue_write /
jbq_result_write 抽出，参数化测试断言：

1. 校验逻辑：三道闸（未知表 / 未登记列 / 主键缺列）全 ValueError
2. SQL 模板：包含 ``insert into / on conflict / do update set /
   last_seen_at = now()``；不含 ``first_seen_at``（首见时间由 DDL 管）
3. bind_values：jsonb 列走 ``psycopg.Json`` 包裹，源列追加 src_hash/src_file
4. 与两个 caller（jc_issue_write / jbq_result_write）共享的"列定义格式"：
   jc 5 张表 + jbq 1 张表都能 validate + build_sql
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from psycopg.types.json import Json

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class FakeCur:
    """FakeCur：记录 execute 入参；不真连数据库。"""
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.calls = []

    def execute(self, query, vals):
        self.calls.append((query, vals))


def _sql_text(query) -> str:
    return query.as_string(None)


# ---------- validate 三道闸 ----------

def test_validate_unknown_table_raises():
    """未知表 → ValueError。"""
    from ingest._upsert_helper import validate

    with pytest.raises(ValueError, match="未知写指令目标表"):
        validate("fact.nope_table", {"fact.x": ("a",)}, {"a": 1}, ("a",))


def test_validate_extra_column_raises():
    """含未登记列 → ValueError。"""
    from ingest._upsert_helper import validate

    with pytest.raises(ValueError, match="未登记列"):
        validate("fact.x", {"fact.x": ("a",)}, {"a": 1, "extra": 2}, ("a",))


def test_validate_missing_pk_column_raises():
    """主键缺列 → ValueError。"""
    from ingest._upsert_helper import validate

    with pytest.raises(ValueError, match="主键缺列"):
        validate("fact.x", {"fact.x": ("a", "b")}, {"a": 1}, ("a", "b"))


def test_validate_empty_string_normalized_to_none():
    """空串归一（""→None）。"""
    from ingest._upsert_helper import validate

    out = validate("fact.x", {"fact.x": ("a", "b")}, {"a": "", "b": "v"}, ("a", "b"))
    assert out == {"a": None, "b": "v"}


def test_validate_pass_through_returns_row():
    """校验通过 → 返回原 row（含归一）。"""
    from ingest._upsert_helper import validate

    row = {"a": "1", "b": "v"}
    out = validate("fact.x", {"fact.x": ("a", "b")}, row, ("a", "b"))
    assert out == row


# ---------- build_upsert_sql ----------

def test_build_sql_contains_expected_clauses():
    """SQL 含 insert / on conflict / do update set / last_seen_at = now()。"""
    from ingest._upsert_helper import build_upsert_sql

    sql_obj = build_upsert_sql("fact.x", ["a", "b"], {"a": "1"})
    txt = _sql_text(sql_obj)
    assert "insert into" in txt
    assert "on conflict" in txt
    assert "do update set" in txt
    assert "last_seen_at = now()" in txt


def test_build_sql_excludes_first_seen_at():
    """first_seen_at 不进 SET（DDL 默认值管首见时间）。"""
    from ingest._upsert_helper import build_upsert_sql

    sql_obj = build_upsert_sql("fact.x", ["a", "b", "first_seen_at"],
                                {"a": "1", "first_seen_at": "x"})
    txt = _sql_text(sql_obj)
    # PK 列不进 SET 子句
    assert "EXCLUDED.first_seen_at" not in txt
    # first_seen_at 也不出现在 SET 子句右侧
    assert "first_seen_at = EXCLUDED" not in txt


def test_build_sql_schema_name_two_arg_identifier():
    """``sql.Identifier(schema, name)`` 两参拆开：``"fact"."x"`` 而非 ``"fact.x"``。"""
    from ingest._upsert_helper import build_upsert_sql

    sql_obj = build_upsert_sql("fact.x", ["a"], {"a": "1"})
    txt = _sql_text(sql_obj)
    assert '"fact"."x"' in txt
    assert '"fact.x"' not in txt


def test_build_sql_pk_columns_sorted_for_conflict_key():
    """冲突键 ``on conflict`` 按 sorted(pk) 排序。"""
    from ingest._upsert_helper import build_upsert_sql

    sql_obj = build_upsert_sql("fact.x", ["a", "b"], {"b": "2", "a": "1"})
    txt = _sql_text(sql_obj)
    # sorted({"a": "1", "b": "2"}) = ["a", "b"]
    assert '"a"' in txt and '"b"' in txt
    # on conflict 子句里 a 在 b 前面
    on_conflict_idx = txt.index("on conflict (")
    after_conflict = txt[on_conflict_idx:]
    a_idx = after_conflict.index('"a"')
    b_idx = after_conflict.index('"b"')
    assert a_idx < b_idx


# ---------- bind_values ----------

def test_bind_values_appends_src_hash_src_file():
    """bind_values 把 src_hash + src_file 追加到 cols/vals 末尾。"""
    from ingest._upsert_helper import bind_values

    cols, vals = bind_values({"a": 1, "b": "v"}, frozenset(), "sh", "f.jsonl")
    assert cols == ["a", "b", "src_hash", "src_file"]
    assert vals == [1, "v", "sh", "f.jsonl"]


def test_bind_values_wraps_jsonb_columns():
    """jsonb 列走 ``psycopg.types.json.Json`` 包裹。"""
    from ingest._upsert_helper import bind_values

    cols, vals = bind_values(
        {"a": "x", "j": {"k": 1}},
        frozenset({"j"}),
        "sh", "f.jsonl",
    )
    json_wrapped = [v for v in vals if isinstance(v, Json)]
    assert len(json_wrapped) == 1
    assert json_wrapped[0].obj == {"k": 1}
    # a 列保持原值
    assert vals[cols.index("a")] == "x"


# ---------- 与 caller 一致：jc + jbq 真实 COLUMNS ----------

def test_jc_issue_write_columns_validate_cleanly():
    """``ingest.jc_issue_write.COLUMNS`` 5 张表都能 validate 通过（已知合法行）。"""
    from ingest import jc_issue_write as w

    # fact.jc_issue 最小合法行
    rows = {
        "fact.jc_issue": {"game_num": "2024-001", "issue_no": "24001",
                          "game_name": "胜负彩", "sale_begin": "2024-01-01",
                          "sale_end": "2024-01-02", "draw_at": "2024-01-03",
                          "n_matches": 14},
        "fact.jc_issue_match": {"game_num": "2024-001", "issue_no": "24001",
                                "seq": 1, "gm_match_id": "M001"},
        "fact.jc_issue_prize": {"game_num": "2024-001", "issue_no": "24001",
                                "tier_no": 1, "prize_level": "一等奖"},
    }
    for table, row in rows.items():
        out = w._validate(table, row)
        assert out["game_num"] == row["game_num"]


def test_jbq_result_write_columns_validate_cleanly():
    """``ingest.jbq_result_write.COLUMNS`` 唯一表能 validate 通过。"""
    from ingest import jbq_result_write as w

    pk = {"match_id": "301"}
    row = {"match_id": "301", "final_score": "88-92", "status": "sold"}
    out = w._validate("fact.jbq_result", pk, row)
    assert out["match_id"] == "301"


# ---------- 端到端：两个 caller 的 upsert 行为不变 ----------

def test_jbq_upsert_emits_same_sql_pattern_as_before():
    """jbq_result_write.upsert_jbq_result_instruction 输出 SQL 含期望子句 + bind jsonb。"""
    from ingest import jbq_result_write as w

    cur = FakeCur(rowcount=1)
    ins = {"table": "fact.jbq_result", "pk": {"match_id": "301"}, "notes": [],
           "row": {"match_id": "301", "final_score": "88-92",
                   "raw_blocks": {"a": [1]}}}
    assert w.upsert_jbq_result_instruction(cur, ins, "sh1", "f.jsonl") == 1
    (q, vals), = cur.calls
    txt = _sql_text(q)
    assert "insert into" in txt and "on conflict" in txt and "do update set" in txt
    assert '"fact"."jbq_result"' in txt
    assert "last_seen_at = now()" in txt
    assert "first_seen_at" not in txt
    json_wrapped = [v for v in vals if isinstance(v, Json)]
    assert len(json_wrapped) == 1
    assert vals[-2:] == ["sh1", "f.jsonl"]


def test_jc_upsert_prize_uses_pure_insert():
    """jc_issue_prize append-only：走纯 INSERT 而非 UPSERT（无 on conflict）。"""
    from ingest import jc_issue_write as w

    cur = FakeCur(rowcount=1)
    out = w.upsert_issue_instruction(
        cur,
        {"table": "fact.jc_issue_prize", "pk": {"game_num": "X", "issue_no": "Y",
                                                  "tier_no": 1},
         "notes": [],
         "row": {"game_num": "X", "issue_no": "Y", "tier_no": 1,
                 "prize_level": "一等奖", "stake_count": 1}},
        "sh", "f.jsonl",
    )
    assert out == 1
    (q, vals), = cur.calls
    txt = _sql_text(q)
    assert "insert into" in txt
    assert "on conflict" not in txt  # append-only：无冲突处理
    assert "last_seen_at = now()" not in txt  # append-only：也无 last_seen_at
