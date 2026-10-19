"""R42-6: web/services/store.py bjt_today + _window_dates 2 tests + D-49 lock.

bjt_today 仅 test_jobs_today_has_data.py / test_jobs_lazy_auto_trigger.py
间接调用, 自身契约未锁。_window_dates 被 covers_date (D-40) 内联调用, helper
自身直接契约未锁 (R41-3 测试 covers_date 间接覆盖 _window_dates 一部分)。

helper 含两条契约 (D-49):
- bjt_today(): 返 datetime.now(BJT).date(); 纯截 today (BJT-aware)
- _window_dates(data): 解析 data['data_window'] 紧凑 'YYYYMMDD-YYYYMMDD';
  4 类无效输入 (非 str / 缺 - / 长度非 8 / ValueError) -> None (silent fallback)

2 tests (EASY):
- D-49 bjt_today 返固定 date (monkeypatch datetime.now)
- D-49 _window_dates happy path '20261015-20261020' -> 解析为 date tuple
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_mod():
    """加载 web/services/store.py 为唯一 module name (与 R41-3 同)。"""
    spec = importlib.util.spec_from_file_location(
        "store_window_dates_test",
        REPO_ROOT / "web" / "services" / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-49 二契约 ────────────────────────


def test_bjt_today_d49_returns_current_bjt_date(store_mod, monkeypatch):
    """R42-6 D-49: bjt_today 返固定日期 (mock store_mod.datetime.now 控制 wall-clock)。

    锁定 BJT 时区使用: BJT = UTC+8, e.g., UTC 16:00 = 次日 BJT 00:00。
    store.py 用 `from datetime import datetime` 模块本地绑定, 必须 patch
    store_mod.datetime 而非 datetime 顶层包。
    """
    sentinel = date(2026, 10, 15)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(store_mod, "datetime", _FrozenDatetime)

    out = store_mod.bjt_today()
    assert out == sentinel


def test_window_dates_d49_parses_compact_data_window(store_mod):
    """R42-6 D-49: _window_dates('20261015-20261020') 解析为 (date(2026,10,15), date(2026,10,20))。

    锁定 happy path 解析; 其他分支被 R41-3 covers_date 覆盖。
    """
    data = {"data_window": "20261015-20261020"}
    out = store_mod._window_dates(data)
    assert out == (date(2026, 10, 15), date(2026, 10, 20))
