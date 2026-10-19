"""Model layer: Poisson, Dixon-Coles, Monte Carlo, Features, Ensemble.

Note: The features module is EXPERIMENTAL and may change without notice.
"""

from core.model.features import (
    FEATURE_COLUMNS,
    NUM_FEATURES,
    build_training_set,
    extract_features,
    feature_dict,
)
from core.model.monte_carlo import monte_carlo_champion
from core.model.onside import (
    compute_onside_signals,
    confederation_score,
    fifa_rank_to_score,
    host_advantage_score,
)
from core.model.poisson import (
    dixon_coles_match_probs,
    dixon_coles_pmf,
    fit_dc_rho,
    poisson_confidence_interval,
    poisson_pmf,
    tau_correction,
)

__all__ = [
    "poisson_pmf",
    "poisson_confidence_interval",
    "dixon_coles_pmf",
    "tau_correction",
    "dixon_coles_match_probs",
    "fit_dc_rho",
    "compute_onside_signals",
    "confederation_score",
    "fifa_rank_to_score",
    "host_advantage_score",
    "monte_carlo_champion",
    "extract_features",
    "feature_dict",
    "build_training_set",
    "FEATURE_COLUMNS",
    "NUM_FEATURES",
]
