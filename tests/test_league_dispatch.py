"""P1 回归测试：确保 league_key 正确传递到 calculate_prediction，

使各联赛的差异化 λ 乘数 (LEAGUE_LAMBDA_MULTIPLIER) 与 ρ (LEAGUE_DC_RHO) 真正生效。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.config import LEAGUE_DC_RHO, LEAGUE_LAMBDA_MULTIPLIER
from core.predictor import calculate_prediction


class TestLeagueSpecificParams(unittest.TestCase):
    """P1-1: league_key 必须驱动 λ/ρ 差异化。"""

    def _make_match(self, **overrides):
        base = {
            "home": "TeamA",
            "away": "TeamB",
            "home_true_prob": 0.5,
            "draw_true_prob": 0.25,
            "away_true_prob": 0.25,
            "odds_data_available": True,
        }
        base.update(overrides)
        return base

    @patch("core.predictor.compute_onside_signals")
    def test_lambda_differs_by_league(self, mock_onside):
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        # 德甲 λ 乘数应高于英超 → 同输入下 lambda_home 更大
        epl = calculate_prediction(self._make_match(), use_dixon_coles=False, league_key="epl")
        bundes = calculate_prediction(self._make_match(), use_dixon_coles=False, league_key="bundesliga")
        self.assertGreater(
            LEAGUE_LAMBDA_MULTIPLIER["bundesliga"], LEAGUE_LAMBDA_MULTIPLIER["epl"]
        )
        self.assertGreater(bundes["lambda_home"], epl["lambda_home"])

    @patch("core.predictor.compute_onside_signals")
    def test_reported_rho_is_league_specific(self, mock_onside):
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        seriea = calculate_prediction(self._make_match(), use_dixon_coles=True, league_key="seriea")
        epl = calculate_prediction(self._make_match(), use_dixon_coles=True, league_key="epl")
        # dixon_coles_league_rho 应反映对应联赛的 ρ
        self.assertEqual(seriea["dixon_coles_league_rho"], LEAGUE_DC_RHO["seriea"])
        self.assertEqual(epl["dixon_coles_league_rho"], LEAGUE_DC_RHO["epl"])
        self.assertNotEqual(seriea["dixon_coles_league_rho"], epl["dixon_coles_league_rho"])


class TestGeneratePredictionsForwardsLeague(unittest.TestCase):
    """P1-1: predict._generate_predictions 必须把 league_key 透传给 calculate_prediction。"""

    def test_league_key_forwarded(self):
        import predict

        future = [
            {"name": "A vs B", "home": "A", "away": "B"},
            {"name": "C vs D", "home": "C", "away": "D"},
        ]
        captured = {}

        def _fake_calc(match, **kwargs):
            captured.setdefault("league_keys", set()).add(kwargs.get("league_key"))
            return {"match": match.get("name")}

        with patch.object(predict, "calculate_prediction", side_effect=_fake_calc):
            predict._generate_predictions(
                future,
                calibration_offset=None,
                fifa_rankings={},
                host_country=None,
                use_dc=True,
                fitted_rho=0.2,
                elo_ratings={},
                league_key="ligue1",
            )
        self.assertEqual(captured["league_keys"], {"ligue1"})


if __name__ == "__main__":
    unittest.main()
