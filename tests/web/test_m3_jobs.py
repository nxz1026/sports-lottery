"""M3 测试：任务触发 + 配额守卫 + 并发/超时 + AI 降级（WO-M3 验收点 B/C/D/E）。

测试安全网：子进程一律假 Popen（monkeypatch web.services.jobs.subprocess），
绝不真实执行引擎；jobs/quota 数据目录全部指向 tmp_path，绝不触碰 web/.data。
"""
from __future__ import annotations

import importlib
import json
import subprocess as _real_subprocess
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BJT = timezone(timedelta(hours=8))


def _fake_sp(proc_cls) -> types.SimpleNamespace:
    """假 subprocess 模块：Popen 可换、STDOUT 常量、TimeoutExpired 用真类。"""
    return types.SimpleNamespace(
        Popen=proc_cls, STDOUT=0, TimeoutExpired=_real_subprocess.TimeoutExpired,
    )


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """临时 env + 临时 jobs/quota 数据目录 + 应用实例。"""
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    monkeypatch.setenv("PREDICT_DAILY_LIMIT", "80")
    monkeypatch.setenv("AUTO_REFRESH_DAILY", "1")

    import web.config as config
    import web.session_store as session_store
    config = importlib.reload(config)
    session_store = importlib.reload(session_store)
    import web.auth as auth_mod
    importlib.reload(auth_mod)

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "QUOTA_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(config, "JOBS_LOCK_FILE", tmp_path / "jobs.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    import web.services.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "_build_env", lambda: {})
    monkeypatch.setattr(jobs_mod, "_build_cmd", lambda args, script="predict": ["python3", "predict.py", *args])

    from web.api import create_app
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def jobs_env(tmp_path, monkeypatch):
    """jobs 模块直测环境：全部数据路径指向 tmp_path。"""
    import web.config as config
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "QUOTA_FILE", tmp_path / "quota.json")
    monkeypatch.setattr(config, "JOBS_LOCK_FILE", tmp_path / "jobs.lock")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    import web.services.jobs as jobs_mod
    return jobs_mod


def _login(client):
    res = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass"})
    assert res.status_code == 200, res.text


def _drain_jobs(timeout: float = 5.0) -> None:
    """等后台任务线程写完结态后再退出用例。

    ``web.services.jobs.executor`` 是**模块级**线程池：用例结束时若线程仍在跑，
    monkeypatch 撤销后 ``_spin_state`` 会读到真实 ``web/.data/jobs``（找不到 tmp
    目录里的任务文件）→ 走 ``_default_job`` 兜底，把任务"重建"成一条
    ``started_at=null`` 的垃圾记录写进生产数据目录，污染看板的「任务状态」。
    本函数保证线程在 monkeypatch 生效期内收尾。
    """
    import web.services.jobs as jobs_mod
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs_mod.active_job() is None:
            return
        time.sleep(0.02)
    raise AssertionError("后台任务线程未在超时内结束，测试隔离可能已破坏")


# --- 验收点 B：并发双触发只起一个子进程 -----------------------------------

class _OkProc:
    """正常完成的假子进程：wait 返回 0。"""

    def __init__(self, cmd, **kw):
        self._killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._killed = True

    def poll(self):
        return 0 if not self._killed else None


class _BlockProc:
    """阻塞门控假子进程：wait 挂起直到 gate 释放（制造 running 窗口）。"""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.gate = threading.Event()

    def wait(self, timeout=None):
        self.gate.wait(5)
        return 0

    def kill(self):
        self.gate.set()

    def poll(self):
        return None


