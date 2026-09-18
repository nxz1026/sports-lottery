from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

# 本文件此前依赖其它测试文件把 scripts/ 塞进 sys.path 的副作用：
# 整目录跑能过、单跑 `pytest tests/test_poisson.py` 直接 ModuleNotFoundError。
REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.model.poisson import (
    dixon_coles_match_probs,
    dixon_coles_pmf,
    fit_dc_rho,
    poisson_confidence_interval,
    poisson_pmf,
    tau_correction,
)


class TestPoissonPmf(unittest.TestCase):
    def test_basic_values(self) -> None:
        p0 = poisson_pmf(0, 1.0)
        self.assertAlmostEqual(p0, math.exp(-1.0), places=6)
        p1 = poisson_pmf(1, 1.0)
        self.assertAlmostEqual(p1, math.exp(-1.0), places=6)
        p2 = poisson_pmf(2, 1.0)
        self.assertAlmostEqual(p2, math.exp(-1.0) / 2, places=6)

    def test_negative_k(self) -> None:
        self.assertEqual(poisson_pmf(-1, 1.0), 0.0)

    def test_zero_lambda(self) -> None:
        self.assertEqual(poisson_pmf(0, 0.0), 1.0)
        self.assertEqual(poisson_pmf(1, 0.0), 0.0)

    def test_negative_lambda(self) -> None:
        self.assertEqual(poisson_pmf(0, -0.5), 1.0)
        self.assertEqual(poisson_pmf(2, -0.5), 0.0)

    def test_large_k(self) -> None:
        p = poisson_pmf(100, 1.0)
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)


class TestPoissonConfidenceInterval(unittest.TestCase):
    def test_small_lambda(self) -> None:
        lo, hi = poisson_confidence_interval(2.0)
        self.assertAlmostEqual(lo, 0.0, places=1)
        expected_hi = round(2.0 + 1.96 * math.sqrt(2.0 + 0.5), 1)
        self.assertAlmostEqual(hi, expected_hi, places=1)

    def test_large_lambda(self) -> None:
        lo, hi = poisson_confidence_interval(15.0)
        self.assertAlmostEqual(lo, 7.4, places=1)
        self.assertAlmostEqual(hi, 22.6, places=1)

    def test_zero_lambda(self) -> None:
        self.assertEqual(poisson_confidence_interval(0.0), (0.0, 0.0))

    def test_negative_lambda(self) -> None:
        self.assertEqual(poisson_confidence_interval(-1.0), (0.0, 0.0))

    def test_boundary_lambda(self) -> None:
        lo, hi = poisson_confidence_interval(10.0)
        self.assertGreater(hi, lo)
        self.assertGreaterEqual(lo, 0.0)


class TestTauCorrection(unittest.TestCase):
    def test_00(self) -> None:
        self.assertAlmostEqual(tau_correction(0, 0, 1.0, 1.0, 0.2), 1.0 - 0.2)

    def test_10(self) -> None:
        self.assertAlmostEqual(tau_correction(1, 0, 1.5, 2.0, 0.2), 1.0 + 0.2 * 2.0)

    def test_01(self) -> None:
        self.assertAlmostEqual(tau_correction(0, 1, 1.5, 2.0, 0.2), 1.0 + 0.2 * 1.5)

    def test_11(self) -> None:
        self.assertAlmostEqual(tau_correction(1, 1, 1.5, 2.0, 0.2), 1.0 - 0.2 * 1.5 * 2.0)

    def test_other(self) -> None:
        self.assertEqual(tau_correction(2, 3, 1.5, 2.0, 0.2), 1.0)
        self.assertEqual(tau_correction(0, 2, 1.5, 2.0, 0.2), 1.0)
        self.assertEqual(tau_correction(2, 0, 1.5, 2.0, 0.2), 1.0)

    def test_default_rho(self) -> None:
        self.assertAlmostEqual(tau_correction(0, 0, 1.0, 1.0), 0.8)

    def test_negative_tau_clamped(self) -> None:
        """P0-2: rho*λ_h*λ_a > 1 时 tau 应被钳位到 0，不能返回负概率。"""
        # rho=0.2, λ_h=3.0, λ_a=3.0 → 1 - 0.2*3*3 = -0.8 → clamp to 0
        self.assertEqual(tau_correction(1, 1, 3.0, 3.0, 0.2), 0.0)
        # rho=1.5, (0,0) → 1 - 1.5 = -0.5 → clamp to 0
        self.assertEqual(tau_correction(0, 0, 1.0, 1.0, 1.5), 0.0)

    def test_dixon_coles_pmf_never_negative(self) -> None:
        """P0-2: 任何参数组合下 dixon_coles_pmf 不应返回负值。"""
        for rho in [-0.3, 0.0, 0.2, 0.5]:
            for lam_h in [0.5, 1.5, 3.0]:
                for lam_a in [0.5, 1.5, 3.0]:
                    for h in range(3):
                        for a in range(3):
                            p = dixon_coles_pmf(h, a, lam_h, lam_a, rho)
                            self.assertGreaterEqual(p, 0.0,
                                f"Negative prob: h={h},a={a},λ_h={lam_h},λ_a={lam_a},ρ={rho}")


