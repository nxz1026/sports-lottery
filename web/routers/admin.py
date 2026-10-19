"""web.routers.admin — 运维/调试端点（require_auth 守门）。

R15b:
- GET /api/v1/admin/events/summary  events 流聚合（kind/league 计数 + 字节数）

设计：
- admin 类端点统一加 ``admin`` segment，便于 auth policy 一刀切控制
  （未来可以把 require_auth 升级成 admin_require_role 而不动其它 router）。
- 端点直接复用 ``core.observability.events_summary`` —— 该函数流式聚合
  iter_events（真惰性），events.jsonl 几十万行不爆内存。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 让 from core import observability 在本模块被导入时即可解析（仅一次）。
# R47：本模块**不能**依赖 web/api.py 的导入顺序 —— R46 的 isort 把 admin 排到
# jc 之前，而此前只有 jc.py 会先把 scripts/ 加进 sys.path，顺序一变就
# ModuleNotFoundError: No module named 'core'（生产 league-web.service 无限重启）。
# 自举后本模块与导入顺序无关（同 web/services/haf.py 的模式）。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import observability
from fastapi import APIRouter, Depends, Query

from web.auth import require_auth

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/events/summary")
def events_summary(
    since_ts: str | None = Query(None, description="ISO 时间戳下界，过滤此前的 events"),
    by_kind: bool = Query(True, description="是否按事件 kind 聚合计数"),
    by_league: bool = Query(True, description="是否按 league 字段聚合计数"),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    """events.jsonl 增长治理摘要（R15b）。

    Returns
    -------
    {
        "total": int,                      # 事件总行数
        "size_bytes": int,                 # events.jsonl + .{N}.bak 备份总字节
        "kinds": {kind: int} | None,       # by_kind=True 时：按事件类型计数
        "leagues": {league: int} | None,   # by_league=True 时：league 字段聚合
                                              （league 缺失/空字符串 → "<empty>" 桶）
        "first_ts": str | None,           # 最早事件 ts（ISO 字符串）
        "last_ts": str | None,            # 最新事件 ts
    }

    鉴权：``require_auth``（与 ``/api/v1/jobs/*`` 同级）。
    """
    return observability.events_summary(
        since_ts=since_ts,
        by_kind=by_kind,
        by_league=by_league,
    )
