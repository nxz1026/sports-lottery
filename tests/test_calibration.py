"""Tests for calibration module (P1-5: 补充核心路径覆盖)"""

import unittest
import tempfile

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from core.calibration import compute_calibration_offset, build_calibration, _parse_score, append_historical_past_matches


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

    def test_no_odds_data_zero_odds_metrics(self):
        """所有场次无 home_true_prob → favored_by_odds=0 / favored_won=0 / odds_accuracy=0。"""
        past = [
            {"score": "2-1"},
            {"score": "1-1"},
        ]
        result = build_calibration(past)
        assert result["favored_by_odds"] == 0
        assert result["favored_won"] == 0
        assert result["odds_accuracy"] == 0, "无 odds 时 odds_accuracy 兜底 0"

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
