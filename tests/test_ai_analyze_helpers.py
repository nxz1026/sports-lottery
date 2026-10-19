"""R25 #3: web/services/ai_analyze.py 内部 helper 直测。

explore 实测：现有 tests/test_ai_analyze.py 7 测全部 monkeypatch 三个
`_read_*` 替开，导致以下路径从未单测：
  · _read_predictions / _read_fixtures / _read_lottery 三处
    try/except → return {} / [] 兜底
  · _class_per_match 在 LLM 返回 {"text": ""} 时走 fallback 文案
  · _class_lottery 按 game_num 字典去重（最新 issue 优先）
  · main() 传非法日期 → 返回 2
  · today_bjt() BJT 时区 (+8h)
  · output_path() 简单格式化

本轮补 8 测补这些隐性回归点（不需要跑 LLM，用 monkeypatch 拦截 generate）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from web.services import ai_analyze as aa

# ──────────────────────── _read_* 兜底 ────────────────────────


def test_read_predictions_returns_empty_on_store_failure(monkeypatch, caplog):
    """R25 #3：web.services.store.latest_by_league raise → _read_predictions 返 {} + warning。"""
    from web.services import store as ws
    monkeypatch.setattr(ws, "latest_by_league", lambda: (_ for _ in ()).throw(RuntimeError("模拟 store 挂")))

    with caplog.at_level("WARNING"):
        out = aa._read_predictions()
    assert out == {}
    assert any("read_predictions failed" in r.message for r in caplog.records)


def test_read_fixtures_returns_empty_on_jc_view_failure(monkeypatch, caplog):
    """R25 #3：from store import jc_view → jc_view.fixtures_on raise → 返 []。"""
    # 注入 fake store / store.jc_view 模块到 sys.modules（运行时 scripts/store
    # 不是包，裸 'store' 不在 sys.path）
    from types import SimpleNamespace
    fake_jc_view = SimpleNamespace(
        fixtures_on=lambda d: (_ for _ in ()).throw(RuntimeError("模拟 jc_view 挂")),
    )
    monkeypatch.setitem(sys.modules, "store", SimpleNamespace(jc_view=fake_jc_view))
    monkeypatch.setitem(sys.modules, "store.jc_view", fake_jc_view)

    with caplog.at_level("WARNING"):
        out = aa._read_fixtures()
    assert out == []
    assert any("read_fixtures failed" in r.message for r in caplog.records)


def test_read_lottery_returns_empty_on_jc_view_failure(monkeypatch, caplog):
    """R25 #3：jc_view.lottery_draws raise → 返 []（per_type 默认 5）。"""
    from types import SimpleNamespace
    fake_jc_view = SimpleNamespace(
        lottery_draws=lambda n: (_ for _ in ()).throw(RuntimeError("模拟 lottery 挂")),
    )
    monkeypatch.setitem(sys.modules, "store", SimpleNamespace(jc_view=fake_jc_view))
    monkeypatch.setitem(sys.modules, "store.jc_view", fake_jc_view)

    with caplog.at_level("WARNING"):
        out = aa._read_lottery()
    assert out == []
    assert any("read_lottery failed" in r.message for r in caplog.records)


# ──────────────────────── _class_per_match LLM 兜底 ────────────────────────


def test_class_per_match_fallback_when_llm_returns_empty(monkeypatch):
    """R25 #3：LLM 返回 {"text": ""} → 走 fallback 文案，含「LLM 解读暂不可用」。"""
    predictions = {
        "epl": {"data": {"predictions": [
            {"home": "Liverpool", "away": "Arsenal", "direction": "A 胜",
             "predicted_score": "2-1", "stars": "3-star",
             "reasoning_factors": {"home_ml_true_prob": 0.55, "draw_true_prob": 0.25,
                                   "away_ml_true_prob": 0.20}},
        ]}},
    }
    import ai.llm_client as llm
    # LLM 返空 → raise → fallback
    monkeypatch.setattr(llm, "generate", lambda prompt: {"text": ""})

    out = aa._class_per_match(predictions, fixtures=[])
    assert len(out) == 1
    assert out[0]["league"] == "epl"
    assert "LLM 解读暂不可用" in out[0]["insight"]
    assert "Liverpool vs Arsenal" in out[0]["match"]
    assert "A 胜" in out[0]["direction"]
    assert out[0]["score"] == "2-1"


