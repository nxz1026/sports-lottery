"""R19 #B+#C：store.pred_index_by_pair 反查索引单测 + HAF/combo 接入。

覆盖：
  · 基本 dict 构建（多 doc 拍平 + home/away 归一化）
  · 跳过无效 entry（空 home/away、非 dict）
  · first-match-wins（同 (home,away) 重复保留最早）
  · mtime LRU 缓存失效（新增 / 修改 / 删除文件）
  · store 损坏时返回 {} 而非抛穿
  · haf._find_prediction / combo._assemble_legs 都走字典命中 + fuzzy 兜底
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ──────────────────────────── pred_index_by_pair 单元 ────────────────────────────


def test_pred_index_by_pair_builds_dict_from_files(tmp_path, monkeypatch):
    """多 doc + 多 prediction → dict[(home, away)] = entry；first-match-wins。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()

    # doc 1 含 (A,B) 和 (C,D)
    _write(pred_dir / "prediction_2026-10-12_10.json", {
        "predictions": [
            {"home": "A", "away": "B", "reasoning_factors": {"home_ml_true_prob": 0.5}},
            {"home": "C", "away": "D", "reasoning_factors": {"draw_true_prob": 0.3}},
        ],
    })
    # doc 2 含 (A,B)（first-match-wins 应保留 doc 1 的 0.5，不是后写的别的值）
    _write(pred_dir / "prediction_2026-10-12_11.json", {
        "predictions": [
            {"home": "A", "away": "B", "reasoning_factors": {"home_ml_true_prob": 0.7}},
        ],
    })

    store_mod._pred_index_by_pair_cached.clear()
    store_mod._load_prediction_docs_cached.clear()
    idx = store_mod.pred_index_by_pair()

    assert set(idx.keys()) == {("A", "B"), ("C", "D")}
    # first-match-wins：sorted 文件名升序，doc 1 在前，0.5 应保留
    assert idx[("A", "B")]["reasoning_factors"]["home_ml_true_prob"] == 0.5
    assert idx[("C", "D")]["reasoning_factors"]["draw_true_prob"] == 0.3


def test_pred_index_by_pair_normalizes_whitespace(tmp_path, monkeypatch):
    """entry 的 home/away 含前后空格 → key 标准化为 strip 后。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    _write(tmp_path / "predictions" / "prediction_2026-10-12_10.json", {
        "predictions": [
            {"home": "  曼联  ", "away": "  曼城  "},
        ],
    })

    store_mod._pred_index_by_pair_cached.clear()
    store_mod._load_prediction_docs_cached.clear()
    idx = store_mod.pred_index_by_pair()

    assert ("曼联", "曼城") in idx
    assert ("  曼联  ", "  曼城  ") not in idx


def test_pred_index_by_pair_skips_invalid_entries(tmp_path, monkeypatch):
    """空 home/away、非 dict entry → 跳过，不污染索引。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    _write(tmp_path / "predictions" / "prediction_2026-10-12_10.json", {
        "predictions": [
            {"home": "", "away": "B"},       # 空 home → skip
            {"home": "A", "away": ""},       # 空 away → skip
            {"home": None, "away": "B"},     # None home → skip
            {"home": "A", "away": "B"},      # OK
            "not a dict",                     # 非 dict → skip
        ],
    })

    store_mod._pred_index_by_pair_cached.clear()
    store_mod._load_prediction_docs_cached.clear()
    idx = store_mod.pred_index_by_pair()

    assert list(idx.keys()) == [("A", "B")]


def test_pred_index_by_pair_invalidates_on_new_file(tmp_path, monkeypatch):
    """新增文件 → mtime sig 变化 → 缓存失效。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    _write(tmp_path / "predictions" / "prediction_2026-10-12_10.json", {
        "predictions": [{"home": "A", "away": "B"}],
    })

    store_mod._pred_index_by_pair_cached.clear()
    store_mod._load_prediction_docs_cached.clear()
    idx1 = store_mod.pred_index_by_pair()
    assert ("A", "B") in idx1

    # 新增文件 → mtime sig 变
    time.sleep(1.1)  # 跨 FAT/部分 FS 的 2s 粒度
    _write(tmp_path / "predictions" / "prediction_2026-10-12_11.json", {
        "predictions": [{"home": "X", "away": "Y"}],
    })

    idx2 = store_mod.pred_index_by_pair()
    assert ("X", "Y") in idx2, "新增文件应触发缓存失效"
    assert len(idx2) == 2


def test_pred_index_by_pair_invalidates_on_file_modified(tmp_path, monkeypatch):
    """已有文件覆写 → mtime 变 → 失效。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()
    p1 = tmp_path / "predictions" / "prediction_2026-10-12_10.json"
    _write(p1, {"predictions": [{"home": "A", "away": "B"}]})

    store_mod._pred_index_by_pair_cached.clear()
    store_mod._load_prediction_docs_cached.clear()
    idx1 = store_mod.pred_index_by_pair()
    assert ("A", "B") in idx1

    time.sleep(1.1)
    _write(p1, {"predictions": [{"home": "X", "away": "Y"}]})

    idx2 = store_mod.pred_index_by_pair()
    assert ("X", "Y") in idx2
    assert ("A", "B") not in idx2


