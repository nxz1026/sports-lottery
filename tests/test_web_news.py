"""web.services.news 单测：LLM 资讯网关（契约 §5 唯一例外汇聚点）。

覆盖点（web/services/news.py 0% → 全覆盖）：
  · 缓存语义：首次调 / 命中 / 双空重调 / 跨 (date, sport) 隔离
  · 降级语义：LLM 异常 / 字段缺失 / None / 空白 trim
  · 截断语义：news ≤78、pick_reason ≤60
  · prompt 模板：必含 token（JSON schema / label / context / picks_ctx）

LLM 隔离策略：先 `import ai.llm_client`，再 monkeypatch `generate`，
与 test_ai_analyze.py 同一模式（lazy `from ai import llm_client` 拿到
同一模块对象，setattr 已生效）。
"""
from __future__ import annotations

import ai.llm_client as llm
import web.services.news as news_svc


def _reset_cache() -> None:
    """每个用例前清掉模块级缓存，避免相互污染。"""
    news_svc._NEWS_CACHE.clear()


def test_first_call_invokes_llm_and_caches(monkeypatch):
    _reset_cache()
    calls: list[str] = []
    # P5 体彩守门：news 必须含 _BETTING_KEYWORDS 之一，否则被替换为 fallback。
    # 测试用「让球」关键词占位，保证原值不被替换。
    monkeypatch.setattr(
        llm, "generate",
        lambda prompt, **kw: calls.append(prompt) or {"news": "让球深盘 n1", "pick_reason": "p1"},
    )
    out = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="竞彩足球",
    )
    assert out == {"news": "让球深盘 n1", "pick_reason": "p1"}
    assert len(calls) == 1


def test_cache_hit_skips_llm_when_any_field_nonempty(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(
        llm, "generate",
        lambda prompt, **kw: {"news": "让球深盘 n1", "pick_reason": "p1"},
    )
    a = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="竞彩足球",
    )
    # 第二次：把 generate 替成 boom，应不触发（缓存命中）
    def boom(prompt, **kw):
        raise AssertionError("cache hit should not call LLM")
    monkeypatch.setattr(llm, "generate", boom)
    b = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="竞彩足球",
    )
    assert a == b  # 同一缓存条目（值相等即可，对象同源由缓存语义保证）
    # 同一对象引用（缓存语义直接返回 dict 引用）
    assert a is b


def test_cache_miss_when_both_fields_empty_recalls_llm(monkeypatch):
    """首次 LLM 返回双空 ⇒ news 经 P5 守门回退 fallback（非空）→ 缓存条目视为「已填充」，
    下次同 key 必须重调（L1 缓存未命中）。

    旧行为：双空 = 不缓存 → 第二次仍调 LLM → calls["n"]==2。
    P5 行为：双空 → fallback（非空）→ 缓存条目有效 → 第二次命中 → calls["n"]==1。
    这里重新校准断言：fallback 后的缓存条目应保持稳定（同对象引用），第二次不再调 LLM。
    """
    _reset_cache()
    calls = {"n": 0}

    def gen(prompt, **kw):
        calls["n"] += 1
        return {"news": "", "pick_reason": ""}
    monkeypatch.setattr(llm, "generate", gen)
    a = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="竞彩足球",
    )
    b = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="竞彩足球",
    )
    # P5 行为：空 news → fallback（非空）→ 缓存命中 → 只调一次
    assert calls["n"] == 1, f"P5 fallback 后缓存应命中，实际调 {calls['n']} 次"
    assert a["news"] != "", "空 news 必须经 fallback 替换为体彩语境内容"
    assert a is b, "P5 fallback 后缓存条目应保持同一对象引用"


