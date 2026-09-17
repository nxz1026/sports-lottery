"""web.services.ai — AI 富化扩展模块（契约 §5，M3）。

设计：AI 是「扩展」而非主流程依赖。
- try-import/try-call：加载失败或调用异常一律降级 {available:false, reason}；
- 不阻塞/拖垮主流程：ai_status 内部全量捕获异常，永不抛穿；
- 引擎侧 LLM 失败已降级为 no-op（契约 §5.5），web 侧只读持久化产物
  （ai_scores.json，仓库根 predictions/，契约 §9-11），绝不 import scripts/ ai/。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path

from web import config
from web.services import store

logger = logging.getLogger("web.ai")

# 契约 §5.4：AI_SCORES_FILE = 仓库根 predictions/ai_scores.json。
AI_SCORES_FILE = Path(config.BASE_DIR / "predictions" / "ai_scores.json")


def _load_ai_scores() -> dict:
    """读 ai_scores.json；缺/坏 → {}（永不抛穿）。"""
    if not AI_SCORES_FILE.is_file():
        return {}
    try:
        with open(AI_SCORES_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.warning("坏 ai_scores.json，按空处理: %s", AI_SCORES_FILE)
        return {}
    return data if isinstance(data, dict) else {}


def ai_status() -> dict:
    """AI 模块可用性 + 富化数据概览（降级语义：失败返回 available:false, reason）。"""
    try:
        scores = _load_ai_scores()
    except Exception as exc:  # 防御：任何异常都不许拖垮端点
        logger.exception("AI 状态加载异常: %s", exc)
        return {"available": False, "reason": f"load_failed: {exc.__class__.__name__}", "matches": 0}
    if not scores:
        return {"available": True, "matches": 0, "note": "no ai_scores yet"}
    by_league: dict[str, int] = {}
    for value in scores.values():
        if isinstance(value, dict):
            league = str(value.get("league", "unknown"))
            by_league[league] = by_league.get(league, 0) + 1
    return {
        "available": True,
        "matches": len(scores),
        "by_league": dict(sorted(by_league.items())),
        "source": str(AI_SCORES_FILE),
    }


def ai_details() -> dict:
    """富化明细（按比赛英文名），条目含 ai_score/ai_summary/league/source。"""
    try:
        scores = _load_ai_scores()
    except Exception as exc:
        logger.exception("AI 明细加载异常: %s", exc)
        return {"available": False, "reason": f"load_failed: {exc.__class__.__name__}"}
    items = []
    for match, value in scores.items():
        if not isinstance(value, dict):
            continue
        items.append({
            "match": match,
            "ai_score": value.get("ai_score"),
            "ai_summary": value.get("ai_summary", ""),
            "ai_notes": value.get("ai_notes", ""),
            "league": value.get("league", ""),
            "source": value.get("source", ""),
        })
    items.sort(key=lambda row: row["ai_score"] or 0, reverse=True)
    return {"available": True, "matches": len(items), "items": items[:50]}


def _day_items(day: date) -> tuple[list[dict], dict[str, int]]:
    items, counts = [], {}
    for league, doc in store.latest_by_league().items():
        data = doc.get("data", {})
        if not store.covers_date(data, day):
            continue
        predictions = data.get("predictions", [])
        if not isinstance(predictions, list):
            continue
        counts[league] = 0
        for prediction in predictions:
            if isinstance(prediction, dict):
                row = dict(prediction)
                row.update(league=league, generated_at=data.get("generated_at"))
                items.append(row)
                counts[league] += 1
    return items, counts


def _joined_item(prediction: dict, scores: dict) -> dict:
    match = prediction.get("match", "")
    value = scores.get(match)
    joined = isinstance(value, dict) and value.get("league", "") == prediction.get("league")
    item = {key: prediction.get(key) for key in ("match", "home", "away", "league", "kickoff_utc", "direction", "stars", "confidence_score", "predicted_score")}
    item["ai_matched"] = joined
    item.update({key: value.get(key) if joined else (None if key == "ai_score" else "") for key in ("ai_score", "ai_summary", "ai_notes", "source")})
    return item


def ai_daily_report(day: date) -> dict:
    try:
        scores = _load_ai_scores()
        predictions, counts = _day_items(day)
        items = [_joined_item(prediction, scores) for prediction in predictions]
        matched_by_league = {}
        for item in items:
            matched_by_league[item["league"]] = matched_by_league.get(item["league"], 0) + int(item["ai_matched"])
        matched = sum(int(item["ai_matched"]) for item in items)
        leagues = {league: {"prediction_count": count, "ai_matched_count": matched_by_league.get(league, 0)} for league, count in sorted(counts.items())}
        return {"available": True, "date": day.isoformat(), "summary": {"prediction_count": len(items), "ai_matched_count": matched, "unmatched_count": len(items) - matched, "leagues": leagues}, "items": items}
    except Exception as exc:
        logger.exception("AI 日报加载异常: %s", exc)
        return {"available": False, "date": day.isoformat(), "reason": f"load_failed: {exc.__class__.__name__}", "summary": {"prediction_count": 0, "ai_matched_count": 0, "unmatched_count": 0, "leagues": {}}, "items": []}


def ai_ranking(day: date, limit: int = 10) -> dict:
    report = ai_daily_report(day)
    definition = "仅 ai_scores.ai_score 数值排序，不代表投注热度"
    if not report.get("available"):
        return {**report, "metric": "ai_score", "definition": definition, "matched_count": 0, "hot": [], "cold": []}
    ranked = [item for item in report["items"] if item["ai_matched"] and isinstance(item.get("ai_score"), (int, float)) and not isinstance(item.get("ai_score"), bool) and math.isfinite(float(item["ai_score"]))]
    key = lambda item: (float(item["ai_score"]), str(item.get("league", "")), str(item.get("match", "")))
    return {"available": True, "date": report["date"], "metric": "ai_score", "definition": definition, "matched_count": len(ranked), "hot": sorted(ranked, key=key, reverse=True)[:limit], "cold": sorted(ranked, key=key)[:limit]}