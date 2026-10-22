"""Tests for calibration module (P1-5: 补充核心路径覆盖)"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import core.calibration as calibration_mod
from core.calibration import _parse_score, append_historical_past_matches, build_calibration, compute_calibration_offset


class TestParseScore(unittest.TestCase):
    def test_normal_score(self):
        self.assertEqual(_parse_score("2-1"), (2, 1))

    def test_zero_score(self):
        self.assertEqual(_parse_score("0-0"), (0, 0))

    def test_high_score(self):
        self.assertEqual(_parse_score("5-4"), (5, 4))

    def test_none_input(self):
        self.assertIsNone(_parse_score(None))

    def test_empty_string(self):
        self.assertIsNone(_parse_score(""))

    def test_no_dash(self):
        self.assertIsNone(_parse_score("21"))

    def test_non_numeric(self):
        self.assertIsNone(_parse_score("a-b"))


class TestComputeCalibrationOffset(unittest.TestCase):
    def test_insufficient_data(self):
        past = [{"score": "1-0"}, {"score": "2-1"}]
        self.assertIsNone(compute_calibration_offset(past))

    def test_balanced_distribution(self):
        past = [
            {"score": "1-0"}, {"score": "0-1"},
            {"score": "1-1"}, {"score": "2-1"}, {"score": "0-1"},
        ]
        offset = compute_calibration_offset(past)
        self.assertIsNotNone(offset)
        self.assertIn("home_correction", offset)
        self.assertEqual(offset["sample_size"], 5)

    def test_home_heavy_bias(self):
        past = [{"score": f"{i}-0"} for i in range(1, 6)]
        offset = compute_calibration_offset(past)
        self.assertIsNotNone(offset)
        self.assertGreater(offset["home_correction"], 1.0)

    def test_output_keys_include_onside(self):
        past = [
            {"score": "2-1"}, {"score": "1-2"}, {"score": "1-1"},
            {"score": "3-0"}, {"score": "0-3"}, {"score": "2-2"},
        ]
        offset = compute_calibration_offset(past)
        expected_keys = {
            "home_correction", "draw_correction", "away_correction",
            "onside_home_correction", "onside_away_correction",
            "sample_size", "sample_weight",
        }
        for key in expected_keys:
            self.assertIn(key, offset)


class TestBuildCalibration(unittest.TestCase):
    def test_empty_past(self):
        result = build_calibration([])
        self.assertIn("note", result)

    def test_with_matches(self):
        past = [
            {"score": "2-1", "home_true_prob": 0.6},
            {"score": "1-2", "home_true_prob": 0.3},
            {"score": "1-1", "home_true_prob": 0.5},
        ]
        result = build_calibration(past)
        self.assertEqual(result["total_matches"], 3)

    def test_missing_score_key_does_not_raise(self):
        """R13 #7 (b)：m["score"] → m.get("score") 防止 KeyError 冒泡主流程。"""
        past = [
            {"score": "2-1", "home_true_prob": 0.6},
            {"home_true_prob": 0.7},  # 缺 score → 不计 favored_wons，但 home_wins 仍统计
        ]
        result = build_calibration(past)
        # total=1（只算 2-1 这一场），favored_by_odds=2（两场 hp>0.5），favored_won=1
        assert result["total_matches"] == 1
        assert result["favored_by_odds"] == 2
        assert result["favored_won"] == 1, "有 score 的 2-1 应让 favored_won=1"

    def test_unparseable_score_does_not_count_as_fav_win(self):
        """score 解析失败（_parse_score 返回 None）→ favored_won 不计。"""
        past = [
            {"score": "abc", "home_true_prob": 0.7},  # parse 失败
            {"score": "1-x", "home_true_prob": 0.6},  # 非数字
            {"score": "3-2", "home_true_prob": 0.55},  # parse 成功 + fav home win
        ]
        result = build_calibration(past)
        # total=1（只 3-2 parse ok），favored_by_odds=3，favored_won=1
        assert result["total_matches"] == 1
        assert result["favored_by_odds"] == 3
        assert result["favored_won"] == 1

    def test_odds_accuracy_rounding(self):
        """favored_won / favored_by_odds 比例保留 3 位小数（odds_accuracy）。"""
        past = [
            {"score": "2-1", "home_true_prob": 0.7},  # win
            {"score": "1-2", "home_true_prob": 0.6},  # lose
            {"score": "1-0", "home_true_prob": 0.8},  # win
        ]
        result = build_calibration(past)
        # favored_by_odds=3，favored_won=2，odds_accuracy=0.667
        assert result["odds_accuracy"] == 0.667, (
            f"odds_accuracy 应 round 到 3 位小数；实际 {result['odds_accuracy']}"
        )

    def test_no_odds_data_odds_metrics_are_none_not_zero(self):
        """所有场次无 home_true_prob → odds_accuracy 是**无数据(None)**，不是 0%。

        生产实测（2026-10-21）：`references/historical_past_matches.json` 的 76 场
        历史样本**没有一场**带 `home_true_prob` ⇒ 旧口径返回 0 ⇒ predict.py 印出
        "Odds favorite accuracy: 0% (0/0)"，读起来像「赔率热门从没赢过」，
        实际是「根本没有赔率样本」⇒ 违反建议 3 的「n 不足不显示均值」纪律。
        """
        past = [
            {"score": "2-1"},
            {"score": "1-1"},
        ]
        result = build_calibration(past)
        assert result["favored_by_odds"] == 0
        assert result["favored_won"] == 0
        assert result["odds_accuracy"] is None, "无赔率样本时 odds_accuracy 必须是 None（无数据），不是 0"
        assert result["odds_note"] == "no_odds_samples"

    def test_odds_note_none_when_odds_samples_exist(self):
        """有赔率样本时 odds_note=None（不留误导性的 note）。"""
        past = [{"score": "2-1", "home_true_prob": 0.7}]
        result = build_calibration(past)
        assert result["favored_by_odds"] == 1
        assert result["odds_accuracy"] == 1.0
        assert result["odds_note"] is None

    def test_home_draw_away_distribution_rates(self):
        """home_win_rate / draw_rate / away_win_rate 正确反映 1/3 / 1/3 / 1/3。"""
        past = [
            {"score": "1-0"},   # home win
            {"score": "0-1"},   # away win
            {"score": "1-1"},   # draw
        ]
        result = build_calibration(past)
        assert result["total_matches"] == 3
        assert result["home_wins"] == 1
        assert result["draws"] == 1
        assert result["away_wins"] == 1
        assert result["home_win_rate"] == 0.333
        assert result["draw_rate"] == 0.333
        assert result["away_win_rate"] == 0.333


