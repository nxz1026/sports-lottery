"""Tests for parse module (P1-5: 补充核心解析逻辑覆盖)"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from core.data.parse import (
    form_to_score,
    parse_american_odds,
    record_to_score,
    remove_vig,
    spread_movement_factor,
    to_cn,
)


class TestRemoveVig(unittest.TestCase):
    def test_all_present(self):
        h, d, a = remove_vig(0.50, 0.28, 0.29)
        total = round(h + d + a, 4)
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_away_missing(self):
        """away_p=None → should compute from 1.0 - home - draw"""
        h, d, a = remove_vig(0.50, 0.27, None)
        self.assertIsNotNone(a)
        self.assertGreater(a, 0)

    def test_home_missing(self):
        h, d, a = remove_vig(None, 0.28, 0.32)
        self.assertIsNotNone(h)
        self.assertGreater(h, 0)

    def test_draw_none(self):
        h, d, a = remove_vig(0.50, None, 0.30)
        self.assertIsNone(h)
        self.assertIsNone(d)
        self.assertIsNone(a)

    def test_both_missing(self):
        h, d, a = remove_vig(None, 0.28, None)
        self.assertIsNone(h)
        self.assertIsNone(d)
        self.assertIsNone(a)

    def test_negative_computed_away_clamped(self):
        """When computed away would be negative → clamp to MIN_IMPLIED_PROB"""
        h, d, a = remove_vig(0.80, 0.15, None)
        self.assertIsNotNone(a)
        self.assertGreaterEqual(a, 0.01)


class TestFormToScore(unittest.TestCase):
    def test_winning_form(self):
        score = form_to_score("WWWW")
        self.assertGreater(score, 0.7)

    def test_losing_form(self):
        score = form_to_score("LLLL")
        self.assertLess(score, 0.3)

    def test_mixed_form(self):
        score = form_to_score("WLWD")
        self.assertAlmostEqual(score, 0.5, delta=0.2)

    def test_empty_form(self):
        score = form_to_score("")
        self.assertAlmostEqual(score, 0.5)

    def test_none_form(self):
        score = form_to_score(None)
        self.assertAlmostEqual(score, 0.5)


class TestRecordToScore(unittest.TestCase):
    def test_good_record(self):
        records = [{"summary": "10-2-1"}]
        score = record_to_score(records)
        self.assertGreater(score, 0.6)

    def test_bad_record(self):
        records = [{"summary": "1-10-2"}]
        score = record_to_score(records)
        self.assertLess(score, 0.4)

    def test_empty_records(self):
        score = record_to_score([])
        self.assertAlmostEqual(score, 0.5)

    def test_no_summary(self):
        records = [{}]
        score = record_to_score(records)
        self.assertAlmostEqual(score, 0.5)


class TestParseAmericanOdds(unittest.TestCase):
    def test_positive_odds(self):
        p = parse_american_odds("+200")
        self.assertIsNotNone(p)
        self.assertGreater(p, 0)
        self.assertLess(p, 1)

    def test_negative_odds(self):
        p = parse_american_odds("-150")
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.5)

    def test_none_input(self):
        self.assertIsNone(parse_american_odds(None))


class TestSpreadMovementFactor(unittest.TestCase):
    def test_no_movement(self):
        open_s = {"odds": "-110", "line": "-0.5"}
        close_s = {"odds": "-110", "line": "-0.5"}
        factor = spread_movement_factor(open_s, close_s)
        self.assertAlmostEqual(factor, 0.0, places=2)

    def test_movement_toward_favorite(self):
        open_s = {"odds": "-105", "line": "-0.25"}
        close_s = {"odds": "-130", "line": "-0.75"}
        factor = spread_movement_factor(open_s, close_s)
        self.assertLess(factor, 0)

    def test_none_inputs(self):
        factor = spread_movement_factor(None, {})
        self.assertEqual(factor, 0.0)


class TestToCn(unittest.TestCase):
    def test_known_country(self):
        self.assertEqual(to_cn("England"), "英格兰")

    def test_unknown_country_passthrough(self):
        result = to_cn("UnknownTeam")
        self.assertIn("UnknownTeam", result)


if __name__ == "__main__":
    unittest.main()
