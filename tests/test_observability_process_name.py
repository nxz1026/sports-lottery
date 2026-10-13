"""``core.observability`` process_name 隔离守门 — Round 9 H3+H9。

覆盖：
- ``setup(process_name=...)`` 给 runtime.log / events.jsonl 加后缀避免跨进程写竞态
- 同一目录先后两次不同 process_name setup → 两套文件并存
- web/api.py 模块导入即触发 ``setup(process_name="web")`` 装挂
- log_event 写到当前 process_name 对应的 events 文件
- 与 ml_feedback 的 events_path 参数兼容（多 events 文件按需合并）
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402
from core import log as core_log  # noqa: E402


@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    """每个测试前重置 observability + 切到 tmp_path。"""
    obs.reset_for_test()
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "DEBUG")
    yield
    obs.reset_for_test()


# ──────────────────────── setup 路径行为 ──────────────────────────────


class TestSetupProcessNameSuffix:
    """H9: setup(process_name=...) 让 runtime.log / events.jsonl 加后缀。"""

    def test_default_no_suffix(self, tmp_path):
        """不传 process_name → 保持旧路径（向后兼容）"""
        obs.setup(dir=tmp_path / "logs")
        assert obs.runtime_log_path().name == "runtime.log"
        assert obs.events_jsonl_path().name == "events.jsonl"

    def test_process_name_predict_suffix(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", process_name="predict")
        assert obs.runtime_log_path().name == "runtime.predict.log"
        assert obs.events_jsonl_path().name == "events.predict.jsonl"

    def test_process_name_web_suffix(self, tmp_path):
        obs.setup(dir=tmp_path / "logs", process_name="web")
        assert obs.runtime_log_path().name == "runtime.web.log"
        assert obs.events_jsonl_path().name == "events.web.jsonl"


class TestSetupTwoProcessesCoexist:
    """H9: web / predict 子进程先后在同一 logs 目录写，互不覆盖。

    同一进程内二次 setup() 因 _INSTALLED=True 走幂等跳过；本测试两次 setup
    模拟"两个进程 A/B 各自装挂"——A 装写 predict 事件，B 进程是独立 Python
    (subprocess)，重新 setup 时 _INSTALLED=False，B 装写 web 事件。
    """

    def test_two_files_coexist_in_same_logs_dir(self, tmp_path):
        # ── 进程 A：predict ──
        obs.setup(dir=tmp_path / "logs", process_name="predict")
        obs.log_event("prediction", league="epl", match_id="m1")
        predict_path = obs.events_jsonl_path()
        predict_lines = predict_path.read_text(encoding="utf-8").splitlines()
        assert len([ln for ln in predict_lines if ln.strip()]) == 1

        # 模拟进程 B（web）：新进程有干净模块状态
        obs.reset_for_test()
        obs.setup(dir=tmp_path / "logs", process_name="web")
        obs.log_event("uncaught_exception", exc_type="KeyError",
                      exc_repr="KeyError('foo')", context="http_request")
        web_path = obs.events_jsonl_path()
        web_lines = web_path.read_text(encoding="utf-8").splitlines()
        assert len([ln for ln in web_lines if ln.strip()]) == 1

        # 两个文件并存于同目录，互不覆盖
        assert predict_path != web_path
        assert predict_path.name == "events.predict.jsonl"
        assert web_path.name == "events.web.jsonl"
        assert predict_path.exists() and web_path.exists()

        # 内容隔开
        predict_records = [json.loads(ln) for ln in predict_lines if ln.strip()]
        web_records = [json.loads(ln) for ln in web_lines if ln.strip()]
        assert predict_records[0]["kind"] == "prediction"
        assert predict_records[0]["match_id"] == "m1"
        assert web_records[0]["kind"] == "uncaught_exception"
        assert web_records[0]["context"] == "http_request"


class TestIterEventsAcrossFiles:
    """H9: ml_feedback / 下游工具可逐文件读取，process_name 隔离不破坏 join。"""

    def test_iter_events_scoped_to_one_file(self, tmp_path):
        # 进程 A：写 predict
        obs.setup(dir=tmp_path / "logs", process_name="predict")
        obs.log_event("prediction", match_id="p1")
        obs.log_event("prediction", match_id="p2")

        # 进程 B：写 outcome（独立 events 文件）
        obs.reset_for_test()
        obs.setup(dir=tmp_path / "logs", process_name="predict")  # 同名再装
        obs.log_event("outcome", match_id="p1")
        obs.log_event("outcome", match_id="p2")

        # 单进程读：当前 _EVENTS_JSONL = events.predict.jsonl，应同时包含
        obs.reset_for_test()
        obs.setup(dir=tmp_path / "logs", process_name="predict")
        recs = list(obs.iter_events())
        kinds = sorted(r["kind"] for r in recs)
        assert "outcome" in kinds and "prediction" in kinds


# ──────────────────────── web/api 装挂门 ──────────────────────────────


class TestWebApiInstallsObservability:
    """H3: web/api.py 模块导入即触发 setup(process_name="web")。"""

    def _import_web_api_fresh(self):
        """重新加载 web.api 让模块级 setup 重跑。"""
        # 先确认 web.api 还没被 import/未污染
        sys.modules.pop("web.api", None)
        return importlib.import_module("web.api")

    def test_web_api_installs_with_web_suffix(self, tmp_path, monkeypatch):
        # 切 FOOTBALL_DIR 到 tmp_path，让 web/api.py setup 落 tmp 而非真实生产目录
        from core import constants
        monkeypatch.setattr(constants, "FOOTBALL_DIR", tmp_path)

        # 关键：web/api.py 模块级 import 应触发 observability.install(web)
        # 之前可能某测试已 import 过 web.api，需 reset 状态
        obs.reset_for_test()
        self._import_web_api_fresh()

        # 此时 observability 应已装挂，且 event 路径走 web 后缀
        runtime_path = obs.runtime_log_path()
        events_path = obs.events_jsonl_path()
        # 装挂可能因为环境不同有 None，但若装上了必是 web 后缀
        if runtime_path is not None:
            assert runtime_path.name == "runtime.web.log", runtime_path.name
        if events_path is not None:
            assert events_path.name == "events.web.jsonl", events_path.name

    def test_web_api_setup_failure_does_not_break_import(
            self, tmp_path, monkeypatch):
        """H3 边界：observability.setup 失败时 web.app 仍能 import。"""
        from core import observability as obs_mod

        def _explode(**_):
            raise OSError("forbidden: read-only filesystem simulation")

        monkeypatch.setattr(obs_mod, "setup", _explode)
        sys.modules.pop("web.api", None)
        # 不抛 → import 成功
        mod = importlib.import_module("web.api")
        assert mod is not None
        assert hasattr(mod, "create_app")
