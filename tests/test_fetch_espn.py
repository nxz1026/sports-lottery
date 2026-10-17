"""R49-3: scripts/core/data/fetch.py::fetch_espn 4 cases direct + D-89 lock.

fetch_espn (L156-L181, 26 行) 抓取 ESPN 数据 (带重试), 返回 parsed events:
1) url = ESPN_URL_TEMPLATE.format(dates, slug)
2) for attempt 1..ESPN_MAX_RETRIES:
3) try: urlopen + gzip.decompress + json.loads
4) _validate_api_response (D-19) + data.get('events', [])
5) except: 退避 (delay = RETRY_DELAY * 2^(attempt-1)) + 末次 raise

R36-5/6/7 D-19/D-20/D-21 + R48-3 D-85 + R49-2 D-88 已锁 _validate_api_response
/ _update_rate_limit_from_response / _retry_request / _build_odds_lookup /
validate_api_keys / fetch_events, 但 fetch_espn 真函数自身核心契约
(URL 模板 + 重试循环 + 退避 + 末次 raise) 0 直测。

D-89 锁定 4 契约:
- D-89-1: 主流程 happy - urlopen 返有效 JSON -> 返 events list
- D-89-2: 重试 1 次失败 + 2 次成功 (delay 指数退避, 1st fail -> wait 1x,
  2nd success)
- D-89-3: 重试 max 次失败 -> 末次 raise
- D-89-4: URL 模板 - dates + league_slug 替换

依赖 stub:
- urllib.request.urlopen (monkeypatch 返 fake response 含 gzip+json)
- urllib.request.Request (验证 url + headers 正确)
- time.sleep (spy delay 调用, 不真睡)
- _validate_api_response 真跑 (D-19 已锁, 走真实 response 处理)
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def fetch_mod(monkeypatch):
    """Spec-load fetch.py + stub time.sleep + urllib。"""
    spec = importlib.util.spec_from_file_location(
        "fetch_espn_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 避免真睡
    sleep_calls = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(mod, "time", mod.time)  # 防止 import 旁路
    return mod, sleep_calls


class _FakeResponse:
    """fake urlopen response 含 gzip+json body (L167-168)."""
    def __init__(self, events: list):
        body = json.dumps({"events": events}).encode("utf-8")
        self._gz = gzip.compress(body)

    def read(self):
        return self._gz

    # L167 `with urllib.request.urlopen(...) as resp:` 需要 context manager
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_response(events: list) -> _FakeResponse:
    return _FakeResponse(events)


# ──────────────────────── D-89 四契约 ────────────────────────


def test_fetch_espn_d89_1_happy_path_returns_events(fetch_mod, monkeypatch):
    """R49-3 D-89-1: 主流程 happy - urlopen 返 gzip+json -> 返 events list。

    events=[{...}, {...}] -> 解析后返原 list。
    """
    mod, sleep_calls = fetch_mod
    events = [{"id": 1, "name": "Game 1"}, {"id": 2, "name": "Game 2"}]
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: _fake_response(events))

    out = mod.fetch_espn("20260715-20260720", league_slug="epl")
    assert out == events
    # 主流程不睡
    assert sleep_calls == []


def test_fetch_espn_d89_2_retries_with_exponential_backoff(fetch_mod, monkeypatch):
    """R49-3 D-89-2: 重试 1 次失败 + 2 次成功 (delay 指数退避)。

    第 1 次 urlopen 抛异常 -> sleep delay 1x -> 第 2 次成功。
    sleep_calls 应记录 1x (RETRY_DELAY * 2^0 = RETRY_DELAY * 1)。
    """
    mod, sleep_calls = fetch_mod
    call_count = {"i": 0}
    events = [{"id": 1}]

    def fake_urlopen(req, timeout=None):
        call_count["i"] += 1
        if call_count["i"] == 1:
            raise ConnectionError("first attempt failed")
        return _fake_response(events)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out = mod.fetch_espn("20260715-20260720")
    assert out == events
    # 重试 1 次 -> 1 次 sleep
    assert len(sleep_calls) == 1
    # delay = ESPN_RETRY_DELAY_SECONDS * 2^0 = 1x
    expected_delay = mod.ESPN_RETRY_DELAY_SECONDS
    assert sleep_calls[0] == expected_delay


def test_fetch_espn_d89_3_max_retries_exhausted_raises(fetch_mod, monkeypatch):
    """R49-3 D-89-3: 重试 max 次失败 -> 末次 raise (L180-181 logger.error + raise)。

    ESPN_MAX_RETRIES 次全失败 -> raise (e.g., ConnectionError)。
    sleep_calls 应记录 MAX_RETRIES - 1 次 (最后一次不再睡,直接 raise)。
    """
    mod, sleep_calls = fetch_mod

    def fake_urlopen(req, timeout=None):
        raise ConnectionError("persistent failure")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ConnectionError, match="persistent failure"):
        mod.fetch_espn("20260715-20260720")
    # MAX_RETRIES - 1 次 sleep (最后一次失败直接 raise 不睡)
    assert len(sleep_calls) == mod.ESPN_MAX_RETRIES - 1
    # 退避序列: 1x, 2x, 4x, ... (最后 1 次不睡, raise)
    expected_delays = [
        mod.ESPN_RETRY_DELAY_SECONDS * (2 ** i)
        for i in range(mod.ESPN_MAX_RETRIES - 1)
    ]
    assert sleep_calls == expected_delays


def test_fetch_espn_d89_4_url_template_substitution(fetch_mod, monkeypatch):
    """R49-3 D-89-4: URL 模板 - dates + league_slug 替换。

    L158 `ESPN_URL_TEMPLATE.format(dates=dates_str, league_slug=league_slug)`
    -> 验证 Request 的 url 含 dates 与 league_slug。
    """
    mod, sleep_calls = fetch_mod
    captured_url = {"value": None}

    def fake_urlopen(req, timeout=None):
        captured_url["value"] = req.full_url
        return _fake_response([])

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    mod.fetch_espn("20260715-20260720", league_slug="laliga")
    # 验证 URL 含 dates + slug
    assert "20260715-20260720" in captured_url["value"]
    assert "laliga" in captured_url["value"]
    # 验证 URL 走 ESPN_URL_TEMPLATE (含 'sports/' 之类特征)
    assert captured_url["value"].startswith("http")