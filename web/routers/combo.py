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

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from web.auth import require_auth
from web.services import combo as combo_mod

logger = logging.getLogger("web.combo")
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
    except Exception as exc:  # noqa: BLE001 - 装配容错（fixtures 缺失不影响其它腿）
        logger.warning("combo: jc_view.fixtures_on failed: %s", exc)
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

    try:
        pred_by_pair = web_store.pred_index_by_pair()
    except Exception as exc:  # noqa: BLE001 - 模型概率缺失时降级为无 p_model 模式
        logger.warning("combo: pred_index_by_pair failed: %s", exc)
        pred_by_pair = {}

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
            # R19 #C（combo 反查哈希化）：O(N×M) fuzzy → O(1) dict 命中 + miss fuzzy 兜底。
            entry = pred_by_pair.get(((home or "").strip(), (away or "").strip()))
            if entry is None and pred_by_pair:
                # fuzzy 兜底（与原 while-loop 同样语义：补齐中英文别名差异）
                from web.services.team_match import teams_match_safe as _tms
                for cand in pred_by_pair.values():
                    if _tms(home, away, cand.get("home") or "", cand.get("away") or ""):
                        entry = cand
                        break
            if entry is not None:
                rf = entry.get("reasoning_factors") or {}
                p_field = {"h": "home_ml_true_prob", "d": "draw_true_prob",
                           "a": "away_ml_true_prob"}.get(key)
                if p_field:
                    try:
                        p_model = float(rf.get(p_field) or 0.0)
                    except (TypeError, ValueError):
                        p_model = 0.0
        out.append({"side": side, "odds": odds, "p_model": p_model})
    return out


def teams_match_safe(h1: str, a1: str, h2: str, a2: str) -> bool:  # noqa: D401
    """R19 #G1 收口 thin wrapper：复用 ``web.services.team_match.teams_match_safe``。

    仅保留路由层公共符号，便于单元测试与既有
    ``monkeypatch.setattr(combo_mod, "teams_match_safe", ...)`` 调用稳定。
    """
    from web.services.team_match import teams_match_safe as _tms
    return _tms(h1, a1, h2, a2)


@router.post("/combo/eval")
def combo_eval(request: Request,
               payload: dict = Body(...),
               _: None = Depends(require_auth)) -> dict:
    """串关 EV / Kelly / 联合 EV / 假设性回报（POST，纯函数 + 持久化轮询入口）。

    降级语义：
      - payload 缺 ``legs`` 与 ``selections`` 之一 → 400（HTTPException）。
      - selections > 32 条 → 400（防组合爆炸）。
      - 单腿 ``odds <= 1`` 或 ``p_model`` 缺失：忽略计入 warning（不算"不编数"，
        是"信息不足"）；warning 数组原样回传供 UI 显示。
      - joint_prob > 1：截断告警；负值 / NaN 钳制为 0。
      - 任一异常：包到 500（接口纯计算，无外部 IO 失败预期）。

    鉴权：``require_auth``（Nginx Basic Auth + 应用 session）。

    Request payload 两种形态：
      1) ``legs: list[{side, odds, p_model, p_model_join?}]`` — 直接算
      2) ``selections: list[{match_num, side}]`` — 按竞彩场次号自动装配 odds / p_model
         （当业务日无该场次 → warning 提示，不 raise）。

    Response schema：
      - 200 : ``{ev, kelly_fraction, joint_prob, fair_odds, hypothetical_return,
        warnings: [str], legs: [{side, odds, p_model, ev_individual, kelly_individual}]}``
      - 400 : legs/selections 缺 / 超 32
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
