"""lottery 四件 + 胆拖 API（GET /api/v1/lottery/*）。

复用 store.jc_view.lottery_draws（数字彩历史开奖）。纯函数能力在 web.services.lottery。
不改动既有 /api/jc/lottery（只读开奖列表）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from web.auth import require_auth
from web.services import lottery as L

router = APIRouter(prefix="/api/v1", tags=["lottery"])


def _nums(v, label):
    if not v:
        raise HTTPException(status_code=400, detail=f"{label} 不能为空")
    parts = str(v).split(",")
    try:
        return [int(x.strip()) for x in parts]
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} 必须逗号分隔的数字")


@router.get("/lottery/prize")
def lottery_prize(game: str = Query(...), ticket: str = Query(...),
                  draw: str = Query(...), _: None = Depends(require_auth)) -> dict:
    """数字彩命中判定（非金额 / 仅等级）。

    降级语义：ticket/draw 为空或非逗号分隔数字 → 400；游戏代码未识别 → 200 + result
    注等级 = None（不抛错）。
    鉴权：``require_auth``。

    Query：
      - game   : 彩种代码（85=大乐透 / 35=排列3 / 350133=排列5 / 04=7星彩）
      - ticket : 玩家号码串（逗号分隔数字）
      - draw   : 当期开奖号码串（逗号分隔数字）

    Response：``{game, ticket, draw, level: str | None, hits: [int], note: str}``
    """
    t = _nums(ticket, "ticket")
    d = _nums(draw, "draw")
    r = L.match_prize(game, t, d)
    return {"game": game, "ticket": t, "draw": d, **r}


@router.get("/lottery/cost")
def lottery_cost(game: str = Query("85"), front: str = Query(""),
                 back: str = Query(""), _: None = Depends(require_auth)) -> dict:
    """复式投注注数 + 金额计算（按彩种规则区分前/后区组合数学）。

    降级语义：front/back 字符串非法 → 400；彩种不识别 → 200 + cost=0 + note 提示。
    鉴权：``require_auth``。

    Response：``{game, cost: {notes: int, per_note: float, total: float}}``
    """
    f = _nums(front, "front") if front else []
    b = _nums(back, "back") if back else []
    return {"game": game, "cost": L.compound_cost(game, {"front": f, "back": b})}


@router.get("/lottery/stats")
def lottery_stats(game: str = Query("35"), n: int = Query(30, ge=1, le=100),
                  _: None = Depends(require_auth)) -> dict:
    """数字彩描述统计（频次 / 奇偶 / 热门号）。

    降级语义：n_periods=0（库内无该彩种历史）→ 200 + stats 空 dict。
    鉴权：``require_auth``。

    Response：``{game, n_periods, stats: {freq: [...], top: [...], parity: {...}}}``
    """
    from store import jc_view
    rows = jc_view.lottery_draws(n)
    history = [[int(x) for x in r.get("numbers_raw", "").split() if x.strip().lstrip("-").isdigit()]
               for r in rows if r.get("game_num") == game]
    return {"game": game, "n_periods": len(history), "stats": L.stats(history)}


@router.get("/lottery/dantuo")
def lottery_dantuo(dan: int = Query(...), tuo: int = Query(...), choose: int = Query(...),
                   _: None = Depends(require_auth)) -> dict:
    """胆拖投注注数计算（组合数学：choose(tuo, choose - dan)）。

    降级语义：参数非法（dan > choose / dan < 1 / choose > tuo+dan）→ 200 +
    result={tickets: 0, note: "invalid"}（前端按 0 注数显示）。
    鉴权：``require_auth``。

    Response：``{result: {tickets: int, combos: [(dan, ...), ...] | None}}``
    """
    return {"result": L.dan_tuo_tickets(dan, tuo, choose)}
