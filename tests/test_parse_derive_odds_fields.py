"""R38-2: scripts/core/data/parse.py::_derive_odds_fields 6 测直测 + D-29 字段集锁（Top 5）。

现状：14 字段聚合输出 0 直测（注：实测 17 字段，探索报告 14 有误）。
集成 5 个底层 helper：parse_details / parse_american_odds / remove_vig /
spread_movement_factor / form_to_score / record_to_score。

D-29 bug-shaped 锁定：17 字段完整性锁。
字段改名/删字段时直接 fail，防 silent 改变下游 consumer。

本轮补 6 测：
- 全字段正常 → 17 字段 dict 完整
- odds 空 → 17 字段全存在（默认值），h_fs/a_fs 兜底 0.5
- D-29：set(out.keys()) == {17 字段}
- drawOdds 缺 → draw_ml=None / draw_implied=None
- records 空 → h_rs/a_rs 走 record_to_score([]) fallback
- spread 完全空 → spread_move 走 None 分支
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def parse_mod():
    spec = importlib.util.spec_from_file_location(
        "parse_derive_odds_fields_test",
        REPO_ROOT / "scripts" / "core" / "data" / "parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 17 字段全集（D-29 锁定）
EXPECTED_KEYS = {
    "ml_odds_str", "draw_ml", "home_ml_implied", "draw_implied",
    "home_true", "draw_true", "away_true",
    "spread_h_line", "spread_h_odds", "spread_move",
    "tot_o_close", "tot_u_close", "odds",
    "h_fs", "a_fs", "h_rs", "a_rs",
}


def _full_odds():
    return {
        "details": "Arsenal -110",
        "drawOdds": {"moneyLine": "+250"},
        "pointSpread": {
            "home": {"open": {"line": "-1.5", "odds": "-110"},
                      "close": {"line": "-1.5", "odds": "-110"}},
            "away": {"open": {"line": "+1.5", "odds": "-110"},
                      "close": {"line": "+1.5", "odds": "-110"}},
        },
        "total": {
            "over": {"close": {"line": "2.5", "odds": "-110"}},
            "under": {"close": {"line": "2.5", "odds": "-110"}},
        },
    }


# ──────────────────────── _derive_odds_fields ────────────────────────


def test_derive_odds_fields_complete(parse_mod):
    """R38-2：全字段正常 → 17 字段完整 + 关键字段值校验。

    ml_odds_str 是 parse_details 拆出的 odds 部分（"-110"），非完整 details 串。
    """
    out = parse_mod._derive_odds_fields(
        _full_odds(), "WWD", "LDL", [], [])
    assert set(out.keys()) == EXPECTED_KEYS
    assert out["ml_odds_str"] == "-110"  # parse_details 拆出的 odds 部分
    assert out["draw_ml"] == "+250"
    assert out["spread_h_line"] == "-1.5"
    assert out["spread_h_odds"] == "-110"


def test_derive_odds_fields_empty_dict(parse_mod):
    """R38-2：odds 空 dict → 17 字段全存在（默认值），h_fs/a_fs 兜底 0.5。"""
    out = parse_mod._derive_odds_fields({}, "", "", [], [])
    assert set(out.keys()) == EXPECTED_KEYS
    # h_fs/a_fs 走 form_to_score("") → 0.5
    assert out["h_fs"] == 0.5
    assert out["a_fs"] == 0.5
    # h_rs/a_rs 走 record_to_score([]) → 0.5
    assert out["h_rs"] == 0.5
    assert out["a_rs"] == 0.5


def test_derive_odds_fields_d29_17_keys_locked(parse_mod):
    """R38-2 D-29 锁定：set(out.keys()) 严格等于 17 字段全集。

    字段改名/删字段时直接 fail，防 silent 改变下游 consumer。
    """
    out = parse_mod._derive_odds_fields(_full_odds(), "", "", [], [])
    assert set(out.keys()) == EXPECTED_KEYS
    assert len(out) == 17


def test_derive_odds_fields_missing_drawOdds(parse_mod):
    """R38-2：drawOdds 缺 → draw_ml=None / draw_implied=None。

    `(odds.get("drawOdds") or {}).get("moneyLine", None)` 链式 None 兜底。
    """
    odds_no_draw = _full_odds()
    del odds_no_draw["drawOdds"]
    out = parse_mod._derive_odds_fields(odds_no_draw, "", "", [], [])
    assert out["draw_ml"] is None
    assert out["draw_implied"] is None


def test_derive_odds_fields_empty_records_uses_default(parse_mod):
    """R38-2：records 空 list → h_rs/a_rs 走 record_to_score([]) fallback（0.5）。"""
    out = parse_mod._derive_odds_fields(_full_odds(), "", "", [], [])
    # record_to_score([]) → 0.5
    assert out["h_rs"] == 0.5
    assert out["a_rs"] == 0.5


def test_derive_odds_fields_empty_spread(parse_mod):
    """R38-2：spread 完全空 → spread_move = 0.0（实际行为锁定）。

    `spread_movement_factor(None, None)` 返 0.0（不是 None/0.5）。锁定
    spread_movement_factor 静默零化行为。
    """
    odds_no_spread = _full_odds()
    del odds_no_spread["pointSpread"]
    out = parse_mod._derive_odds_fields(odds_no_spread, "", "", [], [])
    # spread_h_line/odds 兜底 ""
    assert out["spread_h_line"] == ""
    assert out["spread_h_odds"] == ""
    # spread_move = spread_movement_factor(None, None) = 0.0
    assert out["spread_move"] == 0.0