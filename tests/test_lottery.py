from __future__ import annotations

from web.services import lottery as L


def test_C():
    assert L.C(5, 2) == 10
    assert L.C(6, 3) == 20
    assert L.C(5, 0) == 1
    assert L.C(2, 5) == 0


def test_dan_tuo():
    # 胆2拖3选5 → 补3个拖码，C(3,3)=1
    assert L.dan_tuo_tickets(2, 3, 5)["注数"] == 1
    # 胆3拖5选6 → 补3，C(5,3)=10
    assert L.dan_tuo_tickets(3, 5, 6)["注数"] == 10
    # 选项太小的兜底
    assert L.dan_tuo_tickets(5, 3, 3)["注数"] == 0


def test_dlt_prize():
    assert L._dlt_prize(5, 2) == "一等奖"
    assert L._dlt_prize(5, 1) == "二等奖"
    assert L._dlt_prize(4, 2) == "三等奖"
    assert L._dlt_prize(3, 0) == "六等奖"
    assert L._dlt_prize(0, 0) is None


def test_match_prize_dlt():
    r = L.match_prize("85", [1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 8])
    assert r["status"] == "win"
    assert r["front_hit"] == 5 and r["back_hit"] == 1  # 5+1 → 二等奖


def test_match_prize_nonwin():
    r = L.match_prize("35", [1, 2, 3], [9, 0, 1])
    assert r["status"] == "no"
    assert r["prize"] is None


def test_compound_cost_dlt():
    # 前区选 6、后区选 3 → C(6,5)*C(3,2) = 6*3 = 18 注
    r = L.compound_cost("85", {"front": [1, 2, 3, 4, 5, 6], "back": [1, 2, 3]})
    assert r["注数"] == 18
    assert r["每注2元成本"] == 36


def test_stats():
    r = L.stats([[1, 2, 3], [4, 5, 2], [2, 6, 7]])
    assert r["count"] == 9
    assert r["unique"] == 7
    assert r["most"][0][0] == 2  # 2 出现 3 次
    assert r["odd_even"]["odd"] == 4 and r["odd_even"]["even"] == 5


def test_random_baseline():
    # 注入：固定收益，策略永远猜 [1]，随机也是历史号码；都比 baseline 高
    hist = [[1], [2], [3], [4], [5], [6], [7], [8], [9], [0]]
    def pay(guess, draw):
        return 1 if guess == draw else -1
    strategies = [{"name": "hot1", "generate": lambda: [1]},
                  {"name": "empty", "generate": lambda: [9]}]
    r = L.random_baseline_backtest(pay, hist, strategies, rounds=10)
    assert "random_baseline_avg" in r
    assert r["strategies"]["hot1"]["beats_random"]  # 猜1对1次，净胜


def test_bad_number_fallback():
    r = L.match_prize("85", ["a"], [])
    assert r["status"] == "no"
    assert r["note"] == "bad_number"