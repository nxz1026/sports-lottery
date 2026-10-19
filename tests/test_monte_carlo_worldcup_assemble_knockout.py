"""R36-3: scripts/core/model/monte_carlo_worldcup.py::_assemble_knockout 5 测 + D-17 锁定（Top 3）。

现状：淘汰赛种子装配 + 模拟入口函数 0 直测；唯一相关测试 R35-3 锁的是
`_build_r16_matchups`（配对器），本函数从未被直测。

D-17 bug-shaped 锁定（monte_carlo_worldcup.py:162-169）：`2 <= len(seeded) < 16`
时退化为 `_simulate_knockout_sequential`。历史 bug "该分支恒不可达，冠军静默
为 None" 已修复但易复发，回归测必须锁住退化路径生效。

3 大分支（elif ladder）：
- len(seeded) >= 16 → `_simulate_knockout_bracket`
- 2 <= len(seeded) < 16 → `_simulate_knockout_sequential`（D-17 退化兜底）
- len(seeded) < 2 → [] + warning（冠军 None 契约）

本轮补 5 测：
- < 2 队 → [] + warning（caplog 验证 "冠军为 None"）
- 4 队（2<=n<16）→ sequential 退化分支（D-17）
- 16 队（>=16）→ bracket 路径
- 组名乱序 → sorted 后仍正常装配（冠军返回）
- return 类型锁定（list[str]，champion 应为单队）
"""
from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def mc_wc_mod():
    spec = importlib.util.spec_from_file_location(
        "mc_wc_assemble_knockout_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strengths(teams):
    return {t: {"attack": 1.5, "defense": 1.0} for t in teams}


def _rounds(teams):
    return {t: [] for t in teams}


# ──────────────────────── _assemble_knockout ────────────────────────


def test_assemble_knockout_less_than_two_returns_empty_with_warning(mc_wc_mod, caplog):
    """R36-3：< 2 队 → [] + warning（冠军为 None 契约）。

    group_standings 单组 1 队 → seeded=[a1]，len=1 < 2。
    """
    with caplog.at_level("WARNING"):
        out = mc_wc_mod._assemble_knockout(
            {"A": ["a1"]}, {"a1": {"attack": 1.5, "defense": 1.0}},
            -0.1, {"a1": []})
    # D-17 退化兜底：< 2 返 []
    assert out == []
    # warning 含"冠军为 None"
    assert any("冠军为 None" in r.message for r in caplog.records)


def test_assemble_knockout_sequential_path_for_2_to_15_seeded(mc_wc_mod):
    """R36-3 D-17 锁定：2 <= len(seeded) < 16 走 `_simulate_knockout_sequential`。

    4 队（A1/A2/B1/B2）→ sequential 退化兜底。random.seed(42) 锁定结果。
    """
    random.seed(42)
    standings = {"A": ["a1", "a2"], "B": ["b1", "b2"]}
    teams = ["a1", "a2", "b1", "b2"]
    out = mc_wc_mod._assemble_knockout(
        standings, _strengths(teams), -0.1, _rounds(teams))
    # D-17 退化分支：sequential 至少返 1 队（冠军）
    assert len(out) >= 1
    assert isinstance(out[0], str)


def test_assemble_knockout_bracket_path_for_16_seeded(mc_wc_mod):
    """R36-3：len(seeded) >= 16 走 `_simulate_knockout_bracket`（R35-3 已锁的路径）。"""
    random.seed(42)
    standings = {grp: [f"{grp.lower()}1", f"{grp.lower()}2"]
                 for grp in "ABCDEFGH"}
    teams = [f"{grp.lower()}{i}" for grp in "ABCDEFGH" for i in [1, 2]]
    out = mc_wc_mod._assemble_knockout(
        standings, _strengths(teams), -0.1, _rounds(teams))
    # bracket 路径：冠军唯一
    assert len(out) == 1
    assert out[0] in teams


def test_assemble_knockout_group_order_sorted(mc_wc_mod):
    """R36-3：组名乱序（Z/A/C）→ sorted 后仍正常装配。

    `group_order = sorted(group_standings.keys())` 锁定字母序契约。
    """
    random.seed(42)
    unordered = {"Z": ["z1", "z2"], "A": ["a1", "a2"]}
    teams = ["a1", "a2", "z1", "z2"]
    out = mc_wc_mod._assemble_knockout(
        unordered, _strengths(teams), -0.1, _rounds(teams))
    # 至少返 1 队
    assert len(out) >= 1
    assert isinstance(out[0], str)


def test_assemble_knockout_returns_list_of_strings(mc_wc_mod):
    """R36-3：return 类型锁定 list[str]（冠军必须 str，None 之前 bug 锁定）。"""
    random.seed(42)
    standings = {"A": ["a1", "a2"], "B": ["b1", "b2"]}
    teams = ["a1", "a2", "b1", "b2"]
    out = mc_wc_mod._assemble_knockout(
        standings, _strengths(teams), -0.1, _rounds(teams))
    assert isinstance(out, list)
    for t in out:
        assert isinstance(t, str)
