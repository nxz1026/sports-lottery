"""半全场（haf）一等预测端点：GET/POST /api/v1/haf。

输入：按 对阵(home,away) 或 场次号(match_num) 定位当日预测，取 λh/λa；可选官方让球线 line。
输出：web.services.haf.compute_haf 的九宫格概率 + pick + note。

内部查找复用 web.services.store.latest_by_league + all docs（同 combo），
对账匹配复用 web.services.team_match.teams_match_safe（别名容错）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from web.auth import require_auth

logger = logging.getLogger("web.haf")
router = APIRouter(prefix="/api/v1", tags=["haf"])


def _find_prediction(home: str, away: str):
    """在所有历史/最新预测文档里按 (home,away) 匹配预测 entry；无匹 → None。

    R19 #B（haf 反查哈希化）：原实现每请求遍历所有 doc × preds × teams_match
    模糊匹配 ≈ 1000 次；现走 ``store.pred_index_by_pair()`` 字典命中 O(1)，
    miss 时回退 fuzzy —— 仅遍历 dict 的 values 池（与原 all_docs × preds 一致
    数据量但少了一层嵌套），fuzzy 调用次数同前。
    """
    from web.services import store as web_store
    try:
        idx = web_store.pred_index_by_pair()
    except Exception as exc:  # noqa: BLE001 - store 损坏时返回空 hist
        logger.warning("haf: pred_index_by_pair failed: %s", exc)
        idx = {}
    h = (home or "").strip()
    a = (away or "").strip()
    e = idx.get((h, a))
    if e is not None:
        return e
    # fuzzy 兜底（与原实现同语义：补齐别名差异，如 "曼彻斯特联" / "曼联"）
    if not idx:
        return None
    from web.services.team_match import teams_match
    for entry in idx.values():
        if teams_match(home, away, entry.get("home") or "", entry.get("away") or ""):
            return entry
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
        except Exception as exc:  # noqa: BLE001 - 缺 fixtures 时按 home/away 降级
            logger.warning("haf: fixtures lookup failed: %s", exc)
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
    """半全场九宫格一等预测（GET 版，URL 直传对阵）。

    降级语义（契约 §5.5）：
      - 未找到匹配预测 → 200 + ``{available: False, reason: "no_prediction"}``
        （不 404，避免与"对阵不存在"混淆）。
      - 该场 λ 缺失 → 200 + ``{available: False, reason: "bad_lambda"}``。

    鉴权：``require_auth``（Nginx Basic Auth + 应用 session）。

    Response schema：
      - 200 : ``{available, home, away, line, probs: {hh/hd/hl/dh/dd/dl/ah/al/al: float},
        pick: {result, prob} | None, note: str}``
      - 400 : line 非法（不是数字 / 超范围）
      - 401 : 未登录
    """
    return _haf_calc(home, away, line)


@router.post("/haf")
def haf_post(body: dict = Body(...), _: None = Depends(require_auth)) -> dict:
    """半全场九宫格一等预测（POST 版，支持对阵或场次号反查）。

    降级语义：同 GET（无 404 / 无 500；缺预测或缺 λ 一律 200 + reason）。

    鉴权：``require_auth``（Nginx Basic Auth + 应用 session）。

    Request body 两种形态（择一）：
      - ``{home: str, away: str, line?: float}``
      - ``{match_num: str|int, line?: float}`` —— 用今日 BJT 业务日（``bjt_today``）
        反查五大联赛对阵，避免 ``fixtures_on(None)`` 落到非今日的冷门联赛。

    返回 schema 同 GET 端点；400 出现在 line 非法 / 同时缺 home|away|match_num。
    """
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