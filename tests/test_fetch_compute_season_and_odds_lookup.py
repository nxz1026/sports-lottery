"""R36-7: scripts/core/data/fetch.py::_compute_season + _build_odds_lookup 7 测 + D-21 锁定。

现状：
- _compute_season (line 234-245) — 跨年赛季计算，0 直测
- _build_odds_lookup (line 248-265) — fixture_id → odds 查找表，0 直测

D-21 bug-shaped 锁定（fetch.py:242）：欧洲联赛跨年赛季分界月 = 7
（>= 7 为当前年）。美国/亚洲联赛切月不同，回归测必须包含 `month=6→prev`
与 `month=7→cur` 双向边界。一改 cutoff 全季归档全错。

本轮补 7 测：
- _compute_season 7 月 → 当年
- _compute_season 6 月 → 前一年
- _compute_season 2 月 → 前一年
- _compute_season 短串 fallback（< 4 → year=2025, month=7）
- _build_odds_lookup 网络失败 → {} 兜底
- _build_odds_lookup fixture_id 非整 → 跳过
- _build_odds_lookup 正常响应 → lookup dict
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
def fetch_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "fetch_compute_season_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _compute_season ────────────────────────


def test_compute_season_july_returns_current_year(fetch_mod):
    """R36-7 D-21：7 月 → 当年（赛季起算月）。"""
    assert fetch_mod._compute_season("20260715") == "2026"
    assert fetch_mod._compute_season("20250901") == "2025"


def test_compute_season_june_returns_previous_year(fetch_mod):
    """R36-7 D-21：6 月 → 前一年（跨年赛季尾月）。"""
    assert fetch_mod._compute_season("20260601") == "2025"
    assert fetch_mod._compute_season("20260228") == "2025"


def test_compute_season_december_returns_current_year(fetch_mod):
    """R36-7：12 月 → 当年（赛季中）。"""
    assert fetch_mod._compute_season("20251231") == "2025"


def test_compute_season_short_string_fallback_to_2025_july(fetch_mod):
    """R36-7：短串（< 4 字符）→ year=2025 fallback；< 6 字符 → month=7 fallback。"""
    # 太短 → year=2025 + month=7 → "2025"
    assert fetch_mod._compute_season("2025") == "2025"
    assert fetch_mod._compute_season("202") == "2025"
    assert fetch_mod._compute_season("") == "2025"


# ──────────────────────── _build_odds_lookup ────────────────────────


def test_build_odds_lookup_network_failure_returns_empty(fetch_mod, monkeypatch):
    """R36-7：网络异常（_retry_request raise）→ {} 兜底（warning 而非 raise）。"""
    def boom_retry(req, max_retries=3, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(fetch_mod, "_retry_request", boom_retry)
    out = fetch_mod._build_odds_lookup(39, "2026", "2026-07-15", "test-key", {})
    assert out == {}


def test_build_odds_lookup_filters_non_int_fixture_ids(fetch_mod, monkeypatch):
    """R36-7：fixture_id 缺或非真值 → 跳过该 entry，不污染 lookup。"""
    fake_data = {
        "response": [
            {"fixture": {"id": 123}, "bookmakers": ["bm_a"]},   # OK
            {"fixture": {"id": None}, "bookmakers": ["bm_b"]},  # id=None → skip
            {"fixture": {}, "bookmakers": ["bm_c"]},            # 缺 id → skip
            {"bookmakers": ["bm_d"]},                            # 缺 fixture → skip
            {"fixture": {"id": 456}, "bookmakers": ["bm_e"]},   # OK
        ]
    }

    def fake_retry(req, max_retries=3, timeout=None):
        return fake_data
    monkeypatch.setattr(fetch_mod, "_retry_request", fake_retry)

    out = fetch_mod._build_odds_lookup(39, "2026", "2026-07-15", "test-key", {})
    # 仅 123 和 456 进 lookup
    assert set(out.keys()) == {123, 456}
    assert out[123] == ["bm_a"]
    assert out[456] == ["bm_e"]


def test_build_odds_lookup_normal_response_builds_lookup(fetch_mod, monkeypatch):
    """R36-7：正常 response → fixture_id → bookmakers 列表 lookup dict。"""
    fake_data = {
        "response": [
            {"fixture": {"id": 100}, "bookmakers": [{"id": 1, "name": "B1"}]},
            {"fixture": {"id": 200}, "bookmakers": [{"id": 2, "name": "B2"}]},
        ]
    }

    def fake_retry(req, max_retries=3, timeout=None):
        return fake_data
    monkeypatch.setattr(fetch_mod, "_retry_request", fake_retry)

    out = fetch_mod._build_odds_lookup(39, "2026", "2026-07-15", "test-key", {})
    assert len(out) == 2
    assert out[100] == [{"id": 1, "name": "B1"}]
    assert out[200] == [{"id": 2, "name": "B2"}]