def test_concurrent_trigger_only_one_producer(client, monkeypatch):
    """验收点 B：并发双触发 → 第二个 409 already_running，只起一个子进程。"""
    import web.services.jobs as jobs_mod

    recorded = []
    gate = threading.Event()

    class GateProc(_BlockProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            # 必须复用用例持有的 gate：_BlockProc 自建的事件无人 set，
            # 会让 wait() 空等满 5s 才放行（线程悬挂到用例之后）。
            self.gate = gate
            recorded.append(cmd)

    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(GateProc))
    _login(client)
    try:
        r1 = client.post("/api/v1/jobs/predict", json={"league": "epl"})
        assert r1.status_code == 202
        r2 = client.post("/api/v1/jobs/predict", json={"league": "epl"})
        assert r2.status_code == 409
        body = r2.json()
        assert body["code"] == "already_running"
        assert body["job"]["status"] in ("queued", "running")
        time.sleep(0.2)
        assert len(recorded) == 1
        jobs = client.get("/api/v1/jobs").json()["jobs"]
        assert len(jobs) == 1
    finally:
        gate.set()
        # 放行门控后必须等后台线程写完结态，否则它会在 monkeypatch 撤销后
        # 落到真实 web/.data/jobs（见 _drain_jobs 说明）。
        _drain_jobs()


# --- 验收点 D：状态机轮询到终态 + timeout 路径 ------------------------------

def test_job_poll_to_terminal(client, monkeypatch):
    """POST 202 → GET 轮询到 done（假 Popen 立即成功）。"""
    import web.services.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(_OkProc))
    _login(client)
    r = client.post("/api/v1/jobs/predict", json={})
    assert r.status_code == 202
    jid = r.json()["job"]["id"]
    status = None
    for _ in range(100):
        body = client.get(f"/api/v1/jobs/{jid}").json()["job"]
        status = body["status"]
        if status in ("done", "failed", "timeout"):
            break
        time.sleep(0.05)
    assert status == "done"
    assert body["exit_code"] == 0


class _TimeoutProc:
    def __init__(self, cmd, **kw):
        pass

    def wait(self, timeout=None):
        raise _real_subprocess.TimeoutExpired(cmd="x", timeout=timeout or 1)

    def kill(self):
        pass

    def poll(self):
        return None


def test_run_job_timeout(jobs_env, monkeypatch):
    """超时必杀：wait 抛 TimeoutExpired → 终态 timeout + error 标记。"""
    jid = jobs_env.create_job(["--league", "epl"])["id"]
    monkeypatch.setattr(jobs_env, "subprocess", _fake_sp(_TimeoutProc))
    jobs_env.run_job(jid)
    job = jobs_env.get_job(jid)
    assert job["status"] == "timeout"
    assert job["error"] == "timeout killed"
    assert job["finished_at"] is not None


def test_run_job_failed_exit_code(jobs_env, monkeypatch):
    class FailProc(_TimeoutProc):
        def wait(self, timeout=None):
            return 2

    jid = jobs_env.create_job([])["id"]
    monkeypatch.setattr(jobs_env, "subprocess", _fake_sp(FailProc))
    jobs_env.run_job(jid)
    job = jobs_env.get_job(jid)
    assert job["status"] == "failed"
    assert job["exit_code"] == 2


def test_run_job_done(jobs_env, monkeypatch):
    jid = jobs_env.create_job([])["id"]
    monkeypatch.setattr(jobs_env, "subprocess", _fake_sp(_OkProc))
    jobs_env.run_job(jid)
    job = jobs_env.get_job(jid)
    assert job["status"] == "done"
    assert job["exit_code"] == 0


def test_run_job_spawn_error(jobs_env, monkeypatch):
    class BoomProc:
        def __init__(self, cmd, **kw):
            raise OSError("no such binary")

    jid = jobs_env.create_job([])["id"]
    monkeypatch.setattr(jobs_env, "subprocess", _fake_sp(BoomProc))
    jobs_env.run_job(jid)
    job = jobs_env.get_job(jid)
    assert job["status"] == "failed"
    assert "spawn failed" in job.get("error", "")


# --- 验收点 E：配额耗尽 → 429 ----------------------------------------------

def test_quota_exhausted_429(app, monkeypatch):
    import web.config as config
    import web.services.jobs as jobs_mod

    monkeypatch.setattr(config, "DAILY_TRIGGER_LIMIT", 2)
    assert jobs_mod.quota_consume() is True
    assert jobs_mod.quota_consume() is True
    client = TestClient(app)
    _login(client)
    r = client.post("/api/v1/jobs/predict", json={})
    assert r.status_code == 429
    assert r.json()["code"] == "quota_exhausted"


