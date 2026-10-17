"""R44-6: ai/batch_pipeline.py::_score_batch 5 tests direct + D-61 lock.

LLM 评分批处理 helper 0 直测; analyse_batch 间接调用, 但 helper 自身契约
(按 _batch_id 整数配对 / 漏答原样保留 / score clamp [0, 100] / min_score 过滤)
未锁。已通过 analyse_batch 调用集成测试, 但 _score_batch 真函数未直测。

helper 含四条契约 (D-61):
- 按 _batch_id 整数配对 (确定性, 与语言无关)
- 漏答 / 整批失败的 item 原样保留 (不伪造分数), 必 print warning
- score clamp 到 [0, 100] (max(0, min(100, int(...))))
- score < min_score 过滤掉 (不返回)
- 配对 fallback: allow_position=True 时按位置回退 (仅 first attempt)

5 tests (HIGH):
- happy: 所有 item 在 analyses -> 全 enriched with ai_score/summary/notes
- item missing in analyses -> 原样返回, 无 ai 字段
- score clamp parametrize (score=-5/150/0) -> [-5→0, 150→100, 0→0]
- min_score filter: score=50 + min_score=70 -> 不返回
- LLM 全失败 (generate 抛) -> 所有 item 原样返回 (无 ai 字段)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def batch_mod(monkeypatch):
    """加载 ai/batch_pipeline.py + stub generate (LLM)。"""
    spec = importlib.util.spec_from_file_location(
        "batch_pipeline_score_test",
        REPO_ROOT / "ai" / "batch_pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_generate(monkeypatch, batch_mod, analyses_or_none):
    """Stub ai.llm_client.generate 返受控 analyses。

    analyses_or_none: list[dict] -> 返 {"analyses": [...]};
    None -> 返 {} (模拟 LLM 完全失败但 gen 不抛)
    """
    if analyses_or_none is None:
        def fake_gen(prompt, model=None, rate_limit=None):
            return {}  # 模拟 LLM 死但 gen 不抛
    else:
        def fake_gen(prompt, model=None, rate_limit=None):
            return {"analyses": analyses_or_none}
    monkeypatch.setattr(batch_mod, "generate", fake_gen)


# ──────────────────────── D-61 四契约 ────────────────────────


def test_score_batch_d61_happy_all_enriched(batch_mod, monkeypatch):
    """R44-6 D-61: 所有 item 在 analyses -> 全部 enriched with ai fields。

    锁定按 _batch_id 整数配对的 happy path; 重试不影响首次全量。
    """
    _stub_generate(monkeypatch, batch_mod, [
        {"id": 1, "score": 90, "summary": "great", "notes": "n1"},
        {"id": 2, "score": 75, "summary": "ok", "notes": "n2"},
        {"id": 3, "score": 60, "summary": "weak", "notes": "n3"},
    ])
    batch = [
        {"match": "A vs B"}, {"match": "C vs D"}, {"match": "E vs F"},
    ]
    out = batch_mod._score_batch(
        batch, context="ctx", preference_prompt="", config={},
        model="gemini", rate_limit=0.0, min_score=0,
        max_retries=1, batch_no=1, batch_total=1)
    assert len(out) == 3
    assert out[0]["ai_score"] == 90
    assert out[0]["ai_summary"] == "great"
    assert out[1]["ai_score"] == 75
    assert out[2]["ai_score"] == 60
    # 原 _batch_id 已剥离
    assert all("_batch_id" not in item for item in out)


def test_score_batch_d61_missing_analysis_keeps_item_as_is(batch_mod, monkeypatch):
    """R44-6 D-61: 某 item id 不在 analyses -> 原样返回 (不伪造分数)。

    锁 silent fallback (不编数): 漏答 must 原样保留, 否则"AI 富化"覆盖。
    """
    _stub_generate(monkeypatch, batch_mod, [
        {"id": 1, "score": 90, "summary": "great"},
        # 故意缺 id=2 (模拟 LLM 漏答), 还有 id=3
        {"id": 3, "score": 70, "summary": "ok"},
    ])
    batch = [
        {"match": "A vs B"}, {"match": "C vs D"}, {"match": "E vs F"},
    ]
    out = batch_mod._score_batch(
        batch, context="", preference_prompt="", config={},
        model="gemini", rate_limit=0.0, min_score=0,
        max_retries=0, batch_no=1, batch_total=1)
    # 3 条全返, 但 item2 无 ai 字段
    assert len(out) == 3
    assert out[0].get("ai_score") == 90
    assert out[1].get("ai_score") is None  # 漏答 -> 无 ai_score
    assert out[1]["match"] == "C vs D"
    assert "ai_summary" not in out[1]
    assert out[2].get("ai_score") == 70


@pytest.mark.parametrize("raw_score,expected", [
    (-5, 0),       # 负 -> clamp 到 0
    (150, 100),    # 超 100 -> clamp 到 100
    (0, 0),        # 边界 0
    (100, 100),    # 边界 100
    (85, 85),      # 中间值透传
])
def test_score_batch_d61_score_clamped_to_zero_hundred(
    batch_mod, monkeypatch, raw_score, expected,
):
    """R44-6 D-61: score clamp 到 [0, 100]。max(0, min(100, int(...)))。

    锁定: 越界输入不会污染输出; LLM 返回 150 应 clamp 到 100。
    """
    _stub_generate(monkeypatch, batch_mod, [
        {"id": 1, "score": raw_score, "summary": "x"},
    ])
    batch = [{"match": "A vs B"}]
    out = batch_mod._score_batch(
        batch, context="", preference_prompt="", config={},
        model="gemini", rate_limit=0.0, min_score=0,
        max_retries=0, batch_no=1, batch_total=1)
    assert len(out) == 1
    assert out[0]["ai_score"] == expected


def test_score_batch_d61_min_score_filter_excludes_low(batch_mod, monkeypatch):
    """R44-6 D-61: min_score=70 -> score=50 的 item 被过滤掉 (不出现在 out)。

    锁: min_score 阈值过滤, 防止低分富化项进预测池。
    """
    _stub_generate(monkeypatch, batch_mod, [
        {"id": 1, "score": 90, "summary": "good"},
        {"id": 2, "score": 50, "summary": "weak"},  # < min_score=70
    ])
    batch = [{"match": "A vs B"}, {"match": "C vs D"}]
    out = batch_mod._score_batch(
        batch, context="", preference_prompt="", config={},
        model="gemini", rate_limit=0.0, min_score=70,
        max_retries=0, batch_no=1, batch_total=1)
    # 仅 item1 (score 90) 通过, item2 (score 50) 过滤
    assert len(out) == 1
    assert out[0]["match"] == "A vs B"
    assert out[0]["ai_score"] == 90


def test_score_batch_d61_generate_returns_empty_dict_keeps_all_as_is(batch_mod, monkeypatch, capfd):
    """R44-6 D-61: LLM generate() 返 {} (模拟 LLM 死但 gen 不抛) -> item 原样返回, 必 warning。

    锁: LLM 返回空 dict / 列表时不抛穿; retry max_retries 后仍有 pending
    -> print warning 提示运营 (silent-fallback 与 D-45 ai_scores 同家族)。
    """
    # generate 返空 dict (无 analyses key), generate 不抛
    _stub_generate(monkeypatch, batch_mod, None)

    batch = [
        {"match": "A vs B"}, {"match": "C vs D"}, {"match": "E vs F"},
    ]
    out = batch_mod._score_batch(
        batch, context="", preference_prompt="", config={},
        model="gemini", rate_limit=0.0, min_score=70,
        max_retries=2, batch_no=1, batch_total=1)
    # 全失败后 -> 3 个 item 全原样返回, 无 ai_*
    assert len(out) == 3
    assert all("ai_score" not in item for item in out)
    # 必 print warning 提示运营 (captured stdout 含 LLM 漏答)
    captured = capfd.readouterr()
    assert "未评分" in captured.out or "未返回" in captured.out
