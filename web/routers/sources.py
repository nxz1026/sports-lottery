"""web.routers.sources — 上游数据源状态（静态配置，require_auth）。

GET /api/v1/sources/status
返回：配置了哪些上游、各上游 env 键是否存在（绝不回传值）、联赛默认源映射、
当日预测配额用量与最近任务状态（M3：调度概览收进状态面）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from web.auth import require_auth
from web.services import jobs
from web.services.datasource import SOURCE_KEYS, LEAGUES, DEFAULT_SOURCE

router = APIRouter(prefix="/api/v1", tags=["sources"])


def _recent_job_stats() -> dict:
    rows = jobs.list_jobs(limit=50)
    counts: dict[str, int] = {}
    last: dict | None = None
    for row in rows:
        counts[row.get("status", "unknown")] = counts.get(row.get("status", "unknown"), 0) + 1
        if last is None:
            last = {
                "id": row.get("id"),
                "status": row.get("status"),
                "trigger": row.get("trigger"),
                "created_at": row.get("created_at"),
                "finished_at": row.get("finished_at"),
            }
    # 核查 P2-10：by_status 计数在页面原本无法溯源（3 个 timeout、1 个 failed
    # 从何而来无从查起）。追加最近 10 条作业明细供「数据源与任务」渲染任务表，
    # 字段与 jobs/{id} 端点同一来源，不含敏感值。
    items = []
    for row in jobs.list_jobs(limit=10):
        items.append({
            "id": row.get("id"),
            "status": row.get("status"),
            "trigger": row.get("trigger"),
            "script": row.get("script"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
            "exit_code": row.get("exit_code"),
            "error": (str(row.get("error"))[:120] if row.get("error") else None),
        })
    return {"by_status": counts, "last": last, "items": items}


@router.get("/sources/status")
def sources_status(request: Request,
                   _: None = Depends(require_auth)) -> dict:
    """静态上游配置状态（无敏感值）。"""
    import os
    configured = []
    enabled = {}
    for source, keys in SOURCE_KEYS.items():
        has_keys = all(os.environ.get(k) for k in keys) if keys else True
        configured.append(source)
        enabled[source] = has_keys
    return {
        "configured": configured,
        "enabled": enabled,
        "default_source": DEFAULT_SOURCE,
        "leagues": {
            league: {"name": info["name"], "data_source": info["data_source"]}
            for league, info in LEAGUES.items()
        },
        # M3：调度概览（配额 + 活跃/最近任务）。jobs 模块纯文件操作，零引擎依赖。
        "jobs": {
            "quota": jobs.quota_usage(),
            "active": jobs.active_job(),
            "recent": _recent_job_stats(),
        },
    }