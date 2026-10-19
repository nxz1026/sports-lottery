"""Feature engineering pipeline for ML integration.

Converts match dicts → 26-dimensional feature vectors used by the
production ML classifier (core.model.ml_model.MatchMLModel), which is
blended with the Onside+DC probabilities in core.predictor.

Zero external dependencies.

Usage:
    from core.model.features import extract_features, FEATURE_COLUMNS

    # Single match
    vec = extract_features(match, context)
    # Multiple matches (training)
    X, y = build_training_set(past_matches, context)
"""

from __future__ import annotations

from typing import Any

from core.elo import DEFAULT_ELO, expected_score

FEATURE_COLUMNS: list[str] = [
    # Odds features
    "home_true_prob",
    "draw_true_prob",
    "away_true_prob",
    "odds_available",
    # Form features
    "home_form_score",
    "away_form_score",
    "home_record_score",
    "away_record_score",
    # Market movement
    "spread_movement",
    "home_ml_implied",
    "draw_implied",
    # Signal model features
    "home_onside_score",
    "away_onside_score",
    "home_fifa_score",
    "away_fifa_score",
    # ELO features
    "elo_home_expected",
    "home_elo_rating",
    "away_elo_rating",
    "elo_diff",
    # Engineering features
    "home_form_vs_away_form",
    "home_record_vs_away_record",
    "form_x_record_home",
    "form_x_record_away",
    "odds_vs_elo_diff",
    "onside_vs_elo_diff",
    # Host advantage
    "is_host_country",
]

NUM_FEATURES = len(FEATURE_COLUMNS)


def _append_derived_features(
    vec: list[float],
    match: dict[str, Any],
    context: dict[str, Any],
    hfs: float,
    afs: float,
    hrs: float,
    ars: float,
    hp: float,
    ap: float,
    home_onside: float,
    away_onside: float,
) -> None:
    """ELO 与工程特征按 FEATURE_COLUMNS 顺序追加（数值与原 extract_features 内联计算完全一致）。"""
    elo_ratings = context.get("elo_ratings")
    home_en = match.get("home_en", match.get("home", ""))
    away_en = match.get("away_en", match.get("away", ""))
    if elo_ratings:
        home_elo = elo_ratings.get(home_en, DEFAULT_ELO)
        away_elo = elo_ratings.get(away_en, DEFAULT_ELO)
        elo_home_exp = expected_score(home_elo, away_elo, home_adv=True)
        elo_diff = home_elo - away_elo
    else:
        home_elo = DEFAULT_ELO
        away_elo = DEFAULT_ELO
        elo_home_exp = 0.5
        elo_diff = 0.0

    form_diff = hfs - afs
    record_diff = hrs - ars
    form_x_record_h = hfs * hrs
    form_x_record_a = afs * ars
    odds_elo_diff = (hp - ap) - (elo_home_exp - 0.5)
    onside_elo_diff = (home_onside - away_onside) - (elo_home_exp - 0.5)
    is_host = 1.0 if context.get("host_country") and home_en == context["host_country"] else 0.0

    vec.extend([
        elo_home_exp, home_elo, away_elo, elo_diff,
        form_diff, record_diff,
        form_x_record_h, form_x_record_a,
        odds_elo_diff, onside_elo_diff,
        is_host,
    ])


def extract_features(match: dict[str, Any], context: dict[str, Any] | None = None) -> list[float]:
    """Convert a single match dict into a feature vector (list of floats)."""
    context = context or {}

    hp = match.get("home_true_prob") or 0.5
    dp = match.get("draw_true_prob") or 0.25
    # away_true_prob 区分"未提供"与"明确为 0"：未提供时用 1-hp-dp 推导，
    # 明确提供（含 0.0）则原样保留，避免被 `or 0.25` 误判成缺失值。
    ap_raw = match.get("away_true_prob")
    if ap_raw is None:
        ap = 1.0 - hp - dp
    else:
        ap = ap_raw
    if hp + dp + ap < 0.01:
        hp, dp, ap = 0.45, 0.25, 0.30

    hfs = match.get("home_form_score", 0.5)
    afs = match.get("away_form_score", 0.5)
    hrs = match.get("home_record_score", 0.5)
    ars = match.get("away_record_score", 0.5)
    sm = match.get("spread_movement_score", 0.0)
    hmi = match.get("home_ml_implied") or 0.5
    di = match.get("draw_implied") or 0.25
    odds_avail = 1.0 if match.get("odds_data_available", False) else 0.0

    onside = match.get("onside_signals") or {}
    home_onside = (onside.get("home") or {}).get("onside_score", 0.5)
    away_onside = (onside.get("away") or {}).get("onside_score", 0.5)
    home_fifa = (onside.get("home") or {}).get("fifa_score", 0.5)
    away_fifa = (onside.get("away") or {}).get("fifa_score", 0.5)

    vec = [
        hp, dp, ap, odds_avail,
        hfs, afs, hrs, ars,
        sm, hmi, di,
        home_onside, away_onside, home_fifa, away_fifa,
    ]
    _append_derived_features(vec, match, context, hfs, afs, hrs, ars, hp, ap, home_onside, away_onside)
    return vec


def feature_dict(match: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, float]:
    """Return features as a dict keyed by FEATURE_COLUMNS."""
    return dict(zip(FEATURE_COLUMNS, extract_features(match, context)))


def build_training_set(past_matches: list[dict], context: dict[str, Any] | None = None) -> tuple[list[list[float]], list[int]]:
    """Build (X, y) from historical matches where y = 0/1/2 (home/draw/away win)."""
    X: list[list[float]] = []
    y: list[int] = []
    for m in past_matches:
        score = m.get("score", "")
        if not score or "-" not in score:
            continue
        try:
            hg, ag = int(score.split("-")[0]), int(score.split("-")[1])
        except (ValueError, IndexError):
            continue
        label = 0 if hg > ag else 1 if hg == ag else 2
        X.append(extract_features(m, context))
        y.append(label)
    return X, y
