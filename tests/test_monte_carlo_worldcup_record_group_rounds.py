"""R37-4: scripts/core/model/monte_carlo_worldcup.py::_record_group_rounds 4 测直测（Top 4）。

现状：小组赛 + 16 强轮次记录函数 0 直测；轮次标签（"group_stage" /
"round_of_16"）直接进 `round_reach_probs` API 字段，下游 dashboard
渲染据此判定。

本轮补 4 测：
- 4 队组：4 个都 append "group_stage"
- 前 2 名额外 append "round_of_16"
- team_rounds 已有同名标签 → 追加（不覆盖）
- 不足 2 队组（teams_in_group < 2）→ advance 空（不 append "round_of_16"）
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
def mc_wc_mod():
    spec = importlib.util.spec_from_file_location(
        "mc_wc_record_group_rounds_test",
        REPO_ROOT / "scripts" / "core" / "model" / "monte_carlo_worldcup.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _record_group_rounds ────────────────────────


def test_record_group_rounds_4_teams_all_append_group_stage(mc_wc_mod):
    """R37-4：4 队组 → 4 个都 append "group_stage"。"""
    team_rounds = {}
    mc_wc_mod._record_group_rounds(
        team_rounds,
        teams_in_group={"a1", "a2", "a3", "a4"},
        sorted_teams=["a1", "a2", "a3", "a4"])
    for t in ["a1", "a2", "a3", "a4"]:
        assert "group_stage" in team_rounds[t]


def test_record_group_rounds_top_2_get_round_of_16(mc_wc_mod):
    """R37-4：前 2 名（sorted_teams[:2]）额外 append "round_of_16"。"""
    team_rounds = {}
    mc_wc_mod._record_group_rounds(
        team_rounds,
        teams_in_group={"a1", "a2", "a3", "a4"},
        sorted_teams=["a1", "a2", "a3", "a4"])
    # 前 2 名
    assert "round_of_16" in team_rounds["a1"]
    assert "round_of_16" in team_rounds["a2"]
    # 后 2 名无 round_of_16
    assert "round_of_16" not in team_rounds["a3"]
    assert "round_of_16" not in team_rounds["a4"]


def test_record_group_rounds_existing_tags_appended_not_overwritten(mc_wc_mod):
    """R37-4：team_rounds 已有同名标签 → 追加（不覆盖）。

    a1 已有 ["existing_tag"] → append "group_stage" 后变 ["existing_tag", "group_stage"]。
    """
    team_rounds = {"a1": ["existing_tag"]}
    mc_wc_mod._record_group_rounds(
        team_rounds,
        teams_in_group={"a1", "a2"},
        sorted_teams=["a1", "a2"])
    # 已有标签保留 + 新增
    assert team_rounds["a1"][0] == "existing_tag"
    assert "group_stage" in team_rounds["a1"]
    assert "round_of_16" in team_rounds["a1"]
    # a2 全新
    assert "group_stage" in team_rounds["a2"]
    assert "round_of_16" in team_rounds["a2"]


def test_record_group_rounds_less_than_2_teams_no_round_of_16(mc_wc_mod):
    """R37-4：不足 2 队组（teams_in_group < 2）→ 实际行为锁定。

    sorted_teams[:2] 取前 2 名（1 队组仍晋级 —— D 风格契约钉住）：
    - 1 队组：仍 append "round_of_16"（罕见但存在）
    - 0 队组（空 set/空 list）：team_rounds 保持空，不写入任何 key
    """
    # 1 队组：仍晋级（不期望——但锁定现状）
    team_rounds_1 = {}
    mc_wc_mod._record_group_rounds(
        team_rounds_1,
        teams_in_group={"only_one"},
        sorted_teams=["only_one"])
    assert "group_stage" in team_rounds_1["only_one"]
    assert "round_of_16" in team_rounds_1["only_one"]  # 1 队也"晋级"
    # 0 队组：team_rounds 保持空
    team_rounds_0 = {}
    mc_wc_mod._record_group_rounds(
        team_rounds_0,
        teams_in_group=set(),
        sorted_teams=[])
    assert team_rounds_0 == {}
