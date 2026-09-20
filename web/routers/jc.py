"""web.routers.jc — 竞彩看板只读 API（全部 require_auth，P0-DASH1b）。

端点（数据源：store.jc_view，Postgres ro 只读；store 层任何异常自行降级为 []）：
- GET /api/jc/fixtures   场次 × 玩法最新盘口快照（day 缺省 = 最新 business_date）
- GET /api/jc/issues     传统足彩期头 + 开奖（limit 1..200，越界 400）
- GET /api/jc/backtest   多分类 Brier / argmax 命中率基线口径聚合（每玩法一行）

每端点固定返回 {"rows": [...], "sql_note": ..., "degraded": False}：
口径"该不该有数据"归页面/DASH2 判定，本层不发明健康判定（degraded 恒 False）。
本文件不得出现 psycopg；端点内延迟 `from store import jc_view`（顶层不 import，
T5 免 psycopg 环境）。web 启动带 PYTHONPATH=.（无 scripts/），故模块级自举一次。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from web.auth import require_auth

router = APIRouter(prefix="/api/jc", tags=["jc"])
# Stable v1 alias for consumers that do not use the /api/jc dashboard namespace.
v1_router = APIRouter(prefix="/api/v1", tags=["backtest"])

# T3 启动链 `PYTHONPATH=. uvicorn web.api:app` 不含 scripts/：自举一次，端点才可 import store。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# 用户定范围＝NBA + 五大联赛：每日一图足球表只保留这五家（league_cn 为 fact.jc_match 全名）。
_BIG5 = {
    "英格兰超级联赛", "西班牙甲级联赛", "意大利甲级联赛",
    "德国甲级联赛", "法国甲级联赛",
}

# league_cn 全名 → 预测文件名里的联赛键，供命中率对账（core.backtest）。
_BIG5_KEYS = {
    "英格兰超级联赛": "epl",
    "西班牙甲级联赛": "laliga",
    "意大利甲级联赛": "seriea",
    "德国甲级联赛": "bundesliga",
    "法国甲级联赛": "ligue1",
}


def _hit_stats() -> dict:
    """五大联赛真实方向命中率：从历史预测文件实时对账已结算赛果。

    reconciled=0（无已结算样本）→ available=False，页面显示"数据积累中"，绝不编数。
    优先 30d 窗口（样本更大），空则退回 7d。
    """
    n = hit = 0
    try:
        from core.backtest import league_accuracy
        for lk in _BIG5_KEYS.values():
            for days in (30, 7):
                acc = league_accuracy(lk, days=days)
                if acc and acc.get("reconciled"):
                    n += int(acc["reconciled"])
                    hit += int(round(acc["reconciled"] * float(acc.get("direction_accuracy") or 0)))
                    break
    except Exception:
        pass
    if n > 0:
        return {"available": True, "n": n, "hit": hit, "note": ""}
    return {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}

_NOTES = {
    "fixtures": "每场 5 行（had/hhad/crs/ttg/haf），盘口取该 (场,玩法) 最新一版 snap_ts",
    "issues": "传统足彩期头 + 开奖；head_source='jc_issue_result' 表示期头是合成的",
    "backtest": "多分类 Brier：先按 (场,玩法) 加总各选项平方误差，再对场取平均；acc=argmax 命中率；与已发布基线同式",
    "lottery": "各彩种最近 N 期，按 (彩种, 期号降序)；号码串原样返回（numbers_raw），解析层不重排不去重",
}


def _envelope(kind: str, rows: list) -> dict:
    return {"rows": rows, "sql_note": _NOTES[kind], "degraded": False}


@router.get("/fixtures")
def fixtures(day: str | None = None, _: None = Depends(require_auth)) -> dict:
    """场次 × 玩法盘口；day 非法直接 400（DASH2-D：脏参数不穿到 store 层，不静默降级成空表）。"""
    if day is not None:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(status_code=400, detail="day must be an ISO date like 2026-09-16")
    from store import jc_view
    return _envelope("fixtures", jc_view.fixtures_on(day))


@router.get("/issues")
def issues(limit: str = "20", _: None = Depends(require_auth)) -> dict:
    """期次 + 开奖；limit 按原始字符串校验（非数字/<1/>200 均 400，脏参数不穿到 store 层）。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer 1..200")
    if n < 1 or n > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    from store import jc_view
    return _envelope("issues", jc_view.issues(n))