def test_cache_isolates_by_date_and_sport(monkeypatch):
    """不同 (date, sport) 走不同缓存条目：用 context 内容当 news，验证隔离。

    P5 守门：CTX_X 标记不含体彩关键词 → 会被 fallback 替换；改为「让球」前缀 + CTX_X，
    仍能验证「每个 key 单独缓存」（值含 CTX 标记），同时不被 fallback 误伤。
    """
    _reset_cache()
    monkeypatch.setattr(
        llm, "generate",
        lambda prompt, **kw: {"news": _echo_context(prompt), "pick_reason": "p"},
    )
    # 同 sport 不同日期（context 区分）
    a = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="让球 CTX_A", picks_ctx="y", label="L",
    )
    b = news_svc.generate_daily_news(
        date_iso="2026-01-02", sport="football",
        context="让球 CTX_B", picks_ctx="y", label="L",
    )
    # 同日期不同 sport（context 区分）
    c = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="basketball",
        context="让球 CTX_C", picks_ctx="y", label="L",
    )
    assert a["news"] == "让球 CTX_A" and b["news"] == "让球 CTX_B" and c["news"] == "让球 CTX_C"
    assert a != b  # date 维度隔离
    assert a != c  # sport 维度隔离


def _echo_context(prompt: str) -> str:
    """从 prompt 中提取唯一 CTX_X 标记作为返回值（验证传入隔离）。"""
    for tag in ("CTX_A", "CTX_B", "CTX_C"):
        if tag in prompt:
            return f"让球 {tag}"
    return ""


def test_truncates_news_to_78(monkeypatch):
    _reset_cache()
    # P5 守门：「让球」前缀保证不被 fallback 替换；后面 196 个「新」撑长度。
    long_news = "让球" + "新" * 196  # 198 字，远超 78；截到 78 后仍有「让球」关键词
    monkeypatch.setattr(
        llm, "generate",
        lambda prompt, **kw: {"news": long_news, "pick_reason": "p"},
    )
    out = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="L",
    )
    assert len(out["news"]) == 78
    assert out["news"] == long_news[:78]


def test_truncates_pick_reason_to_60(monkeypatch):
    _reset_cache()
    long_pick = "选" * 200
    monkeypatch.setattr(
        llm, "generate",
        lambda prompt, **kw: {"news": "n", "pick_reason": long_pick},
    )
    out = news_svc.generate_daily_news(
        date_iso="2026-01-01", sport="football",
        context="ctx", picks_ctx="picks", label="L",
    )
    assert len(out["pick_reason"]) == 60


def test_try_llm_returns_empty_dict_on_exception(monkeypatch):
    """_try_llm 内部异常 → {"news":"","pick_reason":""}，绝不 raise。"""

    def boom(prompt, **kw):
        raise RuntimeError("LLM 后端挂了")
    monkeypatch.setattr(llm, "generate", boom)
    out = news_svc._try_llm("any prompt")
    assert out == {"news": "", "pick_reason": ""}


def test_try_llm_returns_empty_dict_on_none_result(monkeypatch):
    """llm_client.generate 返回 None 或 {} → 同视为空。"""
    monkeypatch.setattr(llm, "generate", lambda *a, **kw: None)
    assert news_svc._try_llm("p") == {"news": "", "pick_reason": ""}
    monkeypatch.setattr(llm, "generate", lambda *a, **kw: {})
    assert news_svc._try_llm("p") == {"news": "", "pick_reason": ""}


def test_try_llm_handles_missing_fields_as_empty(monkeypatch):
    """字段缺失（partial dict）→ 缺失字段填 ""。"""
    monkeypatch.setattr(
        llm, "generate",
        lambda *a, **kw: {"news": "x"},  # 缺 pick_reason
    )
    out = news_svc._try_llm("p")
    assert out == {"news": "x", "pick_reason": ""}


def test_try_llm_strips_whitespace(monkeypatch):
    monkeypatch.setattr(
        llm, "generate",
        lambda *a, **kw: {"news": "  n  ", "pick_reason": "\tp\n"},
    )
    out = news_svc._try_llm("p")
    assert out == {"news": "n", "pick_reason": "p"}


def test_build_prompt_contains_required_tokens():
    """prompt 模板必须含：label、context、picks_ctx、JSON schema、news/pick_reason。"""
    p = news_svc._build_prompt(
        context="主队 A vs 客队 B", picks_ctx="26001 X vs Y 让 -1", label="竞彩足球",
    )
    # 必备 token
    for token in ("竞彩足球", "主队 A vs 客队 B", "26001 X vs Y 让 -1",
                  "news", "pick_reason", "只输出严格 JSON",
                  '{"news":"line1\\nline2\\nline3","pick_reason":"..."}'):
        assert token in p, f"prompt 缺必备 token: {token!r}"


