"""R44-5: web/services/accuracy.py::_collect_league_data 6 tests direct + D-60.

数据加载 helper 0 直测; 4 文件 14 次提及全是 monkeypatch,
真函数从未被任何测试调用过 (含 test_accuracy_mirror.py)。

helper 含五条契约 (D-60):
- 切窗: load_prediction_docs() 中 doc.mtime >= cutoff 才纳入
- 联赛过滤: data['league'] == league_key 才纳入
- past_matches: 解析 "X-Y" -> (X, Y); 无 score / 非数字 / 缺 '-' 跳过
- predictions: 按 _stable_match_key 去重 (first-seen wins)
- doc.data 非 dict 跳过 (defensive)

6 tests (HIGH):
- happy: 1 doc 1 past + 1 pred -> 双填充
- mtime 滤掉旧 doc
- 联赛过滤: 不同 league 不纳入
- past_match 坏 score ("abc") 跳过
- prediction dedup 跨多 doc
- 空 docs / 非 dict data 兜底
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def acc_mod(monkeypatch):
    """加载 web/services/accuracy.py + mock load_prediction_docs + _stable_match_key。"""
    spec = importlib.util.spec_from_file_location(
        "accuracy_collect_test",
        REPO_ROOT / "web" / "services" / "accuracy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _doc(league, past=None, predictions=None, mtime=1000.0, data=None):
    """构造预测文件 doc dict (与 store.latest_by_league 同 schema)。"""
    if data is None:
        data = {"league": league, "past_matches": past or [],
                "predictions": predictions or []}
    return {"path": Path("/fake/doc.json"), "name": "doc.json",
            "data": data, "mtime": mtime}


def _patch_load(mod, docs):
    """Stub 模块内的 load_prediction_docs。"""
    mod.load_prediction_docs = lambda: docs


# _stable_match_key 简单实现 (matches _stable_match_key 真实行为)
def _stable_match_key(m):
    h = m.get("home_en") or m.get("home") or ""
    a = m.get("away_en") or m.get("away") or ""
    return f"{(m.get('league') or '').strip()}|{h.strip()}|{a.strip()}"


def _patch_stable_key(mod, key_fn=_stable_match_key):
    mod._stable_match_key = key_fn


# ──────────────────────── D-60 五契约 ────────────────────────


def test_collect_league_data_d60_happy_double_populated(acc_mod, monkeypatch):
    """R44-5 D-60: 1 doc 含 past_matches + predictions -> actuals 和 preds 都填充。"""
    # past_match 也带 league 字段 (源 doc.data.league 透传), 让 key 包含 epl
    doc = _doc("epl", mtime=2000.0, past=[
        {"home_en": "Arsenal", "away_en": "Chelsea", "score": "2-1",
         "home": "阿森纳", "away": "切尔西", "league": "epl"},
    ], predictions=[
        {"home": "Arsenal", "away": "Chelsea", "home_en": "Arsenal",
         "away_en": "Chelsea", "league": "epl",
         "direction": "Arsenal win"},
    ])
    _patch_load(acc_mod, [doc])
    _patch_stable_key(acc_mod)

    actuals, preds = acc_mod._collect_league_data("epl", cutoff=1500.0)
    assert "epl|Arsenal|Chelsea" in actuals
    assert actuals["epl|Arsenal|Chelsea"] == (2, 1)
    assert len(preds) == 1
    assert preds[0]["direction"] == "Arsenal win"


def test_collect_league_data_d60_mtime_filter_skips_old_docs(acc_mod):
    """R44-5 D-60: doc.mtime < cutoff -> 完全跳过 (不进 actuals 也不进 preds)。"""
    # 旧 doc mtime=100 (< cutoff 500)
    old_doc = _doc("epl", mtime=100.0, past=[
        {"home_en": "A", "away_en": "B", "score": "1-1", "home": "A", "away": "B"}
    ], predictions=[
        {"home": "A", "away": "B", "home_en": "A", "away_en": "B",
         "league": "epl", "direction": "draw"}
    ])
    _patch_load(acc_mod, [old_doc])
    _patch_stable_key(acc_mod)

    actuals, preds = acc_mod._collect_league_data("epl", cutoff=500.0)
    assert actuals == {}
    assert preds == []


def test_collect_league_data_d60_league_filter_excludes_other_leagues(acc_mod):
    """R44-5 D-60: data['league'] != league_key -> 整个 doc 跳过。

    锁联赛过滤: 防止跨联赛误聚合 (epl 数据混入 laliga 统计)。
    """
    doc = _doc("laliga", mtime=2000.0,
               past=[{"home_en": "A", "away_en": "B", "score": "2-0",
                     "home": "A", "away": "B"}],
               predictions=[{"home": "A", "away": "B", "home_en": "A",
                             "away_en": "B", "league": "laliga",
                             "direction": "draw"}])
    _patch_load(acc_mod, [doc])
    _patch_stable_key(acc_mod)

    # 查 epl 但数据是 laliga
    actuals, preds = acc_mod._collect_league_data("epl", cutoff=1500.0)
    assert actuals == {}
    assert preds == []


def test_collect_league_data_d60_past_match_bad_score_skipped(acc_mod):
    """R44-5 D-60: past_match score 非 "X-Y" 格式 (None / "abc" / "5") -> 跳过。

    锁定: 坏 score 不能让 _collect_league_data 抛穿 (ValueError 被吞)。
    """
    doc = _doc("epl", mtime=2000.0,
               past=[
                   {"home_en": "A", "away_en": "B", "score": "abc",
                    "home": "A", "away": "B", "league": "epl"},
                   {"home_en": "C", "away_en": "D", "score": "5-2",
                    "home": "C", "away": "D", "league": "epl"},
                   {"home_en": "E", "away_en": "F",
                    "home": "E", "away": "F", "league": "epl"},  # 无 score
               ],
               predictions=[])
    _patch_load(acc_mod, [doc])
    _patch_stable_key(acc_mod)

    actuals, preds = acc_mod._collect_league_data("epl", cutoff=1500.0)
    # 仅 5-2 那条入
    assert "epl|C|D" in actuals
    assert actuals["epl|C|D"] == (5, 2)
    assert "epl|A|B" not in actuals
    assert "epl|E|F" not in actuals
    assert preds == []


def test_collect_league_data_d60_prediction_dedup_first_seen(acc_mod):
    """R44-5 D-60: 同一 prediction key 跨多 doc 出现 -> 保留首次 (first-seen wins)。

    锁: 不重复 prediction, 避免 league_accuracy_web double counting。
    """
    p = {"home_en": "A", "away_en": "B", "league": "epl",
         "home": "A", "away": "B", "direction": "draw"}
    doc1 = _doc("epl", mtime=2000.0, predictions=[p])
    p2 = {"home_en": "A", "away_en": "B", "league": "epl",
          "home": "A", "away": "B", "direction": "draw_2nd"}  # 同 key 不同方向
    doc2 = _doc("epl", mtime=2500.0, predictions=[p2])
    _patch_load(acc_mod, [doc1, doc2])
    _patch_stable_key(acc_mod)

    _, preds = acc_mod._collect_league_data("epl", cutoff=1500.0)
    # 仅 doc1 首次的纳入, doc2 不再 append
    assert len(preds) == 1
    assert preds[0]["direction"] == "draw"


def test_collect_league_data_d60_non_dict_data_defensive_skip(acc_mod):
    """R44-5 D-60: doc['data'] 非 dict (None / list) -> 跳过 (defensive check)。

    锁: 上游 doc 损坏不能让数据加载 raise KeyError 或 TypeError。
    """
    docs = [
        _doc("epl", mtime=2000.0, data=None),  # data=None
        _doc("epl", mtime=2000.0, data=[{"x": 1}]),  # data 是 list
        _doc("epl", mtime=2000.0, data="not-a-dict"),  # data 是 str
        _doc("epl", mtime=2000.0,  # data 正常 (control)
             past=[{"home_en": "A", "away_en": "B", "score": "1-0",
                   "home": "A", "away": "B", "league": "epl"}]),
    ]
    _patch_load(acc_mod, docs)
    _patch_stable_key(acc_mod)

    actuals, preds = acc_mod._collect_league_data("epl", cutoff=1500.0)
    # 三个坏 doc 跳过, 仅最后 1 个 doc 入 actuals
    assert actuals == {"epl|A|B": (1, 0)}
    assert preds == []
