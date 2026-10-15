"""R28 #3: scripts/core/model/onside.py 4 未直测 helper 单测。

explore 实测：tests/test_onside.py 4 个公共函数（fifa_rank_to_score /
confederation_score / host_advantage_score / compute_onside_signals）已测；
未直测的是 4 个 helper：
- _elo_to_approx_rank（line 62-68，ELO 推算 FIFA rank，俱乐部无 FIFA 排名兜底）
- league_footprint_score（line 35-53，5 阈值 + 3 confederation 集）
- get_confederation（line 26-27，COUNTRY_CONFEDERATION.get 兜底）
- _onside_weighted_score（line 71-79，加权求和）

这些是 Onside 4+1 信号模型的内层计算，compute_onside_signals（已测）
调用它们，但未传 elo_ratings 时 ELO fallback 分支未触。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def onside_mod():
    """加载 model/onside.py 为唯一 module name（避免污染 sys.modules）。"""
    spec = importlib.util.spec_from_file_location(
        "onside_test", REPO_ROOT / "scripts" / "core" / "model" / "onside.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _elo_to_approx_rank ────────────────────────


def test_elo_to_approx_rank_default_1500_returns_200(onside_mod):
    """R28 #3：ELO=1500（默认）→ rank=200（公式 r=200-(1500-1500)*199/600=200）。"""
    assert onside_mod._elo_to_approx_rank(1500) == 200


def test_elo_to_approx_rank_1800_returns_100(onside_mod):
    """R28 #3：ELO=1800 → rank≈100（公式 r=200-300*0.332=100.4 → round→100）。"""
    result = onside_mod._elo_to_approx_rank(1800)
    assert 99 <= result <= 101  # 容 0.4 浮点


def test_elo_to_approx_rank_2100_returns_clamped_1(onside_mod):
    """R28 #3：ELO=2100 → rank=1（公式 r=200-600*0.332=1.0 → round→1）。"""
    assert onside_mod._elo_to_approx_rank(2100) == 1


def test_elo_to_approx_rank_below_default_high_rank(onside_mod):
    """R28 #3：ELO=1000（低于默认）→ rank≈366（高 rank = 弱队）。"""
    result = onside_mod._elo_to_approx_rank(1000)
    assert result >= 300  # high rank


def test_elo_to_approx_rank_extreme_low_clamped_to_1(onside_mod):
    """R28 #3：ELO=10000 极端高 → clamp max(1, ...) → 1（保护）。"""
    assert onside_mod._elo_to_approx_rank(10000) == 1


# ──────────────────────── league_footprint_score ────────────────────────


def test_league_footprint_top_10_returns_1(onside_mod):
    """R28 #3：fifa_rank ≤ 10 → 1.0（顶级强队）。"""
    assert onside_mod.league_footprint_score("Brazil", fifa_rank=5) == 1.0


def test_league_footprint_rank_31_to_60_returns_06(onside_mod):
    """R28 #3：31-60 → 0.6。"""
    assert onside_mod.league_footprint_score("Brazil", fifa_rank=50) == 0.6


def test_league_footprint_no_rank_conmebol_returns_07(onside_mod):
    """R28 #3：fifa_rank=None + Brazil (CONMEBOL) → 0.7。"""
    assert onside_mod.league_footprint_score("Brazil", fifa_rank=None) == 0.7


def test_league_footprint_no_rank_uefa_returns_07(onside_mod):
    """R28 #3：fifa_rank=None + England (UEFA) → 0.7。"""
    assert onside_mod.league_footprint_score("England", fifa_rank=None) == 0.7


def test_league_footprint_no_rank_afc_returns_05(onside_mod):
    """R28 #3：fifa_rank=None + China/Japan (AFC) → 0.5。"""
    assert onside_mod.league_footprint_score("China", fifa_rank=None) == 0.5


def test_league_footprint_no_rank_unknown_country_returns_uefa_default_07(onside_mod):
    """R28 #3：fifa_rank=None + 未知国家 → get_confederation 兜底 "UEFA" → 0.7。

    注：``get_confederation`` 对未配置 COUNTRY_CONFEDERATION 的国家一律返回
    "UEFA" 兜底；故 ``league_footprint_score`` 的 else 分支（0.3）在当前配置
    下实际不可达。本测试记录这一行为锁定，防止静默 drift。
    """
    assert onside_mod.league_footprint_score("Atlantis", fifa_rank=None) == 0.7


def test_league_footprint_high_rank_returns_02(onside_mod):
    """R28 #3：fifa_rank > 100 → 0.2（弱队）。"""
    assert onside_mod.league_footprint_score("Brazil", fifa_rank=200) == 0.2


# ──────────────────────── get_confederation ────────────────────────


def test_get_confederation_known_uefa(onside_mod):
    """R28 #3：England → 'UEFA'。"""
    assert onside_mod.get_confederation("England") == "UEFA"


def test_get_confederation_known_conmebol(onside_mod):
    """R28 #3：Brazil → 'CONMEBOL'。"""
    assert onside_mod.get_confederation("Brazil") == "CONMEBOL"


def test_get_confederation_unknown_falls_back_to_uefa(onside_mod):
    """R28 #3：未知国家 → 'UEFA'（兜底，不抛穿）。"""
    assert onside_mod.get_confederation("Atlantis") == "UEFA"


# ──────────────────────── _onside_weighted_score ────────────────────────


def test_onside_weighted_score_unit_inputs_sum_onside_weights(onside_mod):
    """R28 #3：(1,1,1,1) → sum(ONSIDE_WEIGHTS.values()) = 0.25+0.20+0.15+0.20=0.80。"""
    out = onside_mod._onside_weighted_score(1.0, 1.0, 1.0, 1.0)
    assert abs(out - 0.80) < 0.001


def test_onside_weighted_score_zero_inputs(onside_mod):
    """R28 #3：全 0 → 0.0。"""
    assert onside_mod._onside_weighted_score(0, 0, 0, 0) == 0.0


def test_onside_weighted_score_only_fifa(onside_mod):
    """R28 #3：(0.5, 0, 0, 0) → 0.5 × w['fifa_ranking']=0.5×0.25=0.125。"""
    out = onside_mod._onside_weighted_score(0.5, 0, 0, 0)
    assert abs(out - 0.125) < 0.001
