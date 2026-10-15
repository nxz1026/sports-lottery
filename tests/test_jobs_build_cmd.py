"""R27 #3: web/services/jobs.py::_build_cmd 4 分支测。

explore 实测：test_m3_jobs.py 仅 1 测覆盖 predict_bball 分支；predict /
ai_analyze / 默认 web.enrich 3 分支 0 直测。_build_cmd 是 GHA 后台任务调度
dispatcher，写错路径/参数位置会导致子进程静默失控。

本轮补 4 测 + 2 边界：4 分支全覆盖 + 空 args / 任意 args 默认 web.enrich 分支。
"""
from __future__ import annotations

import sys
import sys as _sys

import pytest

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in _sys.path:
        _sys.path.insert(0, p)

from web.services import jobs as jobs_mod
from web import config as web_config


def test_build_cmd_predict_uses_predict_py_entry():
    """R27 #3：script="predict" → [sys.executable, BASE_DIR/scripts/predict.py, *args]。"""
    args = ["--league", "epl", "--date", "2026-10-15"]
    out = jobs_mod._build_cmd(args, script="predict")
    assert out[0] == sys.executable
    assert out[1] == str(web_config.BASE_DIR / "scripts" / "predict.py")
    assert out[2:] == args


def test_build_cmd_predict_bball_uses_bball_run_entry():
    """R27 #3：script="predict_bball" → predict 已 R12 测过；这里精确断言 bball 路径。"""
    args = ["--league", "nba"]
    out = jobs_mod._build_cmd(args, script="predict_bball")
    assert out[0] == sys.executable
    assert out[1] == str(web_config.BASE_DIR / "scripts" / "bball" / "run.py")
    assert out[2:] == ["--league", "nba"]


def test_build_cmd_ai_analyze_uses_dash_m_module_form():
    """R27 #3：script="ai_analyze" → "-m" + "web.services.ai_analyze" + args。"""
    args = ["2026-10-15"]
    out = jobs_mod._build_cmd(args, script="ai_analyze")
    assert out == [sys.executable, "-m", "web.services.ai_analyze", "2026-10-15"]


def test_build_cmd_unknown_script_falls_back_to_enrich():
    """R27 #3：未知 script → 走默认分支，返 [sys.executable, "-m", "web.enrich"] 无 args。"""
    out = jobs_mod._build_cmd(["--ignored", "--args"], script="unknown_script")
    assert out == [sys.executable, "-m", "web.enrich"]


def test_build_cmd_empty_args_predict():
    """R27 #3：空 args + script="predict" → 至少 2 项（executable + script path）。"""
    out = jobs_mod._build_cmd([], script="predict")
    assert len(out) == 2
    assert out[0] == sys.executable
    assert out[1].endswith("predict.py")


def test_build_cmd_default_script_is_predict():
    """R27 #3：_build_cmd 默认 script="predict" → 行为等于显式传 predict。"""
    out_default = jobs_mod._build_cmd(["--league", "epl"])
    out_explicit = jobs_mod._build_cmd(["--league", "epl"], script="predict")
    assert out_default == out_explicit
