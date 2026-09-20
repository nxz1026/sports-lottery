"""test_ticket.py：传统足彩 14场/任九 三向概率票面（纯函数）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))
from services import ticket  # noqa: E402


def _m(home, away, ph=0.5, pd=0.3, pa=0.2):
    return {"home": home, "away": away, "probs": {"主胜": ph, "平": pd, "客胜": pa}}


def test_pick_side_max_prob():
    assert ticket.pick_side({"主胜": 0.6, "平": 0.3, "客胜": 0.1})["side"] == "主胜"
    assert ticket.pick_side({"主胜": 0.2, "平": 0.7, "客胜": 0.1})["side"] == "平"


def test_pick_side_rejects_unbalanced():
    with pytest.raises(ValueError, match="未归一"):
        ticket.pick_side({"主胜": 0.6, "平": 0.3, "客胜": 0.3})  # sum=1.2


def test_pick_side_rejects_bad_type():
    with pytest.raises(ValueError, match="非法"):
        ticket.pick_side({"主胜": "x", "平": 0.5, "客胜": 0.5})


def test_sfc_picks_14_regular():
    ms = [_m(f"H{i}", f"A{i}") for i in range(14)]
    out = ticket.sfc_picks(ms)
    assert len(out) == 14
    assert all(r["side"] == "主胜" for r in out)  # 每场 ph=0.5 最大
    assert all(r["error"] is None for r in out)


def test_sfc_picks_missing_probs_flagged_not_guessed():
    ms = [_m("H1", "A1"), {"home": "H2", "away": "A2"}]  # 第2场无 probs
    out = ticket.sfc_picks(ms)
    assert out[0]["side"] == "主胜"
    assert out[1]["error"] == "no_probs"
    assert out[1]["side"] is None  # 不编数


def test_parallel_prob_independent():
    ms = [_m("A", "B", ph=0.5), _m("C", "D", ph=0.5)]
    picks = ticket.sfc_picks(ms)
    p = ticket.parallel_prob(picks)
    assert p == pytest.approx(0.25, abs=1e-6)


def test_parallel_prob_empty_raises():
    with pytest.raises(ValueError, match="空列表"):
        ticket.parallel_prob([])


def test_choose_combinations_14_9():
    assert ticket.choose_combinations(14, 9) == 2002  # C(14,9)


def test_combine_prob_picks_top9():
    ms = [_m(f"T{i}", f"T{i+1}", ph=0.5 + 0.01 * i, pa=0.2 - 0.01 * i) for i in range(14)]  # 越靠后主胜概率越高，三向仍归一
    picks = ticket.sfc_picks(ms)  # 先生成逐场推荐侧 → 顶层带 .prob
    out = ticket.combine_prob(picks, 9)
    assert out["n_matches"] == 14
    assert out["choose"] == 9
    assert len(out["picked_seqs"]) == 9
    # 依赛场从 0..13，概率递增，选最高 9 → picked_seqs 是 5..13（下标 i→seq i+1）
    assert out["picked_seqs"] == [6, 7, 8, 9, 10, 11, 12, 13, 14]
    assert out["hit_prob"] == pytest.approx(
        (0.5 + 0.01 * 5) * (0.5 + 0.01 * 6) * (0.5 + 0.01 * 7) * (0.5 + 0.01 * 8)
        * (0.5 + 0.01 * 9) * (0.5 + 0.01 * 10) * (0.5 + 0.01 * 11) * (0.5 + 0.01 * 12)
        * (0.5 + 0.01 * 13), abs=1e-6)


def test_combine_prob_choose_invalid():
    ms = [_m("A", "B")]
    with pytest.raises(ValueError, match="choose"):
        ticket.combine_prob(ms, 2)