"""``core.model_probs`` — **模型概率唯一真源**（P0 / 2026-10-23）。

为什么要有这个模块（实测事故复盘）
------------------------------------
2026-10-23 三路审计（静态 + 动态运行 + web 逐元素）在**同一场比赛**上抓到 4 个互相
矛盾的「赢面」：斯图加特 vs 门兴格拉德巴赫（德甲，2026-10-23 生产实测）。

==================  ==============================================  ======================
字段                实际含义                                       谁在用
==================  ==============================================  ======================
``probs``           规则模型 + ML 融合后的**最终**概率              ✅ 决策层 / 归档 / 本模块
``ml_proba``        **ML 分类器原始输出**（未与规则模型融合）       ❌ 被 web 层当成模型概率
``confidence_score`` ``(p − 0.25) × 2`` 的**把握度评分**            ❌ 被页面标成「赢面」
``reasoning_factors`` 多个 ``*_prob`` / ``*_prob_weighted`` 子字段    ⚠️ 混着市场先验
==================  ==============================================  ======================

实测数字（同一场）：

* ``probs``            = ``{"h": 0.6466, "d": 0.1405, "a": 0.2129}``  ← 决策层（推荐单）用的
* ``ml_proba``         = ``[0.9778, 0.0081, 0.0141]``                ← 卡片概率条 / edge / EV 用的
* ``confidence_score`` = ``0.793``（= (0.6466 − 0.25) × 2）          ← 卡片标成「这条的赢面」
* ``reasoning_factors`` = ``home_ml_true_prob=0.704`` / ``draw_true_prob=0.137``
  / ``away_ml_true_prob=0.159``                                     ← 串关 EV 用的（且平局腿取的是
  **市场先验**，因为 ``reasoning_factors`` 里根本没有 ``draw_ml_true_prob``）

``ml_proba`` 在强弱悬殊的场次会**塌方**（本次 162 场里 14 场 max>0.90，德甲 18 场里
12 场与 ``probs`` 差 >0.15）。被当成模型概率后，后果是三重的：

1. 卡片概率条与卡片**自己推荐的 direction** 自相矛盾（direction 来自 ``probs``）；
2. ``edge_prob`` / ``ev_per_unit`` 全是**假账** —— 卡片说主胜「每投 1 元长期多赚 0.33 元」，
   而决策层用**同一个模型**算出主胜 EV = **−0.159**（要亏）；
3. 卡片让买主胜、推荐单让买客胜 —— 同一页面上两个面板互相打脸。

口径纪律（本模块是唯一实现，调用方一律走这里）
----------------------------------------------
1. **模型概率** = ``probs.{h,d,a}``（融合后最终值；也是 ``direction`` / ``stars`` /
   ``confidence_score`` 的唯一来源）。
2. 旧预测文件没有 ``probs`` 时，回退 ``reasoning_factors.{home,draw,away}_prob_weighted``
   （同为模型输出，只是 3 位精度）。
3. **绝不**用 ``home_true_prob`` / ``draw_true_prob`` / ``away_true_prob``
   —— 那是 ESPN 赔率去水后的**市场先验**，不是模型输出（拿它算 EV / CLV 是循环论证）。
4. **绝不**用 ``ml_proba``（ML 分类器原始输出，未融合）。
5. 两者都没有 ⇒ 返回 ``None``（不拿别的数顶替；调用方据此显示「未知」，**不是 0**）。

本模块是**纯函数**，无 IO、无 DB 依赖 —— scripts 侧（归档 / 决策层）与 web 侧
（API / 串关 / 票面）共用同一份实现，杜绝口径漂移。
"""
from __future__ import annotations

import math
from typing import Any

# ``probs`` 的键 → 展示侧键名
_SIDE_KEYS = ("h", "d", "a")
_PROBS_PREFIX = {"h": "home", "d": "draw", "a": "away"}

# 融合后概率写入预测文件时按 **4 位小数**四舍五入（``predictor._assemble_result``），
# 三项求和的舍入误差上界 ≈ 1.5e-4 ⇒ 容差必须 ≥ 1e-3。
# ⚠️ 旧实现在 web 层用 1e-6 ⇒ **61%（99/162）的真实场次被误判为「非法向量」**，
# ``edge_prob`` / ``ev_per_unit`` 整体缺失 ⇒ 卡片静默不显示「比庄家高/低 X 个点」，
# 用户看不出缺了什么。**任何“没给数”都必须说清楚为什么，不能静默消失。**
DEFAULT_SUM_TOL = 1e-3


def as_prob(v: Any) -> float | None:
    """数值校验：bool / 非数值 / 非有限 / 越界 ⇒ None（不猜、不补默认值）。"""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        return None
    return f


def resolve_prob(rec: dict | None, code: str) -> float | None:
    """取**模型自己的**某一侧概率（``code`` ∈ ``'h'`` / ``'d'`` / ``'a'``）。

    读取顺序见模块 docstring 的「口径纪律」1~2 条。取不到返回 ``None``。
    """
    if not isinstance(rec, dict):
        return None
    prefix = _PROBS_PREFIX.get(code)
    if prefix is None:
        return None
    probs = rec.get("probs")
    if isinstance(probs, dict):
        f = as_prob(probs.get(code))
        if f is not None:
            return f
    rf = rec.get("reasoning_factors")
    if isinstance(rf, dict):
        return as_prob(rf.get(f"{prefix}_prob_weighted"))
    return None


def resolve_model_probs(rec: dict | None, *, tol: float = DEFAULT_SUM_TOL) -> dict[str, float] | None:
    """返回 ``{"home": .., "draw": .., "away": ..}``，或 ``None``（表示「未知」）。

    读取顺序：
      ① ``rec["model_probs"]``（``{home,draw,away}`` 完整向量，web API 自己产出的形态）；
      ② ``probs.{h,d,a}``（融合后最终值）；
      ③ ``reasoning_factors.{home,draw,away}_prob_weighted``（旧文件回退）。

    ``tol``：三项求和与 1.0 的允许偏差（默认 ``1e-3``，见 :data:`DEFAULT_SUM_TOL`）。

    ``None`` 的含义是**「不知道」**，不是「概率为 0」—— 调用方必须区分这两者
    （页面要显示「这场还没算出赢面」，而不是「赢面 0%」）。
    """
    if not isinstance(rec, dict):
        return None
    mp = rec.get("model_probs")
    if isinstance(mp, (list, tuple)) and len(mp) == 3:
        vals: dict[str, float] = {}
        for code, raw in zip(_SIDE_KEYS, mp):
            v = as_prob(raw)
            if v is None:
                vals = {}
                break
            vals[_PROBS_PREFIX[code]] = v
        if vals and abs(sum(vals.values()) - 1.0) <= tol:
            return vals
    if isinstance(mp, dict) and set(mp) == {"home", "draw", "away"}:
        vals: dict[str, float] = {}
        for key in ("home", "draw", "away"):
            v = as_prob(mp.get(key))
            if v is None:
                vals = {}
                break
            vals[key] = v
        if vals and abs(sum(vals.values()) - 1.0) <= tol:
            return vals
    vals = {}
    for code in _SIDE_KEYS:
        v = resolve_prob(rec, code)
        if v is None:
            return None
        vals[_PROBS_PREFIX[code]] = v
    if abs(sum(vals.values()) - 1.0) > tol:
        return None
    return vals


__all__ = [
    "DEFAULT_SUM_TOL",
    "as_prob",
    "resolve_model_probs",
    "resolve_prob",
]
