"""Focused regression tests for prediction traceability fields.

These tests intentionally exercise the web projection only: no engine process,
network, database, or production prediction files are touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
