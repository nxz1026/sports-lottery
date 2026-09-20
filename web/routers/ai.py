"""web.routers.ai — AI 富化扩展端点（契约 §5，M3）。

- GET /api/v1/ai/status   require_auth；AI 模块可用性 + 富化数据概览。
- GET /api/v1/ai/details  require_auth；富化明细条目。

降级语义（契约 §5.5）：模块不可用 / ai_scores.json 缺失 / 读取异常，
一律 200 + {available:false, reason}，绝不 500、绝不拖垮 web 主流程。
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from web.auth import require_auth
from web.services import ai

router = APIRouter(prefix="/api/v1", tags=["ai"])


@router.get("/ai/status")
def ai_get_status(request: Request, _: None = Depends(require_auth)) -> dict:
    """AI 模块状态 + 概览（降级语义）。"""
    return ai.ai_status()


@router.get("/ai/details")
def ai_get_details(request: Request, _: None = Depends(require_auth)) -> dict:
    """AI 富化明细（最多 50 条，按 ai_score 降序）。"""
    return ai.ai_details()


@router.get("/ai/analyze")
def ai_analyze_get(date_str: str | None = Query(None, alias="date"),
                   _: None = Depends(require_auth)) -> dict:
    """AI 分析结果（逐场解读 / 胆材叙事 / 开奖复盘）。

    date 缺省=今日 BJT；由 ai_analyze 异步 job 落盘 predictions/ai_analysis/{date}.json，
    本端点只读不触发。如文件不存在或缺字段 → available=False + note。
    """
    from pathlib import Path
    from web.services import ai_analyze as _aa
    target = _aa._today_bjt().isoformat() if not date_str else str(date_str)
    try:
        from datetime import date as _date
        _date.fromisoformat(target)
    except ValueError:
        return {"available": False, "date": target, "note": "invalid_date",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    path = _aa._output_path(target)
    if not path.exists():
        return {"available": False, "date": target,
                "note": f"no analysis yet（先 POST /api/v1/jobs/ai-analyze）",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except Exception as e:
        return {"available": False, "date": target, "note": f"read_failed: {e}",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    return {"available": True, "date": data.get("date"),
            "generated_at": data.get("generated_at"),
            "classes": data.get("classes") or {},
            "warning": data.get("warning") or []}


def _parse_day(value: str | None) -> date:
    if value is None:
        return ai.store.bjt_today()
    return date.fromisoformat(value)


@router.get("/ai/daily")
def ai_daily(request: Request, date_str: str | None = Query(None, alias="date"), _: None = Depends(require_auth)) -> dict:
    try:
        return ai.ai_daily_report(_parse_day(date_str))
    except ValueError:
        return {"available": False, "reason": "invalid_date", "items": [], "summary": {}}


@router.get("/ai/ranking")
def ai_ranking(request: Request, date_str: str | None = Query(None, alias="date"), limit: int = Query(10, ge=1, le=50), _: None = Depends(require_auth)) -> dict:
    try:
        return ai.ai_ranking(_parse_day(date_str), limit)
    except ValueError:
        return {"available": False, "reason": "invalid_date", "hot": [], "cold": [], "matched_count": 0}