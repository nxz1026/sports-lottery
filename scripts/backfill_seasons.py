"""ASEL-BACKFILL-EPL：英超 2022–2024 全赛季赛果回填。

只产标准化 JSON 到 scripts/results/，不入库、不改核心模块、不 commit。
复用 core.data.fetch 的 API-Football 鉴权与重试（_retry_request），
仅改用 ?league={id}&season={y} 一次拉一季整季。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 触发 core/constants._load_dotenv() 加载 repo/.env（含 API_FOOTBALL_KEY）
from core.config import LEAGUE_CONFIG, TIMEOUT_API_FOOTBALL  # noqa: F401
from core.data.fetch import _last_resp_headers, _retry_request, _update_rate_limit_from_response
from core.log import logger

# 安全速率：连发 11 条起 429，每请求后 sleep 7s（同 ops/league-daily-predict.sh 口径）
REQUEST_INTERVAL_SECONDS = 7
SEASON_RETRIES = 1  # 单赛季 429/5xx 时整季重试次数

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "scripts" / "results"
LOG_DIR = REPO_ROOT / ".omp-logs"

FIXTURE_FIELDS = (
    "fixture.id",
    "fixture.date",
    "league.id",
    "teams.home.name",
    "teams.away.name",
    "score.fulltime.home",
    "score.fulltime.away",
    "score.halftime.home",
    "score.halftime.away",
)


def _dig(obj: dict, dotted: str):
    """按点分路径取值，缺失返回 None。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _extract_fixture(f: dict) -> dict:
    """按 FIXTURE_FIELDS 提取标准化字段。"""
    return {field: _dig(f, field) for field in FIXTURE_FIELDS}


def fetch_season(league_id: int, season: int, api_key: str) -> list:
    """拉取单赛季整季 fixtures（含 score.fulltime / score.halftime）。"""
    headers = {
        "User-Agent": "LeaguePredict/4.1",
        "x-apisports-key": api_key,
    }
    url = f"https://v3.football.api-sports.io/fixtures?league={league_id}&season={season}"
    logger.info(f"Fetching API-Football season {season}: {url}")
    req = urllib.request.Request(url, headers=headers)
    data = _retry_request(req, max_retries=3, timeout=TIMEOUT_API_FOOTBALL)
    _update_rate_limit_from_response(dict(_last_resp_headers))
    response = data.get("response", []) if isinstance(data, dict) else []
    logger.info(f"Season {season}: got {len(response)} fixtures")
    return response


def fetch_season_with_retry(league_id: int, season: int, api_key: str) -> tuple[list, int]:
    """整季拉取，429/5xx 时整季重试 SEASON_RETRIES 次。返回 (fixtures, 重试次数)。"""
    retries = 0
    for attempt in range(SEASON_RETRIES + 1):
        try:
            return fetch_season(league_id, season, api_key), retries
        except Exception as e:
            retries += 1
            logger.warning(f"Season {season} attempt {attempt + 1} failed: {type(e).__name__}: {e}")
            if attempt < SEASON_RETRIES:
                logger.info(f"Retrying whole season {season} in {REQUEST_INTERVAL_SECONDS}s...")
                time.sleep(REQUEST_INTERVAL_SECONDS)
            else:
                raise
    return [], retries  # 不可达，仅满足类型


def main() -> int:
    parser = argparse.ArgumentParser(description="回填指定联赛多赛季赛果")
    parser.add_argument("--league", required=True, help="联赛 key，如 epl")
    parser.add_argument("--seasons", required=True, help="逗号分隔赛季，如 2022,2023,2024")
    args = parser.parse_args()

    league_key = args.league
    seasons = [int(s.strip()) for s in args.seasons.split(",") if s.strip()]
    if not seasons:
        logger.error("No seasons provided")
        return 2

    config = LEAGUE_CONFIG.get(league_key)
    if not config:
        logger.error(f"Unknown league key: {league_key}")
        return 2
    league_id = config.get("api_football_id")
    if not league_id:
        logger.error(f"No api_football_id for league {league_key}")
        return 2

    api_key = os.environ.get("API_FOOTBALL_KEY", "")
    if not api_key:
        logger.error("API_FOOTBALL_KEY not set")
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_fixtures: list[dict] = []
    report_lines: list[str] = []
    report_lines.append(f"league={league_key} league_id={league_id} seasons={seasons}")
    report_lines.append(f"fetched_at={fetched_at}")

    for season in seasons:
        try:
            fixtures, retries = fetch_season_with_retry(league_id, season, api_key)
        except Exception as e:
            logger.error(f"Season {season} failed after retries: {e}")
            report_lines.append(f"season={season} status=FAILED error={type(e).__name__}: {e}")
            continue
        extracted = [_extract_fixture(f) for f in fixtures]
        all_fixtures.extend(extracted)
        report_lines.append(
            f"season={season} fixtures={len(extracted)} retries={retries} "
            f"missing_fulltime={sum(1 for x in extracted if x['score.fulltime.home'] is None or x['score.fulltime.away'] is None)} "
            f"missing_halftime={sum(1 for x in extracted if x['score.halftime.home'] is None or x['score.halftime.away'] is None)}"
        )
        # 每赛季之间也 sleep，避免跨赛季连发触发 429
        if season != seasons[-1]:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    out_path = RESULTS_DIR / f"backfill_{league_key}_{seasons[0]}-{seasons[-1]}.json"
    payload = {
        "league": league_key,
        "seasons": seasons,
        "fetched_at": fetched_at,
        "fixtures": all_fixtures,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(f"Wrote {len(all_fixtures)} fixtures to {out_path}")

    report_lines.append(f"total_fixtures={len(all_fixtures)}")
    # report 文件名跟 league 走（避免多联赛覆盖同一文件）
    report_name = f"ASEL-BACKFILL-{league_key.upper()}.report.txt"
    report_path = LOG_DIR / report_name
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines) + "\n")
    logger.info(f"Wrote report to {report_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())