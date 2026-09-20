"""传统足彩（14场/任九）三向概率票面端点：GET/POST /api/v1/ticket/sfc。

读当前/指定一期 fact.jc_issue_match 对阵，用模型 胜平负 概率（reasoning_factors 三向 or
ml_proba）逐场推荐，输出 14场 逐场票面 + 任九(top9) 组合命中概率。

数据来源：
  · 对阵：store.jc_view.issue_matches(game_num=90, issue_no?=最新一期)
  · 模型概率：web.services.store.load_prediction_docs 里命中的预测 entry
    的 reasoning_factors.{home_ml_true_prob,draw_true_prob,away_ml_true_prob}（校准三向）
    或 ml_proba=[home,draw,away]；都不齐 → 该场标 no_probs，不编数。
口径与重现性：
  · 命中概率是独立假设下 P(推荐组全对)，页面标注"独立假设"；不给奖金/金额。
  · 任九 = 按模型概率选命中概率最高的 9 场（官方任九=任选 9 场全中）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from web.auth import require_auth

router = APIRouter(prefix="/api/v1", tags=["ticket"])


def _model_three(e: dict) -> dict | None:
    """从预测 entry 提三向模型概率 {主胜,平,客胜}；三侧都在才算（不归一、不编数）。"""
    rf = (e.get("reasoning_factors") or {})
    h = rf.get("home_ml_true_prob")
    d = rf.get("draw_true_prob")
    a = rf.get("away_ml_true_prob")
    if all(v is not None for v in (h, d, a)) and all(isinstance(v, (int, float)) for v in (h, d, a)):
        return {"主胜": float(h), "平": float(d), "客胜": float(a)}
    ml = e.get("ml_proba")
    if isinstance(ml, list) and len(ml) == 3 and all(isinstance(v, (int, float)) for v in ml):
        return {"主胜": float(ml[0]), "平": float(ml[1]), "客胜": float(ml[2])}
    return None


def _match_probs(home: str, away: str, pred_entries: list) -> dict | None:
    """按 (home,away) 逐预测 entry 找命中，取其模型三向概率。"""
    from web.services.team_match import teams_match
    for e in pred_entries:
        if teams_match(home, away, e.get("home") or "", e.get("away") or ""):
            p = _model_three(e)
            if p:
                return p
    return None


def _all_pred_entries() -> list:
    """全部最新/历史预测文档里的 entry 列表（尽力；异常 → []）。"""
    from web.services import store as web_store
    try:
        docs = web_store.load_prediction_docs()
    except Exception:
        return []
    out: list = []
    for doc in (docs or []):
        out.extend((doc.get("data") or {}).get("predictions", []) or [])
    return out or []


@router.get("/ticket/sfc")
@router.post("/ticket/sfc")
def ticket_sfc(body: dict | None = Body(default=None),
               _: None = Depends(require_auth)) -> dict:
    """当前/指定一期 传统足彩三向票面。body (POST) 或 query (GET)：
    {issue_no?=最新一期, game_num?=90, choose?=9(任九场数)}。返回票面明细 + 命中概率。"""
    payload = body if isinstance(body, dict) else {}

    from web.services.ticket import combine_prob, sfc_picks

    game_num = str(payload.get("game_num", "90"))
    issue_no = payload.get("issue_no") or None
    choose = payload.get("choose", 9)
    try:
        choose = int(choose)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="choose 必须是整数")
    if choose <= 0 or choose > 14:
        raise HTTPException(status_code=400, detail="choose 必须在 [1,14]")

    from store import jc_view
    try:
        matches = jc_view.issue_matches(game_num, issue_no)
    except Exception:
        matches = []
    if not matches:
        return {"available": False, "reason": "no_issue_matches",
                "note": f"传统足彩 {game_num} 期 {issue_no or '最新'} 无对阵数据（fact.jc_issue_match 为空）"}

    actual_issue = matches[0].get("issue_no")
    entries = _all_pred_entries()

    built = []
    for m in matches:
        probs = _match_probs(m.get("home_cn") or m.get("home") or "",
                             m.get("away_cn") or m.get("away") or "", entries)
        built.append({
            "seq": m.get("seq"),
            "home": m.get("home_cn"),
            "away": m.get("away_cn"),
            "league_cn": m.get("league_cn"),
            "is_drawn": m.get("is_drawn"),
            "probs": probs,
        })

    picks = sfc_picks(built)  # 不带 prob 的标 no_probs
    n_ok = sum(1 for p in picks if p["error"] is None)
    combine = None
    if n_ok >= choose:
        try:
            combine = combine_prob(picks, choose)
        except ValueError:
            combine = None

    # 附加官方已开奖信息（若有）：is_drawn 来自对阵表，不随 picks 丢
    drawn = [m for m in built if m.get("is_drawn")]

    return {
        "available": True,
        "game_num": game_num,
        "issue_no": actual_issue,
        "choose": choose,
        "n_matches": len(picks),
        "n_with_probs": n_ok,
        "matches": [
            {k: p.get(k) for k in ("seq", "home", "away", "league_cn", "side", "prob", "error", "probs")}
            for p in picks
        ],
        "renjiu": combine,
        "drawn_count": len(drawn),
        "independent_assumption": True,
        "note": ("推荐 = 每场模型概率最大侧；命中概率为独立假设下推荐组全对概率；"
                 "任九 = 概率最高的 top 平价组合。不给奖金/金额（官方动态）。"),
    }