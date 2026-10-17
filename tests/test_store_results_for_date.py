"""R48-2: web/services/store.py::results_for_date 3 cases direct + D-84 lock.

results_for_date (L273-L279, 7 行) 读 results/result_{date}.json (文件名
UTC 口径, 契约 §4.1)。R43-6 D-55 已锁 results_by_bjt_date (BJT-aware),
但 results_for_date (UTC 单日简单读取) 自身核心契约 (file 存在 + 坏 JSON
+ matches 非 list 兜底) 0 直测。

D-84 锁定 3 契约:
- D-84-1: 主流程 happy (data['matches']=[{...}, ...]) -> 返 list
- D-84-2: 文件不存在 / 坏 JSON (_json_load 返 None) -> []
- D-84-3: matches 非 list (缺键 / 字符串 / dict) -> []

依赖 stub:
- OUTPUT_DIR (monkeypatch 指向 tmp_path/results)
- _json_load 真跑 (D-31 已锁, 真读)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_setup(tmp_path, monkeypatch):
    """Spec-load store.py + 重定向 OUTPUT_DIR + 清缓存。"""
    spec = importlib.util.spec_from_file_location(
        "store_results_for_date_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)
    if hasattr(mod, "_load_prediction_docs_cached"):
        mod._load_prediction_docs_cached.clear()
    if hasattr(mod, "_pred_index_by_pair_cached"):
        mod._pred_index_by_pair_cached.clear()
    return mod, tmp_path


# ──────────────────────── D-84 三契约 ────────────────────────


def test_results_for_date_d84_1_happy_returns_matches_list(store_setup):
    """R48-2 D-84-1: 主流程 happy - data['matches'] 返 list。

    写 tmp/results/result_2026-07-15.json + matches list -> 返该 list。
    """
    mod, tmp = store_setup
    (tmp / "results").mkdir()
    matches = [{"id": 1, "score": "2-1"}, {"id": 2, "score": "0-0"}]
    (tmp / "results" / "result_2026-07-15.json").write_text(
        json.dumps({"matches": matches}), encoding="utf-8")

    out = mod.results_for_date("2026-07-15")
    assert out == matches


def test_results_for_date_d84_2_missing_file_returns_empty(store_setup):
    """R48-2 D-84-2: 文件不存在 / 坏 JSON -> [] (L276-277 兜底)。

    _json_load 对不存在/坏文件返 None (D-31 锁定), results_for_date 返 []。
    """
    mod, tmp = store_setup
    (tmp / "results").mkdir()
    # 文件不存在
    assert mod.results_for_date("2026-07-15") == []
    # 文件存在但坏 JSON
    (tmp / "results" / "result_2026-07-16.json").write_text(
        "not valid json {{", encoding="utf-8")
    assert mod.results_for_date("2026-07-16") == []


def test_results_for_date_d84_3_matches_not_list_returns_empty(store_setup):
    """R48-2 D-84-3: matches 非 list (缺键 / 字符串 / dict) -> [] (L279 兜底)。

    data 缺 'matches' / matches 是字符串 / dict -> 不是 list -> 返 []。
    """
    mod, tmp = store_setup
    (tmp / "results").mkdir()
    # 缺 'matches' 键
    (tmp / "results" / "result_d1.json").write_text(
        json.dumps({"other_key": []}), encoding="utf-8")
    assert mod.results_for_date("d1") == []
    # matches 是字符串
    (tmp / "results" / "result_d2.json").write_text(
        json.dumps({"matches": "not a list"}), encoding="utf-8")
    assert mod.results_for_date("d2") == []
    # matches 是 dict
    (tmp / "results" / "result_d3.json").write_text(
        json.dumps({"matches": {"a": 1}}), encoding="utf-8")
    assert mod.results_for_date("d3") == []
    # matches 是 null
    (tmp / "results" / "result_d4.json").write_text(
        json.dumps({"matches": None}), encoding="utf-8")
    assert mod.results_for_date("d4") == []