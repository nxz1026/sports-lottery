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


if __name__ == "__main__":
    unittest.main()
