"""R52-1+2: ai/llm_client.py 4 个 0 直测 helper 10 测 + D-104/D-105 双契约锁。

R51 末已锁 D-97~D-103 (auth / features / i18n / poisson 内层).

ai/llm_client.py 的 4 个未直测 helper (L26-65 generate 是主入口,
被 integration test_ml_integration / test_pre_push_hook 间接覆盖, 不计 0 直测):

- _call_openai  (L68-101): 3-retry / 429 backoff / 200 parse / 请求异常 返 {}
- _call_gemini  (L104-129): 多 model fallback / 429+404 跳下个 / 其它 返 {}
- _parse_openai (L132-140): 从 resp 提取 JSON, strip ```json...``` fence
- _parse_gemini (L143-158): 从 resp.candidates 链提取 JSON, strip fence

D-104 锁 _parse_openai + _parse_gemini (4 测, 纯函数):
- D-104-1: _parse_openai 正常 JSON content 返 dict
- D-104-2: _parse_openai ```json``` fence 包裹 -> strip fence 后解析
- D-104-3: _parse_gemini 正常 JSON candidates.parts[0].text 返 dict
- D-104-4: _parse_gemini 解析失败 (无 candidates / KeyError) 返 {}

D-105 锁 _call_openai + _call_gemini (6 测, stub requests.post):
- D-105-1: _call_openai 200 + 合法 JSON -> 返 dict (1 POST 调用)
- D-105-2: _call_openai 429 x3 -> 3 retries 后返 {}
- D-105-3: _call_openai 非 200/429 (e.g. 500) -> 立即返 {}
- D-105-4: _call_openai requests.RequestException -> 返 {}
- D-105-5: _call_gemini 200 -> 1 POST 调用即返
- D-105-6: _call_gemini 429 fallback 链 -> 第 1 个 model 429, 第 2 个 200 即成功

策略:
- spec-load llm_client.py 到隔离 namespace (避免 _last_call / _rate_lock 跨测污染)
- _parse_* 用 mock resp (MagicMock + .json() 方法)
- _call_* 用 unittest.mock.patch("requests.post") 替 stub
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def llm_mod(monkeypatch):
    """Spec-load llm_client.py + stub env."""
    spec = importlib.util.spec_from_file_location(
        "ai_llm_client_test",
        REPO_ROOT / "ai" / "llm_client.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 重置全局 state (跨测隔离)
    monkeypatch.setattr(mod, "_last_call", 0.0)

    # api_key 不通过 env, 直接传参避免 env 污染
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    return mod


# ───────── helpers ─────────


def _mock_resp(status_code, payload=None, raise_json=False, headers=None):
    """构造 MagicMock 模拟 requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    if payload is not None:
        if raise_json:
            resp.json.side_effect = ValueError("bad json")
        else:
            resp.json.return_value = payload
    resp.text = json.dumps(payload) if payload else ""
    return resp


# ──────────────────────── D-104: 4 测 ────────────────────────


def test_d104_1_parse_openai_extracts_json_content(llm_mod):
    """D-104-1: _parse_openai 从 resp.json()['choices'][0]['message']['content'] 提取 JSON."""
    resp = _mock_resp(200, payload={
        "choices": [{"message": {"content": '{"key": "value"}'}}]
    })

    out = llm_mod._parse_openai(resp)

    assert out == {"key": "value"}


def test_d104_2_parse_openai_strips_json_fence(llm_mod):
    """D-104-2: _parse_openai 遇到 ```json...``` fence 时 strip fence 后再 parse."""
    fenced = '```json\n{"home": "Fulham", "cn": "富勒姆"}\n```'
    resp = _mock_resp(200, payload={
        "choices": [{"message": {"content": fenced}}]
    })

    out = llm_mod._parse_openai(resp)

    assert out == {"home": "Fulham", "cn": "富勒姆"}


