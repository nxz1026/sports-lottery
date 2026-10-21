"""web.routers.jc — 竞彩看板只读 API（全部 require_auth，P0-DASH1b）。

端点（数据源：store.jc_view，Postgres ro 只读；store 层任何异常自行降级为 []）：
- GET /api/jc/fixtures   场次 × 玩法最新盘口快照（day 缺省 = 最新 business_date）
- GET /api/jc/issues     传统足彩期头 + 开奖（limit 1..200，越界 400）
- GET /api/jc/backtest   多分类 Brier / argmax 命中率基线口径聚合（每玩法一行）
- GET /api/jc/lottery    各彩种最近 N 期开奖（per_type 1..100）
- GET /api/jc/daily-image   每日一图 VM 透传（编排见 ``web/app/jc.py``，README 「应用层机制」）
- GET /api/jc/daily-news    LLM 资讯（网关 ``web.services.news``，契约 §5 唯一例外）
- GET /api/jc/gap           SP 隐含概率 vs 模型错位榜（top 1..30）
- GET /api/jc/movement      两时点盘口快照链（开盘 vs 临场）

每端点固定返回 {"rows": [...], "sql_note": ..., "degraded": False}：
口径"该不该有数据"归页面/DASH2 判定，本层不发明健康判定（degraded 恒 False）。
``/daily-image`` 端点是 VM 透传，编排逻辑全部在 ``web/app/jc.py``（README
「应用层机制」节；该模块可在无 FastAPI 环境下单测）。
本文件不得出现 psycopg；端点内延迟 `from store import jc_view`（顶层不 import，
T5 免 psycopg 环境）。web 启动带 PYTHONPATH=.（无 scripts/），故模块级自举一次。
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from web.auth import require_auth

logger = logging.getLogger("web.jc")
router = APIRouter(prefix="/api/jc", tags=["jc"])
# Stable v1 alias for consumers that do not use the /api/jc dashboard namespace.
v1_router = APIRouter(prefix="/api/v1", tags=["backtest"])

# T3 启动链 `PYTHONPATH=. uvicorn web.api:app` 不含 scripts/：自举一次，端点才可 import store。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# 用户定范围＝NBA + 五大联赛：每日一图足球表只保留这五家（league_cn 为 fact.jc_match 全名）。
# 注意：``_BIG5`` 与 ``_big5_with_temp`` 都已迁到 ``web.config``（单一真源），
# router 仅保留 thin alias 兼容历史 import（tests/test_temp_league_whitelist.py
# 直接 ``from web.routers.jc import _big5_with_temp``）。
from web.config import BIG5 as _BIG5  # noqa: F401 — 兼容 alias
from web.config import big5_with_temp as _big5_with_temp  # noqa: F401 — 兼容 alias

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
    """场次 × 玩法盘口（每场 5 行 had/hhad/crs/ttg/haf，盘口取该 (场,玩法) 最新一版 snap_ts）。

    降级语义（契约 §5.5）：
      - day 非 ISO 日期 → 400（DASH2-D：脏参数不穿到 store 层，不静默降级成空表）。
      - DB 不可达 / 缺数据 → 200 + ``{"items": [], "note": "..."}``。
    鉴权：``require_auth``。

    Query：
      - day : 业务日 YYYY-MM-DD（缺省 = latest business_date）。

    Response schema：
      - 200 : ``{items: [{match_num, league_cn, home_cn, away_cn, kickoff_bj,
        play_type, odds: {home/draw/away, line}, snap_ts}, ...], note: str}``
      - 400 : day 非法
    """
    if day is not None:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(status_code=400, detail="day must be an ISO date like 2026-09-16")
    from store import jc_view
    return _envelope("fixtures", jc_view.fixtures_on(day))


@router.get("/issues")
def issues(limit: str = "20", _: None = Depends(require_auth)) -> dict:
    """传统足彩期次 + 开奖（胜负游戏 90 / 任选9场 900129 / 4场进球 94 / 6场半全场 98）。

    降级语义（契约 §5.5）：
      - limit 非整数 / <1 / >200 → 400（脏参数不穿到 store 层）。
      - DB 不可达 / 缺数据 → 200 + ``{items: [], note: "..."}``。
      - head_source="jc_issue_result" 表示期头是合成的（参见 _NOTES）。
    鉴权：``require_auth``。

    Query：
      - limit : 返回期次数（1..200，默认 20）。

    Response schema：
      - 200 : ``{items: [{issue_num, game_code, sale_begin, sale_end, draw_at,
        head_source, balls: [int, ...], prize_levels: [...]}, ...]}``
      - 400 : limit 非法
    """
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
    """多分类 Brier / log-loss / argmax 命中率（每玩法一行）；异常安全降级为 []。

    降级语义（契约 §5.5）：DB 不可达 → 200 + ``{items: [], note: "..."}``；
    不抛 500、不编数。
    鉴权：``require_auth``。

    Response schema：
      - 200 : ``{items: [{play_type, brier, log_loss, hit_rate, n_fixtures,
        reconciled, sample_count: int | None}]}``
    """
    from store import jc_view
    return _envelope("backtest", jc_view.backtest_summary())


@v1_router.get("/backtest")
def backtest_v1(_: None = Depends(require_auth)) -> dict:
    """JC backtest summary 兼容别名（``/api/v1/backtest`` 路径）。

    降级语义、鉴权、Response schema 同 ``/api/jc/backtest``。
    """
    from store import jc_view
    return _envelope("backtest", jc_view.backtest_summary())


@router.get("/lottery")
def lottery(per_type: str = "20", _: None = Depends(require_auth)) -> dict:
    """各彩种最近 N 期开奖（超级大乐透 85 / 排列3 35 / 排列5 350133 / 7星彩 04）。

    降级语义（契约 §5.5）：
      - per_type 非整数 / <1 / >100 → 400（脏参数不穿到 store 层）。
      - DB 不可达 / 缺数据 → 200 + ``{items: [], note: "..."}``。
    鉴权：``require_auth``。

    彩种清单来自采集端 lottery_draw 主题，解析层无白名单 —— 采集端补采新彩种后
    本端点自动带出，无需改代码（``per_type`` 钳制 1..100，越界 400）。

    Query：
      - per_type : 每彩种返回期次数（1..100，默认 20）。

    Response schema：
      - 200 : ``{items: [{game_num, period, draw_date, numbers_raw: str,
        bonus_raw: str | None}], note: str}``
      - 400 : per_type 非法
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
    """每日一图数据（拆分 sport）—— VM 透传到 ``web.app.jc``。

    降级语义 / 鉴权 / Response schema 同 VM 实现，本端点仅承担 URL 路径与
    鉴权包裹。VM 编排逻辑见 ``web/app/jc.py::daily_image_vm``（README
    「应用层机制」节），单测见 ``tests/app/test_jc_daily_image.py``。
    """
    from web.app import jc as app_jc
    return app_jc.daily_image_vm(sport=sport)


