"""建议 4（决策层：今日推荐单 + Kelly + 竞彩官方赔率结算）的契约单测。

分四组：
  A. Kelly / EV 纯函数（scripts/market/kelly.py —— 公式唯一真源）；
  B. 与 web 层 combo 的**公式一致性**（web 层已改为委托真源，这里锁住行为没漂）；
  C. 推荐单选择逻辑（每场最多推一侧、阈值、概率下限）；
  D. 结算逻辑（win_flag 优先、比分兜底、查不到就不判、结算 SP 算盈亏）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision.recommend import (  # noqa: E402
    Candidate,
    _sp,
    build_candidates,
    load_sale_matches,
    pick_recommendation,
)
from decision.settle import is_hit, settle_pick  # noqa: E402
from market.kelly import (  # noqa: E402
    ev_per_unit,
    kelly_fraction,
    suggested_stake,
)

# 定价快照（fact.jc_offer.snap_ts）必须**相对现在**造：推荐单的定价新鲜度守门按
# 快照龄判定（默认阈值 6 小时），写死日期会随时间推移变成「超龄」⇒ 测试变红
# （2026-10-22 实测踩到：写死的 2026-10-20 快照被守门跳过，候选变 0）。
_FRESH_SNAP = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
_STALE_SNAP = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

# ── A. Kelly / EV 纯函数 ───────────────────────────────────────────────

def test_ev_per_unit_known_values():
    """单位 EV = p × odds − 1。"""
    assert ev_per_unit(0.5, 2.0) == pytest.approx(0.0)
    assert ev_per_unit(0.6, 2.0) == pytest.approx(0.2)
    assert ev_per_unit(0.4, 2.5) == pytest.approx(0.0)
    assert ev_per_unit(0.55, 1.9) == pytest.approx(0.045)


def test_ev_per_unit_illegal_odds_returns_none():
    """赔率非法 ⇒ None（不编数）。"""
    assert ev_per_unit(0.5, 1.0) is None
    assert ev_per_unit(0.5, 0.9) is None
    assert ev_per_unit(0.5, None) is None
    assert ev_per_unit(0.5, "abc") is None


def test_kelly_fraction_known_values():
    """分数 Kelly f* = (b·p − q)/b；负值截 0。"""
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.0)     # 公平赔率 ⇒ 0
    assert kelly_fraction(0.6, 2.0) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 2.0) == pytest.approx(0.0)     # 负 EV ⇒ 截 0
    assert kelly_fraction(0.0, 2.0) == pytest.approx(0.0)     # 无信息腿
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.0)     # 非法赔率


def test_kelly_matches_closed_form():
    """与闭式公式对账（独立实现，避免自证）。"""
    for p, odds in ((0.55, 1.85), (0.62, 2.4), (0.3, 3.6), (0.7, 1.5)):
        b = odds - 1.0
        expect = max(0.0, (b * p - (1.0 - p)) / b)
        assert kelly_fraction(p, odds) == pytest.approx(expect)


def test_suggested_stake_uses_quarter_kelly():
    """建议注额 = 本钱 × 分数 Kelly × 1/4（保守）。"""
    p, odds = 0.6, 2.0
    assert suggested_stake(p, odds, fraction=0.25, bankroll=100.0) == pytest.approx(100 * 0.2 * 0.25)
    assert suggested_stake(p, odds, fraction=1.0, bankroll=100.0) == pytest.approx(20.0)
    assert suggested_stake(0.4, 2.0, bankroll=100.0) == 0.0     # 负 EV ⇒ 不投


def test_sp_parser_handles_string_odds():
    """竞彩 SP 是字符串；非法值 ⇒ None。"""
    assert _sp("1.86") == pytest.approx(1.86)
    assert _sp(2.0) == pytest.approx(2.0)
    assert _sp("") is None
    assert _sp("abc") is None
    assert _sp(None) is None
    assert _sp("0.5") is None
    assert _sp(True) is None


# ── B. 与 web 层 combo 的公式一致性 ───────────────────────────────────

def test_web_combo_kelly_equals_scripts_source():
    """web 层 combo 已改为委托 scripts/market/kelly.py ⇒ 两侧必须逐项相等。

    这是「唯一真源」的关键断言：只要 web 层哪天又自己写一份公式，这里就会红。
    """
    from web.services.combo import evaluate_combo

    cases = [(0.5, 2.0), (0.6, 2.0), (0.4, 2.0), (0.55, 1.85), (0.33, 3.2),
             (0.7, 1.5), (0.28, 4.1), (0.0, 2.0)]
    for p, odds in cases:
        legs = evaluate_combo([{"side": "主胜", "odds": odds, "p_model": p}])
        web_kelly = legs["legs"][0]["kelly"]
        web_ev = legs["legs"][0]["ev"]
        assert web_kelly == pytest.approx(kelly_fraction(p, odds), abs=1e-4)
        expect_ev = ev_per_unit(p, odds)
        assert (web_ev is None) == (expect_ev is None)
        if expect_ev is not None:
            assert web_ev == pytest.approx(expect_ev, abs=1e-4)


def test_web_combo_illegal_odds_behaviour_unchanged():
    """非法赔率 ⇒ EV 为 None、Kelly 不计、warning 提示（历史行为不得变）。"""
    from web.services.combo import evaluate_combo

    out = evaluate_combo([{"side": "主胜", "odds": 1.0, "p_model": 0.5}])
    assert out["legs"][0]["ev"] is None
    assert out["legs"][0]["kelly"] is None
    assert any("非法" in w for w in out["warning"])


# ── C. 推荐单选择逻辑 ─────────────────────────────────────────────────

def _cand(p_home=0.5, p_draw=0.3, p_away=0.2, sp=(2.0, 3.5, 4.0)) -> Candidate:
    return Candidate(
        match_id=2042111, home_cn="甲", away_cn="乙", match_date="2026-10-21",
        sp={"h": sp[0], "d": sp[1], "a": sp[2]},
        p_model={"h": p_home, "d": p_draw, "a": p_away},
        league_key="epl", stars="3-star", confidence=0.7,
    )


def test_pick_recommendation_takes_highest_ev_side_only():
    """每场最多推一侧：三侧里取 EV 最高的。"""
    cand = _cand(p_home=0.5, p_draw=0.3, p_away=0.35, sp=(2.0, 3.5, 4.0))
    pick = pick_recommendation(cand, min_ev=0.0, min_p=0.0)
    assert pick is not None
    # EV: h=0.0 / d=0.05 / a=0.40 ⇒ 选客胜
    assert pick["option_code"] == "a"
    assert pick["ev"] == pytest.approx(0.4)
    assert pick["sp"] == 4.0
    assert pick["p"] == pytest.approx(0.35)
    assert set(pick["all_ev"]) == {"h", "d", "a"}


def test_pick_recommendation_respects_ev_threshold():
    """最高 EV 也不过阈值 ⇒ 整场不推（不给「差一点」的推荐）。

    三侧 EV：h=0.50×2−1=0.00 / d=0.28×3.5−1=−0.02 / a=0.22×4−1=−0.12 ⇒ 最高 0.00 < 0.05。
    """
    cand = _cand(p_home=0.50, p_draw=0.28, p_away=0.22, sp=(2.0, 3.5, 4.0))
    assert pick_recommendation(cand, min_ev=0.05) is None
    assert pick_recommendation(cand, min_ev=0.0, min_p=0.0) is not None


def test_pick_recommendation_respects_probability_floor():
    """概率低于下限 ⇒ 不推（不要长尾彩票）。"""
    cand = _cand(p_home=0.06, p_draw=0.04, p_away=0.90, sp=(2.0, 3.5, 4.0))
    # 客胜 EV 极大但 p=0.9 ⇒ 会被推；把下限调到 0.95 则整场不推
    assert pick_recommendation(cand, min_ev=0.05, min_p=0.95) is None
    assert pick_recommendation(cand, min_ev=0.05, min_p=0.10) is not None


def test_pick_recommendation_kelly_and_stake_present():
    """推荐单必须带 Kelly 与建议注额（建议 4 子项 ②）。"""
    pick = pick_recommendation(_cand(p_home=0.6, p_draw=0.25, p_away=0.15),
                               min_ev=0.05, min_p=0.10)
    assert pick is not None
    assert pick["option_code"] == "h"
    assert pick["kelly"] == pytest.approx(kelly_fraction(0.6, 2.0), abs=1e-6)
    assert pick["stake"] == pytest.approx(suggested_stake(0.6, 2.0), abs=1e-6)
    assert pick["stake"] < 100 * pick["kelly"]      # 1/4 Kelly ⇒ 小于满仓


def test_pick_recommendation_missing_sp_side_is_skipped():
    """某一侧没有价 ⇒ 不参与竞争（不用半个选项）。"""
    cand = _cand()
    cand.sp.pop("a")
    pick = pick_recommendation(cand, min_ev=0.0, min_p=0.0)
    assert pick is not None
    assert pick["option_code"] in ("h", "d")
    assert pick["all_ev"]["a"] is None


# ── D. 结算逻辑 ───────────────────────────────────────────────────────

def test_is_hit_uses_win_flag_first():
    """优先用官方 win_flag（H/D/A）。"""
    assert is_hit("h", "H", None, None) == (True, "win_flag")
    assert is_hit("a", "H", None, None) == (False, "win_flag")
    assert is_hit("d", "D", None, None) == (True, "win_flag")
    assert is_hit("h", "A", None, None) == (False, "win_flag")


def test_is_hit_falls_back_to_score():
    """win_flag 缺失 ⇒ 用比分 ft_h/ft_a，并记明来源。"""
    assert is_hit("h", None, 2, 1) == (True, "score")
    assert is_hit("h", None, 1, 2) == (False, "score")
    assert is_hit("d", None, 1, 1) == (True, "score")
    assert is_hit("a", None, 0, 3) == (True, "score")


def test_is_hit_returns_none_when_unknown():
    """两者都查不到 ⇒ (None, 'none')：**不把「查不到」当成「没中」**。"""
    assert is_hit("h", None, None, None) == (None, "none")
    assert is_hit("h", "", None, None) == (None, "none")


def test_settle_pick_payout_uses_official_settle_sp():
    """命中 ⇒ payout = 注额 × **竞彩官方结算 SP**（不是决策 SP）。"""
    pick = {"report_id": 1, "match_id": 2042111, "option_code": "h",
            "sp_decide": 1.86, "p_model": 0.5, "match_date": "2026-10-21", "league_key": "epl"}
    result = {"sp_home": 2.0, "sp_draw": 3.5, "sp_away": 4.0, "ft_h": 2, "ft_a": 1, "win_flag": "H"}
    s = settle_pick(pick, result, stake=2.0)
    assert s is not None
    assert s["hit"] is True
    assert s["sp_settle"] == pytest.approx(2.0)
    assert s["payout"] == pytest.approx(4.0)       # 2 元 × 结算 SP 2.0
    assert s["pnl"] == pytest.approx(2.0)
    assert s["settle_source"] == "win_flag"


def test_settle_pick_miss_gives_zero_payout():
    """未命中 ⇒ payout = 0，盈亏 = −注额。"""
    pick = {"report_id": 1, "match_id": 2042111, "option_code": "a",
            "sp_decide": 4.0, "p_model": 0.2, "match_date": "2026-10-21", "league_key": "epl"}
    result = {"sp_home": 2.0, "sp_draw": 3.5, "sp_away": 4.0, "ft_h": 2, "ft_a": 1, "win_flag": "H"}
    s = settle_pick(pick, result, stake=2.0)
    assert s is not None
    assert s["hit"] is False
    assert s["payout"] == 0.0
    assert s["pnl"] == pytest.approx(-2.0)


def test_settle_pick_without_result_returns_none():
    """没有官方赛果 ⇒ None（**不算未中**，不算已结算）。"""
    pick = {"report_id": 1, "match_id": 2042111, "option_code": "h",
            "sp_decide": 1.86, "p_model": 0.5, "match_date": "2026-10-21", "league_key": "epl"}
    assert settle_pick(pick, None, stake=2.0) is None
    assert settle_pick(pick, {"sp_home": 2.0, "sp_draw": 3.5, "sp_away": 4.0,
                              "ft_h": None, "ft_a": None, "win_flag": None}, stake=2.0) is None


# ── E. DB 行读取路径（tuple 行形态）—— 生产首跑踩过的坑 ────────────────
#
# `store.pg.connect()` 用 psycopg3 默认的 `tuple_row` ⇒ `conn.execute()` 返回的行是
# **普通 tuple**。原实现写的是 `dict(r)` ⇒ 生产首次跑「今日推荐单」时抛
# `TypeError: object is not iterable`，决策层失败被 warning 吞掉 ⇒ 看板永远是空的。
# 这一组用**真实的 tuple 行形态**把读取路径钉住。

class _TupleCursor:
    """模拟 psycopg3 默认 tuple_row 的游标。"""

    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _TupleConn:
    """按 SQL 关键字返回 tuple 行的假连接。"""

    def __init__(self, *, sale_rows=None, archive_rows=None):
        self._sale_rows = sale_rows or []
        self._archive_rows = archive_rows or []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if "fact.jc_match m" in text and "business_date" in text:
            return _TupleCursor(self._sale_rows)
        if "model.pred_archive_match am" in text:
            return _TupleCursor(self._archive_rows)
        return _TupleCursor([])


def test_load_sale_matches_accepts_tuple_rows():
    """tuple 行 ⇒ 能取到字段（修复前 `dict(r)` 会抛 TypeError）。"""
    conn = _TupleConn(sale_rows=[
        (2042111, "2026-10-21", "甲队", "乙队", "西甲",
         {"h": "1.86", "d": "3.70", "a": "3.12"}, _FRESH_SNAP),
    ])
    rows = load_sale_matches(conn)
    assert len(rows) == 1
    assert rows[0]["match_id"] == 2042111
    assert rows[0]["home_cn"] == "甲队"
    assert rows[0]["away_cn"] == "乙队"
    assert rows[0]["options"]["h"] == "1.86"


def test_build_candidates_today_mode_with_tuple_rows():
    """今日模式整条读取路径在 tuple 行下跑通（生产首跑实测的回归）。"""
    conn = _TupleConn(
        sale_rows=[
            (2042111, "2026-10-21", "甲队", "乙队", "西甲",
             {"h": "1.86", "d": "3.70", "a": "3.12"}, _FRESH_SNAP),
            # 这一场 options={} ⇒ SP 不齐 ⇒ 必须跳过（不猜、不用半个选项）
            (2042112, "2026-10-21", "丙队", "丁队", "西甲",
             {}, _FRESH_SNAP),
        ],
        archive_rows=[
            # (jc_match_id, p_home, p_draw, p_away, stars, confidence, low_conf, match_date, league_key, run_at)
            (2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
             "2026-10-22", "laliga", "2026-10-21 01:30:02+00"),
        ],
    )
    # 预测文件兜底不参与（避免依赖生产目录内容）
    import decision.recommend as rec_mod

    orig = rec_mod.latest_pred_rows
    rec_mod.latest_pred_rows = lambda directory=None: {}
    try:
        cands, stats = build_candidates(conn, backfill=False)
    finally:
        rec_mod.latest_pred_rows = orig

    assert stats["sale"] == 2          # 在售 2 场
    assert stats["with_sp"] == 1       # 其中 1 场 SP 齐全
    assert stats["with_prob"] == 1     # 且归档里有它的概率
    assert len(cands) == 1
    assert cands[0].match_id == 2042111
    assert cands[0].sp == {"h": 1.86, "d": 3.7, "a": 3.12}
    assert cands[0].p_model == {"h": 0.55, "d": 0.25, "a": 0.2}
    assert cands[0].source == "archive"


def test_today_mode_candidate_reaches_pick_stage():
    """端到端：tuple 行 → 候选 → 推荐单（EV 达标才推）。"""
    conn = _TupleConn(
        sale_rows=[
            (2042111, "2026-10-21", "甲队", "乙队", "西甲",
             {"h": "3.00", "d": "3.70", "a": "3.12"}, _FRESH_SNAP),
        ],
        archive_rows=[
            (2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
             "2026-10-22", "laliga", "2026-10-21 01:30:02+00"),
        ],
    )
    import decision.recommend as rec_mod

    orig = rec_mod.latest_pred_rows
    rec_mod.latest_pred_rows = lambda directory=None: {}
    try:
        cands, _stats = build_candidates(conn, backfill=False)
    finally:
        rec_mod.latest_pred_rows = orig

    # h: 0.55 × 3.00 − 1 = 0.65 ⇒ 达标；a: 0.20 × 3.12 − 1 = −0.376；d: 0.25 × 3.7 − 1 = −0.075
    pick = pick_recommendation(cands[0], min_ev=0.05, min_p=0.10)
    assert pick is not None
    assert pick["option_code"] == "h"
    assert pick["ev"] == pytest.approx(0.65)
    assert pick["sp"] == 3.0

