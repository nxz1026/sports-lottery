"""R32 #2: scripts/core/predictor.py::_reconcile_score_with_direction 4 测直测。

现状：P1-4 修复"方向与最可能比分矛盾"逻辑整链 0 直测；
_reconcile_score_with_direction 仅被 calculate_prediction 间接调用。

本轮补 4 测，覆盖：
- "平局" 方向 → 不 reconcile，原样返回 predicted_score
- 主胜方向 + predicted_score 平局（1-1）→ 网格里选 h>a 最高概率比分
- 主胜方向 + predicted_score 已主胜 → 不变
- 客胜方向 + predicted_score 平局（1-1）→ 网格里选 a>h 最高概率比分
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
def predictor_mod():
    spec = importlib.util.spec_from_file_location(
        "reconcile_score_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# 网格示例：(home_goals, away_goals, probability)
SCORES_2_1_HOME = [
    (2, 1, 0.18), (1, 0, 0.15), (1, 1, 0.12),
    (0, 1, 0.10), (3, 1, 0.08), (2, 0, 0.07),
]
# 网格示例：客胜候选最高概率 (0, 2, 0.20) 比 (1, 2, 0.18) 优先
SCORES_0_2_AWAY = [
    (0, 2, 0.20), (1, 2, 0.18), (2, 2, 0.12),
    (1, 1, 0.10), (2, 1, 0.08), (0, 1, 0.07),
]


def test_reconcile_draw_direction_skips_reconcile(predictor_mod):
    """R32 #2："平局"方向 → 不 reconcile，原样返回 predicted_score。

    P1-4 设计：draw 方向不强制比分差异（平局比分 = 主客胜矛盾无意义）。
    """
    out = predictor_mod._reconcile_score_with_direction(
        predicted_score="1-1",
        direction="平局",
        all_scores=SCORES_2_1_HOME,
        match={"home": "Arsenal", "away": "Chelsea"},
    )
    assert out == "1-1"


def test_reconcile_home_win_picks_top_home_score_when_predicted_draw(predictor_mod):
    """R32 #2：主胜方向 + predicted_score 平局 → 从网格选 h>a 最高概率比分。

    SCORES_2_1_HOME 中 h>a 候选：(2,1,0.18)/(1,0,0.15)/(3,1,0.08)/(2,0,0.07)，
    最高概率 (2,1,0.18) → 期望返 "2-1"。
    """
    out = predictor_mod._reconcile_score_with_direction(
        predicted_score="1-1",
        direction="Arsenal 胜",
        all_scores=SCORES_2_1_HOME,
        match={"home": "Arsenal", "away": "Chelsea"},
    )
    assert out == "2-1"


def test_reconcile_home_win_keeps_predicted_when_already_home_win(predictor_mod):
    """R32 #2：主胜方向 + predicted_score 已主胜（如 "2-0"）→ 不变。

    P1-4 设计：predicted_score 与 direction 一致时无需 reconcile。
    """
    out = predictor_mod._reconcile_score_with_direction(
        predicted_score="2-0",
        direction="Arsenal 胜",
        all_scores=SCORES_2_1_HOME,
        match={"home": "Arsenal", "away": "Chelsea"},
    )
    assert out == "2-0"


def test_reconcile_away_win_picks_top_away_score_when_predicted_draw(predictor_mod):
    """R32 #2：客胜方向 + predicted_score 平局（1-1）→ 从网格选 a>h 最高概率比分。

    SCORES_0_2_AWAY 中 a>h 候选：(0,2,0.20)/(1,2,0.18)，
    最高概率 (0,2,0.20) → 期望返 "0-2"。
    """
    out = predictor_mod._reconcile_score_with_direction(
        predicted_score="1-1",
        direction="Chelsea 胜",
        all_scores=SCORES_0_2_AWAY,
        match={"home": "Arsenal", "away": "Chelsea"},
    )
    assert out == "0-2"