def test_d104_3_parse_gemini_extracts_candidates_parts_text(llm_mod):
    """D-104-3: _parse_gemini 从 resp.json().candidates[0].content.parts[0].text 提取 JSON."""
    resp = _mock_resp(200, payload={
        "candidates": [{
            "content": {
                "parts": [{"text": '{"answer": 42}'}]
            }
        }]
    })

    out = llm_mod._parse_gemini(resp)

    assert out == {"answer": 42}


def test_d104_4_parse_gemini_handles_missing_keys(llm_mod):
    """D-104-4: _parse_gemini 解析失败 (缺 candidates / KeyError / 坏 JSON) -> 返 {}."""
    # 场景 a: 无 candidates
    resp_a = _mock_resp(200, payload={})
    assert llm_mod._parse_gemini(resp_a) == {}

    # 场景 b: candidates[0] 无 content
    resp_b = _mock_resp(200, payload={"candidates": [{}]})
    assert llm_mod._parse_gemini(resp_b) == {}

    # 场景 c: text 不是合法 JSON
    resp_c = _mock_resp(200, payload={
        "candidates": [{"content": {"parts": [{"text": "not-json-at-all"}]}}]
    })
    assert llm_mod._parse_gemini(resp_c) == {}


# ──────────────────────── D-105: 6 测 ────────────────────────


def test_d105_1_call_openai_returns_dict_on_200(llm_mod):
    """D-105-1: _call_openai 200 + 合法 JSON -> 1 POST 调用即返 dict."""
    resp = _mock_resp(200, payload={
        "choices": [{"message": {"content": '{"ok": true}'}}]
    })

    with patch("requests.post", return_value=resp) as mock_post:
        out = llm_mod._call_openai("p", "m", "https://api.example.com/v1", "k")

    assert out == {"ok": True}
    assert mock_post.call_count == 1
    # url 拼接
    called_url = mock_post.call_args[0][0]
    assert called_url == "https://api.example.com/v1/chat/completions"


def test_d105_2_call_openai_retries_429_three_times(llm_mod, monkeypatch):
    """D-105-2: _call_openai 429 -> 退避重试, MAX_ATTEMPTS 次全 429 后返 {}.

    关键: sleep 是内部实现 (time.sleep), 用 monkeypatch 让它立即返回避免测试慢.
    次数从固定的 3 改为模块常量 MAX_ATTEMPTS（2026-10-28：未实名账号 RPM≈1/分钟，
    3 次退避必然在配额窗口内空转完就放弃，富化整批丢分）。
    """
    resp_429 = _mock_resp(429)
    monkeypatch.setattr("time.sleep", lambda s: None)  # 跳过 sleep

    with patch("requests.post", return_value=resp_429) as mock_post:
        out = llm_mod._call_openai("p", "m", "https://api.example.com/v1", "k")

    assert out == {}
    assert mock_post.call_count == llm_mod.MAX_ATTEMPTS


def test_d105_3_call_openai_returns_empty_on_500(llm_mod):
    """D-105-3: _call_openai 500 (非 200/429) -> 立即返 {} 无 retry (L90-92)."""
    resp_500 = _mock_resp(500, payload={"error": "server"})

    with patch("requests.post", return_value=resp_500) as mock_post:
        out = llm_mod._call_openai("p", "m", "https://api.example.com/v1", "k")

    assert out == {}
    assert mock_post.call_count == 1  # 不 retry 非 200/429


def test_d105_4_call_openai_returns_empty_on_request_exception(llm_mod):
    """D-105-4: _call_openai requests.RequestException -> 返 {} (L97-99).

    即使尝试 3 次, 每次都抛 RequestException 也立即返 {}.
    """
    import requests as real_requests  # noqa: PLC0415 - real exception class

    with patch("requests.post", side_effect=real_requests.RequestException("network down")):
        out = llm_mod._call_openai("p", "m", "https://api.example.com/v1", "k")

    assert out == {}