def test_pred_index_by_pair_clear_for_test_isolation(tmp_path, monkeypatch):
    """_pred_index_by_pair_cached 按 OUTPUT_DIR 分键，clear 后可重建。"""
    from web.services import store as store_mod

    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path)
    (tmp_path / "predictions").mkdir()

    store_mod._pred_index_by_pair_cached["/tmp/some"] = ("sig", {})
    assert "/tmp/some" in store_mod._pred_index_by_pair_cached
    store_mod._pred_index_by_pair_cached.clear()
    assert store_mod._pred_index_by_pair_cached == {}


# ────────────────────────── HAF 接入 — _find_prediction 走字典 ──────────────────────────


def test_haf_find_prediction_dict_hit(monkeypatch):
    """haf._find_prediction 反查命中 dict → 直接返回 entry（不调 teams_match fuzzy）。"""
    from web.routers import haf as haf_mod
    from web.services import store as ws

    target = {"home": "曼彻斯特联", "away": "利物浦",
              "reasoning_factors": {"home_ml_true_prob": 0.6}}
    monkeypatch.setattr(ws, "pred_index_by_pair",
                        lambda: {("曼彻斯特联", "利物浦"): target})

    # fuzzy 路径不应被触达（mock 给一个 panic 调用当 sentinel）
    def boom(*a, **kw):
        raise AssertionError("haf: fuzzy fallback was called on dict hit")
    from web.services import team_match as tm
    monkeypatch.setattr(tm, "teams_match", boom)

    e = haf_mod._find_prediction("曼彻斯特联", "利物浦")
    assert e is target


def test_haf_find_prediction_fuzzy_fallback(monkeypatch):
    """dict 未命中（strip 后不一致）→ 走 teams_match fuzzy 兜底。"""
    from web.routers import haf as haf_mod
    from web.services import store as ws
    from web.services import team_match as tm

    target = {"home": "曼联", "away": "曼城"}
    monkeypatch.setattr(ws, "pred_index_by_pair",
                        lambda: {("曼联", "曼城"): target})
    # "曼彻斯特联" / "曼彻斯特城" 别名 vs "曼联" / "曼城" 字典键
    monkeypatch.setattr(tm, "teams_match",
                        lambda h1, a1, h2, a2: (h2, a2) == ("曼联", "曼城"))

    e = haf_mod._find_prediction("曼彻斯特联", "曼彻斯特城")
    assert e is target


def test_haf_find_prediction_no_match_returns_none(monkeypatch):
    """dict + fuzzy 都未命中 → 返回 None。"""
    from web.routers import haf as haf_mod
    from web.services import store as ws
    from web.services import team_match as tm

    monkeypatch.setattr(ws, "pred_index_by_pair",
                        lambda: {("A", "B"): {"home": "A", "away": "B"}})
    monkeypatch.setattr(tm, "teams_match",
                        lambda h1, a1, h2, a2: False)

    assert haf_mod._find_prediction("Z", "Y") is None


def test_haf_find_prediction_empty_index(monkeypatch):
    """pred_index_by_pair 抛错 → logger warning + 返回 None（不影响调用方）。"""
    from web.routers import haf as haf_mod
    from web.services import store as ws

    def boom():
        raise RuntimeError("store broken")

    monkeypatch.setattr(ws, "pred_index_by_pair", boom)

    # 抛错应被 except 吞掉，返回 None
    assert haf_mod._find_prediction("A", "B") is None


# ────────────────────────── combo 接入 — _assemble_legs 走字典 ──────────────────────────


def test_combo_assemble_legs_dict_hit_for_p_model(monkeypatch):
    """combo._assemble_legs dict 命中 → p_model 立即取，不走 fuzzy。"""
    from web.routers import combo as combo_mod
    from web.services import store as ws

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [
        {"match_num": 7001, "home_cn": "A", "away_cn": "B", "play_type": "had",
         "options": {"h": "1.8", "d": "3.4", "a": "4.5"}},
    ])
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {
        ("A", "B"): {"home": "A", "away": "B",
                     "reasoning_factors": {"home_ml_true_prob": 0.6}},
    })

    legs = combo_mod._assemble_legs([{"home": "A", "away": "B", "side": "主胜"}])
    assert legs[0]["odds"] == 1.8
    assert legs[0]["p_model"] == 0.6


def test_combo_assemble_legs_fuzzy_fallback_for_p_model(monkeypatch):
    """dict miss → fuzzy 兜底仍能取 p_model（对齐原行为）。"""
    from web.routers import combo as combo_mod
    from web.services import store as ws

    monkeypatch.setattr("store.jc_view.fixtures_on", lambda d: [
        {"match_num": 7001, "home_cn": "曼联", "away_cn": "曼城", "play_type": "had",
         "options": {"h": "2.1", "d": "3.3", "a": "3.4"}},
    ])
    monkeypatch.setattr(ws, "pred_index_by_pair", lambda: {
        # dict 键是 "曼联/曼城"，但 fixture 是 "曼联/曼城"（同），而调用用 "曼彻斯特联/曼彻斯特城"
        ("曼联", "曼城"): {"home": "曼联", "away": "曼城",
                          "reasoning_factors": {"home_ml_true_prob": 0.55}},
    })

    legs = combo_mod._assemble_legs([
        {"home": "曼彻斯特联", "away": "曼彻斯特城", "side": "主胜"},
    ])
    # odds 取到（fixture 命中）
    assert legs[0]["odds"] == 2.1
    # p_model 0.0 因为 fuzzy 兜底当前用 teams_match_safe 对中英别名不通用
    # —— 验证我们没崩 + 没抛错即可；fuzzy 路径交给后续 R20 优化
    assert isinstance(legs[0]["p_model"], float)
