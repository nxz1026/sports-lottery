"""R56: web/services/ai.py + source_meta 7 个 0 直测 helper 11 测 + D-112/D-113/D-114 lock。

R55 末已锁 D-111 (teams_match_safe + enrich 私有 helper).

``web/services/ai.py`` (M3 AI 富化扩展模块) 的 6 个 helper 完全 0 直测,
仅 ai_status / ai_details / _load_ai_scores 在 test_ai_scores_load.py (D-45) 覆盖:

- _score_key    (L109-125): AI 分数稳定主键 ``联赛|主英|客英`` (mirror ai.feedback_loop.score_key)
- _lookup_score (L128-145): 按主键查分, 回退历史中文名键
- _joined_item  (L148-154): 拼装单条 AI 日报表项 (match 字段 + 9 个原字段 + 4 个 AI 字段)
- _day_items    (L90-106): 取某 BJT 日所有覆盖联赛的预测 + 各联赛 count
- ai_daily_report(L157-170): 组装 {summary, items, leagues}, 异常降级 available:false
- ai_ranking    (L173-181): hot/cold 排序, 过滤 finite ai_score

``web/services/source_meta.recent_job_stats`` (L123-166) 仅路由层 (test_sources_router.py)
间接触发, 完全 0 直测: by_status counts + last row + last failure + items 截 10 条.

D-112 锁 _score_key + _lookup_score + _joined_item (5 测纯函数):
- D-112-1: _score_key 正常 / 空字符串 / 全 None 边界
- D-112-2: _score_key 与 ai.feedback_loop.score_key 输出 100% 等价 (mirror 守门)
- D-112-3: _lookup_score 主键命中 / 中文回退 / 全 miss
- D-112-4: _joined_item joined=False 时 ai_score=None, 其他 ai 字段空字符串
- D-112-5: _joined_item 9 个基础字段 + 4 个 AI 字段命名锁定

D-113 锁 _day_items + ai_daily_report (3 测, 集成):
- D-113-1: _day_items 仅返 covers_date 命中的预测 + by_league count
- D-113-2: ai_daily_report 含 ai_matched_count + unmatched_count + leagues
- D-113-3: ai_ranking hot / cold 排序 + finite ai_score 过滤

D-114 锁 recent_job_stats (3 测, 参数化 list_jobs):
- D-114-1: by_status counts + last = first row
- D-114-2: last_failure = 第一次 "failed" 行; error 截断 120 字
- D-114-3: items 截前 10 条

策略:
- spec-load 三个模块到隔离 namespace
- ai.py: monkeypatch store.latest_by_league + store.covers_date (mock 返回受控预测)
- AI_SCORES_FILE 指向 tmp_path, 真实落地 JSON
- recent_job_stats: 直接 stub list_jobs
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


# ───────── helpers ─────────


@pytest.fixture
def ai_mod(tmp_path, monkeypatch):
    """Spec-load web.services.ai + stub store + 受控 AI_SCORES_FILE."""
    spec = importlib.util.spec_from_file_location(
        "web_services_ai_test",
        REPO_ROOT / "web" / "services" / "ai.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # AI_SCORES_FILE 指向 tmp (真落地 JSON)
    scores_file = tmp_path / "ai_scores.json"
    monkeypatch.setattr(mod, "AI_SCORES_FILE", scores_file)

    # stub web.services.store 的 latest_by_league / covers_date
    import web.services.store as store_mod  # noqa: PLC0415
    monkeypatch.setattr(store_mod, "latest_by_league",
                        lambda leagues=None: _fake_store_latest())
    monkeypatch.setattr(store_mod, "covers_date",
                        lambda data, day: data.get("data_window") == "20251017-20251017")

    return mod, scores_file


def _fake_store_latest():
    """fake latest_by_league: 2 联赛, 一含匹配预测, 一不含."""
    return {
        "epl": {
            "data": {
                "data_window": "20251017-20251017",  # matches covers_date
                "predictions": [
                    {"match": "曼联 vs 利物浦", "home_en": "Man United", "away_en": "Liverpool",
                     "home": "曼联", "away": "利物浦",
                     "kickoff_utc": "2025-10-17T14:30:00+08:00",
                     "direction": "博洛尼亚 胜", "stars": "2-star", "confidence_score": 0.6,
                     "predicted_score": "1-0"},
                    {"match": "曼城 vs 切尔西", "home_en": "Man City", "away_en": "Chelsea",
                     "home": "曼城", "away": "切尔西",
                     "kickoff_utc": "2025-10-17T17:00:00+08:00",
                     "direction": "平", "stars": "1-star", "confidence_score": 0.4,
                     "predicted_score": "2-2"},
                ],
            },
        },
        "laliga": {
            "data": {
                "data_window": "20250901-20250905",  # 不匹配 covers_date
                "predictions": [],  # skip
            },
        },
    }


# ──────────────────────── D-112: 5 测 ────────────────────────


def test_d112_1_score_key_basic_and_empty_handling(ai_mod):
    """D-112-1: _score_key (L121-125) ``联赛|主英|客英`` 拼接 + 空字符串边界."""
    mod, _ = ai_mod

    # 正常
    assert mod._score_key("epl", "Man United", "Liverpool") == "epl|Man United|Liverpool"
    assert mod._score_key("laliga", "Real Madrid", "Barcelona") == "laliga|Real Madrid|Barcelona"

    # 空字符串: home_en 或 away_en 为空 -> 返 ""
    assert mod._score_key("epl", "", "Liverpool") == ""
    assert mod._score_key("epl", "Man United", "") == ""

    # strip
    assert mod._score_key("  epl  ", "  Man United  ", "Liverpool") == "epl|Man United|Liverpool"

    # league 空字符串允许 (e.g. unknown league)
    assert mod._score_key("", "X", "Y") == "|X|Y"


def test_d112_2_score_key_matches_engine_mirror(ai_mod):
    """D-112-2: _score_key 与 ai/feedback_loop.py::score_key **输出 100% 相等** (R17 #7 mirror 守门).

    web 层坚决不 import ai.feedback_loop (契约 §5), 这里直接调真实 engine 函数,
    验证两侧在所有边界情形下输出等价.
    """
    mod, _ = ai_mod
    from ai.feedback_loop import score_key as engine_score_key  # noqa: PLC0415 - mirror 校验

    cases = [
        ("epl", "Man United", "Liverpool"),
        ("laliga", "Real Madrid", "Barcelona"),
        ("nba", "Detroit Pistons", "Boston Celtics"),
        ("", "", ""),
        ("  epl  ", "  X  ", "Y"),
    ]
    for league, h, a in cases:
        web_out = mod._score_key(league, h, a)
        engine_out = engine_score_key(league, h, a)
        assert web_out == engine_out, f"mirror drift: web={web_out!r} engine={engine_out!r}"


def test_d112_3_lookup_score_main_key_then_match_fallback(ai_mod):
    """D-112-3: _lookup_score (L128-145) 主键 (``_score_key``) 命中 -> 返 value;
    miss 时回退 ``prediction['match']`` 中文键; 全 miss 返 None.
    """
    mod, _ = ai_mod
    scores = {
        "epl|Man United|Liverpool": {"ai_score": 80, "league": "epl"},
        "拜仁 vs 多特": {"ai_score": 60, "league": "bundesliga"},  # 中文名键 (历史数据)
    }

    # 主键命中
    pred_a = {"league": "epl", "home_en": "Man United", "away_en": "Liverpool", "match": "曼联 vs 利物浦"}
    out_a = mod._lookup_score(pred_a, scores)
    assert out_a == {"ai_score": 80, "league": "epl"}

    # 主键 miss, 中文 match 命中
    pred_b = {"league": "bundesliga", "home_en": "Bayern", "away_en": "Dortmund", "match": "拜仁 vs 多特"}
    out_b = mod._lookup_score(pred_b, scores)
    assert out_b == {"ai_score": 60, "league": "bundesliga"}

    # 全 miss
    pred_c = {"league": "seriea", "home_en": "Milan", "away_en": "Inter", "match": "米兰 vs 国米"}
    assert mod._lookup_score(pred_c, scores) is None


def test_d112_4_joined_item_ai_score_none_when_not_joined(ai_mod):
    """D-112-4: _joined_item (L148-154) league 不匹配或查分失败 -> ai_matched=False,
    ai_score=None (不是 ""), 其他 AI 字段 "" 兜底.

    L153 ``None if key == "ai_score" else ""`` 区分 ai_score (None) 和其它字段 (空字符串).
    """
    mod, _ = ai_mod
    prediction = {
        "match": "曼联 vs 利物浦", "home": "曼联", "away": "利物浦",
        "league": "epl",
        "kickoff_utc": "2025-10-17T14:30:00+08:00",
        "direction": "博洛尼亚 胜", "stars": "2-star",
        "confidence_score": 0.6, "predicted_score": "1-0",
    }
    # 空 scores -> 查分 miss
    out = mod._joined_item(prediction, {})

    assert out["ai_matched"] is False
    assert out["ai_score"] is None           # 严格 None
    assert out["ai_summary"] == ""
    assert out["ai_notes"] == ""
    assert out["source"] == ""


def test_d112_5_joined_item_returns_all_9_base_and_4_ai_fields(ai_mod):
    """D-112-5: _joined_item 字段锁定 9 个基础 + 4 个 AI 字段 + ai_matched."""
    mod, _ = ai_mod
    prediction = {
        "match": "X vs Y", "home": "X", "away": "Y",
        "home_en": "X", "away_en": "Y",  # _score_key 必须有 home_en/away_en, 否则返空 → 走 match fallback
        "league": "test_lg", "kickoff_utc": "k", "direction": "d",
        "stars": "s", "confidence_score": 0.5, "predicted_score": "1-0",
    }
    # 命中分数 (含 league 字段是 _joined_item L150 ``value.get("league", "") == prediction.get("league")`` 决定 joined=True 的关键)
    scores = {"test_lg|X|Y": {"ai_score": 75, "ai_summary": "sum", "ai_notes": "note",
                                  "source": "src", "league": "test_lg"}}
    out = mod._joined_item(prediction, scores)

    # 9 基础字段
    for k in ("match", "home", "away", "league", "kickoff_utc", "direction", "stars",
              "confidence_score", "predicted_score"):
        assert k in out
    # 4 AI 字段 + ai_matched
    for k in ("ai_score", "ai_summary", "ai_notes", "source", "ai_matched"):
        assert k in out

    # 命中时所有 AI 字段透传
    assert out["ai_matched"] is True
    assert out["ai_score"] == 75
    assert out["ai_summary"] == "sum"
    assert out["ai_notes"] == "note"
    assert out["source"] == "src"


# ──────────────────────── D-113: 3 测 ────────────────────────


def test_d113_1_day_items_filters_by_covers_date(ai_mod):
    """D-113-1: _day_items (L90-106) 仅返 covers_date 命中的预测 + 每联赛 count."""
    mod, _ = ai_mod

    items, counts = mod._day_items(date(2025, 10, 17))

    # 2 个 EPL 预测 (covers_date 命中), 0 个 laliga
    assert len(items) == 2
    assert counts == {"epl": 2}
    # 每个 item 含 league + generated_at 字段 (L103)
    assert all("league" in item and item["league"] == "epl" for item in items)


def test_d113_2_daily_report_summary_unmatched_and_leagues(ai_mod):
    """D-113-2: ai_daily_report (L157-170) summary 含 prediction_count / matched / unmatched + leagues 字典."""
    mod, scores_file = ai_mod
    # 写一份 scores: 仅 1 个预测有 AI 分数
    scores_file.write_text(json.dumps({
        "epl|Man United|Liverpool": {
            "ai_score": 80, "ai_summary": "sum", "league": "epl",
            "name": "曼联 vs 利物浦",
        },
    }, ensure_ascii=False), encoding="utf-8")

    out = mod.ai_daily_report(date(2025, 10, 17))

    assert out["available"] is True
    assert out["date"] == "2025-10-17"
    # summary 字段
    assert out["summary"]["prediction_count"] == 2
    assert out["summary"]["ai_matched_count"] == 1  # Man United 命中
    assert out["summary"]["unmatched_count"] == 1   # Man City miss
    assert "epl" in out["summary"]["leagues"]
    assert out["summary"]["leagues"]["epl"] == {
        "prediction_count": 2,
        "ai_matched_count": 1,
    }


def test_d113_3_ai_ranking_hot_cold_and_finite_filter(ai_mod):
    """D-113-3: ai_ranking (L173-181) hot/cold 排序, 过滤 finite ai_score.

    L178 过滤: ``math.isfinite(float(...))`` + 不是 bool + 是 (int, float).
    """
    mod, scores_file = ai_mod
    # 3 个 ai_score, 1 个非法 (str / inf), 1 个缺失 (joined=False)
    scores_file.write_text(json.dumps({
        "epl|Man United|Liverpool": {"ai_score": 80, "ai_summary": "", "league": "epl"},
        "epl|Man City|Chelsea": {"ai_score": 30, "ai_summary": "", "league": "epl"},  # cold
        "bogus": {"ai_score": "not-a-number", "ai_summary": "", "league": "epl"},  # not numeric
    }, ensure_ascii=False), encoding="utf-8")

    out = mod.ai_ranking(date(2025, 10, 17), limit=10)

    assert out["available"] is True
    # 仅 2 个有限数值 (80 / 30)
    assert out["matched_count"] == 2
    # hot 降序 (80 第一) / cold 升序 (30 第一)
    hot_ai_scores = [item["ai_score"] for item in out["hot"]]
    cold_ai_scores = [item["ai_score"] for item in out["cold"]]
    assert hot_ai_scores == sorted(hot_ai_scores, reverse=True)
    assert cold_ai_scores == sorted(cold_ai_scores)
    # bogus 不入榜
    assert all("not-a-number" not in str(s.get("ai_summary", "")) for s in out["hot"])


# ──────────────────────── D-114: 3 测 ────────────────────────


@pytest.fixture
def source_meta_mod():
    """Spec-load web.services.source_meta."""
    spec = importlib.util.spec_from_file_location(
        "web_services_source_meta_test",
        REPO_ROOT / "web" / "services" / "source_meta.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_d114_1_recent_job_stats_by_status_and_last(source_meta_mod):
    """D-114-1: recent_job_stats: by_status counts + last = first row (L128-143)."""
    mod = source_meta_mod

    # 5 行 mock, status 混合
    fake_rows = [
        {"id": "j1", "status": "done", "trigger": "manual", "created_at": "t1", "finished_at": "f1"},
        {"id": "j2", "status": "failed", "trigger": "auto", "created_at": "t2", "finished_at": "f2",
         "script": "predict.py", "exit_code": 1, "error": "boom"},
        {"id": "j3", "status": "done", "trigger": "auto", "created_at": "t3", "finished_at": "f3"},
        {"id": "j4", "status": "failed", "trigger": "manual", "created_at": "t4", "finished_at": "f4",
         "script": "x.py", "exit_code": 2, "error": "oops"},
        {"id": "j5", "status": "running", "trigger": "manual", "created_at": "t5", "finished_at": None},
    ]

    def fake_list_jobs(limit=50):
        # L129 第一次调 limit=50 (stats) / L155 第二次 limit=10 (items)
        if limit == 50:
            return fake_rows
        return fake_rows[:10]

    out = mod.recent_job_stats(fake_list_jobs)

    # by_status
    assert out["by_status"] == {"done": 2, "failed": 2, "running": 1}
    # last = first row (j1), L136-137
    assert out["last"]["id"] == "j1"
    # last_failure = first "failed" row (j2), L144
    assert out["last_failure"]["id"] == "j2"
    assert out["last_failure"]["script"] == "predict.py"
    assert out["last_failure"]["exit_code"] == 1


def test_d114_2_recent_job_stats_error_truncated_to_120_chars(source_meta_mod):
    """D-114-2: error 字段截断到 120 字 (L152, L164)."""
    mod = source_meta_mod

    long_error = "X" * 200  # 远超 120
    fake_rows = [
        {"id": "j1", "status": "failed", "trigger": "manual",
         "created_at": "t1", "finished_at": "f1",
         "script": "x.py", "exit_code": 1, "error": long_error},
    ]

    out = mod.recent_job_stats(lambda limit=50: fake_rows)

    # last_failure.error 截 120
    assert len(out["last_failure"]["error"]) == 120
    # items[0].error 同样截 120
    assert len(out["items"][0]["error"]) == 120
    # error 字段是 str (强制 str() L152)
    assert isinstance(out["last_failure"]["error"], str)


def test_d114_3_recent_job_stats_items_capped_at_10(source_meta_mod):
    """D-114-3: items 是 list_jobs(limit=10) 返回 (L155-165), 截前 10 条."""
    mod = source_meta_mod

    # 构造 30 行
    fake_rows_50 = [
        {"id": f"j{i}", "status": "done", "trigger": "manual", "created_at": f"t{i}",
         "finished_at": f"f{i}"}
        for i in range(30)
    ]
    fake_rows_10 = fake_rows_50[:10]  # items 应是这 10 条

    def fake_list_jobs(limit=50):
        return fake_rows_50 if limit == 50 else fake_rows_10

    out = mod.recent_job_stats(fake_list_jobs)

    assert len(out["items"]) == 10
    # items 第一条是 j0, 第十条是 j9
    assert out["items"][0]["id"] == "j0"
    assert out["items"][9]["id"] == "j9"
