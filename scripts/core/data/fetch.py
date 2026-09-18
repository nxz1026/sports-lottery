"""Data fetching layer: ESPN, football-data.org, API-Football, FIFA rankings."""

from __future__ import annotations

import json
import urllib.request
import gzip
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core.config import (
    ESPN_URL_TEMPLATE, ESPN_MAX_RETRIES, ESPN_RETRY_DELAY_SECONDS,
    ESPN_TIMEOUT_SECONDS, LEAGUE_CONFIG, FOOTBALL_DIR,
    TIMEOUT_FOOTBALL_DATA, TIMEOUT_API_FOOTBALL,
)
from core.log import logger
from core.data.convert import convert_football_data_to_espn_format, convert_api_football_to_espn_format


# ── 启动时 API Key 预检（P1-2）─────────────────────────────
def validate_api_keys() -> dict[str, bool]:
    """预检必要环境变量是否已配置，缺失则提前降级并打 warning"""
    keys = {
        "FOOTBALL_DATA_API_KEY": os.environ.get("FOOTBALL_DATA_API_KEY", ""),
        "API_FOOTBALL_KEY": os.environ.get("API_FOOTBALL_KEY", ""),
    }
    available = {k: bool(v.strip()) for k, v in keys.items()}
    for k, ok in available.items():
        if not ok:
            logger.warning(f"API key '{k}' not set — data sources requiring it will be unavailable")
    return available


# 执行一次全局预检
_API_KEYS_OK: dict[str, bool] = validate_api_keys()


def _validate_api_response(data: dict, source: str, required_keys: list[str] | None = None) -> dict:
    """校验 API 响应结构完整性，缺失关键字段时打 warning 并返回安全默认值。"""
    if not isinstance(data, dict):
        logger.warning(f"[{source}] API response is not a dict (got {type(data).__name__}), returning empty")
        return {}
    if required_keys:
        missing = [k for k in required_keys if k not in data]
        if missing:
            logger.warning(f"[{source}] API response missing keys: {missing}")
    # 检查常见错误响应
    if "message" in data and "error" in str(data.get("message", "")).lower():
        logger.warning(f"[{source}] API returned error message: {data.get('message')}")
    if data.get("count") == 0 and "response" in data:
        logger.info(f"[{source}] API returned 0 results")
    return data


# ── API-Football 速率限制追踪 ──
_rate_limit_info: dict[str, Any] = {"remaining": None, "limit": None, "last_updated": None}
_last_resp_headers: dict[str, str] = {}


def get_rate_limit_status() -> dict[str, Any]:
    """获取 API-Football 当前速率限制状态"""
    return _rate_limit_info.copy()


def _update_rate_limit_from_response(resp_headers: dict) -> None:
    """从 API 响应头中提取速率限制信息"""
    for key, val in resp_headers.items():
        kl = key.lower()
        if kl in ("x-ratelimit-requests-remaining", "x-requests-remaining"):
            try:
                _rate_limit_info["remaining"] = int(val)
            except (ValueError, TypeError):
                pass
        elif kl in ("x-ratelimit-requests-limit", "x-requests-limit"):
            try:
                _rate_limit_info["limit"] = int(val)
            except (ValueError, TypeError):
                pass
    _rate_limit_info["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    remaining = _rate_limit_info.get("remaining")
    if remaining is not None and remaining < 10:
        logger.warning(f"API-Football rate limit low: {remaining} requests remaining")


def _retry_request(req: urllib.request.Request, max_retries: int = 3, timeout: int | None = None) -> dict:
    """通用重试机制：指数退避，适用于所有外部 API 调用。
    成功时把响应头存入 _last_resp_headers，供速率限制追踪使用。"""
    _timeout = timeout or ESPN_TIMEOUT_SECONDS
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=_timeout) as resp:
                _last_resp_headers.clear()
                _last_resp_headers.update(dict(resp.headers))
                return json.loads(resp.read())
        except Exception as e:
            logger.warning(f"Request attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}")
            if attempt < max_retries:
                delay = ESPN_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise

FIFA_RANKINGS_API_URL = "https://api.football-data.org/v4/teams"