def test_class_per_match_empty_predictions_returns_empty_list(monkeypatch):
    """R25 #3：predictions={} → 直接返 []，不调 LLM（性能保护）。"""
    calls = []

    def must_not_run(*args, **kwargs):
        calls.append(args)

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", must_not_run)

    out = aa._class_per_match({}, fixtures=[])
    assert out == []
    assert calls == []  # LLM 未被调


# ──────────────────────── _class_lottery game_num 去重 ────────────────────────


def test_class_lottery_dedupes_by_game_num(monkeypatch):
    """R25 #3：同 game_num 多条 → 仅保留第一条（最新期）。"""
    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", lambda prompt: {"text": "复盘文本。"})

    lottery = [
        {"game_num": "85", "game_name": "超级大乐透", "issue_no": "24001",
         "numbers_raw": "01 02 03 04 05 06 07"},
        # 同 game_num 第二条 issue 不同 → 应被去重丢弃
        {"game_num": "85", "game_name": "超级大乐透", "issue_no": "24002",
         "numbers_raw": "10 11 12 13 14 15 16"},
        # 不同 game_num 应保留
        {"game_num": "08", "game_name": "双色球", "issue_no": "24005",
         "numbers_raw": "01 02 03 04 05 06"},
    ]

    out = aa._class_lottery(lottery)
    game_nums = [r["game_num"] for r in out]
    assert game_nums == ["85", "08"]
    # 保留的是第一条（issue_no=24001）
    assert out[0]["issue_no"] == "24001"
    assert out[0]["numbers_raw"] == "01 02 03 04 05 06 07"


# ──────────────────────── main() 非法日期 ────────────────────────


def test_main_returns_2_on_invalid_date(monkeypatch, capsys):
    """R25 #3：sys.argv 给非 ISO 日期 → 返回 2，stderr 含 '非法日期'。"""
    monkeypatch.setattr(sys, "argv", ["ai_analyze.py", "2026-13-99"])
    # analyze 不应该被执行
    analyzed = []

    def fake_analyze(date_str):
        analyzed.append(date_str)
        return {}

    monkeypatch.setattr(aa, "analyze", fake_analyze)

    rc = aa.main()
    assert rc == 2
    assert analyzed == []
    captured = capsys.readouterr()
    assert "非法日期" in captured.err


# ──────────────────────── today_bjt / output_path 简单 unit ────────────────────────


def test_today_bjt_uses_plus8_offset(monkeypatch):
    """R25 #3：today_bjt() 走 +8h 时区；UTC 凌晨 ≤7:59 视为 BJT 仍同日。"""
    from datetime import datetime, timedelta, timezone
    # 模拟 UTC 2026-10-15T01:00:00Z = BJT 2026-10-15T09:00:00 → 同日
    fake_utc = datetime(2026, 10, 15, 1, 0, 0, tzinfo=timezone.utc)
    fake_bjt = fake_utc.astimezone(timezone(timedelta(hours=8)))
    monkeypatch.setattr(aa, "datetime", type("DT", (), {
        "now": staticmethod(lambda tz=None: fake_utc if tz is timezone.utc else fake_bjt),
    }))

    # 触发 _today_bjt 与 today_bjt（两者都存在；_today_bjt = today_bjt 别名）
    result = aa.today_bjt()
    from datetime import date as _date
    assert result == _date(2026, 10, 15)


def test_output_path_formats_correctly(monkeypatch, tmp_path):
    """R25 #3：output_path(date_str) → OUTPUT_DIR/{date_str}.json。"""
    monkeypatch.setattr(aa, "OUTPUT_DIR", tmp_path)
    p = aa.output_path("2026-10-15")
    assert p == (tmp_path / "2026-10-15.json").resolve() or p == tmp_path / "2026-10-15.json"


# ──────────────────────── _class_banker 高信心筛选 + LLM fallback (R27 #2) ─


def test_class_banker_empty_predictions_skips_llm(monkeypatch):
    """R27 #2：无 ≥3★ 候选 → 返「今日无 ≥ 3★」文案，不调 LLM（性能保护）。"""
    calls = []

    def must_not_run(*args, **kwargs):
        calls.append(args)

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", must_not_run)

    # predictions={}
    out = aa._class_banker({}, fixtures=[])
    assert "今日无 ≥ 3★" in out
    assert calls == []

    # predictions 有但 stars 全 < 3
    out2 = aa._class_banker({"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": "1-star"},
        {"home": "C", "away": "D", "direction": "D 胜", "stars": "2-star"},
    ]}}}, fixtures=[])
    assert "今日无 ≥ 3★" in out2
    assert calls == []


