"""R41-3: web/services/store.py::covers_date 3 tests direct + D-40 lock.

判断比赛日是否被预测文件的 data_window (YYYYMMDD-YYYYMMDD) 覆盖
helper 0 direct tests; _today_has_data 内部消费 (R39-1 已锁 6 测),
但 store.covers_date helper 自身契约未锁。

helper 含两条契约 (D-40):
- 窗口内 (含边界): win[0] <= day <= win[1] -> True
- 窗口外两侧 (边界外一天): win[0]-1day / win[1]+1day -> False
- 非法 data_window (None / 缺键 / 非 str / 长度非 8 / 非 ISO 日) -> False,
  不抛 ValueError (silent 兜底, 防止上游数据污染导致 500)

3 tests (MEDIUM):
- 窗口中段 -> True (核心命中)
- 边界外两侧 -> False (左右界各一)
- D-40 非法窗口全谱 (None / 缺键 / 非 str / 长度非 8 / 非法日) -> False
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_mod():
    """加载 web/services/store.py 为唯一 module name, 隔离 sys.modules 缓存。"""
    spec = importlib.util.spec_from_file_location(
        "store_covers_date_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-40 窗口边界 ────────────────────────


def test_covers_date_d40_inside_window_returns_true(store_mod):
    """R41-3 D-40: day 在窗口中段 -> True (核心命中)。"""
    data = {"data_window": "20261015-20261020"}
    assert store_mod.covers_date(data, date(2026, 10, 17)) is True


@pytest.mark.parametrize("off_window_day", [
    date(2026, 10, 14),  # win[0] - 1 day
    date(2026, 10, 21),  # win[1] + 1 day
])
def test_covers_date_d40_outside_window_returns_false(store_mod, off_window_day):
    """R41-3 D-40: day 在窗口外 1 天 (两侧) -> False (锁闭区间)。"""
    data = {"data_window": "20261015-20261020"}
    assert store_mod.covers_date(data, off_window_day) is False


# ──────────────────────── D-40 非法窗口兜底 ────────────────────────


@pytest.mark.parametrize("bad_data", [
    {},                              # 缺 data_window 键
    {"data_window": None},           # 显式 None
    {"data_window": "2026-10-15"},   # 非紧凑 (含分隔符 -, 但长度非 8)
    {"data_window": "2026101A-20261020"},  # 非法 hex 字符 (A)
    {"data_window": "20261301-20261020"},  # 非法 ISO 月 (13)
    {"data_window": 12345},          # 非 str 类型
])
def test_covers_date_d40_invalid_window_returns_false(store_mod, bad_data):
    """R41-3 D-40: 非法 data_window 全谱 -> False (silent 兜底, 不抛 ValueError)。

    锁定: 上游数据污染 (JSON 错) 不应让 helper 抛穿; _window_dates 内部
    try/except ValueError 已收口。
    """
    # 注意: parametrize 给的是字典, day 给一个普通日期; 无论如何不能抛穿
    assert store_mod.covers_date(bad_data, date(2026, 10, 17)) is False
