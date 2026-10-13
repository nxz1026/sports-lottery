"""Retrain baseline — calibration over stored ``probs`` from ml_feedback events。

设计目标：
- **不重跑整条训练流水线**。H1+H2 落地后 ``events.jsonl`` 落盘了每场的
  ``probs``（H/D/A 概率）与 ``outcome``（H/D/A 标 label + 比分）。
- 本模块只做**概率校准评估**：对比 stored ``probs`` 与一致假设（1/3, 1/3, 1/3）
  在 Brier score 上的差距，给"数据到底值多少"做一个 0 依赖的可验证基线。
- 真正的 ML retrain（用 ``core.model.ml_model.MatchMLModel`` 重新训练）需要
  把 26 维特征向量也落盘到 ``events.jsonl``（见 Round 8 H10）—— 那一档推
  下一轮，本轮只把"评估闭环"打通。

核心 API：
- ``load_joined_samples(events_jsonl_path, league=, since_ts=)`` —— 已 join
  prediction ↔ outcome 的训练样本列表（来自 ``ml_feedback.join_predictions_outcomes``）
- ``evaluate_baseline(samples)`` —— 返回 ``n / brier / naive_brier / accuracy /
  improvement_pct / per_outcome_accuracy``
- ``format_report(metrics)`` —— 格式化人类可读摘要
- ``run(events_jsonl_path, league=, since_ts=)`` —— 一键装载+评估+打印

CLI 入口：
    python -m core.retrain_baseline [--events PATH] [--league LEAGUE] [--since ISO]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# 与 ml_feedback.record_prediction probs 字段对齐（h/d/a 三键浮点）
_PROB_KEYS = ("h", "d", "a")
# ml_feedback._result_label 返回 ``home_win`` / ``away_win`` / ``draw``，本模块
# 内部统一归一为单字母 H/D/A 给评估指标用；映射在函数入口完成。
_RESULT_LABEL_MAP = {"home_win": "H", "away_win": "A", "draw": "D"}
_RESULT_TO_INDEX = {"H": 0, "D": 1, "A": 2}
_INDEX_TO_KEY = {0: "h", 1: "d", 2: "a"}


@dataclass
class BaselineMetrics:
    """评估指标集合。"""
    n: int = 0
    brier: float | None = None  # multi-class Brier（sum）, ∈ [0, 2]
    naive_brier: float | None = None  # 均匀 (1/3,1/3,1/3) 同口径 Brier
    accuracy: float | None = None  # 顶级预测胜率
    naive_accuracy: float | None = None  # 顶级选最多场次的标签胜率（赛道模式）
    improvement_pct: float | None = None  # (1 - brier/naive_brier) × 100，正=占优
    per_outcome: dict[str, dict[str, float]] = field(default_factory=dict)
    model_versions: dict[str, int] = field(default_factory=dict)


def _normalize_result_label(raw: str) -> str | None:
    """把 ml_feedback ``home_win``/``away_win``/``draw`` 归一到 H/D/A。

    透传 H/D/A（旧 schema 兼容）；未识别返回 None（评估时跳过）。
    """
    if raw in _RESULT_TO_INDEX:
        return raw
    return _RESULT_LABEL_MAP.get(raw)


def _brier(probs: dict[str, float], outcome: str) -> float:
    """Multi-class Brier（sum across h/d/a）→ ∈ [0, 2]，0=完美.

    均匀分布 (1/3, 1/3, 1/3) 给任一 one-hot 真实值的 Brier = 2/3.
    """
    actual_idx = _RESULT_TO_INDEX[outcome]
    s = 0.0
    for i, k in enumerate(_PROB_KEYS):
        a = 1.0 if i == actual_idx else 0.0
        s += (probs[k] - a) ** 2
    return s


def _top_label(probs: dict[str, float]) -> str:
    """argmax over (h, d, a) → label letter H/D/A."""
    best_k = max(_PROB_KEYS, key=lambda k: probs[k])
    return _INDEX_TO_KEY[_PROB_KEYS.index(best_k)].upper()


def _naive_top_label(samples: list[dict], *, _normalize=None) -> str:
    """始终选实际最频繁的标签作为对照基线（"众数基线"）。"""
    counts = {"H": 0, "D": 0, "A": 0}
    normalize = _normalize or (lambda s: s["result"])
    for s in samples:
        lbl = normalize(s)
        if lbl in counts:
            counts[lbl] += 1
    return max(counts, key=counts.get)


def evaluate_baseline(samples: list[dict]) -> BaselineMetrics:
    """对比 stored probs 与 1/3 均匀分布 + "always pick majority" 双基线.

    ``samples`` schema 与 ``ml_feedback.join_predictions_outcomes`` 返回一致
    （必含 ``probs`` / ``result`` 字段，其他字段透传不消费）。
    """
    if not samples:
        return BaselineMetrics(n=0)

    brier_sum = 0.0
    naive_brier_sum = 0.0
    correct = 0
    naive_correct = 0
    naive_label = _naive_top_label(
        samples, _normalize=lambda s: _normalize_result_label(s.get("result", "")) or "H"
    )

    per_outcome = {"H": {"n": 0, "n_pred": 0},
                   "D": {"n": 0, "n_pred": 0},
                   "A": {"n": 0, "n_pred": 0}}
    model_versions: dict[str, int] = {}

    for s in samples:
        outcome = _normalize_result_label(s.get("result", ""))
        probs = s["probs"]  # {"h", "d", "a"}
        if outcome is None or outcome not in _RESULT_TO_INDEX:
            continue  # 跳过未识别的 outcome label（如未来扩展 OT/PK）

        brier_sum += _brier(probs, outcome)
        naive_brier_sum += _brier({"h": 1 / 3, "d": 1 / 3, "a": 1 / 3}, outcome)

        pred = _top_label(probs)
        if pred == outcome:
            correct += 1
        per_outcome[outcome]["n"] += 1
        per_outcome[outcome]["n_pred"] += int(pred == outcome)

        if naive_label == outcome:
            naive_correct += 1

        mv = s.get("model", "unknown")
        model_versions[mv] = model_versions.get(mv, 0) + 1

    n = len(samples)
    metrics = BaselineMetrics(
        n=n,
        brier=brier_sum / n,
        naive_brier=naive_brier_sum / n,
        accuracy=correct / n,
        naive_accuracy=naive_correct / n,
        per_outcome={k: {"n": v["n"], "accuracy": v["n_pred"] / v["n"] if v["n"] else 0.0}
                     for k, v in per_outcome.items() if v["n"] > 0},
        model_versions=model_versions,
    )
    if metrics.naive_brier and metrics.naive_brier > 0:
        metrics.improvement_pct = (1 - metrics.brier / metrics.naive_brier) * 100
    return metrics


def load_joined_samples(events_jsonl_path: Path | str | None = None,
                        league: str | None = None,
                        since_ts: str | None = None) -> list[dict]:
    """从 ``events.jsonl`` 装载 join 后样本。委托给 ``ml_feedback``.

    ``events_jsonl_path`` 缺省时走 ``core.observability.events_jsonl_path()``。
    """
    from core import ml_feedback  # noqa: PLC0415 — 运行时延迟导入

    if events_jsonl_path is None:
        return ml_feedback.join_predictions_outcomes(league=league, since_ts=since_ts)
    return ml_feedback.join_predictions_outcomes(
        events_jsonl_path=Path(events_jsonl_path),
        league=league, since_ts=since_ts,
    )


def format_report(metrics: BaselineMetrics) -> str:
    """人类可读摘要（中英混排，10 行内）。"""
    if metrics.n == 0:
        return "[retrain] no joined samples — events.jsonl 中无 prediction↔outcome 配对"

    lines = [
        f"[retrain] n={metrics.n} pairs",
        f"  brier:          {metrics.brier:.4f}  (均匀基线 {metrics.naive_brier:.4f})",
        f"  accuracy:       {metrics.accuracy:.4f}  (众数基线 {metrics.naive_accuracy:.4f})",
        f"  改进:           {metrics.improvement_pct:+.2f}%  vs 均匀（正=占优）",
    ]
    if metrics.per_outcome:
        lines.append("  per outcome:")
        for outcome in ("H", "D", "A"):
            if outcome in metrics.per_outcome:
                v = metrics.per_outcome[outcome]
                lines.append(f"    {outcome}: acc={v['accuracy']:.3f}  n={int(v['n'])}")
    if metrics.model_versions:
        lines.append("  models: " + ", ".join(
            f"{k}={v}" for k, v in sorted(metrics.model_versions.items())
        ))
    return "\n".join(lines)


def run(events_jsonl_path: Path | str | None = None,
        league: str | None = None,
        since_ts: str | None = None,
        *, verbose: bool = True) -> BaselineMetrics:
    """一键装载 + 评估 + 打印/返回。"""
    samples = load_joined_samples(events_jsonl_path, league=league, since_ts=since_ts)
    metrics = evaluate_baseline(samples)
    if verbose:
        print(format_report(metrics))
    return metrics


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M-2 retrain baseline — 用 ml_feedback joined 样本评估 probs Brier/accuracy")
    parser.add_argument("--events", type=Path, default=None,
                        help="events.jsonl 路径（缺省走 observability 默认路径）")
    parser.add_argument("--league", type=str, default=None, help="按联赛过滤（如 epl/laliga）")
    parser.add_argument("--since", type=str, default=None, help="ISO 时间戳，过滤此后的预测")
    args = parser.parse_args(argv)

    try:
        run(events_jsonl_path=args.events, league=args.league, since_ts=args.since)
    except FileNotFoundError as exc:
        print(f"[retrain] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
