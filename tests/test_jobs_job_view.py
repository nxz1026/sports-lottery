"""R40-3: web/routers/jobs.py::_job_view 4 tests direct + D-34 lock.

基础视图 helper 0 direct tests; jobs list 端点通过 _job_view(j) 调用,
端点 wrapper 覆盖 happy path 但 helper 本身未锁 (R39-5 _job_view_detail
间接覆盖 base 视图 4 测, 但 detail 额外加 log_tail 干扰 base 视图判定)。

helper 含一条契约 (D-34):
- 输出严格 10 个 key (id, status, trigger, script, args, created_at,
  started_at, finished_at, exit_code, error)
- 任一字段删除会让前端 list 渲染错位; log_tail 仅 detail 视图追加
- job.get(key) 默认值契约:
  * script 缺 -> "predict"
  * args 缺 -> []
  * 其他字段 -> None (.get 默认)
- None value 透传: dict 内 None 值不被 normalize 替换 (保留原始数据形)

4 tests (MEDIUM):
- happy path: 全字段就位 -> 10 keys 精确集合
- 缺 script: 默认 'predict' 透传
- 缺 args: 默认空 list 透传
- D-34 None value 不 normalize (job 内 None 值保留 None)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.routers.jobs import _job_view


# D-34 锁: detail 视图 11 keys, base 视图 10 keys
EXPECTED_BASE_KEYS = {
    "id", "status", "trigger", "script", "args",
    "created_at", "started_at", "finished_at",
    "exit_code", "error",
}


# ──────────────────────── happy path ────────────────────────


def test_job_view_happy_path_all_keys():
    """R40-3: job 全字段 -> base 视图 10 keys 精确集合, 值透传。"""
    job = {
        "id": "j1", "status": "finished", "trigger": "manual",
        "script": "predict_bball", "args": ["--all"],
        "created_at": 1000.0, "started_at": 1001.0, "finished_at": 1010.0,
        "exit_code": 0, "error": None,
    }
    view = _job_view(job)
    assert set(view.keys()) == EXPECTED_BASE_KEYS
    assert view["id"] == "j1"
    assert view["status"] == "finished"
    assert view["script"] == "predict_bball"
    assert view["args"] == ["--all"]


# ──────────────────────── defaults ────────────────────────


def test_job_view_missing_script_defaults_to_predict():
    """R40-3: job 缺 script -> 默认 'predict' 透传 (避免 list 渲染 'undefined')。"""
    job = {"id": "j2", "status": "queued", "args": []}
    view = _job_view(job)
    assert view["script"] == "predict"


def test_job_view_missing_args_defaults_to_empty_list():
    """R40-3: job 缺 args -> 默认空 list 透传 (前端读 args.length 不会 undefined)。"""
    job = {"id": "j3", "status": "queued", "script": "predict_bball"}
    view = _job_view(job)
    assert view["args"] == []
    assert isinstance(view["args"], list)


# ──────────────────────── D-34 None value passthrough ────────────────────────


def test_job_view_d34_none_values_passthrough_not_normalized():
    """R40-3 D-34: job 内 None value 不被 normalize 替换 (.get() 默认 None)。

    created_at/started_at/finished_at 等时序字段在跑之前的 queued 状态下
    应为 None, 不能被替换为 0.0 (会污染前端相对时间显示)。
    """
    job = {
        "id": "j4", "status": "queued", "trigger": "manual",
        "script": "predict", "args": [],
        "created_at": 1700000000.0,  # 唯一有值
        # started_at, finished_at, exit_code, error 故意缺 -> 应是 None
    }
    view = _job_view(job)
    # 有值的透传
    assert view["created_at"] == 1700000000.0
    # 缺值一律 None (不是 0.0 / "" / False)
    assert view["started_at"] is None
    assert view["finished_at"] is None
    assert view["exit_code"] is None
    assert view["error"] is None
    # 确保 key 总数还是 10 (没漏)
    assert set(view.keys()) == EXPECTED_BASE_KEYS
