"""League Predict v4.1 — core package."""

__version__ = "4.1.0"

from core.config import (
    ONSIDE_WEIGHTS,
    DC_RHO,
    THRESHOLDS,
)
from core.predictor import calculate_prediction
from core.model.onside import compute_onside_signals
from core.model.poisson import dixon_coles_match_probs, fit_dc_rho, poisson_confidence_interval
from core.model.monte_carlo import monte_carlo_champion
from core.calibration import build_calibration, load_historical_past_matches
from core.backtest import reconcile_predictions

__all__ = [
    "__version__",
    "calculate_prediction",
    "compute_onside_signals",
    "dixon_coles_match_probs",
    "fit_dc_rho",
    "poisson_confidence_interval",
    "monte_carlo_champion",
    "build_calibration",
    "load_historical_past_matches",
    "reconcile_predictions",
    "ONSIDE_WEIGHTS",

    "DC_RHO",
    "THRESHOLDS",

]
