"""R44-3: scripts/core/data/convert.py::_map_api_football_status 4 cases + D-58.

API-Football status short -> (ESPN status, completed) 映射 helper 0 直测;
convert_api_football_to_espn_format 走 cover, 但 helper 自身契约未锁。

helper 含四条契约 (D-58):
- 默认 (无 fixture.status.short) -> (STATUS_SCHEDULED, False)
- 未比赛态 (NS / SUSP / INT / PST / CANC / ABD) -> (STATUS_SCHEDULED, False)
- 进行中 (1H / 2H / HT / ET / P) -> (STATUS_IN_PROGRESS, False)
- 完成态 (FT / AET / PEN / AWD / WO) -> (STATUS_FULL_TIME/_ET/_PEN, True)
  (与 R38 D-27 状态枚举对齐)

4 tests (MEDIUM): parametrize 覆盖 4 类 + 完成态多分支
- NS -> SCHEDULED, False
- 进行中 (parametrize over 1H / 2H / HT) -> IN_PROGRESS, False
- 完成 (parametrize over FT / AET / PEN) -> FULL_TIME/_ET/_PEN, True
- 未知 status short -> fallback (SCHEDULED, False) [default 行为锁]
- bonus: 缺 fixture.status dict -> fallback (default 缩到 'NS', 走 default)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def convert_mod():
    """加载 scripts/core/data/convert.py 为唯一 module name (复用 R37-5 模式)。"""
    spec = importlib.util.spec_from_file_location(
        "convert_map_api_football_status_test",
        REPO_ROOT / "scripts" / "core" / "data" / "convert.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fix(status_short: str | None) -> dict:
    """构造 API-Football fixture dict (给定 status.short)。"""
    if status_short is None:
        return {"fixture": {}}  # 缺 status
    return {"fixture": {"status": {"short": status_short}}}


# ──────────────────────── D-58 四契约 ────────────────────────


def test_map_api_football_status_d58_ns_default_scheduled(convert_mod):
    """R44-3 D-58: NS (Not Started) -> STATUS_SCHEDULED, completed=False。

    锁定 default 未比赛态映射; SFU 已知列表 (NS/SUSP/INT/PST/CANC/ABD) 同
    落 SCHEDULED。
    """
    out = convert_mod._map_api_football_status(_fix("NS"))
    assert out == ("STATUS_SCHEDULED", False)


@pytest.mark.parametrize("status_short", ["1H", "2H", "HT", "ET", "P"])
def test_map_api_football_status_d58_in_progress_returns_false_completed(convert_mod, status_short):
    """R44-3 D-58: 进行中状态 (1H/2H/HT/ET/P) -> IN_PROGRESS, False (未完成)。

    锁 D-58: 进行中字段都完成=False; 完成判定只允许 FT / AET / PEN / AWD / WO。
    """
    out = convert_mod._map_api_football_status(_fix(status_short))
    assert out == ("STATUS_IN_PROGRESS", False)


@pytest.mark.parametrize("status_short,expected_espn", [
    ("FT", "STATUS_FULL_TIME"),
    ("AET", "STATUS_FINAL_ET"),
    ("PEN", "STATUS_FINAL_PEN"),
    ("AWD", "STATUS_FULL_TIME"),
    ("WO", "STATUS_FULL_TIME"),
])
def test_map_api_football_status_d58_completed_status_set(convert_mod, status_short, expected_espn):
    """R44-3 D-58: 完成态 (FT/AET/PEN/AWD/WO) -> FULL_TIME/_ET/_PEN, completed=True。

    锁定 completed = status ∈ {STATUS_FULL_TIME, STATUS_FINAL_ET, STATUS_FINAL_PEN}
    集合 (与 R38 D-27 past status enum lock 对齐)。
    """
    out = convert_mod._map_api_football_status(_fix(status_short))
    assert out == (expected_espn, True)


def test_map_api_football_status_d58_unknown_short_falls_back_scheduled(convert_mod):
    """R44-3 D-58: 未知 status short (e.g., "FOO_BAR") -> SCHEDULED, False (silent fallback)。

    锁定 fallback: status_map miss -> default ('STATUS_SCHEDULED'), 不抛穿。
    """
    out = convert_mod._map_api_football_status(_fix("UNKNOWN"))
    assert out == ("STATUS_SCHEDULED", False)


def test_map_api_football_status_d58_missing_status_dict_defaults_to_ns(convert_mod):
    """R44-3 D-58: 缺 fixture.status dict -> default 'NS' -> SCHEDULED, False。

    锁 fixture 完全缺 (或 fixture 不存在) -> 默认 NS 路径, 不抛穿。
    """
    out = convert_mod._map_api_football_status({})
    assert out == ("STATUS_SCHEDULED", False)
    # 缺 fixture 顶层 dict 也走 default
    out2 = convert_mod._map_api_football_status({"fixture": {}})
    assert out2 == ("STATUS_SCHEDULED", False)
