"""R48-4: scripts/core/model/monte_carlo_worldcup.py::simulate_world_cup 4 cases direct + D-86 lock.

simulate_world_cup (L177-L186, 10 行) World Cup Monte Carlo orchestrator:
1) 调 _simulate_group_stage (D-24 R37-2 已锁) -> (group_standings, team_rounds)
2) 调 _assemble_knockout (D-17 R36-3 已锁) -> remaining_teams
3) champion = remaining_teams[0] if remaining_teams else None
4) 返 {"champion", "team_rounds"}

D-17/D-24/D-69 + R45-4 D-65 已锁 _assemble_knockout / _simulate_group_stage
/ simulate_match_dc / _simulate_round 路由, 但 simulate_world_cup 真函数
自身核心契约 (orchestrator 装配顺序 + champion 取首 + 空 remaining 兜底 +
字段透传) 0 直测。

D-86 锁定 4 契约:
- D-86-1: 主流程 happy - group_stage + knockout 都返受控 -> 返 champion +
  team_rounds
- D-86-2: remaining_teams 空 (knockout 返 []) -> champion=None
- D-86-3: 字段透传 (champion + team_rounds 各自真跑结果原样透传)
- D-86-4: 装配顺序 - group_stage 先调, knockout 用其结果 (spy 调用顺序)

依赖 stub:
- _simulate_group_stage (monkeypatch 返受控 (group_standings, team_rounds))
- _assemble_knockout (monkeypatch 返受控 remaining_teams list)
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
def wc_mod():
    """Spec-load scripts/core/model/monte_carlo_worldcup.py。"""
    spec = importlib.util.spec_from_file_location(
        "monte_carlo_worldcup_simulate_world_cup_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_group_stage(mc_mod, monkeypatch, *, standings, team_rounds):
    """Stub _simulate_group_stage 返受控 (standings, team_rounds)。"""
    monkeypatch.setattr(mc_mod, "_simulate_group_stage",
                        lambda f, ts, rho: (standings, team_rounds))


def _stub_assemble_knockout(mc_mod, monkeypatch, *, remaining_teams):
    """Stub _assemble_knockout 返受控 remaining_teams。"""
    monkeypatch.setattr(mc_mod, "_assemble_knockout",
                        lambda gs, ts, rho, tr: remaining_teams)


# ──────────────────────── D-86 四契约 ────────────────────────


def test_simulate_world_cup_d86_1_happy_path_returns_champion_and_rounds(wc_mod, monkeypatch):
    """R48-4 D-86-1: 主流程 happy - group_stage + knockout 正常 -> 返 champion。

    remaining_teams=["Brazil"] -> champion="Brazil"。team_rounds 透传。
    """
    team_rounds = {"Brazil": ["QF", "SF", "Final"]}
    _stub_group_stage(wc_mod, monkeypatch,
        standings={"A": {"Brazil": {"pts": 9}}},
        team_rounds=team_rounds)
    _stub_assemble_knockout(wc_mod, monkeypatch, remaining_teams=["Brazil"])

    out = wc_mod.simulate_world_cup([], team_strengths={"Brazil": {}}, rho=0.2)
    assert out == {"champion": "Brazil", "team_rounds": team_rounds}


def test_simulate_world_cup_d86_2_empty_remaining_returns_champion_none(wc_mod, monkeypatch):
    """R48-4 D-86-2: remaining_teams 空 (knockout 返 []) -> champion=None。

    L181 `champion = remaining_teams[0] if remaining_teams else None`。
    边界: 列表空 -> None。
    """
    _stub_group_stage(wc_mod, monkeypatch,
        standings={}, team_rounds={})
    _stub_assemble_knockout(wc_mod, monkeypatch, remaining_teams=[])

    out = wc_mod.simulate_world_cup([], team_strengths={}, rho=0.2)
    assert out == {"champion": None, "team_rounds": {}}


def test_simulate_world_cup_d86_3_picks_first_remaining_team(wc_mod, monkeypatch):
    """R48-4 D-86-3: champion = remaining_teams[0] (列表首项, 假设 knockout 排序已保证冠军在前)。

    remaining_teams=["Argentina", "France"] (knockout 已排序) -> champion="Argentina"。
    team_rounds 各自透传。
    """
    team_rounds = {"Argentina": ["Final"], "France": ["Final"]}
    _stub_group_stage(wc_mod, monkeypatch,
        standings={"A": {"Argentina": {"pts": 9}}},
        team_rounds=team_rounds)
    _stub_assemble_knockout(wc_mod, monkeypatch,
                              remaining_teams=["Argentina", "France"])

    out = wc_mod.simulate_world_cup([], team_strengths={}, rho=0.2)
    assert out["champion"] == "Argentina"  # 取首项
    # team_rounds 完整透传
    assert out["team_rounds"] == team_rounds


def test_simulate_world_cup_d86_4_assembly_order_group_then_knockout(wc_mod, monkeypatch):
    """R48-4 D-86-4: 装配顺序 - group_stage 先, knockout 用其结果。

    spy 调用顺序: _simulate_group_stage 先, _assemble_knockout 后, 且
    knockout 收到 group_stage 返的 standings + team_rounds。
    """
    call_order = []
    received_args = {}

    def fake_group(fixtures, ts, rho):
        call_order.append("group_stage")
        standings = {"A": {"X": {"pts": 9}}}
        team_rounds = {"X": ["Group"]}
        received_args["group_standings"] = standings
        received_args["group_team_rounds"] = team_rounds
        return standings, team_rounds

    def fake_knockout(standings, ts, rho, team_rounds):
        call_order.append("assemble_knockout")
        received_args["knockout_standings"] = standings
        received_args["knockout_team_rounds"] = team_rounds
        return ["X"]

    monkeypatch.setattr(wc_mod, "_simulate_group_stage", fake_group)
    monkeypatch.setattr(wc_mod, "_assemble_knockout", fake_knockout)

    wc_mod.simulate_world_cup([], team_strengths={"X": {}}, rho=0.2)
    # 顺序: group 先
    assert call_order == ["group_stage", "assemble_knockout"]
    # knockout 收到 group 返的 standings + team_rounds
    assert received_args["knockout_standings"] is received_args["group_standings"]
    assert received_args["knockout_team_rounds"] is received_args["group_team_rounds"]