def fetch_events(dates_str: str, league_key: str = "epl", data_source: str = "",
                 whole_season: bool = False) -> list:
    """
    抽象数据源层：根据联赛配置获取比赛数据。

    Args:
        dates_str: 日期范围字符串 (YYYYMMDD-YYYYMMDD)
        league_key: 联赛键 (epl, laliga, bundesliga, seriea, ligue1, mls)
        data_source: 数据源 (espn, football-data, api-football)，空字符串=取 config 默认
        whole_season: True 时取当前整季赛程而非日期区间。football-data 实测：
            不传日期参数即返回本赛季全部（PL 380 场 = 40 已结束 + 340 未开赛）；
            而传日期区间会跨赛季（±300 天拿到 310 场已结束，混入上赛季），
            会污染积分榜播种。且区间 > 750 天会被源直接 400 拒绝。

    Returns:
        list: events 列表
    """
    config = LEAGUE_CONFIG.get(league_key, LEAGUE_CONFIG["epl"])

    # 用户显式传入的 data_source 优先，否则用 config 默认
    effective_source = data_source or config["data_source"]

    if effective_source == "espn":
        if whole_season:
            logger.warning("espn 数据源不支持整季取数，退回日期区间")
        return fetch_espn(dates_str, config.get("espn_slug", "epl"))
    elif effective_source == "football-data":
        return fetch_football_data(dates_str, config, whole_season=whole_season)
    elif effective_source == "api-football":
        if whole_season:
            logger.warning("api-football 整季取数未接入，退回日期区间")
        # 并行获取 api-football + ESPN fallback
        espn_slug = config.get("espn_slug", "epl")
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_api = pool.submit(fetch_api_football, dates_str, config)
            fut_espn = pool.submit(fetch_espn, dates_str, espn_slug)
            api_results = fut_api.result()
            if api_results:
                return api_results
            logger.info("api-football returned 0 events, using ESPN fallback")
            return fut_espn.result()
    else:
        if whole_season:
            logger.warning(f"{effective_source} 数据源不支持整季取数，退回日期区间")
        return fetch_espn(dates_str, config.get("espn_slug", "epl"))


def fetch_espn(dates_str: str, league_slug: str = "epl") -> list:
    """抓取 ESPN 数据（带重试机制）, 返回 parsed events"""
    url = ESPN_URL_TEMPLATE.format(dates=dates_str, league_slug=league_slug)

    for attempt in range(1, ESPN_MAX_RETRIES + 1):
        try:
            logger.info(f"Fetching ESPN (attempt {attempt}/{ESPN_MAX_RETRIES}): {url}")
            req = urllib.request.Request(url, headers={
                'User-Agent': 'python-requests/2.31',
                'Accept-Encoding': 'gzip'
            })
            with urllib.request.urlopen(req, timeout=ESPN_TIMEOUT_SECONDS) as resp:
                data = json.loads(gzip.decompress(resp.read()))
            data = _validate_api_response(data, "ESPN", required_keys=["events"])

            return data.get("events", [])

        except Exception as e:
            logger.warning(f"Attempt {attempt} failed: {type(e).__name__}: {e}")
            if attempt < ESPN_MAX_RETRIES:
                delay = ESPN_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info(f"Retrying in {delay}s...")
                time.sleep(delay)
            else:
                logger.error(f"All {ESPN_MAX_RETRIES} attempts failed for ESPN")
                raise


def fetch_football_data(dates_str: str, config: dict, whole_season: bool = False) -> list:
    """
    从 football-data.org 获取数据。
    注意：需要 API key（环境变量 FOOTBALL_DATA_API_KEY）

    ``whole_season=True`` 时不传日期参数，返回当前赛季全部比赛。
    """
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    league_id = config["league_id"]

    # 解析日期范围并转为 API 所需格式 YYYY-MM-DD
    if "-" in dates_str:
        start_raw, end_raw = dates_str.split("-", 1)
    else:
        start_raw = end_raw = dates_str

    def fmt_date(s):
        s = s.strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s

    start_date = fmt_date(start_raw)
    end_date = fmt_date(end_raw)

    if whole_season:
        # 不传日期参数：football-data 返回当前赛季全部比赛（PL 实测 380 场）
        url = f"https://api.football-data.org/v4/competitions/{league_id}/matches"
    else:
        url = f"https://api.football-data.org/v4/competitions/{league_id}/matches?dateFrom={start_date}&dateTo={end_date}"

    headers = {
        'User-Agent': 'LeaguePredict/4.1',
    }
    if api_key:
        headers['X-Auth-Token'] = api_key

    try:
        logger.info(f"Fetching football-data.org: {url}")
        req = urllib.request.Request(url, headers=headers)
        data = _retry_request(req, max_retries=3, timeout=TIMEOUT_FOOTBALL_DATA)
        data = _validate_api_response(data, "football-data", required_keys=["matches"])

        # 转换为 ESPN 格式
        return convert_football_data_to_espn_format(data, config)
    except Exception as e:
        logger.warning(f"football-data.org fetch failed: {e}")
        raise


def _compute_season(date_str: str) -> str:
    """根据日期计算赛季字符串（欧洲跨年赛季用前一年，自然年赛季用当年）"""
    if len(date_str) >= 4:
        year = int(date_str[:4])
    else:
        year = 2025
    month = int(date_str[4:6]) if len(date_str) >= 6 else 7
    # 欧洲联赛赛季 7 月～次年 6 月
    if month >= 7:
        return str(year)
    else:
        return str(year - 1)


