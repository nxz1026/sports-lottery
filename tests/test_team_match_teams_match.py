"""R48-1: web/services/team_match.py::teams_match 5 cases direct + D-83 lock.

teams_match (L64-L75, 12 行) 判断 (h1, a1) 与 (h2, a2) 是否同一场 (主客可
互换但同一场)。依赖 _canon 模糊匹配 (D-74 已锁)。

D-74/D-75/D-77/D-81/D-82 已锁 _canon / odd_suggestion / jc_gap_note /
nba_rows / build_rows, 但 teams_match 真函数自身核心契约 (主客同序 +
主客反序 + 完全不等 + 别名 fuzzy 命中) 0 直测 (R19/R44-2/R45-1 走
teams_match_safe wrapper, 真函数 0 直测)。

D-83 锁定 5 契约:
- D-83-1: 主客同序精确匹配 (h1==h2, a1==a2) -> True
- D-83-2: 主客反序匹配 (h1==a2, a1==h2) -> True (容错反序)
- D-83-3: 完全不等 (无交集) -> False
- D-83-4: 别名 fuzzy 命中 (D-74 _canon 扩展集相交) -> True
- D-83-5: 单边相等 (只主队同) -> False (两侧必须同时匹配)

依赖: _canon 真跑 (D-74 已锁); norm_name 真跑 (D-74 已锁)。
Stub: 无 (纯函数, 走 _canon 真链)。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def tm_mod():
    """Spec-load web/services/team_match.py。"""
    spec = importlib.util.spec_from_file_location(
        "team_match_teams_match_test",
        REPO_ROOT / "web" / "services" / "team_match.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-83 五契约 ────────────────────────


def test_teams_match_d83_1_same_order_exact_match(tm_mod):
    """R48-1 D-83-1: 主客同序精确匹配 -> True。

    (阿森纳, 切尔西) == (阿森纳, 切尔西) -> _canon 集合完全相等 -> True。
    """
    assert tm_mod.teams_match("阿森纳", "切尔西", "阿森纳", "切尔西") is True
    # 英文
    assert tm_mod.teams_match("Arsenal", "Chelsea", "Arsenal", "Chelsea") is True
    # 空白归一化 (D-74 norm_name)
    assert tm_mod.teams_match("  阿森纳  ", "  切尔西  ", "阿森纳", "切尔西") is True


def test_teams_match_d83_2_reversed_order_returns_true(tm_mod):
    """R48-1 D-83-2: 主客反序匹配 -> True (容错)。

    (阿森纳, 切尔西) == (切尔西, 阿森纳) -> 反序命中 (L73-74)。
    """
    assert tm_mod.teams_match("阿森纳", "切尔西", "切尔西", "阿森纳") is True
    assert tm_mod.teams_match("A", "B", "B", "A") is True


def test_teams_match_d83_3_completely_different_returns_false(tm_mod):
    """R48-1 D-83-3: 完全不等 -> False。

    (阿森纳, 切尔西) vs (曼城, 曼联) 无交集 -> False。
    """
    assert tm_mod.teams_match("阿森纳", "切尔西", "曼城", "曼联") is False
    assert tm_mod.teams_match("A", "B", "C", "D") is False


def test_teams_match_d83_4_alias_fuzzy_match_via_canon(tm_mod):
    """R48-1 D-83-4: 别名 fuzzy 命中 (D-74 _canon 扩展集相交) -> True。

    LOCK: 盘口与预测两侧都用 D-74 锁定的中文别名, _canon 扩展集相交
    才能同时 S1h ∩ S2h AND S1a ∩ S2a (主客同序) 或反序。
    """
    # 盘口 "阿仙奴/車路士" vs 预测 "阿森纳/切尔西" (两侧都别名)
    assert tm_mod.teams_match("阿仙奴", "車路士", "阿森纳", "切尔西") is True
    # "曼彻斯特联/李斯特城" vs "曼联/莱斯特城" (两侧都别名)
    assert tm_mod.teams_match("曼彻斯特联", "李斯特城",
                                "曼联", "莱斯特城") is True
    # 反序: "阿森纳/切尔西" vs "阿仙奴/車路士" (alias 也命中)
    assert tm_mod.teams_match("阿森纳", "切尔西", "阿仙奴", "車路士") is True


def test_teams_match_d83_5_only_one_side_matches_returns_false(tm_mod):
    """R48-1 D-83-5: 单边相等 -> False (两侧必须同时匹配)。

    主队相同但客队不同 -> S1h ∩ S2h=True 但 S1a ∩ S2a=False -> False。
    """
    assert tm_mod.teams_match("阿森纳", "切尔西", "阿森纳", "曼城") is False
    # 主客同序但客队不同
    assert tm_mod.teams_match("A", "B", "A", "C") is False
    # 反序但只有主客互换
    assert tm_mod.teams_match("A", "B", "C", "B") is False
