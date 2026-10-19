"""R34-2: scripts/core/predictor.py::_debug_fields 7 测直测。

现状：R13 #4 26 维 features_snapshot + 7 个旧别名字段（home_ml_true_prob 等）
完全 0 直测。函数装配 33 字段 dict（26+7）供 events.jsonl 训练数据 join 用。

D-4 bug-shaped锁定：home_ml_true_prob / away_ml_true_prob 别名必须保留
（events 历史已用此 key，join 时不能丢样本）。

本轮补 7 测：
- 基础 33 字段全集存在（26 + 7）
- D-4 别名 home_ml_true_prob/away_ml_true_prob == hp/ap
- onside=None → home_fifa_score/away_fifa_score 返 None
- elo_ratings=None → DEFAULT_ELO 兜底
- host_country 匹配 → is_host_country=1.0；不匹配 → 0.0
- hmi/di=None → 返 None（round 保护）
- 精度契约：probabilities round 3 位，lambda round 4 位
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
        "debug_fields_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(mod, **overrides):
    """公共调用：默认值覆盖"""
    kwargs = dict(
        hfs=0.5, hrs=0.5, afs=0.5, ars=0.5, sm=0.0,
        home_onside=0.6, away_onside=0.4, home_prob=0.5,
        draw_prob_calc=0.25, away_prob=0.25,
        elo_home_expected=0.6, elo_ratings={"Arsenal": 1600, "Chelsea": 1500},
        raw_home=1.1375, raw_away=1.05,
        hp=0.6, dp=0.2, ap=0.2,
        onside={"home": {"fifa_score": 0.85}, "away": {"fifa_score": 0.92}},
        hmi=0.55, di=0.27, odds_available=True,
        host_country="England", home_en="Arsenal", away_en="Chelsea",
    )
    kwargs.update(overrides)
    return mod._debug_fields(**kwargs)


# ──────────────────────── _debug_fields ────────────────────────


def test_debug_fields_basic_33_fields_present(predictor_mod):
    """R34-2：基础 → 返回 {"reasoning_factors": {33 字段}}（26 R13 #4 + 7 旧别名）。

    关键字段名锁定：home_true_prob/draw_true_prob/away_true_prob/odds_available
    + form 4 + market_movement 3 + signal 4 + ELO 4 + engineering 6 + host 1 = 26
    + 别名 7（home_ml_true_prob/away_ml_true_prob/home_prob_weighted/draw_prob_weighted
    /away_prob_weighted/raw_lambda_home/raw_lambda_away）= 33。
    """
    out = _call(predictor_mod)
    assert set(out.keys()) == {"reasoning_factors"}
    fields = out["reasoning_factors"]
    assert len(fields) == 33
    # 抽查几个关键键
    for key in ["home_true_prob", "draw_true_prob", "away_true_prob",
                "home_form_score", "spread_movement", "home_onside_score",
                "home_elo_rating", "elo_diff", "is_host_country",
                "home_ml_true_prob", "raw_lambda_home"]:
        assert key in fields, f"missing {key}"


def test_debug_fields_d4_alias_home_away_ml_true_prob_preserved(predictor_mod):
    """R34-2 D-4 bug-shaped 锁定：home_ml_true_prob / away_ml_true_prob 别名必须保留。

    events 历史 join 用此 key；值 == hp/ap。
    """
    fields = _call(predictor_mod, hp=0.6, ap=0.2)["reasoning_factors"]
    # D-4 契约：别名 == hp/ap（值精度 round 3）
    assert fields["home_ml_true_prob"] == 0.6
    assert fields["away_ml_true_prob"] == 0.2
    # 同样 home_prob_weighted == home_prob / draw_prob / away_prob
    assert fields["home_prob_weighted"] == 0.5
    assert fields["draw_prob_weighted"] == 0.25
    assert fields["away_prob_weighted"] == 0.25
    # raw_lambda_home/away round 4
    assert fields["raw_lambda_home"] == 1.1375
    assert fields["raw_lambda_away"] == 1.05


def test_debug_fields_onside_none_returns_none_fifa(predictor_mod):
    """R34-2：onside=None → home_fifa_score/away_fifa_score 返 None（不抛）。"""
    fields = _call(predictor_mod, onside=None)["reasoning_factors"]
    assert fields["home_fifa_score"] is None
    assert fields["away_fifa_score"] is None


def test_debug_fields_elo_ratings_none_uses_default_elo(predictor_mod):
    """R34-2：elo_ratings=None → home_elo/away_elo 用 DEFAULT_ELO (1500) 兜底。

    home_elo_rating = (elo_ratings or {}).get(home_en, DEFAULT_ELO)
    """
    fields = _call(predictor_mod, elo_ratings=None)["reasoning_factors"]
    assert fields["home_elo_rating"] == 1500.0
    assert fields["away_elo_rating"] == 1500.0
    # elo_diff = home_elo - away_elo = 0
    assert fields["elo_diff"] == 0.0


def test_debug_fields_host_country_match_marks_one(predictor_mod):
    """R34-2：host_country and home_en == host_country → is_host_country=1.0。

    is_host_country = 1.0 if host_country and home_en == host_country else 0.0
    """
    # 不匹配 → 0.0
    fields_no = _call(predictor_mod)["reasoning_factors"]
    assert fields_no["is_host_country"] == 0.0  # Arsenal != England
    # 匹配 → 1.0
    fields_yes = _call(predictor_mod, host_country="Arsenal",
                       home_en="Arsenal")["reasoning_factors"]
    assert fields_yes["is_host_country"] == 1.0
    # host_country=None → 0.0
    fields_none = _call(predictor_mod, host_country=None)["reasoning_factors"]
    assert fields_none["is_host_country"] == 0.0


def test_debug_fields_hmi_di_none_returns_none(predictor_mod):
    """R34-2：hmi=None / di=None → home_ml_implied / draw_implied 返 None（round 保护）。

    `round(hmi, 3) if hmi is not None else None` — 不抛 TypeError。
    """
    fields = _call(predictor_mod, hmi=None, di=None)["reasoning_factors"]
    assert fields["home_ml_implied"] is None
    assert fields["draw_implied"] is None


def test_debug_fields_round_precision_locked(predictor_mod):
    """R34-2：精度契约 — probabilities round 3 位，raw_lambda round 4 位。"""
    fields = _call(predictor_mod,
                   hp=0.123456789, dp=0.234567, ap=0.999,
                   raw_home=1.2345678, raw_away=0.987654)["reasoning_factors"]
    # probabilities 3 位
    assert fields["home_true_prob"] == 0.123
    assert fields["draw_true_prob"] == 0.235  # round half to even
    assert fields["away_true_prob"] == 0.999
    # 别名同样 3 位
    assert fields["home_ml_true_prob"] == 0.123
    assert fields["away_ml_true_prob"] == 0.999
    # raw_lambda 4 位
    assert fields["raw_lambda_home"] == 1.2346
    assert fields["raw_lambda_away"] == 0.9877
