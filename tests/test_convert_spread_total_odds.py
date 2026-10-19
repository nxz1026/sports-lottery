"""R36-1: scripts/core/data/convert.py::_build_spread_total_odds 5 测直测（Top 1）。

现状：Handicap → pointSpread + Over/Under → total 转换 0 直测；
ESPN odds 链路核心。handicap val 需含队名（含 "Home"/"Away" + 队名 + 数字）
否则 side_key 跳过。

D-16 bug-shaped 锁定（convert.py:84）：over_under 缺线时硬编码
"2.5" 兜底。笼统兜底可能在 NBA/NCAA 季节性低分时污染分布。

本轮补 5 测：
- handicap 含 home 队名 + over → pointSpread.home + total.over
- handicap 含 away 队名 + under → pointSpread.away + total.under
- handicap 缺队名 → continue 跳过 → 仅 total（如有）/ {}
- handicap/over_under 都 None → {}
- D-16：over "Over" 缺数字 → line_total="2.5" 兜底
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
        "convert_spread_total_odds_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _build_spread_total_odds ────────────────────────


def test_build_spread_total_odds_home_spread_and_over(convert_mod):
    """R36-1：handicap 含 "Home Arsenal" + over → pointSpread.home + total.over。"""
    out = convert_mod._build_spread_total_odds(
        handicap={"values": [{"value": "Home Arsenal -1.5", "odd": "1.91"}]},
        over_under={"values": [{"value": "Over 2.5", "odd": "1.85"}]},
        home_team="Arsenal", away_team="Chelsea")
    # pointSpread.home 设
    assert "pointSpread" in out
    assert "home" in out["pointSpread"]
    ps = out["pointSpread"]["home"]
    assert ps["open"]["line"] == "-1.5"
    assert ps["open"]["odds"] == "-110"  # 1.91 favorite → -110
    # total.over 设
    assert "total" in out
    assert "over" in out["total"]
    assert out["total"]["over"]["open"]["line"] == "2.5"


def test_build_spread_total_odds_away_spread_and_under(convert_mod):
    """R36-1：handicap 含 "Away Chelsea" + under → pointSpread.away + total.under。"""
    out = convert_mod._build_spread_total_odds(
        handicap={"values": [{"value": "Away Chelsea +0.5", "odd": "1.91"}]},
        over_under={"values": [{"value": "Under 3.0", "odd": "1.85"}]},
        home_team="Arsenal", away_team="Chelsea")
    assert "away" in out["pointSpread"]
    assert out["pointSpread"]["away"]["open"]["line"] == "+0.5"
    assert "under" in out["total"]
    assert out["total"]["under"]["open"]["line"] == "3.0"


def test_build_spread_total_odds_handicap_missing_team_skipped(convert_mod):
    """R36-1：handicap val 不含 home/away 队名 → continue 跳过（不污染 dict）。"""
    out = convert_mod._build_spread_total_odds(
        handicap={"values": [{"value": "Other -0.5", "odd": "1.91"}]},
        over_under=None,
        home_team="Arsenal", away_team="Chelsea")
    # handicap 跳过 → 无 pointSpread
    assert "pointSpread" not in out


def test_build_spread_total_odds_both_none_returns_empty(convert_mod):
    """R36-1：handicap/over_under 都 None → {}（兜底）。"""
    out = convert_mod._build_spread_total_odds(
        None, None, "Arsenal", "Chelsea")
    assert out == {}


def test_build_spread_total_odds_d16_default_line_2_5_when_missing(convert_mod):
    """R36-1 D-16 锁定：over_under val="Over" 缺数字 → line_total 硬编码 "2.5" 兜底。

    val.split(" ") 无空格 → line_total = "2.5"（默认）。业务白盒默认：足球
    中位进球 2.5；NBA/NCAA 季节性低分会污染。锁定现状防 silent 切换时回归。
    """
    out = convert_mod._build_spread_total_odds(
        handicap=None,
        over_under={"values": [{"value": "Over", "odd": "1.85"}]},
        home_team="Arsenal", away_team="Chelsea")
    assert "total" in out
    assert "over" in out["total"]
    # D-16：缺线硬编码 "2.5"
    assert out["total"]["over"]["open"]["line"] == "2.5"
    assert out["total"]["over"]["close"]["line"] == "2.5"
