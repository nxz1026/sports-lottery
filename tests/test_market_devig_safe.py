"""``market.devig.safe_devig`` 行为锁 — P5-2 收口点的输入矩阵。

``safe_devig`` 是 ``devig`` 的 lenient 包装，集中承担"过滤非法 + 单边退化"两个
职责，让 ``jc_view._implied`` 和 ``clv_baseline._devig_home`` 不再各持一份。
本测试按契约矩阵覆盖：

- 全空 / 全无效 → ``None``
- 1 项有效 + ``allow_single=True`` → ``{key: 1.0}``
- 1 项有效 + ``allow_single=False``（默认）→ ``None``
- 2 项有效（h2h 场景）→ 调 ``devig`` 出 1 / (1 - margin) 归一
- 3 项有效（1X2 场景）→ 调 ``devig`` 出 1 / (1 - margin) 归一
- 非法项过滤（None / bool / 字符串 / inf / 0 / 1.0 / <= 1）
- 与 ``devig`` 在合法输入上数学一致（h2h 与 1X2 各取两个 fixture）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from market.devig import devig, safe_devig  # noqa: E402


# ────────────────────────── 边界 / 过滤语义 ──────────────────────────


@pytest.mark.parametrize("odds", [
    {},                                                       # 全空
    {"h": None, "d": None, "a": None},                        # 全 None
    {"h": "abc", "d": "x"},                                   # 全字符串
    {"h": 0, "d": 0.5, "a": -1},                              # 全部 <= 1
    {"h": 0, "d": None, "a": "x"},                            # 全部非法
])
def test_zero_valid_returns_none(odds):
    assert safe_devig(odds) is None
    assert safe_devig(odds, allow_single=True) is None


def test_allow_single_one_valid_returns_implied_one():
    """单边退化：1 项有效 + ``allow_single=True`` → 返 ``{key: 1.0}``。"""
    assert safe_devig({"h": 1.85}, allow_single=True) == {"h": 1.0}
    assert safe_devig({"a": 4.20}, allow_single=True) == {"a": 1.0}
    # 即便有其他非法项，单项胜出
    assert safe_devig({"h": 1.85, "d": None, "a": 0.5}, allow_single=True) == {"h": 1.0}


def test_allow_single_false_one_valid_returns_none():
    """1 项有效 + ``allow_single=False``（默认）→ ``None``（h2h 场景必 2-side）。"""
    assert safe_devig({"h": 1.85}) is None
    assert safe_devig({"a": 4.20}) is None
    # 1 项有效 + 多项非法 → 仍 None
    assert safe_devig({"h": 1.85, "d": "x", "a": None}) is None


# ────────────────────────── 非法项过滤（不影响 devig 路径）─────────


@pytest.mark.parametrize("bad_value", [None, "abc", True, False, 0, 1, 0.5, 1.0, -1.0, float("inf"), float("nan")])
def test_filters_individual_bad_values(bad_value):
    """单条非法值被过滤；剩余若 ≥2 仍走 devig。"""
    odds = {"h": 1.85, "d": 3.40, "a": 4.20, "x_bad": bad_value}
    got = safe_devig(odds)
    expected = devig({"h": 1.85, "d": 3.40, "a": 4.20})
    assert got == expected
    assert "x_bad" not in got


# ────────────────────────── 与 devig 数学一致 ──────────────────────────


@pytest.mark.parametrize("odds", [
    {"home": 1.85, "away": 2.05},                             # h2h 主胜偏低
    {"home": 2.10, "away": 1.55},                             # h2h 客胜偏低
    {"home": 1.91, "away": 1.91},                             # 完全对称
    {"home": 1.30, "away": 9.00},                             # 强主
    {"home": 5.50, "away": 1.40},                             # 强客
])
def test_two_side_equals_devig(odds):
    assert safe_devig(odds) == devig(odds)


@pytest.mark.parametrize("odds", [
    {"h": 1.85, "d": 3.40, "a": 4.20},                       # 标准 1X2
    {"h": 2.10, "d": 3.30, "a": 3.60},
    {"h": 1.30, "d": 5.50, "a": 9.00},
    {"h": 1.75, "d": 3.60, "a": 4.80},
    {"h": 1.55, "d": 4.00, "a": 6.25},
    {"h": 2.05, "d": 3.15, "a": 3.90},
])
def test_three_way_equals_devig(odds):
    assert safe_devig(odds) == devig(odds)


# ────────────────────────── 数值精度 ──────────────────────────


def test_no_rounding_in_safe_devig():
    """safe_devig 不 round —— caller 决定精度（jc_view 3dp / clv_baseline 原值）。"""
    odds = {"h": 1.85, "d": 3.40, "a": 4.20}
    got = safe_devig(odds)
    # devig 默认 full float；jc_view 后处理才 round
    assert got["h"] == 0.5038814396612561
    assert got["d"] == 0.2741707833450953
    assert got["a"] == 0.22194777699364857


def test_result_keys_preserved():
    """过滤后 key 集合保持 devig 行为（dict 序）。"""
    odds = {"z": 1.85, "a": 3.40, "m": 4.20}
    got = safe_devig(odds)
    assert set(got) == {"z", "a", "m"}


# ────────────────────────── 退化情形: 过滤后剩 0/1 → allow_single 开关 ──────


def test_filter_leaves_one_valid_allow_single_true():
    odds = {"h": 1.85, "d": None, "a": 0, "x": "abc"}
    assert safe_devig(odds, allow_single=True) == {"h": 1.0}
    assert safe_devig(odds) is None  # allow_single=False


def test_filter_leaves_zero_valid_even_with_allow_single():
    odds = {"h": 1.0, "d": None, "a": "abc"}  # 1.0 视为非法（<=1）
    assert safe_devig(odds, allow_single=True) is None
    assert safe_devig(odds) is None
