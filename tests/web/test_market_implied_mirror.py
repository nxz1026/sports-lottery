"""P2-4 算法镜像对账 #5：web/_market_projection 的 1/odds 内联
vs scripts/market/devig.implied / devig / margin。

两份独立实现，相同公式 ``s = Σ(1/odds); p_k = (1/odds_k)/s``。
若任一端漂移（scripts 收紧精度阈值 / web 改去水策略），每日一图
``devig_probs`` + ``margin`` 静默错位且无单测捕获。本测试固定一组 1X2
fixture，断言两端输出在 1e-9 内相等。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# 几组实战赔率（涵盖小水/大水/临界 1.01），每组 home/draw/away 严格按序。
_FIXTURES = [
    # (label, decimals_dict)
    ("典型 1X2", {"home": 1.85, "draw": 3.40, "away": 4.20}),
    ("大水 8% 庄家", {"home": 1.95, "draw": 3.60, "away": 4.50}),
    ("均衡低水", {"home": 2.62, "draw": 3.20, "away": 2.62}),
    ("极限低水 1%", {"home": 1.01, "draw": 15.0, "away": 80.0}),
]


def _build_doc(decimals: dict[str, float]) -> dict:
    """构造 _market_projection 期望的 doc 结构（1X2 完整源 + 完整 captures）。"""
    return {
        "match": "Test vs Mirror",
        "market": {
            "source": "test",
            "captured_at": "2026-01-01T00:00:00+08:00",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {k: {"decimal_odds": v} for k, v in decimals.items()},
        },
    }


def _doc_with_flat_selections(decimals: dict[str, float]) -> dict:
    """selections 直接是数值（不是 dict）的分支也要覆盖。"""
    doc = _build_doc(decimals)
    doc["market"]["selections"] = dict(decimals)
    return doc


def test_implied_probs_match_scripts_market_implied():
    """``implied_probs`` = 1/odds：web 与 scripts 一致。"""
    from market.devig import implied as scripts_implied

    from web.routers.predictions import _market_projection

    for label, decimals in _FIXTURES:
        proj = _market_projection(_build_doc(decimals))
        assert proj["status"] == "complete", f"{label}: web 未走 complete 分支"
        web_implied = proj["implied_probs"]
        s_implied = scripts_implied(decimals)
        for key in ("home", "draw", "away"):
            assert math.isclose(web_implied[key], s_implied[key], rel_tol=1e-9, abs_tol=1e-12), (
                f"{label} {key}: web={web_implied[key]} scripts={s_implied[key]}"
            )


def test_devig_probs_match_scripts_market_devig():
    """``devig_probs`` = (1/odds)/Σ(1/odds)：去水后 Σ==1，web 与 scripts 一致。"""
    from market.devig import devig as scripts_devig

    from web.routers.predictions import _market_projection

    for label, decimals in _FIXTURES:
        proj = _market_projection(_build_doc(decimals))
        web_devig = proj["devig_probs"]
        s_devig = scripts_devig(decimals)
        for key in ("home", "draw", "away"):
            assert math.isclose(web_devig[key], s_devig[key], rel_tol=1e-9, abs_tol=1e-12), (
                f"{label} {key}: web={web_devig[key]} scripts={s_devig[key]}"
            )
        # Σ == 1
        assert math.isclose(sum(web_devig.values()), 1.0, abs_tol=1e-9), (
            f"{label}: web Σ={sum(web_devig.values())}"
        )
        assert math.isclose(sum(s_devig.values()), 1.0, abs_tol=1e-9), (
            f"{label}: scripts Σ={sum(s_devig.values())}"
        )


def test_margin_matches_scripts_market_margin():
    """``margin`` = Σ(1/odds) - 1：庄家水钱比例。"""
    from market.devig import margin as scripts_margin

    from web.routers.predictions import _market_projection

    for label, decimals in _FIXTURES:
        proj = _market_projection(_build_doc(decimals))
        web_margin = proj["margin"]
        s_margin = scripts_margin(decimals)
        assert math.isclose(web_margin, s_margin, rel_tol=1e-9, abs_tol=1e-12), (
            f"{label}: web margin={web_margin} scripts={s_margin}"
        )


def test_selections_can_be_flat_numbers():
    """selections 项是裸数值（不是 ``{"decimal_odds": x}``）也应走 complete 分支。"""
    from market.devig import devig as scripts_devig

    from web.routers.predictions import _market_projection

    decimals = {"home": 1.85, "draw": 3.40, "away": 4.20}
    proj = _market_projection(_doc_with_flat_selections(decimals))
    assert proj["status"] == "complete"
    for key in ("home", "draw", "away"):
        assert math.isclose(proj["devig_probs"][key], scripts_devig(decimals)[key],
                            rel_tol=1e-9, abs_tol=1e-12)


def test_invalid_odds_skip_complete_branch():
    """非 1x2 / 缺源 / 赔率 <=1 / 缺 key 都不应进 complete 分支（防静默错位）。"""
    from web.routers.predictions import _market_projection

    # market_type != 1x2
    doc = _build_doc({"home": 1.85, "draw": 3.40, "away": 4.20})
    doc["market"]["market_type"] = "hhad"
    assert _market_projection(doc)["status"] == "partial"

    # 缺 captured_at
    doc = _build_doc({"home": 1.85, "draw": 3.40, "away": 4.20})
    doc["market"].pop("captured_at")
    assert _market_projection(doc)["status"] == "partial"

    # 赔率 <=1
    doc = _build_doc({"home": 1.0, "draw": 3.40, "away": 4.20})
    assert _market_projection(doc)["status"] == "partial"

    # 缺 away key
    doc = _build_doc({"home": 1.85, "draw": 3.40})
    # 自己改 selections 让 home/draw 不带 away
    doc["market"]["selections"] = {"home": {"decimal_odds": 1.85}, "draw": {"decimal_odds": 3.40}}
    assert _market_projection(doc)["status"] == "partial"

    # 非数字
    doc = _build_doc({"home": 1.85, "draw": 3.40, "away": 4.20})
    doc["market"]["selections"]["home"] = {"decimal_odds": "not-a-number"}
    assert _market_projection(doc)["status"] == "partial"
