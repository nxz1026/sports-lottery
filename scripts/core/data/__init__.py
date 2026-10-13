"""Data layer: fetch, parse, convert。

FIFA 排名联网入口（``update_fifa_rankings``）已迁 ``core.rankings``；
本地只读 + 内置默认表的版本也在 ``core.rankings.fetch_fifa_rankings``（P4-1 清理）。
"""

from core.data.fetch import fetch_events
from core.data.parse import parse_events, remove_vig, to_cn
from core.data.convert import convert_football_data_to_espn_format, convert_api_football_to_espn_format

__all__ = [
    "fetch_events",
    "parse_events",
    "remove_vig",
    "to_cn",
    "convert_football_data_to_espn_format",
    "convert_api_football_to_espn_format",
]
