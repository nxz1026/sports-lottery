"""R35-5: scripts/core/elo.py::_elo_apply_result 5 测直测。

现状：单场结果 ELO 更新函数 0 直测；predictor 主链调用密集。

5 分支：
- 主胜 → score_h=1.0 + adj_k=20（goal_diff=1）
- 客胜 → score_h=0.0 + adj_k=20
- 平局 → score_h=0.5 + adj_k=20
- 大比分 (diff=4) → adj_k = 20*(1.75+0.125) = 37.5
- ratings 就地写回 + 缺队 DEFAULT_ELO (1500) 兜底

goal_difference_adjustment（elo.py:76-91）：
- diff <= 0 → base_k
- diff == 1 → base_k * 1.0
- diff == 2 → base_k * 1.5
- diff > 2 → base_k * (1.75 + 0.125*(diff-3))

本轮补 5 测：
- 主胜 2-1 + ratings 就地写回
- 客胜 0-1 + score_h=0.0
- 平局 1-1 + score_h=0.5 + adj_k=20
- 大比分 4-0 (diff=4) → adj_k=37.5
- 缺队 → DEFAULT_ELO (1500) 兜底
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
def elo_mod():
    spec = importlib.util.spec_from_file_location(
        "elo_apply_result_test", REPO_ROOT / "scripts" / "core" / "elo.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _elo_apply_result ────────────────────────


def test_elo_apply_result_home_win_writes_ratings_in_place(elo_mod):
    """R35-5：主胜 2-1（diff=1）→ score_h=1.0 + adj_k=20 + ratings 就地写回。"""
    ratings = {"Arsenal": 1600.0, "Chelsea": 1500.0}
    elo_h, elo_a, new_h, new_a, adj_k = elo_mod._elo_apply_result(
        "Arsenal", "Chelsea", 2, 1, ratings, k=20.0)
    # 5-tuple 返回
    assert elo_h == 1600.0 and elo_a == 1500.0
    # 主胜方分数上升（home_adv=True 让 elo_h effective = 1700）→ score_h=1.0 实际表现 >预期，ELO 加分
    assert new_h > elo_h
    assert new_a < elo_a
    # adj_k = 20（diff=1）
    assert adj_k == 20.0
    # ratings 就地写回
    assert ratings["Arsenal"] == new_h
    assert ratings["Chelsea"] == new_a


def test_elo_apply_result_away_win_score_h_zero(elo_mod):
    """R35-5：客胜 0-1（diff=1）→ score_h=0.0 + adj_k=20 + 主队分数下降。"""
    ratings = {"Arsenal": 1500.0, "Chelsea": 1600.0}
    elo_h, elo_a, new_h, new_a, adj_k = elo_mod._elo_apply_result(
        "Arsenal", "Chelsea", 0, 1, ratings, k=20.0)
    # 主胜 0 < 客胜 1 → score_h=0.0
    assert new_h < elo_h  # 主队分数下降
    assert new_a > elo_a  # 客队分数上升
    # adj_k = 20（diff=1）
    assert adj_k == 20.0


def test_elo_apply_result_draw_score_h_half(elo_mod):
    """R35-5：平局 1-1（diff=0）→ score_h=0.5 + adj_k=20。

    注意：同分 + home_adv=True：effective_h = 1500+100 = 1600；
    expected_score(1600, 1500) ≈ 0.6402 > 0.5。actual=0.5 < expected → 主队
    分数下降（少赢分），客队分数上升（少丢分）。锁定非对称行为。
    """
    ratings = {"Arsenal": 1500.0, "Chelsea": 1500.0}
    elo_h, elo_a, new_h, new_a, adj_k = elo_mod._elo_apply_result(
        "Arsenal", "Chelsea", 1, 1, ratings, k=20.0)
    # 平局下主队分数下降（少赢），客队分数上升（少丢）
    assert new_h < elo_h
    assert new_a > elo_a
    # adj_k = 20（diff=0 走 base_k）
    assert adj_k == 20.0


def test_elo_apply_result_high_diff_adjusts_k(elo_mod):
    """R35-5：大比分 4-0（diff=4）→ adj_k = 20*(1.75 + 0.125*(4-3)) = 37.5。

    ELO 公式含 round(new_a, 1)；分数变化幅度受 expected_score 影响。同分下
    主场大胜 effective=1600 vs 1500 → expected ≈ 0.64；调整后 home 上升 +13.5。
    adj_k 从 20 → 37.5 是关键契约锚点。
    """
    ratings = {"Arsenal": 1500.0, "Chelsea": 1500.0}
    elo_h, elo_a, new_h, new_a, adj_k = elo_mod._elo_apply_result(
        "Arsenal", "Chelsea", 4, 0, ratings, k=20.0)
    assert adj_k == 37.5  # 20 * (1.75 + 0.125) = 37.5
    # 大比分下主队分数上升
    assert new_h > elo_h
    assert new_a < elo_a


def test_elo_apply_result_missing_team_uses_default_elo(elo_mod):
    """R35-5：缺队 → DEFAULT_ELO (1500) 兜底；ratings 写入新键。"""
    ratings = {"Arsenal": 1600.0}  # Chelsea 缺
    elo_h, elo_a, new_h, new_a, adj_k = elo_mod._elo_apply_result(
        "Arsenal", "Chelsea", 2, 0, ratings, k=20.0)
    # Chelsea 默认 1500
    assert elo_a == 1500.0
    # ratings 写入 Chelsea 新分数
    assert "Chelsea" in ratings
    assert ratings["Chelsea"] == new_a
    # Arsenal 原分数写回新分数
    assert ratings["Arsenal"] == new_h