@router.get("/gap")
def gap(top: int = 10, _: None = Depends(require_auth)) -> dict:
    """官方 SP 隐含概率 vs 模型三向概率的错位榜（每日一报）。

    基线=官方让利价隐含概率（The Odds API 按 D9 默认关闭，不含国际收盘）。
    只列主面板联赛在售（五大联赛 + 欧冠）、官方 had 三项齐、模型有胜平负概率的场次；
    按错位分降序，最多 top 条。
    """
    from store import jc_view

    from web.services import store as web_store
    from web.services.team_match import build_rows
    from web.services.team_match import jc_gap as _jc_gap

    try:
        latest = web_store.latest_by_league()
        fixture_rows = jc_view.fixtures_on(None)
        pred_by_league = {
            lg: (doc.get("data") or {}).get("predictions", [])
            for lg, doc in (latest or {}).items()
        }
        rows = build_rows(fixture_rows, pred_by_league)
    except Exception as exc:  # noqa: BLE001 - latest_by_league/fixtures 抛错降级为 []
        logger.warning("jc: _gap gather failed: %s", exc)
        rows = []
    rows = [r for r in rows if (r.get("league_cn") or "") in _big5_with_temp()]
    items = _jc_gap(rows, top_n=min(max(int(top), 1), 30))
    # 建议 5（增量）：把「真实错位」与「盘口漂移」拆开 ——
    # 老字段（gap / official / model）**一字未动**，只附加三个新字段。
    try:
        from web.services.gap import attach_triples, triples_note

        items = attach_triples(items)
        _triple_note = triples_note()
    except Exception as exc:  # noqa: BLE001 - 增强失败不影响老字段
        logger.warning("jc: gap 三分量附加失败（降级：老字段照常）: %s", exc)
        _triple_note = ""
    return {
        "sport": "football", "scope": "五大联赛+欧冠", "count": len(items),
        "items": items,
        "note": "基线=官方让利价隐含概率 vs 模型概率；The Odds API 默认关闭，国际收盘不纳入。",
        "triple_note": _triple_note,
    }


