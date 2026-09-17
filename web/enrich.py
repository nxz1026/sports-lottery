"""web.enrich — AI 摘要富化批处理 CLI（python -m web.enrich），仅 job 子进程使用。"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from web.services import store

LP_AI_PRIORITIES = os.environ.get("LP_AI_PRIORITIES", "")
logger = logging.getLogger(__name__)


def _prediction_time(pred: dict) -> str:
    return str(pred.get("kickoff_utc") or pred.get("kickoff_date") or "")


def _limited_predictions(preds: list[dict], league_key: str) -> list[dict]:
    if any(_prediction_time(pred) for pred in preds):
        preds = sorted(preds, key=lambda pred: _prediction_time(pred) or "\uffff")
    if len(preds) > 40:
        logger.warning("league %s has %d predictions; limiting AI enrichment to 40", league_key, len(preds))
    return preds[:40]


def collect_items() -> list[dict]:
    """从 store 取每联赛最新预测，按开球时间最多取 40 场构造富化项。"""
    by_league = store.latest_by_league()
    items: list[dict] = []
    for league_key, doc in by_league.items():
        preds = _limited_predictions(doc.get("data", {}).get("predictions", []), league_key)
        for pred in preds:
            match = pred.get("match", "")
            items.append({
                "name": match,
                "league": league_key,
                "date_found": "",
                "direction": pred.get("direction", "?"),
                "stars": pred.get("stars", "?"),
                "confidence": pred.get("confidence_score", ""),
            })
    return items


def main() -> int:
    from ai.batch_pipeline import analyse_batch
    from ai.feedback_loop import save_ai_scores

    items = collect_items()
    if not items:
        logger.info("[AI Enrich] no items")
        return 0

    priorities = LP_AI_PRIORITIES.split(",") if LP_AI_PRIORITIES else None
    cfg = {
        "ai": {
            "model": os.environ.get("LLM_MODEL") or "agnes-2.5-flash",
            "batch_size": 5,
            "rate_limit_seconds": 3,
            "min_score": 0,
        },
        "priorities": priorities,
    }

    try:
        enriched = analyse_batch(items, context="", preference_prompt="", config=cfg)
        enriched = [ai for ai in enriched if isinstance(ai, dict)]
        # analyse_batch 在 LLM 调用失败/分析漏配时会原样返回未评分条目（有意契约），
        # 所以 len(enriched) 不等于"真正富化成功"的条数。只有带 ai_score 的才算成功。
        scored = [ai for ai in enriched if "ai_score" in ai]
        save_ai_scores(enriched, league_key="")
        line = "[AI Enrich] processed %d items, wrote back %d" % (len(items), len(scored))
        if not scored:
            # 全部失败：必须响亮报错。历史上这里恒返回 0，任务显示 done，
            # 看板"AI 状态：可用"、条数还因占位记录上涨，用户完全无法察觉。
            logger.error("%s (no item was actually scored; LLM unavailable?)", line)
            sys.stdout.write(line + " — 全部条目未评分，AI 富化失败\n")
            sys.stdout.flush()
            return 1
        logger.info("%s", line)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        logger.error("[AI Enrich] error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
