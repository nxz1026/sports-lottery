"""R37-2: scripts/core/model/monte_carlo_worldcup.py::_simulate_group_stage 5 测直测（Top 2）。

现状：World Cup 模拟的小组 → 16 强分流枢纽 0 直测；R36-3/4 已锁
`_assemble_knockout`（下游），本函数是其上游。分组分流一旦错位
整条锦标赛链失效。

3 大分支：
- fixtures[].stage == "group" → 进 groups（默认）
- fixtures[].stage != "group" → 进 knockout（不进 standings）
- fixtures[] 缺 group 字段 → 默认 "A"

D-24 bug-shaped 锁定：fixtures 缺 `home`/`away` → KeyError 未捕获，
直接抛错。`_simulate_group` 内部 `f["home"]` 硬编码字段名。
当前实现零兜底；分组分流一旦上游 fixture 数据缺字段会终止整轮模拟。

本轮补 5 测：
- 全 group fixtures → 多组进入 standings
- 全 knockout fixtures → groups={} 空
- 混合 group + knockout → 仅 group 进入 standings
- 缺 group 字段 → 默认 "A"
- D-24：缺 home 字段 → KeyError 直接抛（无守卫）
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
        "mc_wc_simulate_group_stage_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 固定模拟比分避免随机性
    monkeypatch.setattr(mod, "simulate_match_dc", lambda lh, la, rho: (1, 0))
    return mod


def _strengths(teams):
    return {t: {"attack": 1.5, "defense": 1.0} for t in teams}


# ──────────────────────── _simulate_group_stage ────────────────────────


def test_simulate_group_stage_groups_fixtures(mc_wc_mod):
    """R37-2：全 group fixtures → 多组进入 standings + team_rounds。"""
    fixtures = [
        {"group": "A", "home": "a1", "away": "a2"},
        {"group": "B", "home": "b1", "away": "b2"},
    ]
    teams = ["a1", "a2", "b1", "b2"]
    out = mc_wc_mod._simulate_group_stage(fixtures, _strengths(teams), -0.1)
    # 2 组各 2 队
    assert set(out[0].keys()) == {"A", "B"}
    assert len(out[0]["A"]) == 2
    assert len(out[0]["B"]) == 2
    # team_rounds 含 group_stage 标签
    for t in teams:
        assert "group_stage" in out[1][t]


def test_simulate_group_stage_all_knockout_returns_empty(mc_wc_mod):
    """R37-2：全 knockout fixtures → groups={} 空，team_rounds 空。"""
    fixtures = [{"stage": "knockout", "home": "a1", "away": "b1"}]
    teams = ["a1", "b1"]
    out = mc_wc_mod._simulate_group_stage(fixtures, _strengths(teams), -0.1)
    # 全 knockout → group_standings 空
    assert out[0] == {}


def test_simulate_group_stage_mixed_group_and_knockout(mc_wc_mod):
    """R37-2：混合 group + knockout → 仅 group 进 standings，knockout 被剔除。"""
    fixtures = [
        {"group": "A", "home": "a1", "away": "a2"},  # group
        {"stage": "knockout", "home": "b1", "away": "b2"},  # knockout
    ]
    teams = ["a1", "a2", "b1", "b2"]
    out = mc_wc_mod._simulate_group_stage(fixtures, _strengths(teams), -0.1)
    # 仅 group 进
    assert "A" in out[0]
    assert "a1" in out[0]["A"]
    # b1/b2 不进 group_standings
    assert "b1" not in out[0].get("A", [])


def test_simulate_group_stage_missing_group_field_defaults_to_A(mc_wc_mod):
    """R37-2：fixture 缺 `group` 字段 → 默认 "A"（隐式契约锁定）。

    `group_name = f.get("group", "A")`。
    """
    fixtures = [{"home": "a1", "away": "a2"}]  # 缺 group
    teams = ["a1", "a2"]
    out = mc_wc_mod._simulate_group_stage(fixtures, _strengths(teams), -0.1)
    # 进 A 组
    assert "A" in out[0]
    assert "a1" in out[0]["A"]


def test_simulate_group_stage_d24_missing_home_key_raises(mc_wc_mod):
    """R37-2 D-24 锁定：fixture 缺 `home` 字段 → KeyError 直接抛（无守卫）。

    `_simulate_group` 内部 `f["home"]` 硬编码字段名。当前实现零兜底；
    分组分流一旦上游 fixture 数据缺字段会终止整轮模拟。
    锁定无守卫现状防未来加 try/except 时未覆盖旧调用点。
    """
    fixtures = [{"group": "A", "home_en": "a1", "away": "a2"}]  # 用 home_en 而非 home
    teams = ["a1", "a2"]
    with pytest.raises(KeyError, match="home"):
        mc_wc_mod._simulate_group_stage(fixtures, _strengths(teams), -0.1)
