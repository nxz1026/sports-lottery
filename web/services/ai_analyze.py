"""AI 分析生成器（异步 job，python -m web.services.ai_analyze [YYYY-MM-DD]）。

三类内容，互不阻塞、互不污染：
  1) per_match_insight：逐场解读（仅取当天 business_date 五大联赛 + NBA 推荐的场次）
  2) banker_narrative：胆材叙事（从当日全部预测里筛高信心 + 高 EV 候选，给出组合逻辑）
  3) lottery_review：开奖复盘（最近 1 期 super 大乐透 + 排列 3/5 等彩种）

输出：predictions/ai_analysis/{YYYY-MM-DD}.json
  {date, generated_at, classes:{per_match:[...], banker:str, lottery:[...], warning:[...]}}

调用：
  同步：在进程内调 analyze(date_str) -> dict 立即返回结果并落盘
  异步：jobs.py 触发 python -m web.services.ai_analyze YYYY-MM-DD 子进程（与 predict 共享配额与并发守卫）
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("web.services.ai_analyze")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "predictions" / "ai_analysis"

# 复用 core.log 的 logger（与 web 服务一致）
try:
    from core.log import logger as _clogger  # type: ignore
    logger = _clogger.getChild("ai_analyze")
except Exception:
    pass


def _ensure_output_dir() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def _today_bjt() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def _output_path(date_str: str) -> Path:
    return OUTPUT_DIR / f"{date_str}.json"


def _read_predictions() -> dict:
    """复用 web.services.store.latest_by_league 拿到最新预测。"""
    try:
        from web.services import store as web_store
        return web_store.latest_by_league() or {}
    except Exception as e:
        logger.warning("read_predictions failed: %s", e)
        return {}


def _read_fixtures() -> list[dict]:
    try:
        from store import jc_view
        return jc_view.fixtures_on(None) or []
    except Exception as e:
        logger.warning("read_fixtures failed: %s", e)
        return []


def _read_lottery(per_type: int = 5) -> list[dict]:
    try:
        from store import jc_view
        return jc_view.lottery_draws(per_type) or []
    except Exception as e:
        logger.warning("read_lottery failed: %s", e)
        return []


# --- 三类生成 -----------------------------------------------------------

def _class_per_match(predictions: dict, fixtures: list) -> list[dict]:
    """逐场解读：每个 league 选 1~2 条最稳的，给出 narrative + 风险。"""
    out: list[dict] = []
    if not predictions:
        return out
    from ai.llm_client import generate
    for league_key, doc in (predictions or {}).items():
        items = (doc.get("data") or {}).get("predictions", []) or []
        # 选 ★ ≥ 3 的前 2 条；不足则取前 1 条
        items = sorted(items, key=lambda p: -(int(str((p.get("stars") or "0-star")).split("-")[0]) if p.get("stars") else 0))[:2]
        for e in items:
            home = e.get("home") or "?"
            away = e.get("away") or "?"
            direction = e.get("direction") or ""
            score = e.get("predicted_score") or ""
            rf = e.get("reasoning_factors") or {}
            p_home = rf.get("home_ml_true_prob")
            p_draw = rf.get("draw_true_prob")
            p_away = rf.get("away_ml_true_prob")
            prompt = (
                f"你是体彩数据分析师。一句话中文解读（≤60 字）：{home} vs {away}，"
                f"模型方向「{direction}」，预测比分「{score}」，模型胜平负真实概率"
                f"主{p_home} 平{p_draw} 客{p_away}。"
                "要求：客观、含风险提示，禁用收益承诺/投注建议字样。输出 JSON：{\"text\":\"...\"}"
            )
            try:
                resp = generate(prompt)
                text = (resp.get("text") if isinstance(resp, dict) else None) or ""
                if not text:
                    raise RuntimeError("LLM returned empty")
            except Exception as ex:
                logger.warning("per_match LLM failed (%s vs %s): %s", home, away, ex)
                text = f"模型方向「{direction}」、预测比分「{score}」（LLM 解读暂不可用）"
            out.append({
                "league": league_key,
                "match": f"{home} vs {away}",
                "direction": direction,
                "score": score,
                "insight": text[:200],
            })
    return out


def _class_banker(predictions: dict, fixtures: list) -> str:
    """胆材叙事：当日 ≥ 3★ 的高信心候选组合一句话叙事。"""
    from ai.llm_client import generate
    candidates = []
    for league_key, doc in (predictions or {}).items():
        for e in (doc.get("data") or {}).get("predictions", []) or []:
            try:
                star_n = int(str((e.get("stars") or "0-star")).split("-")[0])
            except Exception:
                star_n = 0
            if star_n >= 3:
                candidates.append({
                    "lg": league_key, "match": f"{e.get('home')} vs {e.get('away')}",
                    "dir": e.get("direction") or "",
                    "stars": star_n,
                })
    if not candidates:
        return "今日无 ≥ 3★ 的高信心候选，跳过胆材叙事。"
    prompt = (
        "你是体彩胆材分析师。一句话中文叙事（≤80 字），从下列候选里挑 3~5 场形成「组合理由」：\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\n要求：客观、含风险与独立假设说明（事件独立），禁用收益承诺字样。"
          "输出 JSON：{\"text\":\"...\"}"
    )
    try:
        resp = generate(prompt)
        text = (resp.get("text") if isinstance(resp, dict) else None) or ""
        if not text:
            raise RuntimeError("LLM returned empty")
        return text[:300]
    except Exception as ex:
        logger.warning("banker LLM failed: %s", ex)
        return "LLM 解读暂不可用，请人工核对候选名单：" + "；".join(
            f"{c['lg']} {c['match']} {c['dir']}({c['stars']}★)" for c in candidates[:5])


def _class_lottery(lottery: list[dict]) -> list[dict]:
    """开奖复盘：最近 N 期每彩种一句话叙事。"""
    from ai.llm_client import generate
    out: list[dict] = []
    # 按彩种聚合最近一期
    latest_by_game: dict = {}
    for r in lottery or []:
        gn = str(r.get("game_num") or "")
        if not gn or gn in latest_by_game:
            continue
        latest_by_game[gn] = r
    for gn, r in latest_by_game.items():
        game_name = r.get("game_name") or gn
        issue = r.get("issue_no")
        nums = r.get("numbers_raw") or ""
        prompt = (
            f"你是数字彩复盘分析师。一句话中文复盘（≤60 字）：{game_name} 第 {issue} 期，"
            f"开奖号码「{nums}」。要求：客观陈述事实+形态（连号/区间/冷热），禁用收益承诺。"
            f"输出 JSON：{{\"text\":\"...\"}}"
        )
        try:
            resp = generate(prompt)
            text = (resp.get("text") if isinstance(resp, dict) else None) or ""
            if not text:
                raise RuntimeError("LLM returned empty")
            out.append({"game_num": gn, "game_name": game_name,
                        "issue_no": issue, "numbers_raw": nums,
                        "review": text[:200]})
        except Exception as ex:
            logger.warning("lottery LLM failed (%s): %s", gn, ex)
            out.append({"game_num": gn, "game_name": game_name,
                        "issue_no": issue, "numbers_raw": nums,
                        "review": "LLM 复盘暂不可用，号码原样展示。"})
    return out


# --- 主入口 --------------------------------------------------------------

def analyze(date_str: str | None = None) -> dict:
    """生成三类 AI 分析并落 predictions/ai_analysis/{date}.json。

    返回写入的内容（dict）。任何一类失败不影响其他类。
    """
    target_date = date_str or _today_bjt().isoformat()
    _ensure_output_dir()
    predictions = _read_predictions()
    fixtures = _read_fixtures()
    lottery = _read_lottery(per_type=5)

    warning: list[str] = []
    try:
        per_match = _class_per_match(predictions, fixtures)
    except Exception as e:
        per_match = []
        warning.append(f"per_match 失败：{e}")
    try:
        banker = _class_banker(predictions, fixtures)
    except Exception as e:
        banker = "胆材叙事生成失败"
        warning.append(f"banker 失败：{e}")
    try:
        lottery_out = _class_lottery(lottery)
    except Exception as e:
        lottery_out = []
        warning.append(f"lottery 失败：{e}")

    payload = {
        "date": target_date,
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "classes": {
            "per_match": per_match,
            "banker": banker,
            "lottery": lottery_out,
        },
        "warning": warning,
    }
    out_path = _output_path(target_date)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("ai_analyze wrote %s (per_match=%d banker=%s lottery=%d warning=%d)",
                out_path, len(per_match), bool(banker), len(lottery_out), len(warning))
    return payload


def main() -> int:
    """CLI 入口（异步 job 子进程调用此函数）。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    arg_date = sys.argv[1] if len(sys.argv) > 1 else None
    if arg_date:
        try:
            date.fromisoformat(arg_date)
        except ValueError:
            print(f"非法日期: {arg_date}", file=sys.stderr)
            return 2
    result = analyze(arg_date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
