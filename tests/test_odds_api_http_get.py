"""R40-4: scripts/bball/odds_api.py::_http_get 5 cases direct + D-35 lock.

JSON GET helper 0 direct tests; fetch_odds_games / fetch_completed_games
覆盖 happy path 但 helper 本身未锁。helper 是 5 类异常的 catch-all:
HTTPError (401/404) / URLError (DNS 挂) / TimeoutError / ValueError (非 JSON)
统一抛 OddsApiError, url 在 error url 字段经 _sanitize_url 替换 apiKey。

5 tests (HIGH):
- 正常 JSON -> dict
- HTTPError (401) -> OddsApiError; error.url 含 sanitized key
- URLError (DNS 挂) -> OddsApiError
- TimeoutError -> OddsApiError
- ValueError (非 JSON) -> OddsApiError

D-35 (sanitize-in-error lock):
- error.url 字段必走 _sanitize_url (regex 替换 apiKey)
- error.reason 必走 _sanitize_url 链
- raise from error (cause 链保留; 不吞原异常)

URL 用 ?apiKey=secret_real_key 触发 sanitize 路径, 验证 url 字段不泄露。
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

URL_WITH_KEY = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=secret_real_key"
URL_SANITIZED = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=***"


@pytest.fixture
def odds_mod():
    spec = importlib.util.spec_from_file_location(
        "odds_api_http_get_test",
        REPO_ROOT / "scripts" / "bball" / "odds_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_urlopen(monkeypatch, *, payload=None, raises=None):
    """Stub urllib.request.urlopen 为受控 context manager.

    payload: 字节串 (successful 返响应) ; raises: 抛出的异常
    """
    @contextmanager
    def fake(_request, timeout):
        if raises is not None:
            raise raises
        # 成功: 返 _FakeResponse
        yield _FakeResponse(payload)

    monkeypatch.setattr(
        sys.modules["urllib.request"], "urlopen", fake)


class _FakeResponse:
    """Mock urllib response; 提供 .read() 返 payload。"""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ──────────────────────── happy path ────────────────────────


def test_http_get_returns_dict_for_valid_json(odds_mod, monkeypatch):
    """R40-4: 正常 JSON 字节 -> _http_get 返 dict (原 json.loads 透传)。"""
    payload = json.dumps({"key": "value", "n": 42}).encode("utf-8")
    _fake_urlopen(monkeypatch, payload=payload)
    out = odds_mod._http_get(URL_WITH_KEY)
    assert out == {"key": "value", "n": 42}


# ──────────────────────── D-35 异常路径 (sanitize 验证) ────────────────────────


def test_http_get_d35_http_error_sanitized_url(odds_mod, monkeypatch):
    """R40-4 D-35: HTTPError (401) -> OddsApiError; error.url 含 sanitized key (不泄露)。"""
    err = urllib.error.HTTPError(
        url=URL_WITH_KEY, code=401, msg="Unauthorized",
        hdrs={}, fp=io.BytesIO(b""))
    _fake_urlopen(monkeypatch, raises=err)
    with pytest.raises(odds_mod.OddsApiError) as exc_info:
        odds_mod._http_get(URL_WITH_KEY)
    # url 字段走 _sanitize_url, 原始 key 必须被替换
    assert exc_info.value.url == URL_SANITIZED
    assert "secret_real_key" not in exc_info.value.url
    # cause 链保留
    assert exc_info.value.__cause__ is err


def test_http_get_d35_url_error_to_odds_api_error(odds_mod, monkeypatch):
    """R40-4 D-35: URLError (DNS fail) -> OddsApiError 兜底。"""
    err = urllib.error.URLError("Name or service not known")
    _fake_urlopen(monkeypatch, raises=err)
    with pytest.raises(odds_mod.OddsApiError) as exc_info:
        odds_mod._http_get(URL_WITH_KEY)
    assert exc_info.value.url == URL_SANITIZED
    assert exc_info.value.__cause__ is err


def test_http_get_d35_timeout_to_odds_api_error(odds_mod, monkeypatch):
    """R40-4 D-35: TimeoutError -> OddsApiError (兜底, 不抛穿)。"""
    err = TimeoutError("read timeout 15s")
    _fake_urlopen(monkeypatch, raises=err)
    with pytest.raises(odds_mod.OddsApiError) as exc_info:
        odds_mod._http_get(URL_WITH_KEY)
    assert exc_info.value.url == URL_SANITIZED
    assert exc_info.value.__cause__ is err


def test_http_get_d35_invalid_json_to_odds_api_error(odds_mod, monkeypatch):
    """R40-4 D-35: 响应非 JSON -> json.loads 抛 ValueError -> OddsApiError 兜底。

    锁定 fast-fail 契约: 不应该让原始 ValueError 冒到调用方, 必须统一
    OddsApiError 包装, url 字段 sanitized。
    """
    # 模拟成功 urlopen 但 response.read() 返非 JSON 字节, json.loads 抛 ValueError
    @contextmanager
    def fake_with_bad_json(_request, timeout):
        yield _FakeResponse(b"<html>not json at all</html>")

    monkeypatch.setattr(
        sys.modules["urllib.request"], "urlopen", fake_with_bad_json)

    with pytest.raises(odds_mod.OddsApiError) as exc_info:
        odds_mod._http_get(URL_WITH_KEY)
    assert exc_info.value.url == URL_SANITIZED
    # ValueError cause 链
    assert isinstance(exc_info.value.__cause__, ValueError)