def test_quota_consume_limited(jobs_env, monkeypatch):
    import web.config as config
    monkeypatch.setattr(config, "DAILY_TRIGGER_LIMIT", 2)
    assert jobs_env.quota_consume() is True
    assert jobs_env.quota_consume() is True
    assert jobs_env.quota_consume() is False


def test_quota_reset_on_new_day(jobs_env, tmp_path):
    """跨 BJT 日自动重置：昨日计数文件 → used 归零。"""
    yesterday = (datetime.now(BJT) - timedelta(days=1)).strftime("%Y-%m-%d")
    (tmp_path / "quota.json").write_text(
        json.dumps({"day": yesterday, "count": 79}), encoding="utf-8")
    usage = jobs_env.quota_usage()
    assert usage["used"] == 0
    assert usage["day"] == datetime.now(BJT).strftime("%Y-%m-%d")


# --- 惰性刷新：同日去重 -----------------------------------------------------

def test_auto_refresh_trigger_and_dedup(client, tmp_path, monkeypatch):
    import web.services.jobs as jobs_mod
    import web.services.store as store_mod
    monkeypatch.setattr(store_mod, "OUTPUT_DIR", tmp_path / "empty")
    # 本用例经 API 真实投递任务：必须假 Popen（模块契约：测试绝不真跑引擎），
    # 并在退出前排空线程，否则会污染真实 web/.data/jobs。
    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(_OkProc))
    _login(client)
    r1 = client.post("/api/v1/jobs/auto/refresh")
    assert r1.json()["triggered"] is True
    r2 = client.post("/api/v1/jobs/auto/refresh")
    assert r2.json()["triggered"] is False
    assert r2.json()["reason"] == "already_today"
    _drain_jobs()


# --- 验收点 C：AI 模块改坏后 app 仍能起 -------------------------------------

def test_ai_broken_app_starts(tmp_path, monkeypatch):
    """验收点 C：web/services/ai.py 人为改坏 → app 能起、其余端点 200。"""
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    import web.config as config_mod
    importlib.reload(config_mod)
    import web.api as api_mod

    ai_path = Path(api_mod.__file__).resolve().parent / "services" / "ai.py"
    orig = ai_path.read_text(encoding="utf-8")
    try:
        ai_path.write_text("_broken = 1 / 0  # M3 test break\n", encoding="utf-8")
        # sys.modules 值为 None → import 抛 ImportError（标准注入法，reload 可复现）
        monkeypatch.setitem(sys.modules, "web.routers.ai", None)
        api_mod = importlib.reload(api_mod)  # 顶层 try-import 捕获 → ai_router=None
        assert api_mod.ai_router is None
        app = api_mod.create_app()
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/api/v1/ai/status").status_code == 404  # 路由降级跳过
            res = client.post("/api/v1/login", json={
                "username": "unit-test-user", "password": "unit-test-pass"})
            assert res.status_code == 200
            assert client.get("/api/v1/sources/status").status_code == 200
            assert client.get("/api/v1/jobs").status_code == 200
    finally:
        ai_path.write_text(orig, encoding="utf-8")
        sys.modules.pop("web.services.ai", None)
        sys.modules.pop("web.routers.ai", None)
        importlib.import_module("web.services.ai")
        importlib.import_module("web.routers.ai")
        importlib.reload(api_mod)


# --- AI 正常路径（降级语义） ------------------------------------------------

