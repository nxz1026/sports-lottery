"""Retrain baseline — calibration + ML 重训（基于 ml_feedback 已关联样本）。

设计目标：
- **不重跑整条训练流水线**。H1+H2 落地后 ``events.jsonl`` 落盘了每场的
  ``probs``（H/D/A 概率）与 ``outcome``（H/D/A 标 label + 比分）。
- 本模块做两档产出：
  1. **概率校准评估** (Round 8 H1+H2)：对比 stored ``probs`` 与均匀假设在
     Brier / accuracy 上的差距，给"数据到底值多少"做一个 0 依赖可验证基线。
  2. **ML 重训** (Round 12)：用 events.jsonl 中的 features_snapshot 子集训练一个
     ``MatchMLModel``，写到 ``ml_model_<league>_from_events.json``（与生产主模型
     ``ml_model_<league>.json`` 隔离——主模型走 26 维 extract_features，从 events
     训练的产物只用于离线评估与未来上线前的 A/B）。

核心 API：
- ``load_joined_samples(events_jsonl_path, league=, since_ts=)`` —— 已 join
  prediction <-> outcome 的训练样本列表（来自 ``ml_feedback.join_predictions_outcomes``）
- ``evaluate_baseline(samples)`` —— 返回 ``n / brier / naive_brier / accuracy /
  improvement_pct / per_outcome_accuracy``
- ``reliability_diagram(samples, n_bins=10)`` —— Wilson 95% CI + ECE (Round 11 F1)
- ``format_report(metrics)`` —— 格式化人类可读摘要
- ``run(events_jsonl_path, league=, since_ts=)`` —— 一键装载+评估+打印
- ``train_league_model_from_events(league, ...)`` —— 基于 features_snapshot
  训练 ``MatchMLModel``，返回 ``TrainResult`` 摘要 (Round 12 M-3)

CLI 入口：
    python -m core.retrain_baseline [--events PATH] [--league LEAGUE] [--since ISO]
                                    [--reliability N] [--train-league LEAGUE]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


logger = logging.getLogger("predict")


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


# ── ML 重训（Round 12）────────────────────────────────────
@dataclass
class TrainResult:
    """train_league_model_from_events 的返回摘要。

    设计要点：
    - ``reason`` 用短语标签 ("ok" / "no joined samples" / "insufficient features" /
      "insufficient samples" / "training failed")，CLI/测试可直接 ``assertEqual``。
    - 训练后的 in-sample metrics（accuracy / brier）只是参考，**真正的可信度
      由 cv_accuracy / cv_brier 给**（R13 #6：K-fold cross-validation，避免
      小样本 in-sample 严重过乐观）。
    - cv_* 在 n_samples >= ``_CV_MIN_SAMPLES`` (50) 时计算，否则 None —— K-fold
      每折 ~6-10 样本噪声极大，报 cv_* 会误导。
    - ``path`` 与 ``model`` 互斥：失败时 path=None；model 内存对象若用户后续想
      load_league_model 验证，给的是同 league_key 但 ``_from_events`` 后缀路径。
    """
    reason: str = "ok"
    n_samples: int = 0
    n_features: int = 0
    feature_columns: list[str] = field(default_factory=list)
    model: object | None = None  # core.model.ml_model.MatchMLModel（运行时导入）
    path: Path | None = None
    train_accuracy: float | None = None
    train_brier: float | None = None
    cv_accuracy: float | None = None  # R13 #6: K-fold 5-fold CV accuracy（None=n<50）
    cv_brier: float | None = None      # R13 #6: K-fold 5-fold CV brier（None=n<50）
    detail: str = ""


def _events_model_path(league: str, base_dir) -> Path:
    """ML 重训产物的落盘路径：``ml_model_<league>_from_events.json``。

    **与生产主模型 ml_model_<league>.json 隔离**：
    - 主模型 n_features=26 来自 ``extract_features()``，需要完整 match dict
      (home/away/elo/odds/onside_signals/...)；
    - 从 events 训练只能消费 ``features_snapshot``（predictor 的
      ``reasoning_factors`` dump，当前 ~9 维），n_features 远小于 26；
    - ``MatchMLModel.predict_proba`` 喂 26 维会与 from_events 训练产物的 n_features
      不一致，因此**两套模型并存、由 predictor 只加载主模型**，events 模型用于
      离线评估 / 将来 reasoning_factors 扩到 26 维后再考虑并轨。
    """
    from core.config import FOOTBALL_DIR                     # noqa: PLC0415 — runtime
    base_root = base_dir or (FOOTBALL_DIR / "references")
    return Path(base_root) / f"ml_model_{league}_from_events.json"


def _common_feature_keys(samples: list[dict]) -> list[str]:
    """所有含 features_snapshot 样本里**公共覆盖**的数值 key（用作训练子集列）。

    判定：
    - features_snapshot 必须是 dict 且每个 key 的 value 是数字（int/float/非 NaN）；
    - 一个 key 必须出现在**所有**样本中才被采纳，保证 ``(X, y)`` 能构造规整矩阵；
    - 顺序按 key 字符串首次出现顺序（保持训练/推理对齐的可复现性）。
    """
    seen: list[str] = []
    must_have: set[str] | None = None
    for s in samples:
        snap = s.get("features_snapshot")
        if not isinstance(snap, dict):
            continue
        keys_here = []
        for k, v in snap.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            keys_here.append(k)
        if not keys_here:
            continue
        if must_have is None:
            must_have = set(keys_here)
            seen = list(keys_here)
        else:
            must_have &= set(keys_here)
    if not must_have:
        return []
    # 顺序取 ``seen`` 中属于 must_have 的（保首次出现顺序）
    return [k for k in seen if k in must_have]


def _build_xy_from_samples(
    samples: list[dict], feature_cols: list[str]
) -> tuple[list[list[float]], list[int]]:
    """按 ``feature_cols`` 把 samples 投影成 (X, y)。

    - X[r][c] = samples[r]["features_snapshot"][feature_cols[c]]
    - y[r] = _RESULT_TO_INDEX[_normalize_result_label(samples[r]["result"])]
    - 跳过 result 不可识别 / features_snapshot 缺列的样本（已被前置筛过，正常空跑）
    """
    X: list[list[float]] = []
    y: list[int] = []
    for s in samples:
        outcome = _normalize_result_label(s.get("result", ""))
        if outcome is None or outcome not in _RESULT_TO_INDEX:
            continue
        snap = s.get("features_snapshot")
        if not isinstance(snap, dict):
            continue
        try:
            row = [float(snap[c]) for c in feature_cols]
        except (KeyError, TypeError, ValueError):
            continue
        X.append(row)
        y.append(_RESULT_TO_INDEX[outcome])
    return X, y


def _train_metrics(X: list[list[float]], y: list[int], model) -> tuple[float, float]:
    """在训练集上算 accuracy + Brier（in-sample 参考指标，held-out 评估留 R12+）。

    用 predict_proba 逐样本推一遍（已被 model 拟合过，相当于"训练集自学习指标"）。
    """
    if not X:
        return 0.0, 0.0
    correct = 0
    brier_sum = 0.0
    for row, label_idx in zip(X, y):
        probs = model.predict_proba(row)
        # accuracy
        pred_idx = max(range(3), key=lambda i: probs[i])
        if pred_idx == label_idx:
            correct += 1
        # Brier sum (multi-class)
        for i, k in enumerate(_PROB_KEYS):
            actual = 1.0 if i == label_idx else 0.0
            brier_sum += (probs[i] - actual) ** 2
    n = len(X)
    return correct / n, brier_sum / n


# ── R13 #6: K-fold cross-validation 评估 held-out 表现 ──
_CV_K: int = 5                                  # 折数
_CV_MIN_SAMPLES: int = 50                       # 启用 CV 的样本下限（n<50 噪声大 → 不报）


def _kfold_metrics(
    X: list[list[float]], y: list[int], *,
    n_features: int, k: int = _CV_K,
) -> tuple[float | None, float | None]:
    """K-fold cross-validation：每折用其余 k-1 折训练、当前折验证，循环 k 次。

    返回 ``(cv_accuracy, cv_brier)``：
    - ``cv_accuracy``：5 折验证集平均准确率
    - ``cv_brier``：5 折验证集平均 Brier sum

    R13 #6 设计要点：
    - **入口守门**：n_samples < ``_CV_MIN_SAMPLES`` (50) 时返回 ``(None, None)``，
      因为每折 ~10 样本噪声极大，cv_* 会误导运维。
    - **零依赖**：纯 Python 列表索引切分 + 复用 ``MatchMLModel.fit``；
      不用 scikit-learn。
    - **顺序切分**而非随机：保留 events.jsonl 时间序列语义（最早 → 最新按 match_key
      排列，k-fold 切前 80% / 后 20% 等）。如未来要随机化再扩展。
    - 用 ``MatchMLModel(n_features=n_features).fit(X_tr, y_tr).predict_proba(x_te)``
      对每条验证样本单独 predict；与推理路径一致。
    """
    n = len(X)
    if n < _CV_MIN_SAMPLES:
        return None, None
    if k < k or k > n:
        return None, None

    correct_total = 0
    brier_total = 0.0
    n_eval = 0
    fold_size = n // k
    from core.model.ml_model import MatchMLModel            # noqa: PLC0415

    for fold in range(k):
        lo = fold * fold_size
        hi = (fold + 1) * fold_size if fold < k - 1 else n  # 最后一折兜底剩余
        # 验证集 = 折 [lo, hi)；训练集 = 其余（顺序切分保 events.jsonl 时间线语义）
        X_tr = X[:lo] + X[hi:]
        y_tr = y[:lo] + y[hi:]
        X_te = X[lo:hi]
        y_te = y[lo:hi]
        if not X_tr or not X_te:
            continue
        try:
            fold_model = MatchMLModel(n_features=n_features).fit(X_tr, y_tr)
        except ValueError:
            continue
        for row, label_idx in zip(X_te, y_te):
            probs = fold_model.predict_proba(row)
            pred_idx = max(range(3), key=lambda i: probs[i])
            if pred_idx == label_idx:
                correct_total += 1
            for i in range(3):
                actual = 1.0 if i == label_idx else 0.0
                brier_total += (probs[i] - actual) ** 2
            n_eval += 1

    if n_eval == 0:
        return None, None
    return correct_total / n_eval, brier_total / n_eval


def train_league_model_from_events(
    league: str, *,
    min_train_samples: int = 30,
    min_feature_keys: int = 8,
    base_dir=None,
    events_path: Path | str | None = None,
) -> TrainResult:
    """从 ``ml_feedback.join_predictions_outcomes`` 拉 league 样本，
    用 ``features_snapshot`` 公共子集训练 ``MatchMLModel``，写到
    ``ml_model_<league>_from_events.json``。

    失败模式（无 `reason != "ok"` 时 ``model/path`` 都为 None）：
    - ``"no joined samples"`` —— 该 league 在 events.jsonl 中无配对样本
    - ``"insufficient features"`` —— 所有样本 features_snapshot 公共 key < ``min_feature_keys``
    - ``"insufficient samples"`` —— 有效样本数 < ``min_train_samples``
    - ``"training failed"`` —— ``MatchMLModel.fit`` 抛 ValueError（理论上不应触发）

    Args:
        league: 联赛 key（epl / laliga / ...）
        min_train_samples: 最少样本阈值，参考 ``ML_CONFIG["min_train_samples"]``
        min_feature_keys: features_snapshot 公共覆盖 key 数下限。设得太低会让模型
            只看 1-2 维而退化成均值预测；太高会拒绝任何当前 events 形态。
            predictor.reasoning_factors 当前能填 9 个 key，所以默认 8 是临界。
        events_path: 详见 ``ml_feedback.join_predictions_outcomes``；缺省走
            ``observability.events_jsonl_path()``。

    Returns:
        ``TrainResult``（含 reason / n_samples / n_features / model / path /
        train_accuracy / train_brier）。失败时 model 与 path 都为 None，调用方
        据此判断是否走 calibration fallback（已有 ``run`` API）。
    """
    from core import ml_feedback                                # noqa: PLC0415
    from core.model.ml_model import MatchMLModel                # noqa: PLC0415

    try:
        samples = ml_feedback.join_predictions_outcomes(
            league=league,
            events_path=Path(events_path) if events_path is not None else None,
        )
    except FileNotFoundError as e:
        logger.warning("train_league %s: events.jsonl 未找到 -- %s", league, e)
        return TrainResult(reason="no joined samples", detail=f"events.jsonl 未找到: {e}")

    if not samples:
        logger.warning("train_league %s: events.jsonl 中无 prediction/outcome 配对", league)
        return TrainResult(reason="no joined samples", detail=f"league={league} 无配对 prediction/outcome")

    feature_cols = _common_feature_keys(samples)
    if len(feature_cols) < min_feature_keys:
        logger.warning(
            "train_league %s: features_snapshot 公共 key 仅 %d < min_feature_keys=%d，"
            "需先在 predictor._debug_fields 中扩展 reasoning_factors 覆盖",
            league, len(feature_cols), min_feature_keys,
        )
        return TrainResult(
            reason="insufficient features",
            n_samples=len(samples),
            feature_columns=feature_cols,
            detail=(f"features_snapshot 公共 key 仅 {len(feature_cols)} < "
                    f"min_feature_keys={min_feature_keys}；需先在 "
                    f"predictor._debug_fields 中扩展 reasoning_factors 覆盖"),
        )

    X, y = _build_xy_from_samples(samples, feature_cols)
    if len(X) < min_train_samples:
        logger.warning(
            "train_league %s: 有效训练样本 %d < min_train_samples=%d",
            league, len(X), min_train_samples,
        )
        return TrainResult(
            reason="insufficient samples",
            n_samples=len(X),
            n_features=len(feature_cols),
            feature_columns=feature_cols,
            detail=(f"有效训练样本 {len(X)} < min_train_samples={min_train_samples}"),
        )

    try:
        model = MatchMLModel(n_features=len(feature_cols)).fit(X, y)
    except ValueError as e:
        logger.warning("train_league %s: MatchMLModel.fit 抛 ValueError -- %s", league, e)
        return TrainResult(
            reason="training failed",
            n_samples=len(X),
            n_features=len(feature_cols),
            feature_columns=feature_cols,
            detail=f"MatchMLModel.fit 抛 ValueError: {e}",
        )

    train_acc, train_brier = _train_metrics(X, y, model)
    # R13 #6：K-fold CV 给 held-out 评估（n<50 时返回 None，避免每折 ~10 样本噪声误导）。
    cv_acc, cv_brier = _kfold_metrics(X, y, n_features=len(feature_cols))
    path = _events_model_path(league, base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(model.to_dict(), f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("train_league %s: 落盘 %s 失败 -- %s", league, path, e)
        return TrainResult(
            reason="training failed",
            n_samples=len(X),
            n_features=len(feature_cols),
            feature_columns=feature_cols,
            model=model,
            train_accuracy=train_acc,
            train_brier=train_brier,
            cv_accuracy=cv_acc,
            cv_brier=cv_brier,
            detail=f"落盘 {path} 失败: {e}",
        )

    cv_summary = ""
    if cv_acc is not None:
        cv_summary = f" cv_acc={cv_acc:.4f} cv_brier={cv_brier:.4f}"
    else:
        cv_summary = " cv_*=None(n<50)"
    logger.info(
        "train_league %s: 训练完成 n_samples=%d n_features=%d train_acc=%.4f train_brier=%.4f"
        "%s -> %s",
        league, len(X), len(feature_cols), train_acc, train_brier, cv_summary, path,
    )
    return TrainResult(
        reason="ok",
        n_samples=len(X),
        n_features=len(feature_cols),
        feature_columns=feature_cols,
        model=model,
        path=path,
        train_accuracy=train_acc,
        train_brier=train_brier,
        cv_accuracy=cv_acc,
        cv_brier=cv_brier,
        detail=f"trained on {len(X)} samples × {len(feature_cols)} features",
    )


def format_train_report(result: TrainResult) -> str:
    """人类可读 ML 重训摘要（10 行内）。"""
    if result.reason != "ok":
        lines = [f"[train-league] reason={result.reason}"]
        if result.detail:
            lines.append(f"  detail: {result.detail}")
        if result.n_samples:
            lines.append(f"  n_samples={result.n_samples}")
        if result.feature_columns:
            lines.append(
                f"  feature_columns (n={len(result.feature_columns)}): "
                + ", ".join(result.feature_columns)
            )
        return "\n".join(lines)

    lines = [
        f"[train-league] reason={result.reason}  n_samples={result.n_samples}  "
        f"n_features={result.n_features}",
        f"  feature_columns: {', '.join(result.feature_columns)}",
    ]
    if result.train_accuracy is not None:
        lines.append(f"  train accuracy:   {result.train_accuracy:.4f}")
    if result.train_brier is not None:
        lines.append(f"  train brier:      {result.train_brier:.4f}")
    # R13 #6: cv_* 给 held-out 真指标（None 表示 n<50 未启用）
    if result.cv_accuracy is not None:
        lines.append(f"  cv accuracy (5-fold):  {result.cv_accuracy:.4f}")
    else:
        lines.append("  cv accuracy (5-fold):  n/a (n<50)")
    if result.cv_brier is not None:
        lines.append(f"  cv brier (5-fold):     {result.cv_brier:.4f}")
    else:
        lines.append("  cv brier (5-fold):     n/a (n<50)")
    lines.append(f"  saved to: {result.path}")
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
    # M-3 (Round 12): ML 重训开关 —— 给出 league 时跳过 calibration 评估，直接
    # 训练并落盘 ``ml_model_<league>_from_events.json``。两档互斥。
    parser.add_argument("--train-league", type=str, default=None,
                        help="ML 重训某联赛（写 ml_model_<league>_from_events.json）；"
                             "与 --league/--reliability 互斥（重训模式独立）")
    parser.add_argument("--min-train-samples", type=int, default=30,
                        help="ML 重训最少有效样本数（默认 30，对齐 ML_CONFIG.min_train_samples）")
    parser.add_argument("--min-feature-keys", type=int, default=8,
                        help="features_snapshot 公共覆盖 key 数下限（默认 8，对齐当前 reasoning_factors 覆盖）")
    parser.add_argument("--base-dir", type=Path, default=None,
                        help="ML 模型落盘根目录（缺省走 FOOTBALL_DIR/references）")
    args = parser.parse_args(argv)

    try:
        if args.train_league:
            # M-3 (Round 12): 重训模式独立 —— 不走 calibration run()。
            result = train_league_model_from_events(
                args.train_league,
                min_train_samples=args.min_train_samples,
                min_feature_keys=args.min_feature_keys,
                base_dir=args.base_dir,
                events_path=args.events,
            )
            print(format_train_report(result))
            # reason != "ok" 时返回 3，让运维能 script 化捕获失败
            return 0 if result.reason == "ok" else 3

        run(events_jsonl_path=args.events, league=args.league, since_ts=args.since,
            reliability_bins=args.reliability)
    except FileNotFoundError as exc:
        print(f"[retrain] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
