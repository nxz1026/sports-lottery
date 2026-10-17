"""R44-2: web/routers/combo.py nested _find_fix 4 cases direct + D-57 lock.

_find_fix 是 _pred_by_pair 内嵌 closure (L67), 0 直测; 通过 _assemble_legs
间接调用。helper 自身契约 (exact (h,a) 命中 / fuzzy 兜底 / miss -> None) 未锁。

helper 含三条契约 (D-57):
- exact 命中 (h.strip(), a.strip()) 是 fix_by_pair 的 key -> 返 fix dict
- exact miss -> 用 teams_match_safe 扫 fix_by_pair.values(), 首匹配返
- 全 miss -> 返 None (caller 给 odds=0.0 fallback)

4 tests (EASY/MEDIUM): 通过 _assemble_legs 行为间接断言 _find_fix。
_setup fixture: mock jc_view.fixtures_on + pred_index_by_pair, 让
_assemble_legs 走完 _find_fix 三路径。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import combo as combo_mod


# ──────────────────────── D-57 fixture helpers ────────────────────────


def _stub_fixtures(monkeypatch, fixtures: list):
    """Mock store.jc_view.fixtures_on 让 _assemble_legs 用受控 fixture。

    _assemble_legs 内 `from store import jc_view` lazy import, 需要 `store`
    也注册到 sys.modules (不能直接 sub-module stub)。
    """
    jc_view_mod = types.ModuleType("store.jc_view")
    jc_view_mod.fixtures_on = lambda date_iso: fixtures
    store_pkg = types.ModuleType("store")
    store_pkg.jc_view = jc_view_mod
    monkeypatch.setitem(sys.modules, "store.jc_view", jc_view_mod)
    monkeypatch.setitem(sys.modules, "store", store_pkg)


def _stub_pred_by_pair(monkeypatch, pred_by_pair: dict):
    """Mock web.services.store.pred_index_by_pair 给受控 dict。"""
    from web.services import store as web_store
    monkeypatch.setattr(web_store, "pred_index_by_pair",
                        lambda: pred_by_pair)


def _stub_teams_match_safe(monkeypatch, matches: list):
    """Mock teams_match_safe fuzzy: matches[i] 决定 teams_match_safe(...).
    
    matches[i] = True/False: 第 i 次调用 teams_match_safe 返回该 bool.
    """
    import web.services.team_match as tm
    counter = {"i": 0}

    def fake_match(h1, a1, h2, a2):
        idx = counter["i"]
        counter["i"] += 1
        # fuzzy 都 False 默认, 第三个参数空 list 给 True
        if idx < len(matches):
            return matches[idx]
        return False

    monkeypatch.setattr(tm, "teams_match_safe", fake_match)


def _call_assemble_legs(monkeypatch, *, fixtures, pred_by_pair,
                       fuzzy_matches, selections):
    """绕 _pred_by_pair 编排, _find_fix 真函数仍跑 (因 _pred_by_pair 返 dict + closure)。"""
    _stub_fixtures(monkeypatch, fixtures)
    _stub_pred_by_pair(monkeypatch, pred_by_pair)
    _stub_teams_match_safe(monkeypatch, fuzzy_matches)
    # _assemble_legs 内部调 _pred_by_pair 但同时自调 _find_fix。
    return combo_mod._assemble_legs(selections)


# ──────────────────────── D-57 三契约 ────────────────────────


def test_find_fix_d57_exact_match_returns_fix(monkeypatch):
    """R44-2 D-57: exact (h, a) 命中 fix_by_pair -> 返 fix dict 含 plays。

    _find_fix 首先用 (h.strip(), a.strip()) 命中 fixtures dict, 不进 fuzzy。
    fixture options key 用 h/d/a (与 _SIDE_KEY 映射对齐, side='主胜' -> 'h')。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西",
         "play_type": "had", "options": {"h": "1.80", "d": "3.40", "a": "4.20"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    # 不需要 fuzzy (不会调)
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"home": "阿森纳", "away": "切尔西", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 1.80  # exact 命中 -> 有 odds
    assert legs[0]["p_model"] == 0.0  # pred_by_pair 空 -> 默认 0


def test_find_fix_d57_fuzzy_match_after_exact_miss(monkeypatch):
    """R44-2 D-57: exact miss -> teams_match_safe True 命中, 返 fix dict。

    锁 fuzzy 兜底: 第一次 exact 失败后, 若 fuzzy 命中, 返 fm。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "曼联", "away_cn": "利物浦",
         "play_type": "had", "options": {"h": "2.50", "d": "3.20", "a": "2.80"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    # 第 1 次调用 fuzzy 返 True (命中)
    _stub_teams_match_safe(monkeypatch, [True])

    legs = combo_mod._assemble_legs([
        # 请求 "曼彻斯特联" vs "利物浦 FC" (中文别名不同) - fuzzy 命中
        {"home": "曼彻斯特联", "away": "利物浦 FC", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 2.50  # fuzzy 命中 -> 有 odds


def test_find_fix_d57_all_miss_returns_none_odds_zero(monkeypatch):
    """R44-2 D-57: exact miss + fuzzy miss -> None -> odds 默认 0.0。

    _find_fix 返 None 时, _assemble_legs 把 odds 设 0.0 (保 caller 不爆)。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "切尔西", "away_cn": "阿森纳",
         "play_type": "had", "options": {"主胜": "2.50"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    # 所有 fuzzy 都 False
    _stub_teams_match_safe(monkeypatch, [False, False, False])

    legs = combo_mod._assemble_legs([
        {"home": "陌生队A", "away": "陌生队B", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 0.0  # miss -> 默认 0


def test_find_fix_d57_uses_strip_whitespace(monkeypatch):
    """R44-2 D-57: home/away 含空白 -> strip 后命中 (与 _match_probs / haf 一致)。

    D-57 fuzzy 上游统一契约: 输入 strip, 字典键已 strip, 命中 O(1)。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西",
         "play_type": "had", "options": {"h": "1.50"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"home": "  阿森纳  ", "away": "\n切尔西\t", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 1.50  # strip 后命中 exact
