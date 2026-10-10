"""Web 侧命中率计算：纯函数，只读 web/services/store 已加载的预测/赛果 JSON。

引擎侧 ``scripts/core/backtest.league_accuracy`` 同期存在（v4.x 引擎内核
路径使用）；web 端按契约 §5 不允许 import scripts/，因此在本文件复制一份
**完全等价的实现**，调用方 ``web/routers/jc.py::real_accuracy`` 与
``web/routers/predictions.py::league_accuracy_view`` 用本实现替换原 lazy
``from core.backtest import league_accuracy`` 引用。

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