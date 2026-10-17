"""R46-4: web/services/store.py::pending_predictions_by_league 5 cases direct + D-71 lock.

pending_predictions_by_league (L328-L363, 36 行) 按联赛返回「已预测但尚未
完赛」场次计数, 用于页面空态提示 ("N 场待结算, 首场结束后自动出数"),
替代 accuracy 恒空时无信息量的 "—"。

D-40/D-42/D-49/D-55 已锁 store 多 helper (covers_date / latest_by_league /
_window_dates / results_by_bjt_date), 但 pending_predictions_by_league
自身核心契约 (settled 计数 + 联赛分桶 + score 解析 + 类型兜底) 0 直测。

D-71 锁定 5 契约:
- D-71-1: 主流程 happy - 2 league, epl 有部分 settled (3-1), laliga 全 pending
- D-71-2: 完全无 past_matches (全 pending) -> 所有 keys 计数
- D-71-3: 全部已结算 (predictions key 全在 settled) -> 0
- D-71-4: doc 缺 league / league 非 str -> 跳过该 doc
- D-71-5: score 解析失败 ("2-1" OK, "no_score" / "2" / 1.0 都不算 settled)

依赖 stub:
- web.services.store.load_prediction_docs (monkeypatch 返受控 list[dict])
- web.services.store._stable_match_key 真跑 (R46-1 D-68 已用, home_en|away_en 格式)
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
def store_mod():
    """Spec-load web/services/store.py。

    注意: spec-load 后 importlib 创建新模块, 但 store.py 内部 from import
    会走 sys.modules 中的模块, 这正是我们想要的 (load_prediction_docs +
    _stable_match_key 仍指原 store 模块)。
    """
    spec = importlib.util.spec_from_file_location(
        "store_pending_predictions_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_load_prediction_docs(store_mod, monkeypatch, docs: list):
    """Mock load_prediction_docs 返受控 docs list。"""
    monkeypatch.setattr(store_mod, "load_prediction_docs", lambda: docs)


def _doc(league: str, *, past_matches: list = None, predictions: list = None) -> dict:
    """构造一个 doc dict。"""
    return {
        "path": Path("dummy"),
        "name": "dummy.json",
        "generated_at": "2026-07-15T00:00:00+00:00",
        "data": {
            "league": league,
            "past_matches": past_matches or [],
            "predictions": predictions or [],
        },
        "mtime": 0.0,
    }


def _match(home: str, away: str, score: str = None) -> dict:
    """past_matches 项。"""
    m = {"home_en": home, "away_en": away}
    if score is not None:
        m["score"] = score
    return m


def _pred(home: str, away: str) -> dict:
    """predictions 项。"""
    return {"home_en": home, "away_en": away}


# ──────────────────────── D-71 五契约 ────────────────────────


def test_pending_d71_1_happy_path_partial_settled(store_mod, monkeypatch):
    """R46-4 D-71-1: 主流程 happy - 2 league 部分 settled。

    epl: 3 predictions, 1 已 settled (A vs B 比分 2-1) -> 2 pending
    laliga: 2 predictions, 0 settled (无 past_matches) -> 2 pending
    """
    _stub_load_prediction_docs(store_mod, monkeypatch, [
        _doc("epl",
             past_matches=[_match("A", "B", "2-1")],
             predictions=[_pred("A", "B"), _pred("C", "D"), _pred("E", "F")]),
        _doc("laliga",
             past_matches=[],
             predictions=[_pred("X", "Y"), _pred("U", "V")]),
    ])
    out = store_mod.pending_predictions_by_league()
    assert out == {"epl": 2, "laliga": 2}


def test_pending_d71_2_no_past_matches_all_pending(store_mod, monkeypatch):
    """R46-4 D-71-2: 完全无 past_matches (全 pending) -> 所有 keys 计数。

    2 league, 各 3 predictions, 无 past_matches -> epl=3, laliga=3。
    """
    _stub_load_prediction_docs(store_mod, monkeypatch, [
        _doc("epl", past_matches=[], predictions=[
            _pred("A", "B"), _pred("C", "D"), _pred("E", "F"),
        ]),
        _doc("laliga", past_matches=[], predictions=[
            _pred("X", "Y"), _pred("U", "V"), _pred("P", "Q"),
        ]),
    ])
    out = store_mod.pending_predictions_by_league()
    assert out == {"epl": 3, "laliga": 3}


def test_pending_d71_3_all_settled_returns_zero(store_mod, monkeypatch):
    """R46-4 D-71-3: 全部已结算 -> 0。

    predictions key 全在 settled set -> 0 pending。
    """
    _stub_load_prediction_docs(store_mod, monkeypatch, [
        _doc("epl",
             past_matches=[_match("A", "B", "2-1"), _match("C", "D", "1-0")],
             predictions=[_pred("A", "B"), _pred("C", "D")]),
    ])
    out = store_mod.pending_predictions_by_league()
    assert out == {"epl": 0}


def test_pending_d71_4_skip_docs_without_valid_league(store_mod, monkeypatch):
    """R46-4 D-71-4: doc 缺 league / league 非 str -> 跳过。

    1 doc league=None, 1 doc league='', 1 doc league='epl' -> 只计 epl。
    """
    _stub_load_prediction_docs(store_mod, monkeypatch, [
        _doc(None, predictions=[_pred("X", "Y")]),  # league=None
        _doc("", predictions=[_pred("U", "V")]),  # league=空串
        # league=数字应该被 _doc 强制成 str; 我们手造一个非 str
        {"path": Path("d"), "name": "d.json", "generated_at": "",
         "data": {"league": 123, "predictions": [_pred("Q", "W")]}, "mtime": 0.0},
        _doc("epl", predictions=[_pred("A", "B")]),  # 唯一有效
    ])
    out = store_mod.pending_predictions_by_league()
    # 123 is int, not str -> skip; None / "" 都 skip
    assert out == {"epl": 1}


def test_pending_d71_5_invalid_score_does_not_count_settled(store_mod, monkeypatch):
    """R46-4 D-71-5: score 解析失败 -> 不算 settled。

    L348-L354: "x-y" 形式 + 数字 split 失败 ValueError 跳过。
    "no_score" / "2" / "abc" / 1.0 都不算 settled -> 全部 pending。
    """
    _stub_load_prediction_docs(store_mod, monkeypatch, [
        _doc("epl",
             past_matches=[
                 _match("A", "B", "no_score"),  # 无 '-'
                 _match("C", "D", "2"),  # 无 '-'
                 _match("E", "F", "abc"),  # 无 '-' + 非数字
                 # 数字 split ValueError 路径: "a-b"
                 # 但 "_match('G', 'H', 'a-b')" -> int('a') ValueError -> skip
                 _match("G", "H", "a-b"),
                 # 正常 valid
                 _match("I", "J", "1-0"),
             ],
             predictions=[
                 _pred("A", "B"),
                 _pred("C", "D"),
                 _pred("E", "F"),
                 _pred("G", "H"),
                 _pred("I", "J"),
             ]),
    ])
    out = store_mod.pending_predictions_by_league()
    # 只有 I vs J settled, 其他 4 个 pending
    assert out == {"epl": 4}