"""R30 #1: scripts/store/jc_view.py::_nums 6 测直测。

现状：test_jc_view_aggregations.py 21 测覆盖 _fetch / _implied / fixtures_on /
issues / lottery_draws / issue_matches / jc_movement /
latest_business_date_cache，但 _nums（h/d/a 温和提取私有 helper）完全跳过。

本轮补 6 测，覆盖所有 try/except 路径：
- 3 float / 数值字符串 / 缺键 / None 值 / 非数字字符串 / bool 边界 + 多余键
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def jc_view_mod():
    """加载 jc_view.py 为唯一 module name。"""
    spec = importlib.util.spec_from_file_location(
        "jc_view_nums_test", REPO_ROOT / "scripts" / "store" / "jc_view.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _nums ────────────────────────


def test_nums_all_three_floats_pass_through(jc_view_mod):
    """R30 #1：h/d/a 三 float 齐全 → 直接透传。"""
    out = jc_view_mod._nums({"h": 1.5, "d": 2.5, "a": 3.5})
    assert out == {"h": 1.5, "d": 2.5, "a": 3.5}


def test_nums_numeric_strings_coerced(jc_view_mod):
    """R30 #1：数值字符串（"2.5" / "3" / "4"）→ float 转换。"""
    out = jc_view_mod._nums({"h": "2.5", "d": "3", "a": "4"})
    assert out == {"h": 2.5, "d": 3.0, "a": 4.0}


def test_nums_missing_key_returns_none(jc_view_mod):
    """R30 #1：缺键（如无 "d"）→ 缺键位 None（KeyError 兜底）。"""
    out = jc_view_mod._nums({"h": 1.5, "a": 3.5})  # 缺 "d"
    assert out == {"h": 1.5, "d": None, "a": 3.5}


def test_nums_none_value_returns_none(jc_view_mod):
    """R30 #1：value=None → None（TypeError 兜底，因 float(None) 抛 TypeError）。"""
    out = jc_view_mod._nums({"h": None, "d": 2.0, "a": 3.0})
    assert out == {"h": None, "d": 2.0, "a": 3.0}


def test_nums_unparseable_string_returns_none(jc_view_mod):
    """R30 #1：非数字字符串 "abc" → None（ValueError 兜底）。"""
    out = jc_view_mod._nums({"h": "abc", "d": "x", "a": "?"})
    assert out == {"h": None, "d": None, "a": None}


def test_nums_bool_coerces_to_one_zero_and_extras_ignored(jc_view_mod):
    """R30 #1：bool 是 int 子类，float(True)=1.0 / float(False)=0.0；多余键忽略。"""
    # bool 边界：True/False 不会被 ValueError 拦截，会转成 1.0/0.0
    out = jc_view_mod._nums({"h": True, "d": False, "a": 1, "extra": 99})
    # 注意：True/False 转 float 是 1.0/0.0；a=1 (int) → 1.0
    assert out["h"] == 1.0
    assert out["d"] == 0.0
    assert out["a"] == 1.0
    # 仅 h/d/a 三键
    assert set(out) == {"h", "d", "a"}
