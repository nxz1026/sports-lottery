"""Web 侧命中率计算：纯函数，只读 web/services/store 已加载的预测/赛果 JSON。

引擎侧 ``scripts/core/backtest.league_accuracy`` 同期存在（v4.x 引擎内核
路径使用）；web 端按契约 §5 不允许 import scripts/，因此在本文件复制一份
**完全等价的实现**，调用方 ``web/routers/predictions.py::_live_accuracy``
消费本实现（已替换原 lazy ``from core.backtest import league_accuracy`` 引用）。

等价性由两个独立断言守住：
- 键：``web/services/store._stable_match_key`` == ``core/backtest._bk_stable_key``，
  tests/test_stable_match_key_mirror.py 已锁。
- 算法：本文件 ``league_accuracy_web`` 是 ``scripts/core/backtest.league_accuracy``
  算法 1:1 复制（方向 / 比分 / 大小球），仅换掉 JSON 加载路径
  （PREDICTIONS_DIR.glob → load_prediction_docs）。

保持对称：任何对 ``league_accuracy`` 的算法改动需同步两份（CI 无强制，
PR review 守门；详见 README §5 备注）。
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from web.services.store import _stable_match_key, load_prediction_docs


def _collect_league_data(
    league_key: str, cutoff: float
) -> tuple[dict[str, tuple[int, int]], list[dict[str, Any]]]:
    """web 版的 _bk_collect_league_data：从 web 自己的 loader 读预测文件。

    返回 ``(actuals, preds)``：
    - actuals: 稳定键 → (home_goals, away_goals)
    - preds: 历史 predictions 列表（同一联赛）
    """
    actuals: dict[str, tuple[int, int]] = {}
    preds: list[dict[str, Any]] = []
    seen_pred: set[str] = set()

    for doc in load_prediction_docs():
        # doc 是 {"path": Path, "data": dict, "mtime": float}
        if doc.get("mtime", 0) < cutoff:
            continue
        data = doc.get("data") or {}
        if not isinstance(data, dict):
            continue
        if data.get("league") != league_key:
            continue
        # past_matches 含已结算比赛 → actuals
        for m in data.get("past_matches") or []:
            if not isinstance(m, dict):
                continue
            key = _stable_match_key(m)
            score = str(m.get("score") or "")
            if key and "-" in score:
                try:
                    h, a = score.split("-")[:2]
                    actuals[key] = (int(h), int(a))
                except ValueError:
                    continue
        # predictions → preds（按预测键去重，保留首次）
        for p in data.get("predictions") or []:
            if not isinstance(p, dict):
                continue
            key = _stable_match_key(p)
            if not key or key in seen_pred:
                continue
            seen_pred.add(key)
            preds.append(p)
    return actuals, preds


def league_accuracy_web(league_key: str, days: int = 7) -> dict[str, Any] | None:
    """按联赛统计最近 days 天预测的方向/比分/大小球命中率（web 侧）。

    算法与 ``scripts/core/backtest.league_accuracy`` 等价；web 端按契约 §5
    不允许 import scripts/，故在此实现独立版本，键语义通过双侧镜像测试守住
    （tests/test_stable_match_key_mirror.py）。
    返回 None 表示窗口内没有已结算预测样本（页面显示"数据积累中"，不编数）。
    """
    cutoff = time.time() - days * 86400
    actuals, preds = _collect_league_data(league_key, cutoff)

    if not preds:
        return None

    correct_dir = correct_score = correct_ou = total = 0
    for p in preds:
        r = actuals.get(_stable_match_key(p))
        if not r:
            continue
        h_act, a_act = r
        total += 1
        d = p.get("direction", "")
        home_team = p.get("home", "")
        away_team = p.get("away", "")
        is_draw = "平" in d or "平局" in d
        is_home_win = is_away_win = False
        if not is_draw:
            if d.startswith(home_team):
                is_home_win = True
            elif d.startswith(away_team):
                is_away_win = True
        if (is_home_win and h_act > a_act) or (is_draw and h_act == a_act) or (is_away_win and h_act < a_act):
            correct_dir += 1
        if p.get("predicted_score", "") == f"{h_act}-{a_act}":
            correct_score += 1
        ou = p.get("over_under", "")
        if "Over" in ou and h_act + a_act > 2.5:
            correct_ou += 1
        elif "Under" in ou and h_act + a_act < 2.5:
            correct_ou += 1

    if total == 0:
        return None
    return {
        "window_days": days,
        "reconciled": total,
        "direction_accuracy": round(correct_dir / total, 3),
        "score_accuracy": round(correct_score / total, 3),
        "over_under_accuracy": round(correct_ou / total, 3),
    }


# ─────────────────────────────────────────────────────────────
# 建议 3 子项 ①：分层命中率 + 样本量 + 置信区间
#
# 为什么不能只给一个单值（路线图 §1.2）：
#   n=18 时 0.389 与 0.667 的 95% 区间宽度约 ±0.23 ⇒ 五个联赛的差异统计上不可区分。
#   把它当卖点展示是**误导而不是价值**。
# ⇒ 任何展示出来的比率都必须带上 n 与区间；n 太小还要明说「样本不足」。
#
# 区间用 **Wilson score interval**（不是正态近似）：
#   n 小或 p̂ 接近 0/1 时正态近似会给出越界区间；Wilson 在 p̂=0、n=1 时仍给出
#   一个合法的 [0, 上界]，且恒落在 [0,1] 内。
# ─────────────────────────────────────────────────────────────

Z_95 = 1.959963984540054  # 双侧 95% 正态分位数


def wilson_interval(hits: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """命中数 → Wilson 95% 置信区间 ``(lo, hi)``。

    - ``n == 0`` → ``(0.0, 1.0)``（无样本 ⇒ 不给假区间，直接说「不知道」）；
    - 结果恒在 ``[0, 1]`` 内（Wilson 的定义保证，不会像正态近似那样越界）。
    """
    if n <= 0:
        return 0.0, 1.0
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


_STAR_RE = re.compile(r"(\d+)\s*-?\s*star", re.IGNORECASE)


def _stars_of(pred: dict) -> int:
    """星级 → 整数（'2-star' → 2；缺失/无法解析 → 0）。"""
    m = _STAR_RE.search(str(pred.get("stars") or ""))
    return int(m.group(1)) if m else 0


def _conf_band(pred: dict) -> str:
    """置信度分档：<0.4 / 0.4~0.6 / >0.6；缺失 → '未知'。"""
    v = pred.get("confidence_score")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "未知"
    if v < 0.4:
        return "<0.4"
    if v <= 0.6:
        return "0.4-0.6"
    return ">0.6"


def _direction_hit(pred: dict, h_act: int, a_act: int) -> bool:
    """方向是否命中（与 ``league_accuracy_web`` 的判定逐字一致）。"""
    d = pred.get("direction", "")
    home_team = pred.get("home", "")
    away_team = pred.get("away", "")
    is_draw = "平" in d or "平局" in d
    is_home_win = is_away_win = False
    if not is_draw:
        if d.startswith(home_team):
            is_home_win = True
        elif d.startswith(away_team):
            is_away_win = True
    return bool((is_home_win and h_act > a_act) or (is_draw and h_act == a_act)
                or (is_away_win and h_act < a_act))


def _resolve_samples(league_key: str, days: int) -> list[tuple[dict, tuple[int, int]]]:
    """已结算样本：``[(预测行, (主进球, 客进球))]``（对不上的丢弃）。"""
    cutoff = time.time() - days * 86400
    actuals, preds = _collect_league_data(league_key, cutoff)
    out: list[tuple[dict, tuple[int, int]]] = []
    for p in preds:
        r = actuals.get(_stable_match_key(p))
        if r:
            out.append((p, r))
    return out


def league_accuracy_stratified(league_key: str, days: int = 30) -> dict[str, Any] | None:
    """分层命中率 + 样本量 + 置信区间（建议 3 子项 ①）。

    返回结构::

        {
          "window_days": 30,
          "reconciled": 18,                 # 已结算样本总数
          "direction_accuracy": 0.556,
          "direction_ci95": [0.33, 0.76],  # 总体的 Wilson 区间
          "by_stars":   {"1": {...}, "2": {...}, ...},
          "by_confidence": {"<0.4": {...}, "0.4-0.6": {...}, ">0.6": {...}, "未知": {...}},
        }

    每层给 ``n`` / ``hits`` / ``rate`` / ``ci95``；``n < 5`` 时额外给
    ``insufficient: true``（**不隐藏数字，但明说样本不足**）。
    窗口内没有已结算样本时返回 ``None``（页面显示「数据积累中」，不编数）。
    """
    samples = _resolve_samples(league_key, days)
    if not samples:
        return None

    def _layer(items: list[tuple[dict, tuple[int, int]]]) -> dict[str, Any]:
        n = len(items)
        hits = sum(1 for p, (h, a) in items if _direction_hit(p, h, a))
        lo, hi = wilson_interval(hits, n)
        out: dict[str, Any] = {
            "n": n,
            "hits": hits,
            "rate": round(hits / n, 3) if n else None,
            "ci95": [round(lo, 3), round(hi, 3)],
        }
        if n < 5:
            out["insufficient"] = True
        return out

    total_hits = sum(1 for p, (h, a) in samples if _direction_hit(p, h, a))
    lo, hi = wilson_interval(total_hits, len(samples))

    by_stars: dict[str, list] = {}
    by_conf: dict[str, list] = {}
    for p, r in samples:
        by_stars.setdefault(str(_stars_of(p)), []).append((p, r))
        by_conf.setdefault(_conf_band(p), []).append((p, r))

    return {
        "window_days": days,
        "reconciled": len(samples),
        "direction_accuracy": round(total_hits / len(samples), 3),
        "direction_ci95": [round(lo, 3), round(hi, 3)],
        "by_stars": {k: _layer(v) for k, v in sorted(by_stars.items(), key=lambda kv: int(kv[0]))},
        "by_confidence": {k: _layer(v) for k, v in sorted(by_conf.items())},
    }
