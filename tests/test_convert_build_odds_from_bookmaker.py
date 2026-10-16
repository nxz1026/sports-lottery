"""R36-2: scripts/core/data/convert.py::_build_odds_from_bookmaker 4 测直测 + D-15 锁定（Top 2）。

现状：API-Football bookmaker bets → ESPN-style odds 编排器 0 直测；
3 种 bet (Match Winner / Handicap / Goals Over/Under) 编排 + Asian Handicap
别名匹配 + D-15 多 bookmaker 取数契约。

D-15 bug-shaped 锁定（convert.py:106）：`bets = bookmakers[0].get("bets", [])`
固定取 [0]。多数据源拼接时无显式排序，未来加排序逻辑会静默改变赔率源。

本轮补 4 测：
- bookmakers=[] → {}
- 三种 bet 齐全 → 5 keys（details / drawOdds / homeML / awayML / pointSpread / total）
- Asian Handicap alias 匹配 → pointSpread.home 设
- D-15：≥ 2 bookmaker 时仍只取 [0]（odds 取第一本书而非最优）
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
        "convert_build_odds_from_bookmaker_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _build_odds_from_bookmaker ────────────────────────


def test_build_odds_from_bookmaker_empty_list_returns_empty(convert_mod):
    """R36-2：bookmakers=[] → {}（defensive 防 NoneType/空 list）。"""
    assert convert_mod._build_odds_from_bookmaker([], "Arsenal", "Chelsea") == {}


def test_build_odds_from_bookmaker_three_bets_complete(convert_mod):
    """R36-2：Match Winner + Handicap + Goals Over/Under 全齐 → 5 keys。

    moneyLine + spread/total 集成返回：details / drawOdds / homeML / awayML
    + pointSpread.home + total.over。
    """
    bookmaker = {"bets": [
        {"name": "Match Winner", "values": [
            {"value": "Home", "odd": "2.00"},
            {"value": "Draw", "odd": "3.00"},
            {"value": "Away", "odd": "4.00"},
        ]},
        {"name": "Handicap", "values": [{"value": "Home Arsenal -1.5", "odd": "1.91"}]},
        {"name": "Goals Over/Under", "values": [{"value": "Over 2.5", "odd": "1.85"}]},
    ]}
    out = convert_mod._build_odds_from_bookmaker([bookmaker], "Arsenal", "Chelsea")
    assert out["details"] == "Arsenal +100"  # home favorite dec=2.0 → +100
    assert out["drawOdds"] == {"moneyLine": "+200"}
    assert out["homeML"] == "+100"
    assert out["awayML"] == "+300"
    # pointSpread (R36-1 已锁)
    assert out["pointSpread"]["home"]["open"]["line"] == "-1.5"
    # total (R36-1 已锁)
    assert out["total"]["over"]["open"]["line"] == "2.5"


def test_build_odds_from_bookmaker_asian_handicap_alias(convert_mod):
    """R36-2：Handicap 三 alias 之一 "Asian Handicap" 也走同条路径。"""
    bookmaker = {"bets": [
        {"name": "Match Winner", "values": [{"value": "Home", "odd": "1.50"}]},
        {"name": "Asian Handicap", "values": [{"value": "Home Arsenal -0.5", "odd": "1.91"}]},
    ]}
    out = convert_mod._build_odds_from_bookmaker([bookmaker], "Arsenal", "Chelsea")
    assert out["homeML"] == "-200"  # 1.50 favorite → -200
    # Asian Handicap alias 走同一条 pointSpread 装配
    assert "pointSpread" in out
    assert out["pointSpread"]["home"]["open"]["line"] == "-0.5"


def test_build_odds_from_bookmaker_d15_takes_first_only(convert_mod):
    """R36-2 D-15 锁定：≥ 2 bookmaker 时仍只取 [0]，忽略后续。

    bookmaker_a home dec=10.0 → homeML="+900"（underdog 重赔）
    bookmaker_b home dec=1.10 → homeML="-1000"（heavy favorite）
    取 [0]（bookmaker_a），忽略 bookmaker_b 的"更优"赔率。
    锁定现状防未来加排序逻辑时静默改变赔率源。
    """
    bookmaker_a = {"bets": [{"name": "Match Winner",
                              "values": [{"value": "Home", "odd": "10.00"}]}]}
    bookmaker_b = {"bets": [{"name": "Match Winner",
                              "values": [{"value": "Home", "odd": "1.10"}]}]}
    out = convert_mod._build_odds_from_bookmaker(
        [bookmaker_a, bookmaker_b], "Arsenal", "Chelsea")
    # D-15：取 [0] = bookmaker_a（10.00 → +900）
    assert out["homeML"] == "+900"
    # 反向：仅 bookmaker_b 应返 -1000
    out_b = convert_mod._build_odds_from_bookmaker(
        [bookmaker_b, bookmaker_a], "Arsenal", "Chelsea")
    assert out_b["homeML"] == "-1000"
    # 验证：交换 bookmaker 顺序 homeML 改变 → 锁定 bookmaker[0] 顺序敏感