class TestDixonColesPmf(unittest.TestCase):
    def test_basic(self) -> None:
        p = dixon_coles_pmf(1, 0, 1.5, 0.8, 0.2)
        base = poisson_pmf(1, 1.5) * poisson_pmf(0, 0.8)
        tau = tau_correction(1, 0, 1.5, 0.8, 0.2)
        self.assertAlmostEqual(p, base * tau)

    def test_zero_goals(self) -> None:
        p = dixon_coles_pmf(0, 0, 0.0, 0.0, 0.2)
        base = poisson_pmf(0, 0.0) * poisson_pmf(0, 0.0)
        tau = tau_correction(0, 0, 0.0, 0.0, 0.2)
        self.assertAlmostEqual(p, base * tau)


class TestDixonColesMatchProbs(unittest.TestCase):
    def test_probs_sum_to_one(self) -> None:
        result = dixon_coles_match_probs(1.5, 0.8, 0.2, max_goals=8)
        total = result["home_win"] + result["draw"] + result["away_win"]
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_home_advantage(self) -> None:
        result = dixon_coles_match_probs(1.5, 0.8)
        self.assertGreater(result["home_win"], result["away_win"])

    def test_symmetric(self) -> None:
        result = dixon_coles_match_probs(1.0, 1.0)
        self.assertAlmostEqual(result["home_win"], result["away_win"], places=2)

    def test_returns_top_score_probs(self) -> None:
        result = dixon_coles_match_probs(1.5, 0.8)
        self.assertIn("score_probs", result)
        self.assertLessEqual(len(result["score_probs"]), 12)

    def test_strong_away(self) -> None:
        result = dixon_coles_match_probs(0.5, 2.0)
        self.assertGreater(result["away_win"], result["home_win"])


class TestFitDcRho(unittest.TestCase):
    def test_few_matches_returns_default(self) -> None:
        matches = [{"score": "1-0"}, {"score": "2-1"}]
        rho = fit_dc_rho(matches)
        self.assertEqual(rho, 0.2)

    def test_twenty_plus_matches(self) -> None:
        matches = [{"score": f"{i % 3}-{(i + 1) % 2}"} for i in range(25)]
        for m in matches:
            if len(m["score"].split("-")) != 2:
                m["score"] = "1-0"
            parts = m["score"].split("-")
            h, a = int(parts[0]), int(parts[1])
            if h == a == 0:
                m["score"] = "1-0"
        rho = fit_dc_rho(matches)
        self.assertIsInstance(rho, float)

    def test_handles_missing_score(self) -> None:
        matches: list[dict] = [{"result": ""}, {"score": None}]
        rho = fit_dc_rho(matches)
        self.assertEqual(rho, 0.2)

    def test_handles_invalid_score(self) -> None:
        matches = [{"score": "abc"}, {"score": "-1-2"}]
        rho = fit_dc_rho(matches)
        self.assertEqual(rho, 0.2)

    def test_uses_result_key(self) -> None:
        matches = [{"result": "2-0"} for _ in range(25)]
        rho = fit_dc_rho(matches)
        self.assertIsInstance(rho, float)


class TestTauClampWarnOnce(unittest.TestCase):
    """回归：钳位告警曾在热路径逐次记录。

    tau_correction 被 Monte Carlo 每场 × 每次模拟 × 每个比分格点调用，
    逐次 logger.warning 实测让单次 `--all` 产出 285MB / 330 万行日志，
    I/O 把作业拖到 600s 超时。现只记首次。
    """

    def setUp(self) -> None:
        import core.model.poisson as pm
        self.pm = pm
        self._saved = pm._tau_clamp_warned
        pm._tau_clamp_warned = False

    def tearDown(self) -> None:
        self.pm._tau_clamp_warned = self._saved

    def test_clamp_still_returns_zero(self) -> None:
        """钳位行为本身不变：tau<0 时返回 0。"""
        # h=1,a=1 时 tau = 1 - rho*λ_h*λ_a；取 rho=1.0, λ=2 → 1-4 = -3 < 0
        self.assertEqual(tau_correction(1, 1, 2.0, 2.0, rho=1.0), 0.0)

    def test_warns_only_once(self) -> None:
        """连续大量钳位只产生一条 warning。"""
        from unittest import mock
        with mock.patch.object(self.pm.logger, "warning") as w:
            for _ in range(500):
                tau_correction(1, 1, 2.0, 2.0, rho=1.0)
            self.assertEqual(w.call_count, 1, "热路径钳位告警必须只记首次")
        self.assertTrue(self.pm._tau_clamp_warned)

    def test_no_warning_when_not_clamped(self) -> None:
        """不触发钳位时不应有任何告警（也不能把标记置位）。"""
        from unittest import mock
        with mock.patch.object(self.pm.logger, "warning") as w:
            tau_correction(1, 1, 2.0, 2.0, rho=0.01)  # 1-0.04 > 0
            tau_correction(3, 2, 2.0, 2.0, rho=1.0)   # 非四类特例，直接返回 1.0
            self.assertEqual(w.call_count, 0)
        self.assertFalse(self.pm._tau_clamp_warned)

    def test_other_cells_unaffected(self) -> None:
        """非特例格点恒为 1.0，不受钳位逻辑影响。"""
        self.assertEqual(tau_correction(2, 0, 2.0, 2.0, rho=1.0), 1.0)
        self.assertEqual(tau_correction(0, 2, 2.0, 2.0, rho=1.0), 1.0)
