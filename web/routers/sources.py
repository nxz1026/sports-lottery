"""web.routers.sources — 上游数据源状态（静态配置，require_auth）。

GET /api/v1/sources/status
返回：配置了哪些上游、各上游 env 键是否存在（绝不回传值）、联赛默认源映射、
当日预测配额用量与最近任务状态（M3：调度概览收进状态面）。

P2-C1/C2 扩展：
- ``last_failure``：最近一次 status=failed 作业摘要（id / script / created_at /
  error 截断 120 字）。by_status 仅给计数，无法溯源；此处补一条字段让页面
  可定位具体 id（点击展开日志）。
- ``next_runs``：systemd timer 下次触发（UTC ISO + BJT 显示串），按 unit 索引；
  通过 ``systemctl list-timers`` 子进程读取；执行失败（非 systemd 平台 /
  权限缺失）→ 静默返回空 dict，绝不阻塞状态面。

辅助函数 ``list_timer_runs`` / ``recent_job_stats`` 委托给
``web.services.source_meta``（零 web 框架依赖，便于本地 pytest）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from web.auth import require_auth
from web.services import jobs
from web.services.datasource import DEFAULT_SOURCE, LEAGUES, SOURCE_KEYS
from web.services.source_meta import list_timer_runs, recent_job_stats

router = APIRouter(prefix="/api/v1", tags=["sources"])


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
        # M3 + P2-C1/C2：调度概览（配额 + 活跃/最近任务 + 最近失败溯源 + 下次定时）。
        "jobs": {
            "quota": jobs.quota_usage(),
            "active": jobs.active_job(),
            "recent": recent_job_stats(jobs.list_jobs),
            "next_runs": list_timer_runs(),
        },
    }
