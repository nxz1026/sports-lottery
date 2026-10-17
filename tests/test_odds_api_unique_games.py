"""R43-1: scripts/bball/odds_api.py::_unique_games 4 cases direct + D-50 lock.

sorted(home,away) 元组去重保序 helper 0 直测; fetch_odds_games +
fetch_completed_games 走 cover dedup 但 helper 自身契约未锁。3 caller 高
leverage: fetch_odds_games / fetch_completed_games / 外部 cache。

helper 含两条契约 (D-50):
- 排序后 (home, away) 元组为唯一键 (顺逆序同队视为重复)
- 保 first-seen 顺序 (跟原 The Odds API 响应顺序, 不重排)
- 空 key (缺 home_team/away_team) -> pair=("","") 也视为同一组

4 tests (EASY):
- 全唯一 -> 原顺序透传
- 同对重复 (顺逆序) -> 去重保首
- 不同 league 同对 -> 去重 (纯按队名, 不看 league)
- 缺 home/away 字段 -> pair=("","") 视为同一组也去重
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def odds_mod():
    """加载 scripts/bball/odds_api.py 为唯一 module name (复用 R40-1 模式)。"""
    spec = importlib.util.spec_from_file_location(
        "odds_api_unique_games_test",
        REPO_ROOT / "scripts" / "bball" / "odds_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-50 二契约 ────────────────────────


def test_unique_games_all_unique_preserves_order(odds_mod):
    """R43-1: 全唯一 (home,away 各异) -> 透传, 顺序不变。"""
    games = [
        {"home_team": "Lakers", "away_team": "Celtics"},
        {"home_team": "Heat", "away_team": "Bulls"},
    ]
    out = odds_mod._unique_games(games)
    assert out == games


def test_unique_games_d50_sorted_pair_dedup_first_seen(odds_mod):
    """R43-1 D-50: 同 pair (顺/逆序) -> 去重, 保首个 (The Odds API 顺序)。

    关键: pair 用 sorted((home, away)) 元组, 所以 (Lakers, Celtics) 与
    (Celtics, Lakers) 视为同一 pair, 只保留首次出现。
    """
    games = [
        {"home_team": "Lakers", "away_team": "Celtics", "id": "first"},
        {"home_team": "Celtics", "away_team": "Lakers", "id": "reverse"},  # 重复
    ]
    out = odds_mod._unique_games(games)
    assert len(out) == 1
    assert out[0]["id"] == "first"


def test_unique_games_d50_different_leagues_same_pair_still_dedup(odds_mod):
    """R43-1 D-50: 不同 league 但同队对 -> 去重 (按队名, 不看 league 字段)。

    锁定契约现状: helper 只看 home_team/away_team, 不看 league; 若日后
    想加 league 区分, 此测立即 fail 提醒。
    """
    games = [
        {"home_team": "Lakers", "away_team": "Celtics", "league": "nba"},
        {"home_team": "Lakers", "away_team": "Celtics", "league": "euroleague"},
    ]
    out = odds_mod._unique_games(games)
    assert len(out) == 1


def test_unique_games_d50_missing_team_fields_falls_back_to_empty_pair(odds_mod):
    """R43-1 D-50: 缺 home_team / away_team -> pair 走 "" fallback, 不抛错。

    锁定 pair fallback 行为: 防止上游响应字段缺失时未定义。
    三种 game:
    - {"id": "a"} -> pair=("", "") (双缺)
    - {"home_team": "Heat"} -> pair=("Heat", "") -> sorted=("", "Heat")
    - {"away_team": "Heat"} -> pair=("", "Heat") (单缺)
    前两条 pair 不同所以保留; 后两条 pair 相同所以去重, 只保留首条。
    """
    games = [
        {"id": "a"},            # pair=("","")
        {"id": "b", "home_team": "Heat"},  # sorted=("","Heat")
        {"id": "c", "away_team": "Heat"},  # pair=("","Heat") 与 b 重复
    ]
    out = odds_mod._unique_games(games)
    assert len(out) == 2
    assert out[0]["id"] == "a"
    assert out[1]["id"] == "b"  # b 先于 c 出现, 保首
