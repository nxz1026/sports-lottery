"""web.services.jobs — 引擎子进程任务状态机 + 当日配额守卫（M3）。

规格（WO-M3）：
- 子进程跑 scripts/predict.py CLI（cwd=仓库根、sys.executable、独立 env、输出重定向 jobs/<id>.log）；
- 文件锁 jobs.lock 保证同时只有一个生产者；默认超时 600s 到点必杀；
- 状态机 queued/running/done/failed/timeout 写 jobs/<id>.json；
- 配额守卫 quota.json：BJT 日计数，跨日自动重置；无预算 → 429 + 原因。

测试安全网：本模块绝不 import scripts/ 引擎代码；子进程一律经 mock 的
 spawn 逻辑消费（测试 monkeypatch _Popen），运行时才真实执行。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from web import config
from web.errors import LockTimeout

BJT = ZoneInfo("Asia/Shanghai")
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
TERMINAL = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_TIMEOUT})

logger = logging.getLogger("web.jobs")


# --- 路径/工具 ------------------------------------------------------------

def _now_epoch() -> float:
    return time.time()


def _bjt_day_key() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


def job_id() -> str:
    return uuid.uuid4().hex[:12]


def _job_file(jid: str) -> Path:
    return config.JOBS_DIR / f"{jid}.json"


def _log_file(jid: str) -> Path:
    return config.JOBS_DIR / f"{jid}.log"


# --- 配额守卫（BJT 日口径）------------------------------------------------

def load_quota() -> dict:
    """读 quota.json；坏文件/缺目录 → 空计数（幂等重置）。"""
    if not config.QUOTA_FILE.is_file():
        return {"day": _bjt_day_key(), "count": 0}
    try:
        with open(config.QUOTA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.warning("坏配额文件，重置: %s", config.QUOTA_FILE)
        return {"day": _bjt_day_key(), "count": 0}
    if not isinstance(data, dict):
        return {"day": _bjt_day_key(), "count": 0}
    return data


def save_quota(data: dict) -> None:
    config.QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.QUOTA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, config.QUOTA_FILE)


def _quota_state(data: dict) -> tuple[str, int]:
    """把（可能跨日的）配额文件归一成 (今日 BJT 日键, 已用次数)。"""
    today = _bjt_day_key()
    if data.get("day") != today:
        return today, 0
    return today, int(data.get("count", 0))


def quota_usage() -> dict:
    """当日配额用量（跨 BJT 日自动重置）。"""
    today, used = _quota_state(load_quota())
    return {"day": today, "used": used, "limit": config.DAILY_TRIGGER_LIMIT}


def quota_exhausted() -> bool:
    """只读预检：当日配额是否已耗尽。不扣减，供"先检查后扣减"两段式使用。"""
    _, used = _quota_state(load_quota())
    return used >= config.DAILY_TRIGGER_LIMIT


def quota_consume() -> bool:
    """原子消费一次配额（读-改-写带文件锁）；超限返回 False。"""
    with _exclusive_lock(config.QUOTA_FILE.with_suffix(".lock"), timeout=5):
        today, count = _quota_state(load_quota())
        if count >= config.DAILY_TRIGGER_LIMIT:
            return False
        save_quota({"day": today, "count": count + 1})
        return True


# --- 文件锁 ---------------------------------------------------------------


@contextmanager
def _exclusive_lock(lock_path: Path, timeout: float = 10.0):
    """跨进程排他锁（O_CREAT|O_EXCL + stale 检测）；同进程可重入由调用方保证。

    Windows 注意事项：``os.unlink`` 偶发 WinError 32（"另一个进程正在使用此文件"），
    实测是文件系统把刚 ``os.close`` 的 fd 还没完全释放。直接吞掉会导致锁文件
    残留，下个 acquire 全卡死。这里用短退避重试（最多 ~200ms）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except (FileExistsError, PermissionError):
            # PermissionError 也走 stale 路径：Windows 上 unlink→下一个 O_EXCL
            # 之间偶发权限拒绝（OS 还没释放刚 unlink 的文件句柄），与 FileExists
            # 是同一个 race 的两个面。统一兜住走 _drop_stale_lock + sleep。
            _drop_stale_lock(lock_path)
            if time.monotonic() >= deadline:
                raise LockTimeout(f"lock busy: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        # 鲁棒 unlink：Windows 上偶发 WinError 32，10ms→20ms→40ms 三次重试即可。
        for delay in (0.01, 0.02, 0.04):
            try:
                os.unlink(lock_path)
                break
            except FileNotFoundError:
                break  # 已无锁文件，正常
            except OSError as exc:
                if delay == 0.04:
                    logger.warning("unlink 锁文件失败: %s (%s)", lock_path, exc)
                time.sleep(delay)


def _pid_alive(pid: int) -> bool:
    """检查 PID 是否存活（跨平台）。

    POSIX：``os.kill(pid, 0)`` 检查进程存在；ProcessLookupError=已死。
    Windows：``os.kill(pid, 0)`` 实际走 TerminateProcess，对 alive pid 也会
    投递信号。改用 OpenProcess + GetExitCodeProcess（PIL 缺失时回退 mtime 估算）。
    """
    if pid == os.getpid():
        return True  # 自身一定活着；调用方已通过 == os.getpid() 分流，此处兜底
    try:
        if os.name == "nt":
            # OpenProcess = PROCESS_QUERY_LIMITED_INFORMATION (0x1000) = 0x10 | 0x1000
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
            )
            if not handle:
                return False  # OpenProcess 失败 = 进程不存在
            try:
                code = wintypes.DWORD()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok and code.value == STILL_ACTIVE)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except PermissionError:
        # 审计 R45 修复：PermissionError ⊂ OSError，必须排在 OSError 之前，
        # 否则永不可达。"活进程但非本用户持有" → 视为存活（不删锁）。
        return True
    except (ProcessLookupError, OSError):
        return False


