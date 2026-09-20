from __future__ import annotations

from web.services import haf as H


def test_compute_haf_sums_to_one():
    out = H.compute_haf(1.5, 1.2)
    assert out is not None
    assert set(out["probs"]) == set(H.HAF_ORDER)
    assert abs(sum(out["probs"].values()) - 1.0) < 1e-3
    assert out["pick"] in H.HAF_ORDER


def test_compute_haf_main_give_favors_win_win():
    # 主队让 1 球（强主场）→ 胜胜 应较高（主队上半场和全场合计占优）
    out = H.compute_haf(2.2, 0.8, line=-1)
    assert out["probs"]["胜胜"] > out["probs"]["负负"]


def test_compute_haf_bad_input_returns_none():
    assert H.compute_haf(None, 1.0) is None
    assert H.compute_haf(1.0, 0.0) is None
    assert H.compute_haf("x", 1.0) is None


def test_compute_haf_zero_line_same_as_none():
    a = H.compute_haf(1.5, 1.2)
    b = H.compute_haf(1.5, 1.2, line=0)
    assert a["probs"] == b["probs"]


def test_compute_haf_independent_ht_symmetry_weak_home():
    # 实力接近 → 平平概率高
    out = H.compute_haf(1.2, 1.15)
    assert out["probs"]["平平"] > 0.1