"""web.routers.ai — AI 富化扩展端点（契约 §5，M3）。

- GET /api/v1/ai/status   require_auth；AI 模块可用性 + 富化数据概览。
- GET /api/v1/ai/details  require_auth；富化明细条目。

降级语义（契约 §5.5）：模块不可用 / ai_scores.json 缺失 / 读取异常，
一律 200 + {available:false, reason}，绝不 500、绝不拖垮 web 主流程。

⚠️ **P2-C（2026-10-23）**：``ai_status()`` 会回传 ``source`` = 服务端
``ai_scores.json`` 的**绝对路径**（如 ``/home/ubuntu/DSH/sports-lottery/predictions/ai_scores.json``）
⇒ 泄露部署目录布局（对用户无意义、对攻击者有用），与仓库其它端点
（``web/routers/predictions.py::_run_metadata`` 明写 "never expose local paths"）
的策略不一致。
⇒ **本层**（唯一能改的层：``web/services/ai.py`` 不在本轮范围内）统一脱敏：
绝对路径 ⇒ **相对仓库根的逻辑路径**（``predictions/ai_scores.json``），
仓库外的路径 ⇒ 只保留**文件名**。**键集合不变**（只改值）⇒ 既有消费者不受影响。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

from web.auth import require_auth
from web.services import ai

router = APIRouter(prefix="/api/v1", tags=["ai"])
_logger = logging.getLogger("web.ai")

# 会被当成「路径」脱敏的键（其余键原样透传）。
_PATH_KEYS = frozenset({"source", "path", "file", "file_path", "abs_path"})


def _looks_absolute(value: str) -> bool:
    """跨平台判断「像不像绝对路径」（Windows 盘符 + POSIX 根路径）。"""
    if not value or len(value) < 2:
        return False
    if value[0] in "/\\":
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in "\\/"


def _logical_path(value: str) -> str:
    """绝对路径 ⇒ 相对仓库根的逻辑路径；仓库外 ⇒ 只留文件名。"""
    from web import config

    base = str(config.BASE_DIR).replace("\\", "/").rstrip("/")
    norm = value.replace("\\", "/")
    if base and norm.startswith(base + "/"):
        return norm[len(base) + 1:]
    return Path(value.replace("/", "\\")).name or value


def _sanitize_paths(payload):
    """就地脱敏响应里的服务器绝对路径（**只改值，不改键集合**）。"""
    if isinstance(payload, dict):
        return {k: (_logical_path(v) if k in _PATH_KEYS and isinstance(v, str)
                    and _looks_absolute(v) else _sanitize_paths(v))
                for k, v in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_paths(item) for item in payload]
    return payload


@router.get("/ai/status")
def ai_get_status(request: Request, _: None = Depends(require_auth)) -> dict:
    """AI 模块状态 + 概览（降级语义；**不泄露服务器绝对路径**）。"""
    return _sanitize_paths(ai.ai_status())


@router.get("/ai/details")
def ai_get_details(request: Request, _: None = Depends(require_auth)) -> dict:
    """AI 富化明细（最多 50 条，按 ai_score 降序）。"""
    return _sanitize_paths(ai.ai_details())



@router.get("/ai/analyze")
def ai_analyze_get(date_str: str | None = Query(None, alias="date"),
                   _: None = Depends(require_auth)) -> dict:
    """AI 分析结果（逐场解读 / 胆材叙事 / 开奖复盘）。

    date 缺省=今日 BJT；由 ai_analyze 异步 job 落盘 predictions/ai_analysis/{date}.json，
    本端点只读不触发。如文件不存在或缺字段 → available=False + note。
    """
    from web.services import ai_analyze as _aa
    target = _aa._today_bjt().isoformat() if not date_str else str(date_str)
    try:
        from datetime import date as _date
        _date.fromisoformat(target)
    except ValueError:
        # ⚠️ P2-A7 续：降级分支统一带 `reason`（机器可读原因），
        #   note 保持既有值 `invalid_date` 不变（既有消费者不受影响）。
        return {"available": False, "date": target, "note": "invalid_date",
                "reason": "invalid_date",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    path = _aa._output_path(target)
    if not path.exists():
        # ⚠️ **P2-A7（2026-10-23）**：原文案是英文 + 把内部接口路径（POST /api/v1/...）
        #   甩给终端用户 ⇒ 既不中文化，也把「还没生成」与「任务跑挂了」混为一谈。
        #   ⇒ 改成**中文用户态**说明；**不许**出现内部 API 路径（页面直接渲染这个 note）。
        #   ⚠️ `reason` 是**新增**键（响应里多一个可读原因），既有键一个没删、没改语义。
        return {"available": False, "date": target,
                "reason": "not_generated",
                "note": "尚未生成分析：点「生成当日分析」跑一次即可（生成需要一点时间，跑完后刷新本页）",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    try:
        import json as _json
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except Exception as e:
        # ⚠️ **P2-A7 续（2026-10-23）**：原文案是 ``f"read_failed: {e}"`` ⇒
        #   ① 英文，用户看不懂；② **异常对象里可能带服务器绝对路径**
        #   （PermissionError / IsADirectoryError 的 str() 含完整路径）
        #   ⇒ 把部署目录布局甩给终端用户，与本模块 P2-C 的脱敏策略自相矛盾。
        # ⇒ 改成**中文用户态**说明 + 独立 `reason` 键；细节只进服务端日志。
        #   ⚠️ 既有键一个没删（available / date / note / classes 都在），
        #   `reason` 是新增键 ⇒ 键集合只增不减。
        _logger.warning("AI 分析结果读取失败: %s (%s)", target, e)
        return {"available": False, "date": target,
                "reason": "read_failed",
                "note": "分析结果读取失败（文件可能正在写入或已损坏），请稍后重试；若持续失败请重新生成一次。",
                "classes": {"per_match": [], "banker": "", "lottery": []}}
    return {"available": True, "date": data.get("date"),
            "generated_at": data.get("generated_at"),
            "classes": data.get("classes") or {},
            "warning": data.get("warning") or []}


def _parse_day(value: str | None) -> date:
    if value is None:
        # R17 #11: 直接 import bjt_today，避免绕一层 ai.store（与 web.routers.ai
        # 模块路径里 import 用 bjt_today 命名一致）。
        from web.services.store import bjt_today
        return bjt_today()
    return date.fromisoformat(value)


@router.get("/ai/daily")
def ai_daily(request: Request, date_str: str | None = Query(None, alias="date"), _: None = Depends(require_auth)) -> dict:
    try:
        return _sanitize_paths(ai.ai_daily_report(_parse_day(date_str)))
    except ValueError:
        return {"available": False, "reason": "invalid_date", "items": [], "summary": {}}


@router.get("/ai/ranking")
def ai_ranking(request: Request, date_str: str | None = Query(None, alias="date"), limit: int = Query(10, ge=1, le=50), _: None = Depends(require_auth)) -> dict:
    try:
        return _sanitize_paths(ai.ai_ranking(_parse_day(date_str), limit))
    except ValueError:
        return {"available": False, "reason": "invalid_date", "hot": [], "cold": [], "matched_count": 0}
