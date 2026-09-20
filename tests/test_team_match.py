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
         "play_type": "hhad", "options": {"goalLine": "-1", "goalLineValue": "-1.00", "h": "4.0", "d": "3.2", "a": "1.8"}},
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


def test_algo_rating_top2_weighted():
    r = tm.algo_suggestion({
        "direction": "佛罗伦萨 胜", "stars": "2-star",
        "predicted_score": "1-0", "home": "佛罗伦萨",
        "confidence_score": 0.486,
        "poisson_top3": [
            {"score": "1-0", "prob": 0.125}, {"score": "2-1", "prob": 0.086},
            {"score": "0-1", "prob": 0.078}],
        "reasoning_factors": {"home_ml_true_prob": 0.577, "draw_true_prob": 0.182,
                              "away_ml_true_prob": 0.242},
    })
    assert r["rating"] == 49          # round(0.486*100)
    assert r["top2"] == ["1-0", "2-1"]  # 取前2个比分
    assert r["weighted"] == "主胜"      # 主胜0.577 最高


def test_algo_rating_missing_fields():
    r = tm.algo_suggestion({"direction": "A 平", "stars": "0-star",
                            "predicted_score": "1-1", "home": "A"})
    assert r["rating"] is None
    assert r["top2"] == []
    assert "weighted" not in r


def test_build_rows_adds_goal_line():
    fixtures = [
        {"match_num": 7006, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "kickoff_bj": "09-20 02:45",
         "play_type": "hhad", "options": {"goalLine": "-1", "goalLineValue": "-1.00", "h": "4.0"}},
        {"match_num": 7006, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "kickoff_bj": "09-20 02:45",
         "play_type": "had", "options": {"h": "3.02", "d": "3.20", "a": "2.06"}},
    ]
    rows = tm.build_rows(fixtures, {"seriea": []})
    assert rows[0]["goal_line"] == "-1"


