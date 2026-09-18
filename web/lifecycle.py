"""web.lifecycle — 应用启动生命周期（P0-DASH1a：自 web/api.py 逐字搬出）。

定时任务**不在进程内调度**：宿主 systemd 的本地时区是 Etc/UTC，而 apscheduler 的
`CronTrigger` 不传 timezone 时会落系统时区，导致 `CRON_HOUR=9` 实际在 09:00 UTC
（= 17:00 BJT）触发，与文档声称的「每日 09:00 BJT」差 8 小时；且进程内调度在服务
每次重启后都要重算，本机一天重启 11 次，极易错过触发点。

现由 `ops/league-daily-predict.timer` 统一接管（`OnCalendar` 显式带
`Asia/Shanghai` + `Persistent=true` 可补跑错过的触发），进程内调度已移除，
避免两套调度并存导致重复消耗配额。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时清理一次过期会话（幂等；正常路径有惰性清理兜底）。
    from web import session_store
    session_store.purge_expired_sessions()
    yield
