"""web.routers.jobs — 任务触发端点 + 惰性刷新（WO-M3）。

端点：
- POST /api/v1/jobs/predict   require_auth；参数白名单透传（league/dates 等），
  禁任意字符串注入 argv；无预算 → 429；已有运行 → 409/202 语义；
  提交后异步执行（线程池），立即返回 202 + job。
- POST /api/v1/jobs/predict-bball  require_auth；篮球独立入口（scripts/bball/run.py），
  参数白名单 ahead_days/backtest（有界整数）；与足球共用配额与并发守卫。
- GET  /api/v1/jobs/{id}      require_auth；状态机可轮询到终态。
- GET  /api/v1/jobs           require_auth；最近任务列表。

惰性刷新：predictions/today 缺数据时，fire-and-forget 后台任务（经同一把锁
与配额守卫；同日去重，一天至多自动触发一次）。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from web import config, errors
from web.auth import require_auth
from web.errors import LockTimeout
from web.services import jobs, store
from web.services.datasource import LEAGUES

router = APIRouter(prefix="/api/v1", tags=["jobs"])

# 参数白名单（契约 §1.1 安全子集）：固定取值域校验，杜绝任意字符串注入 argv。
# frozenset：只需成员测试，不需要 key→value 映射。
# ⚠️ 不含 --all：它在上面已有显式分支（且需排在 --league 之前），
# 放进来会被追加第二次，实测 {"all":true} → ['--all','--all']。
_FLAG_ARGS: tuple[str, ...] = (
    "--monte-carlo", "--no-dc", "--no-ml", "--dashboard",
)

# value 型参数：key → 合法取值集合（None 表示单独正则/类型校验）。
_DATASOURCE_VALUES: frozenset[str] = frozenset({"football-data", "espn", "api-football", ""})


# 作业来源标记白名单。timer 由 ops/league-daily-predict.timer 传入，
# 用于在 Dashboard「数据源与任务」页把定时作业与手动作业区分开。
# auto 是 /jobs/auto/refresh 惰性刷新的内部标记（直接调 jobs.trigger_predict 传入）。
# 白名单外一律按 manual 处理（不报 400）：来源标记是观测字段，不该让请求失败。
_ALLOWED_TRIGGERS = ("manual", "timer", "cron", "auto")


def _pop_trigger(params: dict) -> str:
    """取出 trigger 标记并从 params 移除（否则会被 _validate_args 当未知参数拒掉）。"""
    raw = params.pop("trigger", "manual")
    return raw if raw in _ALLOWED_TRIGGERS else "manual"


def _validate_args(params: dict) -> list[str]:
    """白名单校验 → argv 列表；非法参数抛 400（code=invalid_params）。"""
    unknown = set(params) - {"league", "dates", "data_source", "monte_carlo",
                             "n_simulations", "no_dc", "no_ml", "dashboard", "all"}
    if unknown:
        raise errors.ApiError("invalid_params", f"未知参数: {sorted(unknown)}")
    argv: list[str] = []
    if params.get("all"):
        argv.append("--all")
    league = params.get("league")
    if league is not None:
        if league not in LEAGUES:
            raise errors.ApiError("invalid_params", f"未知联赛: {league}")
        argv += ["--league", str(league)]
    source = params.get("data_source")
    if source is not None:
        if source not in _DATASOURCE_VALUES:
            raise errors.ApiError("invalid_params", f"未知数据源: {source}")
        if source:
            argv += ["--data-source", str(source)]
    dates = params.get("dates")
    if dates is not None:
        if not isinstance(dates, str) or len(dates) != 17 or dates[8] != "-":
            raise errors.ApiError("invalid_params", "dates 须为 YYYYMMDD-YYYYMMDD")
        try:
            datetime.strptime(dates[:8], "%Y%m%d")
            datetime.strptime(dates[9:], "%Y%m%d")
        except ValueError:
            raise errors.ApiError("invalid_params", "dates 须为 YYYYMMDD-YYYYMMDD")
        argv += ["--dates", dates]
    n_sim = params.get("n_simulations")
    if n_sim is not None:
        if not isinstance(n_sim, int) or n_sim < 1:
            raise errors.ApiError("invalid_params", "n_simulations 须为正整数")
        argv += ["--n-simulations", str(n_sim)]
    for flag in _FLAG_ARGS:
        key = flag[2:].replace("-", "_")
        if params.get(key):
            argv.append(flag)
    return argv


def _job_view(job: dict) -> dict:
    """基础视图：list 端点用；不含 log_tail（需 I/O，按需获取）。"""
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "trigger": job.get("trigger"),
        "script": job.get("script", "predict"),
        "args": job.get("args", []),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "exit_code": job.get("exit_code"),
        "error": job.get("error"),
    }


def _job_view_detail(job: dict, jid: str) -> dict:
    """detail 视图：在基础视图上追加 log_tail（detail 端点专用）。"""
    view = _job_view(job)
    view["log_tail"] = jobs.read_job_log(jid)
    return view


@router.post("/jobs/predict", status_code=202)
def jobs_predict(body: dict | None,
                 _: None = Depends(require_auth)) -> JSONResponse:
    """提交预测任务（队列语义：返回 202 + job；并发时 409 + already_running）。"""
    params = dict(body or {})
    trigger = _pop_trigger(params)
    argv = _validate_args(params)
    try:
        job, reason = jobs.trigger_predict(argv, trigger=trigger)
    except LockTimeout:
        raise errors.ApiError("lock_busy", "系统繁忙，请稍后再试", http_status=503)
    if reason == "quota_exhausted":
        usage = jobs.quota_usage()
        raise errors.ApiError("quota_exhausted",
                              f"今日预测配额已用尽（{usage['used']}/{usage['limit']}）",
                              http_status=429)
    if reason == "already_running":
        return JSONResponse(status_code=409, content={
            "code": "already_running",
            "message": "已有预测任务在运行，请稍后再试",
            "job": _job_view(job),
        })
    # 异步执行（fire-and-forget：失败只写状态文件，绝不抛回请求线程）。
    jobs.submit_job(job["id"])
    return JSONResponse(status_code=202, content={"job": _job_view(job)})


# 篮球参数白名单：--ahead-days / --backtest 为有界整数（NBA 休赛期揭幕战在数月后，
# 默认 1 天会得 0 场，故允许放宽前瞻窗口）。
_BBALL_MAX_AHEAD_DAYS = 180
_BBALL_MAX_BACKTEST = 60


def _validate_bball_args(params: dict) -> list[str]:
    """篮球参数白名单校验 → argv；非法参数抛 400（code=invalid_params）。"""
    unknown = set(params) - {"ahead_days", "backtest"}
    if unknown:
        raise errors.ApiError("invalid_params", f"未知参数: {sorted(unknown)}")
    argv: list[str] = []
    ahead = params.get("ahead_days")
    if ahead is not None:
        if not isinstance(ahead, int) or isinstance(ahead, bool) or not 1 <= ahead <= _BBALL_MAX_AHEAD_DAYS:
            raise errors.ApiError("invalid_params",
                                  f"ahead_days 须为 1..{_BBALL_MAX_AHEAD_DAYS} 的整数")
        argv += ["--ahead-days", str(ahead)]
    backtest = params.get("backtest")
    if backtest is not None:
        if not isinstance(backtest, int) or isinstance(backtest, bool) or not 1 <= backtest <= _BBALL_MAX_BACKTEST:
            raise errors.ApiError("invalid_params",
                                  f"backtest 须为 1..{_BBALL_MAX_BACKTEST} 的整数")
        argv += ["--backtest", str(backtest)]
    return argv


@router.post("/jobs/predict-bball", status_code=202)
def jobs_predict_bball(body: dict | None,
                       _: None = Depends(require_auth)) -> JSONResponse:
    """提交篮球（NBA）预测任务 —— 独立入口 scripts/bball/run.py。

    与足球预测共用配额计数器与并发守卫：两者不能同时跑（409），避免同一小时内
    两个引擎争抢同一批上游配额。需要 ODDS_API_KEY；未配置时任务会以 exit 2 失败。
    """
    params = dict(body or {})
    trigger = _pop_trigger(params)
    argv = _validate_bball_args(params)
    try:
        job, reason = jobs.trigger_bball(argv, trigger=trigger)
    except LockTimeout:
        raise errors.ApiError("lock_busy", "系统繁忙，请稍后再试", http_status=503)
    if reason == "quota_exhausted":
        usage = jobs.quota_usage()
        raise errors.ApiError("quota_exhausted",
                              f"今日预测配额已用尽（{usage['used']}/{usage['limit']}）",
                              http_status=429)
    if reason == "already_running":
        return JSONResponse(status_code=409, content={
            "code": "already_running",
            "message": "已有预测任务在运行，请稍后再试",
            "job": _job_view(job),
        })
    jobs.submit_job(job["id"])
    return JSONResponse(status_code=202, content={"job": _job_view(job)})


@router.post("/jobs/ai-enrich", status_code=202)
def jobs_ai_enrich(body: dict | None = None,
                   _: None = Depends(require_auth)) -> JSONResponse:
    """提交 AI 摘要重生成任务（python -m web.enrich，argv 固定为空）。

    语义与 /jobs/predict 一致：202 + job / 409 already_running / 429 quota_exhausted。
    配额与 predict 共享同一计数器。body 只接受可选的 trigger 来源标记。
    """
    trigger = _pop_trigger(dict(body or {}))
    try:
        job, reason = jobs.trigger_ai_enrich(trigger=trigger)
    except LockTimeout:
        raise errors.ApiError("lock_busy", "系统繁忙，请稍后再试", http_status=503)
    if reason == "quota_exhausted":
        usage = jobs.quota_usage()
        raise errors.ApiError("quota_exhausted",
                              f"今日任务配额已用尽（{usage['used']}/{usage['limit']}）",
                              http_status=429)
    if reason == "already_running":
        return JSONResponse(status_code=409, content={
            "code": "already_running",
            "message": "已有任务在运行，请稍后再试",
            "job": _job_view(job),
        })
    jobs.submit_job(job["id"])
    return JSONResponse(status_code=202, content={"job": _job_view(job)})


@router.post("/jobs/ai-analyze", status_code=202)
def jobs_ai_analyze(body: dict | None = None,
                    _: None = Depends(require_auth)) -> JSONResponse:
    """提交 AI 分析任务（逐场解读/胆材叙事/开奖复盘）。

    body 可选 {"date":"YYYY-MM-DD"}；缺省 = 今日 BJT。
    异步执行：返回 202 + job，主预测链路不阻塞。
    """
    payload = dict(body or {})
    raw_date = payload.get("date")
    target_date = None
    if raw_date:
        try:
            from datetime import date as _date
            _date.fromisoformat(str(raw_date))
        except ValueError:
            raise errors.ApiError("bad_request", "date 必须 YYYY-MM-DD", http_status=400)
        target_date = str(raw_date)
    trigger = _pop_trigger(payload)
    try:
        job, reason = jobs.trigger_ai_analyze(date_str=target_date, trigger=trigger)
    except LockTimeout:
        raise errors.ApiError("lock_busy", "系统繁忙，请稍后再试", http_status=503)
    if reason == "quota_exhausted":
        usage = jobs.quota_usage()
        raise errors.ApiError("quota_exhausted",
                              f"今日任务配额已用尽（{usage['used']}/{usage['limit']}）",
                              http_status=429)
    if reason == "already_running":
        return JSONResponse(status_code=409, content={
            "code": "already_running",
            "message": "已有任务在运行，请稍后再试",
            "job": _job_view(job),
        })
    jobs.submit_job(job["id"])
    return JSONResponse(status_code=202, content={"job": _job_view(job)})


@router.get("/jobs/{jid}")
def jobs_get(jid: str, _: None = Depends(require_auth)) -> dict:
    """查询任务状态（可轮询到终态）。"""
    job = jobs.get_job(jid)
    if job is None:
        raise errors.ApiError("job_not_found", f"任务不存在: {jid}", http_status=404)
    return {"job": _job_view_detail(job, jid)}


@router.get("/jobs")
def jobs_list(_: None = Depends(require_auth)) -> dict:
    """最近任务列表（按创建时间倒序）。"""
    return {"jobs": [_job_view(j) for j in jobs.list_jobs(limit=20)]}


# --- 惰性刷新（predictions/today 缺数据兜底）-----------------------------

def _today_has_data() -> bool:
    """今天各联赛预测是否齐全：**每个**注册联赛的最新文档都覆盖今日才算齐全。"""
    day = store.bjt_today()
    docs = store.latest_by_league()
    return all(
        league in docs and store.covers_date(docs[league].get("data", {}), day)
        for league in LEAGUES if LEAGUES[league].get("active", True)
    )


def _lazy_auto_trigger() -> dict:
    """同日去重的自动触发：成功/已触发 → 200 语义；配额/并发 → 说明。"""
    marker = config.DATA_DIR / "auto_refresh_last.json"
    today = store.bjt_today().isoformat()
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        if payload.get("day") == today:
            return {"triggered": False, "reason": "already_today"}
    if not config.AUTO_REFRESH_DAILY:
        return {"triggered": False, "reason": "disabled"}
    # 必须显式传 --all：空 argv 会走 predict.py 的 --league 默认值 epl，
    # 自动刷新将永远只产出英超，仪表盘上其余联赛恒为空（实测症状）。
    job, reason = jobs.trigger_predict(["--all"], trigger="auto")
    if reason == "quota_exhausted":
        return {"triggered": False, "reason": "quota_exhausted"}
    if reason == "already_running":
        return {"triggered": False, "reason": "already_running"}
    try:
        jobs.submit_job(job["id"])
    except Exception:
        jobs.mark_failed(job["id"], "submit failed")
        raise
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"day": today, "job": job["id"]},
                                 ensure_ascii=False), encoding="utf-8")
    return {"triggered": True, "job": job["id"]}


@router.post("/jobs/auto/refresh")
def jobs_auto(_: None = Depends(require_auth)) -> dict:
    """惰性刷新入口：today 有数据 → 不触发；缺 → 同日去重自动触发。"""
    if _today_has_data():
        return {"triggered": False, "reason": "data_available", "auto": False}
    result = _lazy_auto_trigger()
    result["auto"] = True
    return result