def test_ai_status_endpoints(client, tmp_path, monkeypatch):
    import web.services.ai as ai_mod
    p = tmp_path / "ai_scores.json"
    p.write_text(json.dumps({"Arsenal vs Chelsea": {
        "ai_score": 88, "ai_summary": "s", "league": "epl", "source": "epl"}}),
        encoding="utf-8")
    monkeypatch.setattr(ai_mod, "AI_SCORES_FILE", p)
    _login(client)
    r = client.get("/api/v1/ai/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["matches"] == 1
    assert body["by_league"] == {"epl": 1}


# --- 鉴权矩阵补充 -----------------------------------------------------------

def test_jobs_require_auth(client):
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.post("/api/v1/jobs/predict", json={}).status_code == 401
    assert client.post("/api/v1/jobs/auto/refresh").status_code == 401


class TestOrphanRecovery:
    def test_stale_running_is_recovered(self, client, tmp_path, monkeypatch):
        """running 超过 2*timeout → active_job 回收为 failed 并放行新任务。"""
        import time as _t
        from web.services import jobs as jb
        stale = jb.create_job([], trigger="test")
        jb._spin_state(stale["id"], jb.STATUS_RUNNING, started_at=_t.time() - 99999)
        assert jb.active_job() is None
        reloaded = jb.get_job(stale["id"])
        assert reloaded["status"] == jb.STATUS_FAILED
        assert "orphan" in (reloaded.get("error") or "")

def test_rejected_trigger_does_not_consume_quota(client, monkeypatch):
    """被 409 拒绝的请求没有产生任何任务，不得扣减当日配额。

    旧行为：quota_consume() 排在 active_job() 之前 → 实测 3 并发得 1×202 + 2×409，
    quota.json count 4→7，两次被拒请求永久吃掉当日预算（反复点击即可耗尽额度）。
    """
    import web.services.jobs as jobs_mod

    gate = threading.Event()

    class GateProc(_BlockProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.gate = gate

    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(GateProc))
    _login(client)
    try:
        assert client.post("/api/v1/jobs/predict", json={"league": "epl"}).status_code == 202
        used_after_first = jobs_mod.quota_usage()["used"]
        for _ in range(3):
            r = client.post("/api/v1/jobs/predict", json={"league": "epl"})
            assert r.status_code == 409
            assert r.json()["code"] == "already_running"
        assert jobs_mod.quota_usage()["used"] == used_after_first, \
            "被 409 拒绝的请求不得扣减配额"
    finally:
        gate.set()
        _drain_jobs()


# --- 篮球（NBA）独立入口：WO 2026-09-18 -------------------------------------

def test_bball_build_cmd_uses_bball_entry(jobs_env):
    """predict_bball 必须指向 scripts/bball/run.py（独立入口），不是足球的 predict.py。"""
    cmd = jobs_env._build_cmd(["--ahead-days", "90"], "predict_bball")
    joined = " ".join(cmd)
    assert joined.endswith("scripts/bball/run.py --ahead-days 90"), joined
    assert "predict.py" not in joined


def test_bball_route_202_and_script(client, monkeypatch):
    import web.services.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(_OkProc))
    _login(client)
    try:
        r = client.post("/api/v1/jobs/predict-bball", json={"ahead_days": 90})
        assert r.status_code == 202, r.text
        job = r.json()["job"]
        assert job["script"] == "predict_bball"
        assert job["args"] == ["--ahead-days", "90"]
    finally:
        _drain_jobs()


def test_bball_route_rejects_bad_args(client):
    _login(client)
    for bad in ({"ahead_days": 0}, {"ahead_days": 999}, {"ahead_days": "90"},
                {"ahead_days": True}, {"backtest": -1}, {"foo": 1}):
        r = client.post("/api/v1/jobs/predict-bball", json=bad)
        assert r.status_code == 400, (bad, r.status_code)
        assert r.json()["code"] == "invalid_params"


def test_bball_requires_auth(client):
    assert client.post("/api/v1/jobs/predict-bball", json={}).status_code == 401


def test_bball_shares_quota_and_concurrency_with_football(client, monkeypatch):
    """篮球与足球共用配额与并发守卫：一方在跑，另一方 409；配额只扣一次。"""
    import web.config as config
    import web.services.jobs as jobs_mod
    monkeypatch.setattr(config, "DAILY_TRIGGER_LIMIT", 5)
    gate = threading.Event()

    class GateProc(_BlockProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            self.gate = gate

    monkeypatch.setattr(jobs_mod, "subprocess", _fake_sp(GateProc))
    _login(client)
    try:
        assert client.post("/api/v1/jobs/predict", json={"league": "epl"}).status_code == 202
        used = jobs_mod.quota_usage()["used"]
        r = client.post("/api/v1/jobs/predict-bball", json={"ahead_days": 90})
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "already_running"
        assert jobs_mod.quota_usage()["used"] == used, "被拒的篮球请求不得扣配额"
    finally:
        gate.set()
        _drain_jobs()
