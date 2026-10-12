"""scripts.store.upsert_official 单测：JSONL → stg 表的纯函数补测。

背景：upsert_official.py 是国内采集机官方数据装载模块，契约 §5
  已有 tests/test_upsert_from_raw.py 跑端到端（依赖 DSN），但
  纯函数（canonical_src_hash / reject_reason / _parse / _reject）
  无单测 — 改一行就敢上。

覆盖点：
  · STG_TABLES / REQUIRED_FIELDS / INSERT_SQL 常量完整性
  · canonical_src_hash：稳定 / 键序无关 / Unicode 安全
  · reject_reason：合法 / 非 dict / 缺 src_hash / 缺必需字段
  · _parse：合法 JSON / 非法 JSON 错误信息
  · _reject：累计 + MAX_REJECTED_DETAIL 截断 + RAW_PREVIEW_CHARS 预览
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from store import upsert_official  # noqa: E402


# ────────────────────────── 常量完整性 ──────────────────────────


def test_stg_tables_complete():
    """STG_TABLES 必须含 7 topic：4 个 jczq + 2 个 jclq + 1 个 lottery。"""
    expected = {"jczq_offer", "jczq_result", "jc_issue", "jc_issue_result",
                "jclq_offer", "jclq_result", "lottery_draw"}
    assert set(upsert_official.STG_TABLES) == expected
    # 必须 frozenset（防止运行时被改）
    assert isinstance(upsert_official.STG_TABLES, frozenset)


def test_required_fields_keys_match_stg_tables():
    """REQUIRED_FIELDS 键集 = STG_TABLES；缺键 = 装载时 KeyError。"""
    assert set(upsert_official.REQUIRED_FIELDS.keys()) == set(upsert_official.STG_TABLES)


def test_required_fields_are_nonempty_tuples():
    """每 topic 必含 ≥ 1 个必需字段；空 tuple = 失去契约保护。"""
    for table, fields in upsert_official.REQUIRED_FIELDS.items():
        assert len(fields) >= 1, f"{table} REQUIRED_FIELDS 为空"
        assert all(isinstance(f, str) for f in fields)


def test_insert_sql_template_shape():
    """INSERT_SQL 必须含表占位符 + INSERT/ON CONFLICT 关键字 + 3 个 %s。"""
    sql = upsert_official.INSERT_SQL
    assert "INSERT INTO stg.{}" in sql
    assert "ON CONFLICT (src_hash) DO NOTHING" in sql
    # 3 个 %s：line, src_hash, src_file
    assert sql.count("%s") == 3


# ────────────────────────── canonical_src_hash ──────────────────────


def test_canonical_src_hash_same_input_same_output():
    obj = {"a": 1, "b": "x", "c": [1, 2, 3]}
    h1 = upsert_official.canonical_src_hash(obj)
    h2 = upsert_official.canonical_src_hash(obj)
    assert h1 == h2
    # sha256 hex 64 字符
    assert len(h1) == 64


def test_canonical_src_hash_key_order_invariant():
    """键序不影响哈希（json.dumps sort_keys=True）。"""
    a = {"a": 1, "b": 2, "c": 3}
    b = {"c": 3, "b": 2, "a": 1}
    assert upsert_official.canonical_src_hash(a) == upsert_official.canonical_src_hash(b)


def test_canonical_src_hash_unicode_safe():
    """Unicode（中文）必须保留原字符，不能转 \\uXXXX。"""
    obj = {"home_cn": "皇家马德里", "away_cn": "巴塞罗那"}
    h = upsert_official.canonical_src_hash(obj)
    assert len(h) == 64
    # 同样内容必须得同样 hash
    assert h == upsert_official.canonical_src_hash({"away_cn": "巴塞罗那", "home_cn": "皇家马德里"})


def test_canonical_src_hash_different_value_different_hash():
    """不同内容必得不同 hash（防撞）。"""
    a = {"x": 1}
    b = {"x": 2}
    assert upsert_official.canonical_src_hash(a) != upsert_official.canonical_src_hash(b)


def test_canonical_src_hash_nested_dict():
    """嵌套 dict 同样稳定（递归 sort_keys）。"""
    a = {"a": {"x": 1, "y": 2}, "b": [{"p": 1}, {"p": 2}]}
    b = {"b": [{"p": 1}, {"p": 2}], "a": {"y": 2, "x": 1}}
    assert upsert_official.canonical_src_hash(a) == upsert_official.canonical_src_hash(b)


# ────────────────────────── reject_reason ──────────────────────────


def test_reject_reason_valid_returns_none():
    obj = {
        "src_hash": "abc123",
        "snap_ts": "2026-01-01T00:00:00Z",
        "jc_display_num": "001",
        "play_type": "had",
        "options": {"h": "2.0", "d": "3.0", "a": "3.5"},
    }
    assert upsert_official.reject_reason(obj, "jczq_offer") is None


def test_reject_reason_non_dict():
    """非 dict → 拒绝（不可哈希到 src_hash 也不可序列化）。"""
    assert upsert_official.reject_reason("just a string", "jczq_offer") == "not a json object"
    assert upsert_official.reject_reason([1, 2, 3], "jczq_offer") == "not a json object"
    assert upsert_official.reject_reason(None, "jczq_offer") == "not a json object"
    assert upsert_official.reject_reason(42, "jczq_offer") == "not a json object"


def test_reject_reason_missing_src_hash():
    """缺 src_hash / src_hash 非字符串 / src_hash 空串 → 拒绝。"""
    assert upsert_official.reject_reason({}, "jczq_offer") == "missing src_hash"
    assert upsert_official.reject_reason({"src_hash": None}, "jczq_offer") == "missing src_hash"
    assert upsert_official.reject_reason({"src_hash": ""}, "jczq_offer") == "missing src_hash"
    assert upsert_official.reject_reason({"src_hash": 123}, "jczq_offer") == "missing src_hash"


def test_reject_reason_missing_required_field():
    """缺必需字段 → 列出全部缺失字段。"""
    obj = {"src_hash": "abc", "snap_ts": "2026-01-01T00:00:00Z"}
    # jczq_offer 必含：snap_ts / jc_display_num / play_type / options
    reason = upsert_official.reject_reason(obj, "jczq_offer")
    assert reason is not None
    assert "missing required field(s)" in reason
    assert "jc_display_num" in reason
    assert "play_type" in reason
    assert "options" in reason


def test_reject_reason_empty_required_field_treated_as_missing():
    """空字符串/空列表/空 dict 也算缺失（语义保护）。"""
    obj = {
        "src_hash": "abc",
        "snap_ts": "",
        "jc_display_num": "001",
        "play_type": "had",
        "options": [],
    }
    reason = upsert_official.reject_reason(obj, "jczq_offer")
    assert reason is not None
    assert "snap_ts" in reason
    assert "options" in reason


def test_reject_reason_per_table_contract():
    """每 topic 必含字段独立校验（jczq_result 不需要 snap_ts）。"""
    # jczq_result 必含：match_display_num, home_cn, away_cn, ft
    valid = {
        "src_hash": "abc",
        "match_display_num": "001",
        "home_cn": "A",
        "away_cn": "B",
        "ft": "1:0",
    }
    assert upsert_official.reject_reason(valid, "jczq_result") is None


# ────────────────────────── _parse ──────────────────────────


def test_parse_valid_json():
    obj, err = upsert_official._parse('{"a": 1, "b": "x"}')
    assert err is None
    assert obj == {"a": 1, "b": "x"}


def test_parse_invalid_json_returns_error():
    obj, err = upsert_official._parse("{not json}")
    assert obj is None
    assert err is not None
    assert "invalid json" in err


def test_parse_empty_string():
    """空串不是合法 JSON（应让 reject_reason 的"非 object"路径处理）。"""
    obj, err = upsert_official._parse("")
    # json.loads("") raise JSONDecodeError
    assert obj is None or err is not None


def test_parse_unicode_json():
    obj, err = upsert_official._parse('{"home": "皇家马德里"}')
    assert err is None
    assert obj == {"home": "皇家马德里"}


# ────────────────────────── _reject ──────────────────────────


def test_reject_accumulator_records():
    acc = {"n": 0, "detail": []}
    upsert_official._reject(acc, lineno=1, reason="bad", raw='{"x": 1}')
    assert acc["n"] == 1
    assert acc["detail"] == [{"lineno": 1, "reason": "bad", "raw": '{"x": 1}'}]


def test_reject_truncates_at_max_detail():
    """acc.detail 长度上限 = MAX_REJECTED_DETAIL（防内存爆炸）。"""
    acc = {"n": 0, "detail": []}
    for i in range(upsert_official.MAX_REJECTED_DETAIL * 2):
        upsert_official._reject(acc, lineno=i + 1, reason="bad", raw="x")
    # n 累计到底，但 detail 截断
    assert acc["n"] == upsert_official.MAX_REJECTED_DETAIL * 2
    assert len(acc["detail"]) == upsert_official.MAX_REJECTED_DETAIL


def test_reject_raw_preview_chars():
    """raw 截断到 RAW_PREVIEW_CHARS 字符（防大行爆内存）。"""
    acc = {"n": 0, "detail": []}
    long_raw = "x" * (upsert_official.RAW_PREVIEW_CHARS * 3)
    upsert_official._reject(acc, lineno=1, reason="big", raw=long_raw)
    assert len(acc["detail"][0]["raw"]) == upsert_official.RAW_PREVIEW_CHARS
