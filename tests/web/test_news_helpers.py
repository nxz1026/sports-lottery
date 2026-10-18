"""R54: web/services/news.py 4 helper + 1 主入口 10 测 + D-109/D-110 lock。

R53 已锁 web/services/store.py 7 private helpers (D-107/D-108). web/services/news.py
完全 0 直测 — 集成 test_web_news.py 测端点 / router, 但**模块内 4 个 helper
和 generate_daily_news 核心契约未直测**:

- _evict_expired_news (L35-52): TTL 剔除 + LRU 容量裁剪 (R21 #1)
- _build_prompt        (L55-77): 组装 LLM prompt (1:1 复制 jc.py daily_news)
- _try_llm             (L80-95): LLM 调用 + 异常兜底
- generate_daily_news  (L98-124): 主入口, 缓存 + LLM 调用 + 长度限制

D-109 锁 _evict_expired_news + _build_prompt (5 测纯函数):
- D-109-1: _evict_expired_news 过期条目剔除 (ts + TTL < now)
- D-109-2: _evict_expired_news 容量超 MAX 时按 ts 升序剔除最早
- D-109-3: _build_prompt 包含 label / context / picks_ctx 三个占位符
- D-109-4: _build_prompt 锁定关键中文字符串 (禁止编造约束)
- D-109-5: _build_prompt 输出闭合 JSON {"news":.., "pick_reason":..}

D-110 锁 _try_llm + generate_daily_news 缓存 (5 测, 拼装主流程):
- D-110-1: _try_llm 正常 LLM 返回 dict -> 返 news/pick_reason strip
- D-110-2: _try_llm LLM 抛异常 -> 返 {"news": "", "pick_reason": ""}
- D-110-3: generate_daily_news 同一 (date, sport) 二次调用返同一对象 (a is b)
- D-110-4: generate_daily_news news/pick_reason 长度截断 (78 / 60)
- D-110-5: generate_daily_news LLM 返空时不写入缓存 (双空不该占位)

策略:
- spec-load web.services.news 到隔离 namespace
- monkeypatch sys.modules["ai"].llm_client.generate 让 _try_llm 走受控路径
- 跨测隔离: 直接清空 spec-load 模块的 _NEWS_CACHE
"""
from __future__ import annotations