def test_nba_rows_builds_table():
    rows = tm.nba_rows([
        {"home": "底特律活塞", "away": "波士顿凯尔特人",
         "direction": "底特律活塞 胜", "spread_prediction": None,
         "total_prediction": None, "predicted_margin": 3.1,
         "predicted_score": "114-110"},
        {"home": "", "away": "X"},  # 空队名应跳过
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["match"] == "底特律活塞 vs 波士顿凯尔特人"
    assert r["margin"] == 3.1
    assert r["score"] == "114-110"
    assert tm.nba_rows([]) == []


def test_poisson_hhad_probs_sum_to_one_and_pick_valid():
    hh = tm.poisson_hhad(1.6, 0.9, "-1")
    assert hh is not None
    assert hh["line"] == "-1"
    probs = hh["probs"]
    assert set(probs) == {"让胜", "让平", "让负"}
    assert abs(sum(probs.values()) - 1.0) < 1e-3
    assert hh["pick"] in probs
    # 主让 1 球且主队进攻强：让负（主队让球后不赢）概率应高于让胜（需净胜 2+）
    assert probs["让负"] > probs["让胜"]


def test_poisson_hhad_main_handicap_favors_home_cover():
    # 主受让 1 球（line=+1）：主队赢球概率（让胜）显著更高
    hh = tm.poisson_hhad(1.6, 0.9, "+1")
    assert hh["probs"]["让胜"] > hh["probs"]["让负"]


def test_poisson_hhad_returns_none_on_bad_input():
    assert tm.poisson_hhad(None, 1.0, "-1") is None
    assert tm.poisson_hhad(1.0, 0.0, "-1") is None
    assert tm.poisson_hhad(1.0, 1.0, "abc") is None


def test_build_rows_attaches_hhad_model():
    fixtures = [
        {"match_num": 7100, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "kickoff_bj": "09-20 02:45",
         "play_type": "hhad", "options": {"goalLine": "-1", "goalLineValue": "-1.00"}},
    ]
    pred_by_league = {
        "seriea": [{"home": "佛罗伦萨", "away": "那不勒斯", "direction": "佛罗伦萨 胜",
                    "lambda_home": 1.8, "lambda_away": 0.8, "predicted_score": "2-0"}],
    }
    rows = tm.build_rows(fixtures, pred_by_league)
    assert rows[0]["hhad_model"]["line"] == "-1"
    assert rows[0]["hhad_model"]["pick"] in ("让胜", "让平", "让负")
    # 缺 λ 的预测不产 hhad_model（不编数）
    rows2 = tm.build_rows(fixtures, {"seriea": [{"home": "佛罗伦萨", "away": "那不勒斯"}]})
    assert "hhad_model" not in rows2[0]


def test_algo_suggestion_carries_probs():
    out = tm.algo_suggestion({
        "home": "佛罗伦萨", "away": "那不勒斯", "direction": "佛罗伦萨 胜",
        "reasoning_factors": {"home_ml_true_prob": 0.5, "draw_true_prob": 0.3,
                              "away_ml_true_prob": 0.2},
    })
    assert out["probs"] == {"主胜": 0.5, "平": 0.3, "客胜": 0.2}
    assert out["weighted"] == "主胜"
    # 无 reasoning_factors → 无 probs
    out2 = tm.algo_suggestion({"home": "A", "away": "B", "direction": "A 胜"})
    assert "probs" not in out2


def _gap_fixture_rows():
    # 两场：官方 h/d/a 3.02/3.20/2.06（隐含≈0.33/0.31/0.35）
    return [
        {"match_num": 8001, "league_cn": "意甲", "home_cn": "佛罗伦萨",
         "away_cn": "那不勒斯", "play_type": "had",
         "options": {"h": "3.02", "d": "3.20", "a": "2.06"}},
        {"match_num": 8002, "league_cn": "英超", "home_cn": "利兹联",
         "away_cn": "水晶宫", "play_type": "had",
         "options": {"h": "2.40", "d": "3.30", "a": "2.90"}},
        # 官方缺 a 的场次（如仅 hhad 在售）→ 不入榜
        {"match_num": 8003, "league_cn": "德甲", "home_cn": "拜仁",
         "away_cn": "多特", "play_type": "had", "options": {"h": "1.30", "d": "4.50"}},
    ]


def _gap_preds():
    return {
        "seriea": [{"home": "佛罗伦萨", "away": "那不勒斯",
                    "reasoning_factors": {"home_ml_true_prob": 0.10,
                                          "draw_true_prob": 0.30,
                                          "away_ml_true_prob": 0.60}}],
        "epl": [{"home": "利兹联", "away": "水晶宫",
                 "reasoning_factors": {"home_ml_true_prob": 0.45,
                                       "draw_true_prob": 0.30,
                                       "away_ml_true_prob": 0.25}}],
    }


def test_jc_gap_ranks_and_filters():
    rows = tm.build_rows(_gap_fixture_rows(), _gap_preds())
    out = tm.jc_gap(rows)
    assert len(out) == 2                 # 缺 a 的场次被过滤
    for it in out:
        assert set(it["official"]) == {"主胜", "平", "客胜"}
        assert abs(sum(it["official"].values()) - 1.0) < 1e-6
        assert 0 <= it["gap"] <= 1
    # 场次1：模型主胜0.10 vs 官方隐含≈0.33 → 主胜分歧最大，模型远低 → 官方隐含更看好主胜
    it1 = next(x for x in out if x["match_num"] == 8001)
    assert it1["worst"] == "主胜"
    assert "官方隐含更看好主胜" in it1["note"]
    # top_n 截断
    assert len(tm.jc_gap(rows, top_n=1)) == 1


def test_jc_gap_empty_and_note():
    assert tm.jc_gap([]) == []
    assert tm.jc_gap_note("客胜", 0.2, 0.5) == "模型较市场更看好客胜"
    assert tm.jc_gap_note("主胜", 0.4, 0.1) == "官方隐含更看好主胜"
    assert tm.jc_gap_note("平", 0.3, 0.35) == "平分歧居前"