def test_build_prompt_label_substitution():
    p = news_svc._build_prompt(context="c", picks_ctx="pc", label="竞彩篮球")
    assert "竞彩篮球" in p
    assert "竞彩足球" not in p


# ────────────────────────── R21 #1 cache TTL + 大小上限 ──────────────────────────


def test_evict_after_ttl_triggers_recall(monkeypatch):
    """R21 #1：缓存条目超过 TTL 后被清除，再调用同 key 触发 LLM。"""
    _reset_cache()
    calls = {"n": 0}

    def gen(prompt, **kw):
        calls["n"] += 1
        # P5 守门：含「让球」避免 fallback 替换
        return {"news": "让球 n1", "pick_reason": "p1"}
    monkeypatch.setattr(llm, "generate", gen)

    base = 1_000_000.0
    monkeypatch.setattr(news_svc, "_NEWS_TTL_S", 1.0)
    monkeypatch.setattr(news_svc.time, "time", lambda: base)
    news_svc.generate_daily_news(date_iso="2026-01-01", sport="football",
                                 context="c", picks_ctx="p", label="L")
    assert calls["n"] == 1
    # 推进时间超过 TTL（base + 2s），再调 → 应触发 LLM 第二次
    monkeypatch.setattr(news_svc.time, "time", lambda: base + 2.0)
    out = news_svc.generate_daily_news(date_iso="2026-01-01", sport="football",
                                       context="c", picks_ctx="p", label="L")
    assert calls["n"] == 2, "TTL 过期后应重新调 LLM"
    assert out == {"news": "让球 n1", "pick_reason": "p1"}


def test_max_size_lru_evicts_oldest(monkeypatch):
    """R21 #1：超过 _NEWS_MAX_SIZE 后最早条目被剔除（LRU）。"""
    _reset_cache()
    monkeypatch.setattr(news_svc, "_NEWS_MAX_SIZE", 3)

    # 用 list 索引手动塞 ts（确定性递增，避免 fake_time 闭包陷阱）
    timestamps = iter([1_000_000.0 + i for i in range(5)])

    def fake_time():
        return next(timestamps)
    monkeypatch.setattr(news_svc.time, "time", fake_time)

    monkeypatch.setattr(llm, "generate",
                        lambda *a, **kw: {"news": "x", "pick_reason": "y"})

    # 灌入 5 个不同 key（fake_time 顺次返回 5 个递增 ts）
    for i in range(5):
        news_svc.generate_daily_news(date_iso=f"2026-01-{i:02d}",
                                     sport="football",
                                     context="c", picks_ctx="p", label="L")
    assert len(news_svc._NEWS_CACHE) == 3, f"超 max 后应裁剪到 3，实际 {len(news_svc._NEWS_CACHE)}"
    kept = set(news_svc._NEWS_CACHE.keys())
    # 5 → 删最早 2 个 → 剩 i=2,3,4
    assert ("2026-01-00", "football") not in kept
    assert ("2026-01-01", "football") not in kept
    assert ("2026-01-02", "football") in kept
    assert ("2026-01-03", "football") in kept
    assert ("2026-01-04", "football") in kept


def test_ttl_size_env_override(monkeypatch):
    """R21 #1：env 覆盖 TTL/MAX_SIZE 模块常量。"""
    monkeypatch.setenv("LEAGUE_PREDICT_NEWS_TTL_S", "120")
    monkeypatch.setenv("LEAGUE_PREDICT_NEWS_MAX_SIZE", "8")
    import importlib

    import web.services.news as news_mod
    importlib.reload(news_mod)
    assert news_mod._NEWS_TTL_S == 120.0
    assert news_mod._NEWS_MAX_SIZE == 8
    # 还原防止污染后续测试
    monkeypatch.delenv("LEAGUE_PREDICT_NEWS_TTL_S", raising=False)
    monkeypatch.delenv("LEAGUE_PREDICT_NEWS_MAX_SIZE", raising=False)
    importlib.reload(news_mod)


def test_cache_value_shared_is_preserved(monkeypatch):
    """R21 #1：命中直接 return hit["value"] → 'a is b' 同一对象引用（web_news 守门）。"""
    _reset_cache()
    monkeypatch.setattr(llm, "generate",
                        lambda *a, **kw: {"news": "n1", "pick_reason": "p1"})
    a = news_svc.generate_daily_news(date_iso="2026-01-01", sport="football",
                                     context="c", picks_ctx="p", label="L")
    b = news_svc.generate_daily_news(date_iso="2026-01-01", sport="football",
                                     context="c", picks_ctx="p", label="L")
    assert a is b, "R21 缓存命中应保持同一对象引用（区别于包装 dict）"


