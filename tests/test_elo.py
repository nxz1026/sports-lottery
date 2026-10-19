"""Tests for core.elo — ELO dynamic rating system."""

from __future__ import annotations

import unittest

from core.elo import (
    DEFAULT_ELO,
    K_FACTOR,
    elo_to_lambda_scale,
    expected_score,
    goal_difference_adjustment,
    init_ratings_from_fifa,
    process_match_result,
    update_elo,
)


class TestExpectedScore(unittest.TestCase):
    """P0-1 fix verification: ELO expected_score formula."""

    def test_equal_elo(self) -> None:
        self.assertAlmostEqual(expected_score(1500, 1500), 0.5, places=4)

    def test_higher_elo_favored(self) -> None:
        """Stronger team should have > 0.5 expected score."""
        self.assertGreater(expected_score(1800, 1500), 0.5)

    def test_home_advantage(self) -> None:
        """Home advantage should increase expected score."""
        without_home = expected_score(1500, 1500, home_adv=False)
        with_home = expected_score(1500, 1500, home_adv=True)
        self.assertGreater(with_home, without_home)

    def test_symmetry(self) -> None:
        """E_A(A,B) + E_A(B,A) should sum to 1 (without home adv)."""
        e_ab = expected_score(1600, 1400, home_adv=False)
        e_ba = expected_score(1400, 1600, home_adv=False)
        self.assertAlmostEqual(e_ab + e_ba, 1.0, places=6)

    def test_extreme_elo_diff(self) -> None:
        """Very large ELO difference should approach 1.0."""
        self.assertGreater(expected_score(2200, 1000), 0.95)


class TestUpdateElo(unittest.TestCase):
    def test_win_increases_elo(self) -> None:
        new_a, new_b = update_elo(1500, 1500, 1.0, home_adv=False)
        self.assertGreater(new_a, 1500)
        self.assertLess(new_b, 1500)

    def test_loss_decreases_elo(self) -> None:
        new_a, new_b = update_elo(1500, 1500, 0.0, home_adv=False)
        self.assertLess(new_a, 1500)
        self.assertGreater(new_b, 1500)

    def test_draw_slight_change(self) -> None:
        new_a, new_b = update_elo(1500, 1500, 0.5, home_adv=False)
        self.assertEqual(new_a, 1500)
        self.assertEqual(new_b, 1500)

    def test_elo_conservation(self) -> None:
        """Total ELO should be conserved (zero-sum)."""
        new_a, new_b = update_elo(1600, 1400, 1.0, home_adv=False)
        self.assertAlmostEqual(new_a + new_b, 1600 + 1400, places=1)


class TestGoalDifferenceAdjustment(unittest.TestCase):
    def test_no_diff(self) -> None:
        self.assertEqual(goal_difference_adjustment(0), K_FACTOR)

    def test_one_goal(self) -> None:
        self.assertEqual(goal_difference_adjustment(1), K_FACTOR)

    def test_two_goals(self) -> None:
        self.assertEqual(goal_difference_adjustment(2), K_FACTOR * 1.5)

    def test_big_win(self) -> None:
        self.assertGreaterEqual(goal_difference_adjustment(5), K_FACTOR * 2)


class TestProcessMatchResult(unittest.TestCase):
    def test_home_win(self) -> None:
        ratings = {"A": 1500.0, "B": 1500.0}
        result = process_match_result("A", "B", 2, 0, ratings)
        self.assertGreater(ratings["A"], 1500)
        self.assertLess(ratings["B"], 1500)
        self.assertEqual(result["match"], "A 2-0 B")

    def test_new_team_gets_default(self) -> None:
        ratings: dict[str, float] = {}
        process_match_result("NewTeam", "Other", 1, 0, ratings)
        self.assertIn("NewTeam", ratings)
        self.assertNotEqual(ratings["NewTeam"], DEFAULT_ELO)


class TestEloToLambda(unittest.TestCase):
    def test_mid_elo(self) -> None:
        lam = elo_to_lambda_scale(1600)
        self.assertGreater(lam, 0.8)
        self.assertLess(lam, 3.0)

    def test_low_elo(self) -> None:
        lam = elo_to_lambda_scale(1000)
        self.assertGreaterEqual(lam, 0.8)
        self.assertLess(lam, 1.5)

    def test_high_elo(self) -> None:
        lam = elo_to_lambda_scale(2200)
        self.assertGreater(lam, 2.0)
        self.assertLessEqual(lam, 3.0)


class TestInitFromFifa(unittest.TestCase):
    def test_basic(self) -> None:
        rankings = {"Brazil": 1, "Argentina": 2, "San Marino": 210}
        ratings = init_ratings_from_fifa(rankings)
        self.assertGreater(ratings["Brazil"], ratings["San Marino"])
        self.assertEqual(len(ratings), 3)

    def test_rank1_highest(self) -> None:
        rankings = {"Top": 1, "Mid": 50, "Low": 150}
        ratings = init_ratings_from_fifa(rankings)
        self.assertGreater(ratings["Top"], ratings["Mid"])
        self.assertGreater(ratings["Mid"], ratings["Low"])