@router.get("/odds-move")
def odds_move(hours: int = 24, limit: int = 50, _: None = Depends(require_auth)) -> dict:
    """赔率异动事件（建议 5 子项 ②）。

    ⚠️ **口径**：异动 = **相邻 ~10 分钟快照的跳变**，不是「开盘 vs 临场」的首末差
    —— 后者实测 ≥5% 命中 77%（纯噪音）；相邻跳变 ≥5% 命中 0.40% ⇒ 约 0.6 次/天。
    ⚠️ **盘口异动是「信息」，不是下注理由**（模型概率与竞彩盘口不同源，且竞彩 had 有水位）。
    """
    from web.services import odds_move as odds_move_service

    rows = odds_move_service.recent(hours=hours, limit=limit)
    return {
        "sport": "football",
        "count": len(rows),
        "items": rows,
        "note": ("异动口径 = 相邻 ~10 分钟快照的跳变（不是开盘 vs 临场的首末差 —— "
                 "后者 ≥5% 命中 77%，是噪音）。"
                 "⚠️ 盘口异动是「信息」，不是下注理由。"),
    }


@router.get("/movement")
def movement(day: str | None = None, window_days: int | None = None,
             _: None = Depends(require_auth)) -> dict:
    """两时点盘口快照链：开盘(first) vs 临场(last) 赔率与隐含概率变动。

    数据源=竞彩官方 10 分钟采集在 fact.jc_offer 累积的多时点 snap_ts（D9 口径，
    不含 The Odds API）。只列主面板联赛在售场次（五大联赛 + 欧冠）；day 缺省 → 最新 business_date。

    建议 5 ③：**「开盘价」取第一版三项齐全的 had**（不是第一版快照）——
    实测 405/406 场的第 1 版 `options` 是 `{}` ⇒ 原实现让 delta 全是 None。
    建议 5 ④：``window_days``（**可选参数**）→ 看最近 N 期而不只最新一期
    （刚开盘场次只有 1 个版本 ⇒ 老口径永远为空）；**缺省行为不变**。
    """
    from store import jc_view

    # ⚠️ 缺省路径**逐字保持老调用形态**（只传 day）⇒ 老行为一字不变、既有 mock 不用改；
    #    只有显式给了 window_days 才走新参数。
    if window_days is None:
        rows = jc_view.jc_movement(day=day)
    else:
        rows = jc_view.jc_movement(day=day, window_days=window_days)
    rows = [r for r in rows if (r.get("league_cn") or "") in _big5_with_temp()]
    return {
        "sport": "football", "scope": "五大联赛+欧冠",
        "day": day or ("window_%s" % window_days if window_days else "latest"),
        "count": len(rows), "items": rows,
    }


# _nba_available 已迁到 web.app.jc（router 只透传 daily_image，不持有 VM 私有工具）。
# R18 #2: 移除本模块 _NEWS_CACHE 声明（dict 死写入已删）—— 真缓存统一在
# web.services.news 模块。


