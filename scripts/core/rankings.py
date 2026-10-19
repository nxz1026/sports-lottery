"""FIFA rankings: single source of truth, breaks circular dependency between model and data layers."""

from __future__ import annotations

import json

from core.config import FOOTBALL_DIR
from core.log import logger


def fetch_fifa_rankings() -> dict:
    """获取 FIFA 世界排名，返回 {country_name: rank} 字典。

    优先级：本地文件 > 内置默认值。
    此函数是 model 层和 data 层的统一入口，避免循环依赖（P1-3）。
    """
    rank_file = FOOTBALL_DIR / "references" / "fifa_rankings.json"

    # 尝试从本地文件加载
    if rank_file.exists():
        try:
            with open(rank_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and len(data) > 10:
                logger.debug(f"Loaded {len(data)} FIFA rankings from local file")
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read local FIFA rankings: {e}")

    # 内置默认排名（常用球队，避免空字典导致全用默认 rank=200）
    _DEFAULT_RANKINGS = {
        "Argentina": 1, "France": 2, "Spain": 3, "England": 4, "Brazil": 5,
        "Portugal": 6, "Netherlands": 7, "Belgium": 8, "Italy": 9, "Germany": 10,
        "Croatia": 11, "Uruguay": 12, "Morocco": 13, "Colombia": 14, "Mexico": 15,
        "Japan": 16, "Senegal": 17, "USA": 18, "Iran": 19, "Switzerland": 20,
        "South Korea": 21, "Denmark": 22, "Austria": 23, "Sweden": 24, "Poland": 25,
        "Australia": 26, "Egypt": 27, "Algeria": 28, "Ukraine": 29, "Turkey": 30,
        "Serbia": 31, "Romania": 32, "Paraguay": 33, "Wales": 34, "Czech Republic": 35,
        "Nigeria": 36, "Norway": 37, "Slovakia": 38, "Slovenia": 39, "Scotland": 40,
        "Canada": 41, "Hungary": 42, "Ghana": 43, "Ivory Coast": 44, "Tunisia": 45,
        "Qatar": 46, "Jamaica": 47, "Chile": 48, "Costa Rica": 49, "Panama": 50,
        "China PR": 80, "China": 80, "Saudi Arabia": 55, "Iraq": 56,
        "New Zealand": 100, "DR Congo": 45, "Cameroon": 38,
    }
    logger.info(f"Using built-in default FIFA rankings ({len(_DEFAULT_RANKINGS)} teams)")
    return _DEFAULT_RANKINGS
