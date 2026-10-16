"""R34-1: scripts/core/predictor.py::_derive_lambdas_and_grid 7 测直测。

现状：P0-2 统一权重推导 DC 强度 λ + 9×9 泊松网格 0 直测；
是 _assemble_result 主链核心。R33-1 _score_grid 锁定的 5-tuple
被本函数包装成 9-tuple 返给上游（D-14 探针）。

返回 9-tuple：(lambda_home, lambda_away, raw_home, raw_away,
              predicted_score, top3, btts_prob, over_25_prob, all_scores)
（注：_score_grid 返 5 元素被包装为 9 元组；all_scores 也透传）

本轮补 7 测：
- 基础非 DC（sm=0）→ 9-tuple 结构
- DC 分支：委托 _score_grid 返 DC 5-tuple
- D-14 锁定：非 DC 分支 + sm_capped 极负 + lambda_lower_bound=0.3 →
  IndexError 触发（all_scores 全过滤 + top3 空 + 非 DC 的 top3[0] 越界）
- D-14 #6 lambda_lower_bound DC 边界：use_dixon_coles=True + sm=-100 →
  DC 闭式解保留低概率项，top3 有效
- league multiplier：bundesliga(3.2) vs unknown(2.8) → lambda_home 不同
- ELO 加成：elo_home_expected=0.7 → raw_home 上抬
- 9-tuple shape 锁定（恒为 9）
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
        "derive_lambdas_and_grid_test",
        REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(mod, **overrides):
    """公共调用：market 三元 + onside + sm_capped=0 + onside_weight=0.3 + 非 DC"""
    kwargs = dict(
        league_key="epl",
        hp=0.5, ap=0.5,
        home_onside=0.5, away_onside=0.5,
        sm_capped=0.0, onside_weight=0.3,
        elo_ratings=None, elo_home_expected=None, elo_away_expected=None,
        ELO_WEIGHT=0.18,
        hfs=0.5, hrs=0.5, afs=0.5, ars=0.5,
        use_dixon_coles=False, dc_rho=0.0,
    )
    kwargs.update(overrides)
    return mod._derive_lambdas_and_grid(**kwargs)


# ──────────────────────── _derive_lambdas_and_grid ────────────────────────


def test_derive_lambdas_and_grid_basic_returns_9tuple(predictor_mod):
    """R34-1：sm=0 + 非 DC 路径 → 返 9-tuple 结构；lambda_home/away 由 raw * LM 推算。"""
    out = _call(predictor_mod)
    assert len(out) == 9
    lambda_home, lambda_away, raw_home, raw_away, predicted, top3, btts, over, all_s = out
    # lambda 钳位后 safety >= lambda_lower_bound（0.3）
    assert lambda_home >= 0.3 and lambda_away >= 0.3
    # raw 来自 raw_home_unified*0.75 + (hfs*0.15+hrs*0.10)
    # raw_home_unified = 0.5*0.45+0.5*0.3+0 = 0.375 → raw = 0.375*0.75+0.125 = 0.40625
    assert abs(raw_home - 0.40625) < 1e-9
    # lambda = raw * 2.8 = 1.1375
    assert abs(lambda_home - 1.1375) < 1e-9
    # predicted_score 字符串
    assert isinstance(predicted, str) and "-" in predicted
    # top3 非空
    assert len(top3) >= 1
    # btts/over ∈ [0, 1]
    assert 0 <= btts <= 1 and 0 <= over <= 1


def test_derive_lambdas_and_grid_dc_branch_delegates(predictor_mod):
    """R34-1：use_dixon_coles=True → top3 来自 _score_grid DC 分支（结构闭式解）。"""
    out = _call(predictor_mod, use_dixon_coles=True, dc_rho=-0.1)
    lambda_home, lambda_away, raw_home, raw_away, predicted, top3, btts, over, all_s = out
    # DC 分支 all_scores 应 ≥ 1（含 (0,0) 等低概率项，R33-1 已锁）
    assert len(all_s) >= 1
    # predicted 由 top3[0] 拼接
    assert predicted == f"{top3[0][0]}-{top3[0][1]}"
    # 排序降序
    for i in range(len(top3) - 1):
        assert top3[i][2] >= top3[i + 1][2]


def test_derive_lambdas_and_grid_d14_non_dc_lambda_lower_bound_raises(predictor_mod):
    """R34-1 D-14 锁定：非 DC 分支 + sm_capped=-100 + lambda_lower_bound=0.3
    → IndexError（all_scores 全过滤 + top3 空）。

    raw_home_unified = 0*0.45 + 0*0.3 + (-100)*0.5 = -50 → raw_home = -50*0.75+0 = -37.5
    lambda_home = max(-37.5*2.8, 0.3) = 0.3（钳到下界）
    raw_away_unified = 0*0.45 + 0*0.3 + 100*0.5 = 50 → raw_away = 37.5
    lambda_away = max(37.5*2.8, 0.3) = 105

    _score_grid(0.3, 105, False, 0)：非 DC 分支 9×9 枚举
    poisson_pmf(0, 0.3) ≈ 0.55, poisson_pmf(0, 105) ≈ 0 → 多数单元格 p < 0.001
    all_scores 全过滤 → top3 空 → top3[0] IndexError。

    锁定现状：lambda_lower_bound 是 0.3（不是 0）使非 DC 分支极端不对称
    场景触发 IndexError，未来切换到 lambda_lower_bound=0 时需重测。
    """
    with pytest.raises(IndexError):
        _call(predictor_mod, hp=0.0, ap=0.0, home_onside=0.0, away_onside=0.0,
              sm_capped=-100.0, hfs=0.0, hrs=0.0, afs=0.0, ars=0.0,
              use_dixon_coles=False)


def test_derive_lambdas_and_grid_d14_lambda_lower_bound_DC_survives(predictor_mod):
    """R34-1 D-14 #6：DC 分支在温和 lambda_lower_bound 边界（0.3, 5.25）仍能返有效 top3。

    DC 闭式解 dixon_coles_match_probs(0.3, 5.25) 保留低概率项。
    极端 (0.3, 105) + dc_rho=-0.1 会触发 `tau_correction clamped to 0` → score_probs 空
    → DC 也 IndexError（D-14 边界扩展，本测用温和输入验证 DC 正常路径）。
    """
    out = _call(predictor_mod, hp=0.0, ap=0.0, home_onside=0.0, away_onside=0.0,
                sm_capped=-5.0, hfs=0.0, hrs=0.0, afs=0.0, ars=0.0,
                use_dixon_coles=True, dc_rho=-0.1)
    lambda_home, lambda_away, raw_home, raw_away = out[:4]
    # lambda 钳位生效：raw_home=-1.875*2.8=-5.25 → max(-5.25, 0.3)=0.3
    assert lambda_home == 0.3
    # raw_away=1.875*2.8=5.25（不上抬到下界）
    assert lambda_away == 5.25
    # top3 有效（DC 温和输入下保留低概率项）
    top3 = out[5]
    assert len(top3) >= 1


def test_derive_lambdas_and_grid_league_multiplier_overrides_default(predictor_mod):
    """R34-1：league_key="bundesliga" (LM=3.2) vs "unknown" (LM=2.8) → lambda_home 不同。

    LEAGUE_LAMBDA_MULTIPLIER (scripts/core/config.py):
    {epl:2.8, laliga:2.7, bundesliga:3.2, seriea:2.5, ligue1:2.7}
    """
    out_b = _call(predictor_mod, league_key="bundesliga")
    out_u = _call(predictor_mod, league_key="unknown")
    # bundesliga 用 3.2，unknown 用 2.8 → 同 raw 下 lambda 不同
    assert out_b[0] > out_u[0]  # bundesliga 更大
    # raw 相同（league_key 不影响 raw）
    assert out_b[2] == out_u[2]
    # lambda 比例 = 3.2/2.8
    assert abs(out_b[0] / out_u[0] - 3.2 / 2.8) < 1e-9


def test_derive_lambdas_and_grid_elo_bonus_lifts_raw_home_and_away(predictor_mod):
    """R34-1：elo_ratings + elo_home_expected/elo_away_expected → raw_home/away 上抬。

    raw_home_unified = 0.5*0.45+0.5*0.3+0 + 0.7*0.18 = 0.501
    raw_home = 0.501*0.75 + (0.5*0.15+0.5*0.10) = 0.50075
    raw_away_unified = 0.5*0.45+0.5*0.3+0 + 0.3*0.18 = 0.429
    raw_away = 0.429*0.75 + 0.125 = 0.44675
    """
    out_no = _call(predictor_mod, elo_ratings=None)
    out_elo = _call(predictor_mod, elo_ratings={"A": 1500},
                    elo_home_expected=0.7, elo_away_expected=0.3)
    # ELO 加成让 raw_home 上抬
    assert out_elo[2] > out_no[2]  # raw_home 更大
    assert out_elo[3] > out_no[3]  # raw_away 更大


def test_derive_lambdas_and_grid_return_shape_9tuple_locked(predictor_mod):
    """R34-1：返回值 shape 锁定 — 任何参数下恒为 9 元组。"""
    for params in [
        dict(use_dixon_coles=False),
        dict(use_dixon_coles=True, dc_rho=-0.1),
        dict(league_key="bundesliga"),
        dict(elo_ratings={"A": 1500}, elo_home_expected=0.6, elo_away_expected=0.4),
        dict(sm_capped=0.1),
    ]:
        out = _call(predictor_mod, **params)
        assert isinstance(out, tuple) and len(out) == 9