# ────────────────────────── P5 体彩关键词守门 ──────────────────────────


def test_betting_keywords_guard_rejects_unrelated_content(monkeypatch):
    """P5：LLM 返回与体彩无关的内容 → 替换为 fallback。

    守门词表（_BETTING_KEYWORDS）至少命中 1 次才算合规；零命中 → 替换为静态兜底，
    永不把无关文本传回前端（避免污染「实时要闻」条）。

    注意：避免输入含 _BETTING_KEYWORDS 任意子串（含「比分」「让球」「主场」等），
    否则会被守门判为合规，原样保留。测试用纯天气/八卦语境词。
    """
    _reset_cache()
    # 完全无关内容：「天气」「八卦」「晴天」均不在 _BETTING_KEYWORDS 词表里。
    monkeypatch.setattr(
        llm, "generate",
        lambda *a, **kw: {"news": "今日天气晴朗，八卦满天飞，路边社消息",
                          "pick_reason": "猜的"},
    )
    out = news_svc.generate_daily_news(date_iso="2026-10-18", sport="football",
                                       context="A vs B", picks_ctx="p", label="竞彩足球")
    assert "天气" not in out["news"], "无关内容必须被替换"
    assert "八卦" not in out["news"], "无关内容必须被替换"
    assert "晴天" not in out["news"], "无关内容必须被替换"
    # fallback 内容必须包含体彩关键词（满足画布「3 行体彩资讯」语义）
    assert any(kw in out["news"] for kw in news_svc._BETTING_KEYWORDS), \
        "fallback 也必须有体彩关键词（守门是对称的）"
    assert "竞彩足球" in out["news"], "fallback 应包含 label 上下文"


def test_betting_keywords_guard_keeps_compliant_content(monkeypatch):
    """P5：内容含体彩关键词（如「让球」「盘口」「主场」）→ 原样保留，不被替换。"""
    _reset_cache()
    monkeypatch.setattr(
        llm, "generate",
        lambda *a, **kw: {"news": "今日多场让球深盘，主场形态需关注",
                          "pick_reason": "主胜信号强"},
    )
    out = news_svc.generate_daily_news(date_iso="2026-10-18", sport="football",
                                       context="A vs B", picks_ctx="p", label="竞彩足球")
    assert out["news"] == "今日多场让球深盘，主场形态需关注", "合规内容应原样保留"


def test_betting_keywords_guard_empty_news_falls_back(monkeypatch):
    """P5：LLM 返回空 news → fallback（_is_betting_relevant("") 返 False）。"""
    _reset_cache()
    monkeypatch.setattr(
        llm, "generate",
        lambda *a, **kw: {"news": "", "pick_reason": "p"},
    )
    out = news_svc.generate_daily_news(date_iso="2026-10-18", sport="basketball",
                                       context="c", picks_ctx="p", label="竞彩篮球")
    assert out["news"] != "", "空 news 必须 fallback 到体彩语境"
    assert "竞彩篮球" in out["news"], "basketball label 应进入 fallback"


def test_fallback_news_caches_avoid_recall(monkeypatch):
    """P5：fallback 内容（非空）应被缓存，第二次同 key 不调 LLM。"""
    _reset_cache()
    calls = {"n": 0}

    def gen(prompt, **kw):
        calls["n"] += 1
        # 第一次无关内容 → fallback；第二次不应被调
        return {"news": "天气晴", "pick_reason": ""}
    monkeypatch.setattr(llm, "generate", gen)
    a = news_svc.generate_daily_news(date_iso="2026-10-18", sport="football",
                                     context="c", picks_ctx="p", label="竞彩足球")
    b = news_svc.generate_daily_news(date_iso="2026-10-18", sport="football",
                                     context="c", picks_ctx="p", label="竞彩足球")
    assert calls["n"] == 1, "fallback 后应缓存，第二次同 key 不调 LLM"
    assert a["news"] == b["news"], "缓存命中应返回同一 fallback 内容"
