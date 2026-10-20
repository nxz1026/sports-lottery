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
    # 欧冠：实测平局率 9.7%（联赛阶段 72 场），远低于五大联赛 23%~28%
    # ⇒ 低分平局校正更弱，取 0.08（按 EPL 0.17 × 9.7/23 等比缩放）。
    "ucl":      0.08,
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
    # 欧冠：实测联赛阶段 72 场场均 3.736 球（主 2.236 / 客 1.500），
    # 淘汰赛 44 场场均 3.9（小样本）。取 3.7（联赛阶段实测值，弃用偏高的
    # 2026 单季 4.083 —— 仅 36 场，样本不足）。
    "ucl":      3.7,
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
    # ── 欧冠（2026-10-20 新增，路线图《建议 1》）────────────────────────
    # 数据源 = ESPN：football-data.org 没有欧冠（``league_id`` **故意不设**，
    # 设了回填器会拿它拼出无效的 football-data URL）。
    # ESPN scoreboard 的 ``dates`` 参数只接受单日 YYYYMMDD：传区间
    # ``20261020-20261024`` 实测 **HTTP 400**（其他联赛是返回 0 场）⇒
    # 整季取数走 ``dates=<年份>`` 通道（实测 dates=2026 → 189 场，
    # dates=2027 → 36 场，limit=200 即可取全；limit=50 会被截断）。
    "ucl": {
        "name": "UEFA Champions League",
        "name_cn": "欧冠",
        "tournament_type": "league",
        "data_source": "espn",
        "api_football_id": 2,
        "espn_slug": "uefa.champions",
        "espn_season_fetch": True,   # 整季取数（见 core/data/fetch.py::fetch_espn_season）
        "host_country": "World",     # 无主场国家概念 ⇒ 主场优势信号不生效
        "monte_carlo": False,        # 积分榜式夺冠模拟不适用于两阶段赛制（见 predict.py::_setup_league_run）
        "groups": True,              # 语义标记；当前**死字段**（全仓无人读取）
        "knockout": True,            # 同上
    },
}

# ════════════════════════════════════════════════
# 冷启动联赛的低置信标注
# ════════════════════════════════════════════════
# 路线图《建议 1》验收 ③：UCL 预测的 ``confidence_note`` 必须含「冷启动」字样，
# 且「不许与英超同档展示」。
#
# 路线图原文说「FIFA 排名是现成冷启动路径」——**实测推翻**：
# ``core.rankings.fetch_fifa_rankings`` 返回的是**国家队**排名
# （Argentina/France/…），俱乐部不在其中 ⇒ 欧冠队拿不到 FIFA 播种；
# ``core/elo.py`` 的全局 ELO 文件里也没有这些队 ⇒ 全部回落 ``DEFAULT_ELO``
# （无区分度）。⇒ 欧冠预测的第一信号其实是**市场赔率**（ESPN 自带
# moneyline / 让球 / 大小球），基本面（form/record）靠已完赛场次推导。
#
# 因此这里的文案必须**同时**说清「冷启动」和「FIFA 播种对俱乐部无效」，
# 不能谎称已用 FIFA 播种。
LEAGUE_CONFIDENCE_NOTES: dict[str, str] = {
    "ucl": "欧冠冷启动：俱乐部不在 FIFA 国家队排名内，ELO 亦无播种，"
           "信号主要来自市场赔率；置信度与五大联赛不同档",
}

# 冷启动联赛的置信度上限（钳位后重算星级）。五大联赛不设上限。
LEAGUE_CONFIDENCE_CAP: dict[str, float] = {
    "ucl": 0.5,   # star_3 门槛是 0.55 ⇒ 冷启动联赛最高 2-star
}
