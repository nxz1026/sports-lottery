"""P2-C1/C2 单元测试：sources router 暴露 next_runs + last_failure。

辅助函数 ``list_timer_runs`` / ``recent_job_stats`` 放在 ``web/services/source_meta.py``
（零 web 框架依赖），便于本地 pytest 不依赖 fastapi 直接覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# 与 tests/web/* 一致：把仓库根 + scripts/ 加进 sys.path
REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── list_timer_runs：systemctl list-timers 行解析 ────────────────────────

def test_list_timer_runs_parses_standard_table():
    """systemctl list-timers 标准输出 → next_runs 字典（BJT 8h 偏移）。"""
    sample = (
        "NEXT                        LEFT LAST                        PASSED UNIT                       ACTIVATES\n"
        "Mon 2026-10-12 01:30:00 UTC  14h Sun 2026-10-11 01:30:01 UTC 9h ago league-daily-predict.timer league-daily-predict.service\n"
        "1 timers listed.\n"
    )
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = sample
    with patch("subprocess.run", return_value=fake_proc):
        from web.services.source_meta import list_timer_runs
        out = list_timer_runs()
    assert "league-daily-predict.timer" in out
    info = out["league-daily-predict.timer"]
    # 2026-10-12 01:30:00 UTC = 2026-10-12 09:30:00 BJT
    assert info["next_bjt"] == "2026-10-12 09:30:00"
    assert info["next_utc"].startswith("2026-10-12T01:30:00")
    assert info["last_bjt"] == "2026-10-11 09:30:01"


def test_list_timer_runs_silent_when_systemctl_missing():
    """systemctl 不存在（开发机 / macOS） → 静默返回空 dict，绝不抛。"""
    with patch("subprocess.run", side_effect=FileNotFoundError("no systemctl")):
        from web.services.source_meta import list_timer_runs
        out = list_timer_runs()
    assert out == {}


def test_list_timer_runs_silent_on_nonzero_returncode():
    """systemctl hang / 权限缺失 → 静默返回空 dict，绝不阻塞状态面。"""
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stdout = ""
    with patch("subprocess.run", return_value=fake_proc):
        from web.services.source_meta import list_timer_runs
        out = list_timer_runs()
    assert out == {}


# ── recent_job_stats：last_failure + by_status + items ───────────────────

def test_recent_job_stats_exposes_last_failure():
    """list_jobs 包含 failed 作业时，last_failure 字段携带溯源信息。"""
    sample_rows = [
        {"id": "ok1", "script": "predict", "trigger": "timer",
         "created_at": 100.0, "finished_at": 200.0, "status": "done",
         "exit_code": 0, "error": None},
        {"id": "fail1", "script": "ai_enrich", "trigger": "manual",
         "created_at": 300.0, "finished_at": 400.0, "status": "failed",
         "exit_code": 1, "error": "OpenAI rate limit"},
        {"id": "ok2", "script": "predict_bball", "trigger": "timer",
         "created_at": 500.0, "finished_at": 600.0, "status": "done",
         "exit_code": 0, "error": None},
    ]
    from web.services.source_meta import recent_job_stats
    # 实际注入 list_jobs；用普通 callable 而不是 mock，因为是参数化函数。
    stats = recent_job_stats(lambda limit=50: sample_rows)
    assert stats["by_status"]["done"] == 2
    assert stats["by_status"]["failed"] == 1
    assert stats["last_failure"]["id"] == "fail1"
    assert stats["last_failure"]["script"] == "ai_enrich"
    assert stats["last_failure"]["exit_code"] == 1
    assert stats["last_failure"]["error"] == "OpenAI rate limit"
    # 同样含 limit=10 的 items 用于表格
    assert len(stats["items"]) == 3
    assert stats["items"][1]["id"] == "fail1"


def test_recent_job_stats_no_failure_yields_null():
    """全 done / running 时 last_failure=None，绝不污染计数。"""
    rows = [{"id": "ok1", "script": "predict", "trigger": "timer",
             "created_at": 100.0, "finished_at": 200.0, "status": "done",
             "exit_code": 0, "error": None}]
    from web.services.source_meta import recent_job_stats
    stats = recent_job_stats(lambda limit=50: rows)
    assert stats["last_failure"] is None


def test_recent_job_stats_error_truncated_120_chars():
    """error 字段超 120 字时按 web 显示口径截断（避免 UI 撑爆）。"""
    long_err = "x" * 200
    rows = [{"id": "f", "script": "predict", "trigger": "timer",
             "created_at": 0.0, "finished_at": 1.0, "status": "failed",
             "exit_code": 1, "error": long_err}]
    from web.services.source_meta import recent_job_stats
    stats = recent_job_stats(lambda limit=50: rows)
    assert len(stats["last_failure"]["error"]) == 120


# ── 端到端（需要 fastapi，本地缺失时跳过）───────────────────────────────

def test_sources_status_route_exposes_next_runs_and_last_failure():
    """端到端：sources_status 路由包含 next_runs + last_failure。

    注：本测试需要 fastapi / httpx；本地 fastapi 缺失时跳过。
    """
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        import pytest
        pytest.skip("fastapi not installed; run on Oracle only")
    sample_jobs_rows = [{"id": "fail1", "script": "predict", "trigger": "manual",
                         "created_at": 100.0, "finished_at": 200.0, "status": "failed",
                         "exit_code": 1, "error": "API key missing"}]
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = (
        "NEXT                        LEFT LAST                        PASSED UNIT                       ACTIVATES\n"
        "Tue 2026-10-13 01:30:00 UTC  14h Mon 2026-10-12 01:30:00 UTC 1m ago league-daily-predict.timer league-daily-predict.service\n"
        "1 timers listed.\n"
    )
    with patch("web.services.jobs.list_jobs", return_value=sample_jobs_rows), \
         patch("subprocess.run", return_value=fake_proc):
        # 在 router 加载前 reload web.auth + web.api 系列，确保 require_auth
        # 引用与 router 注册时捕获的一致（避免 test_m1_auth reload 后不一致）。
        import importlib
        for mod_name in ("web.config", "web.session_store", "web.auth",
                         "web.routers.sources", "web.api"):
            mod = importlib.import_module(mod_name)
            importlib.reload(mod)
        from web.api import create_app
        app = create_app()
        from web.routers import sources as sources_mod
        app.dependency_overrides[sources_mod.require_auth] = lambda: None
        client = TestClient(app)
        resp = client.get("/api/v1/sources/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jobs"]["recent"]["last_failure"]["id"] == "fail1"
    assert "league-daily-predict.timer" in body["jobs"]["next_runs"]
