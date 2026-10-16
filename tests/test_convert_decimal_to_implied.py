"""R35-1: scripts/core/data/convert.py::_decimal_to_implied 4 测直测。

现状：football-data.org / API-Football → ESPN 格式转换入口 helper，
上游多个 caller 依赖（_build_moneyline_odds / _build_spread_total_odds /
_build_odds_from_bookmaker），0 直测。

3 分支：
- decimal_str=None → None
- d > 0 → 1.0/d（合法）
- d <= 0 或 ValueError/TypeError → None（兜底）

本轮补 4 测：
- None → None
- 正常 decimal ("2.00") → 0.5
- 边界：d=0 → None（防 div-by-zero）
- 非法字符串 → None（ValueError 兜底）
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
        "convert_decimal_to_implied_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _decimal_to_implied ────────────────────────


def test_decimal_to_implied_none_returns_none(convert_mod):
    """R35-1：decimal_str=None → None（不做 default fallback，调用方决定）。"""
    assert convert_mod._decimal_to_implied(None) is None


def test_decimal_to_implied_normal_decimal_returns_probability(convert_mod):
    """R35-1：合法 decimal → 1/d（"2.00" → 0.5；"4.00" → 0.25）。"""
    assert convert_mod._decimal_to_implied("2.00") == 0.5
    assert convert_mod._decimal_to_implied("4.00") == 0.25
    assert convert_mod._decimal_to_implied("1.50") == pytest.approx(2 / 3, rel=1e-9)


def test_decimal_to_implied_zero_returns_none(convert_mod):
    """R35-1：d <= 0 边界 → None（防 div-by-zero + 非法赔率）。"""
    assert convert_mod._decimal_to_implied("0") is None
    assert convert_mod._decimal_to_implied("0.0") is None
    assert convert_mod._decimal_to_implied("-1.5") is None


def test_decimal_to_implied_invalid_string_returns_none(convert_mod):
    """R35-1：非法字符串 → None（ValueError 兜底，浮点解析容错）。"""
    assert convert_mod._decimal_to_implied("not_a_number") is None
    assert convert_mod._decimal_to_implied("") is None
    assert convert_mod._decimal_to_implied("2.5.5") is None  # 多点
    assert convert_mod._decimal_to_implied([]) is None  # list 走 TypeError 路径
    assert convert_mod._decimal_to_implied({}) is None  # dict 走 TypeError 路径