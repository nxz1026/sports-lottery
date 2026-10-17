"""R40-1: scripts/bball/odds_api.py::_sanitize_url 4 cases direct + D-32 lock.

URL API key 脱敏 + 长度截断 helper 0 direct tests; OddsApiError 间接走
_http_get 路径触发 (test_bball_run exit_code 测), 但 helper 自身契约未锁。

helper 含两条契约 (D-32):
- (?apiKey=) / (&apiKey=) 双模式都替换 key 部分为 ***;
  regex `r"([?&]apiKey=)[^&]*"` 匹配 question-mark 或 ampersand 前缀,
  捕获组 1 即前缀 + "=", 替换保留前缀 + "***"
- 500 字符硬截断: re.sub 完整替换后才裁, 故超长 URL 的 *** 写入后再裁
- 无 apiKey 时 URL 完全不变 (除截断)

4 tests (EASY lockdown):
- ?apiKey= -> ?apiKey=***
- &apiKey= -> &apiKey=***
- 超长 URL (>500 字符) -> 截断 500, 含 key 替换后内容
- 无 apiKey -> 原 URL 原样 (除截断)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def odds_mod():
    """加载 scripts/bball/odds_api.py 为唯一 module name, 复用 R37-5/R39-2 模式。"""
    spec = importlib.util.spec_from_file_location(
        "odds_api_sanitize_url_test",
        REPO_ROOT / "scripts" / "bball" / "odds_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-32 apiKey 替换 ────────────────────────


def test_sanitize_url_question_mark_apikey(odds_mod):
    """R40-1 D-32: URL 含 ?apiKey=xxx -> ?apiKey=*** (question mark 前缀)。"""
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds?apiKey=secret123&regions=uk"
    out = odds_mod._sanitize_url(url)
    assert "apiKey=secret123" not in out
    assert "apiKey=***" in out
    assert out == ("https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
                   "?apiKey=***&regions=uk")


def test_sanitize_url_ampersand_apikey(odds_mod):
    """R40-1 D-32: URL 含 &apiKey=xxx -> &apiKey=*** (ampersand 前缀也命中)。

    query string 中间位置 (前有别的 param) 用 & 分隔, regex ([?&]apiKey=)
    双前缀都命中。
    """
    url = ("https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
           "?regions=uk&apiKey=my_secret_key&markets=h2h")
    out = odds_mod._sanitize_url(url)
    assert "apiKey=my_secret_key" not in out
    assert "&apiKey=***" in out
    assert "markets=h2h" in out  # 后续 param 保留


# ──────────────────────── 长度截断 (LOCK) ────────────────────────


def test_sanitize_url_d32_truncates_to_500_chars(odds_mod):
    """R40-1 D-32: URL > 500 字符硬截断 (key 替换之后再裁)。

    注: key 部分已缩到 *** 3 字符; 后面的 padding 通过 & 分隔避免被
    regex 当作 key 全部吞掉。截断点会把尾部 padding 切掉一部分。
    """
    base = ("https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
            "?apiKey=secret")
    # 用 padding + 分隔 query, 让 regex 只吞 "secret" 到第一个 & 之间的 key
    padding = "&pad=" + "x" * 600
    out = odds_mod._sanitize_url(base + padding)
    # 严格 500 字符 ([:500])
    assert len(out) == 500
    # 含 sanitized key
    assert "apiKey=***" in out
    # 不要含原 key 值
    assert "apiKey=secret" not in out


# ──────────────────────── 无 apiKey ────────────────────────


def test_sanitize_url_no_apikey_unchanged(odds_mod):
    """R40-1: URL 无 apiKey 段 -> regex 不命中, 原 URL 原样 (除截断)。"""
    url = ("https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
           "?regions=uk&markets=h2h")
    out = odds_mod._sanitize_url(url)
    assert out == url
