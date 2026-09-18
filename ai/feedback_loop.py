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


def score_key(league: str, home_en: str, away_en: str) -> str:
    """AI 分数的稳定主键：``联赛|主队英文原名|客队英文原名``。

    为什么不用中文名（旧键）当主键：中文名是 ``core.i18n.to_cn()`` 的**派生显示值**，
    由 LLM 翻译 + 缓存生成；而同一个 LLM 在富化时会把这些译名"纠正"成别的队
    （实测 '西班牙人 vs 埃尔切' → '西班牙人 vs 阿根廷'、'勒芒 vs 里昂' → '洛森 vs
    里昂'、'法兰克福 vs 弗赖堡' → '法兰克福 vs 德累斯顿'，21 条仅 14 条精确照抄），
    导致按名字查找时静默丢分。英文原名是数据源的原始标识符，且模型对其先验强、照抄
    准确（实测 5/5）。

    英文名缺失时返回 ""，调用方回退到旧的中文名键，保证历史数据仍可读。
    """
    home_en = (home_en or "").strip()
    away_en = (away_en or "").strip()
    if not home_en or not away_en:
        return ""
    return f"{(league or '').strip()}|{home_en}|{away_en}"


def load_ai_adjustments(league_key: str = "") -> dict[str, dict]:
    """Load AI enrichment scores from previous run.

    Returns dict mapping 稳定主键（或历史中文名键）→ {"ai_score": int, ...}
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
        league = league_key or item.get("league") or item.get("source", "")
        # 稳定主键优先：league|home_en|away_en。英文名缺失时回退旧的中文名键，
        # 保证没有 home_en 的历史条目仍能落盘（读侧两种键都查）。
        key = score_key(league, item.get("home_en", ""), item.get("away_en", "")) or name
        if not key:
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
        existing[key] = {
            "ai_score": item["ai_score"],
            "ai_summary": item.get("ai_summary", ""),
            "ai_notes": notes,
            # league_key 为空时回退到条目自带联赛：历史上只读 item["source"]，
            # 而 collect_items() 写的是 item["league"]，导致落盘 league 恒为 ""，
            # 使 ai_daily 的 league 校验（scores[match]["league"] == prediction["league"]）
            # 永假 —— AI 日报与分数榜命中数结构性永久为 0。
            "league": league,
            "source": item.get("source", ""),
            # 保留中文名供显示与排错（它不再是主键）。
            "name": name,
            "home_en": item.get("home_en", ""),
            "away_en": item.get("away_en", ""),
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
    # 稳定主键优先（league|home_en|away_en），回退历史中文名键。
    adj = None
    key = score_key(prediction.get("league", ""), prediction.get("home_en", ""), prediction.get("away_en", ""))
    for candidate in (key, prediction.get("match", "")):
        if candidate and candidate in ai_adjustments:
            adj = ai_adjustments[candidate]
            break
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
