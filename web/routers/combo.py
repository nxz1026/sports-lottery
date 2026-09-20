"""combo EV 引擎端点（POST /api/v1/combo/eval）。

路由层做：
  1) 装配：从 fixtures_on 拿竞彩官方赔率，从 latest_by_league 拿 reasoning_factors 模型概率
  2) 调纯函数 web.services.combo.evaluate_combo
  3) 响应封装 + auth

支持两种入参（兼容前端两种场景）：
  A) {legs: [{side, odds, p_model}, ...]}   直接传已知赔率/概率（不查 store）
  B) {selections: [{match_num, side}, ...]}  按竞彩场次号自动装配赔率与模型概率

入参超过 32 拒绝；任意单腿 odds<=1 或 p_model 缺失 → 评估模块降级为 warning。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from web.auth import require_auth
from web.services import combo as combo_mod

router = APIRouter(prefix="/api/v1", tags=["combo"])


_SIDE_KEY = {"主胜": "h", "平": "d", "客胜": "a",
             "让胜": "h", "让平": "d", "让负": "a",
             "home": "h", "draw": "d", "away": "a"}


def _assemble_legs(selections: list[dict]) -> list[dict]:
    """按 home/away 自动从竞彩官方盘口 + 模型 reasoning_factors 装配 odds/p_model。

    复用 store.jc_view.fixtures_on（盘口）与 web.services.store.latest_by_league（预测）。
    单腿装配失败 → 返回的 legs 含 odds=0/p_model=0，由 evaluate_combo 计入 warning。
    入参每项必含 home/away/side；兼容旧 match_num（按 match_num 反查 home/away）。
    """
    from store import jc_view
    from web.services import store as web_store
    try:
        fixtures = jc_view.fixtures_on(None) or []
    except Exception:
        fixtures = []

    # 把 fixture 按 (home_cn, away_cn) 索引（容错 teams_match）
    fix_by_pair: dict = {}
    for f in fixtures:
        h = (f.get("home_cn") or "").strip()
        a = (f.get("away_cn") or "").strip()
        if not h or not a:
            continue
        fix_by_pair.setdefault((h, a), {"home_cn": h, "away_cn": a,
                                        "plays": {}})
        m = fix_by_pair[(h, a)]
        pt = f.get("play_type")
        if pt and pt not in m["plays"]:
            m["plays"][pt] = f.get("options") or {}

    latest = web_store.latest_by_league()
    try:
        all_docs = web_store.load_prediction_docs()
    except Exception:
        all_docs = []
    # 模型概率从所有历史预测文件里匹配（latest 偶有当日空文档，回退到最近一份非空）
    pred_index: list[dict] = []
    for doc in (all_docs or []):
        for e in (doc.get("data") or {}).get("predictions", []) or []:
            pred_index.append(e)

    def _find_fix(home: str, away: str):
        h, a = (home or "").strip(), (away or "").strip()
        m = fix_by_pair.get((h, a))
        if m:
            return m
        for (fh, fa), fm in fix_by_pair.items():
            if teams_match_safe(h, a, fh, fa):
                return fm
        return None

    out: list[dict] = []
    for sel in selections:
        side = str(sel.get("side") or "")
        home = sel.get("home") or ""
        away = sel.get("away") or ""
        # 旧 match_num 入参：按 match_num 反查（fixture 列表里）
        if (not home or not away) and sel.get("match_num") is not None:
            for f in fixtures:
                if str(f.get("match_num")) == str(sel.get("match_num")):
                    home = home or f.get("home_cn") or ""
                    away = away or f.get("away_cn") or ""
                    break
        key = _SIDE_KEY.get(side, side.lower() if isinstance(side, str) else "")
        odds = 0.0
        m = _find_fix(home, away)
        if m:
            opts = (m["plays"] or {}).get("had") or (m["plays"] or {}).get("hhad") or {}
            try:
                odds = float(opts.get(key) or 0.0)
            except (TypeError, ValueError):
                odds = 0.0
        p_model = 0.0
        if m and (m.get("home_cn") or m.get("away_cn")):
            for e in pred_index:
                if teams_match_safe(home, away, e.get("home") or "", e.get("away") or ""):
                    rf = e.get("reasoning_factors") or {}
                    p_field = {"h": "home_ml_true_prob", "d": "draw_true_prob",
                               "a": "away_ml_true_prob"}.get(key)
                    if p_field:
                        try:
                            p_model = float(rf.get(p_field) or 0.0)
                        except (TypeError, ValueError):
                            p_model = 0.0
                    break
        out.append({"side": side, "odds": odds, "p_model": p_model})
    return out


def teams_match_safe(h1: str, a1: str, h2: str, a2: str) -> bool:
    """复用 web.services.team_match.teams_match（容错别名匹配），异常时退化为字符串相等。"""
    try:
        from web.services.team_match import teams_match as _tm
        return _tm(h1, a1, h2, a2)
    except Exception:
        return (h1.strip() == h2.strip() and a1.strip() == a2.strip())


@router.post("/combo/eval")
def combo_eval(request: Request,
               payload: dict = Body(...),
               _: None = Depends(require_auth)) -> dict:
    """计算串关 EV / Kelly / 联合 EV / 假设性回报。

    入参 payload 支持两种形状：
      1) legs: list[{side, odds, p_model}] 直接算
      2) selections: list[{match_num, side}] 按竞彩场次号自动装配
    joint_prob / stake 同 web.services.combo.evaluate_combo。
    """
    legs = payload.get("legs")
    if not isinstance(legs, list):
        selections = payload.get("selections")
        if not isinstance(selections, list):
            raise HTTPException(status_code=400,
                                detail="legs 或 selections 必须是数组")
        if len(selections) > 32:
            raise HTTPException(status_code=400, detail="selections 最多 32 条")
        legs = _assemble_legs(selections)
    elif len(legs) > 32:
        raise HTTPException(status_code=400, detail="legs 最多 32 条")

    joint_prob = payload.get("joint_prob")
    if joint_prob is not None:
        try:
            jp = float(joint_prob)
            if jp < 0.0 or jp > 1.0:
                raise HTTPException(status_code=400, detail="joint_prob 必须在 [0, 1]")
            joint_prob = jp
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="joint_prob 必须是数字")
    try:
        stake = float(payload.get("stake", 2.0))
    except (TypeError, ValueError):
        stake = 2.0
    if stake <= 0:
        stake = 2.0
    return combo_mod.evaluate_combo(legs, joint_prob=joint_prob, stake=stake)
