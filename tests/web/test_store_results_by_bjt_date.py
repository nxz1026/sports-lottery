"""R24 #2: web/services/store.py results_by_bjt_date + _stable_match_key 单测。

results_by_bjt_date 没有直接 unit 测；仅集成测间接覆盖（fixture 构造两条
result_<date>.json + monkeypatch OUTPUT_DIR）。_stable_match_key 更零单测，
但其语义必须与 scripts/core/backtest._bk_stable_key 一致；任何漂移都会让
「待结算」计数与 accuracy 出数口径错位（契约 §1）。

本轮补 9 测，覆盖：
- _stable_match_key 4：英文优先 / 中文 fallback / 全缺空 / 字段为 None
- results_by_bjt_date 5：单日合并 / 双日有 id 去重 / 双日无 id JSON-fallback 去重 /
  双日混合 id 去重 / 文件全缺空列表
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_mod(tmp_path, monkeypatch):
    """monkeypatch OUTPUT_DIR → tmp_path/scripts，回避真实 LP_OUTPUT_DIR。"""
    fake_scripts = tmp_path / "scripts"
    fake_scripts.mkdir()
    (fake_scripts / "results").mkdir()
    (fake_scripts / "predictions").mkdir()
    (fake_scripts / "references").mkdir()

    import web.services.store as store
    monkeypatch.setattr(store, "OUTPUT_DIR", fake_scripts)
    # 缓存失效：pred_index_by_pair/results_for_date 都有 mtime 缓存
    if hasattr(store, "_pred_index_by_pair_cached"):
        store._pred_index_by_pair_cached.clear()
    return store


def _write_result_file(store_mod, day_str: str, entries: list[dict]) -> None:
    """results 契约 §4.1：顶层 dict，matches 字段为 list（results_for_date 读取）。"""
    target = store_mod.OUTPUT_DIR / "results" / f"result_{day_str}.json"
    target.write_text(json.dumps({"matches": entries}, ensure_ascii=False),
                      encoding="utf-8")


# ──────────────────────── _stable_match_key 直接 unit 测 ────────────────────────


def test_stable_match_key_prefers_english_names(store_mod):
    """R24 #2：home_en+away_en 同时非空 → 返回 "Home|En"（优先于中文）。"""
    rec = {"home_en": "Liverpool", "away_en": "Arsenal",
           "name": "利物浦", "match": "利物浦 vs 阿森纳"}
    assert store_mod._stable_match_key(rec) == "Liverpool|Arsenal"


def test_stable_match_key_falls_back_to_chinese_name(store_mod):
    """R24 #2：英文缺一 → 回退 name；name 也缺 → 回退 match。"""
    # home_en 缺 → fall back
    rec = {"home_en": "", "away_en": "Arsenal",
           "name": "利物浦", "match": "x"}
    assert store_mod._stable_match_key(rec) == "利物浦"
    # name 也缺 → fall back to match
    rec2 = {"home_en": "", "away_en": "", "name": "", "match": "match-string"}
    assert store_mod._stable_match_key(rec2) == "match-string"


def test_stable_match_key_returns_empty_when_all_missing(store_mod):
    """R24 #2：所有字段都空/缺失 → 返回空字符串（不抛穿）。"""
    assert store_mod._stable_match_key({}) == ""
    # 字段存在但 strip 后空 → 返回空字符串
    rec = {"home_en": "   ", "away_en": "\t", "name": "", "match": None}
    assert store_mod._stable_match_key(rec) == ""


def test_stable_match_key_handles_none_field_values(store_mod):
    """R24 #2：字段值是 None（JSON null 解析后）→ `None or ""` 短路为空字符串，跳过英文分支走 fallback。"""
    rec = {"home_en": None, "away_en": None, "name": "中文队", "match": None}
    # home_en/away_en 解析后 `None or ""` → ""，进入 name fallback 分支
    assert store_mod._stable_match_key(rec) == "中文队"
    # 都缺 → 空字符串
    assert store_mod._stable_match_key({"home_en": None, "away_en": None}) == ""


# ──────────────────────── results_by_bjt_date 业务流测 ────────────────────────


