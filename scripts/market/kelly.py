"""单场 EV 与 Kelly —— **建议 4 的公式唯一真源**（scripts 层）。

为什么抽到这个模块：
- `web/services/combo.py` 里早就有一套 Kelly/EV（串关用），但它在 **web 层**；
- 建议 4 的「今日推荐单」要落库、要跟 `predict.py` 同节奏跑 ⇒ 在 **scripts 层**；
  两边各写一份公式，迟早会漂（改一处忘另一处 ⇒ 页面上的注数与落库的建议对不上）。
⇒ 本模块是唯一真源；`web/services/combo.py` 改为**委托**本模块（见其注释），
   两侧公式一致由 `tests/test_suggestion4_decision.py` 的对照用例锁定。

口径（与 web 层历史行为**逐字一致**，不得擅自改）：
- 赔率合法门槛 ``ODDS_MIN = 1.01``（<=1.01 视为无效腿）
- 单位 EV：``ev = p_model × odds − 1``（>ODDS_MIN 才算）
- 分数 Kelly：``f* = (b·p − q) / b``，``b = odds − 1``，``q = 1 − p``；**负数截 0**
- 概率钳制到 ``[0, 1]``；``p = 0`` 时 Kelly = 0（无信息腿）

⚠️ 使用纪律：这些数**只是「按模型概率与给定赔率算出的数学结果」**，
不代表「有真实优势」。本仓库的模型概率是**市场锚定**的（ESPN 赔率 + onside + ELO + 形态加权），
所以 EV 里混着跨市场定价差 ⇒ 落地一律用 **1/4 Kelly** 保守，且页面必须标注。
"""
from __future__ import annotations

import math
from typing import Any

# 赔率合法门槛（与 web/services/combo.py 的 _ODDS_MIN 同值）
ODDS_MIN = 1.01

# 概率钳制边界
PROB_MIN = 0.0
PROB_MAX = 1.0

# 默认仓位系数：1/4 Kelly（保守）
DEFAULT_FRACTION = 0.25


def clamp_prob(p: Any) -> float:
    """概率钳制到 [0, 1]；非数值 ⇒ 0.0（视为无信息腿）。"""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(p):
        return 0.0
    return max(PROB_MIN, min(PROB_MAX, p))


def clamp_odds(odds: Any) -> float:
    """赔率转 float；非数值/非有限 ⇒ 0.0（调用方按 <= ODDS_MIN 判非法）。"""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(o):
        return 0.0
    return o


def ev_per_unit(p_model: Any, odds: Any) -> float | None:
    """单位 EV = p × odds − 1；赔率非法（<= ODDS_MIN）⇒ None（不编数）。"""
    o = clamp_odds(odds)
    if o <= ODDS_MIN:
        return None
    return clamp_prob(p_model) * o - 1.0


def kelly_fraction(p_model: Any, odds: Any) -> float:
    """分数 Kelly ``f* = (b·p − q) / b``，``b = odds − 1``，``q = 1 − p``；负数截 0。

    赔率非法或 ``p = 0`` ⇒ 0.0（无信息腿不投）。
    """
    o = clamp_odds(odds)
    if o <= ODDS_MIN:
        return 0.0
    p = clamp_prob(p_model)
    if p <= 0.0:
        return 0.0
    b = o - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (b * p - (1.0 - p)) / b)


def suggested_stake(p_model: Any, odds: Any, *, fraction: float = DEFAULT_FRACTION,
                    bankroll: float = 100.0) -> float:
    """建议注额 = 资金 × 分数 Kelly × 系数（默认 1/4 Kelly，保守）。

    ``bankroll`` 是「可投总额」的口径（默认 100 = 100% 本金，单位与 bankroll 相同）。
    返回 0.0 表示不建议投（Kelly 为 0 或赔率非法）。
    """
    frac = max(0.0, float(fraction))
    try:
        bb = float(bankroll)
    except (TypeError, ValueError):
        bb = 0.0
    if bb <= 0 or frac <= 0:
        return 0.0
    return round(bb * kelly_fraction(p_model, odds) * frac, 4)
