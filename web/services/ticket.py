"""传统足彩（14场/任九）三向概率票面（纯函数：无 DB/IO/网络）。

被 P0-COLLECT2 解开：fact.jc_issue_match 现在有真实对阵，本模块把每场三向（胜/平/负）
模型概率输入 → 输出「逐场推荐票面」+「命中概率」。

口径（≤框架红线，不编数）：
  · 每场只认三种结果：主胜/平/客胜（sfc 玩法无让球，官方是 "3"=胜 /"1"=平/"0"=负）。
  · 不做金额/奖金（官方动态奖池，parse_jcissue 只存原值字符串，不编数字）。
  · probs 必须 Σ≈1（三向归一），违反 → ValueError，不许静默归一。
  · 穿管概率 = 独立假设下 P(全部命中)（与 combo 同假设，注明独立假设）。
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Mapping, Sequence

_KEYS = ("主胜", "平", "客胜")
_SUM_TOL = 1e-6


@lru_cache(maxsize=None)
def _C(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def _norm_probs(probs: Mapping[str, float]) -> tuple[float, float, float]:
    """三向概率校验并返回 (主胜,平,客胜) 元组；非法 → ValueError。"""
    if not isinstance(probs, Mapping):
        raise ValueError(f"probs 必须为 Mapping，得到 {probs!r}")
    missing = [k for k in _KEYS if k not in probs]
    if missing:
        raise ValueError(f"probs 缺三向键 {missing}（须含 主胜/平/客胜）")
    vals = []
    for k in _KEYS:
        p = probs[k]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError(f"probs[{k!r}]={probs[k]!r} 非法：须为 [0,1] 内数值")
        vals.append(float(p))
    if abs(math.fsum(vals) - 1.0) > _SUM_TOL:
        raise ValueError(f"probs 三向概率和 {math.fsum(vals)!r} 偏离 1 超过 {_SUM_TOL}：未归一不得做票面")
    return vals[0], vals[1], vals[2]


def pick_side(probs: Mapping[str, float]) -> dict:
    """单场三向：最大概率侧 = 推荐结果；返回 {side, prob, probs} 或全 0 时 ValueError。"""
    p_h, p_d, p_a = _norm_probs(probs)
    side = max(_KEYS, key=lambda k: probs[k])
    if probs[side] <= 0:
        raise ValueError("三向概率全 0，无从推荐")
    return {"side": side, "prob": probs[side], "probs": {k: probs[k] for k in _KEYS}}


def sfc_picks(matches: Sequence[Mapping]) -> list[dict]:
    """14场比赛 → 逐场推荐票面。每项须含 {home, away, probs}；缺 probs 项的跳过并标注 'no_probs'。

    返回与输入同序；有 probs 的给推荐侧，缺失的侧/概率为 None（不编数、不猜）。
    """
    rows = []
    for i, m in enumerate(matches, 1):
        home = m.get("home") or m.get("home_cn") or ""
        away = m.get("away") or m.get("away_cn") or ""
        probs_raw = m.get("probs")
        base = {"seq": i, "home": home, "away": away}
        if not probs_raw:
            rows.append({**base, "side": None, "prob": None, "error": "no_probs"})
            continue
        try:
            pick = pick_side(probs_raw)
            rows.append({**base, **pick, "error": None})
        except ValueError as exc:
            rows.append({**base, "side": None, "prob": None, "error": str(exc)})
    return rows


def parallel_prob(probs_list: Sequence[Mapping]) -> float:
    """独立假设下各场推荐侧同时命中的联合概率（pipeline 的 P(全对)）。空 → ValueError。"""
    if not probs_list:
        raise ValueError("parallel_prob 收到空列表")
    log_total = 0.0
    for m in probs_list:
        p = m.get("prob")
        if not isinstance(p, (int, float)) or math.isnan(p) or p <= 0:
            raise ValueError(f"parallel_prob 含非法 prob={p!r}（须 >0 的数值）")
        log_total += math.log(float(p))
    return math.exp(log_total)


def choose_combinations(n: int, k: int) -> int:
    """任九：从 n 场选 k 场 → C(n,k) 组合数（用于说明票面规模，实际出票走胆拖工具）。"""
    if not isinstance(n, int) or not isinstance(k, int) or n < 0 or k < 0:
        raise ValueError("choose_combinations 参数必须为非负整数")
    return _C(n, k)


def combine_prob(matches: Sequence[Mapping], choose: int) -> dict:
    """14场里选 choose 场（如任九=9）的组合命中概率。

    独立假设：单场命中的概率是「该场推荐侧模型概率」。任九的中奖口径是所选 9 场全对
    （官方任九=任选9场全中）。此处返回选 9 场的推荐组合，命中概率 = 该组合各场概率连乘。
    """
    if not matches:
        raise ValueError("combine_prob 收到空列表")
    n = len(matches)
    if choose <= 0 or choose > n:
        raise ValueError(f"choose={choose} 非法：须在 [1, {n}]")
    picked = sorted(range(n), key=lambda i: -(matches[i].get("prob") or 0.0))[:choose]
    chosen = [matches[i] for i in picked]
    p = 1.0
    for m in chosen:
        pv = m.get("prob")
        if not isinstance(pv, (int, float)) or math.isnan(pv) or pv <= 0:
            raise ValueError(f"任九组合含非法 prob={pv!r}")
        p *= float(pv)
    return {
        "n_matches": n,
        "choose": choose,
        "combinations": choose_combinations(n, choose),
        "picked_seqs": sorted(i + 1 for i in picked),
        "hit_prob": round(p, 6),
        "independent_assumption": True,
    }