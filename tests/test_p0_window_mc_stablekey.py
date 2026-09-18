"""三个 P0 修复的回归测试：取数窗口回看 30 天 / 英文稳定主键 / 蒙特卡洛整季播种。

- 修复 A（P0-1）：predict.py 取数窗口由「今天-明天」改为「今天-past_days 天 - 明天」，
  DEFAULT_PAST_DAYS=30 同时落在 core.constants 与 core.config（再导出）。
- 修复 B（P0-1b）：backtest.py 命中率/对账链路改用 _bk_stable_key（home_en|away_en
  优先，回退 name/match），解决 i18n 译名与英文原名键交集为 0 的漏配。
- 修复 C（P0-2）：monte_carlo 支持整季赛程 + 积分榜播种（initial_standings）、
  champion_probs 保留 0 概率球队；fetch_events(..., whole_season=True) 在
  football-data 源下不拼 dateFrom/dateTo。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# 仓库约定：需要 core.* 的测试自行把 scripts 加入 sys.path。
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ══════════════════════════════════════════════════════════════════════════════
# 修复 A：取数窗口回看 30 天（P0-1）
# ══════════════════════════════════════════════════════════════════════════════

def test_fix_a_constants_and_config_agree_on_30():
    from core import config, constants

    assert constants.DEFAULT_PAST_DAYS == 30
    assert config.DEFAULT_PAST_DAYS == 30
    # 再导出必须指向同一个值，两处漂移会让 argparse 默认与文档脱节
    assert config.DEFAULT_PAST_DAYS == constants.DEFAULT_PAST_DAYS


def test_fix_a_parser_parses_past_days_7():
    import predict

    args = predict.build_parser().parse_args(["--past-days", "7"])
    assert args.past_days == 7


def test_fix_a_parser_default_past_days_30():
    import predict

    args = predict.build_parser().parse_args([])
    assert args.past_days == 30


def test_fix_a_main_window_spans_past_30_days_to_tomorrow(capsys, monkeypatch):
    """不传 --dates 时 main() 构造的窗口是 (今天-past_days) - 明天。

    只断言格式与跨度（不 pin 具体日期，避免跨日跑挂）。main() 正常路径会
    发起取数，这里用 monkeypatch 把 run_league 换成捕获 dates_str 的桩。
    """
    import predict

    captured: dict[str, str] = {}

    def fake_run_league(league_key, args, now_bjt, dates_str, silent=False):
        captured["dates_str"] = dates_str
        return {"league": league_key}

    monkeypatch.setattr(predict, "run_league", fake_run_league)

    argv = ["prog"]  # 不带 --dates，走默认窗口构造
    monkeypatch.setattr(sys, "argv", argv)
    predict.main()
    dates_str = captured["dates_str"]

    assert "-" in dates_str
    d0, d1 = dates_str.split("-")
    assert len(d0) == 8 and len(d1) == 8 and d0.isdigit() and d1.isdigit()

    start = datetime.strptime(d0, "%Y%m%d")
    end = datetime.strptime(d1, "%Y%m%d")
    now_bjt = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)

    # 起始日 ≤ 今天 - 29 天（即至少回看 30 天含今天本身）
    assert start <= now_bjt - timedelta(days=29)
    # 结束日是明天（BJT），允许跨日 ±1 天容差
    tomorrow = (now_bjt + timedelta(days=1)).date()
    assert abs((end.date() - tomorrow).days) <= 1
    # 总跨度约 31 天
    span = (end - start).days
    assert 29 <= span <= 32


# ══════════════════════════════════════════════════════════════════════════════
# 修复 B：命中率/对账链路改用英文稳定主键（P0-1b）
# ══════════════════════════════════════════════════════════════════════════════

def test_fix_b_stable_key_chinese_name_only():
    from core.backtest import _bk_stable_key

    assert _bk_stable_key({"name": "布伦特福德|切尔西"}) == "布伦特福德|切尔西"


def test_fix_b_stable_key_english_pair_wins_over_name():
    from core.backtest import _bk_stable_key

    rec = {"name": "布伦特福德|切尔西", "home_en": "Brentford", "away_en": "Chelsea"}
    key = _bk_stable_key(rec)
    assert key == "Brentford|Chelsea"
    # 旧实现直接用 name —— 中英文键不相等正是漏配根因
    assert key != rec["name"]


@pytest.mark.parametrize("rec", [
    {"home_en": "Brentford", "away_en": "", "name": "布伦特福德|切尔西"},
    {"home_en": "", "away_en": "Chelsea", "name": "布伦特福德|切尔西"},
])
def test_fix_b_stable_key_falls_back_to_name_when_one_english_missing(rec):
    from core.backtest import _bk_stable_key

    assert _bk_stable_key(rec) == rec["name"]


def test_fix_b_stable_key_falls_back_to_match_field():
    from core.backtest import _bk_stable_key

    assert _bk_stable_key({"match": "Brentford vs Chelsea"}) == "Brentford vs Chelsea"


@pytest.mark.parametrize("rec", [{}, {"name": "", "match": "", "home_en": "", "away_en": ""}])
def test_fix_b_stable_key_empty_when_nothing_available(rec):
    from core.backtest import _bk_stable_key

    assert _bk_stable_key(rec) == ""


def test_fix_b_stable_key_strips_whitespace():
    from core.backtest import _bk_stable_key

    assert _bk_stable_key({"home_en": " Brentford ", "away_en": "Chelsea "}) == "Brentford|Chelsea"


PAST_MATCH = {"home": "布伦特福德", "away": "切尔西",
              "home_en": "Brentford", "away_en": "Chelsea", "score": "2-1"}
PRED_MATCH = {"home": "布伦特福德", "away": "切尔西",
              "home_en": "Brentford", "away_en": "Chelsea",
              "match": "布伦特福德 vs 切尔西", "direction": "布伦特福德胜",
              "predicted_score": "2-1", "over_under": "Over 2.5"}


def test_fix_b_reconcile_predictions_matches_english_key(monkeypatch, tmp_path):
    """带 home_en/away_en 的预测与同一场 past_matches 必须对上（旧实现键交集为 0）。"""
    import core.backtest as bt

    # 不读真实 scripts/predictions/：_bk_load_recent_predictions 打桩，绕过 PREDICTIONS_DIR
    monkeypatch.setattr(bt, "_bk_load_recent_predictions", lambda cutoff: [dict(PRED_MATCH)])

    result = bt.reconcile_predictions([dict(PAST_MATCH)], days=7)
    assert result is not None
    assert result["reconciled"] == 1
    assert result["correct_score"] == 1
    assert result["direction_accuracy"] == 1.0


def test_fix_b_reconcile_predictions_no_match_when_english_differs(monkeypatch):
    """反向：home_en 指向别的队则匹配不上，reconciled 为 0（返回 None）。"""
    import core.backtest as bt

    wrong = dict(PRED_MATCH, home_en="Arsenal")
    monkeypatch.setattr(bt, "_bk_load_recent_predictions", lambda cutoff: [wrong])

    assert bt.reconcile_predictions([dict(PAST_MATCH)], days=7) is None


def test_fix_b_reconcile_matches_chinese_name_only_fallback(monkeypatch):
    """只有中文名（无 home_en/away_en）时回退 name 键仍应对上。"""
    import core.backtest as bt

    past = {"home": "布伦特福德", "away": "切尔西", "name": "布伦特福德|切尔西", "score": "1-1"}
    pred = {"match": "布伦特福德|切尔西", "direction": "平局",
            "predicted_score": "1-1", "over_under": "Under 2.5"}
    monkeypatch.setattr(bt, "_bk_load_recent_predictions", lambda cutoff: [pred])

    result = bt.reconcile_predictions([past], days=7)
    assert result is not None
    assert result["reconciled"] == 1
    assert result["correct_direction"] == 1


def test_fix_b_reconcile_returns_none_when_no_actuals(monkeypatch):
    import core.backtest as bt

    monkeypatch.setattr(bt, "_bk_load_recent_predictions", lambda cutoff: [dict(PRED_MATCH)])
    assert bt.reconcile_predictions([{"home": "A", "away": "B", "score": "x-y"}], days=7) is None


def test_fix_b_collect_league_data_uses_stable_key_both_sides(monkeypatch, tmp_path):
    """_bk_collect_league_data 两侧都用稳定键：英文预测对中文 actuals。"""
    import core.backtest as bt

    pred_file = tmp_path / "prediction_20260918.json"
    pred_file.write_text(
        json.dumps({
            "league": "epl",
            "past_matches": [dict(PAST_MATCH)],
            "predictions": [dict(PRED_MATCH)],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(bt, "PREDICTIONS_DIR", tmp_path)

    actuals, preds = bt._bk_collect_league_data("epl", cutoff=0.0)
    assert actuals == {"Brentford|Chelsea": (2, 1)}
    assert len(preds) == 1

    assert bt.league_accuracy("epl", days=7) == {
        "window_days": 7,
        "reconciled": 1,
        "direction_accuracy": 1.0,
        "score_accuracy": 1.0,
        "over_under_accuracy": 1.0,
    }


def test_fix_b_collect_league_data_dedupes_by_stable_key(monkeypatch, tmp_path):
    """同一预测出现两次（重跑留痕）→ 按稳定键去重只留一条。

    注意：中文键与英文键本就不同（这正是修复 B 要解决的漏配），去重只对
    解析到同一稳定键的记录生效。
    """
    import core.backtest as bt

    rerun = dict(PRED_MATCH, predicted_score="1-0")  # 旧一次运行的预测
    pred_file = tmp_path / "prediction_20260918.json"
    pred_file.write_text(
        json.dumps({
            "league": "epl",
            "past_matches": [dict(PAST_MATCH)],
            "predictions": [dict(PRED_MATCH), rerun],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(bt, "PREDICTIONS_DIR", tmp_path)

    actuals, preds = bt._bk_collect_league_data("epl", cutoff=0.0)
    assert actuals == {"Brentford|Chelsea": (2, 1)}
    assert len(preds) == 1  # 同键去重，只留先出现的那条
    assert preds[0]["predicted_score"] == "2-1"


# ══════════════════════════════════════════════════════════════════════════════
# 修复 C：蒙特卡洛整季赛程 + 积分榜播种（P0-2）
# ══════════════════════════════════════════════════════════════════════════════

def test_fix_c_build_league_standings_points_gf_ga_gd():
    from core.model.monte_carlo import build_league_standings

    matches = [
        {"home": "A", "away": "B", "home_goals": 2, "away_goals": 1},  # A 胜
        {"home": "B", "away": "C", "home_goals": 0, "away_goals": 3},  # C 客胜
        {"home": "C", "away": "A", "home_goals": 1, "away_goals": 1},  # 平
    ]
    st = build_league_standings(matches)

    assert st["A"] == {"points": 4, "gf": 3, "ga": 2, "gd": 1}
    assert st["B"] == {"points": 0, "gf": 1, "ga": 5, "gd": -4}
    assert st["C"] == {"points": 4, "gf": 4, "ga": 1, "gd": 3}


def test_fix_c_build_league_standings_skips_incomplete_rows():
    from core.model.monte_carlo import build_league_standings

    st = build_league_standings([
        {"home": "A", "away": "B", "home_goals": None, "away_goals": 1},
        {"home": "", "away": "B", "home_goals": 1, "away_goals": 1},
        {"home": "A", "away": "B"},  # 无比分（未开赛）
    ])
    assert st == {}


def test_fix_c_derive_strengths_empty_input_returns_empty():
    from core.model.monte_carlo import derive_team_strengths

    assert derive_team_strengths([]) == {}
    assert derive_team_strengths([{"home": "A", "away": "B"}]) == {}


def test_fix_c_derive_strengths_all_zero_goals_returns_empty():
    from core.model.monte_carlo import derive_team_strengths

    matches = [{"home": "A", "away": "B", "home_goals": 0, "away_goals": 0}] * 3
    assert derive_team_strengths(matches) == {}


def test_fix_c_derive_strengths_strong_team_above_one_weak_below():
    from core.model.monte_carlo import derive_team_strengths

    matches = []
    # 强队 A 大比分碾压弱队 B 多次
    matches += [{"home": "A", "away": "B", "home_goals": 4, "away_goals": 0},
                {"home": "B", "away": "A", "home_goals": 0, "away_goals": 4}]
    # C vs D 互交白卷以外的中性比分，保证联赛总进球 > 0 且有四支队伍
    matches += [{"home": "C", "away": "D", "home_goals": 1, "away_goals": 1},
                {"home": "D", "away": "C", "home_goals": 1, "away_goals": 1}]

    st = derive_team_strengths(matches)
    assert set(st) == {"A", "B", "C", "D"}
    assert st["A"]["attack"] > 1.0
    assert st["B"]["attack"] < 1.0


def test_fix_c_derive_strengths_lambdas_finite_positive():
    from core.model.monte_carlo import derive_team_strengths

    matches = [
        {"home": "A", "away": "B", "home_goals": 2, "away_goals": 1},
        {"home": "B", "away": "A", "home_goals": 1, "away_goals": 2},
        {"home": "A", "away": "B", "home_goals": 3, "away_goals": 0},
    ]
    st = derive_team_strengths(matches)
    for team, s in st.items():
        for k in ("lambda_home", "lambda_away"):
            assert s[k] > 0
            assert s[k] == s[k]  # NaN check
            assert float("inf") > s[k] > 0


def test_fix_c_simulate_league_seeded_standings_decide_champion():
    """空 fixtures + 非空 initial_standings：没有比赛可模拟，冠军就是积分第一。"""
    from core.model.monte_carlo import simulate_league

    standings = {
        "Top": {"points": 80, "gf": 70, "ga": 30, "gd": 40},
        "Bottom": {"points": 30, "gf": 30, "ga": 60, "gd": -30},
    }
    result = simulate_league([], {}, rho=0.2, initial_standings=standings)

    assert result["champion"] == "Top"
    assert result["final_standings"][0] == {"team": "Top", "points": 80, "gd": 40, "gf": 70}
    assert result["final_standings"][1] == {"team": "Bottom", "points": 30, "gd": -30, "gf": 30}


def test_fix_c_simulate_league_without_seed_starts_all_at_zero():
    from core.model.monte_carlo import simulate_league

    result = simulate_league([], {}, rho=0.2, initial_standings=None)
    assert result["champion"] is None
    assert result["final_standings"] == []


def test_fix_c_simulate_league_does_not_mutate_initial_standings():
    from core.model.monte_carlo import simulate_league

    standings = {"A": {"points": 10, "gf": 5, "ga": 3, "gd": 2}}
    simulate_league([], {}, rho=0.2, initial_standings=standings)
    assert standings == {"A": {"points": 10, "gf": 5, "ga": 3, "gd": 2}}


def test_fix_c_champion_probs_keep_zero_probability_teams():
    """2 队极小模拟量下 champion_probs 仍保留全部参赛球队（不被 count>0 过滤）。"""
    from core.model.monte_carlo import monte_carlo_champion

    fixtures = [{"home": "Strong", "away": "Weak"}]
    strengths = {
        "Strong": {"attack": 2.0, "defence": 0.8, "lambda_home": 3.5, "lambda_away": 2.8},
        "Weak": {"attack": 0.4, "defence": 1.5, "lambda_home": 0.3, "lambda_away": 0.2},
    }
    out = monte_carlo_champion(fixtures, strengths, n_simulations=10, rho=0.2,
                               tournament_type="league")
    probs = out["champion_probs"]

    assert set(probs) == {"Strong", "Weak"}
    assert abs(sum(probs.values()) - 1.0) < 1e-6


def test_fix_c_champion_probs_include_standings_only_teams():
    """播种积分榜里的球队即使不在剩余赛程中也要进榜（概率分母不漏队）。"""
    from core.model.monte_carlo import monte_carlo_champion

    fixtures = [{"home": "A", "away": "B"}]
    standings = {"A": {"points": 60, "gf": 50, "ga": 20, "gd": 30},
                 "B": {"points": 40, "gf": 30, "ga": 25, "gd": 5},
                 "Idle": {"points": 0, "gf": 0, "ga": 0, "gd": 0}}
    out = monte_carlo_champion(fixtures, {}, n_simulations=10, rho=0.2,
                               tournament_type="league", initial_standings=standings)
    probs = out["champion_probs"]
    assert "Idle" in probs
    assert set(probs) == {"A", "B", "Idle"}
    assert abs(sum(probs.values()) - 1.0) < 1e-6


def test_fix_c_fetch_events_whole_season_passthrough(monkeypatch):
    """football-data 源下 whole_season=True 原样透传给 fetch_football_data。"""
    import core.data.fetch as fetch_mod

    calls: list[tuple] = []

    def fake_fd(dates_str, config, whole_season=False):
        calls.append((dates_str, whole_season))
        return [{"fake": True}]

    monkeypatch.setattr(fetch_mod, "fetch_football_data", fake_fd)
    events = fetch_mod.fetch_events("20260820-20260919", "epl", whole_season=True)

    assert events == [{"fake": True}]
    assert calls == [("20260820-20260919", True)]


def test_fix_c_fetch_events_whole_season_default_false(monkeypatch):
    """默认 whole_season=False 也要如实传下去（回归保护）。"""
    import core.data.fetch as fetch_mod

    calls: list[bool] = []
    monkeypatch.setattr(fetch_mod, "fetch_football_data",
                        lambda d, c, whole_season=False: calls.append(whole_season) or [])

    fetch_mod.fetch_events("20260820-20260919", "epl")
    assert calls == [False]


def test_fix_c_fetch_football_data_whole_season_url_has_no_dates(monkeypatch):
    """whole_season=True 时构造的 URL 不含 dateFrom/dateTo（拦截 _retry_request 断言）。"""
    import core.data.fetch as fetch_mod

    captured: dict[str, str] = {}

    class FakeResponse:
        def read(self):
            return __import__("json").dumps({"matches": []}).encode()

    def fake_retry(req, max_retries=3, timeout=None):
        captured["url"] = req.full_url
        return {"matches": []}

    monkeypatch.setattr(fetch_mod, "_retry_request", fake_retry)
    monkeypatch.setattr(fetch_mod, "convert_football_data_to_espn_format",
                        lambda data, config: [])

    fetch_mod.fetch_football_data("20260820-20260919",
                                  {"league_id": "PL"}, whole_season=True)
    url = captured["url"]
    assert url == "https://api.football-data.org/v4/competitions/PL/matches"
    assert "dateFrom" not in url and "dateTo" not in url


def test_fix_c_fetch_football_data_dated_url_keeps_dates(monkeypatch):
    """whole_season=False 时保留 dateFrom/dateTo（对照用例）。"""
    import core.data.fetch as fetch_mod

    captured: dict[str, str] = {}

    def fake_retry(req, max_retries=3, timeout=None):
        captured["url"] = req.full_url
        return {"matches": []}

    monkeypatch.setattr(fetch_mod, "_retry_request", fake_retry)
    monkeypatch.setattr(fetch_mod, "convert_football_data_to_espn_format",
                        lambda data, config: [])

    fetch_mod.fetch_football_data("20260820-20260919",
                                  {"league_id": "PL"}, whole_season=False)
    url = captured["url"]
    assert "dateFrom=2026-08-20" in url
    assert "dateTo=2026-09-19" in url


def test_fix_c_fetch_events_whole_season_espn_falls_back_without_error(monkeypatch):
    """espn 数据源不支持整季：记 warning 并退回日期区间，不抛异常。"""
    import core.data.fetch as fetch_mod

    calls: list[tuple] = []

    def fake_espn(dates_str, league_slug="epl"):
        calls.append((dates_str, league_slug))
        return [{"espn": True}]

    monkeypatch.setattr(fetch_mod, "fetch_espn", fake_espn)
    events = fetch_mod.fetch_events("20260820-20260919", "epl",
                                    data_source="espn", whole_season=True)

    assert events == [{"espn": True}]
    # epl 的 espn_slug 是 eng.1（而非字面 "epl"）
    assert calls == [("20260820-20260919", "eng.1")]


def test_fix_c_fetch_events_whole_season_unknown_source_falls_back(monkeypatch):
    """未知数据源 + whole_season=True 同样退回日期区间（espn 兜底路径）。"""
    import core.data.fetch as fetch_mod

    calls: list[str] = []
    monkeypatch.setattr(fetch_mod, "fetch_espn",
                        lambda dates_str, league_slug="epl": calls.append(dates_str) or [])

    events = fetch_mod.fetch_events("20260820-20260919", "epl",
                                    data_source="the-odds", whole_season=True)
    assert events == []
    assert calls == ["20260820-20260919"]


# ══════════════════════════════════════════════════════════════════════════════
# A-1：命中率跨文件结算（day-N 预测 + day-N+1 赛果 → 自动出数）
# 线上背景：2026-09-18 核查时 /api/v1/accuracy 恒空。根因不是连接键，而是
# 「被预测过的比赛还没完赛」——actuals 全在 08-21~09-14（旧赛果），preds 全在
# 09-18/19（未开赛），交集天然为 0。本测试锁死跨文件结算链路：预测文件里的
# predictions 与次日文件 past_matches 按 home_en|away_en 连接后必须出数。
# ══════════════════════════════════════════════════════════════════════════════

def test_a1_cross_file_settlement_produces_accuracy(monkeypatch, tmp_path):
    """day-N 预测文件 + day-N+1 带赛果文件 → league_accuracy 出数。"""
    import core.backtest as bt

    # day-N（09-18）：预测了布伦特福德 vs 切尔西（当时未开赛）
    day_n = tmp_path / "prediction_2026-09-18_17_epl.json"
    day_n.write_text(json.dumps({
        "league": "epl",
        "past_matches": [],  # 当日文件里没有这场比赛的赛果
        "predictions": [{
            "home": "布伦特福德", "away": "切尔西",
            "home_en": "Brentford FC", "away_en": "Chelsea FC",
            "match": "布伦特福德 vs 切尔西",
            "direction": "布伦特福德 胜", "predicted_score": "2-1",
            "over_under": "Over 2.5",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    # day-N+1（09-19）：比赛完赛，赛果 2-1 进入新文件的 past_matches
    day_n1 = tmp_path / "prediction_2026-09-19_09_epl.json"
    day_n1.write_text(json.dumps({
        "league": "epl",
        "past_matches": [{
            "home": "布伦特福德", "away": "切尔西",
            "home_en": "Brentford FC", "away_en": "Chelsea FC",
            "score": "2-1",
        }],
        "predictions": [],  # 新预测与本测试无关
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(bt, "PREDICTIONS_DIR", tmp_path)

    acc = bt.league_accuracy("epl", days=7)
    assert acc is not None, "跨文件结算必须出数（day-N 预测 × day-N+1 赛果）"
    assert acc["reconciled"] == 1
    assert acc["direction_accuracy"] == 1.0
    assert acc["score_accuracy"] == 1.0
    assert acc["over_under_accuracy"] == 1.0


def test_a1_unsettled_predictions_return_none(monkeypatch, tmp_path):
    """预测尚未完赛（无对应赛果）→ 返回 None，页面显示待结算而非假 0%。"""
    import core.backtest as bt

    f = tmp_path / "prediction_2026-09-18_17_epl.json"
    f.write_text(json.dumps({
        "league": "epl",
        "past_matches": [],
        "predictions": [{
            "home_en": "Brentford FC", "away_en": "Chelsea FC",
            "direction": "布伦特福德 胜", "predicted_score": "2-1",
            "over_under": "Over 2.5",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bt, "PREDICTIONS_DIR", tmp_path)

    assert bt.league_accuracy("epl", days=7) is None


def test_a1_wrong_direction_counts_as_miss(monkeypatch, tmp_path):
    """方向猜错必须计入分母（防止只统计命中的偏差）。"""
    import core.backtest as bt

    (tmp_path / "prediction_2026-09-18_17_epl.json").write_text(json.dumps({
        "league": "epl", "past_matches": [],
        "predictions": [{
            "home": "布伦特福德", "away": "切尔西",
            "home_en": "Brentford FC", "away_en": "Chelsea FC",
            "direction": "布伦特福德 胜", "predicted_score": "2-1",
            "over_under": "Over 2.5",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "prediction_2026-09-19_09_epl.json").write_text(json.dumps({
        "league": "epl", "past_matches": [{
            "home_en": "Brentford FC", "away_en": "Chelsea FC", "score": "0-2",
        }], "predictions": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bt, "PREDICTIONS_DIR", tmp_path)

    acc = bt.league_accuracy("epl", days=7)
    assert acc is not None
    assert acc["reconciled"] == 1
    assert acc["direction_accuracy"] == 0.0
    assert acc["score_accuracy"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# C-1：联赛模式 round_reach_probs 语义空洞修复
# 线上背景：simulate_league 硬编码 team_rounds[team]=["season"]，API 的
# round_reach_probs.season 对每队恒为 1.0（3000/3000），联赛本无轮次概念。
# ══════════════════════════════════════════════════════════════════════════════

def test_c1_simulate_league_returns_empty_team_rounds():
    from core.model.monte_carlo import simulate_league

    fixtures = [{"home": "A", "away": "B"}, {"home": "B", "away": "C"}]
    strengths = {
        "A": {"lambda_home": 1.8, "lambda_away": 1.0},
        "B": {"lambda_home": 1.4, "lambda_away": 1.2},
        "C": {"lambda_home": 1.2, "lambda_away": 1.4},
    }
    result = simulate_league(fixtures, strengths, rho=0.2)
    assert result["team_rounds"] == {}, "联赛模式不应再输出恒 1.0 的 season 轮次"
    assert result["champion"] in {"A", "B", "C"}


def test_c1_monte_carlo_league_round_reach_empty_but_champion_intact():
    from core.model.monte_carlo import monte_carlo_champion

    fixtures = [{"home": "A", "away": "B"}, {"home": "B", "away": "C"},
                {"home": "C", "away": "A"}]
    strengths = {
        "A": {"lambda_home": 1.8, "lambda_away": 1.0},
        "B": {"lambda_home": 1.4, "lambda_away": 1.2},
        "C": {"lambda_home": 1.2, "lambda_away": 1.4},
    }
    out = monte_carlo_champion(fixtures, strengths, n_simulations=200,
                               tournament_type="league")
    assert out["round_reach_probs"] == {}
    assert set(out["champion_probs"]) == {"A", "B", "C"}
    assert abs(sum(out["champion_probs"].values()) - 1.0) < 1e-6
    assert out["simulation_count"] == 200