def _drop_stale_lock(lock_path: Path) -> None:
    """锁文件持 PID 超过 10 分钟视为 stale（进程被杀残留），删除。

    注意：Windows 的 ``os.kill(pid, 0)`` 在 pid 是当前进程时会无限阻塞
    （实测 PID 自身调用直接挂住），所以**同进程持有的锁**走短超时判废
    路径（同进程其他线程不会持有同名锁文件 → 短超时 = 已失效）。
    """
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if pid <= 0:
        return
    # 跨进程锁才有意义去查 PID liveness；同进程 pid == self，Windows 上
    # os.kill(self, 0) 会挂死。改用"短 mtime 阈值即视为 stale"兜底：
    # 锁的实际持锁时间以 ms 计，>5s 还在持必为线程崩溃或 unlink 失败残留。
    if pid == os.getpid():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return
        if age < 5.0:
            return
        # 同进程 mtime > 5s 强制删：覆盖 _exclusive_lock finally 里 unlink
        # 偶发 WinError 32 残留的场景 + 线程崩死没人 unlink 的场景。
        try:
            lock_path.unlink()
        except OSError:
            pass
        return
    alive = _pid_alive(pid)
    stale = not alive and (time.time() - lock_path.stat().st_mtime > 120)
    if stale:
        logger.warning("删除 stale 锁: %s", lock_path)
        try:
            lock_path.unlink()
        except OSError:
            pass


# --- 状态机读/写 ----------------------------------------------------------

def _read_job(jid: str) -> dict | None:
    """读取任务文件（带 Windows fd 释放延迟重试 — 与 ``_exclusive_lock`` 同语义）。"""
    path = _job_file(jid)
    if not path.is_file():
        return None
    # Windows 上的 transient PermissionError / OSError 三次重试共 ~30ms：
    #   - 同进程 worker 线程刚 ``_write_job`` 完成 ``os.replace``，尚未来得及彻底释放；
    #   - Windows Defender 冷启索引首撞 fs 持有 fd 数十毫秒；
    #   - 同时序的``os.replace`` 把文件名换成另一个 inode，旧 inode 短暂残留拒绝句柄复用。
    # 不区分原因 — 一律视为瞬态。
    last_exc: Exception | None = None
    for delay in (0.0, 0.01, 0.02):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError) as exc:
            last_exc = exc
            if delay:
                time.sleep(delay)
    logger.debug("_read_job %s fd 释放延迟重试耗尽: %s", jid, last_exc)
    return None


def _write_job(jid: str, data: dict) -> None:
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _job_file(jid).with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, _job_file(jid))


