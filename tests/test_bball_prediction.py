"""R39-2: scripts/bball/run.py::_prediction 6 tests direct + D-31 alias key lock.

篮球预测单场组装 0 direct tests; run() 集成层仅通过 _past_detail 间接覆
盖 _prediction 一小部分 (match 字段 + kickoff_date + home/away translation)。
未锁的真契约:
- result 是 predict_game dict 的 mutation, 不是 copy (memo 共享风险)
- 9 个 overlay 字段总是被写入 (idempotent overwrite)
- spread_pred/total_pred 是 spread_prediction/total_prediction 的别名字段;
  用 `result.get("spread_prediction")` 拿到 None 时 alias 同样是 None (锁)

D-31 (alias key lock):
- `spread_pred = result.get("spread_prediction")`: 若有人改为 `result["spread_prediction"]`
  (mandatory access), predict_game 漏写该键时 _prediction 直接 KeyError,
  fixture 流挂掉; 用 .get() 才能 silently alias None。
- `total_pred` 同契约。

6 tests:
- D-31 happy path: 9 个 overlay 字段全部存在 + 值正确
- match 字段 = f"{home} vs {away}" (中英文都可)
- home_en/away_en = 原始英文 (与 _display_name 翻译后的 home/away 分离)
- D-31 spread_pred alias when key present: == spread_prediction 同值
- D-31 spread_pred alias when key absent (LOCK): result 无 spread_prediction
  key -> spread_pred 仍然是 None (没有 KeyError)
- kickoff_date/utc fallback: commence_time 解析失败时两个字段走 fallback
  (kickoff_date=now BJT date, kickoff_utc="") 而不是抛错
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bball import run as bball_run  # noqa: E402

# 固定 now: 2026-09-15 10:00 UTC, BJT 2026-09-15 18:00 (同日 BJT date = 09-15)
# 18:00Z commence 在 BJT 是 2026-09-16 02:00 -> kickoff_date = "2026-09-16"
NOW = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
BJT_DATE = "2026-09-15"  # _kickoff_date 在 now arg 兜底时使用
COMMENCE_BJT_DATE = "2026-09-16"  # _kickoff_date 用 commence_time 解析时使用


def _stub_predict_game(**overrides):
    """返一可控 base predict_game 字典; 调用方可覆盖任意字段。"""
    base = {
        "home": "Lakers",  # 模拟 _display_name 已翻译, 但 _prediction 会覆盖
        "away": "Celtics",
        "spread_prediction": -3.5,
        "total_prediction": 215.5,
        "win_probability": 0.65,
    }
    base.update(overrides)
    return base


@pytest.fixture
def stub_dependencies(monkeypatch):
    """把 parse_odds / predict_game / _display_name 全 stub, 只测 _prediction 自身。"""

    def _fake_parse_odds(game, home):
        # 返 (odds_home, _odds_away, spread, total) 4 元组
        return (-110, +100, -3.5, 215.5)

    def _fake_predict_game(home, away, ratings, spread, total, odds_home, _injuries):
        # 透传给外层 override, 让测试控制返回值
        return _stub_predict_game()

    def _fake_display_name(name):
        # 简易翻译映射: Los Angeles Lakers -> 湖人
        m = {"Los Angeles Lakers": "湖人", "Boston Celtics": "凯尔特人"}
        return m.get(name, name)

    monkeypatch.setattr(bball_run, "parse_odds", _fake_parse_odds)
    monkeypatch.setattr(bball_run, "predict_game", _fake_predict_game)
    monkeypatch.setattr(bball_run, "_display_name", _fake_display_name)


# ──────────────────────── D-31 overlay & alias lock ────────────────────────


def test_prediction_d31_happy_path_all_overlays_present(
    stub_dependencies,
):
    """R39-2 D-31: happy path 9 个 overlay 字段全部写入。

    验证 _prediction 在 predict_game dict 基础上稳定追加 9 个字段:
    home/away/match/home_en/away_en/kickoff_date/kickoff_utc/spread_pred/total_pred。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-09-15T18:00:00Z",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)

    # D-31 锁: 9 overlay 字段稳定存在
    for key in ("home", "away", "match", "home_en", "away_en",
                "kickoff_date", "kickoff_utc", "spread_pred", "total_pred"):
        assert key in out, f"_prediction 漏写 {key}"

    # 翻译字段 (display_name 应用)
    assert out["home"] == "湖人"
    assert out["away"] == "凯尔特人"
    # 原始英文字段 (与翻译分离)
    assert out["home_en"] == "Los Angeles Lakers"
    assert out["away_en"] == "Boston Celtics"
    # match = "home vs away"
    assert out["match"] == "湖人 vs 凯尔特人"
    # kickoff_date BJT date: commence_time 18:00Z + 8h = 次日 02:00 -> "2026-09-16"
    assert out["kickoff_date"] == COMMENCE_BJT_DATE
    assert out["kickoff_utc"] == "2026-09-15T18:00:00Z"
    # spread_pred/total_pred = 别名
    assert out["spread_pred"] == -3.5
    assert out["total_pred"] == 215.5


