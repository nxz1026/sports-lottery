"""lottery 四件套 + 胆拖/复式组合数学（纯函数，无 DB/IO）。

对应 Phase3 item10「lottery 四件（奖级判定/复式成本/描述统计/随机基线证伪回测）」
与奖期票面的胆拖组合数学。规则只做「命中判定」，不编造具体奖金额（官方动态）。
彩种：超级大乐透(85)=前区5[1-35]+后区2[1-12]；排列3(35)=3位0-9；排列5(350133)=5位0-9；
7星彩(04)=6位+特别号[0-14]。
"""
from __future__ import annotations

import math
from collections import Counter
from functools import lru_cache
from typing import Iterable


# ---- 彩种元数据（范围/位数；不做金额） ----
GAME_SPEC: dict = {
    "85":      {"name": "超级大乐透", "front": 5, "back": 2, "front_pool": 35, "back_pool": 12},
    "35":      {"name": "排列3",       "digits": 3, "pool": 10},
    "350133":  {"name": "排列5",       "digits": 5, "pool": 10},
    "04":      {"name": "7星彩",       "digits": 6, "pool": 10, "special_pool": 15},  # 特别号 0-14
}


# ---------- 1) 奖级判定（命中判定，非金额） ----------
def match_prize(game_num: str, ticket: list, draw: list) -> dict:
    """判定一注 ticket 命中 draw 的大小关系（超赢/全中/未中）。

    ticket/draw: 号码列表（数字或字符串均可；不依赖顺序，除 35/350133 按位）
    返回 {status:'win'|'hit_part'|'no', front_hit, back_hit, digit_hit, prize}
    """
    g = GAME_SPEC.get(game_num)
    if not g:
        return {"status": "no", "prize": None, "note": "unknown_game"}
    try:
        t = [int(x) for x in ticket]
        d = [int(x) for x in draw]
    except (TypeError, ValueError):
        return {"status": "no", "prize": None, "note": "bad_number"}

    if game_num == "85":
        front_hit = len(set(t[:5]) & set(d[:5]))
        back_hit = len(set(t[5:]) & set(d[5:7]))
        prize = _dlt_prize(front_hit, back_hit)
        status = "win" if prize in ("一等奖", "二等奖") else ("hit_part" if prize else "no")
        return {"status": status, "front_hit": front_hit, "back_hit": back_hit,
                "prize": prize or None, "note": _dlt_note(front_hit, back_hit)}
    if game_num in ("35", "350133"):
        digit_hit = sum(1 for a, b in zip(t, d) if a == b)
        full = digit_hit == len(d)
        return {"status": "win" if full else "no", "digit_hit": digit_hit,
                "prize": "一等奖" if full else None}
    if game_num == "04":
        digit_hit = sum(1 for a, b in zip(t[:6], d[:6]) if a == b)
        special_match = t[6] == d[6] if len(t) > 6 and len(d) > 6 else False
        full = digit_hit == 6 and special_match
        return {"status": "win" if full else "no", "digit_hit": digit_hit,
                "special_match": special_match, "prize": "一等奖" if full else None}
    return {"status": "no", "prize": None}


def _dlt_prize(fh: int, bh: int) -> str | None:
    """超级大乐透命中判定（官方固定表）。"""
    if fh == 5 and bh == 2: return "一等奖"
    if fh == 5 and bh == 1: return "二等奖"
    if (fh, bh) in ((5, 0), (4, 2)): return "三等奖"
    if (fh, bh) in ((4, 1), (3, 2)): return "四等奖"
    if (fh, bh) in ((4, 0), (3, 1), (2, 2)): return "五等奖"
    if (fh, bh) in ((3, 0), (2, 1), (1, 2), (0, 2)): return "六等奖"
    return None


def _dlt_note(fh: int, bh: int) -> str:
    return f"前区中{fh}、后区中{bh}"


