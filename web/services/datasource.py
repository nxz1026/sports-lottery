"""web.services.datasource — 上游数据源静态配置。

只含 env 键名清单与联赛默认源（契约 §6.1/§7.1），绝不内含任何真实值。
web 侧不得 import scripts/ 引擎代码，故 LEAGUES 为静态复制（键名与引擎一致）。
"""
from __future__ import annotations

# 每个上游所需 env 键（空元组 = 无需 key，如 ESPN 公开 API）。
SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "football-data": ("FOOTBALL_DATA_API_KEY",),
    "api-football": ("API_FOOTBALL_KEY",),
    "espn": (),
}

# 引擎 LEAGUE_CONFIG 键名/默认源（契约 §7.1 静态复制）。
LEAGUES: dict[str, dict] = {
    "epl": {"name": "English Premier League", "data_source": "football-data"},
    "laliga": {"name": "La Liga", "data_source": "football-data"},
    "bundesliga": {"name": "Bundesliga", "data_source": "football-data"},
    "seriea": {"name": "Serie A", "data_source": "football-data"},
    "ligue1": {"name": "Ligue 1", "data_source": "football-data"},
    "nba": {"name": "NBA", "data_source": "the-odds", "active": False},
}

DEFAULT_SOURCE = "football-data"
