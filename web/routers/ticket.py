"""传统足彩（14场/任九）三向概率票面端点：GET/POST /api/v1/ticket/sfc。

读当前/指定一期 fact.jc_issue_match 对阵，用模型 胜平负 概率（reasoning_factors 三向 or
ml_proba）逐场推荐，输出 14场 逐场票面 + 任九(top9) 组合命中概率。

数据来源：
  · 对阵：store.jc_view.issue_matches(game_num=90, issue_no?=最新一期)
  · 模型概率：web.services.store.pred_index_by_pair 里命中 (home,away) 的预测 entry
    的 reasoning_factors.{home_ml_true_prob,draw_true_prob,away_ml_true_prob}（校准三向）
    或 ml_proba=[home,draw,away]；都不齐 → 该场标 no_probs，不编数。
口径与重现性：
  · 命中概率是独立假设下 P(推荐组全对)，页面标注"独立假设"；不给奖金/金额。
  · 任九 = 按模型概率选命中概率最高的 9 场（官方任九=任选 9 场全中）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException

from web.auth import require_auth

# scripts/（core 纯函数，如 core.model_probs —— 「模型概率唯一真源」）供本模块复用：
# 追加到 sys.path **末尾**（不前置），避免 scripts/ 下同名包遮蔽 web/、store/
# （与 web/api.py 的做法一致）。自举 ⇒ 本模块与导入顺序无关（R47 同款加固）。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

logger = logging.getLogger("web.ticket")

router = APIRouter(prefix="/api/v1", tags=["ticket"])


def _model_three(e: dict) -> dict | None:
    """从预测 entry 提三向模型概率 {主胜,平,客胜}；三侧都在才算（不归一、不编数）。

    P0（2026-10-23）：改走**模型概率唯一真源** ``core.model_probs``。
    ⚠️ 旧实现读 ``reasoning_factors.{home_ml_true_prob, draw_true_prob, away_ml_true_prob}``
    —— 其中 ``draw_ml_true_prob`` 这个键**根本不存在** ⇒ 平局腿实际取到的是
    ``draw_true_prob``（**市场先验**）；又回退到 ``ml_proba``（ML 分类器原始输出，
    强弱悬殊的场次会塌成 [0.978, 0.008, 0.014]）。
    ⇒ 票面的三向概率与推荐单/卡片不同源、且平局腿是市场价 ⇒ 命中概率失真。
    """
    from core.model_probs import resolve_model_probs

    probs = resolve_model_probs(e)
    if not probs:
        return None
    return {"主胜": probs["home"], "平": probs["draw"], "客胜": probs["away"]}


def _match_probs(home: str, away: str, pred_by_pair: dict) -> dict | None:
    """按 (home,away) 反查字典（命中 + miss fuzzy 兜底）取模型三向概率。

    R20 #t：原实现 14 场 × all entries fuzzy 遍历；现与 haf / combo /
    build_rows 同源复用 store.pred_index_by_pair()（mtime LRU 失效缓存）
    单一入口，命中 O(1)，miss 调 teams_match_safe 兜底。
    """
    if not pred_by_pair:
        return None
    h = (home or "").strip()
    a = (away or "").strip()
    e = pred_by_pair.get((h, a))
    if e is None:
        from web.services.team_match import teams_match_safe
        for cand in pred_by_pair.values():
            if teams_match_safe(home, away, cand.get("home") or "", cand.get("away") or ""):
                e = cand
                break
    if e is None:
        return None
    return _model_three(e)


def _pred_by_pair() -> dict:
    """ticket 端反查字典；异常 → {}，与 haf / combo 等同源"""
    from web.services import store as web_store
    try:
        return web_store.pred_index_by_pair()
    except Exception as exc:  # noqa: BLE001 - store 损坏时返回空 dict
        logger.warning("ticket: pred_index_by_pair failed: %s", exc)
        return {}


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
    except Exception as exc:  # noqa: BLE001 - issue 数据缺失时降级为无对阵
        logger.warning("ticket: issue_matches failed (%s/%s): %s", game_num, issue_no, exc)
        matches = []
    if not matches:
        return {"available": False, "reason": "no_issue_matches",
                "note": f"传统足彩 {game_num} 期 {issue_no or '最新'} 无对阵数据（fact.jc_issue_match 为空）"}

    actual_issue = matches[0].get("issue_no")
    pred_by_pair = _pred_by_pair()

    built = []
    for m in matches:
        probs = _match_probs(m.get("home_cn") or m.get("home") or "",
                             m.get("away_cn") or m.get("away") or "", pred_by_pair)
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
