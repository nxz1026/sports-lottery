from __future__ import annotations

import json
import os
import time
from pathlib import Path

from web.services import ai_analyze as aa


def _fake_generate_factory(text_by_prompt_prefix):
    """生成一个 fake generate(prompt)：根据 prompt 前缀返回 {'text': ...}。"""
    def fake(prompt, **kwargs):
        for prefix, txt in text_by_prompt_prefix.items():
            if prompt.startswith(prefix):
                return {"text": txt}
        return {"text": "默认解读"}
    return fake


def test_analyze_writes_three_classes(monkeypatch, tmp_path):
    # monkeypatch OUTPUT_DIR
    monkeypatch.setattr(aa, "OUTPUT_DIR", tmp_path)
    # monkeypatch predictions/fixtures/lottery 读取
    monkeypatch.setattr(aa, "_read_predictions", lambda: {
        "epl": {"data": {"predictions": [
            {"home": "A", "away": "B", "direction": "A 胜", "predicted_score": "2-1",
             "stars": "3-star",
             "reasoning_factors": {"home_ml_true_prob": 0.55, "draw_true_prob": 0.25,
                                   "away_ml_true_prob": 0.20}},
            {"home": "C", "away": "D", "direction": "平", "predicted_score": "1-1",
             "stars": "1-star",
             "reasoning_factors": {"home_ml_true_prob": 0.40, "draw_true_prob": 0.30,
                                   "away_ml_true_prob": 0.30}},
        ]}},
    })
    monkeypatch.setattr(aa, "_read_fixtures", lambda: [])
    monkeypatch.setattr(aa, "_read_lottery", lambda per_type=5: [
        {"game_num": "85", "game_name": "超级大乐透", "issue_no": "24001",
         "numbers_raw": "01 02 03 04 05 06 07"},
    ])

    # monkeypatch LLM client generate
    import ai.llm_client as llm
    fake = _fake_generate_factory({
        "你是体彩数据分析师": "A 胜路径清晰，主胜概率 0.55。",
        "你是体彩胆材分析师": "组合叙事：高信心 A 胜路径清晰。",
        "你是数字彩复盘分析师": "超级大乐透 开奖连号 01-05。",
    })
    monkeypatch.setattr(llm, "generate", fake)

    out = aa.analyze("2026-09-19")
    assert out["date"] == "2026-09-19"
    cls = out["classes"]
    assert cls["per_match"], "逐场解读应有内容"
    assert isinstance(cls["banker"], str) and cls["banker"], "胆材叙事应有内容"
    assert cls["lottery"], "开奖复盘应有内容"
    assert (tmp_path / "2026-09-19.json").exists()


def test_analyze_per_class_failure_does_not_pollute_others(monkeypatch, tmp_path):
    monkeypatch.setattr(aa, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(aa, "_read_predictions", lambda: {
        "epl": {"data": {"predictions": [
            {"home": "A", "away": "B", "direction": "A 胜", "stars": "3-star",
             "predicted_score": "2-1",
             "reasoning_factors": {"home_ml_true_prob": 0.55, "draw_true_prob": 0.25,
                                   "away_ml_true_prob": 0.20}},
        ]}},
    })
    monkeypatch.setattr(aa, "_read_fixtures", lambda: [])
    monkeypatch.setattr(aa, "_read_lottery", lambda per_type=5: [])

    import ai.llm_client as llm
    def boom(prompt, **kw):
        # 只让"你是体彩数据分析师"（per_match）抛错
        if prompt.startswith("你是体彩数据分析师"):
            raise RuntimeError("LLM down")
        return {"text": "其他类 OK"}
    monkeypatch.setattr(llm, "generate", boom)

    out = aa.analyze("2026-09-19")
    # 隔离：per_match 内部有兜底（LLM 失败仍写 fallback 条目），banker/lottery 不受影响
    cls = out["classes"]
    assert cls["per_match"], "per_match 失败时内部兜底应至少一条"
    assert "其他类 OK" in cls["banker"], "banker 不应受 per_match 失败影响"
    assert cls["lottery"] == []          # lottery 无数据 → 空（独立）


def test_analyze_returns_payload_even_with_empty_data(monkeypatch, tmp_path):
    monkeypatch.setattr(aa, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(aa, "_read_predictions", lambda: {})
    monkeypatch.setattr(aa, "_read_fixtures", lambda: [])
    monkeypatch.setattr(aa, "_read_lottery", lambda per_type=5: [])
    out = aa.analyze("2026-09-19")
    assert out["classes"]["per_match"] == []
    assert "无 ≥ 3★" in out["classes"]["banker"]
    assert out["classes"]["lottery"] == []
    assert (tmp_path / "2026-09-19.json").exists()


def test_cli_main_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(aa, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(aa, "_read_predictions", lambda: {})
    monkeypatch.setattr(aa, "_read_fixtures", lambda: [])
    monkeypatch.setattr(aa, "_read_lottery", lambda per_type=5: [])
    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", lambda p, **k: {"text": "x"})

    import sys
    old_argv = sys.argv
    sys.argv = ["ai_analyze", "2026-09-19"]
    try:
        rc = aa.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    assert (tmp_path / "2026-09-19.json").exists()
