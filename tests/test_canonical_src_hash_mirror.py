"""``canonical_src_hash`` 镜像对账测试 — 防 §5.0 幂等键公式 drift。

背景（P4-3）：``scripts/store/parse_values.py::canonical_src_hash`` 与
``scripts/store/upsert_official.py::canonical_src_hash`` 曾是**字节级重复实现**
（一字不差：``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False) → sha256.hexdigest()``）。任何一边公式改了（最常：删除
``sort_keys`` / 改 ``indent`` / 加 ``default=str``），重放幂等键会分裂 → stg
``<src_hash>`` 唯一约束可能允许重复入库。

策略：本测试保证两条 import 路径得到同一函数，并且对若干真实负载产生同一
hash（这两件事一起，未来谁想重新加一份本地副本或改公式，立刻被截断）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest


@pytest.fixture(scope="module")
def parse_values_hash():
    return importlib.import_module("store.parse_values").canonical_src_hash


@pytest.fixture(scope="module")
def upsert_official_hash():
    return importlib.import_module("store.upsert_official").canonical_src_hash


def test_both_paths_resolve_to_same_function(parse_values_hash, upsert_official_hash):
    """``parse_values.canonical_src_hash is upsert_official.canonical_src_hash``。

    公式唯一真源的唯一结构证据：两边模块属性是**同一对象** —— 若有人重新加
    一份本地副本，本断言先跳红（hash 不相等是后话）。
    """
    assert parse_values_hash is upsert_official_hash


@pytest.mark.parametrize(
    "payload",
    [
        # 1. 空 dict
        {},
        # 2. 单层中文键值
        {"home_cn": "皇家马德里", "away_cn": "巴塞罗那"},
        # 3. 嵌套 dict（键有无顺序差异，必须产生同 hash）
        {"a": 1, "b": {"y": 2, "x": 1}, "c": [3, 2, 1]},
        # 4. list-of-dict（jczq_offer 典型形状）
        {
            "snap_ts": "2026-01-15T12:00:00Z",
            "jc_display_num": "7001",
            "play_type": "had",
            "options": [{"o": "3", "v": "2.10"}, {"o": "1", "v": "3.30"}],
        },
        # 5. value 含 None 嵌套
        {"snap_ts": None, "league": "EPL", "score": {"home": None, "away": 2}},
        # 6. 真实 jc_issue 形状
        {
            "issue_no": "26001",
            "game": "90",
            "matches": [
                {"home": "A队", "away": "B队", "ft": "3-1"},
                {"home": "C队", "away": "D队", "ft": "0-0"},
            ],
            "results": [3, 1, 1, 3],
        },
    ],
    ids=["empty", "cn_pairs", "nested_unordered_keys", "list_of_dict",
         "none_nested", "jc_issue_shape"],
)
def test_produces_identical_hash_for_each_payload(parse_values_hash, upsert_official_hash, payload):
    """两个 import 路径对同一负载必须出同一 hex 串。"""
    a = parse_values_hash(payload)
    b = upsert_official_hash(payload)
    assert isinstance(a, str) and len(a) == 64, "sha256 hex 应为 64 字符"
    assert a == b, f"drift: parse_values={a} upsert_official={b}"


def test_key_sorting_invariant(parse_values_hash, upsert_official_hash):
    """键顺序不影响 hash（formula 含 ``sort_keys=True`` 的契约锁）。"""
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert parse_values_hash(a) == parse_values_hash(b) == upsert_official_hash(a)


def test_chinese_ensure_ascii_false(parse_values_hash, upsert_official_hash):
    """中文内容 json.dumps 不转 ``\\uXXXX``（``ensure_ascii=False`` 契约锁）。

    Note: ``"\\u7687..."`` 与 ``"皇家马德里"`` 在 Python 编译期是同一字符串，
    不能借 dict 输入验证；改为直接构造期望的 json 字节序列并独立 sha256，
    再与函数输出对照。
    """
    import hashlib
    import json as _json

    payload = {"home_cn": "皇家马德里"}
    expected = hashlib.sha256(
        _json.dumps(payload, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert parse_values_hash(payload) == expected
    assert upsert_official_hash(payload) == expected
