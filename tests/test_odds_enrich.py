"""B 修复（核查 P0-2）：ESPN 赔率富化。

football-data 源不带赔率 → 全部预测行 market.status=missing，今日推荐 KPI
与串关组合赔率恒「—」。odds_enrich 按「归一化队名 + 开球日/时点」把 ESPN
scoreboard 的 1x2 收盘赔率（moneyline.home/draw/away.close）回填进预测行。

覆盖：
- 美式→十进制赔率转换；
- 队名归一化（噪音词/重音/别名）与容器等价判定；
- 三层匹配：队名精确、队名容器、开球时点唯一/多候选消歧；
- 富化后字段：odds_data_available / home_true_prob / market.decimal_odds；
- 非致命：空事件列表不改变 future。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


# ── 美式 → 十进制 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("+160", 2.60), ("+275", 3.75), ("-145", 1.69), ("-175", 1.57),
    ("+100", 2.00), ("-100", 2.00),
    ("", None), (None, None), ("garbage", None), ("0", None),
])
def test_american_to_decimal(raw, expected):
    from core.data.odds_enrich import american_to_decimal
    assert american_to_decimal(raw) == expected


# ── 队名归一化 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Brentford FC", "brentford"),
    ("FC Barcelona", "barcelona"),
    ("Atlético de Madrid", "atletico madrid"),
    ("Hamburger SV", "hamburg sv"),        # alias hamburger→hamburg
    ("Hamburg SV", "hamburg sv"),
    ("1. FC Köln", "1 cologne"),            # alias koln→cologne
    ("FC Cologne", "cologne"),
    ("Olympique Lyonnais", "olympique lyon"),
    ("Le Havre AC", "le havre"),
])
def test_normalize_team(name, expected):
    from core.data.odds_enrich import normalize_team
    assert normalize_team(name) == expected


@pytest.mark.parametrize("a,b,expected", [
    ("Hamburger SV", "Hamburg SV", True),
    ("Inter Milan", "Inter", True),
    ("Marseille", "Nice", False),
    ("Milton Keynes Dons", "Milton", False),  # 差 2 词不配
    ("", "Chelsea", False),
])
def test_names_equivalent(a, b, expected):
    from core.data.odds_enrich import _names_equivalent
    assert _names_equivalent(a, b) is expected


# ── ESPN 事件构造工具 ──────────────────────────────────────────────────────

def _espn_event(home, away, date, home_ml, draw_ml, away_ml):
    return {
        "date": date,
        "competitions": [{
            "competitors": [
                {"homeAway": "home", "team": {"displayName": home}},
                {"homeAway": "away", "team": {"displayName": away}},
            ],
            "odds": [{
                "moneyline": {
                    "home": {"close": {"odds": home_ml}},
                    "draw": {"close": {"odds": draw_ml}},
                    "away": {"close": {"odds": away_ml}},
                },
                "drawOdds": {"moneyLine": int(draw_ml.lstrip("+")) if str(draw_ml).startswith("+") else None},
            }],
        }],
    }


def _fd_match(name, home_en, away_en, kickoff, odds=False):
    return {"name": name, "home_en": home_en, "away_en": away_en,
            "kickoff_utc": kickoff, "odds_data_available": odds,
            "home": "主队", "away": "客队"}


# ── 三层匹配 ────────────────────────────────────────────────────────────────

def test_layer1_exact_team_names():
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("A vs B", "Arsenal FC", "Chelsea FC", "2026-09-19T15:00:00Z")]
    events = [_espn_event("Arsenal", "Chelsea", "2026-09-19T15:00Z", "+150", "+270", "-400")]
    n = enrich_soccer_odds(future, events, NOW)
    assert n == 1
    assert future[0]["odds_data_available"] is True
    sel = future[0]["market"]["selections"]
    assert sel["home"]["decimal_odds"] == 2.50
    assert sel["draw"]["decimal_odds"] == 3.70
    assert sel["away"]["decimal_odds"] == 1.25
    # 三向去水后概率和 ≈ 1
    s = future[0]["home_true_prob"] + future[0]["draw_true_prob"] + future[0]["away_true_prob"]
    assert abs(s - 1.0) < 1e-6


def test_layer1_does_not_override_existing_odds():
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("A vs B", "Arsenal", "Chelsea", "2026-09-19T15:00:00Z", odds=True)]
    n = enrich_soccer_odds(future, [_espn_event("Arsenal", "Chelsea", "2026-09-19T15:00Z",
                                                 "+100", "+300", "-100")], NOW)
    assert n == 0


def test_layer3_single_candidate_by_kickoff_time():
    """队名全对不上（跨语言），但开球时点唯一 → 命中。"""
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("x", "Hamburger SV", "1. FC Köln", "2026-09-19T13:30:00Z")]
    events = [_espn_event("Hamburg SV", "FC Cologne", "2026-09-19T13:30Z", "+120", "+280", "-420")]
    assert enrich_soccer_odds(future, events, NOW) == 1
    assert future[0]["market"]["source"] == "espn"


def test_layer3_multi_candidate_disambiguated_by_team_names():
    """同时刻 4 场：队名相似度打分消歧，唯一最高分命中。"""
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("汉堡 vs 科隆", "Hamburger SV", "1. FC Köln", "2026-09-19T13:30:00Z")]
    events = [
        _espn_event("1. FC Union Berlin", "Bayern Munich", "2026-09-19T13:30Z", "+150", "+250", "-400"),
        _espn_event("Hamburg SV", "FC Cologne", "2026-09-19T13:30Z", "+120", "+280", "-420"),
        _espn_event("SC Freiburg", "Eintracht Frankfurt", "2026-09-19T13:30Z", "+130", "+260", "-380"),
        _espn_event("FC Augsburg", "Werder Bremen", "2026-09-19T13:30Z", "+140", "+270", "-390"),
    ]
    assert enrich_soccer_odds(future, events, NOW) == 1
    # 命中的应是 Hamburg SV vs FC Cologne 那条
    assert future[0]["market"]["selections"]["home"]["decimal_odds"] == 2.20


def test_layer3_multi_candidate_ambiguous_skipped():
    """同时刻候选且队名都打 0 分 → 跳过（绝不错配）。"""
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("x", "CompletelyUnknown", "TotallyForeign", "2026-09-19T13:30:00Z")]
    events = [
        _espn_event("Team One", "Team Two", "2026-09-19T13:30Z", "+150", "+250", "-400"),
        _espn_event("Team Three", "Team Four", "2026-09-19T13:30Z", "+130", "+260", "-380"),
    ]
    assert enrich_soccer_odds(future, events, NOW) == 0
    assert future[0].get("odds_data_available") in (None, False)


def test_missing_kickoff_uses_name_only_when_unique():
    """预测行无开球时刻 → 退化为纯队名匹配，唯一候选命中。"""
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("x", "Sevilla FC", "FC Barcelona", "")]
    events = [_espn_event("Sevilla", "Barcelona", "2026-09-19T19:00Z", "-200", "+220", "+250")]
    assert enrich_soccer_odds(future, events, NOW) == 1


def test_missing_kickoff_name_only_ambiguous_skipped():
    """无开球时刻 + 队名匹配到多场 → 跳过（绝不错配）。"""
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("x", "Sevilla", "Barcelona", "")]
    events = [
        _espn_event("Sevilla", "Barcelona", "2026-09-19T19:00Z", "-200", "+220", "+250"),
        _espn_event("Sevilla", "Barcelona", "2026-09-26T18:00Z", "-190", "+230", "+260"),
    ]
    assert enrich_soccer_odds(future, events, NOW) == 0


def test_empty_espn_events_noop():
    from core.data.odds_enrich import enrich_soccer_odds
    future = [_fd_match("x", "A", "B", "2026-09-19T15:00:00Z")]
    assert enrich_soccer_odds(future, [], NOW) == 0
    assert future[0].get("market") is None


def test_partial_moneyline_keeps_available_sides():
    """仅 home/draw 有赔率 → selections 缺 away，web 侧会降级 status=partial。"""
    from core.data.odds_enrich import _espn_market_dict, american_to_decimal
    captured = NOW.isoformat()
    dec = {"home": american_to_decimal("+150"), "draw": american_to_decimal("+270"), "away": None}
    market = _espn_market_dict(dec, captured)
    assert set(market["selections"]) == {"home", "draw"}
    assert market["market_type"] == "1x2"


def test_empty_market_returns_none():
    from core.data.odds_enrich import _espn_market_dict
    assert _espn_market_dict({"home": None, "draw": None, "away": None}, "now") is None
