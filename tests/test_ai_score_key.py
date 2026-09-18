"""AI 分数稳定主键（英文原名）+ id 配对 + 漏答重试 的回归测试。

背景（实测，非推断）：AI 富化曾按 `prediction["match"]`（中文译名）做精确字符串
配对。agnes-3.0-flash 会把中文译名"纠正"成别的队 —— 21 条里只有 14 条精确照抄：

    送入 '西班牙人 vs 埃尔切'      → 回 '西班牙人 vs 阿根廷'
    送入 '勒芒 vs 洛里昂'          → 回 '洛森 vs 洛里昂'
    送入 '法兰克福 vs 弗赖堡'      → 回 '法兰克福 vs 德累斯顿'
    送入 '维戈塞尔塔 vs 桑坦德竞技' → 回 '维格塞尔塔 vs 桑坦德竞技'

后果是这些条目**静默丢分**（一次富化 68 条只写回 48 条，日志只报 "wrote 48"）。
换成英文原名后精确照抄 5/5；改用不透明整数 id 后同样 5/5 且与语言无关。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# 仓库约定：需要 core.* 的测试自行把 scripts 加入 sys.path。
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ── score_key ────────────────────────────────────────────────────────────────

def test_score_key_format_is_league_and_english_pair():
    from ai.feedback_loop import score_key

    assert score_key("epl", "Brentford FC", "Chelsea FC") == "epl|Brentford FC|Chelsea FC"


def test_score_key_strips_whitespace():
    from ai.feedback_loop import score_key

    assert score_key(" epl ", " Brentford FC ", " Chelsea FC ") == "epl|Brentford FC|Chelsea FC"


@pytest.mark.parametrize("home,away", [("", "Chelsea FC"), ("Brentford FC", ""), ("", "")])
def test_score_key_returns_empty_when_english_missing(home, away):
    """英文名缺失必须返回空串，让调用方回退旧中文名键，而不是造出 `epl||Chelsea FC`。"""
    from ai.feedback_loop import score_key

    assert score_key("epl", home, away) == ""


def test_score_key_is_language_independent():
    """同一场比赛无论中文显示名怎么变，键都相同 —— 这正是旧中文键做不到的。"""
    from ai.feedback_loop import score_key

    stable = score_key("laliga", "RCD Espanyol", "Elche CF")
    assert stable == score_key("laliga", "RCD Espanyol", "Elche CF")
    # 旧键会因 LLM 把 '埃尔切' 写成 '阿根廷' 而分裂成两个键
    assert "埃尔切" not in stable and "阿根廷" not in stable


# ── id 配对 ──────────────────────────────────────────────────────────────────

def _load_bp(monkeypatch):
    """取得 batch_pipeline 模块。

    注意：**不要** delitem(sys.modules, "ai.batch_pipeline") 后重新 import ——
    那会造出第二个模块对象，使别处按字符串路径的 monkeypatch
    （如 tests/web/test_m6_ai_enrich.py 的 `"ai.batch_pipeline.analyse_batch"`）
    打到另一个对象上而静默失效，实测会把 m6 的两个 main() 测试打成真实 LLM 调用。
    直接 import 即可，monkeypatch.setattr 自身会还原。
    """
    import ai.batch_pipeline as bp

    return bp


def test_pair_batch_prefers_id_over_name(monkeypatch):
    """LLM 回填 id → 按 id 配对，即使 match 被写错也不丢条目。"""
    bp = _load_bp(monkeypatch)
    batch = [
        {"_batch_id": 1, "name": "西班牙人 vs 埃尔切"},
        {"_batch_id": 2, "name": "塞维利亚 vs 巴塞罗那"},
    ]
    # 模拟真实的"纠正"行为：match 全写错，但 id 是对的
    analyses = [
        {"id": 2, "match": "塞维利亚 vs 巴塞尔那", "score": 70, "summary": "乙"},
        {"id": 1, "match": "西班牙人 vs 阿根廷", "score": 30, "summary": "甲"},
    ]
    paired = bp._pair_batch(analyses, batch)
    assert paired[1]["score"] == 30
    assert paired[2]["score"] == 70


def test_pair_batch_accepts_stringified_id(monkeypatch):
    """LLM 把整数回成字符串也要能配上。"""
    bp = _load_bp(monkeypatch)
    batch = [{"_batch_id": 7, "name": "A vs B"}]
    paired = bp._pair_batch([{"id": "7", "score": 55}], batch)
    assert paired[7]["score"] == 55


def test_pair_batch_rejects_bool_id(monkeypatch):
    """True 是 int 的子类，不能被当成 id=1。

    带一个（错的）match 键以排除位置回退的干扰，确保断言只针对 id 逻辑。
    """
    bp = _load_bp(monkeypatch)
    batch = [{"_batch_id": 1, "name": "A vs B"}]
    assert bp._pair_batch([{"id": True, "match": "X vs Y", "score": 55}], batch) == {}


def test_pair_batch_ignores_id_outside_batch(monkeypatch):
    """LLM 编造不存在的 id 时不能凭空配对（带 match 键排除位置回退）。"""
    bp = _load_bp(monkeypatch)
    batch = [{"_batch_id": 1, "name": "A vs B"}]
    assert bp._pair_batch([{"id": 99, "match": "X vs Y", "score": 55}], batch) == {}


def test_pair_batch_falls_back_to_name_without_id(monkeypatch):
    """旧 prompt 返回（只有 match、无 id）仍要能配对 —— 兼容既有 LLM 行为。"""
    bp = _load_bp(monkeypatch)
    batch = [{"_batch_id": 1, "name": "A vs B"}, {"_batch_id": 2, "name": "B vs C"}]
    paired = bp._pair_batch([{"match": "B vs C", "score": 90}], batch)
    assert paired[2]["score"] == 90


def test_pair_batch_position_fallback_can_be_disabled(monkeypatch):
    """重试批次是子集：禁止位置回退，否则会把第 1 条的分析扣到第 2 条头上。"""
    bp = _load_bp(monkeypatch)
    batch = [{"_batch_id": 2, "name": "B vs C"}]
    analyses = [{"score": 88, "summary": "其实这是别的条目的"}]
    assert bp._pair_batch(analyses, batch, allow_position=False) == {}
    assert bp._pair_batch(analyses, batch, allow_position=True)[2]["score"] == 88


# ── 漏答重试 ─────────────────────────────────────────────────────────────────

def test_analyse_batch_retries_missing_items_by_id(monkeypatch):
    """首轮漏答 → 重试并补上；重试只针对缺失条目。"""
    bp = _load_bp(monkeypatch)
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            return {"analyses": [{"id": 1, "score": 80, "summary": "甲", "notes": ""}]}
        return {"analyses": [{"id": 2, "score": 60, "summary": "乙", "notes": ""}]}

    monkeypatch.setattr(bp, "generate", fake_generate)
    out = bp.analyse_batch(
        [{"name": "A vs B"}, {"name": "B vs C"}],
        config={"ai": {"max_retries": 1}},
    )
    assert len(calls) == 2
    assert [o.get("ai_score") for o in out] == [80, 60]


def test_analyse_batch_does_not_fabricate_score_after_retries_exhausted(monkeypatch):
    """重试用尽仍缺 → 条目原样保留、无 ai_ 字段（绝不伪造 50 分）。"""
    bp = _load_bp(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {"analyses": []})
    out = bp.analyse_batch([{"name": "A vs B"}], config={"ai": {"max_retries": 2}})
    assert "ai_score" not in out[0]
    assert "ai_summary" not in out[0]


def test_analyse_batch_retry_count_is_bounded(monkeypatch):
    """max_retries=0 → 只调用一次，不做重试。"""
    bp = _load_bp(monkeypatch)
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(prompt)
        return {"analyses": []}

    monkeypatch.setattr(bp, "generate", fake_generate)
    bp.analyse_batch([{"name": "A vs B"}], config={"ai": {"max_retries": 0}})
    assert len(calls) == 1


def test_analyse_batch_strips_internal_batch_id(monkeypatch):
    """内部 _batch_id 不得泄漏到产物里。"""
    bp = _load_bp(monkeypatch)
    monkeypatch.setattr(bp, "generate", lambda prompt, **k: {
        "analyses": [{"id": 1, "score": 70, "summary": "甲", "notes": ""}]
    })
    out = bp.analyse_batch([{"name": "A vs B"}], config={})
    assert "_batch_id" not in out[0]


def test_build_prompt_asks_for_id_not_name(monkeypatch):
    """prompt 必须要求回填整数 id，并明确不要回填队名。"""
    bp = _load_bp(monkeypatch)
    prompt = bp._build_prompt([{"_batch_id": 3, "name": "西班牙人 vs 埃尔切"}], "", "", {})
    assert '"id": 3' in prompt
    assert "不要回填队名" in prompt
    assert "简体中文" in prompt  # 既有中文指令契约（test_m6）不被破坏


# ── 落盘键 ───────────────────────────────────────────────────────────────────

def test_save_ai_scores_keys_by_english_pair(monkeypatch, tmp_path):
    """有英文名 → 用稳定主键落盘，并保留中文名供显示。"""
    import ai.feedback_loop as fl

    target = tmp_path / "ai_scores.json"
    monkeypatch.setattr(fl, "AI_SCORES_FILE", target)
    fl.save_ai_scores([{
        "name": "布伦特福德 vs 切尔西",
        "home_en": "Brentford FC", "away_en": "Chelsea FC",
        "league": "epl", "ai_score": 70, "ai_summary": "甲", "ai_notes": "",
    }])
    import json

    data = json.loads(target.read_text(encoding="utf-8"))
    assert list(data) == ["epl|Brentford FC|Chelsea FC"]
    entry = data["epl|Brentford FC|Chelsea FC"]
    assert entry["ai_score"] == 70
    assert entry["name"] == "布伦特福德 vs 切尔西"
    assert entry["league"] == "epl"


def test_save_ai_scores_falls_back_to_chinese_key_without_english(monkeypatch, tmp_path):
    """无英文名的历史条目仍按中文名落盘（不能丢）。"""
    import json

    import ai.feedback_loop as fl

    target = tmp_path / "ai_scores.json"
    monkeypatch.setattr(fl, "AI_SCORES_FILE", target)
    fl.save_ai_scores([{
        "name": "某队 vs 某队", "league": "csl",
        "ai_score": 60, "ai_summary": "乙", "ai_notes": "",
    }])
    data = json.loads(target.read_text(encoding="utf-8"))
    assert list(data) == ["某队 vs 某队"]


def test_save_ai_scores_still_skips_unscored_items(monkeypatch, tmp_path):
    """未评分条目一律不落盘（不伪造分数）—— 既有契约。"""
    import ai.feedback_loop as fl

    target = tmp_path / "ai_scores.json"
    monkeypatch.setattr(fl, "AI_SCORES_FILE", target)
    fl.save_ai_scores([{
        "name": "A vs B", "home_en": "A", "away_en": "B", "league": "epl",
    }])
    assert not target.exists() or target.read_text(encoding="utf-8").strip() in ("", "{}")


# ── 读侧：预测调整 ───────────────────────────────────────────────────────────

def test_adjust_prediction_finds_by_english_key():
    from ai.feedback_loop import adjust_prediction, score_key

    pred = {
        "match": "布伦特福德 vs 切尔西",
        "home_en": "Brentford FC", "away_en": "Chelsea FC", "league": "epl",
        "confidence_score": 0.5,
    }
    adj = {score_key("epl", "Brentford FC", "Chelsea FC"): {"ai_score": 100, "league": "epl"}}
    out = adjust_prediction(dict(pred), adj)
    assert out["ai_adjusted"] is True
    assert out["ai_score_used"] == 100
    # factor = 0.7 + 0.3*(100/100) = 1.0 → 0.5 * 1.0
    assert out["confidence_score"] == 0.5


def test_adjust_prediction_factor_range_is_one_directional():
    """**已记录缺陷**：docstring 声称 factor 0.7–1.3、"ai_score=100 → boost 30%"，
    但实现是 `0.7 + 0.3*(ai_score/100)`，取值 0.7–1.0。

    后果：AI 反馈回路**只能降低信心、永远无法提高**（score=100 时 factor=1.0，恰好
    不动）。这与"AI 认可该预测就加分"的设计意图相反。此测试固化**当前实际行为**，
    以免日后无声漂移；修实现还是修文档需要单独决策（改实现会改变所有预测的信心值）。
    """
    from ai.feedback_loop import adjust_prediction

    def factor_for(score):
        pred = {"match": "A vs B", "home_en": "A", "away_en": "B",
                "league": "epl", "confidence_score": 1.0}
        out = adjust_prediction(pred, {"epl|A|B": {"ai_score": score}})
        return out["ai_adjustment_factor"]

    assert factor_for(0) == 0.7
    assert factor_for(50) == 0.85   # 中性点
    assert factor_for(100) == 1.0   # 上限就是 1.0，不是 1.3
    assert factor_for(100) < 1.3


def test_adjust_prediction_falls_back_to_chinese_key():
    """旧的中文名键数据仍要能生效。"""
    from ai.feedback_loop import adjust_prediction

    pred = {
        "match": "布伦特福德 vs 切尔西",
        "home_en": "Brentford FC", "away_en": "Chelsea FC", "league": "epl",
        "confidence_score": 0.5,
    }
    out = adjust_prediction(dict(pred), {"布伦特福德 vs 切尔西": {"ai_score": 100}})
    assert out["ai_adjusted"] is True


def test_adjust_prediction_ignores_unrelated_key():
    from ai.feedback_loop import adjust_prediction

    pred = {"match": "A vs B", "home_en": "A", "away_en": "B", "league": "epl", "confidence_score": 0.5}
    out = adjust_prediction(dict(pred), {"epl|X|Y": {"ai_score": 100}})
    assert "ai_adjusted" not in out


# ── 读侧：web 日报 ───────────────────────────────────────────────────────────

def test_web_lookup_prefers_english_key():
    from web.services.ai import _lookup_score, _score_key

    scores = {_score_key("epl", "Brentford FC", "Chelsea FC"): {"ai_score": 70, "league": "epl"}}
    got = _lookup_score(
        {"match": "布伦特福德 vs 切尔西", "home_en": "Brentford FC",
         "away_en": "Chelsea FC", "league": "epl"},
        scores,
    )
    assert got["ai_score"] == 70


def test_web_lookup_falls_back_to_chinese_key():
    from web.services.ai import _lookup_score

    got = _lookup_score(
        {"match": "布伦特福德 vs 切尔西", "home_en": "Brentford FC",
         "away_en": "Chelsea FC", "league": "epl"},
        {"布伦特福德 vs 切尔西": {"ai_score": 42, "league": "epl"}},
    )
    assert got["ai_score"] == 42


def test_web_lookup_returns_none_when_absent():
    from web.services.ai import _lookup_score

    assert _lookup_score({"match": "A vs B", "home_en": "A", "away_en": "B", "league": "epl"}, {}) is None


def test_web_score_key_matches_engine_score_key():
    """web 侧是**故意复制**的实现（契约 §5 禁止 import ai/），必须与引擎侧一致。"""
    from ai.feedback_loop import score_key as engine_key
    from web.services.ai import _score_key as web_key

    for args in [("epl", "Brentford FC", "Chelsea FC"), ("laliga", "RCD Espanyol", "Elche CF"),
                 ("epl", "", "Chelsea FC"), ("epl", " A ", " B ")]:
        assert web_key(*args) == engine_key(*args), args


def test_ai_details_shows_display_name_not_raw_key(monkeypatch):
    """明细页必须显示中文名，不能把稳定主键 `nba|Detroit Pistons|...` 当比赛名。

    这是把键从中文名换成英文主键后**必然引入**的显示回归：ai_details 原先把字典键
    直接当 match 返回。
    """
    from web.services import ai as ai_svc

    monkeypatch.setattr(ai_svc, "_load_ai_scores", lambda: {
        "nba|Detroit Pistons|Boston Celtics": {
            "ai_score": 90, "ai_summary": "甲", "ai_notes": "",
            "league": "nba", "name": "底特律活塞 vs 波士顿凯尔特人",
        },
        "某历史队 vs 另一队": {"ai_score": 40, "ai_summary": "乙", "league": "csl"},
    })
    items = {i["match"] for i in ai_svc.ai_details()["items"]}
    assert "底特律活塞 vs 波士顿凯尔特人" in items
    assert "nba|Detroit Pistons|Boston Celtics" not in items
    assert "某历史队 vs 另一队" in items  # 历史条目无 name 字段 → 回退到键（键即中文名）


# ── 端到端：predict 产物必须带英文原名 ───────────────────────────────────────

def test_predict_carries_english_names():
    """predict.py 必须把 home_en/away_en 写进预测产物 —— 否则下游主键全空。"""
    src = (REPO_ROOT / "scripts" / "predict.py").read_text(encoding="utf-8")
    assert 'pred["home_en"]' in src
    assert 'pred["away_en"]' in src


def test_enrich_items_carry_english_names():
    """enrich 构造的富化项必须带英文原名，否则落盘只能退回中文键。"""
    src = (REPO_ROOT / "web" / "enrich.py").read_text(encoding="utf-8")
    assert '"home_en": pred.get("home_en", "")' in src
    assert '"away_en": pred.get("away_en", "")' in src
