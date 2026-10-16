"""R36-5: scripts/core/data/fetch.py::_retry_request 5 测直测 + D-19 锁定（Top 6）。

现状：通用重试机制 0 直测；仅在 test_p0_window_mc_stablekey.py / 
test_setup_league_run_branches.py 中以 monkeypatch 间接使用。
所有外部 API 调用的统一重试器，指数退避。

D-19 bug-shaped 锁定（fetch.py:100）：`delay = ESPN_RETRY_DELAY_SECONDS * 2^(attempt-1)`
指数退避基数 30s × {1, 2, 4} = 30/60/120s。max_retries=2/3/5 任一处偏离
都会显著影响真实网络抖动场景的总耗时。

本轮补 5 测：
- 第一次成功 → 不重试，不 sleep（_last_resp_headers 写入）
- 第 N 次成功 → 指数退避 sleep 调用次数 = N-1
- 重试 max_retries 次后最终 raise
- delay 序列：30 / 60 / 120（D-19 锁定）
- max_retries=2 → 只重试 1 次（边界）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _FakeResponse:
    """模拟 urllib.request.urlopen 上下文管理器返的响应对象。"""

    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def fetch_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "fetch_retry_request_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # 清空 _last_resp_headers 防跨测试污染
    mod._last_resp_headers.clear()
    return mod


def _make_req():
    """构造一个 urllib.request.Request 桩对象（不真发请求，urlopen 会被 mock）。"""
    import urllib.request
    return urllib.request.Request("http://example.test/api")


# ──────────────────────── _retry_request ────────────────────────


def test_retry_request_first_attempt_succeeds_no_sleep(fetch_mod, monkeypatch):
    """R36-5：第一次成功 → 不重试，不 sleep。"""
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(b'{"ok":true}', {"X-RateLimit-Remaining": "100"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    req = _make_req()
    out = fetch_mod._retry_request(req, max_retries=3)
    assert out == {"ok": True}
    assert sleeps == []
    # _last_resp_headers 写入（保留原大小写）
    assert "X-RateLimit-Remaining" in fetch_mod._last_resp_headers
    assert fetch_mod._last_resp_headers["X-RateLimit-Remaining"] == "100"


def test_retry_request_succeeds_on_nth_attempt_with_backoff(fetch_mod, monkeypatch):
    """R36-5：第 3 次成功 → 2 次指数退避 sleep 调用。"""
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise OSError(f"connection refused (call {call_count['n']})")
        return _FakeResponse(b'{"ok":true}', {"X-RateLimit-Remaining": "99"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    req = _make_req()
    out = fetch_mod._retry_request(req, max_retries=3)
    assert out == {"ok": True}
    # 2 次 sleep（D-19 序列：30, 60）
    assert sleeps == [fetch_mod.ESPN_RETRY_DELAY_SECONDS * 1,
                      fetch_mod.ESPN_RETRY_DELAY_SECONDS * 2]
    assert call_count["n"] == 3


def test_retry_request_raises_after_max_retries(fetch_mod, monkeypatch):
    """R36-5：所有 attempt 都失败 → 最终 raise（不写 _last_resp_headers）。"""
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=None):
        raise OSError("persistent failure")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    req = _make_req()
    with pytest.raises(OSError, match="persistent failure"):
        fetch_mod._retry_request(req, max_retries=3)
    # max_retries-1 次 sleep（最后一次不 sleep）
    assert len(sleeps) == 2
    # _last_resp_headers 保持空
    assert dict(fetch_mod._last_resp_headers) == {}


def test_retry_request_d19_exponential_backoff_sequence(fetch_mod, monkeypatch):
    """R36-5 D-19 锁定：delay 序列 = ESPN_RETRY_DELAY_SECONDS * 2^(attempt-1)。

    max_retries=5 → 4 次 sleep（最后一次失败 raise）；序列 30 / 60 / 120 / 240。
    """
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=None):
        raise OSError("never succeed")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    req = _make_req()
    with pytest.raises(OSError):
        fetch_mod._retry_request(req, max_retries=5)
    # D-19 序列
    expected = [
        fetch_mod.ESPN_RETRY_DELAY_SECONDS * (2 ** 0),  # 30
        fetch_mod.ESPN_RETRY_DELAY_SECONDS * (2 ** 1),  # 60
        fetch_mod.ESPN_RETRY_DELAY_SECONDS * (2 ** 2),  # 120
        fetch_mod.ESPN_RETRY_DELAY_SECONDS * (2 ** 3),  # 240
    ]
    assert sleeps == expected


def test_retry_request_max_retries_2_only_one_retry(fetch_mod, monkeypatch):
    """R36-5：max_retries=2 → 只重试 1 次（最小退化边界）。"""
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    def fake_urlopen(req, timeout=None):
        raise OSError("always fail")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    req = _make_req()
    with pytest.raises(OSError):
        fetch_mod._retry_request(req, max_retries=2)
    # 1 次 sleep
    assert len(sleeps) == 1
    assert sleeps[0] == fetch_mod.ESPN_RETRY_DELAY_SECONDS * 1