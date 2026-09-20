"""把 OMP 回填的 5 联赛历史 JSON（API-Football 格式）适配并 append 进
references/historical_past_matches.json（ML 训练 / 校准的累积样本）。

不写 fact.jc_match（那是竞彩官方事实表，schema 不兼容 API-Football 数据；
强行插入会污染 league_id/sporttery_id 等"竞彩官方"语义字段）。

用法：
    PYTHONPATH=scripts:. python3 scripts/import_backfill.py [--dry-run]

幂等：append_historical_past_matches 按 (league, kickoff_utc, home, away) 去重；
同一文件多次跑不会重复添加。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("import_backfill")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "scripts" / "results"

# league_key 在文件名后缀与 _picks_context/_hit_stats 等一致；score 字段要求 "h-a" 字符串
BACKFILL_FILES = [
    ("epl",        RESULTS_DIR / "backfill_epl_2022-2024.json"),
    ("laliga",     RESULTS_DIR / "backfill_laliga_2022-2024.json"),
    ("seriea",     RESULTS_DIR / "backfill_seriea_2022-2024.json"),
    ("bundesliga", RESULTS_DIR / "backfill_bundesliga_2022-2024.json"),
    ("ligue1",     RESULTS_DIR / "backfill_ligue1_2022-2024.json"),
]


def _adapt(fx: dict) -> dict | None:
    """API-Football fixture 平铺字段 → 训练样本字段。返回 None 表示该场应跳过。"""
    # 平铺字段：fixture.id, fixture.date, teams.home.name, ...
    full = fx.get("score.fulltime.home")
    away = fx.get("score.fulltime.away")
    if full is None or away is None:
        return None
    try:
        ih, ia = int(full), int(away)
    except (TypeError, ValueError):
        return None
    if ih < 0 or ia < 0:
        return None
    home = fx.get("teams.home.name")
    away_name = fx.get("teams.away.name")
    kickoff = fx.get("fixture.date")
    if not (home and away_name and kickoff):
        return None
    return {
        "name": f"{home} vs {away_name}",
        "status": "STATUS_FULL_TIME",
        "completed": True,
        "kickoff_utc": kickoff,
        "home": home,
        "away": away_name,
        "score": f"{ih}-{ia}",
        # 其余字段（ml_*/form/record 等）走训练时重新计算，本次 append 不带，
        # 避免把 API-Football 不存在的字段填"UNK"污染历史样本。
    }


def _load(league_key: str, path: Path) -> list[dict]:
    if not path.exists():
        logger.warning("跳过 %s：文件不存在 %s", league_key, path)
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        logger.error("读 %s 失败：%s", path, e)
        return []
    out: list[dict] = []
    skipped = 0
    for fx in data.get("fixtures") or []:
        row = _adapt(fx)
        if row is None:
            skipped += 1
            continue
        out.append(row)
    logger.info("%s 读取 %d 场，跳过 %d 场（缺 score/teams/kickoff）",
                league_key, len(data.get("fixtures") or []), skipped)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="导入 OMP 回填 JSON 进 historical_past_matches.json")
    p.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = p.parse_args()

    from core.calibration import append_historical_past_matches

    total_added = 0
    total_skipped_files = 0
    for league_key, path in BACKFILL_FILES:
        rows = _load(league_key, path)
        if not rows:
            total_skipped_files += 1
            continue
        if args.dry_run:
            logger.info("[dry-run] %s 将 append %d 条（league=%s）", league_key, len(rows), league_key)
            total_added += len(rows)
            continue
        added = append_historical_past_matches(league_key, rows)
        logger.info("%s append %d 条（league=%s）", league_key, added, league_key)
        total_added += added

    logger.info("=== 总计：append %d 条；跳过 %d 个联赛文件 ===", total_added, total_skipped_files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
