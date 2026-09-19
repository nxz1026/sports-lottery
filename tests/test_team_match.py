"""team_match 合并逻辑单测：队名匹配（含别名）、odd 层、算法层、行构建。

新能力先单测再扩：每日一图的核心后端逻辑在此验证。
"""
from __future__ import annotations

import web.services.team_match as tm


def test_norm_name_strips_whitespace_and_case():
    assert tm.norm_name(" 博洛尼亚 ") == "博洛尼亚"
    assert tm.norm_name(" PSV 埃因霍温 ") == "psv埃因霍温"


def test_teams_match_exact_and_reversed():
    assert tm.teams_match("佛罗伦萨", "那不勒斯", "佛罗伦萨", "那不勒斯")
    # 主客反序视为同一场
    assert tm.teams_match("尼斯", "里尔", "里尔", "尼斯")
    assert not tm.teams_match("尼斯", "里尔", "弗赖堡", "美因茨05")


def test_teams_match_alias():
    # 盘口"曼彻斯特联" ↔ 预测"曼联"
    assert tm.teams_match("富勒姆", "曼彻斯特联", "富勒姆", "曼联")
    # 盘口"埃尔沃斯堡" ↔ 预测"鄂尔士贝格"
    assert tm.teams_match("沙尔克04", "埃尔沃斯堡", "沙尔克04", "鄂尔士贝格")
    # 盘口"弗洛西诺内" ↔ 预测"弗罗西诺内"
    assert tm.teams_match("弗洛西诺内", "科莫", "弗罗西诺内", "科莫")


def test_odd_suggestion_picks_lowest_odds():
    # 主3.05 平3.25 客2.03 → 客胜(2.03) 最低
    assert tm.odd_suggestion({"h": "3.05", "d": "3.25", "a": "2.03"}) == {
        "pick": "客胜", "odds": "2.03"}
    # h 最高不明显时仍取最低
    assert tm.odd_suggestion({"h": "1.19", "d": "5.30", "a": "10.00"})["pick"] == "主胜"
    # 空/坏数据
    assert tm.odd_suggestion({}) == {}
    assert tm.odd_suggestion({"h": "abc", "d": "x", "a": "y"}) == {}


def test_algo_suggestion_parses_direction():
    assert tm.algo_suggestion({
        "direction": "博洛尼亚 胜", "stars": "2-star",
        "predicted_score": "1-0", "home": "博洛尼亚"})["pick"] == "主胜"
    assert tm.algo_suggestion({
        "direction": "卡利亚里 胜(接近)", "stars": "1-star",
        "predicted_score": "0-1", "home": "乌迪内斯"})["pick"] == "客胜"
    r = tm.algo_suggestion({
        "direction": "A 平", "stars": "0-star", "predicted_score": "1-1",
        "home": "A"})
    assert r["pick"] == "平"
    assert r["stars"] == 0


def test_build_rows_combines_fixture_and_prediction():
    fixtures = [
        {"match_num": 7006, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "kickoff_bj": "09-20 02:45",
         "play_type": "had", "options": {"h": "3.02", "d": "3.20", "a": "2.06"}},
        {"match_num": 7006, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "kickoff_bj": "09-20 02:45",
         "play_type": "hhad", "options": {"goal_line": "-1", "h": "4.0", "d": "3.2", "a": "1.8"}},
        # 无预测的一场比赛（如日职），matched=False
        {"match_num": 7003, "league_cn": "日职", "home_cn": "大阪钢巴",
         "away_cn": "神户胜利船", "kickoff_bj": "09-20 16:00",
         "play_type": "had", "options": {"h": "1.8", "d": "3.2", "a": "4.0"}},
    ]
    pred_by_league = {
        "seriea": [{
            "home": "佛罗伦萨", "away": "那不勒斯",
            "direction": "那不勒斯 胜", "stars": "2-star",
            "predicted_score": "0-1",
        }],
    }
    rows = tm.build_rows(fixtures, pred_by_league)
    assert len(rows) == 2
    by_mn = {r["match_num"]: r for r in rows}
    # 有预测的场次：matched=True、odd 层 + 算法层
    r = by_mn[7006]
    assert r["matched"] is True
    assert r["odd"] == {"pick": "客胜", "odds": "2.06"}
    assert r["algo"]["pick"] == "客胜"
    assert r["algo"]["stars"] == 2
    # 无预测的场次：matched=False、只有 odd 层
    r2 = by_mn[7003]
    assert r2["matched"] is False
    assert r2.get("algo") is None
    assert r2["odd"]["pick"] == "主胜"


def test_build_rows_matches_via_alias():
    fixtures = [
        {"match_num": 7020, "league_cn": "英超", "home_cn": "富勒姆",
         "away_cn": "曼彻斯特联", "kickoff_bj": "09-20 00:30",
         "play_type": "had", "options": {"h": "3.37", "d": "3.65", "a": "1.79"}},
    ]
    pred_by_league = {
        "epl": [{"home": "富勒姆", "away": "曼联", "direction": "曼彻斯特联 胜",
                 "stars": "3-star", "predicted_score": "0-2"}],
    }
    rows = tm.build_rows(fixtures, pred_by_league)
    assert rows and rows[0]["matched"] is True
    assert rows[0]["algo"]["pick"] == "客胜"
    assert rows[0]["algo"]["stars"] == 3