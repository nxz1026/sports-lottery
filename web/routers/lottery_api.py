"""lottery 四件 + 胆拖 API（GET /api/v1/lottery/*）。

复用 store.jc_view.lottery_draws（数字彩历史开奖）。纯函数能力在 web.services.lottery。
不改动既有 /api/jc/lottery（只读开奖列表）。

建议 6 追加两个**只读**端点（老端点与老字段一字未动）：
  GET /api/v1/lottery/omission —— 遗漏值/热号/冷号（按「期」计，⚠️ 非按天）
  GET /api/v1/lottery/splits   —— 复式/拆单式/胆拖 拆法对比（注数·成本·出票张数·覆盖）
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


# ---------- 建议 6：体彩侧独立小产品（遗漏值 / 拆法对比） ----------
def _history_asc(rows: list[dict], game: str) -> list[list]:
    """取某彩种的开奖号码，按期**升序**（遗漏值对顺序敏感，必须升序）。

    store.lottery_draws 返回的是 issue_no **降序**（最新在前）⇒ 这里按
    (draw_date, issue_no) 重新升序排。纯函数层不做排序。
    """
    out = []
    for r in rows:
        if r.get("game_num") != game:
            continue
        nums = [int(x) for x in str(r.get("numbers_raw") or "").split()
                if x.strip().lstrip("-").isdigit()]
        if not nums:
            continue
        out.append((str(r.get("draw_date") or ""), str(r.get("issue_no") or ""), nums))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


@router.get("/lottery/omission")
def lottery_omission(game: str = Query("85"), n: int = Query(100, ge=1, le=100),
                     _: None = Depends(require_auth)) -> dict:
    """遗漏值 / 热号 / 冷号 统计（**只做历史事实呈现，不做选号建议**）。

    降级语义：库内无该彩种历史 → 200 + n_periods=0 + pools=[]（不 5xx）；
    彩种代码未识别 → 200 + note="unknown_game"。
    鉴权：``require_auth``。

    ⚠️ 口径：**按「期」计不是按「天」计**（大乐透 2~3 天一期、7星彩 2 天一期）。
       cur=距上次出现后未出现的期数；avg=总期数÷出现次数=1/p；max=最长连续未出现期数。

    Response：``{game, game_name, n_periods, unit, definition, pools: [...], note}``
    """
    from store import jc_view
    rows = jc_view.lottery_draws(n)
    history = _history_asc(rows, game)
    return L.omission_report(game, history)


@router.get("/lottery/splits")
def lottery_splits(game: str = Query("85"), front: str = Query(""),
                   back: str = Query(""), _: None = Depends(require_auth)) -> dict:
    """复式 / 拆单式 / 胆拖 拆法对比（注数·成本·出票张数·覆盖组合）。

    降级语义：front/back 非逗号分隔数字 → 400；选号不足以成注 → 200 + rows=[] + note；
    彩种非大乐透（排列/星彩只有直选）→ 200 + rows=[] + note。
    鉴权：``require_auth``。

    恒等式：拆单式与复式覆盖同一批组合 ⇒ 注数与成本必然相同，差别只是出票张数；
    胆拖注数更少但覆盖是复式的子集（胆码须全中）⇒ 不是等价替换。

    Response：``{game, game_name, front_count, back_count, formula, rows: [...], note}``
    """
    f = _nums(front, "front") if front else []
    b = _nums(back, "back") if back else []
    return L.compound_splits(game, f, b)
