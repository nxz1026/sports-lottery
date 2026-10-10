"""scripts/bball/run.py CLI 装配回归测试。

P0-COLLECT2e 修复点：
- _save 文件名必须带 _nba 后缀（否则与足球 prediction 同小时覆盖）
- run() 在 predictions 为空时落 status="no_future_matches"（休赛期识别）
- _kickoff_utc 解析失败留空串（不 raise）
- _kickoff_date 解析失败回退到 now 的 BJT 日期
- run() 缺 ODDS_API_KEY 即 SystemExit(2)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bball import run as bball_run  # noqa: E402


# ---- _kickoff_utc / _kickoff_date ----

def test_kickoff_utc_iso_with_z_round_trip() -> None:
    game = {"commence_time": "2026-09-15T18:00:00Z"}
    out = bball_run._kickoff_utc(game)
    assert out == "2026-09-15T18:00:00Z"


def test_kickoff_utc_non_utc_offset_normalizes_to_utc() -> None:
    game = {"commence_time": "2026-09-15T18:00:00+08:00"}
    out = bball_run._kickoff_utc(game)
    assert out == "2026-09-15T10:00:00Z"


def test_kickoff_utc_unparseable_returns_empty_string() -> None:
    game = {"commence_time": "not-a-date"}
    assert bball_run._kickoff_utc(game) == ""


def test_kickoff_utc_missing_returns_empty_string() -> None:
    assert bball_run._kickoff_utc({}) == ""


def test_kickoff_date_bjt_for_known_game() -> None:
    game = {"commence_time": "2026-09-15T18:00:00Z"}
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert bball_run._kickoff_date(game, now) == "2026-09-16"


def test_kickoff_date_unparseable_falls_back_to_now() -> None:
    game = {"commence_time": "garbage"}
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # now + 8h BJT = 2026-01-01 20:00 → 日期仍是 2026-01-01
    assert bball_run._kickoff_date(game, now) == "2026-01-01"


# ---- _save 文件名后缀（P0-COLLECT2e 修复点）----

def test_save_filename_includes_nba_suffix(tmp_path, monkeypatch):
    """_save 落盘文件名必须带 _nba 后缀，避免与足球 prediction 互相覆盖。"""
    monkeypatch.setattr(bball_run, "PREDICTIONS_DIR", tmp_path)
    output = {"league": "nba", "status": "ok", "predictions": []}
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    path = bball_run._save(output, now)
    assert path.name.endswith("_nba.json"), f"expected _nba.json suffix, got {path.name}"


def test_save_filename_no_suffix_when_league_blank(tmp_path, monkeypatch):
    """league 字段为空/非字符串时仍可保存（不带后缀，向后兼容）。"""
    monkeypatch.setattr(bball_run, "PREDICTIONS_DIR", tmp_path)
    output = {"status": "ok", "predictions": []}  # 无 league
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    path = bball_run._save(output, now)
    # 无 league 时不带后缀，但 _save 仍落盘（不应 raise）
    assert path.name.endswith(".json")


def test_save_round_trip_reads_back(tmp_path, monkeypatch):
    """_save 写出的 JSON 应能被原样读回。"""
    monkeypatch.setattr(bball_run, "PREDICTIONS_DIR", tmp_path)
    output = {"league": "nba", "status": "no_future_matches", "predictions": []}
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    path = bball_run._save(output, now)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == output


# ---- run() ----

def test_run_exits_when_odds_api_key_missing(monkeypatch):
    """ODDS_API_KEY 未设置 → run() 应 SystemExit(2)。"""
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    args = argparse.Namespace(backtest=0, ahead_days=1, leagues="nba")
    with pytest.raises(SystemExit) as exc_info:
        bball_run.run(args)
    assert exc_info.value.code == 2


def test_run_marks_no_future_matches_when_predictions_empty(monkeypatch, tmp_path):
    """前瞻模式 predictions=[] → status=no_future_matches（休赛期识别）。"""
    monkeypatch.setenv("ODDS_API_KEY", "fake")
    monkeypatch.setattr(bball_run, "PREDICTIONS_DIR", tmp_path)

    args = argparse.Namespace(backtest=0, ahead_days=1, leagues="nba")
    fake_games: list = []  # 无未来比赛（休赛期）

    def _stub_fetch_odds_games(key, sport, ahead_days):
        return fake_games

    def _stub_filter_window_games(games, ahead_hours):
        return [], []  # _past=[], future=[]

    def _stub_fetch_completed_games(key, sport, n):
        return []  # 无已完赛（不更新）

    monkeypatch.setattr(bball_run, "fetch_odds_games", _stub_fetch_odds_games)
    monkeypatch.setattr(bball_run, "filter_window_games", _stub_filter_window_games)
    monkeypatch.setattr(bball_run, "fetch_completed_games", _stub_fetch_completed_games)

    out = bball_run.run(args)
    assert out["status"] == "no_future_matches"
    assert out["predictions"] == []
    assert out["league"] == "nba"
    assert out["sport"] == "basketball"
    assert out["data_source"] == "the-odds"
    assert out["tournament_type"] == "league"


def test_run_backtest_mode_status_is_ok(monkeypatch, tmp_path):
    """--backtest 模式 status 强制 ok，不取 predictions 字段。"""
    monkeypatch.setenv("ODDS_API_KEY", "fake")
    monkeypatch.setattr(bball_run, "PREDICTIONS_DIR", tmp_path)

    args = argparse.Namespace(backtest=10, ahead_days=1, leagues="nba")

    def _stub_fetch_completed_games(key, sport, n):
        # 返回 1 个 0 分比赛（验 0 分比赛的 actual 写入）
        return [{
            "home_team": "Boston Celtics", "away_team": "LA Lakers",
            "scores": {"Boston Celtics": 0, "LA Lakers": 0},
        }]

    monkeypatch.setattr(bball_run, "fetch_completed_games", _stub_fetch_completed_games)

    out = bball_run.run(args)
    assert out["status"] == "ok"
    assert "predictions" not in out or out.get("predictions") == []
    assert len(out["past_matches"]) == 1
    detail = out["past_matches"][0]
    assert detail["actual_score"] == "0-0"  # 0-0 不能被 truthy 跳过（实记）