"""Zero-dependency ML model for match outcome prediction.

Implements a multinomial logistic regression (one-vs-rest style softmax
classifier) trained with mini-batch-free full-batch gradient descent and L2
regularization. No third-party libraries required.

The model consumes the 26-dimensional feature vector produced by
``core.model.features.extract_features`` and outputs H/D/A probabilities,
which the main predictor blends with the Onside+DC probabilities.

Usage:
    from core.model.ml_model import MatchMLModel, load_league_model, save_league_model

    model = MatchMLModel(n_features=len(FEATURE_COLUMNS))
    model.fit(X, y)                 # X: list[list[float]], y: list[int] in {0,1,2}
    proba = model.predict_proba(x)  # -> [p_home, p_draw, p_away]
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from core.config import ML_CONFIG
from core.log import logger


class MatchMLModel:
    """Multinomial logistic regression with softmax + L2 gradient descent."""

    def __init__(self, n_features: int, n_classes: int = 3,
                 lr: float | None = None, epochs: int | None = None, l2: float | None = None):
        self.n_features = n_features
        self.n_classes = n_classes
        self.lr = lr if lr is not None else ML_CONFIG["lr"]
        self.epochs = epochs if epochs is not None else ML_CONFIG["epochs"]
        self.l2 = l2 if l2 is not None else ML_CONFIG["l2"]
        # 权重初始化为 0（确定性，避免随机种子依赖）
        self.weights: list[list[float]] = [[0.0] * n_features for _ in range(n_classes)]
        self.bias: list[float] = [0.0] * n_classes
        # 特征标准化参数（训练时拟合，推理时复用）
        self.mean: list[float] | None = None
        self.std: list[float] | None = None

    # ── 特征标准化 ───────────────────────────────
    def _fit_scaler(self, X: list[list[float]]) -> None:
        n = len(X)
        mean = [sum(X[r][c] for r in range(n)) / n for c in range(self.n_features)]
        var = [sum((X[r][c] - mean[c]) ** 2 for r in range(n)) / n for c in range(self.n_features)]
        std = [math.sqrt(v) if v > 1e-9 else 1.0 for v in var]
        self.mean = mean
        self.std = std

    def _standardize(self, X: list[list[float]]) -> list[list[float]]:
        if self.mean is None or self.std is None:
            return X
        return [[(X[r][c] - self.mean[c]) / self.std[c] for c in range(self.n_features)]
                for r in range(len(X))]

    # ── 前向 ─────────────────────────────────────
    @staticmethod
    def _softmax(logits: list[float]) -> list[float]:
        m = max(logits)
        exps = [math.exp(z - m) for z in logits]
        s = sum(exps)
        return [e / s for e in exps]

    def _logits(self, x: list[float]) -> list[float]:
        return [sum(self.weights[c][i] * x[i] for i in range(self.n_features)) + self.bias[c]
                for c in range(self.n_classes)]

    def predict_proba(self, x: list[float]) -> list[float]:
        if self.mean is not None:
            x = [(x[c] - self.mean[c]) / self.std[c] for c in range(self.n_features)]
        return self._softmax(self._logits(x))

    # ── 训练 ─────────────────────────────────────
    def fit(self, X: list[list[float]], y: list[int]) -> "MatchMLModel":
        if not X or len(X) != len(y):
            raise ValueError("X and y must be non-empty and equal length")
        self._fit_scaler(X)
        Xs = self._standardize(X)
        n = len(Xs)

        for _ in range(self.epochs):
            for c in range(self.n_classes):
                gw = [0.0] * self.n_features
                gb = 0.0
                for r in range(n):
                    prob = self._softmax(self._logits([Xs[r][i] for i in range(self.n_features)]))
                    delta = prob[c] - (1.0 if y[r] == c else 0.0)
                    for i in range(self.n_features):
                        gw[i] += delta * Xs[r][i] + self.l2 * self.weights[c][i]
                    gb += delta
                for i in range(self.n_features):
                    self.weights[c][i] -= self.lr * gw[i] / n
                self.bias[c] -= self.lr * gb / n
        return self

    # ── 持久化 ───────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "n_features": self.n_features,
            "n_classes": self.n_classes,
            "weights": self.weights,
            "bias": self.bias,
            "mean": self.mean,
            "std": self.std,
            "lr": self.lr,
            "epochs": self.epochs,
            "l2": self.l2,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MatchMLModel":
        model = cls(
            n_features=d["n_features"], n_classes=d.get("n_classes", 3),
            lr=d.get("lr"), epochs=d.get("epochs"), l2=d.get("l2"),
        )
        model.weights = [list(w) for w in d["weights"]]
        model.bias = list(d["bias"])
        model.mean = d.get("mean")
        model.std = d.get("std")
        return model


# ── 训练流程（按联赛）──────────────────────────────
def train_league_model(league_key: str, elo_ratings: dict | None = None,
                       days: int = 365, base_dir=None) -> "MatchMLModel | None":
    """用历史比赛训练某联赛的 ML 模型并落盘。

    复用 calibration.load_historical_past_matches 按联赛过滤后的历史，
    经 features.build_training_set 构造 (X, y)。样本不足时返回 None，
    由调用方回退到规则基线（不启用 ML 融合）。
    """
    from core.calibration import load_historical_past_matches
    from core.config import LEAGUE_CONFIG, ML_CONFIG
    from core.model.features import NUM_FEATURES, build_training_set

    past = load_historical_past_matches(days=days, league=league_key)
    if len(past) < ML_CONFIG["min_train_samples"]:
        logger.info(f"ML train [{league_key}]: 历史样本不足 ({len(past)} < "
                    f"{ML_CONFIG['min_train_samples']})，跳过训练，回退规则基线")
        return None

    host_country = LEAGUE_CONFIG.get(league_key, {}).get("host_country")
    context = {"elo_ratings": elo_ratings, "host_country": host_country}
    X, y = build_training_set(past, context)
    if len(X) < ML_CONFIG["min_train_samples"]:
        logger.info(f"ML train [{league_key}]: 有效带比分样本不足 ({len(X)} < "
                    f"{ML_CONFIG['min_train_samples']})，跳过训练")
        return None

    model = MatchMLModel(n_features=NUM_FEATURES)
    model.fit(X, y)
    save_league_model(model, league_key, base_dir)
    logger.info(f"ML train [{league_key}]: 训练完成，样本数={len(X)}")
    return model


def train_all_league_models(elo_ratings: dict | None = None, days: int = 365,
                            base_dir=None) -> dict[str, "MatchMLModel | None"]:
    """训练所有已配置联赛的 ML 模型。"""
    from core.config import LEAGUE_CONFIG
    results: dict[str, "MatchMLModel | None"] = {}
    for league_key in LEAGUE_CONFIG:
        results[league_key] = train_league_model(league_key, elo_ratings, days, base_dir)
    return results


def _model_path(league_key: str, base_dir) -> Path:
    from core.config import FOOTBALL_DIR
    base = base_dir or (FOOTBALL_DIR / "references")
    return base / f"ml_model_{league_key}.json"


def save_league_model(model: MatchMLModel, league_key: str, base_dir=None) -> None:
    path = _model_path(league_key, base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(model.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"Saved ML model for {league_key} -> {path}")
    except OSError as e:
        logger.warning(f"Failed to save ML model for {league_key}: {e}")


def load_league_model(league_key: str, base_dir=None) -> MatchMLModel | None:
    path = _model_path(league_key, base_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return MatchMLModel.from_dict(d)
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to load ML model for {league_key}: {e}")
        return None
