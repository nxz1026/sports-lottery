"""lottery 四件套 + 胆拖/复式组合数学（纯函数，无 DB/IO）。

对应 Phase3 item10「lottery 四件（奖级判定/复式成本/描述统计/随机基线证伪回测）」
与奖期票面的胆拖组合数学。规则只做「命中判定」，不编造具体奖金额（官方动态）。
彩种：超级大乐透(85)=前区5[1-35]+后区2[1-12]；排列3(35)=3位0-9；排列5(350133)=5位0-9；
7星彩(04)=6位+特别号[0-14]。

建议 6（体彩侧独立小产品）新增两块**纯函数**（口径见路线图 §6.8.2 / §6.8.3）：
  6) omission_stats / lottery_pools / omission_report —— 按「期」计的遗漏值 / 热号 / 冷号；
  7) compound_splits —— 复式 / 拆单式 / 胆拖 的注数·成本·出票张数·覆盖对比。
两者都**只做统计呈现与组合数学**，不做选号建议（DISCLAIMER 逐字出现在接口与看板）。
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


# ---------- 6) 遗漏值统计（建议 6） ----------
# 规范句：必须逐字出现在 ① 接口 note ② 看板面板顶部 ③ 路线图 §6.8.6
DISCLAIMER = ("本页只做历史统计呈现与组合数学计算，不构成投注建议；"
              "热号/冷号/遗漏值只描述已发生的历史事实，实测选号能力与纯随机无法区分。")

OMISSION_DEFINITION = {
    "cur": "当前遗漏：距最近一次出现后未出现的期数（0=上期刚开；从未出现=总期数）",
    "count": "出现次数：该号码在样本内出现的期数",
    "avg": "平均遗漏：总期数 ÷ 出现次数（含首尾）= 1/p，理论平均重现周期",
    "max": "最大遗漏：历史最长连续未出现期数（含首段/中段/尾段三段）",
}


def omission_stats(periods: list[list], lo: int, hi: int) -> dict:
    """按「期」计的遗漏值统计（纯函数，无 DB/IO）。

    periods: 按期**升序**的号码列表，每期一个号码列表。
    lo/hi:   号码池闭区间（前端 1-35、后区 1-12、数字位 0-9、特别号 0-14）。

    返回 {号码: {cur, avg, max, count}}，口径见 OMITTED_DEFINITION 与路线图 §6.8.2。

    ⚠️ 单位是「期」不是「天」—— 大乐透 2~3 天一期、7星彩 2 天一期，
       按天算会把「间隔天数」误当「遗漏期数」。
    ⚠️ 调用方必须保证 periods 已按期升序（遗漏值对顺序敏感）。
    """
    n = len(periods)
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    mid: dict[int, int] = {}
    count: dict[int, int] = {num: 0 for num in range(lo, hi + 1)}
    for idx, nums in enumerate(periods):
        for num in set(nums):
            if lo <= num <= hi:
                if num in last:
                    # 相邻两次出现之间的「连续未出现期数」= 索引差 - 1
                    mid[num] = max(mid.get(num, 0), idx - last[num] - 1)
                else:
                    first[num] = idx
                last[num] = idx
                count[num] += 1
    out: dict[int, dict] = {}
    for num in range(lo, hi + 1):
        cur = (n - 1 - last[num]) if num in last else n
        lead = first.get(num, n)          # 首段：首次出现前的未出现期数
        out[num] = {
            "cur": cur,
            "avg": round(n / count[num], 2) if count[num] else None,
            "max": max(lead, mid.get(num, 0), cur),   # 首段 / 中段 / 尾段(=cur) 三段取最大
            "count": count[num],
        }
    return out


def lottery_pools(game_num: str, draws: list[list]) -> list[dict]:
    """把开奖记录拆成「号码池」—— 每个池是一个可算遗漏/冷热的维度（纯函数）。

    85 大乐透：前区（5 个 1-35）+ 后区（2 个 1-12）—— 一期两个池，每池取子集。
    04 七星彩：第1~6位（各 0-9）+ 特别号（0-14）—— 一期七个数，各成一池。
    35/350133：第1~N 位（各 0-9）—— 按位各成一池。
    """
    g = GAME_SPEC.get(game_num)
    if not g:
        return []
    out: list[dict] = []
    if game_num == "85":
        out.append({"pool": "front", "name": "前区", "lo": 1, "hi": g["front_pool"],
                    "periods": [d[:5] for d in draws]})
        out.append({"pool": "back", "name": "后区", "lo": 1, "hi": g["back_pool"],
                    "periods": [d[5:7] for d in draws]})
    elif game_num == "04":
        for i in range(g["digits"]):
            out.append({"pool": f"pos{i + 1}", "name": f"第{i + 1}位", "lo": 0, "hi": 9,
                        "periods": [[d[i]] for d in draws]})
        # ⚠️ GAME_SPEC 里 front_pool/back_pool 是**上界**（1..35 / 1..12），
        #    而 pool / special_pool 是**个数**（0..9 共 10 个 / 0..14 共 15 个）
        #    ⇒ 特别号上界必须减 1，否则会把不存在的 15 号算进遗漏值。
        out.append({"pool": "special", "name": "特别号", "lo": 0, "hi": g["special_pool"] - 1,
                    "periods": [d[6:7] for d in draws]})
    else:
        for i in range(g["digits"]):
            out.append({"pool": f"pos{i + 1}", "name": f"第{i + 1}位", "lo": 0, "hi": g["pool"] - 1,
                        "periods": [[d[i]] for d in draws]})
    return out


def omission_report(game_num: str, draws: list[list]) -> dict:
    """遗漏值 / 热号 / 冷号 报告（纯函数；draws 必须按期升序）。

    注意：热号 / 冷号 / 遗漏**只是历史事实的统计呈现**，不是选号建议
    （路线图 §6.8.4 实测：选号能力与纯随机无法区分）。
    """
    g = GAME_SPEC.get(game_num)
    base = {"game": game_num, "game_name": g["name"] if g else None,
            "n_periods": len(draws), "unit": "期",
            "definition": OMISSION_DEFINITION, "note": DISCLAIMER}
    if not g:
        return {**base, "pools": [], "note": "unknown_game"}
    if not draws:
        # 库内无该彩种历史 ⇒ 不给空池（避免每号都显示「遗漏 0 期」这种无意义行）
        return {**base, "pools": []}
    pools = []
    for spec in lottery_pools(game_num, draws):
        st = omission_stats(spec["periods"], spec["lo"], spec["hi"])
        items = [{"num": num, **st[num]} for num in range(spec["lo"], spec["hi"] + 1)]
        hot = sorted(items, key=lambda x: (-x["count"], x["num"]))[:5]
        cold = sorted(items, key=lambda x: (x["count"], x["num"]))[:5]
        longest = sorted(items, key=lambda x: (-x["cur"], x["num"]))[:5]
        pools.append({
            "pool": spec["pool"], "name": spec["name"],
            "lo": spec["lo"], "hi": spec["hi"], "size": spec["hi"] - spec["lo"] + 1,
            "items": items,
            "hot": [{"num": x["num"], "count": x["count"], "cur": x["cur"]} for x in hot],
            "cold": [{"num": x["num"], "count": x["count"], "cur": x["cur"]} for x in cold],
            "max_omission": [{"num": x["num"], "cur": x["cur"], "max": x["max"]} for x in longest],
        })
    return {**base, "pools": pools}


# ---------- 7) 复式 / 拆单式 / 胆拖 拆法对比（建议 6） ----------
def compound_splits(game_num: str, front: list, back: list) -> dict:
    """复式 / 拆单式 / 胆拖 三方案的注数、成本、出票张数、覆盖组合对比（纯函数）。

    两条恒等式（可证，路线图 §6.8.3）：
      ① 一注 = 前5+后2 ⇒ 复式注数 = C(m,5)×C(n,2)（**不是 m×n**）；
         拆单式与复式覆盖**同一批组合** ⇒ 注数与成本必然相同，
         真实差别只是**出票张数**（1 张复式票 vs C(m,5)×C(n,2) 张单式票）。
      ② 胆拖注数更少，但覆盖是复式的**真子集**（胆码必须全中）
         ⇒ 不是等价替换，是「用更严的命中条件换更少的注数」。
    """
    g = GAME_SPEC.get(game_num)
    if not g:
        return {"game": game_num, "game_name": None, "rows": [], "note": "unknown_game",
                "disclaimer": DISCLAIMER}
    if game_num != "85":
        return {"game": game_num, "game_name": g["name"], "front_count": 0, "back_count": 0,
                "rows": [], "formula": None, "disclaimer": DISCLAIMER,
                "note": DISCLAIMER + " 排列/星彩类只有直选（一注），无复式与胆拖拆法；请用大乐透(85)。"}
    m = len(_uniq_range(front, 1, g["front_pool"]))
    n = len(_uniq_range(back, 1, g["back_pool"]))
    if m < g["front"] or n < g["back"]:
        return {"game": game_num, "game_name": g["name"], "front_count": m, "back_count": n,
                "rows": [], "formula": None, "disclaimer": DISCLAIMER,
                "note": DISCLAIMER + f" 前区至少选 {g['front']} 个、后区至少选 {g['back']} 个才能成注。"}
    comp = C(m, g["front"]) * C(n, g["back"])
    rows = [
        {"scheme": "复式（1 张票）", "tickets": comp, "cost": comp * 2, "ticket_count": 1,
         "coverage": comp, "coverage_ratio": 1.0,
         "note": f"1 张票覆盖全部 {comp} 个组合"},
        {"scheme": "拆单式", "tickets": comp, "cost": comp * 2, "ticket_count": comp,
         "coverage": comp, "coverage_ratio": 1.0,
         "note": f"与复式覆盖完全等价；差别只是出票张数（1 张 vs {comp} 张）与兑奖便利"},
    ]
    dan_rows = []
    for d in range(0, min(3, m - g["front"]) + 1):
        for db in range(0, min(2, n - g["back"]) + 1):
            if d == 0 and db == 0:
                continue    # 全拖 = 复式本身，已在上面两行
            t, tb = m - d, n - db
            tickets = C(t, g["front"] - d) * C(tb, g["back"] - db)
            dan_rows.append({
                "scheme": f"胆拖 前{d}胆{t}拖/后{db}胆{tb}拖",
                "tickets": tickets, "cost": tickets * 2, "ticket_count": 1,
                "coverage": tickets, "coverage_ratio": round(tickets / comp, 4),
                "note": f"覆盖是复式的真子集（{tickets}/{comp} = {tickets / comp:.1%}，胆码须全中）",
            })
    dan_rows.sort(key=lambda r: (-r["tickets"], r["scheme"]))
    rows.extend(dan_rows)
    return {
        "game": game_num, "game_name": g["name"], "front_count": m, "back_count": n,
        "formula": f"复式注数 = C({m},{g['front']})×C({n},{g['back']}) = {comp}",
        "rows": rows, "disclaimer": DISCLAIMER,
        "note": DISCLAIMER + " 拆单式与复式覆盖同一批组合 ⇒ 注数与成本必然相同，差别只是出票张数；"
                            "胆拖注数更少但覆盖是复式的子集（胆码须全中），不是等价替换。",
    }


# ---------- 5) 随机基线证伪回测 ----------
def random_baseline_backtest(payout_func, history: list[list], strategies: list[dict],
                             rounds: int = 1000, seed: int = 42) -> dict:
    """随机基线证伪：对比若干「策略」与「纯随机」在历史开奖上的累计收益。

    payout_func(预测号码, 历史draw) -> 收益（正=中奖，负=未中成本）。需外部注入（取决于彩种奖级/成本）。
    strategies: [{name, generate: () -> 一注号码}]
    返回各策略期望收益 vs 随机基线期望收益；若无策略跑赢随机 → 定位正确。

    审计 R45 修复：原实现 ``random.sample(...) if False else i`` 是**死分支**——
    随机基线实际拿"循环下标 i"当预测号（且是 int，与策略产出的号码列表不同型），
    于是 payout_func 恒走"未中"分支、random_baseline_avg 恒为最差值（实测 -1.0），
    任何策略都会被判"跑赢随机"，与函数目的（证伪策略）完全相反。
    现在按与策略相同的"号码列表"语义，从历史号码空间随机取样；
    用固定 seed 保证可复现（否则 beats_random 会随抽样漂移）。
    """
    import random
    rng = random.Random(seed)
    window = history[:rounds]
    random_outcomes = [
        payout_func(list(rng.choice(history)), hist) for hist in window
    ] if (window and history) else []
    random_avg = sum(random_outcomes) / len(random_outcomes) if random_outcomes else 0.0
    out = {}
    for st in strategies:
        payoffs = []
        for i, hist in enumerate(history[:rounds]):
            guess = st["generate"]()
            payoffs.append(payout_func(guess, hist))
        avg = sum(payoffs) / len(payoffs) if payoffs else 0.0
        out[st["name"]] = {"earn_avg": round(avg, 4), "beats_random": avg > random_avg}
    return {"random_baseline_avg": round(random_avg, 4), "strategies": out}
