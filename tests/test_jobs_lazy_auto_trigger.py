"""R38-6: web/routers/jobs.py::_lazy_auto_trigger 7 tests direct + D-30 marker monotonicity.

同日去重自动触发 0 direct tests; HTTP /jobs/auto/refresh endpoint 内调用,
endpoint-level 仅测试 wrapper, helper 本身未锁。

helper 内含一条关键契约:
- D-30 marker 单调性: 今日已触发必返 `already_today`; day field 一旦撞上当日,
  后续触发全部 short-circuit (永不再次触发预测脚本)。

7 tests:
- D-30 today marker already -> already_today + trigger_predict 不被调用
- stale marker (1990 day) -> 不 short-circuit + 走 trigger + 覆盖 marker
- no marker -> 走 trigger + 写入 today marker
- corrupt marker (invalid JSON) -> 视为空 -> 走 trigger
- AUTO_REFRESH_DAILY=False -> disabled (marker 缺席也不触发)
- quota_exhausted reason -> 返 quota_exhausted + 不写 marker
- happy path success -> 写入 marker + job id 透传
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web import config
from web.services import jobs as jobs_mod
from web.services import store
from web.routers.jobs import _lazy_auto_trigger


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 web.config.DATA_DIR 重定向到 tmp，隔离真实 web/.data 目录。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def enable_auto_refresh(monkeypatch):
    """默认开启 AUTO_REFRESH_DAILY。"""
    monkeypatch.setattr(config, "AUTO_REFRESH_DAILY", True)


def _write_marker(directory: Path, day: str, job_id: str = "j_marker") -> Path:
    marker = directory / "auto_refresh_last.json"
    marker.write_text(
        json.dumps({"day": day, "job": job_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    return marker


def _patch_trigger(monkeypatch, job_id="j_new", reason="ok"):
    """Mock jobs.trigger_predict 返 fake (job, reason); submit_job/mark_failed 静默。"""
    fake_job = {"id": job_id}
    calls = {"n": 0}

    def fake_trigger_predict(args, trigger="manual"):
        calls["n"] += 1
        # 校验调用参数：必须含 --all（避免默认只跑 epl 的隐性 bug 复发）
        assert "--all" in args, f"trigger_predict args 缺 --all: {args}"
        assert trigger == "auto", f"trigger 应为 auto，实测 {trigger}"
        return fake_job, reason

    monkeypatch.setattr(jobs_mod, "trigger_predict", fake_trigger_predict)
    monkeypatch.setattr(jobs_mod, "submit_job", lambda jid: None)
    monkeypatch.setattr(jobs_mod, "mark_failed", lambda jid, err: None)
    return calls


# ──────────────────────── D-30 marker monotonicity ────────────────────────


def test_lazy_auto_trigger_d30_marker_already_today(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6 D-30: marker day == today -> already_today，trigger_predict 不被调用。

    一旦今日 marker 写入，永不再次触发。
    """
    today = store.bjt_today().isoformat()
    _write_marker(data_dir, day=today, job_id="j_today")

    calls = _patch_trigger(monkeypatch, reason="ok")

    result = _lazy_auto_trigger()

    assert result == {"triggered": False, "reason": "already_today"}
    assert calls["n"] == 0  # 关键：trigger_predict 0 次调用 (单调性锁)


# ──────────────────────── proceeds branches ────────────────────────


def test_lazy_auto_trigger_stale_marker_proceeds(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6: marker day=1990 (遥远过去) -> 不 short-circuit，走 trigger + 覆盖 marker。"""
    _write_marker(data_dir, day="1990-01-01", job_id="j_stale")

    calls = _patch_trigger(monkeypatch, job_id="j_new", reason="ok")

    result = _lazy_auto_trigger()

    today = store.bjt_today().isoformat()
    assert calls["n"] == 1
    assert result == {"triggered": True, "job": "j_new"}
    # marker 已被覆盖为 today + 新 job_id
    new_marker = json.loads(
        (data_dir / "auto_refresh_last.json").read_text(encoding="utf-8"))
    assert new_marker["day"] == today
    assert new_marker["job"] == "j_new"


def test_lazy_auto_trigger_no_marker_proceeds(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6: 无 marker 文件 -> 走 trigger，写入 today marker。"""
    calls = _patch_trigger(monkeypatch, job_id="j_first", reason="ok")

    # 确认 marker 确实不存在
    assert not (data_dir / "auto_refresh_last.json").exists()

    result = _lazy_auto_trigger()

    today = store.bjt_today().isoformat()
    assert calls["n"] == 1
    assert result == {"triggered": True, "job": "j_first"}
    new_marker = json.loads(
        (data_dir / "auto_refresh_last.json").read_text(encoding="utf-8"))
    assert new_marker["day"] == today
    assert new_marker["job"] == "j_first"


def test_lazy_auto_trigger_corrupt_marker_treated_as_empty(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6: marker JSON 解析失败 (ValueError) -> 视为 {} -> 走 trigger。

    `try: payload = json.loads(...)` 异常 -> payload = {}, 等价于无 marker。
    """
    marker = data_dir / "auto_refresh_last.json"
    marker.write_text("not valid json {{{ broken", encoding="utf-8")

    calls = _patch_trigger(monkeypatch, job_id="j_recover", reason="ok")

    result = _lazy_auto_trigger()

    assert calls["n"] == 1
    assert result == {"triggered": True, "job": "j_recover"}


# ──────────────────────── disabled / exhausted branches ────────────────────────


def test_lazy_auto_trigger_disabled_by_config(
    data_dir, monkeypatch,
):
    """R38-6: AUTO_REFRESH_DAILY=False -> disabled (即便 marker 缺席也不触发)。"""
    monkeypatch.setattr(config, "AUTO_REFRESH_DAILY", False)

    calls = _patch_trigger(monkeypatch, reason="ok")

    result = _lazy_auto_trigger()

    assert result == {"triggered": False, "reason": "disabled"}
    assert calls["n"] == 0
    # marker 不应被写入
    assert not (data_dir / "auto_refresh_last.json").exists()


def test_lazy_auto_trigger_quota_exhausted_no_marker_written(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6: trigger 返 quota_exhausted -> 透传 + 不写 marker (否则污染 D-30 单调性)。"""
    calls = _patch_trigger(monkeypatch, reason="quota_exhausted")

    result = _lazy_auto_trigger()

    assert calls["n"] == 1
    assert result == {"triggered": False, "reason": "quota_exhausted"}
    # marker 不应被写入 (quota 明天恢复后仍可触发)
    assert not (data_dir / "auto_refresh_last.json").exists()


# ──────────────────────── happy path ────────────────────────


def test_lazy_auto_trigger_happy_path_writes_marker(
    data_dir, enable_auto_refresh, monkeypatch,
):
    """R38-6: trigger success -> 写入 marker (day=today + job=id) + 返 {triggered,job}。"""
    calls = _patch_trigger(monkeypatch, job_id="j_happy", reason="ok")

    result = _lazy_auto_trigger()

    today = store.bjt_today().isoformat()
    assert calls["n"] == 1
    assert result == {"triggered": True, "job": "j_happy"}
    # marker 含 day=today + job=j_happy
    new_marker = json.loads(
        (data_dir / "auto_refresh_last.json").read_text(encoding="utf-8"))
    assert new_marker["day"] == today
    assert new_marker["job"] == "j_happy"
