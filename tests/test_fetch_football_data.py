"""R49-4: scripts/core/data/fetch.py::fetch_football_data 4 cases direct + D-90 lock.

fetch_football_data (L184-L231, 48 行) 从 football-data.org 获取数据:
1) api_key = env FOOTBALL_DATA_API_KEY
2) league_id = config['league_id']
3) 解析 dates_str (split on '-') + fmt_date 8 位 -> YYYY-MM-DD
4) whole_season=True -> URL 不带 date 参数
5) headers 加 X-Auth-Token (仅 api_key 非空)
6) _retry_request (D-19) + _validate_api_response (D-19)
7) convert_football_data_to_espn_format (D-73)
8) except: logger.warning + raise

R36-5/6/7 + R48-3 + R49-2/3 D-19/20/21/85/88/89 已锁 _retry_request /
_validate_api_response / _build_odds_lookup / fetch_events / fetch_espn,
但 fetch_football_data 真函数自身核心契约 (URL 构建 + whole_season 切换 +
X-Auth-Token 头 + fmt_date 8 位) 0 直测。

D-90 锁定 4 契约:
- D-90-1: 主流程 happy (whole_season=False + api_key 存在) -> URL 含
  dateFrom/dateTo + X-Auth-Token 头 + 返 convert_football_data_to_espn_format 结果
- D-90-2: whole_season=True -> URL 不含 dateFrom/dateTo
- D-90-3: api_key 缺 -> URL 不含 X-Auth-Token 头
- D-90-4: fmt_date 8 位数字转换 (20260715 -> 2026-07-15)

依赖 stub:
- _retry_request (monkeypatch 返受控 data)
- convert_football_data_to_espn_format (stub 返受控 events)
- os.environ (FOOTBALL_DATA_API_KEY)
- urllib.request.Request (捕获 url + headers 验证)
"""
from __future__ import annotations

import importlib.util
import os
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
    """Spec-load fetch.py + 默认 stub 下游 + 清 env var。"""
    spec = importlib.util.spec_from_file_location(
        "fetch_football_data_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 默认 stub: _retry_request 返空 matches
    monkeypatch.setattr(mod, "_retry_request",
                        lambda req, max_retries=3, timeout=None: {"matches": []})
    # convert_football_data_to_espn_format stub
    monkeypatch.setattr(mod, "convert_football_data_to_espn_format",
                        lambda data, config: [{"espn_event": "stub"}])
    # 清 env var 防污染
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    return mod


def _capture_request(monkeypatch, mod):
    """monkeypatch urllib.request.Request 捕获构造的 Request (url + headers)。"""
    captured = {"url": None, "headers": None}

    def fake_request(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers or {}
        # 返个有 full_url 属性的对象, 后续 urlopen 不被调 (因为 _retry_request 已 stub)
        class _R:
            pass
        r = _R()
        r.full_url = url
        return r

    monkeypatch.setattr(mod.urllib.request, "Request", fake_request)
    return captured


# ──────────────────────── D-90 四契约 ────────────────────────


def test_fetch_football_data_d90_1_happy_path_with_dates_and_api_key(fetch_mod, monkeypatch):
    """R49-4 D-90-1: 主流程 happy - URL 含 dateFrom/dateTo + X-Auth-Token 头。

    dates_str='20260715-20260720' -> dateFrom=2026-07-15, dateTo=2026-07-20。
    api_key 存在 -> headers['X-Auth-Token'] = api_key。
    """
    mod = fetch_mod
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fd-test-key")
    captured = _capture_request(monkeypatch, mod)

    config = {"league_id": "PL", "season": 2025}
    out = mod.fetch_football_data("20260715-20260720", config, whole_season=False)
    # URL 验证
    assert "competitions/PL/matches" in captured["url"]
    assert "dateFrom=2026-07-15" in captured["url"]
    assert "dateTo=2026-07-20" in captured["url"]
    # X-Auth-Token
    assert captured["headers"].get("X-Auth-Token") == "fd-test-key"
    # 返 convert_football_data_to_espn_format stub 结果
    assert out == [{"espn_event": "stub"}]


def test_fetch_football_data_d90_2_whole_season_omits_dates(fetch_mod, monkeypatch):
    """R49-4 D-90-2: whole_season=True -> URL 不含 dateFrom/dateTo。

    L211: `f".../matches"` (无参数) vs L213: `f".../matches?dateFrom=...&dateTo=..."`。
    """
    mod = fetch_mod
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fd-key")
    captured = _capture_request(monkeypatch, mod)

    config = {"league_id": "PL", "season": 2025}
    mod.fetch_football_data("20260715-20260720", config, whole_season=True)
    # URL 不含 dateFrom/dateTo
    assert "competitions/PL/matches" in captured["url"]
    assert "dateFrom" not in captured["url"]
    assert "dateTo" not in captured["url"]
    # 没有 ? 单独分隔
    assert "?" not in captured["url"]


def test_fetch_football_data_d90_3_missing_api_key_no_auth_header(fetch_mod, monkeypatch):
    """R49-4 D-90-3: api_key 缺 -> URL 不含 X-Auth-Token 头。

    L218-219 `if api_key: headers['X-Auth-Token'] = api_key` 仅非空加头。
    """
    mod = fetch_mod
    # 确保 env 未设
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    captured = _capture_request(monkeypatch, mod)

    config = {"league_id": "PL", "season": 2025}
    mod.fetch_football_data("20260715-20260720", config)
    # headers 应只有 User-Agent, 无 X-Auth-Token
    assert "X-Auth-Token" not in captured["headers"]
    assert "User-Agent" in captured["headers"]
    # 验证 env 真未设 (防止 fixture 残留)
    assert "FOOTBALL_DATA_API_KEY" not in os.environ


def test_fetch_football_data_d90_4_fmt_date_eight_digit_conversion(fetch_mod, monkeypatch):
    """R49-4 D-90-4: fmt_date 8 位数字转换 (20260715 -> 2026-07-15)。

    L200-L204 内嵌 fmt_date: 8 位数字 -> "YYYY-MM-DD", 否则原样。
    """
    mod = fetch_mod
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fd-key")
    captured = _capture_request(monkeypatch, mod)

    config = {"league_id": "PL", "season": 2025}
    mod.fetch_football_data("20260715-20260720", config, whole_season=False)
    # URL 含 dateFrom=2026-07-15 (fmt_date 转换 20260715)
    assert "dateFrom=2026-07-15" in captured["url"]
    assert "dateTo=2026-07-20" in captured["url"]
    # 验证未转 - 仍含原始 8 位数字 (URL 含 raw 数字)
    # 注: 实际 URL 只有转换后格式, 因为 fmt_date 替换
    assert "dateFrom=20260715" not in captured["url"]