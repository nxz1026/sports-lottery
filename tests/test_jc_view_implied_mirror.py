"""P2-23 镜像锁 + P5-2 收口：``scripts/store/jc_view._implied`` ≡ ``scripts/market/devig.safe_devig``。

``_implied`` 是 SQL 输出端赔率 → 归一化隐含概率的 helper。P2-23 把它
delegate 到 ``market.devig.devig``；P5-2 进一步把 lenient 过滤（None / <=1 /
非数值）收口到 ``market.devig.safe_devig``，本模块 ``_implied`` 只负责
3 位小数 round。P5-2 起 ``_implied`` 提到模块级（不再嵌套在
``jc_movement`` 里），可直接 import。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _implied_test(nums):
    """``_implied`` 模块级函数（P5-2 起），直接 import。"""
    from store import jc_view
    return jc_view._implied(nums)


def test_valid_had_three_way_matches_market_devig():
    """标准 1X2（h/d/a 三项齐）→ _implied ≡ market.devig.devig（同公式 3dp round）。"""
    from market.devig import devig

    nums = {"h": 1.85, "d": 3.40, "a": 4.20}
    got = _implied_test(nums)
    expected = devig(nums)
    expected_rounded = {k: round(expected[k], 3) for k in expected}
    assert got == expected_rounded


def test_had_only_two_odds_one_zero_skipped():
    """SQL 输出 a 为 0/None → 该项静默跳过，剩余两项归一化。"""
    from market.devig import devig

    # a 为 0 → _implied 跳过 a，剩余 h/d
    nums = {"h": 1.85, "d": 3.40, "a": 0}
    got = _implied_test(nums)
    # 市场 devig 会拒绝（含 a=0）
    with pytest.raises(ValueError):
        devig(nums)
    # _implied 应得 h+d 两项
    expected = devig({"h": 1.85, "d": 3.40})
    assert got == {k: round(expected[k], 3) for k in expected}


def test_had_only_two_odds_one_none_skipped():
    """SQL 输出 a 为 None → 同样跳过。"""
    got = _implied_test({"h": 1.85, "d": 3.40, "a": None})
    assert set(got) == {"h", "d"}
    assert all(v > 0 and v < 1 for v in got.values())


def test_had_one_returns_implied_one():
    """退化情形：只 1 项有效 odds → _implied 返 ``{key: 1.0}``（单边胜率 100%）。

    与历史 ABI 兼容（jc_view 旧实现也是这行为）；``market.devig`` 至少要 2 项
    才能算 Σ=1（"至少需要 2 个选项" ValueError）。
    """
    got = _implied_test({"h": 1.85})
    assert got == {"h": 1.0}
    # market.devig 拒绝单边
    from market.devig import devig
    with pytest.raises(ValueError):
        devig({"h": 1.85})


def test_all_none_returns_empty():
    """SQL 全 None → 返空。"""
    assert _implied_test({"h": None, "d": None, "a": None}) == {}


def test_odds_at_one_skipped():
    """odds == 1.0 边界 → 跳过（市场 devig 同样拒收 =1）。"""
    got = _implied_test({"h": 1.0, "d": 3.40, "a": 4.20})
    assert "h" not in got
    assert set(got) == {"d", "a"}


def test_had_huge_underdog_skewed():
    """极大客胜赔率（50.0）+ 小主胜：归一化偏向主胜。"""
    from market.devig import devig

    nums = {"h": 1.05, "d": 12.0, "a": 50.0}
    got = _implied_test(nums)
    expected = devig(nums)
    expected_rounded = {k: round(expected[k], 3) for k in expected}
    assert got == expected_rounded
    # 主胜隐含概率应该远高于平/客
    assert got["h"] > 0.9


def test_source_level_devig_integration():
    """源码层验证：``jc_view`` 源码已删内联 ``1/v`` 累加，改为 delegate ``safe_devig``。

    防止后续有人 PR 把 ``safe_devig(...)`` 改成重新内联（漂移回退）。
    P5-2 起 call 形式从 ``devig(valid)`` 升级到 ``safe_devig(..., allow_single=True)``。
    """
    from store import jc_view
    import inspect
    src = inspect.getsource(jc_view)
    # 旧实现特征：sum(1/v for ...) 或 (1/nums[k])/s
    assert "sum(1 / v for" not in src, (
        "jc_view 又出现内联 1/v 累加，应已 delegate 到 market.devig"
    )
    # 新实现特征：safe_devig
    assert "safe_devig" in src, (
        "jc_view 没 delegate 到 market.devig.safe_devig"
    )
