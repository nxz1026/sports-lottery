"""串关 EV 引擎：单关 EV、parlay 联合 EV、Kelly 比例、风险警告。

纯函数模块。无 DB/网络/IO 依赖。所有边界场景（赔率<1、概率缺失、单腿等）
都在这里处理，路由层只做转发。

约定：
- side 字符串: "主胜" / "平" / "客胜" / "让胜" / "让平" / "让负" / NBA 玩法可扩展
- odds: 十进制赔率（>1.0），<=1 视为无效腿，忽略计入 warning
- p_model: 该侧模型估计概率（0-1 浮点），缺失 → p=0（视为无信息腿）
- 联合概率默认按"乘积假设"（独立假设，不相关性）。非独立场景由调用方传入 joint_prob 覆盖。
- Kelly 用分数 Kelly（f* = (bp - q) / b），b=odds-1，p=该腿概率，q=1-p。
  parlay Kelly = 各腿 fractional Kelly 之和（保守：leg_kelly / 1 作为权重）。
- warning 数组包含无法计算项、赔率异常、联合概率>1 等提示，UI 原样展示。
"""
from __future__ import annotations

from typing import Iterable


# 边角输入钳制
_ODDS_MIN = 1.01
_PROB_MIN = 0.0
_PROB_MAX = 1.0


def evaluate_combo(legs: list[dict],
                   joint_prob: float | None = None,
                   stake: float = 2.0) -> dict:
    """计算串关 EV / Kelly / 联合 EV。

    legs 每项: {side, odds, p_model}
      - side: 玩法侧字符串（仅展示/校验，不参与数学）
      - odds: 十进制赔率，>1.01 才算有效
      - p_model: 模型概率（0-1）
    joint_prob: 显式联合概率（独立场景由调用方传 None，由本函数按 leg 乘积算）。
    stake: 假设性投注额（仅展示用，UI 显示 "假设性 X 元回报"）。
    """
    eval_legs: list[dict] = []
    warnings: list[str] = []
    p_product: float | None = None  # None = 尚未确定；否则是各腿 p 的乘积
    odds_product = 1.0
    kelly_total = 0.0

    for i, leg in enumerate(legs or []):
        side = str(leg.get("side") or "")
        try:
            odds = float(leg.get("odds") or 0.0)
        except (TypeError, ValueError):
            odds = 0.0
        try:
            p_model = float(leg.get("p_model") or 0.0)
        except (TypeError, ValueError):
            p_model = 0.0
        p_model = max(_PROB_MIN, min(_PROB_MAX, p_model))
        ev = p_model * odds - 1.0 if odds > _ODDS_MIN else None
        if odds <= _ODDS_MIN:
            warnings.append(f"腿 {i+1}: 赔率 {odds!r} 非法（<=1.01），跳过 EV/Kelly")
            kelly = None
        else:
            if p_model <= 0.0:
                kelly = 0.0
                warnings.append(f"腿 {i+1}: 模型概率为空，Kelly 视为 0")
            else:
                b = odds - 1.0
                kelly = max(0.0, (b * p_model - (1.0 - p_model)) / b) if b > 0 else 0.0
        eval_legs.append({
            "idx": i + 1, "side": side, "odds": odds, "p_model": round(p_model, 4),
            "ev": round(ev, 4) if ev is not None else None,
            "kelly": round(kelly, 4) if kelly is not None else None,
        })
        if odds > _ODDS_MIN:
            odds_product *= odds
        # 联合概率：任一腿缺 p → 联合为 0
        if p_model <= 0.0 or p_model > 1.0:
            p_product = 0.0
        elif p_product is None:
            p_product = p_model
        else:
            p_product *= p_model

    n_valid = sum(1 for L in eval_legs if L["odds"] > _ODDS_MIN and L["p_model"] > 0)

    assumption = "independent"
    if joint_prob is None:
        joint_p = p_product if (eval_legs and p_product is not None) else 0.0
    else:
        try:
            raw = float(joint_prob)
        except (TypeError, ValueError):
            joint_p = p_product if (eval_legs and p_product is not None) else 0.0
            warnings.append("joint_prob 非法，回退到独立假设乘积")
        else:
            if raw > 1.000001:
                warnings.append("联合概率 >1（模型概率和>1），按 1 钳制")
            joint_p = max(0.0, min(1.0, raw))
            assumption = "supplied"

    parlay_ev = joint_p * odds_product - 1.0 if (eval_legs and odds_product > _ODDS_MIN) else None
    if n_valid < len(eval_legs):
        warnings.append(f"{len(eval_legs) - n_valid} 条腿概率或赔率缺失，parlay EV 可能偏低")
    if n_valid >= 2 and odds_product > _ODDS_MIN:
        kelly_total = max(0.0, (joint_p * (odds_product - 1.0) - (1.0 - joint_p)) / (odds_product - 1.0))
    elif eval_legs and odds_product > _ODDS_MIN:
        warnings.append("有效腿 <2，Kelly 按 0 处理（无法 parlay）")

    return {
        "legs": eval_legs,
        "n_legs": len(eval_legs),
        "n_valid": n_valid,
        "joint_prob": round(joint_p, 6),
        "joint_prob_assumption": assumption,
        "odds_product": round(odds_product, 4) if odds_product > _ODDS_MIN else None,
        "parlay_ev": round(parlay_ev, 4) if parlay_ev is not None else None,
        "parlay_return_2yuan": round((odds_product * stake), 2) if (odds_product > _ODDS_MIN and stake > 0) else None,
        "kelly_total": round(kelly_total, 4) if (odds_product > _ODDS_MIN and n_valid >= 2) else 0.0,
        "stake": stake,
        "warning": warnings,
    }
