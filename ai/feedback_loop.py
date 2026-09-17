"""
AI Feedback Loop — bridge between AI enrichment and prediction engine.

Flow:
  Day N:  predict.py → ai_enrich_gha.py → save_ai_scores()
  Day N+1: predict.py → load_ai_scores() → adjust prediction confidence → predict

Usage:
  from ai.feedback_loop import load_ai_adjustments, save_ai_scores
  adjustments = load_ai_adjustments(league_key)
  # pass adjustments into calculate_prediction()
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_SCORES_FILE = REPO_ROOT / "predictions" / "ai_scores.json"


def load_ai_adjustments(league_key: str = "") -> dict[str, dict]:
    """Load AI enrichment scores from previous run.

    Returns dict mapping match name → {"ai_score": int, "ai_summary": str, ...}
    """
    if not AI_SCORES_FILE.exists():
        return {}
    try:
        with open(AI_SCORES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    # Filter by league if specified
    if league_key:
        return {k: v for k, v in data.items() if v.get("league") == league_key}
    return data


def save_ai_scores(enriched_items: list[dict], league_key: str = ""):
    """Save AI enrichment scores for next prediction run.

    Args:
        enriched_items: output from analyse_batch() — **只有带 ai_score 的条目会落盘**；
            LLM 失败或漏配时 analyse_batch 会原样返回未评分条目，此处一律跳过。
        league_key: 单联赛调用时的联赛标识；为空时回退到条目自带的 league/source。
    """
    existing = load_ai_adjustments()
    written = 0
    for item in enriched_items:
        name = item.get("name", "")
        if not name:
            continue
        # Skip mock data
        notes = item.get("ai_notes", "")
        if "mock" in notes:
            continue
        # 未评分条目必须跳过：analyse_batch 在 LLM 调用失败/分析漏配时会原样返回
        # 条目（契约见 test_analyse_batch_missing_analysis_keeps_item_plain）。
        # 若在此处兜底成 50 分，就会把"完全没分析"伪造成"中性 50 分"，而
        # adjust_prediction 的 0.7+0.3*50/100=0.85 会静默削掉 15% 信心。
        if "ai_score" not in item:
            continue
        existing[name] = {
            "ai_score": item["ai_score"],
            "ai_summary": item.get("ai_summary", ""),
            "ai_notes": notes,
            # league_key 为空时回退到条目自带联赛：历史上只读 item["source"]，
            # 而 collect_items() 写的是 item["league"]，导致落盘 league 恒为 ""，
            # 使 ai_daily 的 league 校验（scores[match]["league"] == prediction["league"]）
            # 永假 —— AI 日报与分数榜命中数结构性永久为 0。
            "league": league_key or item.get("league") or item.get("source", ""),
            "source": item.get("source", ""),
        }
        written += 1

    AI_SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AI_SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"[AI Feedback] wrote {written} this run, {len(existing)} total in {AI_SCORES_FILE}")


def adjust_prediction(prediction: dict, ai_adjustments: dict[str, dict]) -> dict:
    """Apply AI enrichment scores to adjust prediction confidence.

    Formula:
      adjusted_confidence = base_confidence * (0.7 + 0.3 * ai_score/100)

    This means:
      ai_score=100 → boost confidence by 30%
      ai_score=50  → no change
      ai_score=0   → reduce confidence by 30%

    Returns adjusted prediction dict (mutated copy).
    """
    match_name = prediction.get("match", "")
    adj = ai_adjustments.get(match_name)
    if not adj:
        return prediction

    ai_score = adj.get("ai_score", 50)
    base_conf = prediction.get("confidence_score", 0.5)

    # Adjustment factor: 0.7 to 1.3 (center at 50)
    factor = 0.7 + 0.3 * (ai_score / 100)
    adjusted_conf = min(base_conf * factor, 1.0)

    prediction["confidence_score"] = round(adjusted_conf, 3)
    prediction["ai_adjusted"] = True
    prediction["ai_score_used"] = ai_score
    prediction["ai_adjustment_factor"] = round(factor, 3)

    # Also adjust stars based on new confidence
    from core.config import THRESHOLDS
    if adjusted_conf >= THRESHOLDS["star_5"]:
        prediction["stars"] = "5-star"
    elif adjusted_conf >= THRESHOLDS["star_4"]:
        prediction["stars"] = "4-star"
    elif adjusted_conf >= THRESHOLDS["star_3"]:
        prediction["stars"] = "3-star"
    elif adjusted_conf >= THRESHOLDS["star_2"]:
        prediction["stars"] = "2-star"
    else:
        prediction["stars"] = "1-star"

    return prediction
