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
def daily_image(_: None = Depends(require_auth)) -> dict:
    """每日一图数据：竞彩盘口在售场次 × 模型算法层预判（只读合并）。

    数据源：
      - 盘口在售场次/odd 层：from store import jc_view → fixtures_on()
      - 算法层预判：store.latest_by_league() 各联赛最新 predictions
    合并逻辑在 web.services.team_match.build_rows（纯函数，单测覆盖）。

    返回 {"rows": [...], "date": "YYYY-MM-DD", "generated_at": ...}。
    只合并"已预测且队名可匹配"的场次；无预测的场次仅含 odd 层。
    """
    from store import jc_view
    from web.services.team_match import build_rows, nba_rows
    from web.services import store as web_store

    fixture_rows = jc_view.fixtures_on(None)
    latest = web_store.latest_by_league()
    pred_by_league = {
        lg: (doc.get("data") or {}).get("predictions", [])
        for lg, doc in latest.items()
    }
    rows = build_rows(fixture_rows, pred_by_league)
    # NBA 表：从 latest['nba'] 产出行（足篮各自用可用列，不强求同列）
    nba_predictions = (latest.get("nba") or {}).get("data", {}).get("predictions", [])
    nba = nba_rows(nba_predictions)
    return {
        "rows": rows,          # 足球表（保留键名，前端已用 d.rows）
        "football": rows,      # 别名，等同 rows
        "nba": nba,            # NBA 表
        "date": web_store.bjt_today().isoformat(),
        "match_note": ("仅展示有模型预判且队名可匹配的在售场次；"
                       "odd 层取官方 had 赔率最低项，算法层为模型方向/星级/波胆。"),
    }


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
