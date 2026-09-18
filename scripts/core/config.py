"""Configuration constants for the league-predict engine.

This module re-exports all symbols from constants.py and leagues.py
for backward compatibility.  New code should import from the
specific sub-modules directly.
"""

from __future__ import annotations

# ── 从 constants 重新导出所有符号 ──────────────────
from core.constants import (                     # noqa: F401
    _SKILL_DIR,
    BOOKMAKER_MARGIN,
    CONFEDERATION_STRENGTH,
    COUNTRY_CN,
    COUNTRY_CONFEDERATION,
    DC_RHO,
    DEFAULT_N_SIMULATIONS,
    DEFAULT_PAST_DAYS,
    ESPN_MAX_RETRIES,
    ESPN_RETRY_DELAY_SECONDS,
    ESPN_TIMEOUT_SECONDS,
    ESPN_URL_TEMPLATE,
    FOOTBALL_DIR,
    MARKET_ODDS_WEIGHT,
    MAX_GOALS_MC,
    MIN_IMPLIED_PROB,
    ML_CONFIG,
    ONSIDE_WEIGHTS,
    PREDICTIONS_DIR,
    RESULTS_DIR,
    THRESHOLDS,
    TIMEOUT_API_FOOTBALL,
    TIMEOUT_FOOTBALL_DATA,
    TRENDS_FILE,
)

# ── 从 leagues 重新导出所有符号 ────────────────────
from core.leagues import (                      # noqa: F401
    LEAGUE_CONFIG,
    LEAGUE_DC_RHO,
    LEAGUE_LAMBDA_MULTIPLIER,
)
 
LEAGUE_CONFIG["nba"] = {
    "name": "NBA",
    "name_cn": "NBA",
    "data_source": "the-odds",
    "sport": "basketball",
    "active": False,
}