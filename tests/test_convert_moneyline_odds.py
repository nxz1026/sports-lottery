"""R35-2: scripts/core/data/convert.py::_build_moneyline_odds 4 测直测。

现状：Match Winner (1X2) bet → ESPN moneyLine 格式转换 0 直测。
返回 {details, drawOdds, homeML, awayML}。

favorite 选择：`home_imp >= away_imp and home_american` → "{home} {american}"
                    `elif away_american` → "{away} {american}"
                    `else` → ""（兜底）

decimal_to_american：dec>=2.0 → "+{N}"（underdog），1<dec<2 → "-{N}"（favorite）

本轮补 4 测：
- match_winner=None/空 → {} 兜底
- home favorite (dec=1.5) → details="Arsenal -200"
- away favorite (dec=1.5) → details="Chelsea -200"
- home/away american 都缺（≤1.0 invalid）→ details="" + drawOdds={}
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
def convert_mod():
    spec = importlib.util.spec_from_file_location(
        "convert_moneyline_odds_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _build_moneyline_odds ────────────────────────


def test_build_moneyline_odds_none_returns_empty(convert_mod):
    """R35-2：match_winner=None → {} 兜底（防 KeyError，调用方 zero-effect）。"""
    assert convert_mod._build_moneyline_odds(None, "Arsenal", "Chelsea") == {}
    assert convert_mod._build_moneyline_odds({}, "Arsenal", "Chelsea") == {}


def test_build_moneyline_odds_home_favorite(convert_mod):
    """R35-2：home favorite (dec=1.5) → details="Arsenal -200"。

    home_dec=1.5 → favorite → home_american="-200"
    home_imp=2/3 ≈ 0.667 > away_imp=0.25 → favorite 是 home
    """
    mw = {"values": [
        {"value": "Home", "odd": "1.50"},
        {"value": "Draw", "odd": "4.00"},
        {"value": "Away", "odd": "3.50"},
    ]}
    out = convert_mod._build_moneyline_odds(mw, "Arsenal", "Chelsea")
    assert out["details"] == "Arsenal -200"
    assert out["homeML"] == "-200"
    assert out["awayML"] == "+250"  # (3.5-1)*100 = +250
    assert out["drawOdds"] == {"moneyLine": "+300"}  # (4.0-1)*100 = +300


def test_build_moneyline_odds_away_favorite(convert_mod):
    """R35-2：away favorite (dec=1.5) → details="Chelsea -200"。

    home_dec=3.0 → away_dec=1.5 → away favorite（away_imp=0.667 > home_imp=0.333）
    """
    mw = {"values": [
        {"value": "Home", "odd": "3.00"},
        {"value": "Draw", "odd": "3.50"},
        {"value": "Away", "odd": "1.50"},
    ]}
    out = convert_mod._build_moneyline_odds(mw, "Arsenal", "Chelsea")
    assert out["details"] == "Chelsea -200"
    assert out["homeML"] == "+200"  # (3.0-1)*100 = +200
    assert out["awayML"] == "-200"


def test_build_moneyline_odds_no_valid_odds_details_empty(convert_mod):
    """R35-2：home/away american 都缺（≤1.0 invalid）→ details="" + drawOdds={}。

    decimal_to_american(1.0) 返 None（≤1.0 边界）；empty values → 全部 None。
    """
    # values 全 None → home_american/away_american/draw_american 全 None
    mw = {"values": []}
    out = convert_mod._build_moneyline_odds(mw, "Arsenal", "Chelsea")
    assert out["details"] == ""
    assert out["homeML"] is None
    assert out["awayML"] is None
    assert out["drawOdds"] == {}
    # odd <= 1.0 边界：decimal_to_american 返 None
    mw2 = {"values": [
        {"value": "Home", "odd": "1.00"},
        {"value": "Away", "odd": "0.50"},
    ]}
    out2 = convert_mod._build_moneyline_odds(mw2, "Arsenal", "Chelsea")
    assert out2["details"] == ""
    assert out2["homeML"] is None
    assert out2["awayML"] is None