@router.get("/backtest")
def backtest(_: None = Depends(require_auth)) -> dict:
    """基线 Brier/log-loss/hit-rate 聚合（每玩法一行）；异常安全降级为 []。"""
    from store import jc_view
    return _envelope("backtest", jc_view.backtest_summary())


@v1_router.get("/backtest")
def backtest_v1(_: None = Depends(require_auth)) -> dict:
    """Authenticated compatibility alias for the existing JC backtest summary."""
    from store import jc_view
    return _envelope("backtest", jc_view.backtest_summary())


@router.get("/lottery")
def lottery(per_type: str = "20", _: None = Depends(require_auth)) -> dict:
    """各彩种最近 N 期开奖（超级大乐透/排列3/排列5/7星彩）；异常安全降级为 []。

    彩种清单来自采集端 lottery_draw 主题，解析层无白名单 —— 采集端补采新彩种后
    本端点自动带出，无需改代码。
    """
    try:
        n = int(per_type)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="per_type must be an integer 1..100")
    if n < 1 or n > 100:
        raise HTTPException(status_code=400, detail="per_type must be 1..100")
    from store import jc_view
    return _envelope("lottery", jc_view.lottery_draws(n))


@router.get("/daily-image")
def daily_image(sport: str = "football", _: None = Depends(require_auth)) -> dict:
    """每日一图数据（拆分 sport）。

    sport=football（默认）：竞彩足球五大联赛在售 × 模型算法层预判（只读合并）。
    sport=basketball：NBA 预测表；休赛期（最近一场开球 >7 天外）返回 available=False 空态，
      不把赛程预估当"每日推荐"（NBA 未开赛 / 竞彩篮球未开售）。
    """
    from store import jc_view
    from web.services.team_match import build_rows, nba_rows
    from web.services import store as web_store

    latest = web_store.latest_by_league()
    today = web_store.bjt_today()

    if sport == "basketball":
        nba_predictions = (latest.get("nba") or {}).get("data", {}).get("predictions", [])
        nba = nba_rows(nba_predictions)
        if not _nba_available(nba_predictions, today):
            return {
                "sport": "basketball", "available": False, "rows": [],
                "date": today.isoformat(),
                "note": "NBA 2026-27 赛季尚未开赛，篮球每日一图暂未生成（预计 10 月中下旬开赛）。",
                "stats": {"scope": "NBA", "on_sale": 0, "recommend": 0, "nba": 0,
                          "hit": {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}},
            }
        return {
            "sport": "basketball", "available": True, "rows": nba,
            "date": today.isoformat(),
            "stats": {"scope": "NBA", "on_sale": len(nba), "recommend": len(nba), "nba": len(nba),
                      "hit": {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}},
        }

    # sport == "football"（默认）
    # 用今天（BJT）作 day 查所有场次——不查 latest business_date，避免被非五大联赛日期
    # （如亚运/友谊赛）覆盖；daily-image 口径 = "今日在售的五大联赛"
    fixture_rows = jc_view.fixtures_on(today.isoformat())
    pred_by_league = {
        lg: (doc.get("data") or {}).get("predictions", [])
        for lg, doc in latest.items()
    }
    rows = build_rows(fixture_rows, pred_by_league)
    # 用户定范围＝NBA + 五大联赛：足球表只保留五大联赛在售（无预判的仍列 odd 层）。
    rows = [r for r in rows if (r.get("league_cn") or "") in _BIG5]
    matched = [r for r in rows if r.get("matched")]
    return {
        "sport": "football", "available": True, "rows": rows,
        "date": today.isoformat(),
        "stats": {
            "scope": "五大联赛", "on_sale": len(rows), "recommend": len(matched),
            "nba": 0,
            "hit": _hit_stats(),
        },
        "match_note": ("范围：NBA + 五大联赛。推荐列为官方盘口(odd)与模型算法层的合并；"
                       "让球线取官方 hhad 盘口；命中率待实际结算数据积累后如实展示。"),
    }