def test_d105_5_call_gemini_returns_dict_on_200(llm_mod):
    """D-105-5: _call_gemini 200 -> 1 POST 调用即返 dict."""
    resp = _mock_resp(200, payload={
        "candidates": [{"content": {"parts": [{"text": '{"result": "yes"}'}]}}]
    })

    with patch("requests.post", return_value=resp) as mock_post:
        out = llm_mod._call_gemini("p", "gemini-2.0-flash", "k", 0.0)

    assert out == {"result": "yes"}
    assert mock_post.call_count == 1
    # URL 路径包含 model
    assert "gemini-2.0-flash" in mock_post.call_args[0][0]


def test_d105_6_call_gemini_falls_back_on_429(llm_mod, monkeypatch):
    """D-105-6: _call_gemini 第一个 model 429 -> fallback 到下个 model, 第 2 个 200 成功.

    L106 `[model] + [m for m in GEMINI_FALLBACK if m != model]` 形成链.
    """
    # 跳过 sleep
    monkeypatch.setattr("time.sleep", lambda s: None)

    resp_429 = _mock_resp(429)
    resp_ok = _mock_resp(200, payload={
        "candidates": [{"content": {"parts": [{"text": '{"recovered": true}'}]}}]
    })

    # 第 1 个 model 返 429, 第 2 个 model 返 200 (side_effect 列表)
    with patch("requests.post", side_effect=[resp_429, resp_ok]) as mock_post:
        out = llm_mod._call_gemini("p", "gemini-2.0-flash", "k", 0.0)

    assert out == {"recovered": True}
    assert mock_post.call_count == 2


# --- D-106：429 退避（2026-10-28 事故）--------------------------------------
# 未实名 agnes 账号实测 RPM≈1 次/分钟且无 Retry-After，旧的固定 10/20/30s
# 三次重试在配额窗口内必然全部撞 429 后放弃 ⇒ 富化整批丢分。

def test_d106_1_backoff_grows_exponentially(llm_mod, monkeypatch):
    """D-106-1: 无 Retry-After 时按 base*2^attempt 递增并封顶 BACKOFF_MAX_SECONDS."""
    monkeypatch.setattr(llm_mod, "BACKOFF_BASE_SECONDS", 20.0)
    monkeypatch.setattr(llm_mod, "BACKOFF_MAX_SECONDS", 120.0)
    monkeypatch.setattr(llm_mod.random, "uniform", lambda a, b: 0.0)

    assert llm_mod._backoff_seconds(0) == 20.0
    assert llm_mod._backoff_seconds(1) == 40.0
    assert llm_mod._backoff_seconds(2) == 80.0
    assert llm_mod._backoff_seconds(3) == 120.0  # 160 -> 封顶
    assert llm_mod._backoff_seconds(9) == 120.0


def test_d106_2_backoff_honours_retry_after(llm_mod, monkeypatch):
    """D-106-2: 响应带 Retry-After 时优先采用（仍受上限约束）."""
    monkeypatch.setattr(llm_mod, "BACKOFF_MAX_SECONDS", 120.0)

    resp = _mock_resp(429, headers={"Retry-After": "30"})
    assert llm_mod._backoff_seconds(0, resp) == 30.0

    resp_huge = _mock_resp(429, headers={"Retry-After": "9999"})
    assert llm_mod._backoff_seconds(0, resp_huge) == 120.0

    resp_bad = _mock_resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    monkeypatch.setattr(llm_mod.random, "uniform", lambda a, b: 0.0)
    assert llm_mod._backoff_seconds(0, resp_bad) == llm_mod.BACKOFF_BASE_SECONDS


def test_d106_3_call_openai_recovers_after_backoff(llm_mod, monkeypatch):
    """D-106-3: 先 429 后 200 -> 退避一次即成功，不丢这一批."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    resp_429 = _mock_resp(429)
    resp_ok = _mock_resp(200, payload={
        "choices": [{"message": {"content": '{"analyses": [{"id": 1, "score": 80}]}'}}]
    })

    with patch("requests.post", side_effect=[resp_429, resp_ok]) as mock_post:
        out = llm_mod._call_openai("p", "m", "https://api.example.com/v1", "k")

    assert out == {"analyses": [{"id": 1, "score": 80}]}
    assert mock_post.call_count == 2