def _default_job(jid: str) -> dict:
    """新建/兜底共用的空任务字段（字段集与 create_job 一致）。"""
    return {
        "id": jid,
        "status": STATUS_QUEUED,
        "trigger": "manual",
        "script": "predict",
        "args": [],
        "created_at": _now_epoch(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "timeout": config.PREDICT_TIMEOUT_SECONDS,
    }


def create_job(args: list[str], trigger: str = "manual", script: str = "predict") -> dict:
    """登记 queued 任务（写状态文件），返回 job 记录。script: predict|ai_enrich。"""
    jid = job_id()
    job = _default_job(jid)
    job["trigger"] = trigger
    job["script"] = script
    job["args"] = args
    _write_job(jid, job)
    return job


def get_job(jid: str) -> dict | None:
    return _read_job(jid)


def list_jobs(limit: int = 20) -> list[dict]:
    if not config.JOBS_DIR.is_dir():
        return []
    rows = []
    for path in sorted(config.JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        data = _read_job(path.name.replace(".json", ""))
        if data:
            rows.append(data)
    return rows


def _spin_state(jid: str, status: str, **extra) -> dict:
    """带锁的状态迁移（同进程并发安全；跨进程由文件锁兜底）。

    Note: P3-21 试过 timeout 5s→10s 治 Windows fs 锁延迟，但全仓 15x 压测反
    而 4/15 失败（vs 1/15）。P3-22 revert 至 5s，让 ``_drain_jobs`` 的 10s
    poll 上限作为容错层；state.lock 本身 5s 是设计意图，5s+ 真的等就是
    LockTimeout，应该被 caller 当作"任务僵死"显式 fail-fast。
    """
    with _exclusive_lock(_job_file(jid).with_suffix(".state.lock"), timeout=5):
        job = _read_job(jid) or _default_job(jid)
        job["status"] = status
        job.update(extra)
        _write_job(jid, job)
        return job

def _write_state(jid: str, status: str, **extra) -> dict:
    """无 state.lock 的状态写入；仅限已持 JOBS_LOCK_FILE 的调用方使用。

    规定锁偏序：JOBS_LOCK_FILE > state.lock；凡持 JOBS_LOCK_FILE 者
    必须走此函数而非 _spin_state，避免嵌套取锁。
    """
    job = _read_job(jid) or _default_job(jid)
    job["status"] = status
    job.update(extra)
    _write_job(jid, job)
    return job


# --- 子进程执行 -----------------------------------------------------------

def _build_env() -> dict:
    """白名单透传 env 给子进程（避免全量 os.environ 泄漏敏感 key）。

    仅透传：
    - PATH / PYTHONPATH / PYTHONUNBUFFERED（解释器与 stdlib 必需）
    - LP_OUTPUT_DIR（引擎输出位置）
    - API_FOOTBALL_KEY 等 6 个外部 API 凭据（predict / predict_bball / ai_analyze 实际用到）
    - DB_*/DSN_* 系列 8 个变量（psycopg 连接）

    显式排除（web-only）：
    - AUTH_USERNAME / AUTH_PASSWORD / SESSION_* / LOGIN_* / USE_HTTPS
    - WEB_HOST / WEB_PORT / CORS_ORIGINS
    - SESSION_DB_PATH
    - 其它任何未在白名单的变量（最小特权原则）

    测试可通过 monkeypatch / 测试 fixture 注入覆盖（`_build_env` 仍是模块
    级可调用函数，方便 patching）。
    """
    ALLOW = (
        # 解释器 / stdlib / 引擎输出
        "PATH", "PYTHONPATH", "PYTHONUNBUFFERED", "LP_OUTPUT_DIR",
        # 外部 API（仅引擎子进程实际调用）
        "API_FOOTBALL_KEY", "API_FOOTBALL_ENRICH_ENABLED", "API_FOOTBALL_ENABLED",
        "API_FOOTBALL_BASE_URL", "API_FOOTBALL_DAILY_LIMIT", "API_FOOTBALL_MINUTE_LIMIT",
        "FOOTBALL_DATA_API_KEY",
        "NBA_API_KEY", "ODDS_API_KEY",
        "LLM_API_KEY", "LLM_API_BASE", "LLM_MODEL",
        # 数据库连接
        "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_ING_USER", "DB_ING_PASSWORD",
        "DB_APP_USER", "DB_APP_PASSWORD",
        "DB_RO_USER", "DB_RO_PASSWORD",
        "DSN_ING", "DSN_APP", "DSN_RO",
        # Windows 习惯
        "SYSTEMROOT", "TEMP", "TMP",
    )
    return {k: os.environ[k] for k in ALLOW if k in os.environ}


def _build_cmd(args: list[str], script: str = "predict") -> list[str]:
    """按脚本名拼 CLI；篮球脚本使用独立运行入口。"""
    if script == "predict":
        return [sys.executable, str(config.BASE_DIR / "scripts" / "predict.py"), *args]
    if script == "predict_bball":
        return [sys.executable, str(config.BASE_DIR / "scripts" / "bball" / "run.py"), *args]
    if script == "ai_analyze":
        return [sys.executable, "-m", "web.services.ai_analyze", *args]
    return [sys.executable, "-m", "web.enrich"]


def run_job(jid: str) -> None:
    """执行一个 queued 任务到终态（同步；调用方负责不阻塞请求线程）。"""
    # 原子步：确认 queued → running（持 JOBS_LOCK_FILE 仅限此窗口）
    with _exclusive_lock(config.JOBS_LOCK_FILE, timeout=10):
        job = _read_job(jid)
        if job is None or job.get("status") not in (STATUS_QUEUED, STATUS_RUNNING):
            return
        _write_state(jid, STATUS_RUNNING, started_at=_now_epoch())
        args = list(job.get("args", []))
        script = job.get("script", "predict")
        cmd = _build_cmd(args, script)
    # 锁已释放：spawn、等待、写终态均不持 JOBS_LOCK_FILE
    log_path = _log_file(jid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = _now_epoch()
    try:
        with open(log_path, "wb") as log_fh:
            proc = subprocess.Popen(
                cmd, cwd=str(config.BASE_DIR), env=_build_env(),
                stdout=log_fh, stderr=subprocess.STDOUT, text=False,
            )
    except OSError as exc:
        # C.1 (Round 10): _spin_state 自身可能 OSError（lock timeout / fs quota
        # 等），需 catch-all 兜底落 events.web.jsonl，而不是交给 threading.excepthook
        # 单一通道失败。
        try:
            _spin_state(jid, STATUS_FAILED, finished_at=_now_epoch(),
                        exit_code=-1, error=f"spawn failed: {exc}")
        except Exception as spin_exc:
            try:
                from core import observability
                observability.log_error(spin_exc,
                    context="jobs.spin_state_after_spawn_failed",
                    jid=jid, original=str(exc)[:200])
            except Exception:
                pass
        return
    try:
        code = proc.wait(timeout=config.PREDICT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            _spin_state(jid, STATUS_TIMEOUT, finished_at=_now_epoch(),
                        exit_code=None, error="timeout killed")
        except Exception as spin_exc:
            try:
                from core import observability
                observability.log_error(spin_exc,
                    context="jobs.spin_state_after_timeout",
                    jid=jid)
            except Exception:
                pass
    else:
        try:
            if code == 0:
                _spin_state(jid, STATUS_DONE, finished_at=_now_epoch(), exit_code=0)
            else:
                # 非零退出必须留下可诊断原因：引擎把真实报错写在 stdout/stderr 日志里，
                # 只记 exit_code 会让失败任务在 API/看板上显示 error=null。
                _spin_state(jid, STATUS_FAILED, finished_at=_now_epoch(),
                            exit_code=code, error=_failure_hint(log_path, code))
        except Exception as spin_exc:
            try:
                from core import observability
                observability.log_error(spin_exc,
                    context="jobs.spin_state_after_nonzero_exit",
                    jid=jid, exit_code=code, failure_hint=_failure_hint(log_path, code)[:200])
            except Exception:
                pass
    finally:
        if proc.poll() is None:
            proc.kill()
        logger.info("job %s done in %.1fs", jid, _now_epoch() - started)


# --- 线程池投递 -----------------------------------------------------------

# 引擎子进程执行不阻塞请求线程（fire-and-forget：失败只写状态文件）。
# P3-1：模块级 executor，跨测试 / 跨 worker 进程共享。shutdown 不走 lifespan
# （lifespan 在 TestClient ``with`` 块退出时也会触发，会把后续测试的 executor
# 提前关掉 → ``RuntimeError: cannot schedule new futures after shutdown``），
# 改用 atexit 在解释器退出时收尾。
import atexit

executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

_atexit_registered = False


def _atexit_shutdown_executor() -> None:
    """进程退出时 drain 任务，避免半截崩 + BoundedSemaphore 句柄泄漏。

    ``wait=False`` + ``cancel_futures=True``：Python 3.9+ 的标准收尾姿势——
    不等已 in-flight 的子进程结束（worker 进程退出由 systemd/SIGTERM 控），
    但取消还没起跑的 pending 任务，释放池子资源。
    """
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass


if not _atexit_registered:
    atexit.register(_atexit_shutdown_executor)
    _atexit_registered = True


def submit_job(jid: str) -> None:
    """向线程池投递任务执行（调用方不等待结果）。"""
    executor.submit(run_job, jid)


def mark_failed(jid: str, error: str) -> None:
    """把任务标记为 failed（供路由层 submit 异常兜底）。"""
    _spin_state(jid, STATUS_FAILED, finished_at=_now_epoch(), error=error)


def read_job_log(jid: str, tail: int = 200) -> str:
    path = _log_file(jid)
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-tail:])


def _failure_hint(log_path: Path, code: int, limit: int = 300) -> str:
    """非零退出的可诊断摘要：退出码 + 日志最后一行非空内容。

    引擎（predict/enrich）把真实报错写在 stdout/stderr 日志里；只记 exit_code
    会让 API 与看板上的失败任务显示 ``error=null``，无从定位。
    """
    tail = ""
    try:
        text = log_path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        text = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            tail = stripped[:limit]
            break
    return f"exit {code}: {tail}" if tail else f"exit {code}"


def active_job() -> dict | None:
    """当前 running/queued 的任务（含过期孤儿回收：超时无进展 → failed）。"""
    now = _now_epoch()
    for row in list_jobs(limit=100):
        if row.get("status") in TERMINAL:
            continue
        jid = row.get("id", "")
        base = row.get("started_at") or row.get("created_at") or 0
        limit = (row.get("timeout") or config.PREDICT_TIMEOUT_SECONDS) * 2
        if row.get("status") == STATUS_RUNNING and now - base > limit:
            _spin_state(jid, STATUS_FAILED, finished_at=now, error="orphan recovered")
            continue
        if row.get("status") == STATUS_QUEUED and now - (row.get("created_at") or 0) > 300:
            _spin_state(jid, STATUS_FAILED, finished_at=now, error="orphan queued expired")
            continue
        return row
    return None


def _spawn(script: str, extra_argv: list[str], trigger: str = "manual") -> tuple[dict | None, str | None]:
    """公共提交链路：配额守卫 + 并发守卫 + 登记。返回 (job 或 None, 拒绝原因)。

    契约：任务提交即占配额（predict 与 ai_enrich 共享同一计数器）；
    并发有新任务时返回 (existing, "already_running")。
    锁偏序：JOBS_LOCK_FILE > quota.lock（quota_consume 内部持 quota.lock）。
    """
    with _exclusive_lock(config.JOBS_LOCK_FILE, timeout=10):
        # 两段式：先"检查"再"扣减"。被 409 拒绝的请求没有产生任何任务，
        # 绝不能扣配额——旧代码先扣后判，实测 3 并发得 1×202 + 2×409，
        # quota.json count 4→7，两次被拒请求永久吃掉当日预算。
        # 顺序上配额优先于并发：两者同时成立时报"额度已用尽"
        # （既有契约见 test_quota_shared_between_predict_and_ai_enrich）。
        if quota_exhausted():
            return None, "quota_exhausted"
        existing = active_job()
        if existing is not None:
            return existing, "already_running"
        if not quota_consume():
            return None, "quota_exhausted"
        job = create_job(extra_argv, trigger=trigger, script=script)
        return job, None


def trigger_predict(args: list[str], trigger: str = "manual") -> tuple[dict | None, str | None]:
    """提交预测任务（脚本 scripts/predict.py）。"""
    return _spawn("predict", args, trigger)


def trigger_bball(args: list[str], trigger: str = "manual") -> tuple[dict | None, str | None]:
    """提交篮球预测任务（脚本 scripts/bball/run.py，独立入口）。

    与足球预测共用同一配额计数器与并发守卫（_spawn 的契约）——篮球与足球不能同时跑，
    避免两个引擎在同一小时内争抢同一批 API 配额。
    """
    return _spawn("predict_bball", args, trigger)


def trigger_ai_enrich(trigger: str = "manual") -> tuple[dict | None, str | None]:
    """提交 AI 富化任务（python -m web.enrich，argv 固定为空）。"""
    return _spawn("ai_enrich", [], trigger)


def trigger_ai_analyze(date_str: str | None = None,
                       trigger: str = "manual") -> tuple[dict | None, str | None]:
    """提交 AI 分析任务（python -m web.services.ai_analyze [YYYY-MM-DD]）。

    与 predict/ai_enrich 共享同一配额计数器（_spawn 的契约）——AI 分析异步执行、
    不阻塞主预测链路；任何内部异常被 jobs._spawn 捕获，不会拖累 predict 主流程。
    """
    args = [date_str] if date_str else []
    return _spawn("ai_analyze", args, trigger)