@router.get("/gap")
def gap(top: int = 10, _: None = Depends(require_auth)) -> dict:
    """官方 SP 隐含概率 vs 模型三向概率的错位榜（每日一报）。

    基线=官方让利价隐含概率（The Odds API 按 D9 默认关闭，不含国际收盘）。
    只列五大联赛在售、官方 had 三项齐、模型有胜平负概率的场次；
    按错位分降序，最多 top 条。
    """
    from store import jc_view
    from web.services import store as web_store
    from web.services.team_match import build_rows, jc_gap as _jc_gap

    latest = web_store.latest_by_league()
    fixture_rows = jc_view.fixtures_on(None)
    pred_by_league = {
        lg: (doc.get("data") or {}).get("predictions", [])
        for lg, doc in latest.items()
    }
    rows = build_rows(fixture_rows, pred_by_league)
    rows = [r for r in rows if (r.get("league_cn") or "") in _BIG5]
    items = _jc_gap(rows, top_n=min(max(int(top), 1), 30))
    return {
        "sport": "football", "scope": "五大联赛", "count": len(items),
        "items": items,
        "note": "基线=官方让利价隐含概率 vs 模型概率；The Odds API 默认关闭，国际收盘不纳入。",
    }


@router.get("/movement")
def movement(day: str | None = None, _: None = Depends(require_auth)) -> dict:
    """两时点盘口快照链：开盘(first) vs 临场(last) 赔率与隐含概率变动。

    数据源=竞彩官方 10 分钟采集在 fact.jc_offer 累积的多时点 snap_ts（D9 口径，
    不含 The Odds API）。只列五大联赛在售场次；day 缺省 → 最新 business_date。
    """
    from store import jc_view

    rows = jc_view.jc_movement(day=day)
    rows = [r for r in rows if (r.get("league_cn") or "") in _BIG5]
    return {
        "sport": "football", "scope": "五大联赛", "day": day or "latest",
        "count": len(rows), "items": rows,
    }


def _nba_available(preds: list, today) -> bool:
    """NBA 是否在开赛窗口：存在最近 ≤7 天的排期才算可出图；
    否则（纯赛程预估、休赛期）不生成篮球每日图。preds 含 kickoff_date('YYYY-MM-DD')。"""
    for p in preds or []:
        kd = str(p.get("kickoff_date") or "")
        try:
            d = date.fromisoformat(kd)
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= 7:
            return True
    return False


_NEWS_CACHE: dict = {}


@router.get("/daily-news")
def daily_news(sport: str = "football", _: None = Depends(require_auth)) -> dict:
    """每日一图今日资讯（LLM 生成 + 按 (日期,sport) 缓存）。

    返回两条 LLM 生成文本（前端 Promise.race 300s 等，超时走占位）：
      news        实时要闻（≤78 字 / 3 行，每行 ≤26 字，\\n 分隔）
      pick_reason 今日精选推荐理由（≤60 字；精选为 0 条时为空字符串）
    LLM 失败/超时/字段缺失 → news/pick_reason 回退占位，**绝不编造赛果/收益**。
    """
    from web.services import store as web_store
    today = web_store.bjt_today().isoformat()
    key = (today, sport)
    hit = _NEWS_CACHE.get(key)
    if hit and (hit.get("news") or hit.get("pick_reason")):
        return hit
    label = "竞彩篮球" if sport == "basketball" else "竞彩足球"
    context = _news_context(sport)
    picks_ctx = _picks_context(sport, limit=2)
    prompt = (
        "你是面向彩民的赛事资讯编辑。用户是买彩票的球迷，他们关心：球队状态/伤停/主客场/赛季压力/让球线合理性等\n"
        "会影响判断的信号。**禁止编造具体伤员、历史比分、天气等你没有的事实**——\n"
        "没有信息就给'看角度'，不要假装你查了数据。\n\n"
        "基于下面『今日在售赛事』与『精选候选』，生成两条短文：\n"
        "① news：3 行今日可关注的客观角度，每行 ≤26 字、共 3 行（用 \\n 分隔），总计 ≤78 字。\n"
        "   **不要复述对阵名/场次号**（表格里已有）——只写角度。\n"
        "   可用角度（每行挑一个，写'今日 + 角度 + 含义'，如'今日多场 +1 深盘需核合理'）：\n"
        "   - 让球盘深度（多场 +1/-1 深盘：是否反映真实实力差）\n"
        "   - 赛季阶段（争冠/保级压力差）\n"
        "   - 主客场近期形态（哪边有主场上风）\n"
        "   - 赛程密度（周中杯赛 + 周末联赛、连场消耗）\n"
        "   - 大球场次（榜首/榜尾/欧战资格争夺）\n"
        "   禁止：收益承诺、感叹号、预测比分、人名/伤员/历史编造。\n"
        "② pick_reason：精选候选为什么值得跟进，≤60 字。\n"
        "   应给玩家'怎么核验这场'的角度（让球合理性 + 主客形态 + 赛季重要性），不要只复述模型方向。\n"
        "   如果精选候选为空 → 返回空字符串。\n\n"
        f"今日在售赛事（按业务日列出 8 场，含主客）：\n{context}\n\n"
        f"精选候选（前 2 条，含官方让球线与模型方向）：\n{picks_ctx}\n\n"
        "只输出严格 JSON：{\"news\":\"line1\\nline2\\nline3\",\"pick_reason\":\"...\"}"
    )
    news = ""
    pick_reason = ""
    try:
        from ai import llm_client
        res = llm_client.generate(prompt) or {}
        news = str(res.get("news") or "").strip()
        pick_reason = str(res.get("pick_reason") or "").strip()
    except Exception:
        pass
    if not news:
        news = (f"{label}暂无在售赛事，数据同步中"
                if sport == "basketball" else f"{label}今日有多场五大联赛在售，详情见下表")
    if not pick_reason:
        pick_reason = ""
    out = {"sport": sport, "date": today, "news": news[:78], "pick_reason": pick_reason[:60]}
    _NEWS_CACHE[key] = out
    return out


