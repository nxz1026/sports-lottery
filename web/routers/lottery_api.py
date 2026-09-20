"""lottery 四件 + 胆拖 API（GET /api/v1/lottery/*）。

复用 store.jc_view.lottery_draws（数字彩历史开奖）。纯函数能力在 web.services.lottery。
不改动既有 /api/jc/lottery（只读开奖列表）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
    t = _nums(ticket, "ticket")
    d = _nums(draw, "draw")
    r = L.match_prize(game, t, d)
    return {"game": game, "ticket": t, "draw": d, **r}


@router.get("/lottery/cost")
def lottery_cost(game: str = Query("85"), front: str = Query(""),
                 back: str = Query(""), _: None = Depends(require_auth)) -> dict:
    f = _nums(front, "front") if front else []
    b = _nums(back, "back") if back else []
    return {"game": game, "cost": L.compound_cost(game, {"front": f, "back": b})}


@router.get("/lottery/stats")
def lottery_stats(game: str = Query("35"), n: int = Query(30, ge=1, le=100),
                  _: None = Depends(require_auth)) -> dict:
    from store import jc_view
    rows = jc_view.lottery_draws(n)
    history = [[int(x) for x in r.get("numbers_raw", "").split() if x.strip().lstrip("-").isdigit()]
               for r in rows if r.get("game_num") == game]
    return {"game": game, "n_periods": len(history), "stats": L.stats(history)}


@router.get("/lottery/dantuo")
def lottery_dantuo(dan: int = Query(...), tuo: int = Query(...), choose: int = Query(...),
                   _: None = Depends(require_auth)) -> dict:
    return {"result": L.dan_tuo_tickets(dan, tuo, choose)}