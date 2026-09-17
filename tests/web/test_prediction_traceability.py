"""Focused regression tests for prediction traceability fields.

These tests intentionally exercise the web projection only: no engine process,
network, database, or production prediction files are touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.routers.predictions import _prediction_summary, _run_metadata  # noqa: E402


def test_prediction_summary_preserves_traceability_and_calibration_fields():
    doc = {
        "match": "A vs B",
        "home": "A",
        "away": "B",
        "direction": "A 胜",
        "confidence_score": 0.72,
        "odds_data_available": False,
        "confidence_note": "赔率不可用，未计算 edge",
        "reasoning_factors": ["form", "poisson"],
        "ml_model_used": True,
        "ml_proba": {"home": 0.61, "draw": 0.22, "away": 0.17},
        "poisson_top3": [{"score": "1-0", "prob": 0.19}],
        "lambda_home": 1.4,
        "lambda_away": 0.9,
        "lambda_home_ci95": [1.1, 1.8],
        "lambda_away_ci95": [0.6, 1.2],
        "edge": 0.99,  # must not be invented/exposed from incomplete odds
    }

    out = _prediction_summary(doc, None)

    for key in (
        "odds_data_available", "confidence_note", "reasoning_factors",
        "ml_model_used", "ml_proba", "poisson_top3", "lambda_home",
        "lambda_away", "lambda_home_ci95", "lambda_away_ci95",
    ):
        assert out[key] == doc[key]
    assert "edge" not in out


def test_prediction_summary_does_not_add_optional_fields_when_absent():
    out = _prediction_summary({"match": "A vs B"}, None)
    assert out["match"] == "A vs B"
    assert "odds_data_available" not in out
    assert "confidence_note" not in out
    assert "ml_proba" not in out
    assert out["market"] == {"status": "missing"}


def test_market_projection_complete_1x2_calculates_safe_value():
    out = _prediction_summary({
        "match": "A vs B",
        "model_probs": {"home": 0.55, "draw": 0.25, "away": 0.20},
        "market": {
            "source": "test-book",
            "captured_at": "2026-09-16T12:00:00Z",
            "market_type": "1x2",
            "odds_format": "decimal",
            "selections": {"home": 2.0, "draw": 3.5, "away": 4.5},
        },
    }, None)
    market = out["market"]
    assert market["status"] == "complete"
    assert market["model_probs"] == {"home": 0.55, "draw": 0.25, "away": 0.2}
    assert market["decimal_odds"]["home"] == 2.0
    assert market["ev_per_unit"]["home"] == pytest.approx(0.1)
    assert set(market["edge_prob"]) == {"home", "draw", "away"}


def test_market_projection_partial_never_fabricates_value():
    out = _prediction_summary({
        "match": "A vs B",
        "confidence_score": 0.9,
        "market": {"source": "test-book", "market_type": "1x2", "selections": {"home": 2.0}},
    }, None)
    market = out["market"]
    assert market["status"] == "partial"
    assert "ev_per_unit" not in market
    assert "edge_prob" not in market


def test_market_projection_rejects_reasoning_factors_as_model_probs():
    out = _prediction_summary({
        "match": "A vs B",
        "reasoning_factors": {"home_ml_true_prob": 0.8, "draw_true_prob": 0.1, "away_ml_true_prob": 0.1},
        "market": {"source": "test-book", "captured_at": "now", "market_type": "1x2",
                   "selections": {"home": 2.0, "draw": 3.5, "away": 4.5}},
    }, None)
    assert "model_probs" not in out["market"]
    assert "ev_per_unit" not in out["market"]


def test_run_metadata_exposes_safe_provenance_without_local_path():
    out = _run_metadata(
        "epl",
        {
            "name": "prediction_2026-07-26_10.json",
            "path": "/secret/server/predictions/prediction.json",
            "data": {
                "generated_at": "2026-07-26T10:00:00+08:00",
                "data_window": "20260726-20260728",
                "status": "ok",
                "data_source": "football-data",
                "tournament_type": "league",
                "dixon_coles_enabled": True,
                "dixon_coles_rho": -0.13,
                "predictions": [{"match": "A vs B"}],
            },
        },
    )

    assert out == {
        "league": "epl",
        "file": "prediction_2026-07-26_10.json",
        "generated_at": "2026-07-26T10:00:00+08:00",
        "data_window": "20260726-20260728",
        "status": "ok",
        "data_source": "football-data",
        "tournament_type": "league",
        "dixon_coles_enabled": True,
        "dixon_coles_rho": -0.13,
        "n_predictions": 1,
    }
    assert "/secret/" not in str(out)