class TestHistoricalPastMatches(unittest.TestCase):
    """P5: 历史完赛记录跨运行累计（线上 ML 激活前提）。"""

    def test_append_dedup_and_only_scored(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            # 第一次运行：2 场完赛 + 1 场未结束（无比分）
            past1 = [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"},
                {"kickoff_utc": "2026-01-02T12:00:00Z", "home": "C", "away": "D", "score": "0-0"},
                {"kickoff_utc": "2026-01-03T12:00:00Z", "home": "E", "away": "F"},  # 无比分 -> 跳过
            ]
            n1 = append_historical_past_matches("epl", past1, base_dir=base)
            self.assertEqual(n1, 2)

            # 第二次运行：含 1 场重复（应去重）+ 1 场新
            past2 = [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"},
                {"kickoff_utc": "2026-01-04T12:00:00Z", "home": "G", "away": "H", "score": "3-1"},
            ]
            n2 = append_historical_past_matches("epl", past2, base_dir=base)
            self.assertEqual(n2, 1)

            # load_historical_past_matches 默认读 PREDICTIONS_DIR，这里直接读文件验证
            import json as _json
            data = _json.loads((base / "references" / "historical_past_matches.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 3)
            for m in data:
                self.assertEqual(m["league"], "epl")
            # 仅含比分的条目被正确写入
            self.assertIn("2-1", [m["score"] for m in data])
            self.assertIn("3-1", [m["score"] for m in data])

    def test_empty_past_matches_no_op(self):
        """R23 #5：past_matches=[] → added=0，**不写文件**。"""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            n = append_historical_past_matches("epl", [], base_dir=base)
            self.assertEqual(n, 0)
            # historical_past_matches.json 不应被创建
            self.assertFalse((base / "references" / "historical_past_matches.json").exists())

    def test_duplicate_key_merges_odds_fields(self):
        """同键重复时**不能"先到先得"丢字段**（§6.12.6 训练/服务漂移的根因）。

        `predictions/*.json` 的 past_matches 排在前面且没有赔率，
        `references/historical_past_matches.json` 里同一场可能已回填真实先验
        ⇒ 旧口径下先验被遮蔽 ⇒ 训练集赔率特征恒为常数。
        """
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            preds = base / "predictions"
            refs = base / "references"
            preds.mkdir(parents=True)
            refs.mkdir(parents=True)

            match = {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"}
            # predictions 版本：**没有**赔率字段
            (preds / "prediction_2026-01-02_00.json").write_text(
                _json.dumps({"league": "epl", "past_matches": [dict(match)]}), encoding="utf-8")
            # references 版本：同一场，**有**回填的先验
            (refs / "historical_past_matches.json").write_text(
                _json.dumps([{**match, "league": "epl", "home_true_prob": 0.61,
                              "draw_true_prob": 0.22, "away_true_prob": 0.17,
                              "home_ml_implied": 0.65, "draw_implied": 0.24,
                              "away_ml_implied": 0.18, "odds_data_available": True}]),
                encoding="utf-8")

            old_preds, old_root = calibration_mod.PREDICTIONS_DIR, calibration_mod.FOOTBALL_DIR
            calibration_mod.PREDICTIONS_DIR = preds
            calibration_mod.FOOTBALL_DIR = base
            try:
                rows = calibration_mod.load_historical_past_matches(days=3650, league="epl")
            finally:
                calibration_mod.PREDICTIONS_DIR, calibration_mod.FOOTBALL_DIR = old_preds, old_root

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["home_true_prob"], 0.61)
            self.assertEqual(rows[0]["away_true_prob"], 0.17)
            self.assertTrue(rows[0]["odds_data_available"])
            # 其它字段仍取先到的那一份（不被 references 覆盖）
            self.assertEqual(rows[0]["score"], "2-1")

    def test_duplicate_key_keeps_existing_odds(self):
        """已有赔率的那一份**不被覆盖**（只补缺）。"""
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            preds = base / "predictions"
            refs = base / "references"
            preds.mkdir(parents=True)
            refs.mkdir(parents=True)

            match = {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"}
            (preds / "prediction_2026-01-02_00.json").write_text(
                _json.dumps({"league": "epl", "past_matches": [
                    {**match, "home_true_prob": 0.55, "odds_data_available": True}]}),
                encoding="utf-8")
            (refs / "historical_past_matches.json").write_text(
                _json.dumps([{**match, "league": "epl", "home_true_prob": 0.99,
                              "draw_true_prob": 0.005, "away_true_prob": 0.005,
                              "odds_data_available": True}]),
                encoding="utf-8")

            old_preds, old_root = calibration_mod.PREDICTIONS_DIR, calibration_mod.FOOTBALL_DIR
            calibration_mod.PREDICTIONS_DIR = preds
            calibration_mod.FOOTBALL_DIR = base
            try:
                rows = calibration_mod.load_historical_past_matches(days=3650, league="epl")
            finally:
                calibration_mod.PREDICTIONS_DIR, calibration_mod.FOOTBALL_DIR = old_preds, old_root

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["home_true_prob"], 0.55)  # 先到的那份不被覆盖
            self.assertEqual(rows[0]["draw_true_prob"], 0.005)  # 缺的被补上
            self.assertEqual(rows[0]["away_true_prob"], 0.005)

    def test_multi_league_dedup_by_league_kickoff_pair(self):
        """R23 #5：多 league 时 dedup key 含 league — 同一 (kickoff, home, away) 在
        epl 和 laliga 算两条独立数据（real-world 罕见但应正确处理）。
        """
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            # epl 写 1 场 + laliga 写同名对阵（不同 league）
            append_historical_past_matches("epl", [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"},
            ], base_dir=base)
            n = append_historical_past_matches("laliga", [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "0-2"},
            ], base_dir=base)
            # laliga 写入 1 条（league 不同的独立 dedup key）
            self.assertEqual(n, 1)
            import json as _json
            data = _json.loads((base / "references" / "historical_past_matches.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            # 各 league 仅 1 条
            from collections import Counter
            self.assertEqual(Counter(m["league"] for m in data), Counter(epl=1, laliga=1))

    def test_corrupt_existing_file_falls_back_to_empty_list(self):
        """R23 #5：historical_past_matches.json 已坏 → existing 走 [] 兜底，新数据正常合并。"""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ref_dir = base / "references"
            ref_dir.mkdir(parents=True)
            (ref_dir / "historical_past_matches.json").write_text("{not valid json", encoding="utf-8")

            n = append_historical_past_matches("epl", [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "2-1"},
            ], base_dir=base)
            self.assertEqual(n, 1)
            import json as _json
            data = _json.loads((ref_dir / "historical_past_matches.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["home"], "A")

    def test_invalid_score_inputs_all_skipped(self):
        """R23 #5：所有 score 字段非数字 / 缺 "-" / 空 → 全部跳过，added=0 不写文件。"""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            n = append_historical_past_matches("epl", [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B"},  # 缺 score
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "C", "away": "D", "score": "abc"},
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "E", "away": "F", "score": "1"},  # 缺 "-"
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "G", "away": "H", "score": "1-2-3"},  # 三段
            ], base_dir=base)
            self.assertEqual(n, 0)
            self.assertFalse((base / "references" / "historical_past_matches.json").exists())

    def test_existing_is_dict_falls_back_to_empty_list(self):
        """R23 #5：historical_past_matches.json 顶层是 dict（异常状态）→ existing=[] 兜底。"""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ref_dir = base / "references"
            ref_dir.mkdir(parents=True)
            # 顶层 dict（不是 list）—— 损坏或历史版本遗物
            import json as _json
            (ref_dir / "historical_past_matches.json").write_text(
                _json.dumps({"unexpected": "dict_top_level"}), encoding="utf-8")

            n = append_historical_past_matches("epl", [
                {"kickoff_utc": "2026-01-01T12:00:00Z", "home": "A", "away": "B", "score": "1-0"},
            ], base_dir=base)
            self.assertEqual(n, 1)
            data = _json.loads((ref_dir / "historical_past_matches.json").read_text(encoding="utf-8"))
            # 写入时是 list（覆盖原 dict）
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)


if __name__ == "__main__":
    unittest.main()
