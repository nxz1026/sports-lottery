"""R48-3: scripts/core/data/fetch.py::validate_api_keys 2 cases direct + D-85 lock.

validate_api_keys (L23-L33, 11 行) 预检 2 个 env var (FOOTBALL_DATA_API_KEY /
API_FOOTBALL_KEY) 是否已配置, 缺失则 logger.warning。R35-4 D-19 + R36-5/6/7
已锁 _validate_api_response / _retry_request / _update_rate_limit_from_response,
但 validate_api_keys 自身核心契约 (env var 读取 + 空白兜底 + warning) 0 直测。

D-85 锁定 2 契约:
- D-85-1: 主流程 happy - 2 env var 都设 -> {FOOTBALL_DATA_API_KEY: True,
  API_FOOTBALL_KEY: True}; 缺一个 -> {True, False}
- D-85-2: 空白字符串 ("  ") 视为未设 (strip 后空 -> False)

依赖 stub:
- os.environ (monkeypatch.setenv / delenv)

LOCK 范围: validate_api_keys 仅, 不锁 fmt_date (内嵌在 fetch_football_data
闭包内, 不是模块级 helper, 集成测覆盖)。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def fetch_mod(monkeypatch):
    """Spec-load scripts/core/data/fetch.py + 清 env var 防污染。"""
    # 清 2 个 API key env var (默认状态)
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    spec = importlib.util.spec_from_file_location(
        "fetch_validate_api_keys_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-85 二契约 ────────────────────────


def test_validate_api_keys_d85_1_env_var_state(monkeypatch, fetch_mod):
    """R48-3 D-85-1: 主流程 - env var 状态映射。

    2 env var 都设 -> {True, True}; 缺一个 -> {True, False}; 都缺 -> {False, False}。
    """
    # 都设
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fd-test")
    monkeypatch.setenv("API_FOOTBALL_KEY", "af-test")
    out = fetch_mod.validate_api_keys()
    assert out == {"FOOTBALL_DATA_API_KEY": True, "API_FOOTBALL_KEY": True}

    # 只设一个
    monkeypatch.delenv("API_FOOTBALL_KEY")
    out_partial = fetch_mod.validate_api_keys()
    assert out_partial == {"FOOTBALL_DATA_API_KEY": True, "API_FOOTBALL_KEY": False}

    # 都不设
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY")
    out_none = fetch_mod.validate_api_keys()
    assert out_none == {"FOOTBALL_DATA_API_KEY": False, "API_FOOTBALL_KEY": False}

    # 空字符串视为未设 (bool(''.strip()) = False)
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "")
    out_empty = fetch_mod.validate_api_keys()
    assert out_empty == {"FOOTBALL_DATA_API_KEY": False, "API_FOOTBALL_KEY": False}


def test_validate_api_keys_d85_2_whitespace_treated_as_unset(monkeypatch, fetch_mod):
    """R48-3 D-85-2: 空白字符串 ("  " / "\\t\\n") 视为未设 (strip 后空 -> False)。

    L29 `bool(v.strip())`: 纯空白 strip 后空串, bool("") = False。
    """
    # 纯空格
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "   ")
    monkeypatch.setenv("API_FOOTBALL_KEY", "  \t\n  ")
    out = fetch_mod.validate_api_keys()
    assert out == {"FOOTBALL_DATA_API_KEY": False, "API_FOOTBALL_KEY": False}

    # 头尾空白但中间有字符 (e.g. " fd-key ") -> strip 保留 "fd-key" -> True
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", " fd-key ")
    out_padded = fetch_mod.validate_api_keys()
    assert out_padded["FOOTBALL_DATA_API_KEY"] is True
    # API_FOOTBALL_KEY 仍是空白 -> False
    assert out_padded["API_FOOTBALL_KEY"] is False
