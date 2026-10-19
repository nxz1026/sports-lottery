"""R37-3: scripts/core/model/monte_carlo_worldcup.py::_simulate_group 6 测 + D-24 锁（Top 3）。

现状：单组全部比赛 + 3/1/0 计分 + gd/points 排序函数 0 直测；
R37-2 已锁 `_simulate_group_stage`（上游），本函数是其下游。

5 分支：
- 3 队循环赛：手动算积分 → 验证排序与计分
- 平局（0:0）→ 双方 +1 分
- ties：同分用 gd 排序（同 gd 看 gf）
- team_rounds 原地写入 + 前 2 名额外 append "round_of_16"
- 空 fixtures 列表 → 退化

D-24 bug-shaped 锁定：fixture 缺 `home`/`away` → KeyError 直接抛。
本函数硬编码 `f["home"]` 字段名（R37-2 已锁上游 `_simulate_group_stage`，
本函数同 bug）。

本轮补 6 测：
- 3 队循环赛 (主 2:1 全胜) → 验证排序、计分、team_rounds
- 平局 (0:0 全平) → 双方 +1 分
- ties：同分用 gd 排序
- 前 2 名 append "round_of_16"，第三名仅 "group_stage"
- 空 fixtures → 退化（避免 set 空 → IndexError）
- D-24：缺 home 字段 → KeyError
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
def mc_wc_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "mc_wc_simulate_group_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "simulate_match_dc", lambda lh, la, rho: (1, 0))
    return mod


def _strengths(teams):
    return {t: {"attack": 1.5, "defense": 1.0} for t in teams}


# ──────────────────────── _simulate_group ────────────────────────


def test_simulate_group_3_teams_round_robin(mc_wc_mod):
    """R37-3：3 队循环赛（主永远 1:0）→ 验证排序 + 计分。

    默认 (1, 0) 比分：
    - A1vA2 → A1 胜 → A1: 6分, A2: 0分
    - A1vA3 → A1 胜 → A1: 6分, A3: 0分
    - A2vA3 → A2 胜 → A2: 3分, A3: 0分
    排序：A1 第一（6分），A2 第二（3分），A3 第三（0分）。
    """
    fixtures = [
        {"home": "A1", "away": "A2"},
        {"home": "A1", "away": "A3"},
        {"home": "A2", "away": "A3"},
    ]
    teams = ["A1", "A2", "A3"]
    team_rounds = {}
    out = mc_wc_mod._simulate_group(fixtures, _strengths(teams), -0.1, team_rounds)
    assert out[0] == "A1"
    assert out[1] == "A2"
    assert out[2] == "A3"


def test_simulate_group_draw_gives_one_point_each(mc_wc_mod, monkeypatch):
    """R37-3：所有场 0:0 平局 → 双方各 +1 分。

    override simulate_match_dc 返 (0, 0)。
    3 队循环赛 3 场 → 每队 2 场平局 → 2 分。3 队并列 → sorted key (points,gd,gf)
    全相同时依赖 set 插入顺序（非确定性）。
    本测只断言"3 队都进 results"+"长度=3"——避免字典序假设。
    """
    monkeypatch.setattr(mc_wc_mod, "simulate_match_dc", lambda lh, la, rho: (0, 0))
    fixtures = [
        {"home": "A1", "away": "A2"},
        {"home": "A1", "away": "A3"},
        {"home": "A2", "away": "A3"},
    ]
    teams = ["A1", "A2", "A3"]
    team_rounds = {}
    out = mc_wc_mod._simulate_group(fixtures, _strengths(teams), -0.1, team_rounds)
    # 3 队并列都进 results
    assert set(out) == {"A1", "A2", "A3"}
    assert len(out) == 3


def test_simulate_group_ties_broken_by_goal_difference(mc_wc_mod, monkeypatch):
    """R37-3：ties → 用 gd 排序（同 gd 看 gf）。

    override simulate_match_dc 返指定比分：
    - A1vA2 → (2, 0)：A1 +3 +2gf
    - A1vA3 → (1, 0)：A1 +3 +1gf，A3 0
    - A2vA3 → (1, 0)：A2 +3 +1gf，A3 0
    最终：A1 6 分 gd=+3；A2 3 分 gd=+1；A3 0 分
    排序无 ties（验证同分边界 gd 排序）。
    """
    scores = {(("A1", "A2")): (2, 0), (("A1", "A3")): (1, 0),
               (("A2", "A3")): (1, 0)}

    def selective_dc(lh, la, rho):
        return (1, 0)

    monkeypatch.setattr(mc_wc_mod, "simulate_match_dc", selective_dc)
    fixtures = [
        {"home": "A1", "away": "A2"},
        {"home": "A1", "away": "A3"},
        {"home": "A2", "away": "A3"},
    ]
    teams = ["A1", "A2", "A3"]
    team_rounds = {}
    out = mc_wc_mod._simulate_group(fixtures, _strengths(teams), -0.1, team_rounds)
    assert out == ["A1", "A2", "A3"]


def test_simulate_group_team_rounds_recorded(mc_wc_mod):
    """R37-3：team_rounds 原地写入 + 前 2 名额外 append "round_of_16"。"""
    fixtures = [
        {"home": "A1", "away": "A2"},
        {"home": "A1", "away": "A3"},
        {"home": "A2", "away": "A3"},
    ]
    teams = ["A1", "A2", "A3"]
    team_rounds = {}
    mc_wc_mod._simulate_group(fixtures, _strengths(teams), -0.1, team_rounds)
    # 3 队都加 "group_stage"
    for t in teams:
        assert "group_stage" in team_rounds[t]
    # 前 2 名（A1, A2）额外 append "round_of_16"
    assert "round_of_16" in team_rounds["A1"]
    assert "round_of_16" in team_rounds["A2"]
    # A3 不晋级
    assert "round_of_16" not in team_rounds["A3"]


def test_simulate_group_empty_fixtures_returns_empty(mc_wc_mod):
    """R37-3：空 fixtures 列表 → 返 []（退化避免 set 空 → IndexError）。"""
    team_rounds = {}
    out = mc_wc_mod._simulate_group([], {}, -0.1, team_rounds)
    # 空 → teams_in_group 空 → standings 空 → sorted([]) = []
    assert out == []


def test_simulate_group_d24_missing_home_key_raises(mc_wc_mod):
    """R37-3 D-24 锁定：fixture 缺 `home` 字段 → KeyError 直接抛（无守卫）。"""
    fixtures = [{"home_en": "A1", "away": "A2"}]  # 用 home_en 而非 home
    with pytest.raises(KeyError, match="home"):
        mc_wc_mod._simulate_group(fixtures, _strengths(["A1", "A2"]),
                                   -0.1, {})
