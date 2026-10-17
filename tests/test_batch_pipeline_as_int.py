"""R45-5: ai/batch_pipeline.py::_as_int 6 cases direct + D-66 lock.

_as_int (L106-L114, 9 行) 是 batch_pipeline 整数解析助手, 解决 LLM 把整数
回成字符串的常见问题。延续 R38-5 D-28 bool 陷阱风格, 显式排除 bool
(`isinstance(True, int) == True` 会让 bool 通过 int 分支, 但语义不对)。

D-66 锁定 6 契约:
- D-66-1: bool True/False -> None (排除 `isinstance(True, int) == True` 陷阱)
- D-66-2: 真 int (含负数 0/-3/大整数) -> 原值返回
- D-66-3: str '5' -> 5
- D-66-4: str '-3' -> -3 (负号 strip 处理)
- D-66-5: str '5.0' / 'abc' / '' / '  ' -> None (浮点字符串 + 空字符串 + 纯空白拒绝)
- D-66-6: None / float / list / dict -> None (类型兜底)

依赖: 无, 纯函数。
Stub: importlib.spec_from_file_location 直接加载 (避免 ai/__init__.py 链路)。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def bp_mod():
    """Spec-load ai/batch_pipeline.py (复用 R44-6 套路)。"""
    spec = importlib.util.spec_from_file_location(
        "batch_pipeline_as_int_test",
        REPO_ROOT / "ai" / "batch_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-66 六契约 ────────────────────────


def test_as_int_d66_1_bool_returns_none(bp_mod):
    """R45-5 D-66-1: bool True/False -> None (D-28 风格 bool 陷阱延续)。

    `isinstance(True, int) == True` 会让 bool 错误地通过 int 分支被接受。
    _as_int L108-L109 显式 `isinstance(value, bool)` 先排除。
    """
    assert bp_mod._as_int(True) is None
    assert bp_mod._as_int(False) is None


def test_as_int_d66_2_real_int_returns_value(bp_mod):
    """R45-5 D-66-2: 真 int (含负数/0/大整数) -> 原值返回。

    L110-L111 isinstance(value, int) 走直返路径 (bool 已先排除)。
    """
    assert bp_mod._as_int(0) == 0
    assert bp_mod._as_int(5) == 5
    assert bp_mod._as_int(-3) == -3
    assert bp_mod._as_int(999999) == 999999


def test_as_int_d66_3_string_integer_returns_int(bp_mod):
    """R45-5 D-66-3: str '5' -> 5 (LLM 常见返回: 数字回成字符串)。

    L112-L113 `value.strip().lstrip('-').isdigit()` 通过后 int(value.strip())。
    """
    assert bp_mod._as_int("5") == 5
    assert bp_mod._as_int("100") == 100


def test_as_int_d66_4_negative_string_returns_negative_int(bp_mod):
    """R45-5 D-66-4: str '-3' -> -3 (负数 strip 处理)。

    `value.strip().lstrip('-').isdigit()` -> '-3'.strip()='-3', lstrip('-')='3', isdigit=True。
    int(value.strip())=int('-3')=-3。
    """
    assert bp_mod._as_int("-3") == -3
    assert bp_mod._as_int("-100") == -100


def test_as_int_d66_5_invalid_string_returns_none(bp_mod):
    """R45-5 D-66-5: 浮点字符串 / 字母 / 空串 / 纯空白 -> None。

    '5.0' -> '5.0'.lstrip('-').isdigit()=False (点不在 digit 中)。
    'abc' / '' / '  ' -> 同样非 digit。
    """
    assert bp_mod._as_int("5.0") is None  # 浮点字符串拒绝
    assert bp_mod._as_int("abc") is None  # 字母
    assert bp_mod._as_int("") is None  # 空串
    assert bp_mod._as_int("   ") is None  # 纯空白
    assert bp_mod._as_int("5abc") is None  # 混合


def test_as_int_d66_6_unsupported_types_return_none(bp_mod):
    """R45-5 D-66-6: None / float / list / dict -> None (类型兜底)。

    bool/int/str 之外的所有类型走 L114 `return None`。
    注意 float 必须拒绝 (不接受 '5.0' 等同于 5)。
    """
    assert bp_mod._as_int(None) is None
    assert bp_mod._as_int(5.0) is None  # float 拒绝
    assert bp_mod._as_int(5.5) is None
    assert bp_mod._as_int([5]) is None
    assert bp_mod._as_int({"id": 5}) is None
    assert bp_mod._as_int((5,)) is None  # tuple 拒绝