def _build_odds_lookup(league_id: int, season: str, formatted_date: str, api_key: str, headers: dict) -> dict[int, list]:
    """获取 API-Football 赔率数据并建立 fixture_id → odds 查找表"""
    odds_url = f"https://v3.football.api-sports.io/odds?date={formatted_date}"
    try:
        logger.info(f"Fetching API-Football odds: {odds_url}")
        odds_req = urllib.request.Request(odds_url, headers=headers)
        odds_data = _retry_request(odds_req, max_retries=2, timeout=TIMEOUT_API_FOOTBALL)
        odds_resp = odds_data.get("response", [])
        lookup: dict[int, list] = {}
        for item in odds_resp:
            fixture_id = item.get("fixture", {}).get("id")
            if fixture_id:
                lookup[fixture_id] = item.get("bookmakers", [])
        logger.info(f"Odds data available for {len(lookup)} fixtures")
        return lookup
    except Exception as e:
        logger.warning(f"API-Football odds fetch failed (non-fatal): {e}")
        return {}


def fetch_api_football(dates_str: str, config: dict) -> list:
    """
    从 API-Football 获取数据（含赔率）。
    注意：需要 API key（环境变量 API_FOOTBALL_KEY）
    """
    api_key = os.environ.get("API_FOOTBALL_KEY", "")
    fixture_league_id = config.get("api_football_id")

    if not fixture_league_id:
        logger.warning(f"No api_football_id for {config.get('name', 'unknown')}, falling back to ESPN")
        return fetch_espn(dates_str, config.get("espn_slug", "epl"))

    if "-" in dates_str:
        date = dates_str.split("-")[0]
    else:
        date = dates_str

    # 转换日期格式 YYYYMMDD → YYYY-MM-DD
    formatted_date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    season = _compute_season(date)

    headers = {
        'User-Agent': 'LeaguePredict/4.1',
        'x-apisports-key': api_key,
    }

    try:
        # 1. 获取赛程（按日期，不收 league/season 限制 — 免费计划不支持 season 过滤）
        fixtures_url = f"https://v3.football.api-sports.io/fixtures?date={formatted_date}"
        logger.info(f"Fetching API-Football fixtures: {fixtures_url}")
        fixtures_req = urllib.request.Request(fixtures_url, headers=headers)
        fixtures_data = _retry_request(fixtures_req, max_retries=3, timeout=TIMEOUT_API_FOOTBALL)
        # 速率限制追踪：从响应头提取 API-Football 配额（P1/清理：此前为死功能）
        _update_rate_limit_from_response(dict(_last_resp_headers))
        fixtures_data = _validate_api_response(fixtures_data, "API-Football", required_keys=["response"])

        # 在客户端按 league_id 过滤
        all_fixtures = fixtures_data.get("response", [])
        filtered = [f for f in all_fixtures if f.get("league", {}).get("id") == fixture_league_id]
        fixtures_data["response"] = filtered
        logger.info(f"Fixtures: {len(all_fixtures)} total, {len(filtered)} for league {fixture_league_id}")

        # 2. 获取赔率
        odds_lookup = _build_odds_lookup(fixture_league_id, season, formatted_date, api_key, headers)

        return convert_api_football_to_espn_format(fixtures_data, config, odds_lookup)
    except Exception as e:
        logger.warning(f"API-Football fetch failed: {e}")
        raise


def update_fifa_rankings() -> dict:
    """从 API 获取 FIFA 排名并保存到本地文件"""
    rank_file = FOOTBALL_DIR / "references" / "fifa_rankings.json"
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")

    headers = {'User-Agent': 'LeaguePredict/4.1'}
    if api_key:
        headers['X-Auth-Token'] = api_key

    try:
        logger.info(f"Fetching FIFA rankings from API: {FIFA_RANKINGS_API_URL}")
        req = urllib.request.Request(FIFA_RANKINGS_API_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=ESPN_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())

        rankings = {}
        for team in data.get("teams", []):
            name = team.get("name", "")
            if name:
                rankings[name] = team.get("fifa_rank", team.get("id", 200))

        if rankings:
            rank_file.parent.mkdir(parents=True, exist_ok=True)
            with open(rank_file, "w", encoding="utf-8") as f:
                json.dump(rankings, f, indent=2)
            logger.info(f"FIFA rankings updated from API: {len(rankings)} teams")
            return rankings

        logger.debug("No team data in API response, falling back to local file")
    except Exception as e:
        logger.warning(f"FIFA rankings API fetch failed: {e}")

    return {}


def fetch_fifa_rankings(force_refresh: bool = False) -> dict:
    """获取 FIFA 世界排名 — 委托给 core.rankings.fetch_fifa_rankings（统一入口）"""
    from core.rankings import fetch_fifa_rankings as _fetch
    if force_refresh:
        update_fifa_rankings()
    return _fetch()

   