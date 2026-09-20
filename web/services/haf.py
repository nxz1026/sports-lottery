"""半全场（haf）一等预测：全场泊松率 λh/λa 拆出上半场率，联合遍历得到九宫格概率。

竞彩半全场 = 上半场结果 × 全场结果（各为 胜/平/负），9 种：胜胜/胜平/胜负/平胜/平平/
平负/负胜/负平/负负。官方 hhad 让球线（goal_line）施加于**全场**结果（上半场不加）。

模型假设：
  - 全场进球 ~ Poisson(λh, λa)（复用 prediction 的 lambda_home/lambda_away）
  - 上半场进球 ~ Poisson(λh·r, λa·r)，r=HT_RATIO（经验 0.45，可配置）
  - 上半场与下半场独立（用全场-上半场余量作为下半场，等价于独立假设；近似）
  - goal_line 施加后全场结果与上半场联合 ⇒ 9 宫格概率

输出：{line, probs:{9宫格}, pick, note}
"""
from __future__ import annotations

from math import exp, factorial

HT_RATIO = 0.45  # 上半场进球占比经验值
HAF_ORDER = ["胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负"]

# 上半场结果 → 前缀（第一个字），全场结果 → 后缀（第二个字）
# 左侧：上半场，右侧：全场；用结果符号
_HT_SYM = {0: "平", -1: "负", 1: "胜"}   # 上半场净胜
_FT_SYM = {0: "平", -1: "负", 1: "胜"}   # 全场让球后净胜


def _pois(lmb: float, max_g: int) -> list[float]:
    if lmb <= 0:
        lmb = 0.001
    return [exp(-lmb) * lmb ** i / factorial(i) for i in range(max_g + 1)]


def compute_haf(lambda_home, lambda_away, line=0, max_goals: int = 10) -> dict | None:
    """半全场九宫格概率。λ 非法 → None。line 是官方 hhad 让球线（home 让球为负）。"""
    try:
        lh = float(lambda_home)
        la = float(lambda_away)
        ln = float(line or 0)
    except (TypeError, ValueError):
        return None
    if lh <= 0 or la <= 0:
        return None

    lh_ht = lh * HT_RATIO
    la_ht = la * HT_RATIO

    ph = _pois(lh, max_goals)
    pa = _pois(la, max_goals)
    ph_ht = _pois(lh_ht, max_goals)
    pa_ht = _pois(la_ht, max_goals)

    # 联合遍历：全场 (i,j) × 上半场 (hi,hj)，独立假设
    probs = {k: 0.0 for k in HAF_ORDER}
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p_ft = ph[i] * pa[j]
            ft_diff = i + ln - j          # 全场施加让球后的净胜
            ft_sym = 1 if ft_diff > 0 else (-1 if ft_diff < 0 else 0)
            for hi in range(max_goals + 1):
                for hj in range(max_goals + 1):
                    p_ht = ph_ht[hi] * pa_ht[hj]
                    ht_diff = hi - hj
                    ht_sym = 1 if ht_diff > 0 else (-1 if ht_diff < 0 else 0)
                    key = _HT_SYM[ht_sym] + _FT_SYM[ft_sym]
                    probs[key] += p_ft * p_ht

    total = sum(probs.values())
    if total <= 0:
        return None
    probs = {k: round(v / total, 4) for k, v in probs.items()}
    pick = max(probs, key=lambda k: probs[k])
    return {"line": str(line), "probs": probs, "pick": pick,
            "note": "上半场按全场λ×0.45独立泊松近似；不含上半场让球，仅全场让球线"}