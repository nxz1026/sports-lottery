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
  prediction <-> outcome 的训练样本列表（来自 ``ml_feedback.join_predictions_outcomes``）
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


# Reliability 分桶（Brier 单值无法定位"哪段 prob 区间有问题"）—— Round 11 F1
@dataclass
class ReliabilityBin:
    """一个 prob 桶的 reliability 摘要。Wilson 95% CI（比正态近似小样本更稳）。"""
    pred_lo: float          # 桶下界 [0,1)
    pred_hi: float          # 桶上界 (0,1]
    n: int                  # 桶内样本数
    mean_pred: float        # 桶内 mean(top_prob)
    actual_rate: float      # 桶内顶级预测胜率（实际）
    ci_lo: float            # Wilson 95% 下界
    ci_hi: float            # Wilson 95% 上界


@dataclass
class ReliabilityMetrics:
    bins: list[ReliabilityBin] = field(default_factory=list)
    ece: float = 0.0        # Expected Calibration Error（按 n 加权 |mean_pred - actual_rate|）
    n_total: int = 0


def reliability_diagram(samples: list[dict], n_bins: int = 10) -> ReliabilityMetrics:
    """F1 (Round 11) Reliability diagram：把 samples 按顶级预测 prob 排序分桶，
    每桶计算 Wilson 95% CI 的实际胜率。

    输出形态：
    - ``bins``：每桶 ``ReliabilityBin``（mean_pred / actual_rate / ci_lo / ci_hi / n）
    - ``ece``：Expected Calibration Error（全桶 sum(|mean_pred - actual_rate|) × n / total）

    设计要点：
    - ``top_prob = max(s["probs"].values())``（即最高置信度那条的概率）
    - ``actual = 1.0 if _top_label(probs) == outcome else 0.0``（顶级赢没赢）
    - Wilson CI 公式： ``p̂ ± z²/(2n) ± z·sqrt(p̂(1-p̂)/n + z²/(4n²))``，z=1.96
    - 桶 n=0 时仍占位（保留 0 样本桶，便于后续绘图）

    注：与现有 ``evaluate_baseline`` 互补 —— Brier 单值给"平均错误"，
    reliability 给"哪段 prob 区间高估/低估"。
    """
    if n_bins < 1:
        n_bins = 1
    if not samples:
        return ReliabilityMetrics(bins=[], ece=0.0, n_total=0)

    bins: list[ReliabilityBin] = []
    ece_num = 0.0
    n_total = 0
    z = 1.96  # Wilson 95% CI

    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        in_bucket: list[tuple[float, float]] = []  # (top_prob, hit01)
        for s in samples:
            outcome = _normalize_result_label(s.get("result", ""))
            if outcome is None or outcome not in _RESULT_TO_INDEX:
                continue
            probs = s.get("probs") or {}
            top_prob = max(probs.get(k, 0.0) for k in _PROB_KEYS)
            # 闭桶右端：0.99 才进 [0.9, 1.0]；top_prob=1.0 走最后一桶
            if lo <= top_prob < hi or (i == n_bins - 1 and top_prob == hi):
                hit = 1.0 if _top_label(probs) == outcome else 0.0
                in_bucket.append((top_prob, hit))

        if not in_bucket:
            bins.append(ReliabilityBin(lo, hi, 0, 0.0, 0.0, 0.0, 0.0))
            continue
        n = len(in_bucket)
        mean_pred = sum(p for p, _ in in_bucket) / n
        actual = sum(h for _, h in in_bucket) / n
        # Wilson 95% CI for binomial proportion
        denom = 1 + z * z / n
        center = (actual + z * z / (2 * n)) / denom
        margin = z * math.sqrt(actual * (1 - actual) / n + z * z / (4 * n * n)) / denom
        ci_lo = max(0.0, center - margin)
        ci_hi = min(1.0, center + margin)
        bins.append(ReliabilityBin(
            pred_lo=round(lo, 4), pred_hi=round(hi, 4),
            n=n, mean_pred=round(mean_pred, 4),
            actual_rate=round(actual, 4),
            ci_lo=round(ci_lo, 4), ci_hi=round(ci_hi, 4),
        ))
        ece_num += n * abs(mean_pred - actual)
        n_total += n

    ece = ece_num / max(n_total, 1)
    return ReliabilityMetrics(bins=bins, ece=ece, n_total=n_total)


def format_reliability_report(rm: ReliabilityMetrics) -> str:
    """人类可读 reliability 输出（每桶 pred_lo - pred_hi / n / mean / actual / CI）。"""
    if rm.n_total == 0:
        return "[reliability] 无样本，跳过分桶评估"
    lines = [
        f"[reliability] bins={len(rm.bins)}  n={rm.n_total}  ece={rm.ece:.4f}",
        f"  {'bucket':<14} {'n':>5} {'mean_pred':>9} {'actual':>7}   ci[lo,hi]",
    ]
    for b in rm.bins:
        lines.append(
            f"  {b.pred_lo:.2f}-{b.pred_hi:<7.2f} {b.n:>5} {b.mean_pred:>9.4f} "
            f"{b.actual_rate:>7.4f}   [{b.ci_lo:.4f}, {b.ci_hi:.4f}]"
        )
    return "\n".join(lines)


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
        return "[retrain] no joined samples -- events.jsonl 中无 prediction<->outcome 配对"

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
        *,
        verbose: bool = True,
        reliability_bins: int | None = None) -> BaselineMetrics:
    """一键装载 + 评估 + 打印/返回。

    ``reliability_bins`` (Round 11 F3)：给出 N 时额外输出 reliability diagram
    （每 1/N 桶 Wilson 95% CI 实际胜率 + ECE）。
    """
    samples = load_joined_samples(events_jsonl_path, league=league, since_ts=since_ts)
    metrics = evaluate_baseline(samples)
    if verbose:
        print(format_report(metrics))
        if reliability_bins:
            rm = reliability_diagram(samples, n_bins=reliability_bins)
            print(format_reliability_report(rm))
    return metrics


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="M-2 retrain baseline — 用 ml_feedback joined 样本评估 probs Brier/accuracy")
    parser.add_argument("--events", type=Path, default=None,
                        help="events.jsonl 路径（缺省走 observability 默认路径）")
    parser.add_argument("--league", type=str, default=None, help="按联赛过滤（如 epl/laliga）")
    parser.add_argument("--since", type=str, default=None, help="ISO 时间戳，过滤此后的预测")
    # F3 (Round 11): reliability 分桶开关 —— 给出 N 时除 Brier/accuracy 外
    # 额外打印每 1/N 桶的实际胜率 + Wilson 95% CI + ECE。
    parser.add_argument("--reliability", type=int, default=None,
                        help="额外输出 reliability diagram（每 1%% 桶实际胜率 + Wilson 95%% CI）")
    args = parser.parse_args(argv)

    try:
        run(events_jsonl_path=args.events, league=args.league, since_ts=args.since,
            reliability_bins=args.reliability)
    except FileNotFoundError as exc:
        print(f"[retrain] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
