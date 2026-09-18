"""League-specific configuration: per-league DC rho, lambda multiplier, and metadata."""

from __future__ import annotations

# ════════════════════════════════════════════════
# 联赛差异化 Dixon-Coles ρ 参数
# 不同联赛的平局倾向差异显著：
#   Serie A 平局率 ~28% → ρ=0.28 (低分平局校正更强)
#   Bundesliga 平局率 ~24% → ρ=0.19
#   EPL 平局率 ~23% → ρ=0.17 (进攻型联赛，低分平局少)
# ════════════════════════════════════════════════
LEAGUE_DC_RHO: dict[str, float] = {
    "epl":      0.17,   # 英超：进攻性强，平局偏少
    "laliga":   0.22,   # 西甲：技术流，中等平局
    "bundesliga": 0.19,  # 德甲：进球多，平局略少
    "seriea":   0.28,   # 意甲：防守型，平局最多
    "ligue1":   0.21,   # 法甲：接近平均
}

# ════════════════════════════════════════════════
# 联赛差异化 λ 映射系数
# 不同联赛场均进球差异显著，统一 2.8 导致系统性偏差
# 数据来源: 各联赛近 5 赛季场均进球统计
# ════════════════════════════════════════════════
LEAGUE_LAMBDA_MULTIPLIER: dict[str, float] = {
    "epl":      2.8,    # 英超: ~2.8 球/场
    "laliga":   2.7,    # 西甲: ~2.65 球/场
    "bundesliga": 3.2,   # 德甲: ~3.2+ 球/场（进攻型）
    "seriea":   2.5,    # 意甲: ~2.5 球/场（防守型）
    "ligue1":   2.7,    # 法甲: ~2.7 球/场
}

# ── 联赛元数据配置 ─────────────────────────────────
# football-data.org 为默认数据源（免费，含赔率）
# api-football 为备用（需额外 API key）
LEAGUE_CONFIG: dict[str, dict[str, object]] = {
    "epl": {
        "name": "English Premier League",
        "data_source": "football-data",  # 默认 football-data.org
        "league_id": "PL",
        "api_football_id": 39,
        "espn_slug": "eng.1",
        "host_country": "England",
        "groups": False,
        "knockout": False,
    },
    "laliga": {
        "name": "La Liga",
        "tournament_type": "league",
        "data_source": "football-data",
        "league_id": "PD",
        "api_football_id": 140,
        "espn_slug": "esp.1",
        "host_country": "Spain",
        "groups": False,
        "knockout": False,
    },
    "bundesliga": {
        "name": "Bundesliga",
        "tournament_type": "league",
        "data_source": "football-data",
        "league_id": "BL1",
        "api_football_id": 78,
        "espn_slug": "ger.1",
        "host_country": "Germany",
        "groups": False,
        "knockout": False,
    },
    "seriea": {
        "name": "Serie A",
        "tournament_type": "league",
        "data_source": "football-data",
        "league_id": "SA",
        "api_football_id": 135,
        "espn_slug": "ita.1",
        "host_country": "Italy",
        "groups": False,
        "knockout": False,
    },
    "ligue1": {
        "name": "Ligue 1",
        "tournament_type": "league",
        "data_source": "football-data",
        "league_id": "FL1",
        "api_football_id": 61,
        "espn_slug": "fra.1",
        "host_country": "France",
        "groups": False,
        "knockout": False,
    },
}