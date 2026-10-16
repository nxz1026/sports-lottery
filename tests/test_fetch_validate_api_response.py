"""R35-4: scripts/core/data/fetch.py::_validate_api_response 4 测直测。

现状：API 响应校验入口 0 直测，仅在 test_setup_league_run_branches.py 和
test_p0_window_mc_stablekey.py 中以 monkeypatch 间接使用。60 个 caller 依赖。

4 分支：
- 非 dict 输入 → warning + 返 {} 兜底
- 缺 required_keys → warning
- message 含 "error"（大小写不敏感）→ warning
- count == 0 且含 "response" key → info（不返空，仍透传原 data）

本轮补 4 测：
- 非 dict 输入 → {} + warning（caplog 验证）
- 缺 required_keys → warning（caplog 验证缺失字段名）
- message 含 "error" → warning
- count=0 + response → info + 透传
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
        "fetch_validate_api_response_test",
        REPO_ROOT / "scripts" / "core" / "data" / "fetch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _validate_api_response ────────────────────────


def test_validate_api_response_non_dict_returns_empty_with_warning(fetch_mod, caplog):
    """R35-4：非 dict 输入（list/str/None）→ warning + 返 {} 兜底。"""
    with caplog.at_level("WARNING"):
        assert fetch_mod._validate_api_response([], "test_source") == {}
        assert fetch_mod._validate_api_response("string", "test_source") == {}
        assert fetch_mod._validate_api_response(None, "test_source") == {}
        assert fetch_mod._validate_api_response(123, "test_source") == {}
    # warning 至少 4 次（含 4 个非 dict 输入）
    warn_count = sum(1 for r in caplog.records
                     if "not a dict" in r.message and "test_source" in r.message)
    assert warn_count >= 4


def test_validate_api_response_missing_required_keys_warns(fetch_mod, caplog):
    """R35-4：required_keys 真值，data 缺部分键 → warning 含缺失字段名。"""
    data = {"response": [{"id": 1}]}  # 缺 "paging" / "results"
    with caplog.at_level("WARNING"):
        out = fetch_mod._validate_api_response(
            data, "api_football", required_keys=["response", "paging", "results"])
    # 仍透传原 data（不返空）
    assert out == data
    # warning 含缺失字段名
    assert any("missing keys" in r.message and "paging" in r.message
               for r in caplog.records)


def test_validate_api_response_error_message_warns(fetch_mod, caplog):
    """R35-4：message 含 "error"（大小写不敏感）→ warning。

    `"error" in str(data.get("message", "")).lower()`：消息原文不区分大小写。
    """
    data = {"message": "API rate limit ERROR exceeded"}
    with caplog.at_level("WARNING"):
        out = fetch_mod._validate_api_response(data, "api_football")
    assert out == data
    assert any("error message" in r.message and "rate limit" in r.message
               for r in caplog.records)


def test_validate_api_response_count_zero_logs_info(fetch_mod, caplog):
    """R35-4：count=0 + "response" key → info（不返空，透传 data）。

    info 级别而非 warning；调用方知道是合法空结果集。
    """
    data = {"count": 0, "response": []}
    with caplog.at_level("INFO"):
        out = fetch_mod._validate_api_response(data, "api_football")
    # 透传原 data
    assert out == data
    assert any("0 results" in r.message and "api_football" in r.message
               for r in caplog.records)