import importlib.util
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def news_mod(monkeypatch):
    """Spec-load web.services.news + stub ai.llm_client.generate."""
    spec = importlib.util.spec_from_file_location(
        "web_services_news_test",
        REPO_ROOT / "web" / "services" / "news.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 清空 _NEWS_CACHE (跨测隔离)
    mod._NEWS_CACHE.clear()

    # stub ai.llm_client.generate (受控返回)
    fake_generate = MagicMock(return_value={
        "news": "今日 5 场 +1 深盘需核合理\n主队近期 4 战 3 胜\n赛季保级压力突出",
        "pick_reason": "让球合理性 + 主场形态",
    })
    # 1) stub sys.modules["ai"] + sys.modules["ai.llm_client"]
    fake_ai = types.ModuleType("ai")
    fake_llm_client = types.ModuleType("ai.llm_client")
    fake_llm_client.generate = fake_generate
    monkeypatch.setitem(sys.modules, "ai", fake_ai)
    monkeypatch.setitem(sys.modules, "ai.llm_client", fake_llm_client)

    return mod, fake_generate


# ──────────────────────── D-109: 5 测 ────────────────────────


def test_d109_1_evict_expired_removes_old_entries(news_mod):
    """D-109-1: _evict_expired_news L42-45: 剔除过期 (now - ts > TTL) 的条目."""
    mod, _ = news_mod
    now = 1_000_000.0
    # 过期: ts + TTL < now 即 (now - ts) > TTL
    mod._NEWS_CACHE["old-1"] = {"value": {"news": "x", "pick_reason": "y"}, "ts": now - 200_000}  # 超过 86400 TTL
    mod._NEWS_CACHE["old-2"] = {"value": {"news": "", "pick_reason": "y"}, "ts": now - 100_000}  # 过期
    mod._NEWS_CACHE["fresh"] = {"value": {"news": "fresh", "pick_reason": ""}, "ts": now - 10}

    mod._evict_expired_news(now)

    # 过期条目已剔除, fresh 保留
    assert "old-1" not in mod._NEWS_CACHE
    assert "old-2" not in mod._NEWS_CACHE
    assert "fresh" in mod._NEWS_CACHE


def test_d109_2_evict_caps_max_size_by_oldest_first(news_mod):
    """D-109-2: _evict_expired_news L47-52: 容量超 MAX 时按 ts 升序剔除最早.

    注意: 必须让 ts 都在 TTL 内, 否则 L42-45 TTL 剔除分支先把全部条目 pop 掉,
    然后 L47 检查 ``len(_NEWS_CACHE) >= _NEWS_MAX_SIZE`` 永远是 False, 容量裁剪
    不触发. 修复: 用 ``ts = now - i`` 让 ts 距 now 均 < 86400 TTL.
    """
    mod, _ = news_mod
    now = 1_000_000.0
    # 临时改 MAX 为 3 便于测
    mod._NEWS_MAX_SIZE = 3
    # 注满 5 条, ts 都未过期 (< TTL=86400)
    for i in range(5):
        # ts 升序: 越后面的 ts 越大 (越近), 越不可能被剔除
        mod._NEWS_CACHE[f"k{i}"] = {
            "value": {"news": "x", "pick_reason": ""},
            "ts": now - (10 - i),  # k0=now-10, k4=now-6 都在 TTL 内
        }

    mod._evict_expired_news(now)

    # 保留 <= MAX-1 = 2 条, 应该是 ts 最大的 2 条: k3 (now-7), k4 (now-6)
    assert len(mod._NEWS_CACHE) <= 2
    assert "k3" in mod._NEWS_CACHE
    assert "k4" in mod._NEWS_CACHE
    assert "k0" not in mod._NEWS_CACHE
    assert "k1" not in mod._NEWS_CACHE


def test_d109_3_prompt_contains_all_three_placeholders(news_mod):
    """D-109-3: _build_prompt L57-77 输出含 context / picks_ctx / label 三块信息."""
    mod, _ = news_mod
    ctx = "今日 8 场在售比赛内容"
    picks = "精选候选前 2 条详情"
    label = "英超"

    out = mod._build_prompt(ctx, picks, label)

    assert "英超" in out
    assert ctx in out
    assert picks in out


def test_d109_4_prompt_locks_key_chinese_constraints(news_mod):
    """D-109-4: _build_prompt 锁定关键中文约束 (禁编造 / 长度限制 / 字段名)."""
    mod, _ = news_mod
    out = mod._build_prompt("ctx", "picks", "label")

    assert "禁止编造" in out or "不要假装" in out  # 禁编造约束
    assert "JSON" in out
    assert "news" in out
    assert "pick_reason" in out
    assert "≤78 字" in out or "<=78" in out or "≤78" in out  # news 上限
    assert "≤60" in out or "<=60" in out  # pick_reason 上限


def test_d109_5_prompt_outputs_closed_json_spec(news_mod):
    """D-109-5: _build_prompt 末尾严格 JSON 输出约束 {"news":.., "pick_reason":..}."""
    mod, _ = news_mod
    out = mod._build_prompt("ctx", "picks", "label")

    assert "只输出严格 JSON" in out
    assert '{"news"' in out
    assert '"pick_reason"' in out


# ──────────────────────── D-110: 5 测 ────────────────────────


def test_d110_1_try_llm_extracts_news_and_pick_reason(news_mod):
    """D-110-1: _try_llm 调 llm_client.generate 后 strip news/pick_reason 字段."""
    mod, _ = news_mod
    out = mod._try_llm("any prompt")

    assert out["news"] == "今日 5 场 +1 深盘需核合理\n主队近期 4 战 3 胜\n赛季保级压力突出"
    assert out["pick_reason"] == "让球合理性 + 主场形态"


def test_d110_2_try_llm_returns_empty_on_exception(news_mod):
    """D-110-2: _try_llm LLM 抛异常 -> 返 {"news": "", "pick_reason": ""}."""
    mod, fake_generate = news_mod
    fake_generate.side_effect = RuntimeError("LLM API down")

    out = mod._try_llm("p")

    assert out == {"news": "", "pick_reason": ""}


def test_d110_3_generate_daily_news_same_object_identity(news_mod):
    """D-110-3: generate_daily_news 同一 (date, sport) 二次调用返同一对象 (a is b, 锁定缓存契约).

    这是 test_web_news.py 守门的关键契约, 必须在单测层面也锁定.
    """
    mod, fake_generate = news_mod

    out_a = mod.generate_daily_news(
        date_iso="2025-10-17", sport="football",
        context="context", picks_ctx="picks", label="英超",
    )
    out_b = mod.generate_daily_news(
        date_iso="2025-10-17", sport="football",
        context="context-2", picks_ctx="picks-2", label="意甲",  # 即使参数变, 缓存命中
    )

    # 同一对象引用
    assert out_a is out_b
    # 第二次不会真打 LLM
    assert fake_generate.call_count == 1


def test_d110_4_generate_daily_news_truncates_to_length_limits(news_mod, monkeypatch):
    """D-110-4: generate_daily_news news <=78 / pick_reason <=60 长度截断 (L118-119)."""
    mod, fake_generate = news_mod

    long_news = "X" * 200  # 远超 78 上限
    long_pick = "Y" * 100  # 远超 60 上限
    fake_generate.return_value = {"news": long_news, "pick_reason": long_pick}

    out = mod.generate_daily_news(
        date_iso="2025-10-17", sport="football",
        context="c", picks_ctx="p", label="label",
    )

    assert len(out["news"]) == 78
    assert len(out["pick_reason"]) == 60


def test_d110_5_generate_daily_news_does_not_cache_all_empty(news_mod):
    """D-110-5: LLM 返 {"news": "", "pick_reason": ""} 时不写入缓存 (双空不该占位, L122-123)."""
    mod, fake_generate = news_mod
    fake_generate.return_value = {"news": "", "pick_reason": ""}

    mod.generate_daily_news(
        date_iso="2025-10-17", sport="football",
        context="c", picks_ctx="p", label="label",
    )

    # 缓存不写入
    assert ("2025-10-17", "football") not in mod._NEWS_CACHE
