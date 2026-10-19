"""R36-6: scripts/core/data/fetch.py::_update_rate_limit_from_response 6 测 + D-20 锁定（Top 7）。

现状：API 响应头速率限制提取函数 0 直测；仅在 test_setup_league_run_branches.py
中以 monkeypatch 间接使用。R35 P1 修复"死功能"激活后值得直测。

D-20 bug-shaped 锁定（fetch.py:71, 76）：双 alias 同时识别
`x-ratelimit-requests-remaining` / `x-requests-remaining`（保留值）。
API-Football 已倾向 `x-requests-*` 但旧 key 仍兼容，移除任一会让 R35 P1
修复回归。

本轮补 6 测：
- 大小写混合 header key（`key.lower()` 路径）
- 双 alias 识别（`x-ratelimit-requests-remaining` + `x-requests-remaining`）
- 非法 int（`int(val)` try/except 吞错路径）
- < 10 warning 触发 vs >= 10 不触发
- `last_updated` 写入（`time.strftime` 副作用）
- 更新既有的 limit / remaining（不重置）
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
def fetch_mod():
    spec = importlib.util.spec_from_file_location(
        "fetch_update_rate_limit_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 重置 _rate_limit_info 防跨测试污染
    mod._rate_limit_info["remaining"] = None
    mod._rate_limit_info["limit"] = None
    mod._rate_limit_info["last_updated"] = None
    return mod


# ──────────────────────── _update_rate_limit_from_response ────────────────────────


def test_update_rate_limit_lowercases_keys(fetch_mod):
    """R36-6：`key.lower()` 路径 — 大小写混合 header 仍能识别。

    API 响应头实际返回可能是 "X-RateLimit-Requests-Remaining"（首字母大写）。
    """
    fetch_mod._update_rate_limit_from_response(
        {"X-RateLimit-Requests-Remaining": "100"})
    assert fetch_mod._rate_limit_info["remaining"] == 100


def test_update_rate_limit_d20_both_aliases_recognized(fetch_mod):
    """R36-6 D-20 锁定：双 alias 同时识别 `x-ratelimit-requests-remaining` / `x-requests-remaining`。

    API-Football 已倾向 `x-requests-*` 但旧 key 仍兼容，移除任一 alias 会让 R35 P1 修复回归。
    """
    # 旧 alias
    fetch_mod._update_rate_limit_from_response({
        "X-RateLimit-Requests-Remaining": "50",
        "X-RateLimit-Requests-Limit": "100",
    })
    assert fetch_mod._rate_limit_info["remaining"] == 50
    assert fetch_mod._rate_limit_info["limit"] == 100
    # 新 alias 覆盖（最新调用更新）
    fetch_mod._update_rate_limit_from_response({
        "X-Requests-Remaining": "25",
        "X-Requests-Limit": "75",
    })
    assert fetch_mod._rate_limit_info["remaining"] == 25
    assert fetch_mod._rate_limit_info["limit"] == 75


def test_update_rate_limit_invalid_int_swallowed(fetch_mod):
    """R36-6：非法 int（`int(val)` try/except）→ 不抛，remaining 保持 None。

    `int(val)` ValueError/TypeError 兜底。锁住异常吞行为。
    """
    fetch_mod._update_rate_limit_from_response({
        "X-RateLimit-Requests-Remaining": "not_a_number"})
    assert fetch_mod._rate_limit_info["remaining"] is None


def test_update_rate_limit_low_remaining_warns(fetch_mod, caplog):
    """R36-6：remaining < 10 → warning 触发（"API-Football rate limit low"）。"""
    with caplog.at_level("WARNING"):
        fetch_mod._update_rate_limit_from_response(
            {"X-RateLimit-Requests-Remaining": "5"})
    assert fetch_mod._rate_limit_info["remaining"] == 5
    assert any("rate limit low" in r.message and "5" in r.message
               for r in caplog.records)


def test_update_rate_limit_high_remaining_no_warning(fetch_mod, caplog):
    """R36-6：remaining >= 10 → 不触发 warning。"""
    with caplog.at_level("WARNING"):
        fetch_mod._update_rate_limit_from_response(
            {"X-RateLimit-Requests-Remaining": "100"})
    assert fetch_mod._rate_limit_info["remaining"] == 100
    # 无 rate limit warning
    assert not any("rate limit low" in r.message for r in caplog.records)


def test_update_rate_limit_writes_last_updated_timestamp(fetch_mod):
    """R36-6：last_updated 写入（`time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`）。

    副作用：每次调用更新 last_updated 为当前 UTC 时间。
    """
    before = fetch_mod._rate_limit_info["last_updated"]
    fetch_mod._update_rate_limit_from_response(
        {"X-RateLimit-Requests-Remaining": "100"})
    after = fetch_mod._rate_limit_info["last_updated"]
    # last_updated 应被更新为非 None 字符串
    assert after is not None
    assert isinstance(after, str)
    # 格式校验：YYYY-MM-DDTHH:MM:SSZ
    assert after.endswith("Z")
    assert "T" in after
    assert len(after) == 20  # 2017-01-01T00:00:00Z 长度
