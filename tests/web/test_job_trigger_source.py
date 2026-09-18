"""作业来源标记（trigger）测试。

背景：三个作业端点此前把 trigger **硬编码为 "manual"**，于是
`ops/league-daily-predict.timer` 触发的作业与页面上手点的作业在
Dashboard「数据源与任务」页完全无法区分——定时任务到底跑没跑无法自证。
现改为接受可选的 trigger 字段（白名单 manual/timer/cron/auto），
白名单外一律回落 manual 且**不报 400**（来源标记是观测字段，不该让请求失败）。

安全网：与 test_m3_jobs 一致——假 _build_cmd，绝不真实执行引擎；
jobs/quota 数据目录全部指向 tmp_path，绝不触碰 web/.data。
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def app(tmp_path, monkeypatch):
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
    monkeypatch.setattr(jobs_mod, "_build_cmd",
                        lambda args, script="predict": ["python3", "predict.py", *args])

    from web.api import create_app
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _login(client: TestClient) -> None:
    res = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass"})
    assert res.status_code == 200, res.text


def _drain_jobs(timeout: float = 5.0) -> None:
    """等后台任务线程写完结态后再退出用例（否则撤销 monkeypatch 后会污染生产数据目录）。"""
    import web.services.jobs as jobs_mod
    deadline = time.time() + timeout
    while time.time() < deadline:
        if jobs_mod.active_job() is None:
            return
        time.sleep(0.02)
    raise AssertionError("后台任务线程未在超时内结束，测试隔离可能已破坏")


# --- _pop_trigger 单元语义 ------------------------------------------------

def test_pop_trigger_defaults_to_manual():
    from web.routers.jobs import _pop_trigger
    assert _pop_trigger({}) == "manual"


def test_pop_trigger_accepts_timer():
    from web.routers.jobs import _pop_trigger
    assert _pop_trigger({"trigger": "timer"}) == "timer"


@pytest.mark.parametrize("value", ["manual", "timer", "cron", "auto"])
def test_pop_trigger_whitelist_all_pass_through(value):
    from web.routers.jobs import _pop_trigger
    assert _pop_trigger({"trigger": value}) == value


@pytest.mark.parametrize("bad", ["", "scheduler", "MANUAL", "Timer", None, 123, ["timer"]])
def test_pop_trigger_unknown_falls_back_to_manual(bad):
    """白名单外一律 manual——来源标记不该让请求失败。"""
    from web.routers.jobs import _pop_trigger
    assert _pop_trigger({"trigger": bad}) == "manual"


def test_pop_trigger_removes_key_from_params():
    """必须 pop 掉，否则 _validate_args 会把 trigger 当未知参数拒掉（400）。"""
    from web.routers.jobs import _pop_trigger, _validate_args
    params = {"all": True, "trigger": "timer"}
    assert _pop_trigger(params) == "timer"
    assert "trigger" not in params
    assert _validate_args(params) == ["--all"]


def test_trigger_key_not_leaked_into_argv():
    """trigger 是观测字段，绝不能出现在传给引擎的 argv 里。"""
    from web.routers.jobs import _pop_trigger, _validate_args
    params = {"all": True, "trigger": "timer"}
    _pop_trigger(params)
    assert "timer" not in _validate_args(params)


def test_all_flag_emitted_once():
    """回归：--all 曾在 _validate_args 里被追加两次（显式分支 + _FLAG_ARGS 都含它），
    作业记录里 args 恒为 ['--all','--all']。argparse 能容忍，但记录不该带噪声。"""
    from web.routers.jobs import _validate_args
    assert _validate_args({"all": True}) == ["--all"]
    assert _validate_args({"all": True, "league": "epl"}) == ["--all", "--league", "epl"]
    assert _validate_args({"all": False}) == []


def test_other_flag_args_still_work():
    """去掉 --all 后，其余 flag 型参数必须照旧生效。"""
    from web.routers.jobs import _validate_args
    assert _validate_args({"monte_carlo": True}) == ["--monte-carlo"]
    assert _validate_args({"no_dc": True, "no_ml": True}) == ["--no-dc", "--no-ml"]


# --- 端点集成：trigger 落到 job 记录 --------------------------------------

def test_predict_defaults_to_manual_trigger(client):
    _login(client)
    res = client.post("/api/v1/jobs/predict", json={"all": True})
    assert res.status_code == 202, res.text
    assert res.json()["job"]["trigger"] == "manual"
    _drain_jobs()


def test_predict_accepts_timer_trigger(client):
    _login(client)
    res = client.post("/api/v1/jobs/predict", json={"all": True, "trigger": "timer"})
    assert res.status_code == 202, res.text
    body = res.json()["job"]
    assert body["trigger"] == "timer"
    assert body["args"] == ["--all"]
    _drain_jobs()


def test_predict_unknown_trigger_is_manual_not_400(client):
    """白名单外的 trigger 不能把请求打成 400（旧实现会因未知参数 400）。"""
    _login(client)
    res = client.post("/api/v1/jobs/predict", json={"all": True, "trigger": "whatever"})
    assert res.status_code == 202, res.text
    assert res.json()["job"]["trigger"] == "manual"
    _drain_jobs()


def test_predict_bball_accepts_timer_trigger(client):
    _login(client)
    res = client.post("/api/v1/jobs/predict-bball",
                      json={"ahead_days": 90, "trigger": "timer"})
    assert res.status_code == 202, res.text
    body = res.json()["job"]
    assert body["trigger"] == "timer"
    assert body["args"] == ["--ahead-days", "90"]
    assert body["script"] == "predict_bball"
    _drain_jobs()


def test_ai_enrich_accepts_timer_trigger(client):
    _login(client)
    res = client.post("/api/v1/jobs/ai-enrich", json={"trigger": "timer"})
    assert res.status_code == 202, res.text
    body = res.json()["job"]
    assert body["trigger"] == "timer"
    assert body["args"] == []
    assert body["script"] == "ai_enrich"
    _drain_jobs()


def test_ai_enrich_without_body_defaults_to_manual(client):
    """旧调用方（不带 body）必须继续可用。"""
    _login(client)
    res = client.post("/api/v1/jobs/ai-enrich")
    assert res.status_code == 202, res.text
    assert res.json()["job"]["trigger"] == "manual"
    _drain_jobs()


def test_timer_trigger_visible_in_job_list(client):
    """Dashboard「数据源与任务」页读 /jobs，必须能看到 timer 标记。"""
    _login(client)
    client.post("/api/v1/jobs/predict", json={"all": True, "trigger": "timer"})
    _drain_jobs()
    res = client.get("/api/v1/jobs")
    assert res.status_code == 200, res.text
    triggers = [j["trigger"] for j in res.json()["jobs"]]
    assert "timer" in triggers
