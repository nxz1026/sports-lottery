"""``core.ml_feedback`` — 预测 / 实际结果 关联日志（ML 训练数据保留）。

P-6 (2026-10-13) 引入。**目标**：为后续提升预测准确率保留结构化数据。

数据流：
  1. ``predict.py::_save_output`` 写 prediction JSON → 调
     ``record_prediction(...)`` → events.jsonl 写一行 ``kind=prediction``
  2. ``output.py::save_results`` 写 results JSON → 调
     ``record_outcome(...)`` → events.jsonl 写一行 ``kind=outcome``
  3. ``join_predictions_outcomes()`` 读 events.jsonl + 按 ``match_key``
     (home_en + away_en + kickoff_utc 三元组) 关联，返回每场的
     ``(probs, score, ...)`` 训练样本

设计要点：
- 全部 JSON-serializable 字段：可直接 pandas / polars 加载
- ``match_key`` 与 ``output.py::save_results`` 现有的 ``f"{home}_{away}"`` 兼容
  ；额外带 kickoff_utc 防同日两场同对阵
- 错误处理：写 events 失败不能影响主流程（吞 OSError 写 logger.warning）
- 不直接 import observability 但共用同一份 ``log_event`` 入口 —— 写 events.jsonl
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import core.observability as _obs

logger = logging.getLogger("predict")


def _stable_match_key(home: str, away: str, kickoff_utc: str) -> str:
    """``output.py::save_results`` 现有 ``f"{home}_{away}"`` 的扩展（加 kickoff 防同日二番战）。"""
    return f"{home}_{away}__{kickoff_utc}"


def record_prediction(
    *,
    league: str,
    match_id: str,
    home: str,
    away: str,
    kickoff_utc: str,
    probs: dict[str, float],
    model: str = "default",
    goal_line: float | None = None,
    market_signal: dict[str, Any] | None = None,
    features_snapshot: dict[str, float] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """写 ``events.jsonl`` 一行 ``kind=prediction``。

    ``probs`` 期望 ``{"h": 0.5, "d": 0.3, "a": 0.2}``（主胜/平/客胜三向）。
    ``model`` 是模型版本号或名（ML / Dixon-Coles / baseline / ai-adjusted 等），
    后续 ML 训练可按 model 切片分析各模型贡献。
    ``features_snapshot``（Round 11 E1）：26 维特征向量快照，给未来
    ``retrain_baseline.train_league_model_from_events`` 直接消费。
    注意：仅 ``dict[str, float]``；key 不必严格等于 ``FEATURE_COLUMNS``，
    但下游 ``train_league_model_from_events`` 会按 key 对齐索引，所以
    缺字段时该样本被跳过（旧 events 无该字段→calibration 仍可用）。
    ``extra`` 允许塞额外非数值字段（置信度 / 解释等）。
    """
    fields: dict[str, Any] = {
        "league": league,
        "match_id": match_id,
        "home": home,
        "away": away,
        "kickoff_utc": kickoff_utc,
        "match_key": _stable_match_key(home, away, kickoff_utc),
        "probs": probs,
        "model": model,
    }
    if goal_line is not None:
        fields["goal_line"] = goal_line
    if market_signal is not None:
        fields["market_signal"] = market_signal
    if features_snapshot:
        fields["features_snapshot"] = features_snapshot
    if extra:
        fields["extra"] = extra
    _safe_log_event("prediction", **fields)


def record_outcome(
    *,
    league: str,
    match_id: str,
    home: str,
    away: str,
    kickoff_utc: str,
    home_score: int,
    away_score: int,
    status: str = "FT",
    extra: dict[str, Any] | None = None,
) -> None:
    """写 ``events.jsonl`` 一行 ``kind=outcome``。"""
    fields: dict[str, Any] = {
        "league": league,
        "match_id": match_id,
        "home": home,
        "away": away,
        "kickoff_utc": kickoff_utc,
        "match_key": _stable_match_key(home, away, kickoff_utc),
        "home_score": int(home_score),
        "away_score": int(away_score),
        "total_goals": int(home_score) + int(away_score),
        "goal_diff": int(home_score) - int(away_score),
        "result": _result_label(int(home_score), int(away_score)),
        "status": status,
    }
    if extra:
        fields["extra"] = extra
    _safe_log_event("outcome", **fields)


def _result_label(home: int, away: int) -> str:
    if home > away:
        return "home_win"
    if home < away:
        return "away_win"
    return "draw"


def _safe_log_event(kind: str, **fields: Any) -> None:
    """写 events.jsonl 失败不能影响主流程（吞 OSError 写 logger.warning）。"""
    try:
        _obs.log_event(kind, **fields)
    except OSError as e:
        logger.warning("ml_feedback log_event(%s) failed: %s", kind, e)


# ── 训练数据组装（给 ML 反馈 / 训练管线）────────────────
def join_predictions_outcomes(
    *,
    league: str | None = None,
    since_ts: str | None = None,
    events_path: Path | None = None,
) -> list[dict[str, Any]]:
    """读 events.jsonl，按 ``match_key`` 关联 prediction ↔ outcome，返回训练样本。

    每条样本形如：
    ``{"match_key": ..., "league": ..., "home": ..., "away": ..., "kickoff_utc": ...,
       "probs": {...}, "model": ..., "home_score": N, "away_score": N, "result": "...",
       "pred_ts": "...", "settle_ts": "..."}``

    缺 outcome 的 prediction（尚未结算）→ 不返回（保证每条都是 labeled）。
    """
    preds: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}

    for ev in _obs.iter_events(since_ts=since_ts, path=events_path):
        if league and ev.get("league") != league:
            continue
        kind = ev.get("kind")
        key = ev.get("match_key")
        if not key:
            continue
        if kind == "prediction":
            # 同一 match_key 可能被多次预测（同日重复跑）—— 用最新覆盖
            ts = ev.get("ts", "")
            if key not in preds or ts > preds[key].get("ts", ""):
                preds[key] = ev
        elif kind == "outcome":
            ts = ev.get("ts", "")
            if key not in outcomes or ts > outcomes[key].get("ts", ""):
                outcomes[key] = ev

    samples: list[dict[str, Any]] = []
    for key, p in preds.items():
        o = outcomes.get(key)
        if not o:
            continue  # 未结算：不算入 labeled
        samples.append({
            "match_key": key,
            "match_id": p.get("match_id"),
            "league": p.get("league"),
            "home": p.get("home"),
            "away": p.get("away"),
            "kickoff_utc": p.get("kickoff_utc"),
            "probs": p.get("probs", {}),
            "model": p.get("model", "default"),
            "goal_line": p.get("goal_line"),
            "market_signal": p.get("market_signal"),
            # E1 (Round 11): 透传 features_snapshot；后续 ML 重训直接消费。
            # 不存则 None（dict key 也可能不存在 — tests 用 in 判断）。
            "features_snapshot": p.get("features_snapshot"),
            "home_score": o.get("home_score"),
            "away_score": o.get("away_score"),
            "total_goals": o.get("total_goals"),
            "goal_diff": o.get("goal_diff"),
            "result": o.get("result"),
            "status": o.get("status"),
            "pred_ts": p.get("ts"),
            "settle_ts": o.get("ts"),
        })
    return samples


__all__ = [
    "record_prediction",
    "record_outcome",
    "join_predictions_outcomes",
    "_stable_match_key",
]