def test_class_banker_filters_under_three_stars(monkeypatch):
    """R27 #2：2★/3★/4-star 混存 → 仅 ≥3★ 入 candidates（4-star parse → 4）。"""
    seen_prompts = []

    def fake_generate(prompt):
        seen_prompts.append(prompt)
        return {"text": "高信心组合：A 胜 + D 胜"}

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", fake_generate)

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A1", "away": "B1", "direction": "A1 胜", "stars": "1-star"},   # 跳过
        {"home": "A2", "away": "B2", "direction": "A2 胜", "stars": "2-star"},   # 跳过
        {"home": "Liverpool", "away": "Arsenal", "direction": "L 胜", "stars": "3-star"},  # 入
        {"home": "Chelsea", "away": "Spurs", "direction": "Ch 胜", "stars": "4-star"},     # 入
        {"home": "V", "away": "W"},  # stars=None → 0 → 跳过
    ]}}}
    out = aa._class_banker(predictions, fixtures=[])
    assert "高信心组合" in out
    # 确认 prompt 中只含 ≥3★
    prompt = seen_prompts[0] if seen_prompts else ""
    assert "A1" not in prompt or "1-star" not in prompt.split("A1")[0].split("A2")[0]
    assert "Liverpool" in prompt
    assert "Chelsea" in prompt


def test_class_banker_llm_empty_falls_back(monkeypatch):
    """R27 #2：候选 ≥3★ 但 LLM 返 {"text": ""} → 走 fallback 文案 + 列表前 5 条。"""
    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", lambda prompt: {"text": ""})

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": "3-star"},
        {"home": "C", "away": "D", "direction": "D 胜", "stars": "4-star"},
    ]}}}
    out = aa._class_banker(predictions, fixtures=[])
    assert "LLM 解读暂不可用" in out
    assert "A vs B" in out
    assert "C vs D" in out
    assert "3★" in out or "4★" in out


def test_class_banker_llm_truncates_to_300(monkeypatch):
    """R27 #2：LLM 返 500 字 → 截断到 text[:300]。"""
    long_text = "一" * 500

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", lambda prompt: {"text": long_text})

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": "3-star"},
    ]}}}
    out = aa._class_banker(predictions, fixtures=[])
    assert len(out) == 300


def test_class_banker_llm_exception_falls_back(monkeypatch, caplog):
    """R27 #2：LLM 抛 ConnectionError → fallback 文案 + logger.warning。"""
    def fake_generate(prompt):
        raise ConnectionError("LLM 接口 502")

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", fake_generate)

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": "3-star"},
    ]}}}
    with caplog.at_level("WARNING"):
        out = aa._class_banker(predictions, fixtures=[])
    assert "LLM 解读暂不可用" in out
    assert "A vs B" in out
    assert any("banker LLM failed" in r.message for r in caplog.records)


# ──────────────────────── _class_lottery LLM fallback (R27 #2 bonus) ───


def test_class_lottery_llm_failure_returns_fallback(monkeypatch, caplog):
    """R27 #2：game_num 命中后 LLM 抛异常 → 出「LLM 复盘暂不可用」文案 + log_warning。"""
    def fake_generate(prompt):
        raise ConnectionError("LLM 接口 502")

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", fake_generate)

    lottery = [
        {"game_num": "85", "game_name": "超级大乐透", "issue_no": "24001",
         "numbers_raw": "01 02 03 04 05 06 07"},
    ]
    with caplog.at_level("WARNING"):
        out = aa._class_lottery(lottery)
    assert len(out) == 1
    assert out[0]["game_num"] == "85"
    assert "LLM 复盘暂不可用" in out[0]["review"]
    assert any("lottery LLM failed" in r.message for r in caplog.records)


# ──────────────────────── _class_per_match sort + 截断 (R31 #2) ───────


