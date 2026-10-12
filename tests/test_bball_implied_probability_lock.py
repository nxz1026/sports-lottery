"""P2-24 行为锁：``scripts/bball/predictor._implied_probability``。

函数仅 3 行（``1/close_odds``），但与 ``clv_baseline._devig_home``
（已 delegate market.devig）+ ``jc_view._implied``（已 delegate market.devig）
同属"赔率 → 隐含概率"路径。任何一边改了 1/v 公式或校验，其他两条
不知情地漂移。本测试守住 bball 端：

1. 数学结果 = 1/close_odds
2. 校验优先级：``implied_home``（已算好的概率）非空时优先返回；
   ``close_odds`` 仅在 implied_home 缺失 / 非法时才用
3. 边界：close_odds <= 0 / None → None
4. 边界：implied_home 不在 (0,1) → 退回看 close_odds
5. 极端大赔率（500.0）数值精度（1/500 = 0.002）
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _extract_implied_function():
    """``_implied_probability`` 是模块级私有函数，从模块 globals 直接拿。"""
    from bball import predictor
    fn = getattr(predictor, "_implied_probability", None)
    if fn is None:
        raise RuntimeError("_implied_probability not found in bball.predictor")
    return fn


def _implied(close_odds, implied_home):
    fn = _extract_implied_function()
    return fn(close_odds, implied_home)


def test_basic_1_over_close_odds():
    """close_odds=2.0 ⇒ 0.5。"""
    assert _implied(2.0, None) == 0.5


def test_implied_home_takes_priority_when_valid():
    """implied_home=0.7 (in (0,1)) → 优先返回 0.7，忽略 close_odds。"""
    assert _implied(2.0, 0.7) == 0.7


def test_implied_home_out_of_range_falls_back_to_close_odds():
    """implied_home=0 或 1 或 >1 → 非法，回退看 close_odds。"""
    assert _implied(2.0, 0) == 0.5
    assert _implied(2.0, 1) == 0.5
    assert _implied(2.0, 1.5) == 0.5
    assert _implied(2.0, -0.1) == 0.5


def test_close_odds_none_returns_none():
    """close_odds=None 且 implied_home=None → None（无可用概率）。"""
    assert _implied(None, None) is None


def test_close_odds_zero_or_negative_returns_none():
    """close_odds <= 0（非法赔率）→ None。"""
    assert _implied(0, None) is None
    assert _implied(-1.5, None) is None


def test_extreme_longshot_odds():
    """极大赔率（500.0）→ 极小概率（0.002），数值精度守住。"""
    assert _implied(500.0, None) == pytest.approx(0.002, abs=1e-9)


def test_implied_home_none_falls_back_to_close_odds():
    """implied_home=None（缺值）→ 回退到 close_odds 路径。"""
    assert _implied(2.0, None) == 0.5


# pytest import for approx
import pytest
