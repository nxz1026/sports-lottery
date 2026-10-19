"""R45-1: web/routers/combo.py::_assemble_legs 7 cases direct + D-62 lock.

_assemble_legs (L32-L119, 88 行) 是 combo 路由层最重 0-直测 helper:
- fixtures_on 装载盘口 (had/hhad)
- pred_index_by_pair 装载 reasoning_factors
- _SIDE_KEY 中文/英文映射 (主胜/平/客胜/home/draw/away + 让球)
- match_num 反查 home/away (旧入参兼容)
- _find_fix nested closure: exact -> fuzzy -> None
- odds 装配: had 优先, hhad 让球 fallback
- p_model 装配: dict 命中 + fuzzy 兜底 (走 reasoning_factors.h/d/a)

D-62 锁定 7 契约 (避免与 R44-2 D-57 _find_fix 重复):
- D-62-1: 主流程 happy path (exact + 中文 side 映射 had)
- D-62-2: match_num 反查 home/away
- D-62-3: hhad 让球 fallback (fixture 只有 hhad 时)
- D-62-4: 英文 side (home/draw/away) 映射
- D-62-5: 完全 miss (fixtures 空 + pred 空) -> odds=0/p_model=0
- D-62-6: jc_view.fixtures_on raise -> 静默降级 (fixtures=[])
- D-62-7: p_model fuzzy 兜底 (dict miss + fuzzy True 命中 pred_by_pair)

依赖 stub:
- _stub_fixtures: 写 sys.modules['store'] + ['store.jc_view']
- _stub_pred_by_pair: monkeypatch web.services.store.pred_index_by_pair
- _stub_teams_match_safe: 计数式 fuzzy stub (matches[i] 决定第 i 次调用结果)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers import combo as combo_mod

# ──────────────────────── D-62 fixture helpers (复用 R44-2 套路) ────────────────────────


def _stub_fixtures(monkeypatch, fixtures: list, *, fixtures_on_side_effect=None):
    """Mock store.jc_view.fixtures_on 让 _assemble_legs 用受控 fixture。

    fixtures_on_side_effect: 若给 Exception -> 让 fixtures_on 抛异常 (测降级路径)。
    """
    jc_view_mod = types.ModuleType("store.jc_view")

    def _fixtures_on(date_iso):
        if fixtures_on_side_effect is not None:
            raise fixtures_on_side_effect
        return fixtures

    jc_view_mod.fixtures_on = _fixtures_on
    store_pkg = types.ModuleType("store")
    store_pkg.jc_view = jc_view_mod
    monkeypatch.setitem(sys.modules, "store.jc_view", jc_view_mod)
    monkeypatch.setitem(sys.modules, "store", store_pkg)


def _stub_pred_by_pair(monkeypatch, pred_by_pair: dict, *, side_effect=None):
    """Mock web.services.store.pred_index_by_pair 给受控 dict。

    side_effect: 若给 Exception -> 让 pred_index_by_pair 抛异常 (测降级路径)。
    """
    from web.services import store as web_store

    def _pred():
        if side_effect is not None:
            raise side_effect
        return pred_by_pair

    monkeypatch.setattr(web_store, "pred_index_by_pair", _pred)


def _stub_teams_match_safe(monkeypatch, matches: list):
    """Mock teams_match_safe fuzzy: matches[i] 决定第 i 次调用结果。"""
    import web.services.team_match as tm
    counter = {"i": 0}

    def fake_match(h1, a1, h2, a2):
        idx = counter["i"]
        counter["i"] += 1
        if idx < len(matches):
            return matches[idx]
        return False

    monkeypatch.setattr(tm, "teams_match_safe", fake_match)


# ──────────────────────── D-62 七契约 ────────────────────────


def test_assemble_legs_d62_1_happy_path_exact_with_p_model(monkeypatch):
    """R45-1 D-62-1: 主流程 — exact 命中 + pred_by_pair 字典命中。

    fixtures had opts.h='1.80' + pred_by_pair[('阿森纳','切尔西')].reasoning_factors.home_ml_true_prob=0.55。
    side='主胜' 映射 key='h' -> odds=1.80 + p_model=0.55。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西",
         "play_type": "had", "options": {"h": "1.80", "d": "3.40", "a": "4.20"}},
    ])
    _stub_pred_by_pair(monkeypatch, {
        ("阿森纳", "切尔西"): {
            "home": "阿森纳", "away": "切尔西",
            "reasoning_factors": {
                "home_ml_true_prob": 0.55,
                "draw_true_prob": 0.25,
                "away_ml_true_prob": 0.20,
            },
        },
    })
    _stub_teams_match_safe(monkeypatch, [])  # 不会调 fuzzy (exact 命中)

    legs = combo_mod._assemble_legs([
        {"home": "阿森纳", "away": "切尔西", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0] == {"side": "主胜", "odds": 1.80, "p_model": 0.55}


def test_assemble_legs_d62_2_match_num_reverse_lookup(monkeypatch):
    """R45-1 D-62-2: match_num 入参 -> 反查 home/away。

    旧入参只给 match_num 不给 home/away, _assemble_legs L83-L88 走 fixtures 扫描
    反查 home_cn/away_cn。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7001, "home_cn": "阿森纳", "away_cn": "切尔西",
         "play_type": "had", "options": {"h": "1.80"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"match_num": 7001, "side": "主胜"},  # 不给 home/away
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 1.80  # 反查后命中 exact


def test_assemble_legs_d62_3_hhad_fallback_when_had_missing(monkeypatch):
    """R45-1 D-62-3: hhad 让球 fallback。

    fixture 只有 play_type='hhad' 没有 'had', _assemble_legs L93 走 hhad options。
    side='让胜' 映射 key='h' (与中文 '主胜' 同 key, 让球维度复用 h/d/a 编码)。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7002, "home_cn": "AC米兰", "away_cn": "国米",
         "play_type": "hhad", "options": {"h": "1.95", "d": "3.30", "a": "3.80"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"home": "AC米兰", "away": "国米", "side": "让胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 1.95


def test_assemble_legs_d62_4_english_side_key_mapping(monkeypatch):
    """R45-1 D-62-4: 英文 side 小写映射 + 大写 fallback 容错。

    _SIDE_KEY 含 'home'/'draw'/'away' -> 映射到 h/d/a 短码。
    side='HOME' (大写) 不在 _SIDE_KEY, 走 fallback `side.lower()`='home',
    但 fixture options key 是 'h' 短码 -> odds=0.0 (LOCK: 大写需 caller 自己 lowercase)。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7003, "home_cn": "曼城", "away_cn": "热刺",
         "play_type": "had", "options": {"h": "1.50", "d": "4.00", "a": "5.50"}},
    ])
    _stub_pred_by_pair(monkeypatch, {})
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        # 小写英文 -> _SIDE_KEY 命中 -> 短码 'h' -> 命中 1.50
        {"home": "曼城", "away": "热刺", "side": "home"},
        # 小写英文 -> _SIDE_KEY 命中 -> 短码 'a' -> 命中 5.50
        {"home": "曼城", "away": "热刺", "side": "away"},
        # 大写 'HOME' -> _SIDE_KEY miss -> fallback 'home' (非 'h') -> odds=0
        # (LOCK: 大写 caller-side normalize 才能命中 fixture options)
        {"home": "曼城", "away": "热刺", "side": "HOME"},
    ])
    assert len(legs) == 3
    assert legs[0]["odds"] == 1.50   # home -> h
    assert legs[1]["odds"] == 5.50   # away -> a
    assert legs[2]["odds"] == 0.0    # HOME 大写 -> 'home' 找不到 -> 0


def test_assemble_legs_d62_5_total_miss_returns_zero_legs(monkeypatch):
    """R45-1 D-62-5: 完全 miss (fixtures 空 + pred 空) -> 全 0 降级。

    odds=0/p_model=0, 由 evaluate_combo 计入 warning (combo_eval L142 不算'不编数')。
    """
    _stub_fixtures(monkeypatch, [])  # 业务日无 fixture
    _stub_pred_by_pair(monkeypatch, {})
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"home": "陌生队A", "away": "陌生队B", "side": "主胜"},
        {"home": "陌生队C", "away": "陌生队D", "side": "平"},
    ])
    assert len(legs) == 2
    assert all(lg["odds"] == 0.0 for lg in legs)
    assert all(lg["p_model"] == 0.0 for lg in legs)
    assert [lg["side"] for lg in legs] == ["主胜", "平"]  # side 透传


def test_assemble_legs_d62_6_fixtures_on_raises_silently_degrades(monkeypatch):
    """R45-1 D-62-6: jc_view.fixtures_on raise -> 静默降级 (fixtures=[])。

    L41-L45 try/except 捕获异常 logger.warning 后 fixtures=[]。
    sel 入参 -> _assemble_legs 不爆, 走 fixtures=[] 全 0 路径。
    """
    _stub_fixtures(monkeypatch, fixtures=[],  # 不用真实 fixtures
                   fixtures_on_side_effect=RuntimeError("fixtures_on backend broken"))
    _stub_pred_by_pair(monkeypatch, {
        ("X", "Y"): {  # 不会命中 (fixtures 空 -> _find_fix 返 None)
            "reasoning_factors": {"home_ml_true_prob": 0.99},
        },
    })
    _stub_teams_match_safe(monkeypatch, [])

    legs = combo_mod._assemble_legs([
        {"home": "X", "away": "Y", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 0.0
    assert legs[0]["p_model"] == 0.0  # m=None 走 L99 False 分支


def test_assemble_legs_d62_7_p_model_fuzzy_fallback_to_pred(monkeypatch):
    """R45-1 D-62-7: p_model fuzzy 兜底 (dict miss + fuzzy True 命中 pred_by_pair)。

    fix_by_pair 命中 (fixtures 有 '曼彻斯特联') 但 pred_by_pair 字典不含
    ('曼彻斯特联', '曼城') 这个精确键 -> 走 fuzzy 兜底 L102-L108。
    pred_by_pair 含 ('Manchester United', 'Manchester City') 别名 entry,
    teams_match_safe 返 True 命中。
    """
    _stub_fixtures(monkeypatch, [
        {"match_num": 7004, "home_cn": "曼彻斯特联", "away_cn": "曼城",
         "play_type": "had", "options": {"h": "2.10"}},
    ])
    _stub_pred_by_pair(monkeypatch, {
        # 字典键是英文别名, 与 fixture home/away 不一致 -> exact miss
        ("Manchester United", "Manchester City"): {
            "home": "Manchester United", "away": "Manchester City",
            "reasoning_factors": {
                "home_ml_true_prob": 0.45,
                "draw_true_prob": 0.30,
                "away_ml_true_prob": 0.25,
            },
        },
    })
    # 第一次 fuzzy (p_model L106 兜底): True 命中
    _stub_teams_match_safe(monkeypatch, [True])

    legs = combo_mod._assemble_legs([
        {"home": "曼彻斯特联", "away": "曼城", "side": "主胜"},
    ])
    assert len(legs) == 1
    assert legs[0]["odds"] == 2.10  # fixtures exact 命中
    assert legs[0]["p_model"] == 0.45  # pred_by_pair fuzzy 命中
