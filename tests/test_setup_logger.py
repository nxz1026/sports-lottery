"""R30 #2: scripts/core/log.py::setup_logger 5 测直测。

现状：0 直测；test_observability.py 中 LEAGUE_PREDICT_LOG_LEVEL env 设置是给
core.observability.setup() 的，与本函数无关。setup_logger 的 env 解析、
handler 清理幂等、formatter 形态全部无覆盖。

本轮补 5 测，覆盖：
- 默认 INFO + handler 数=1
- env DEBUG/WARNING 透传（logger.level 与 handler.level 同步）
- env 非法值 "FOO" → INFO（getattr fallback）
- env 小写 "info" → uppercase 归一
- 同 name 连调两次 handler 不累积
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def log_mod(monkeypatch):
    """加载 log.py 为唯一 module name；清 env 防污染。"""
    monkeypatch.delenv("LEAGUE_PREDICT_LOG_LEVEL", raising=False)
    spec = importlib.util.spec_from_file_location(
        "log_setup_test", REPO_ROOT / "scripts" / "core" / "log.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_setup_logger_default_level_info_when_env_unset(log_mod):
    """R30 #2：env 未设 → 默认 INFO；handler 数=1，handler 是 StreamHandler。"""
    logger = log_mod.setup_logger("test_default")
    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


def test_setup_logger_env_debug_and_warning_propagated(log_mod, monkeypatch):
    """R30 #2：env DEBUG / WARNING → logger.level 与 handler.setLevel 同步。"""
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "DEBUG")
    logger = log_mod.setup_logger("test_debug")
    assert logger.level == logging.DEBUG
    assert logger.handlers[0].level == logging.DEBUG

    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "WARNING")
    logger2 = log_mod.setup_logger("test_warning")
    assert logger2.level == logging.WARNING
    assert logger2.handlers[0].level == logging.WARNING


def test_setup_logger_env_invalid_falls_back_to_info(log_mod, monkeypatch):
    """R30 #2：env 非法值 "FOO" → getattr fallback INFO，不抛错。"""
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "FOO")
    logger = log_mod.setup_logger("test_invalid_env")
    assert logger.level == logging.INFO


def test_setup_logger_lowercase_env_normalized(log_mod, monkeypatch):
    """R30 #2：env 小写 "info" → uppercase 归一 → INFO。"""
    monkeypatch.setenv("LEAGUE_PREDICT_LOG_LEVEL", "info")
    logger = log_mod.setup_logger("test_lowercase")
    assert logger.level == logging.INFO


def test_setup_logger_second_call_does_not_accumulate(log_mod):
    """R30 #2：同 name 连调两次 handler 不累积；不同 name 各 1 handler。

    注意：log_mod 装载时顶层 setup_logger("predict") 已建一个 logger，连调
    会 clear 重置。验证 setup 自身语义：连调两次后 handler 数仍为 1。
    """
    name = "test_idempotent"
    logger1 = log_mod.setup_logger(name)
    logger2 = log_mod.setup_logger(name)
    assert logger1 is logger2  # 同 name 返同对象
    assert len(logger2.handlers) == 1  # handler 数未被累积

    # 不同 name 各 1 handler
    a = log_mod.setup_logger("test_logger_a_unique")
    b = log_mod.setup_logger("test_logger_b_unique")
    assert len(a.handlers) == 1
    assert len(b.handlers) == 1
    assert a is not b
