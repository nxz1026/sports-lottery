"""半全场（haf）九宫格概率：全场泊松率 λh/λa 拆出上半场率（HT_RATIO=0.45），
联合遍历得到 9 宫格概率（胜胜/胜平/胜负/平胜/平平/平负/负胜/负平/负负）。

竞彩半全场 = 上半场结果 × 全场结果（各为 胜/平/负），共 9 种。官方 hhad 让球线
（goal_line）施加于**全场**结果（上半场不加让球）。

模型假设：
  - 全场进球 ~ Poisson(λh, λa)（与 hhad/had 共用 λ 通道）
  - 上半场进球 ~ Poisson(λh·HT_RATIO, λa·HT_RATIO)，HT_RATIO=0.45 经验值
  - 上半场与下半场独立（用全场-上半场余量作为下半场，等价于独立假设；近似）
  - 独立假设下全场与上半场联合：P(全场, 上半场) = P(全场) · P(上半场)

输出：``{"line": str, "probs": {9宫格: float}, "pick": "胜胜"|..., "note": str}``；
λ 或 line 非法 → ``None``。

注：原 web/services/haf.py 1:1 移植（与 hhad/had 走同一 Poisson 假设，但
独立构造"上半场"维度），未引入 scripts/ 已实现的 grid/dc_pmf_grid（这些
针对 1 场比分矩阵，没有"全场 + 上半场"二维结构）。
"""
from __future__ import annotations

from math import exp, factorial

HT_RATIO = 0.45  # 上半场进球占比经验值（可调）

# 半全场 9 宫格顺序：上半场结果 × 全场结果（胜/平/负 各 3）
HAF_ORDER = ["胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负"]

# 净胜（>0 胜，=0 平，<0 负）→ 中文符号
_DIFF_SYM = {0: "平", -1: "负", 1: "胜"}


def _pois(lmb: float, max_g: int) -> list[float]:
    """单变量 Poisson PMF（k=0..max_g）。λ≤0 时钳 0.001 防 exp(-0) 退化。"""
    if lmb <= 0:
        lmb = 0.001
    return [exp(-lmb) * lmb ** i / factorial(i) for i in range(max_g + 1)]


def probs(lambda_home, lambda_away, line=0, max_goals: int = 10) -> dict | None:
    """半全场九宫格概率。λ 或 line 非法 → None。

    Args:
        lambda_home / lambda_away: 全场进球率（与 had/hhad 共用）
        line: 官方 hhad 让球线（home 让球为负，施加于全场结果）
        max_goals: Poisson 截断上限（默认 10，与 web/services/haf.py 一致）

    Returns:
        ``{"line": str, "probs": {9宫格: 0~1}, "pick": str, "note": str}``
        或 ``None``（输入非法）。
    """
    try:
        lh = float(lambda_home)
        la = float(lambda_away)
        ln = float(line or 0)
    except (TypeError, ValueError):
        return None
    if lh <= 0 or la <= 0:
        return None

    # 全场 + 上半场 Poisson 分布
    ph = _pois(lh, max_goals)
    pa = _pois(la, max_goals)
    ph_ht = _pois(lh * HT_RATIO, max_goals)
    pa_ht = _pois(la * HT_RATIO, max_goals)

    # 联合遍历：全场 (i,j) × 上半场 (hi,hj)，独立假设
    probs_out: dict[str, float] = {k: 0.0 for k in HAF_ORDER}
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
                    key = _DIFF_SYM[ht_sym] + _DIFF_SYM[ft_sym]
                    probs_out[key] += p_ft * p_ht

    total = sum(probs_out.values())
    if total <= 0:
        return None
    probs_out = {k: round(v / total, 4) for k, v in probs_out.items()}
    pick = max(probs_out, key=lambda k: probs_out[k])
    return {
        "line": str(line),
        "probs": probs_out,
        "pick": pick,
        "note": "上半场按全场λ×0.45独立泊松近似；不含上半场让球，仅全场让球线",
    }
