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
    """随机基线必须是**真随机**：能区分"赢随机"与"输随机"的策略。

    审计 R45：旧版此测试断言 ``hot1``（恒猜 [1]）beats_random —— 但它只在
    随机基线坏掉（拿循环下标当预测号、恒取最差 -1.0）时才成立。修好基线后
    证实该断言是假的：10 场里恒猜 [1] 只中 1 场，而随机基准约 2 场，
    hot1 实际**输**给随机。这里改为断言真实契约。
    """
    hist = [[1], [2], [3], [4], [5], [6], [7], [8], [9], [0]]
    def pay(guess, draw):
        return 1 if guess == draw else -1
    # 用可索引的作弊策略：第 i 局猜 history[i]
    calls = {"i": 0}
    def cheat_gen():
        g = hist[calls["i"] % len(hist)]
        calls["i"] += 1
        return g
    strategies = [{"name": "cheat", "generate": cheat_gen},
                  {"name": "always1", "generate": lambda: [1]}]
    r = L.random_baseline_backtest(pay, hist, strategies, rounds=10)
    assert "random_baseline_avg" in r
    # 作弊：10/10 命中 → earn_avg=1.0，必赢随机
    assert r["strategies"]["cheat"]["beats_random"] is True
    assert r["strategies"]["cheat"]["earn_avg"] == 1.0
    # 恒猜 [1]：只中 1 场 → 输给随机（随机期望约 2 场）
    assert r["strategies"]["always1"]["beats_random"] is False
    # 随机基线不再是退化的最差值 -1.0
    assert r["random_baseline_avg"] > -1.0


def test_random_baseline_is_reproducible():
    """同 seed → 同结果（beats_random 不随抽样漂移）。"""
    hist = [[1], [2], [3], [4], [5], [6], [7], [8], [9], [0]]
    def pay(guess, draw):
        return 1 if guess == draw else -1
    st = [{"name": "x", "generate": lambda: [3]}]
    a = L.random_baseline_backtest(pay, hist, st, rounds=10, seed=7)
    b = L.random_baseline_backtest(pay, hist, st, rounds=10, seed=7)
    assert a == b
    # 不同 seed 允许不同（不强断言，只确保接口可传）
    c = L.random_baseline_backtest(pay, hist, st, rounds=10, seed=99)
    assert "random_baseline_avg" in c


def test_bad_number_fallback():
    r = L.match_prize("85", ["a"], [])
    assert r["status"] == "no"
    assert r["note"] == "bad_number"