@router.get("/daily-news")
def daily_news(sport: str = "football", _: None = Depends(require_auth)) -> dict:
    """每日一图今日资讯（LLM 生成 + 按 (日期,sport) 缓存）。

    返回两条 LLM 生成文本（前端 Promise.race 300s 等，超时走占位）：
    R18 #2 移除本模块 _NEWS_CACHE 写入（声明在下面已删）—— 真正的缓存统一在
    ``web/services/news.py::_NEWS_CACHE``（已按 (today, sport) 缓存 + 命中率高）。
    本端点重复写一份空 dict 永驻内存（无 TTL 无失效），契约混乱。
      news        实时要闻（≤78 字 / 3 行，每行 ≤26 字，\\n 分隔）
      pick_reason 今日精选推荐理由（≤60 字；精选为 0 条时为空字符串）
    LLM 调用统一由 ``web.services.news.generate_daily_news`` 承载（契约 §5
    文档化例外：本端点是 web → ai 的唯一汇聚点；新增 LLM 端点请调用该服务
    函数而不是直接 ``from ai import llm_client``）。
    LLM 失败/超时/字段缺失 → news/pick_reason 回退占位，**绝不编造赛果/收益**。
    """
    from web.services import news as news_service
    from web.services import store as web_store
    today = web_store.bjt_today().isoformat()
    label = "竞彩篮球" if sport == "basketball" else "竞彩足球"
    context = _news_context(sport)
    picks_ctx = _picks_context(sport, limit=2)
    out = news_service.generate_daily_news(
        date_iso=today, sport=sport, context=context, picks_ctx=picks_ctx, label=label,
    )
    if not out.get("news"):
        out["news"] = (f"{label}暂无在售赛事，数据同步中"
                       if sport == "basketball" else f"{label}今日有多场五大联赛在售，详情见下表")
    out = {"sport": sport, "date": today,
           "news": (out.get("news") or "")[:78],
           "pick_reason": (out.get("pick_reason") or "")[:60]}
    # R18 #2: 缓存由 web.services.news 统一维护（_NEWS_CACHE 在那边）
    return out


def _picks_context(sport: str, limit: int = 2) -> str:
    """精选候选的 LLM 上下文（场次号 + 对阵 + 官方让球线 + 模型方向）；无精选则空串。"""
    if sport != "football":
        return ""
    try:
        from store import jc_view as _jv

        from web.services import store as _ws
        from web.services.team_match import build_rows as _br
        today = _ws.bjt_today()
        fixture_rows = _jv.fixtures_on(today.isoformat()) or []
        pred_by_league = {
            lg: (doc.get("data") or {}).get("predictions", []) or []
            for lg, doc in (_ws.latest_by_league() or {}).items()
        }
        rows = _br(fixture_rows, pred_by_league)
        rows = [r for r in rows if (r.get("league_cn") or "") in _big5_with_temp()]
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
    except Exception as exc:  # noqa: BLE001 - picks 上下文降级为空串
        logger.warning("jc: _picks_context failed: %s", exc)
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
            if (r.get("league_cn") or "") not in _big5_with_temp():
                continue
            mn = r.get("match_num")
            if mn is None or mn in seen:
                continue
            seen[mn] = f"{r.get('home_cn') or '?'} vs {r.get('away_cn') or '?'}"
        lines = list(seen.values())[:8]
        return "；".join(lines) or "（暂无在售）"
    except Exception as exc:  # noqa: BLE001 - news 上下文降级
        logger.warning("jc: _news_context failed: %s", exc)
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
        # 客户端未传完整 URL 时，回退到 PUBLIC_BASE_URL env（部署可配）；
        # 默认值保留旧行为（Oracle 当前公网 IP）以兼容历史部署。
        from web import config as _config
        url = _config.PUBLIC_BASE_URL or "https://140.83.62.161/dashboard/jc/"
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
