"""R34-4: web/services/jobs.py::_atexit_shutdown_executor 4 测直测 + D-8 锁定。

现状：进程退出 drain 钩子 0 直测；模块顶层 atexit.register 一次 + 顶层
`if not _atexit_registered` 守卫重复 import 不重复注册（D-8 契约）。

D-8 bug-shaped 契约（jobs.py:499-501）：
    if not _atexit_registered:
        atexit.register(_atexit_shutdown_executor)
        _atexit_registered = True

测试方向（D-8 锁定）：
- 模块导入后 `_atexit_registered == True`（顶层守卫已生效）
- `_atexit_shutdown_executor` 用 `executor.shutdown(wait=False, cancel_futures=True)`
- 二次 shutdown 不抛（Python 3.9+ ThreadPoolExecutor shutdown 自身）
- executor.shutdown 抛异常 → 不冒泡（D 类全 catch）
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def jobs_mod():
    """加载 jobs.py 为唯一 module name（web/services/jobs.py 顶层有 atexit.register）。"""
    spec = importlib.util.spec_from_file_location(
        "jobs_atexit_test", REPO_ROOT / "web" / "services" / "jobs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _atexit_shutdown_executor + D-8 ────────────────────────


def test_atexit_registered_flag_true_after_module_import_d8(jobs_mod):
    """R34-4 D-8 锁定：模块导入后 `_atexit_registered == True`（顶层守卫已执行）。"""
    # jobs.py 顶层 if not _atexit_registered: ... _atexit_registered = True
    assert jobs_mod._atexit_registered is True


def test_atexit_shutdown_executor_uses_wait_false_cancel_futures_true(jobs_mod, monkeypatch):
    """R34-4：shutdown 锁定参数 `wait=False, cancel_futures=True`（Python 3.9+ 标准收尾姿势）。

    monkeypatch executor.shutdown，调用 _atexit_shutdown_executor，捕获 kwargs。
    """
    calls = []

    def fake_shutdown(*, wait=True, cancel_futures=False):
        calls.append({"wait": wait, "cancel_futures": cancel_futures})
    monkeypatch.setattr(jobs_mod.executor, "shutdown", fake_shutdown)
    jobs_mod._atexit_shutdown_executor()
    assert len(calls) == 1
    assert calls[0] == {"wait": False, "cancel_futures": True}


def test_atexit_shutdown_executor_tolerates_already_shutdown_executor(jobs_mod):
    """R34-4：二次 shutdown 不抛异常（Python 3.9+ ThreadPoolExecutor 自身允许二次调用）。"""
    # 第一次 shutdown
    jobs_mod.executor.shutdown(wait=False, cancel_futures=True)
    # 第二次 shutdown（_atexit_shutdown_executor 内部调）—— 不抛
    jobs_mod._atexit_shutdown_executor()


def test_atexit_shutdown_executor_swallows_shutdown_exception(jobs_mod, monkeypatch):
    """R34-4：executor.shutdown 抛异常 → 不冒泡（`except Exception: pass` 兜底）。

    D 类契约钉死：进程退出钩子永远不抛；让 Python atexit 链路正常结束。
    """
    def boom_shutdown(*, wait=True, cancel_futures=False):
        raise RuntimeError("executor already finalized")
    monkeypatch.setattr(jobs_mod.executor, "shutdown", boom_shutdown)
    # 不抛异常（D-8 兜底）
    jobs_mod._atexit_shutdown_executor()