def _picks_context(sport: str, limit: int = 2) -> str:
    """精选候选的 LLM 上下文（场次号 + 对阵 + 官方让球线 + 模型方向）；无精选则空串。"""
    if sport != "football":
        return ""
    try:
        from web.services.team_match import build_rows as _br
        from web.services import store as _ws
        from store import jc_view as _jv
        today = _ws.bjt_today()
        fixture_rows = _jv.fixtures_on(today.isoformat()) or []
        pred_by_league = {
            lg: (doc.get("data") or {}).get("predictions", []) or []
            for lg, doc in (_ws.latest_by_league() or {}).items()
        }
        rows = _br(fixture_rows, pred_by_league)
        rows = [r for r in rows if (r.get("league_cn") or "") in _BIG5]
        matched = [r for r in rows if r.get("matched") and r.get("algo")]
        matched.sort(key=lambda r: -(r.get("algo", {}).get("stars") or 0))
        picks = matched[:limit]
        if not picks:
            return ""
        out = []
        for p in picks:
            algo = p.get("algo") or {}
            out.append(
                f"{p.get('match_num')} {p.get('home_cn') or '?'} vs {p.get('away_cn') or '?'}"
                f"（让球 {p.get('goal_line') or '—'}）→ 模型方向 {algo.get('direction') or '?'}"
            )
        return "\n".join(out)
    except Exception:
        return ""


def _news_context(sport: str) -> str:
    """为 LLM 组装今日在售赛事上下文（足球取五大联赛前 8 场对阵；篮球空态）。"""
    if sport != "football":
        return "（暂无在售赛事）"
    try:
        from store import jc_view
        rows = jc_view.fixtures_on(None)
        seen: dict = {}
        for r in rows:
            if (r.get("league_cn") or "") not in _BIG5:
                continue
            mn = r.get("match_num")
            if mn is None or mn in seen:
                continue
            seen[mn] = f"{r.get('home_cn') or '?'} vs {r.get('away_cn') or '?'}"
        lines = list(seen.values())[:8]
        return "；".join(lines) or "（暂无在售）"
    except Exception:
        return "（暂无在售）"


@router.get("/qr")
def daily_qr(url: str = "", _: None = Depends(require_auth)) -> dict:
    """每日一图角落二维码：链接到本 Dashboard web 界面。

    前端把自身 location.href 作为 url 查询参数传入（默认缺省则用固定入口）。
    返回 SVG data URL（纯 python qrcode + SvgPathImage，无 pillow）。url 视为只读目标入码，
    校验为 http(s) 上下文避免 javascript: 等注入。
    """
    import base64 as _b64
    import io as _io
    import urllib.parse as _up
    import qrcode
    import qrcode.image.svg

    if not url.startswith(("http://", "https://")):
        url = "https://140.83.62.161/dashboard/jc/"
    else:
        url = _up.unquote(url)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=1)
    qr.add_data(url)
    qr.make()
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = _io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    b64 = _b64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {"qr_data_url": f"data:image/svg+xml;base64,{b64}", "url": url}