def test_class_per_match_sorts_by_stars_desc_and_truncates_two(monkeypatch):
    """R31 #2：sort by stars desc + 截断到每 league top 2；LLM 调用数 = 2。

    5-star / 4-star / 3-star / 1-star / None / 0-star 6 项；预期 LLM
    被调 2 次，传入 5-star 和 4-star（顺序按 stars 降序）。
    """
    llm_calls = {"n": 0, "prompts": []}

    def fake_generate(prompt):
        llm_calls["n"] += 1
        llm_calls["prompts"].append(prompt)
        return {"text": f"insight #{llm_calls['n']}"}

    import ai.llm_client as llm
    monkeypatch.setattr(llm, "generate", fake_generate)

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": "3-star"},
        {"home": "C", "away": "D", "direction": "C 胜", "stars": "5-star"},
        {"home": "E", "away": "F", "direction": "E 胜", "stars": "1-star"},
        {"home": "G", "away": "H", "direction": "G 胜", "stars": None},
        {"home": "I", "away": "J", "direction": "I 胜", "stars": "0-star"},
        {"home": "K", "away": "L", "direction": "K 胜", "stars": "4-star"},
    ]}}}

    out = aa._class_per_match(predictions, fixtures=[])
    # 仅 2 次 LLM
    assert llm_calls["n"] == 2
    # 验证传入的 home/away 是 stars 最高的两个 (5-star C vs D, 4-star K vs L)
    assert "C vs D" in llm_calls["prompts"][0]
    assert "K vs L" in llm_calls["prompts"][1]
    # 输出 2 条
    assert len(out) == 2


def test_class_per_match_missing_or_zero_stars_treated_as_zero(monkeypatch):
    """R31 #2：stars=None 与 stars="0-star" 同档；stars="" 也归零（不会 int 解析崩溃）。"""
    import ai.llm_client as llm
    llm_calls = []

    def fake_generate(prompt):
        llm_calls.append(prompt)
        return {"text": "ok"}

    monkeypatch.setattr(llm, "generate", fake_generate)

    predictions = {"epl": {"data": {"predictions": [
        {"home": "A", "away": "B", "direction": "A 胜", "stars": None},
        {"home": "C", "away": "D", "direction": "C 胜", "stars": ""},
        {"home": "E", "away": "F", "direction": "E 胜", "stars": "0-star"},
    ]}}}

    # 不应 raise（None → falsy → key=0；"" → falsy → key=0）
    aa._class_per_match(predictions, fixtures=[])
    # 全部 key=0 → [] 取不到最大 → 仍 truncate 到 2 行
    assert len(llm_calls) == 2


def test_class_per_match_truncates_each_league_independently(monkeypatch):
    """R31 #2：多 league 各 3 items → 输出总数 = 4（每 league 各取 top 2）。"""
    import ai.llm_client as llm

    llm_calls = {"prompts": []}

    def fake_generate(prompt):
        llm_calls["prompts"].append(prompt)
        return {"text": "ok"}

    monkeypatch.setattr(llm, "generate", fake_generate)

    predictions = {
        "epl": {"data": {"predictions": [
            {"home": "Liverpool", "away": "Arsenal", "stars": "5-star"},
            {"home": "Chelsea", "away": "Spurs", "stars": "3-star"},
            {"home": "Wolves", "away": "Brentford", "stars": "1-star"},
        ]}},
        "laliga": {"data": {"predictions": [
            {"home": "Real", "away": "Barca", "stars": "5-star"},
            {"home": "Atletico", "away": "Valencia", "stars": "4-star"},
            {"home": "Sevilla", "away": "Villarreal", "stars": "2-star"},
        ]}},
    }

    out = aa._class_per_match(predictions, fixtures=[])
    # 每 league 各 2 → 总 4
    assert len(out) == 4
    assert len(llm_calls["prompts"]) == 4
    # 每 league top 2 是 (5-star, 3-star) → 验证 predict 数据
    epl_prompts = [p for p in llm_calls["prompts"] if "Liverpool" in p or "Chelsea" in p]
    laliga_prompts = [p for p in llm_calls["prompts"] if "Real" in p or "Atletico" in p]
    assert len(epl_prompts) == 2  # Liverpool + Chelsea (top 2)
    assert len(laliga_prompts) == 2  # Real + Atletico (top 2)
    # Wolves/Sevilla 不应进入 prompt（低 stars 被截断）
    assert not any("Wolves" in p for p in llm_calls["prompts"])
    assert not any("Sevilla" in p for p in llm_calls["prompts"])
