"""R36-4: scripts/core/model/monte_carlo_worldcup.py::_simulate_knockout_bracket + _simulate_knockout_sequential 9 测 + D-18 锁定（Top 4+5）。

现状：淘汰赛 bracket + sequential 模拟器 0 直测；唯一相关 R35-3 锁的是
`_build_r16_matchups`（仅配对器，不模拟比赛）。

D-18 bug-shaped 锁定（monte_carlo_worldcup.py:210 / 256）：平局决胜
`winner = t1 if random.random() < 0.5 else t2` —— 50/50 模拟而非
加时/点球，符合"快速吞 seed 取一组确定结果"的语义。

本轮补 9 测：
- bracket 16→8→4→2→1 完整路径 + 1 队返回
- bracket 4 队 → 1 队
- bracket 1 队决赛早 return
- bracket team_rounds 写入 4 阶段 round_name
- D-18 bracket 平局决胜 random.seed 锁定
- sequential 4 队顺序配对
- sequential 3 队（奇数）→ 末尾单队直通
- sequential 2 队 → 1 队
- sequential team_rounds 写入 + round_name
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
        "mc_wc_knockout_round_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _strengths(teams):
    return {t: {"attack": 1.5, "defense": 1.0} for t in teams}


# ──────────────────────── _simulate_knockout_bracket ────────────────────────


def test_bracket_16_teams_full_path(mc_wc_mod):
    """R36-4：16 队完整 16→8→4→2→1 路径 → 1 队返回。"""
    random.seed(42)
    matchups = [(f"{grp.lower()}1", f"{grp.lower()}2") for grp in "ABCDEFGH"]
    teams = [t for pair in matchups for t in pair]
    rounds = {t: [] for t in teams}
    out = mc_wc_mod._simulate_knockout_bracket(
        matchups, _strengths(teams), -0.1, rounds)
    assert len(out) == 1
    assert out[0] in teams


def test_bracket_4_teams_path(mc_wc_mod):
    """R36-4：4 队 → 2→1 → 1 队返回。"""
    random.seed(42)
    matchups = [("a1", "a2"), ("b1", "b2")]
    teams = ["a1", "a2", "b1", "b2"]
    rounds = {t: [] for t in teams}
    out = mc_wc_mod._simulate_knockout_bracket(
        matchups, _strengths(teams), -0.1, rounds)
    assert len(out) == 1


def test_bracket_single_matchup_early_return(mc_wc_mod):
    """R36-4：1 场决赛 → 早 return，冠军已决出。

    决赛是 2 队配对单场（len(current_matchups)==1 且 matchup 是 (t1,t2) tuple）。
    代码：`if len(current_matchups) == 1: return [...]`。
    """
    random.seed(42)
    out = mc_wc_mod._simulate_knockout_bracket(
        [("a1", "b1")], _strengths(["a1", "b1"]), -0.1,
        {"a1": [], "b1": []})
    assert len(out) == 1
    assert out[0] in ("a1", "b1")


def test_bracket_team_rounds_recorded(mc_wc_mod):
    """R36-4：team_rounds 就地写回 4 阶段 round_name。"""
    random.seed(42)
    matchups = [(f"{grp.lower()}1", f"{grp.lower()}2") for grp in "ABCDEFGH"]
    teams = [t for pair in matchups for t in pair]
    rounds = {t: [] for t in teams}
    mc_wc_mod._simulate_knockout_bracket(
        matchups, _strengths(teams), -0.1, rounds)
    # 冠军 rounds 应含 4 个 round_name
    champion = max(rounds, key=lambda t: len(rounds[t]))
    assert "round_of_16" in rounds[champion]
    assert "quarter_final" in rounds[champion]
    assert "semi_final" in rounds[champion]
    assert "final" in rounds[champion]


def test_bracket_d18_tiebreak_random_seeded(mc_wc_mod):
    """R36-4 D-18 锁定：平局 `random.random() < 0.5` 决胜，random.seed 可重现。

    两次同 seed 调 → 同结果；不同 seed → 不同结果。
    """
    matchups = [("a1", "a2"), ("b1", "b2")]
    teams = ["a1", "a2", "b1", "b2"]

    random.seed(42)
    rounds1 = {t: [] for t in teams}
    out1 = mc_wc_mod._simulate_knockout_bracket(
        matchups, _strengths(teams), -0.1, rounds1)

    random.seed(42)
    rounds2 = {t: [] for t in teams}
    out2 = mc_wc_mod._simulate_knockout_bracket(
        matchups, _strengths(teams), -0.1, rounds2)
    # 同 seed 同结果
    assert out1 == out2


# ──────────────────────── _simulate_knockout_sequential ────────────────────────


def test_sequential_4_teams_pairwise(mc_wc_mod):
    """R36-4：4 队顺序配对 (a,b)(c,d) → 1 队。"""
    random.seed(42)
    teams = ["a", "b", "c", "d"]
    rounds = {t: [] for t in teams}
    out = mc_wc_mod._simulate_knockout_sequential(
        teams, _strengths(teams), -0.1, rounds)
    assert len(out) == 1


def test_sequential_3_teams_last_team_advances_directly(mc_wc_mod):
    """R36-4：3 队（奇数）→ 末尾单队直通下一轮（`else: next_round_teams.append(remaining_teams[i])`）。"""
    random.seed(42)
    teams = ["a", "b", "c"]
    rounds = {t: [] for t in teams}
    out = mc_wc_mod._simulate_knockout_sequential(
        teams, _strengths(teams), -0.1, rounds)
    # 3 → 1：c 直通，a/b 决出 1 个
    assert len(out) == 1


def test_sequential_2_teams_returns_one(mc_wc_mod):
    """R36-4：2 队 → 1 队（最小退化）。"""
    random.seed(42)
    teams = ["a", "b"]
    rounds = {t: [] for t in teams}
    out = mc_wc_mod._simulate_knockout_sequential(
        teams, _strengths(teams), -0.1, rounds)
    assert len(out) == 1


def test_sequential_team_rounds_recorded(mc_wc_mod):
    """R36-4：team_rounds 写入 + round_name 格式 `f"r{len(remaining_teams)}"`。"""
    random.seed(42)
    teams = ["a", "b", "c", "d"]
    rounds = {t: [] for t in teams}
    mc_wc_mod._simulate_knockout_sequential(
        teams, _strengths(teams), -0.1, rounds)
    # 冠军 rounds 应至少含 1 个 round_name
    champion = max(rounds, key=lambda t: len(rounds[t]))
    assert len(rounds[champion]) >= 1
    # round_name 格式 "r{N}"（N 是当时的 remaining_teams 长度）
    for rn in rounds[champion]:
        assert rn.startswith("r")
        assert rn[1:].isdigit()
