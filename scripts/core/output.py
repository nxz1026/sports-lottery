from __future__ import annotations

"""Output: save results and cleanup old files."""

import json
import time
from datetime import datetime, timezone
from typing import Any

from core.config import PREDICTIONS_DIR, RESULTS_DIR
from core.log import logger


def cleanup_old_files(days: int = 7) -> int:
    """清理超过 N 天的 predictions/ 和 results/ 文件"""
    cutoff = time.time() - days * 86400
    removed = 0
    for directory in [PREDICTIONS_DIR, RESULTS_DIR]:
        if not directory.exists():
            continue
        for f in directory.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
                logger.info(f"Cleaned up: {f.name}")
    if removed > 0:
        logger.info(f"Cleanup complete: removed {removed} files older than {days} days")
    return removed


def save_results(past_matches: list[dict[str, Any]]) -> None:
    """将已结束比赛结果保存到 results/ 目录，每天一份。"""
    scored = [m for m in past_matches if m.get("score") and "-" in m.get("score","")]
    if not scored:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = RESULTS_DIR / f"result_{today}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # 加载已有结果（同日多次运行时合并）。注意：老文件可能没有 home_en/away_en 字段
    # （2026-10-11 之前的版本），backfill 时按当前 home/away 取值回填（writer 内部已
    # 用 home_en 优先取值），确保 reconciliation `_bk_stable_key` 能命中。
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for m in data.get("matches", []):
                key = f"{m.get('home','')}_{m.get('away','')}"
                # backfill：补 home_en/away_en（缺失时沿用 home/away）
                if not m.get("home_en"):
                    m["home_en"] = m.get("home", "")
                if not m.get("away_en"):
                    m["away_en"] = m.get("away", "")
                existing[key] = m
        except (json.JSONDecodeError, OSError):
            pass

    try:
        matches: list[dict[str, Any]] = []
        for m in scored:
            parts = m["score"].split("-")
            if len(parts) == 2:
                # writer 同步写 home_en / away_en 字段，避免 reconciliation stable key 失配
                home_en = m.get("home_en") or m.get("home", "")
                away_en = m.get("away_en") or m.get("away", "")
                match_data = {
                    "id": m.get("name",""),
                    "kickoff_utc": m.get("kickoff_utc",""),
                    "home": home_en,
                    "home_en": home_en,
                    "away": away_en,
                    "away_en": away_en,
                    "home_score": int(parts[0]),
                    "away_score": int(parts[1]),
                    "status": m.get("status",""),
                }
                key = f"{match_data['home']}_{match_data['away']}"
                existing[key] = match_data

        matches = list(existing.values())
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": today, "matches": matches}, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved results: {path} ({len(matches)} matches, merged)")
    except Exception as e:
        logger.info(f"save_results error: {e}")
