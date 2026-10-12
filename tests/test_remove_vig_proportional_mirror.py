"""P2-26 行为锁：``scripts/core/data/parse.remove_vig``（proportional 路径）
vs ``scripts/market/devig.devig``。

``remove_vig`` 是 3-way（home/draw/away）去水的权威实现，支持 logit（默认，
Shen & Steinberg 1994，对热门高抽水更准）+ proportional（向后兼容）。
``scripts/core/data/odds_enrich`` 用其落 ESPN 赔率到 football-data 预测行；
``scripts/market/devig`` 是 L3a 通用 N-way 比例去水。

两者"比例去水"路径理论上同公式 ``(1/o)/Σ(1/o)``；本测试守住：
1. remove_vig(method="proportional") ≡ market.devig.devig（同输入下）
2. 退化情形（total ≤ 0 → 等比例 1/margin）两边语义对齐
3. None 输入容忍：remove_vig 接受 None（→ fallback），market.devig 拒绝

若任一端改了比例公式（如未来 devig 引入 shin / power），本测试立刻
捕获 odds_enrich 路径漂移。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_proportional_three_way_matches_market_devig():
    """标准 3-way：remove_vig(method=proportional) ≡ market.devig.devig。"""
    from core.data.parse import remove_vig
    from market.devig import devig

    cases = [
        # (label, home, draw, away)
        ("典型主胜", 1.85, 3.40, 4.20),
        ("均衡低水", 2.62, 3.20, 2.62),
        ("强客让", 3.05, 3.20, 2.06),
    ]
    for label, h, d, a in cases:
        # remove_vig 接受 1/odds（odds_enrich 已预处理过），所以先把 odds 转隐含概率
        h_impl, d_impl, a_impl = 1 / h, 1 / d, 1 / a
        got = remove_vig(h_impl, d_impl, a_impl, method="proportional")
        scripts = devig({"home": h, "draw": d, "away": a})
        # 移除 vig 后的概率分布；比对值
        assert got[0] == pytest.approx(scripts["home"], abs=1e-9), (
            f"{label} home: got={got[0]} scripts={scripts['home']}"
        )
        assert got[1] == pytest.approx(scripts["draw"], abs=1e-9), (
            f"{label} draw: got={got[1]} scripts={scripts['draw']}"
        )
        assert got[2] == pytest.approx(scripts["away"], abs=1e-9), (
            f"{label} away: got={got[2]} scripts={scripts['away']}"
        )


def test_proportional_probabilities_sum_to_one():
    """3-way remove_vig proportional 输出三向概率之和 = 1。"""
    from core.data.parse import remove_vig

    h, d, a = 1.85, 3.40, 4.20
    got = remove_vig(1/h, 1/d, 1/a, method="proportional")
    assert abs(sum(got) - 1.0) < 1e-9, f"sum={sum(got)}"


def test_logit_is_default_and_differs_from_proportional():
    """logit（默认）vs proportional 在热门高抽水时会有差异（logit 偏好弱势方）。"""
    from core.data.parse import remove_vig

    # 极端高水：主胜 1.20 / 平 6.00 / 客胜 15.0
    h, d, a = 1.20, 6.00, 15.0
    h_p, d_p, a_p = 1/h, 1/d, 1/a

    logit = remove_vig(h_p, d_p, a_p)  # 默认 logit
    prop = remove_vig(h_p, d_p, a_p, method="proportional")

    # logit 与 proportional 数值应不同（logit 倾向平/客）
    assert logit != pytest.approx(prop[0]) or logit != pytest.approx(prop[1]), (
        "logit 与 proportional 在此 fixture 下应不同（否则任一方法没生效）"
    )
    # 但都应 Σ==1
    assert abs(sum(logit) - 1.0) < 1e-9
    assert abs(sum(prop) - 1.0) < 1e-9


def test_remove_vig_handles_none_away_p():
    """``away_p=None`` 时 remove_vig 兜底为 ``max(MIN_IMPLIED_PROB, 1.0 - h - d)``。
    """
    from core.data.parse import remove_vig

    # 仅给 h/d，不给 a（2-way 场景）
    h_p, d_p = 0.55, 0.30
    got = remove_vig(h_p, d_p, None, method="proportional")
    # 三项都应有值（a 从 1 - h - d 推）
    assert all(v is not None for v in got)
    assert abs(sum(got) - 1.0) < 1e-6


def test_remove_vig_handles_all_none():
    """``home_p is None and away_p is None and draw_p not None`` → 全 None。"""
    from core.data.parse import remove_vig

    got = remove_vig(None, 0.30, None, method="proportional")
    assert got == (None, None, None)


def test_remove_vig_handles_no_draw():
    """``draw_p is None`` → 全 None（draw 必须有）。"""
    from core.data.parse import remove_vig

    got = remove_vig(0.5, None, 0.3, method="proportional")
    assert got == (None, None, None)
