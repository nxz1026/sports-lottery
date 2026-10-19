"""R32 #4: scripts/core/predictor.py::_elo_expectations 3 测直测。

现状：ELO 期望胜率信号 0 直测；仅被 calculate_prediction 间接调用。

ELO 公式（scripts/core/elo.py:33-45）：
    expected_score(elo_a, elo_b, home_adv) = 1 / (1 + 10^((elo_b - effective_a) / 400))
    effective_a = elo_a + (100 if home_adv else 0)
DEFAULT_ELO=1500, HOME_ADVANTAGE_ELO=100, SCALE_FACTOR=400

本轮补 3 测：
- elo_ratings=None → (None, None, 0.18)
- elo_ratings={...} 含 home/away → 双边 expected（home_adv=True → 主场加成 +100）
- 队名不在 ratings → DEFAULT_ELO 兜底
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
        "elo_expectations_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _elo_expectations ────────────────────────


def test_elo_expectations_none_ratings_returns_none_with_default_weight(predictor_mod):
    """R32 #4：elo_ratings=None → (None, None, 0.18)（ELO_WEIGHT 默认）。"""
    h, a, w = predictor_mod._elo_expectations(
        elo_ratings=None, home_en="Arsenal", away_en="Chelsea")
    assert h is None
    assert a is None
    assert w == 0.18  # THRESHOLDS["elo_weight"]


def test_elo_expectations_with_ratings_returns_home_adv_expects(predictor_mod):
    """R32 #4：elo_ratings 含 home/away → 双边 expected_score，home_adv=True（+100）。

    home_elo=1600, away_elo=1500, home_adv=True：
    effective_a = 1600 + 100 = 1700
    home_expected = 1 / (1 + 10^((1500-1700)/400)) = 1 / (1 + 10^-0.5) ≈ 0.7597
    away_expected = 1 - 0.7597 ≈ 0.2403
    """
    h, a, w = predictor_mod._elo_expectations(
        elo_ratings={"Arsenal": 1600.0, "Chelsea": 1500.0},
        home_en="Arsenal", away_en="Chelsea")
    assert abs(h - 0.7597469) < 1e-4
    assert abs(a - 0.2402531) < 1e-4
    assert abs(h + a - 1.0) < 1e-9  # 互斥必守恒
    assert w == 0.18


def test_elo_expectations_missing_team_falls_back_to_default_elo(predictor_mod):
    """R32 #4：队名不在 ratings → DEFAULT_ELO (1500) 兜底。

    ratings 只含 Arsenal=1600，Chelsea 缺 → DEFAULT_ELO=1500；
    home_adv=True → effective_a = 1600 + 100 = 1700
    home_expected = 1 / (1 + 10^((1500-1700)/400)) ≈ 0.7597（与 case2 同）。
    """
    h, a, w = predictor_mod._elo_expectations(
        elo_ratings={"Arsenal": 1600.0},  # Chelsea 缺 → DEFAULT_ELO
        home_en="Arsenal", away_en="Chelsea")
    # Chelsea → 1500 fallback；effective_a=1700；away=1500
    assert abs(h - 0.7597469) < 1e-4
    assert abs(a - 0.2402531) < 1e-4
    assert abs(h + a - 1.0) < 1e-9
    assert w == 0.18
