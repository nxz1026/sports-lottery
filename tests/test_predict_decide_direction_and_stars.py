"""R32 #3: scripts/core/predictor.py::_decide_direction_and_stars 5 测直测。

现状：方向判断 + 置信度钳位 + 无盘口降级 + 星级映射 5 个分支全 0 直测；
每场推荐都过这里。

THRESHOLDS（scripts/core/constants.py:85-94）：
- direction_min_prob=0.45, direction_odds_ratio=1.3, draw_threshold=0.40
- near_mode_base=0.33
- star_5=0.90, star_4=0.72, star_3=0.55, star_2=0.35

本轮补 5 测：
- home_prob 大胜（>0.45 且 >away*1.3）→ "X 胜" + confidence=(0.6-0.25)*2=0.7 → 3-star
- draw_prob > 0.40 → "平局" + 2-star
- 三方都不显著 → near_mode 走最大者
- odds_data_available=False → confidence-0.25 + note="无盘口数据，仅基本面参考"
- confidence 钳到 1.0 → 5-star 边界
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
        "decide_dir_stars_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _decide_direction_and_stars ────────────────────────


def test_decide_home_win_picks_home_and_3star(predictor_mod):
    """R32 #3：home_prob=0.6 > 0.45 且 > 0.2*1.3=0.26 → "Arsenal 胜"。

    confidence_raw = (0.6 - 0.25) * 2 = 0.7 → 3-star（>=0.55 但 <0.72）。
    odds_data_available=True → confidence_note=None。
    """
    out = predictor_mod._decide_direction_and_stars(
        home_prob=0.6, draw_prob=0.2, away_prob=0.2,
        match={"home": "Arsenal", "away": "Chelsea", "odds_data_available": True},
    )
    direction, confidence, note, stars = out
    assert direction == "Arsenal 胜"
    assert abs(confidence - 0.7) < 1e-9
    assert note is None
    assert stars == "3-star"


def test_decide_draw_direction_picks_draw(predictor_mod):
    """R32 #3：draw_prob=0.45 > 0.40 → "平局"；confidence=(0.45-0.25)*2=0.4 → 2-star。"""
    out = predictor_mod._decide_direction_and_stars(
        home_prob=0.275, draw_prob=0.45, away_prob=0.275,
        match={"home": "Arsenal", "away": "Chelsea", "odds_data_available": True},
    )
    direction, confidence, note, stars = out
    assert direction == "平局"
    assert abs(confidence - 0.4) < 1e-9
    assert note is None
    assert stars == "2-star"


def test_decide_near_mode_picks_max_of_three(predictor_mod):
    """R32 #3：home=away=draw=0.33（均不显著）→ near_mode 走 home（home≥away 且 home≥draw）。

    confidence_raw = (0.33 - 0.33) * 3 = 0.0 → 1-star。
    """
    out = predictor_mod._decide_direction_and_stars(
        home_prob=0.34, draw_prob=0.33, away_prob=0.33,
        match={"home": "Arsenal", "away": "Chelsea", "odds_data_available": True},
    )
    direction, confidence, note, stars = out
    assert direction == "Arsenal 胜 (接近)"
    assert abs(confidence - 0.03) < 1e-9  # (0.34-0.33)*3
    assert note is None
    assert stars == "1-star"


def test_decide_no_odds_data_subtracts_confidence_with_note(predictor_mod):
    """R32 #3：odds_data_available=False → confidence_raw -= 0.25（最小 0）+ note。

    与 case1 同样 (0.55, 0.25, 0.20) 但 odds_data_available=False → 0.6-0.25=0.35 → 2-star。
    """
    out = predictor_mod._decide_direction_and_stars(
        home_prob=0.55, draw_prob=0.25, away_prob=0.20,
        match={"home": "Arsenal", "away": "Chelsea", "odds_data_available": False},
    )
    direction, confidence, note, stars = out
    assert direction == "Arsenal 胜"
    assert abs(confidence - 0.35) < 1e-9  # 0.6 - 0.25
    assert note == "无盘口数据，仅基本面参考"
    assert stars == "2-star"


def test_decide_clamp_confidence_to_1_for_5star(predictor_mod):
    """R32 #3：home_prob=0.85 → (0.85-0.25)*2=1.2 钳到 1.0 → 5-star。

    置信度上限钳位（max(confidence, 0.0) / min(confidence, 1.0)）防 silent 上溢。
    """
    out = predictor_mod._decide_direction_and_stars(
        home_prob=0.85, draw_prob=0.075, away_prob=0.075,
        match={"home": "Arsenal", "away": "Chelsea", "odds_data_available": True},
    )
    direction, confidence, note, stars = out
    assert direction == "Arsenal 胜"
    assert abs(confidence - 1.0) < 1e-9
    assert note is None
    assert stars == "5-star"