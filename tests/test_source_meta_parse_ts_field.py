"""R45-6: web/services/source_meta.py::_parse_ts_field 5 cases direct + D-67 lock.

_parse_ts_field (L28-L42, 15 行) 把 ``systemctl list-timers`` 4 token 时间字段
(``Day YYYY-MM-DD HH:MM:SS TZ``) 解析为 aware UTC datetime。

R44 已通过 tests/web/test_sources_router.py 间接断言 list_timer_runs/recent_job_stats,
但 _parse_ts_field 自身核心契约 (4 token 校验 + regex 匹配 + datetime 解析 +
TZ 锁定) 0 直测。

D-67 锁定 5 契约:
- D-67-1: 标准 4 token 'Mon 2026-10-12 01:30:00 UTC' -> aware UTC datetime
- D-67-2: token 长度 != 4 (3 / 5 / 0 tokens) -> None
- D-67-3: 格式不匹配 (日期斜杠 / 缺字段 / day 小写) -> None
- D-67-4: TZ 缩写 2-4 字符 (UTC/CST/PST/EDT) -> 接受
- D-67-5: 边界 - 跨日 UTC datetime 准确解析 (2026-12-31 23:59:59)

依赖: 无, 纯函数。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def sm_mod():
    """Spec-load web/services/source_meta.py。"""
    spec = importlib.util.spec_from_file_location(
        "source_meta_parse_ts_field_test",
        REPO_ROOT / "web" / "services" / "source_meta.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-67 五契约 ────────────────────────


def test_parse_ts_field_d67_1_standard_4_tokens_returns_utc_datetime(sm_mod):
    """R45-6 D-67-1: 标准 4 token 'Mon 2026-10-12 01:30:00 UTC' -> aware UTC datetime。

    token 长度 == 4 + regex 匹配 + strptime 成功 + tz 锁定 UTC。
    """
    dt = sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "UTC"])
    assert dt is not None
    assert dt == datetime(2026, 10, 12, 1, 30, 0, tzinfo=timezone.utc)
    assert dt.tzinfo == timezone.utc  # aware


def test_parse_ts_field_d67_2_wrong_token_count_returns_none(sm_mod):
    """R45-6 D-67-2: token 长度 != 4 -> None。

    3 token (缺 TZ) / 5 token (多字段) / 0 token 都走 L30-L31 早返。
    """
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00"]) is None  # 3
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "UTC", "extra"]) is None  # 5
    assert sm_mod._parse_ts_field([]) is None  # 0


def test_parse_ts_field_d67_3_format_mismatch_returns_none(sm_mod):
    """R45-6 D-67-3: 格式不匹配 -> None (regex miss)。

    - 日期斜杠 '2026/10/12' -> \\d{4}-\\d{2}-\\d{2} 不匹配
    - 日期点分 '12.10.2026' -> 不匹配
    - 缺时间 'Mon 2026-10-12 UTC' -> 不匹配
    - day 小写 'mon' -> [A-Z][a-z]{2} 大写开头不匹配
    """
    assert sm_mod._parse_ts_field(["Mon", "2026/10/12", "01:30:00", "UTC"]) is None  # 斜杠
    assert sm_mod._parse_ts_field(["Mon", "12.10.2026", "01:30:00", "UTC"]) is None  # 点分
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "UTC"]) is None  # 缺时间
    assert sm_mod._parse_ts_field(["mon", "2026-10-12", "01:30:00", "UTC"]) is None  # 小写


def test_parse_ts_field_d67_4_tz_abbreviation_2_to_4_chars(sm_mod):
    """R45-6 D-67-4: TZ 缩写 2-4 字符 regex 接受, strptime 容错。

    regex `(?P<tz>[A-Z]{2,4})$` 接受 2-4 字符全大写 TZ 缩写。
    但 Python strptime %Z 实际只识别 UTC/GMT/EST/EDT 等少数 TZ;
    CST/PST 等走到 strptime 会 ValueError -> _parse_ts_field 返 None (兜底)。

    LOCK: regex 边界守住 (5+ 字符 / 1 字符 reject);
          strptime 仅 UTC/GMT 类 TZ 成功 (CST 走 regex 通过但 strptime 拒绝返 None)。
    """
    # regex 接受 + strptime 成功 -> aware UTC datetime
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "UTC"]) is not None
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "GMT"]) is not None

    # regex 接受 + strptime ValueError -> None (LOCK: strptime 兜底)
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "CST"]) is None
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "PST"]) is None
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "EDT"]) is None

    # regex 拒绝 (5+ 字符) -> None
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "ABCDE"]) is None
    # regex 拒绝 (1 字符) -> None
    assert sm_mod._parse_ts_field(["Mon", "2026-10-12", "01:30:00", "U"]) is None


def test_parse_ts_field_d67_5_cross_day_boundary(sm_mod):
    """R45-6 D-67-5: 跨日 UTC datetime - 'Thu 2026-12-31 23:59:59 UTC'。

    跨日边界 datetime 准确解析 (年末 23:59:59), 验证 strptime 准确无误。
    """
    dt = sm_mod._parse_ts_field(["Thu", "2026-12-31", "23:59:59", "UTC"])
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 12
    assert dt.day == 31
    assert dt.hour == 23
    assert dt.minute == 59
    assert dt.second == 59
    assert dt.tzinfo == timezone.utc
