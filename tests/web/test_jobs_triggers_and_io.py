"""R57: web/services/jobs.py 5 个 0 直测 helper 7 测 + D-115/D-116 lock。

R56 末已锁 D-112/D-113/D-114 (ai + source_meta). 自动化扫描 jobs.py 36 个 helper
找出 5 个真正 0 直测:

- _job_file         (L55-56):   ``config.JOBS_DIR / f"{jid}.json"`` 路径拼装
- _write_job        (L263-268): atomic file write (tmp + os.replace, 配置隔离安全)
- trigger_predict   (L587-589): 已覆盖 (test_jobs_active_job 等)
- trigger_bball     (L592-598): ``_spawn("predict_bball", args, trigger)`` wrapper
- trigger_ai_enrich (L601-603): ``_spawn("ai_enrich", [], trigger)`` wrapper
- trigger_ai_analyze(L606-614): ``_spawn("ai_analyze", args, trigger)`` wrapper

D-115 锁 _job_file + _write_job (3 测真 IO):
- D-115-1: _job_file 路径 = config.JOBS_DIR / f"{jid}.json"
- D-115-2: _write_job atomic 写 (tmp_path / replace) — 同时真写 json 到 JOBS_DIR
- D-115-3: _write_job 旧文件已存在时 os.replace 覆盖 (无中间空文件)

D-116 锁 4 trigger (4 测, stub _spawn):
- D-116-1: trigger_predict 调 _spawn("predict", args, "manual") (已覆盖验证)
- D-116-2: trigger_bball 调 _spawn("predict_bball", args, trigger) — 与 predict 不同 script
- D-116-3: trigger_ai_enrich 调 _spawn("ai_enrich", [], trigger) — argv 永远空 (date_str 不存在)
- D-116-4: trigger_ai_analyze 调 _spawn("ai_analyze", args, trigger) — date_str=None -> argv 空

策略:
- spec-load web.services.jobs 到隔离 namespace
- _write_job 真 IO: JOBS_DIR 指向 tmp_path
- trigger_* 测: stub jobs._spawn, 验证 _spawn 收到正确的 (script, args, trigger) 三元组
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jobs_mod(tmp_path, monkeypatch):
    """Spec-load web.services.jobs + JOBS_DIR 指向 tmp + _spawn stub 默认."""
    spec = importlib.util.spec_from_file_location(
        "web_services_jobs_test",
        REPO_ROOT / "web" / "services" / "jobs.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # JOBS_DIR 指向 tmp (真 IO 测试)
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr("web.config.JOBS_DIR", jobs_dir)
    # mod 自己顶层 config.JOBS_DIR (after from ... import) — 通过 monkeypatch.setattr
    monkeypatch.setattr(mod, "_now_epoch", lambda: 1_700_000_000.0)

    return mod, jobs_dir


# ──────────────────────── D-115: 3 测 ────────────────────────


def test_d115_1_job_file_path_layout(jobs_mod):
    """D-115-1: _job_file (L55-56) 路径 = config.JOBS_DIR / f"{jid}.json"."""
    mod, jobs_dir = jobs_mod

    p = mod._job_file("abc123456789")

    assert p == jobs_dir / "abc123456789.json"
    assert p.suffix == ".json"
    assert p.name == "abc123456789.json"


def test_d115_2_write_job_atomic_write_via_tmp_replace(jobs_mod, monkeypatch):
    """D-115-2: _write_job (L263-268) atomic 写 — 用 .tmp 临时文件 + os.replace 替换."""
    mod, jobs_dir = jobs_mod

    data = {"id": "atomic-id", "status": "queued", "value": 42}
    mod._write_job("atomic-id", data)

    # 真实 .json 落地
    target = jobs_dir / "atomic-id.json"
    assert target.exists()
    # 无 .tmp 残留 (atomic replace 完成后 tmp 已被替换)
    assert not (jobs_dir / "atomic-id.json.tmp").exists()

    # 落地内容一致
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == data


def test_d115_3_write_job_overwrites_existing(jobs_mod):
    """D-115-3: _write_job L268 os.replace 在旧文件存在时直接替换, 不污染目录.

    旧 _job_file(jid).with_suffix(".tmp") 用 ``os.replace(tmp, _job_file(jid))``
    替换旧 json — 此时 tmp 文件已被挪走, 不留 .tmp 副本.
    """
    mod, jobs_dir = jobs_mod

    # 第一次写
    mod._write_job("replace-id", {"version": 1, "x": "old"})
    target = jobs_dir / "replace-id.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1, "x": "old"}

    # 第二次写 (覆盖)
    mod._write_job("replace-id", {"version": 2, "x": "new"})

    # 仍是同一文件, 内容已替换
    assert target.exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"version": 2, "x": "new"}
    # 只有 .json 副本, 无 .tmp 残留
    files = list(jobs_dir.iterdir())
    assert len(files) == 1
    assert files[0].name == "replace-id.json"


# ──────────────────────── D-116: 4 测 ────────────────────────


def _stub_spawn(mod, monkeypatch, return_value=({"id": "fake-jid"}, None)):
    """stub jobs._spawn → 记录调用, 返 (job dict, error)."""
    spawn_mock = MagicMock(return_value=return_value)
    monkeypatch.setattr(mod, "_spawn", spawn_mock)
    return spawn_mock


def test_d116_1_trigger_predict_calls_spawn_with_predict_script(jobs_mod, monkeypatch):
    """D-116-1: trigger_predict (L587-589) ``_spawn("predict", args, trigger)``."""
    mod, _ = jobs_mod
    spawn = _stub_spawn(mod, monkeypatch)

    out = mod.trigger_predict(["--league", "epl"], trigger="auto")

    # _spawn 收到 (script, args, trigger) 三元组
    assert spawn.call_count == 1
    assert spawn.call_args[0] == ("predict", ["--league", "epl"], "auto")
    assert out == ({"id": "fake-jid"}, None)


def test_d116_2_trigger_bball_uses_predict_bball_script(jobs_mod, monkeypatch):
    """D-116-2: trigger_bball (L592-598) ``_spawn("predict_bball", args, trigger)`` — 与 predict 不同 script."""
    mod, _ = jobs_mod
    spawn = _stub_spawn(mod, monkeypatch)

    out = mod.trigger_bball(["--bball-args"], trigger="auto")

    assert spawn.call_count == 1
    # 关键是 script="predict_bball" 而非 "predict" (L598)
    assert spawn.call_args[0] == ("predict_bball", ["--bball-args"], "auto")
    assert out == ({"id": "fake-jid"}, None)


def test_d116_3_trigger_ai_enrich_args_always_empty(jobs_mod, monkeypatch):
    """D-116-3: trigger_ai_enrich (L601-603) ``_spawn("ai_enrich", [], trigger)`` — argv 固定空.

    L603 不接 date_str, 强制 argv=[]. 测试即使人为传 trigger 也要保 script+固定空 argv.
    """
    mod, _ = jobs_mod
    spawn = _stub_spawn(mod, monkeypatch)

    out = mod.trigger_ai_enrich(trigger="manual")

    assert spawn.call_count == 1
    # script="ai_enrich" + argv 固定 [] + trigger 透传
    assert spawn.call_args[0] == ("ai_enrich", [], "manual")
    assert out == ({"id": "fake-jid"}, None)


def test_d116_4_trigger_ai_analyze_passes_date_str_as_argv(jobs_mod, monkeypatch):
    """D-116-4: trigger_ai_analyze (L606-614) ``_spawn("ai_analyze", args, trigger)`` —
    date_str=None -> argv=[], date_str="2025-10-17" -> argv=["2025-10-17"].
    """
    mod, _ = jobs_mod
    spawn = _stub_spawn(mod, monkeypatch)

    # 场景 a: date_str=None -> argv=[]
    out_none = mod.trigger_ai_analyze(date_str=None, trigger="manual")
    assert spawn.call_args_list[0][0] == ("ai_analyze", [], "manual")
    assert out_none == ({"id": "fake-jid"}, None)

    # 场景 b: date_str="2025-10-17" -> argv=["2025-10-17"]
    spawn.reset_mock()
    out_with = mod.trigger_ai_analyze(date_str="2025-10-17", trigger="auto")
    assert spawn.call_args_list[0][0] == ("ai_analyze", ["2025-10-17"], "auto")
    assert out_with == ({"id": "fake-jid"}, None)