def test_prediction_match_field_format(stub_dependencies):
    """R39-2: match 字段严格 = f"{home} vs {away}"。"""
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-09-15T18:00:00Z",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)
    # 分隔符严格 " vs "; 顺序 home 在前
    assert out["match"] == f"{out['home']} vs {out['away']}"
    assert " vs " in out["match"]


def test_prediction_home_en_away_en_are_raw_english(stub_dependencies):
    """R39-2: home_en/away_en 必须保留原始英文名 (i18n 翻译后中文名只用于显示)。

    `home_en` 是 The Odds API 的原始标识符; ai/feedback_loop.py::score_key
    使用 league|home_en|away_en 作为稳定主键, 翻译会破坏主键稳定性。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-09-15T18:00:00Z",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)
    # 严格 == 原始 team 字符串 (不是 _display_name 输出)
    assert out["home_en"] == "Los Angeles Lakers"
    assert out["away_en"] == "Boston Celtics"
    # 显示字段是翻译后
    assert out["home"] != out["home_en"] or out["home"] == "Los Angeles Lakers"
    # 注: _display_name 翻译仅在字典命中时与 raw 不同; 上例中 home 已被翻译
    # 是通过 _fake_display_name 的 "Los Angeles Lakers" -> "湖人" 映射
    assert out["home"] == "湖人"


def test_prediction_d31_spread_pred_alias_when_key_present(
    stub_dependencies, monkeypatch,
):
    """R39-2 D-31 alias 契约: spread_pred == spread_prediction。

    predict_game 返回 dict 含 spread_prediction 时, _prediction alias 透传。
    """
    def _with_spread(home, away, ratings, spread, total, odds_home, _inj):
        return {"spread_prediction": -7.5, "total_prediction": 220.0,
                "win_probability": 0.7}
    monkeypatch.setattr(bball_run, "predict_game", _with_spread)

    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-09-15T18:00:00Z",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)
    assert out["spread_pred"] == -7.5
    assert out["spread_pred"] == out["spread_prediction"]
    assert out["total_pred"] == 220.0
    assert out["total_pred"] == out["total_prediction"]


def test_prediction_d31_spread_pred_alias_when_key_absent(
    stub_dependencies, monkeypatch,
):
    """R39-2 D-31 alias 契约 (LOCK): spread_prediction 缺 -> spread_pred = None。

    若有人改 `result.get("spread_prediction")` 为 `result["spread_prediction"]`,
    本测 KeyError 立即爆, _prediction 退化失败。`.get()` 才能 silently 默认 None。

    同样适用于 total_pred / total_prediction。
    """
    def _no_spread(home, away, ratings, spread, total, odds_home, _inj):
        # 不返回 spread_prediction / total_prediction
        return {"win_probability": 0.5}
    monkeypatch.setattr(bball_run, "predict_game", _no_spread)

    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2026-09-15T18:00:00Z",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)
    assert out["spread_pred"] is None
    assert out["total_pred"] is None
    # 验证不是 KeyError 路径


def test_prediction_kickoff_fallback_on_unparseable_commence_time(
    stub_dependencies,
):
    """R39-2: commence_time 解析失败 -> kickoff_date = now(BJT), kickoff_utc = ''。

    与 _kickoff_utc / _kickoff_date 单测保持一致; 这里验证 _prediction 串联
    两个独立 fallback 不抛错。
    """
    game = {
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "garbage-not-a-date",
    }
    out = bball_run._prediction(game, ratings={}, now=NOW)
    # kickoff_date 走 now BJT date fallback = 2026-09-15
    assert out["kickoff_date"] == BJT_DATE
    # kickoff_utc 解析失败返空串
    assert out["kickoff_utc"] == ""
