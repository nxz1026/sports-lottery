"""R49-1: scripts/core/model/monte_carlo_league.py::simulate_league 5 cases direct + D-87 lock.

simulate_league (L115-L188, 74 行) 模拟联赛剩余赛程, 返回冠军。
1) standings 从 initial_standings 浅拷贝 (L127 `dict(v)`)
2) 遍历 fixtures: 每场 simulate_match_dc 采 (hg, ag)
3) λ 推算: attack * defence 双因子, 缺一退默认 (1.5/1.2)
4) 累加 points (胜 3/平 1/负 0) + gf/ga/gd
5) 排序 by (points, gd, gf) 倒序
6) team_rounds 恒为 {} (LOCK: 联赛无轮次晋级概念)
7) 返 {"champion", "team_rounds": {}, "final_standings": list[dict]}

D-17/D-24/D-69 + R45-4 D-65/R48-4 D-86 已锁 _assemble_knockout /
_simulate_group_stage / simulate_match_dc / _simulate_round /
simulate_world_cup, 但 simulate_league 真函数自身核心契约 (积分累加 +
排序 + 浅拷贝 + λ 推导 + team_rounds 恒空) 0 直测 (test_p0_window_mc_stablekey
仅 3 间接断言)。

D-87 锁定 5 契约:
- D-87-1: 主流程 happy - 3 teams + 2 fixtures -> 累加 + 排序 + 完整 final_standings
- D-87-2: initial_standings 浅拷贝 (L127 `dict(v)`) - 不 mutate 原 dict
- D-87-3: 排序 tie-break - points 同 -> gd -> gf (L169 倒序)
- D-87-4: team_rounds 恒为 {} (LOCK 联赛无轮次晋级概念, L178 注释)
- D-87-5: 缺 attack/defence 退默认 λ (1.5/1.2) (L139-L149)

依赖 stub:
- simulate_match_dc (monkeypatch 返受控 (hg, ag))
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
def lg_mod():
    """Spec-load scripts/core/model/monte_carlo_league.py。"""
    spec = importlib.util.spec_from_file_location(
        "monte_carlo_league_simulate_league_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_league.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_match_dc(lg_mod, monkeypatch, results: list):
    """Stub simulate_match_dc 顺序返受控 (hg, ag) 元组。"""
    counter = {"i": 0}

    def fake_match(lh, la, rho=0.2):
        idx = counter["i"]
        counter["i"] += 1
        if idx < len(results):
            return results[idx]
        return 0, 0

    monkeypatch.setattr(lg_mod, "simulate_match_dc", fake_match)


# ──────────────────────── D-87 五契约 ────────────────────────


def test_simulate_league_d87_1_happy_aggregate_and_sort(lg_mod, monkeypatch):
    """R49-1 D-87-1: 主流程 happy - 3 teams + 3 fixtures, 累加 + 排序 + final_standings。

    3 fixtures: A vs B (A 胜 2-0) / B vs C (B 胜 1-0) / A vs C (A 胜 3-0)。
    A: 6 分, gf=5, ga=0, gd=5
    B: 3 分, gf=1, ga=2, gd=-1
    C: 0 分, gf=0, ga=4, gd=-4
    champion=A, 排序 A > B > C。
    """
    _stub_match_dc(lg_mod, monkeypatch, [(2, 0), (1, 0), (3, 0)])
    out = lg_mod.simulate_league(
        fixtures=[
            {"home": "A", "away": "B"},
            {"home": "B", "away": "C"},
            {"home": "A", "away": "C"},
        ],
        team_strengths={
            "A": {"lambda_home": 1.5, "lambda_away": 1.2},
            "B": {"lambda_home": 1.5, "lambda_away": 1.2},
            "C": {"lambda_home": 1.5, "lambda_away": 1.2},
        },
        rho=0.2,
    )
    assert out["champion"] == "A"
    # final_standings 完整 + 排序
    standings = out["final_standings"]
    assert len(standings) == 3
    assert [s["team"] for s in standings] == ["A", "B", "C"]
    assert standings[0]["points"] == 6
    assert standings[0]["gd"] == 5
    assert standings[0]["gf"] == 5
    assert standings[1]["points"] == 3
    assert standings[2]["points"] == 0


def test_simulate_league_d87_2_initial_standings_shallow_copy(lg_mod, monkeypatch):
    """R49-1 D-87-2: initial_standings 浅拷贝 (L127 `dict(v)`) - 不 mutate 原 dict。

    initial_standings = {"A": {"points": 3, ...}} + 1 fixture (A 胜) -> A 累加 3 -> 6
    原 dict 仍为 3 (不 mutate)。
    """
    _stub_match_dc(lg_mod, monkeypatch, [(2, 0)])
    original = {"A": {"points": 3, "gf": 1, "ga": 0, "gd": 1}}
    # 浅拷贝 (避免 test 间污染)
    snapshot = {k: dict(v) for k, v in original.items()}

    out = lg_mod.simulate_league(
        fixtures=[{"home": "A", "away": "B"}],
        team_strengths={"A": {"lambda_home": 1.5, "lambda_away": 1.2},
                          "B": {"lambda_home": 1.5, "lambda_away": 1.2}},
        rho=0.2,
        initial_standings=original,
    )
    # A 累加胜 3 分: 3 + 3 = 6
    assert out["final_standings"][0]["points"] == 6
    # 原 dict 不被 mutate
    assert original == snapshot  # A 仍 points=3
    # 同时新 B 在 standings 中 (B 没预填, default 0; B 输 0 分; 仍 0 分)
    b_row = next(s for s in out["final_standings"] if s["team"] == "B")
    assert b_row["points"] == 0


def test_simulate_league_d87_3_sort_tiebreak_points_gd_gf(lg_mod, monkeypatch):
    """R49-1 D-87-3: 排序 tie-break - points 同 -> gd -> gf (L169 倒序)。

    2 teams 都 3 分 (1 胜 0 负 1 平): A: gf=1, ga=0, gd=1; B: gf=2, ga=1, gd=1。
    tie-break: gd 相同 (1=1) -> gf 高 (B 2 > A 1) -> B 排前。
    """
    # 模拟 2 场: A vs X (A 1-0) + B vs Y (B 2-1) + 平局 (不, 我们要 2 场即可)
    # A: 1 胜 3 分; B: 1 胜 3 分; gd 1=1, gf 1<2
    _stub_match_dc(lg_mod, monkeypatch, [(1, 0), (2, 1)])
    out = lg_mod.simulate_league(
        fixtures=[
            {"home": "A", "away": "X"},  # A 1-0
            {"home": "B", "away": "Y"},  # B 2-1
        ],
        team_strengths={
            "A": {"lambda_home": 1.5, "lambda_away": 1.2},
            "B": {"lambda_home": 1.5, "lambda_away": 1.2},
            "X": {"lambda_home": 1.5, "lambda_away": 1.2},
            "Y": {"lambda_home": 1.5, "lambda_away": 1.2},
        },
        rho=0.2,
    )
    # A=3pts gf=1 gd=1; B=3pts gf=2 gd=1; tie-break gf: B 排前
    # X/Y 0 分排最后 (0pts, gf=0/1, gd=-1)
    teams = [s["team"] for s in out["final_standings"]]
    assert teams[:2] == ["B", "A"]  # B 排前 (gf tie-break)
    # X/Y 排后 (0 分)
    assert set(teams[2:]) == {"X", "Y"}


def test_simulate_league_d87_4_team_rounds_always_empty(lg_mod, monkeypatch):
    """R49-1 D-87-4: team_rounds 恒为 {} (LOCK 联赛无轮次晋级概念)。

    L178 注释明示: 联赛是单循环积分制, 没有"晋级轮次"概念。
    即便 fixtures 多场, team_rounds 仍 {} (不是按场数算的"season")。
    """
    _stub_match_dc(lg_mod, monkeypatch, [(1, 0), (2, 1), (3, 0)])
    out = lg_mod.simulate_league(
        fixtures=[
            {"home": "A", "away": "B"},
            {"home": "B", "away": "C"},
            {"home": "A", "away": "C"},
        ],
        team_strengths={
            "A": {"lambda_home": 1.5, "lambda_away": 1.2},
            "B": {"lambda_home": 1.5, "lambda_away": 1.2},
            "C": {"lambda_home": 1.5, "lambda_away": 1.2},
        },
        rho=0.2,
    )
    # team_rounds 恒 {}
    assert out["team_rounds"] == {}
    # 不会因 fixtures 多而添加 "season" 等
    assert "season" not in out["team_rounds"]


def test_simulate_league_d87_5_missing_attack_defence_falls_back(lg_mod, monkeypatch):
    """R49-1 D-87-5: 缺 attack/defence 退默认 λ (1.5/1.2) (L139-L149)。

    team_strengths = {"A": {}} 无 attack/defence -> λh = default 1.5 (L145)
    `lambda_home` 也缺 -> 走 L145 `sh.get("lambda_home", 1.5) = 1.5`。
    验证 simulate_match_dc 收到 lh=1.5 la=1.2。
    """
    received = {"lh": None, "la": None}

    def fake_match(lh, la, rho=0.2):
        received["lh"] = lh
        received["la"] = la
        return 1, 0

    monkeypatch.setattr(lg_mod, "simulate_match_dc", fake_match)
    lg_mod.simulate_league(
        fixtures=[{"home": "A", "away": "B"}],
        team_strengths={"A": {}, "B": {}},  # 缺 attack/defence/lambda
        rho=0.2,
    )
    # 走 default: lh = 1.5, la = 1.2
    assert received["lh"] == 1.5
    assert received["la"] == 1.2