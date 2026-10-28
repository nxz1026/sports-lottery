"""web.enrich — AI 摘要富化批处理 CLI（python -m web.enrich），仅 job 子进程使用。"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ⚠️ **P1（2026-10-23）**：观测文件名后缀必须是本进程自己的名字 ——
#    `core.log` 是第一个执行 `observability.setup()` 的地方，写死 "predict"
#    ⇒ 本子进程会与 predict 主进程**抢同一个 events 文件**。
os.environ.setdefault("LEAGUE_PROCESS_NAME", "ai_enrich")

from web.services import store

LP_AI_PRIORITIES = os.environ.get("LP_AI_PRIORITIES", "")
# 当日富化进度（断点续跑）：RPM 受限账号一轮要跑几十分钟，作业被超时杀掉后
# 靠它跳过已完成条目，而不是从头再来。
CHECKPOINT_FILE = REPO_ROOT / "predictions" / ".ai_enrich_checkpoint.json"
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
                # 英文原名：AI 分数的稳定主键（见 ai/feedback_loop.py::score_key）。
                # 中文名是 to_cn() 的派生显示值，LLM 会把它"纠正"成别的队
                # （实测 '埃尔切'→'阿根廷'、'勒芒'→'洛森'），不能作匹配键。
                "home_en": pred.get("home_en", ""),
                "away_en": pred.get("away_en", ""),
                "league": league_key,
                "date_found": "",
                "direction": pred.get("direction", "?"),
                "stars": pred.get("stars", "?"),
                "confidence": pred.get("confidence_score", ""),
            })
    return items


def _item_key(item: dict) -> str:
    """与 save_ai_scores 同源的稳定主键（league|home_en|away_en，缺英文名回退中文名）。"""
    from ai.feedback_loop import score_key

    return score_key(item.get("league", ""), item.get("home_en", ""), item.get("away_en", "")) \
        or item.get("name", "")


def _load_checkpoint(today: str) -> set[str]:
    """读当日 checkpoint。日期不符（跨天）一律视为空，避免昨天进度吞掉今天条目。"""
    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if data.get("date") != today:
        return set()
    return {k for k in (data.get("done") or []) if isinstance(k, str)}


def _write_checkpoint(today: str, done: set[str]) -> None:
    """原子写 checkpoint（先写 .tmp 再 replace），进程被 TERM 也不会留半截 JSON。"""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"date": today, "done": sorted(done)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, CHECKPOINT_FILE)


def main() -> int:
    from ai.batch_pipeline import analyse_batch
    from ai.feedback_loop import save_ai_scores

    items = collect_items()
    if not items:
        logger.info("[AI Enrich] no items")
        return 0

    # 未实名账号 RPM 约 1 次/分钟（2026-10-28 实测），所以：串行 + 长间隔定速
    # + 每批 checkpoint。作业被 1800s/5400s 超时杀掉后，下一次触发只补没做完的。
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    done = _load_checkpoint(today)
    pending = [it for it in items if _item_key(it) not in done]
    if not pending:
        line = "[AI Enrich] %s 已完成（checkpoint %d 条），跳过" % (today, len(done))
        logger.info("%s", line)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0
    if len(pending) < len(items):
        logger.info(
            "[AI Enrich] 续跑：checkpoint 已有 %d 条，本次待处理 %d/%d",
            len(done), len(pending), len(items),
        )

    priorities = LP_AI_PRIORITIES.split(",") if LP_AI_PRIORITIES else None
    cfg = {
        "ai": {
            "model": os.environ.get("LLM_MODEL") or "agnes-2.5-flash",
            "batch_size": 5,
            # 定速：低于上游 RPM 上限只会换来越多 429 退避，不会更快。
            "rate_limit_seconds": float(os.environ.get("LP_AI_MIN_INTERVAL_SECONDS", "60")),
            "min_score": 0,
        },
        "priorities": priorities,
    }

    scored_this_run = 0

    def _checkpoint(batch_no: int, batch_total: int, scored_items: list[dict]) -> None:
        nonlocal scored_this_run
        scored = [it for it in scored_items if isinstance(it, dict) and "ai_score" in it]
        if scored:
            save_ai_scores(scored, league_key="")
        for it in scored:
            done.add(_item_key(it))
        scored_this_run += len(scored)
        _write_checkpoint(today, done)
        line = "[AI Enrich] 批次 %d/%d 完成，本轮已评分 %d 条，checkpoint 累计 %d 条" % (
            batch_no, batch_total, scored_this_run, len(done),
        )
        logger.info("%s", line)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    try:
        # 落盘由 _checkpoint 按批完成：即使进程随后被超时杀掉，已完成的批次也在。
        # analyse_batch 在 LLM 失败/漏配时会原样返回未评分条目（有意契约），
        # 只有带 ai_score 的才计入 done。
        result = analyse_batch(
            pending, context="", preference_prompt="", config=cfg, on_batch=_checkpoint,
        )
        # 兜底对账：回调是主路径，这里再按返回值补一次，覆盖 on_batch 未被调用的
        # 实现（含测试替身/未来改动），避免"跑完了却一条没落盘"。
        fresh = [
            it for it in (result or [])
            if isinstance(it, dict) and "ai_score" in it and _item_key(it) not in done
        ]
        if fresh:
            save_ai_scores(fresh, league_key="")
            for it in fresh:
                done.add(_item_key(it))
            scored_this_run += len(fresh)
            _write_checkpoint(today, done)
        remaining = [it for it in pending if _item_key(it) not in done]
        line = "[AI Enrich] processed %d items, wrote back %d (累计 %d/%d)" % (
            len(pending), scored_this_run, len(done), len(items),
        )
        if not scored_this_run:
            # 全部失败：必须响亮报错。历史上这里恒返回 0，任务显示 done，
            # 看板"AI 状态：可用"、条数还因占位记录上涨，用户完全无法察觉。
            logger.error("%s (no item was actually scored; LLM unavailable?)", line)
            sys.stdout.write(line + " — 全部条目未评分，AI 富化失败\n")
            sys.stdout.flush()
            return 1
        if remaining:
            logger.warning(
                "[AI Enrich] 仍有 %d 条未评分，下次触发按 checkpoint 续跑", len(remaining),
            )
        logger.info("%s", line)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0
    except Exception as exc:
        logger.error("[AI Enrich] error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
