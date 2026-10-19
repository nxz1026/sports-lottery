"""R38-5: web/routers/jobs.py::_validate_bball_args 5 tests direct + D-28 bool trap.

篮球参数白名单校验 0 direct tests; HTTP 层只测试 happy path。helper 内
含两条隐性契约:

- D-28 bool 陷阱: `isinstance(True, int) == True`, 因此 `ahead_days=True` 会
  通过 isinstance(int) 但被显式 isinstance(bool) 拦截 -> ApiError。
  若有人删去 isinstance(bool) 检查 -> bool 静默通过, 然后 `1 <= 1 <= 180`
  通过 range 检查, str(True) = "True" 被拼成 argv, 子进程收垃圾参数。
- 范围守护: ahead_days ∈ [1, 180], backtest ∈ [1, 60]。
- 参数白名单: 仅 ahead_days/backtest 可接受, 其他键 -> ApiError。

5 tests:
- D-28 bool trap: ahead_days=True / backtest=True 均抛 ApiError
- valid ahead_days in range -> argv 含 --ahead-days
- valid backtest in range -> argv 含 --backtest
- 未知参数 (e.g. foo=1) -> ApiError 抛出
- 越界值: ahead_days=0/200, backtest=100 均抛 ApiError
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.errors import ApiError
from web.routers.jobs import _validate_bball_args

# ──────────────────────── D-28 bool trap ────────────────────────


@pytest.mark.parametrize("key", ["ahead_days", "backtest"])
def test_validate_bball_args_d28_bool_trap(key):
    """R38-5 D-28: bool 静默陷阱锁定.

    Python `isinstance(True, int) == True`, 不显式 isinstance(bool) 拒绝
    则 bool 静默通过 range 检查后被 `str(True)="True"` 拼成 argv。
    若有人删去 isinstance(bool) 检查, 此测直接 fail。
    """
    with pytest.raises(ApiError) as exc_info:
        _validate_bball_args({key: True})
    assert exc_info.value.code == "invalid_params"


# ──────────────────────── valid branches ────────────────────────


def test_validate_bball_args_valid_ahead_days():
    """R38-5: ahead_days=7 -> argv = ["--ahead-days", "7"]."""
    argv = _validate_bball_args({"ahead_days": 7})
    assert argv == ["--ahead-days", "7"]


def test_validate_bball_args_valid_backtest():
    """R38-5: backtest=14 -> argv = ["--backtest", "14"]."""
    argv = _validate_bball_args({"backtest": 14})
    assert argv == ["--backtest", "14"]


# ──────────────────────── error branches ────────────────────────


def test_validate_bball_args_unknown_param_rejected():
    """R38-5: 未知参数 (e.g. foo=1) -> ApiError 抛出, 不进 argv."""
    with pytest.raises(ApiError) as exc_info:
        _validate_bball_args({"foo": 1})
    assert exc_info.value.code == "invalid_params"
    assert "foo" in str(exc_info.value)


@pytest.mark.parametrize("bad_value,key", [
    (0, "ahead_days"),    # below min
    (200, "ahead_days"),  # above max (180)
    (100, "backtest"),    # above max (60)
])
def test_validate_bball_args_out_of_range(bad_value, key):
    """R38-5: 越界值 -> ApiError. ahead_days ∈ [1, 180], backtest ∈ [1, 60]."""
    with pytest.raises(ApiError) as exc_info:
        _validate_bball_args({key: bad_value})
    assert exc_info.value.code == "invalid_params"
