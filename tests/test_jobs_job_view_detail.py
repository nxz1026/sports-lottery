"""R39-5: web/routers/jobs.py::_job_view_detail 4 tests direct + D-31 11-keys lock.

detail 视图 helper 0 direct tests; HTTP GET /jobs/{id} 通过 detail 端点调用,
endpoint-level 测覆盖 happy path 但 helper 自身缺直测。helper 内含一条
隐契约: log_tail 必须总是 str, 即便 read_job_log 因文件损坏 / 缺文件返 ""。

D-31 (11-keys contract lock):
- detail 视图必须严格 11 个 key (10 base from _job_view + log_tail):
  id, status, trigger, script, args, created_at, started_at,
  finished_at, exit_code, error, log_tail
- 任一字段省略都会让前端竞态 (前端先收不全的 dict 再去轮询, 重渲染时机错乱)
- log_tail 类型: 总是 str (即便 read_job_log 失败也返 "")
- _job_view 默认值 (script="predict", args=[]) 在 detail 视图也应保留

4 tests:
- happy path: 全部 base + log_tail populated (read_job_log 返真实内容)
- 缺 log 文件: read_job_log 返 "" -> log_tail = "" (类型仍 str, 不爆 KeyError)
- 缺 script 字段: _job_view 默认 "predict" 透传到 detail
- args 保留 (空 / 非空两种); detail 视图不剥 args
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.services import jobs as jobs_mod
from web.routers.jobs import _job_view_detail


# D-31 11-keys 契约: detail 视图必须严格含这 11 个 key
EXPECTED_KEYS = {
    "id", "status", "trigger", "script", "args",
    "created_at", "started_at", "finished_at",
    "exit_code", "error", "log_tail",
}


def _patch_read_job_log(monkeypatch, content: str):
    """Stub jobs.read_job_log 让 detail 视图拿到受控 log_tail。"""
    monkeypatch.setattr(jobs_mod, "read_job_log",
                        lambda jid, tail=200: content)


# ──────────────────────── D-31 detail view contract ────────────────────────


def test_job_view_detail_happy_path_populates_log_tail(monkeypatch):
    """R39-5: log file 存在 + job 完整 -> 11 keys 全在, log_tail = read 内容。"""
    _patch_read_job_log(monkeypatch, "started\nrunning\nfinished")
    job = {
        "id": "j1", "status": "finished", "trigger": "manual",
        "script": "predict", "args": ["--all"],
        "created_at": 1000.0, "started_at": 1001.0, "finished_at": 1010.0,
        "exit_code": 0, "error": None,
    }
    view = _job_view_detail(job, "j1")
    assert set(view.keys()) == EXPECTED_KEYS
    assert view["log_tail"] == "started\nrunning\nfinished"
    # base fields 透传
    assert view["id"] == "j1"
    assert view["script"] == "predict"
    assert view["args"] == ["--all"]
    assert view["exit_code"] == 0


def test_job_view_detail_d31_missing_log_file_yields_empty_string(monkeypatch):
    """R39-5 D-31: 缺 log 文件 -> read_job_log 返 "" -> log_tail = "" (类型仍 str)。

    实测 `if not path.is_file(): return ""` 路径, _job_view_detail 直接
    用返回值, 不抛错。
    """
    _patch_read_job_log(monkeypatch, "")  # 模拟缺文件
    job = {"id": "j2", "status": "queued"}
    view = _job_view_detail(job, "j2")
    # 11 keys 仍在; log_tail 空字符串而非 None / KeyError
    assert set(view.keys()) == EXPECTED_KEYS
    assert view["log_tail"] == ""
    assert isinstance(view["log_tail"], str)


# ──────────────────────── defaults propagation ────────────────────────


def test_job_view_detail_missing_script_defaults_to_predict(monkeypatch):
    """R39-5: job 缺 script 字段 -> _job_view 默认 'predict' 透传到 detail。

    detail 视图必须展示同一默认值, 否则 detail 端点不一致 (list 端点看 predict,
    detail 端点看 None / 缺失)。
    """
    _patch_read_job_log(monkeypatch, "log")
    job = {"id": "j3", "status": "finished"}
    # 故意不提供 script, args
    view = _job_view_detail(job, "j3")
    assert view["script"] == "predict"
    assert view["args"] == []  # 默认空 list


def test_job_view_detail_args_preserved_as_passed(monkeypatch):
    """R39-5: 自定义 args 数组原样保留, detail 视图不剥不重组。"""
    _patch_read_job_log(monkeypatch, "log")
    job = {
        "id": "j4", "status": "finished",
        "script": "predict_bball", "args": ["--ahead-days", "7", "--all"],
    }
    view = _job_view_detail(job, "j4")
    assert view["args"] == ["--ahead-days", "7", "--all"]
    # script 透传 (非默认)
    assert view["script"] == "predict_bball"
