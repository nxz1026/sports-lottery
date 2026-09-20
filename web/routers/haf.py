"""半全场（haf）一等预测端点：GET/POST /api/v1/haf。

输入：按 对阵(home,away) 或 场次号(match_num) 定位当日预测，取 λh/λa；可选官方让球线 line。
输出：web.services.haf.compute_haf 的九宫格概率 + pick + note。

内部查找复用 web.services.store.latest_by_league + all docs（同 combo），
对账匹配复用 web.services.team_match.teams_match_safe（别名容错）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from web.auth import require_auth

router = APIRouter(prefix="/api/v1", tags=["haf"])


def _find_prediction(home: str, away: str):
    """在所有历史/最新预测文档里按 (home,away) 匹配预测 entry；无匹 → None。"""
    from web.services import store as web_store
    try:
        all_docs = web_store.load_prediction_docs()
    except Exception:
        all_docs = []
    from web.services.team_match import teams_match
    for doc in (all_docs or []):
        preds = (doc.get("data") or {}).get("predictions", []) or []
        for e in preds:
            if teams_match(home, away, e.get("home") or "", e.get("away") or ""):
                return e
    return None


def _resolve_match(payload: dict) -> tuple[str, str, float]:
    """从 payload 解析 (home, away, line)。返回 home,away（可能空），line（float 或 None→0）。
    支持 {home,away,line?} 或 {match_num, line?}（match_num 需反查对阵）。"""
    home = payload.get("home") or ""
    away = payload.get("away") or ""
    line = payload.get("line", 0)
    try:
        line_f = float(line if line not in (None, "") else 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="line 必须是数字")
    if line_f < -10 or line_f > 10:
        raise HTTPException(status_code=400, detail="line 超出 [-10,10]")
    # 场次号反查（用今天 BJT 业务日，而非 latest business_date——避免非五大联赛日期覆盖）
    if (not home or not away) and payload.get("match_num") is not None:
        try:
            from store import jc_view
            from web.services import store as web_store
            today = web_store.bjt_today().isoformat()
            fixtures = jc_view.fixtures_on(today) or []
        except Exception:
            fixtures = []
        for f in fixtures:
            if str(f.get("match_num")) == str(payload.get("match_num")):
                home = home or f.get("home_cn") or ""
                away = away or f.get("away_cn") or ""
                break
    if not home or not away:
        raise HTTPException(status_code=400, detail="需提供 home/away 或 match_num")
    return home, away, line_f


@router.get("/haf")
def haf_get(home: str = "", away: str = "", line: float = 0.0,
            _: None = Depends(require_auth)) -> dict:
    return _haf_calc(home, away, line)


@router.post("/haf")
def haf_post(body: dict = Body(...), _: None = Depends(require_auth)) -> dict:
    home, away, line = _resolve_match(body)
    return _haf_calc(home, away, line)


def _haf_calc(home: str, away: str, line: float) -> dict:
    from web.services.haf import compute_haf
    e = _find_prediction(home, away)
    if e is None:
        return {"available": False, "reason": "no_prediction",
                "home": home, "away": away, "line": line,
                "probs": {}, "pick": None, "note": "未找到该对阵的模型预测"}
    out = compute_haf(e.get("lambda_home"), e.get("lambda_away"), line)
    if out is None:
        return {"available": False, "reason": "bad_lambda",
                "home": home, "away": away, "line": line,
                "probs": {}, "pick": None, "note": "该场 λ 缺失，无法计算"}
    return {"available": True, "home": home, "away": away, "line": line,
            **out}