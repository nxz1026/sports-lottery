from __future__ import annotations

from web.services import combo as C


def test_empty_legs_returns_zero_metrics():
    r = C.evaluate_combo([])
    assert r["n_legs"] == 0
    assert r["n_valid"] == 0
    assert r["joint_prob"] == 0
    assert r["odds_product"] is None
    assert r["parlay_ev"] is None
    assert r["kelly_total"] == 0.0
    assert r["warning"] == []


def test_single_valid_leg_kelly_zero_p_below_threshold():
    # p=0.30, odds=2.0 → ev = 0.6-1=-0.4, kelly=0
    r = C.evaluate_combo([{"side": "主胜", "odds": 2.0, "p_model": 0.30}])
    assert r["n_legs"] == 1
    assert r["n_valid"] == 1
    assert r["legs"][0]["ev"] == -0.4
    assert r["legs"][0]["kelly"] == 0.0
    assert r["odds_product"] == 2.0
    # 单腿 parlay_ev 仍可算（joint_p=0.30, product_odds=2.0, ev=-0.40）
    assert abs(r["parlay_ev"] - (-0.4)) < 1e-6
    # 单腿不构成 parlay，Kelly=0
    assert r["kelly_total"] == 0.0


def test_two_leg_positive_ev_aggregates():
    # leg1: p=0.55, odds=1.8 → b=0.8, f*=(0.8*0.55-0.45)/0.8=0.0125
    # leg2: p=0.60, odds=1.7 → b=0.7, f*=(0.7*0.60-0.40)/0.7=0.0286
    # joint p = 0.55*0.60=0.33; product odds = 1.8*1.7=3.06
    # parlay ev = 0.33*3.06-1 ≈ 0.0098
    r = C.evaluate_combo([
        {"side": "主胜", "odds": 1.8, "p_model": 0.55},
        {"side": "客胜", "odds": 1.7, "p_model": 0.60},
    ])
    assert r["n_legs"] == 2
    assert r["n_valid"] == 2
    assert r["odds_product"] == 3.06
    assert abs(r["joint_prob"] - 0.33) < 1e-6
    assert r["parlay_ev"] > 0
    assert r["kelly_total"] > 0
    assert r["warning"] == []


def test_invalid_odds_marked_and_warning():
    r = C.evaluate_combo([{"side": "主胜", "odds": 0.5, "p_model": 0.5}])
    assert r["legs"][0]["ev"] is None
    assert r["legs"][0]["kelly"] is None
    assert any("赔率" in w for w in r["warning"])


def test_missing_p_model_handled():
    r = C.evaluate_combo([
        {"side": "主胜", "odds": 1.8, "p_model": None},
        {"side": "平",   "odds": 3.2, "p_model": 0.30},
    ])
    assert r["legs"][0]["p_model"] == 0
    assert r["legs"][0]["kelly"] == 0.0
    # joint_p 用 0 * 0.30 = 0
    assert r["joint_prob"] == 0.0


def test_supplied_joint_prob_used_with_warning_on_bad_input():
    r = C.evaluate_combo(
        [{"side": "主胜", "odds": 2.0, "p_model": 0.5}],
        joint_prob="not_a_number",
    )
    # 非法 → 回退独立 + 警告
    assert r["joint_prob_assumption"] == "independent"
    assert any("joint_prob" in w for w in r["warning"])


def test_joint_prob_over_one_clamped():
    r = C.evaluate_combo(
        [{"side": "主胜", "odds": 1.5, "p_model": 1.0}],
        joint_prob=1.5,
    )
    assert r["joint_prob"] == 1.0
    assert any("联合概率" in w for w in r["warning"])


def test_stake_used_in_parlay_return():
    r = C.evaluate_combo(
        [{"side": "主胜", "odds": 2.0, "p_model": 0.5}],
        stake=10.0,
    )
    assert r["parlay_return_2yuan"] == 20.0


def test_prob_out_of_range_clamped():
    r = C.evaluate_combo([{"side": "主胜", "odds": 2.0, "p_model": 1.5}])
    assert r["legs"][0]["p_model"] == 1.0