def test_results_by_bjt_date_only_today_file(store_mod):
    """R24 #2：BJT 当日 result 文件存在 → 返回该日全部条目。"""
    today = date(2026, 10, 13)
    entries = [
        {"id": "m1", "home": "A", "away": "B", "score": "2-1"},
        {"id": "m2", "home": "C", "away": "D", "score": "0-0"},
    ]
    _write_result_file(store_mod, today.isoformat(), entries)

    out = store_mod.results_by_bjt_date(today)
    assert len(out) == 2
    assert {m["id"] for m in out} == {"m1", "m2"}


def test_results_by_bjt_date_deduplicates_by_id(store_mod):
    """R24 #2：双日同 id → 去重（先 today 后 prev_day 时 today 优先）。"""
    today = date(2026, 10, 13)
    prev = today - timedelta(days=1)
    # 同 id 出现两天，score 略不同（today 是更新的版本）
    today_entries = [{"id": "m1", "score": "3-2"}, {"id": "m2", "score": "1-1"}]
    prev_entries = [{"id": "m1", "score": "3-1"}, {"id": "m3", "score": "2-2"}]
    _write_result_file(store_mod, today.isoformat(), today_entries)
    _write_result_file(store_mod, prev.isoformat(), prev_entries)

    out = store_mod.results_by_bjt_date(today)
    # m1 只保留一份（today 版）；m2 + m3 各一份
    assert len(out) == 3
    seen_ids = [m["id"] for m in out]
    assert seen_ids.count("m1") == 1
    # today 版本先入 seen → 保留的是 today score=3-2
    m1 = next(m for m in out if m["id"] == "m1")
    assert m1["score"] == "3-2"


def test_results_by_bjt_date_dedup_falls_back_to_json_when_no_id(store_mod):
    """R24 #2：双日都没 id 但 record 内容相同 → JSON-fingerprint 去重。"""
    today = date(2026, 10, 13)
    prev = today - timedelta(days=1)
    # 完全相同的 dict（无 id）出现在两天文件中
    dup = {"home": "A", "away": "B", "score": "2-1", "kickoff_utc": "2026-10-12T20:00:00+00:00"}
    today_entries = [dict(dup), {"home": "C", "away": "D", "score": "0-0"}]
    prev_entries = [dict(dup)]  # 字典内容完全相同
    _write_result_file(store_mod, today.isoformat(), today_entries)
    _write_result_file(store_mod, prev.isoformat(), prev_entries)

    out = store_mod.results_by_bjt_date(today)
    # dup 只保留一份（today 版，因 json.dumps sort_keys=true 指纹一致）
    home_away_pairs = [(m.get("home"), m.get("away")) for m in out]
    assert home_away_pairs.count(("A", "B")) == 1
    assert ("C", "D") in home_away_pairs
    # 总计 2 条（dup 去重 + C/D 新增）
    assert len(out) == 2


def test_results_by_bjt_date_mixed_id_and_no_id(store_mod):
    """R24 #2：双日混合（有 id + 无 id）→ 都正确去重。"""
    today = date(2026, 10, 13)
    prev = today - timedelta(days=1)
    today_entries = [
        {"id": "x1", "home": "A", "away": "B"},  # 有 id
        {"home": "P", "away": "Q"},  # 无 id
    ]
    prev_entries = [
        {"id": "x1", "home": "A2", "away": "B2"},  # 同 id 但不同内容（id 命中去重）
        {"home": "P", "away": "Q"},  # 无 id，JSON 指纹与 today 相同
    ]
    _write_result_file(store_mod, today.isoformat(), today_entries)
    _write_result_file(store_mod, prev.isoformat(), prev_entries)

    out = store_mod.results_by_bjt_date(today)
    # x1：id 命中去重，保留 today 版（home=A）
    # P/Q：JSON 指纹命中去重，保留 today 版
    assert len(out) == 2
    ids = [m.get("id") for m in out]
    assert ids.count("x1") == 1
    assert ids.count(None) == 1


def test_results_by_bjt_date_returns_empty_when_no_files(store_mod):
    """R24 #2：双日 result 文件全缺 → 返回空列表（不抛穿）。"""
    today = date(2026, 10, 13)
    assert store_mod.results_by_bjt_date(today) == []
