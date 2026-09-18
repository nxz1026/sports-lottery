"""Core constants: paths, URLs, retry config, model parameters, weights, thresholds."""

from __future__ import annotations

import os
from pathlib import Path

# ── 加载 .env（零依赖） ─────────────────────────
_env_loaded = False

def _load_dotenv() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    _skill_dir = Path(__file__).parent.parent
    _base = Path(os.environ.get("LP_OUTPUT_DIR", str(_skill_dir.parent)))
    _env_file = _base / ".env"
    if _env_file.exists():
        try:
            with open(_env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("\"'")
                    if key and not os.environ.get(key):
                        os.environ[key] = val
        except OSError:
            pass

_load_dotenv()

# ── 路径配置 ──────────────────────────────────────
_SKILL_DIR: Path = Path(__file__).parent.parent
FOOTBALL_DIR: Path = Path(os.environ.get("LP_OUTPUT_DIR", str(_SKILL_DIR)))
PREDICTIONS_DIR: Path = FOOTBALL_DIR / "predictions"
RESULTS_DIR: Path = FOOTBALL_DIR / "results"
TRENDS_FILE: Path = _SKILL_DIR / "references" / "tournament-trends.md"

# ── ESPN URL ──────────────────────────────────────
ESPN_URL_TEMPLATE: str = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/scoreboard?dates={dates}&limit=50"

# ── 重试配置 ──────────────────────────────────────
ESPN_MAX_RETRIES: int = 3
ESPN_RETRY_DELAY_SECONDS: int = 30
ESPN_TIMEOUT_SECONDS: int = 15

# ── Dixon-Coles 参数 ──────────────────────────────
DC_RHO: float = 0.2

# ── 蒙特卡洛参数 ──────────────────────────────────
DEFAULT_N_SIMULATIONS: int = 10000

# ── 取数窗口 ──────────────────────────────────────
# 回看天数：预测窗口若只取「今天-明天」，past_matches 恒为空，会连带打死
# 校准（calibration）、命中率（accuracy_summary）、对账（reconciliation）
# 以及模型自身的 form/record 特征（无历史比赛可推导 → 回退中性值）。
# 30 天可覆盖一个月的已结束比赛并带回比分；同一区间仍是一次 HTTP 请求。
DEFAULT_PAST_DAYS: int = 30

# ── 庄家水线常量（去水用）──────────────────────────
BOOKMAKER_MARGIN: float = 1.07

# ── Monte Carlo 统一进球范围 ───────────────────────
MAX_GOALS_MC: int = 8

# ── 市场赔率权重 ──────────────────────────────────
MARKET_ODDS_WEIGHT: float = 0.45

# ── Onside 4 信号权重 ─────────────────────────────
ONSIDE_WEIGHTS: dict[str, float] = {
    "fifa_ranking":     0.25,
    "league_footprint": 0.20,
    "host_advantage":   0.15,
    "confederation":    0.20,
}

# ── 预测阈值字典 ──────────────────────────────────
THRESHOLDS: dict = {
    "direction_min_prob":    0.45,
    "direction_odds_ratio":  1.3,
    "draw_threshold":        0.40,
    "near_mode_base":        0.33,
    "star_5":                0.90,
    "star_4":                0.72,
    "star_3":                0.55,
    "star_2":                0.35,
    "lambda_multiplier":     2.8,
    "lambda_lower_bound":    0.3,
    "draw_base_score":       0.15,
    "confidence_baseline":   0.25,
    "no_spread_penalty":     0.5,
    "no_odds_penalty":       0.25,
    "fifa_rank_default":     200,
    "host_advantage_score":  1.0,
    "total_min_divisor":     0.05,
    "rho_fit_min_sample":    20,
    "spread_movement_cap":   0.15,
    "elo_weight":            0.18,
}

# ── 足联实力系数 ──────────────────────────────────
CONFEDERATION_STRENGTH: dict[str, float] = {
    "UEFA":     1.00,
    "CONMEBOL": 0.95,
    "CONCACAF": 0.70,
    "AFC":      0.65,
    "CAF":      0.60,
    "OFC":      0.40,
}

# ── 国家→足联映射 ─────────────────────────────────
COUNTRY_CONFEDERATION: dict[str, str] = {
    "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Uruguay": "CONMEBOL",
    "Colombia": "CONMEBOL", "Chile": "CONMEBOL", "Paraguay": "CONMEBOL",
    "Ecuador": "CONMEBOL", "Peru": "CONMEBOL", "Venezuela": "CONMEBOL",
    "Bolivia": "CONMEBOL",
    "England": "UEFA", "France": "UEFA", "Germany": "UEFA", "Spain": "UEFA",
    "Italy": "UEFA", "Netherlands": "UEFA", "Portugal": "UEFA", "Belgium": "UEFA",
    "Croatia": "UEFA", "Switzerland": "UEFA", "Denmark": "UEFA", "Poland": "UEFA",
    "Austria": "UEFA", "Scotland": "UEFA", "Serbia": "UEFA", "Wales": "UEFA",
    "Turkey": "UEFA", "Norway": "UEFA", "Sweden": "UEFA", "Ukraine": "UEFA",
    "Czech Republic": "UEFA", "Czechia": "UEFA", "Hungary": "UEFA", "Greece": "UEFA",
    "Romania": "UEFA", "Slovakia": "UEFA", "Slovenia": "UEFA", "Finland": "UEFA",
    "Ireland": "UEFA", "Iceland": "UEFA", "Northern Ireland": "UEFA",
    "USA": "CONCACAF", "United States": "CONCACAF", "Mexico": "CONCACAF",
    "Canada": "CONCACAF", "Costa Rica": "CONCACAF", "Panama": "CONCACAF",
    "Jamaica": "CONCACAF", "Honduras": "CONCACAF",
    "Japan": "AFC", "South Korea": "AFC", "Korea Republic": "AFC",
    "Australia": "AFC", "Iran": "AFC", "Saudi Arabia": "AFC",
    "Qatar": "AFC", "China PR": "AFC", "China": "AFC",
    "Iraq": "AFC", "United Arab Emirates": "AFC", "Uzbekistan": "AFC",
    "Nigeria": "CAF", "Egypt": "CAF", "Senegal": "CAF", "Morocco": "CAF",
    "Cameroon": "CAF", "Ghana": "CAF", "Ivory Coast": "CAF", "Cote d'Ivoire": "CAF",
    "Algeria": "CAF", "Tunisia": "CAF", "Mali": "CAF", "South Africa": "CAF",
    "DR Congo": "CAF", "Congo DR": "CAF",
    "New Zealand": "OFC",
    "Burkina Faso": "CAF", "Guinea": "CAF", "Benin": "CAF",
    "Syria": "AFC", "Thailand": "AFC", "Vietnam": "AFC",
    "Oman": "AFC", "Bahrain": "AFC", "Jordan": "AFC",
    "Palestine": "AFC", "UAE": "AFC",
    "North Macedonia": "UEFA", "Georgia": "UEFA", "Armenia": "UEFA",
    "Israel": "UEFA", "Belarus": "UEFA", "Kosovo": "UEFA",
}

# ── 中文队名映射 ──────────────────────────────────
COUNTRY_CN: dict[str, str] = {
    # ── 国家队 ──
    "England": "英格兰", "France": "法国", "Germany": "德国", "Spain": "西班牙",
    "Italy": "意大利", "Netherlands": "荷兰", "Portugal": "葡萄牙", "Belgium": "比利时",
    "Croatia": "克罗地亚", "Switzerland": "瑞士", "Denmark": "丹麦", "Poland": "波兰",
    "Sweden": "瑞典", "Norway": "挪威", "Serbia": "塞尔维亚", "Ukraine": "乌克兰",
    "Turkey": "土耳其", "Austria": "奥地利", "Scotland": "苏格兰", "Hungary": "匈牙利",
    "Romania": "罗马尼亚", "Slovakia": "斯洛伐克", "Slovenia": "斯洛文尼亚", "Czech Republic": "捷克",
    "Czechia": "捷克", "Greece": "希腊", "Finland": "芬兰", "Wales": "威尔士",
    "Ireland": "爱尔兰", "Iceland": "冰岛", "Russia": "俄罗斯",
    "Brazil": "巴西", "Argentina": "阿根廷", "Uruguay": "乌拉圭", "Colombia": "哥伦比亚",
    "Chile": "智利", "Peru": "秘鲁", "Ecuador": "厄瓜多尔", "Paraguay": "巴拉圭",
    "Venezuela": "委内瑞拉", "Bolivia": "玻利维亚",
    "USA": "美国", "United States": "美国", "Mexico": "墨西哥", "Canada": "加拿大",
    "Costa Rica": "哥斯达黎加", "Panama": "巴拿马", "Jamaica": "牙买加", "Honduras": "洪都拉斯",
    "Japan": "日本", "South Korea": "韩国", "Korea Republic": "韩国",
    "Australia": "澳大利亚", "Iran": "伊朗", "Saudi Arabia": "沙特阿拉伯",
    "Qatar": "卡塔尔", "China PR": "中国", "China": "中国",
    "Iraq": "伊拉克", "UAE": "阿联酋", "United Arab Emirates": "阿联酋",
    "Uzbekistan": "乌兹别克斯坦", "Nigeria": "尼日利亚", "Egypt": "埃及",
    "Senegal": "塞内加尔", "Morocco": "摩洛哥", "Cameroon": "喀麦隆",
    "Ghana": "加纳", "Tunisia": "突尼斯", "Algeria": "阿尔及利亚",
    "Ivory Coast": "科特迪瓦", "Cote d'Ivoire": "科特迪瓦", "Mali": "马里",
    "South Africa": "南非", "DR Congo": "民主刚果", "Congo DR": "民主刚果",
    "New Zealand": "新西兰",
    # ── 英超俱乐部 ──
    "Arsenal": "阿森纳", "Manchester City": "曼城", "Liverpool": "利物浦",
    "Chelsea": "切尔西", "Manchester United": "曼联", "Tottenham": "热刺",
    "Tottenham Hotspur": "热刺", "Aston Villa": "阿斯顿维拉",
    "Newcastle": "纽卡斯尔", "Newcastle United": "纽卡斯尔",
    "Brighton": "布莱顿", "Brighton & Hove Albion": "布莱顿",
    "West Ham": "西汉姆联", "West Ham United": "西汉姆联",
    "Crystal Palace": "水晶宫", "Bournemouth": "伯恩茅斯",
    "Fulham": "富勒姆", "Brentford": "布伦特福德",
    "Everton": "埃弗顿", "Nottingham Forest": "诺丁汉森林",
    "Wolverhampton": "狼队", "Wolves": "狼队",
    "Leicester": "莱斯特城", "Leicester City": "莱斯特城",
    "Southampton": "南安普顿", "Ipswich Town": "伊普斯维奇",
    # ── 西甲俱乐部 ──
    "Real Madrid": "皇家马德里", "Barcelona": "巴塞罗那", "Atlético Madrid": "马德里竞技",
    "Real Betis": "皇家贝蒂斯", "Betis": "皇家贝蒂斯",
    "Villarreal": "比利亚雷亚尔", "Sevilla": "塞维利亚",
    "Athletic Bilbao": "毕尔巴鄂竞技", "Athletic Club": "毕尔巴鄂竞技",
    "Real Sociedad": "皇家社会", "Valencia": "瓦伦西亚",
    "Celta Vigo": "塞尔塔", "Celta": "塞尔塔",
    "Osasuna": "奥萨苏纳", "Getafe": "赫塔费",
    "Rayo Vallecano": "巴列卡诺", "Mallorca": "马略卡",
    "Girona": "赫罗纳", "Espanyol": "西班牙人",
    "Alavés": "阿拉维斯", "Las Palmas": "拉斯帕尔马斯",
    "Sevilla FC": "塞维利亚", "Leganés": "莱加内斯",
    "Valladolid": "巴拉多利德",
    "Levante": "莱万特", "Elche": "埃尔切",
    "Deportivo La Coruña": "拉科鲁尼亚", "Deportivo": "拉科鲁尼亚",
    "Racing Santander": "桑坦德竞技", "Racing Club": "桑坦德竞技",
    "Racing Club de Santander": "桑坦德竞技", "Racing de Santander": "桑坦德竞技",
    "Sporting Gijón": "希洪竞技", "Sporting": "希洪竞技",
    "Tenerife": "特内里费", "Zaragoza": "萨拉戈萨",
    "Huesca": "韦斯卡", "Albacete": "阿尔巴塞特",
    "Cartagena": "卡塔赫纳", "Burgos": "布尔戈斯",
    "Mirandés": "米兰德斯", "Eibar": "埃瓦尔",
    "Almería": "阿尔梅里亚", "Granada": "格拉纳达",
    "Cádiz": "加的斯", "Córdoba": "科尔多瓦",
    "Espanyol de Barcelona": "西班牙人", "Espanyol Barcelona": "西班牙人",
    # ── 德甲俱乐部 ──
    "Bayern Munich": "拜仁慕尼黑", "Borussia Dortmund": "多特蒙德",
    "Bayer Leverkusen": "勒沃库森", "Leverkusen": "勒沃库森",
    "RB Leipzig": "莱比锡红牛", "Leipzig": "莱比锡红牛",
    "Eintracht Frankfurt": "法兰克福", "Stuttgart": "斯图加特",
    "Borussia Mönchengladbach": "门兴格拉德巴赫",
    "Borussia M'gladbach": "门兴格拉德巴赫",
    "Werder Bremen": "云达不莱梅", "Wolfsburg": "沃尔夫斯堡",
    "Freiburg": "弗赖堡", "FC Augsburg": "奥格斯堡",
    "Augsburg": "奥格斯堡", "Hoffenheim": "霍芬海姆",
    "Union Berlin": "柏林联合", "Mainz": "美因茨",
    "Mainz 05": "美因茨", "Bochum": "波鸿",
    "Heidenheim": "海登海姆", "FC St. Pauli": "圣保利",
    "St. Pauli": "圣保利", "Holstein Kiel": "荷尔斯泰因基尔",
    # ── 意甲俱乐部 ──
    "Juventus": "尤文图斯", "AC Milan": "AC米兰", "Inter Milan": "国际米兰",
    "Napoli": "那不勒斯", "Roma": "罗马", "AS Roma": "罗马",
    "Lazio": "拉齐奥", "Atalanta": "亚特兰大",
    "Fiorentina": "佛罗伦萨", "Bologna": "博洛尼亚",
    "Torino": "都灵", "Udinese": "乌迪内斯",
    "Genoa": "热那亚", "Cagliari": "卡利亚里",
    "Hellas Verona": "维罗纳", "Monza": "蒙扎",
    "Lecce": "莱切", "Empoli": "恩波利",
    "Parma": "帕尔马", "Venezia": "威尼斯",
    "Como": "科莫",
    # ── 法甲俱乐部 ──
    "Paris Saint-Germain": "巴黎圣日耳曼", "PSG": "巴黎圣日耳曼",
    "Marseille": "马赛", "Lyon": "里昂", "Monaco": "摩纳哥",
    "Lille": "里尔", "Nice": "尼斯", "Rennes": "雷恩",
    "Lens": "朗斯", "Strasbourg": "斯特拉斯堡",
    "Montpellier": "蒙彼利埃", "Nantes": "南特",
    "Reims": "兰斯", "Toulouse": "图卢兹",
    "Brest": "布雷斯特", "Auxerre": "欧塞尔",
    "Angers": "昂热", "Saint-Étienne": "圣埃蒂安",
    "Stade de Reims": "兰斯", "Stade Rennais": "雷恩",
    "Le Havre": "勒阿弗尔",
    # ── 中超俱乐部 ──
    "Qingdao Hainiu": "青岛海牛", "Shanghai Shenhua": "上海申花",
    "Dalian Yingbo": "大连英博", "Liaoning Tieren": "辽宁铁人",
    "Zhejiang Professional FC": "浙江队", "Wuhan Three Towns": "武汉三镇",
    "Yunnan Yukun": "云南玉昆", "Chengdu Rongcheng": "成都蓉城",
    "Beijing Guoan": "北京国安", "Shenzhen Xinpengcheng": "深圳新鹏城",
    "Shanghai Port": "上海海港", "Shanghai SIPG": "上海海港",
    "Shandong Taishan": "山东泰山", "Shandong Luneng": "山东泰山",
    "Tianjin Jinmen Tigers": "天津津门虎", "Tianjin Teda": "天津津门虎",
    "Henan FC": "河南队", "Henan Songshan Longmen": "河南队",
    "Changchun Yatai": "长春亚泰", "Meizhou Hakka": "梅州客家",
    "Nantong Zhiyun": "南通支云", "Shenzhen FC": "深圳队",
}

# ── API 超时配置 ──────────────────────────────────
TIMEOUT_FOOTBALL_DATA: int = 15
TIMEOUT_API_FOOTBALL: int = 20

# ── 去水相关常量 ──────────────────────────────────
MIN_IMPLIED_PROB: float = 0.01

# ── ML 融合配置（零依赖轻量多分类器，与主模型概率融合）────────
ML_CONFIG: dict = {
    "enabled": True,            # 是否启用 ML 概率融合
    "blend_weight": 0.15,       # ML 概率在最终方向概率(H/D/A)中的融合权重
    "min_train_samples": 30,    # 训练样本低于此值则不训练/加载模型，回退规则基线
    "lr": 0.1,                  # 梯度下降学习率
    "epochs": 300,              # 训练轮数
    "l2": 0.01,                 # L2 正则化系数（防过拟合，小样本关键）
}