# ---------- 2) 复式成本（组合数学） ----------
@lru_cache(maxsize=None)
def _C(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def compound_cost(game_num: str, picks: list) -> dict:
    """复式投注成本：选的号码集合 → 拆成标准注数。

    picks: 该注实际选的号码（前区/后区/各位），可能有重复/超选。
    返回 {注数, 每注单价假定, 总成本, 拆法}
    """
    g = GAME_SPEC.get(game_num)
    if not g:
        return {"注数": 0, "总成本": 0, "note": "unknown_game"}
    if game_num == "85":  # 前区选 m、后区选 n → C(m,5)*C(n,2)
        # picks 里前 5 属前区池，后一步进后区池——由调用方按规范提供 pick_front/pick_back
        front = len(_uniq_range(picks.get("front", []), 1, g["front_pool"]))
        back = len(_uniq_range(picks.get("back", []), 1, g["back_pool"]))
        tickets = C(front, g["front"]) * C(back, g["back"]) if front >= g["front"] and back >= g["back"] else 0
        return {"注数": tickets, "每注2元成本": 2 * tickets, "拆法": f"C({front},{g['front']})×C({back},{g['back']})"}
    # 排列类：every 位一位数字 → 只有直选 1 注（len(picks) 位全定）
    return {"注数": 1, "每注2元成本": 2, "拆法": "直选一注"}


def _uniq_range(vals: Iterable, lo: int, hi: int) -> list:
    out: list[int] = []
    for v in vals:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if lo <= n <= hi and n not in out:
            out.append(n)
    return out


def C(n: int, k: int) -> int:
    return _C(n, k)


# ---------- 3) 胆拖组合数学（奖期票面工具） ----------
def dan_tuo_tickets(dan: int, tuo: int, choose: int) -> dict:
    """胆拖注数：胆码 dan 个（必选）、拖码 tuo 个（补到 choose）。
    每注 = dan 胆全选 + 从 tuo 里选 (choose-dan) 个 → C(tuo, choose-dan)。"""
    if choose <= dan:
        return {"注数": 0, "note": f"选号数 {choose} 必须大于胆码数 {dan}"}
    k = choose - dan
    return {"注数": C(tuo, k), "公式": f"C({tuo},{k})", "note": f"胆{dan}拖{tuo}选{choose}"}


# ---------- 4) 描述统计 ----------
def stats(numbers: list[list]) -> dict:
    """号码频次/奇偶/和值/区间冷热（历史开奖描述统计）。"""
    flat: list[int] = []
    for row in numbers:
        extended = [int(x) for x in row if str(x).strip().lstrip("-").isdigit()]
        flat.extend(extended)
    if not flat:
        return {"count": 0, "note": "empty"}
    freq = Counter(flat)
    even = sum(1 for x in flat if x % 2 == 0)
    return {
        "count": len(flat),
        "unique": len(freq),
        "most": freq.most_common(5),
        "odd_even": {"odd": len(flat) - even, "even": even},
        "sum_min": min(flat), "sum_max": max(flat),
        "avg": round(sum(flat) / len(flat), 2),
    }


# ---------- 5) 随机基线证伪回测 ----------
def random_baseline_backtest(payout_func, history: list[list], strategies: list[dict],
                             rounds: int = 1000) -> dict:
    """随机基线证伪：对比若干「策略」与「纯随机」在历史开奖上的累计收益。

    payout_func(预测号码, 历史draw) -> 收益（正=中奖，负=未中成本）。需外部注入（取决于彩种奖级/成本）。
    strategies: [{name, generate: () -> 一注号码}]
    返回各策略期望收益 vs 随机基线期望收益；若无策略跑赢随机 → 定位正确。
    """
    import random
    out = {}
    random_outcomes = [payout_func(random.sample(history[0] if history else [0], len(history)) if False else i, hist)
                       for i, hist in enumerate(history[:rounds])]
    random_avg = sum(random_outcomes) / len(random_outcomes) if random_outcomes else 0.0
    for st in strategies:
        payoffs = []
        for i, hist in enumerate(history[:rounds]):
            guess = st["generate"]()
            payoffs.append(payout_func(guess, hist))
        avg = sum(payoffs) / len(payoffs) if payoffs else 0.0
        out[st["name"]] = {"earn_avg": round(avg, 4), "beats_random": avg > random_avg}
    return {"random_baseline_avg": round(random_avg, 4), "strategies": out}