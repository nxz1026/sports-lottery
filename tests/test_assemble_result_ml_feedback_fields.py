"""Tests for predictor result — ML feedback 必需字段守门。

H1+H2（Round 8 扫到的塌方）：``_assemble_result`` 必须始终写入 ``probs`` /
``model_version`` / ``goal_line``，否则 ``_emit_ml_predictions`` 写到
``events.jsonl`` 的全是 ``{h:0,d:0,a:0}`` + ``"default"``，retrain 拿到的
"概率"永远为 0，数据塌方。

这些测试直接调用 ``calculate_prediction`` 走端到端，锁住 result 顶层键。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestAssembleResultMlFeedbackFields(unittest.TestCase):
    """H1+H2 fix: result 顶层必须有 probs / model_version / goal_line。"""

    def _make_match(self, **overrides):
        base = {
            "home": "Brazil",
            "away": "Argentina",
            "home_true_prob": 0.55,
            "draw_true_prob": 0.25,
            "away_true_prob": 0.20,
            "odds_data_available": True,
            "total_over_close": "2.5",
        }
        base.update(overrides)
        return base

    @patch("core.predictor.compute_onside_signals")
    def test_probs_present_with_h_d_a(self, mock_onside):
        """H1: result['probs'] = {h, d, a}，三个键齐全且都是 ≤1 的浮点。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.7},
            "away": {"onside_score": 0.4},
        }
        from core.predictor import calculate_prediction
        match = self._make_match(home_true_prob=0.55, draw_true_prob=0.22, away_true_prob=0.23)
        result = calculate_prediction(match, use_dixon_coles=False)

        self.assertIn("probs", result, "result 缺 probs → ml_feedback 数据塌方")
        probs = result["probs"]
        self.assertIn("h", probs)
        self.assertIn("d", probs)
        self.assertIn("a", probs)
        for k in ("h", "d", "a"):
            self.assertIsInstance(probs[k], float)
            self.assertGreaterEqual(probs[k], 0.0)
            self.assertLessEqual(probs[k], 1.0)
        # H/D/A 概率和 ≈ 1（允许 0.01 容差做四舍五入累积）
        total = probs["h"] + probs["d"] + probs["a"]
        self.assertAlmostEqual(total, 1.0, places=2)

    @patch("core.predictor._blend_ml_probs")
    @patch("core.predictor.compute_onside_signals")
    def test_model_version_rule_v1_without_ml(self, mock_onside, mock_blend):
        """H2: ml_proba=None 时 model_version = 'rule-v1'。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        # _blend_ml_probs 返 (h, d, a, ml_proba) — ml_proba=None 走规则基线
        mock_blend.return_value = (0.55, 0.25, 0.20, None)
        from core.predictor import calculate_prediction
        match = self._make_match()
        result = calculate_prediction(match, use_dixon_coles=False)
        self.assertIn("model_version", result)
        self.assertEqual(result["model_version"], "rule-v1")

    @patch("core.predictor._blend_ml_probs")
    @patch("core.predictor.compute_onside_signals")
    def test_model_version_ml_blend_with_weight(self, mock_onside, mock_blend):
        """H2: ml_proba 非空时 model_version = 'ml-blend-w{weight:.2f}'。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        # ml_proba 三元组（H/D/A），触发 ml-blend 路径
        mock_blend.return_value = (0.55, 0.25, 0.20, [0.5, 0.25, 0.25])
        from core.predictor import ML_CONFIG, calculate_prediction
        match = self._make_match()
        result = calculate_prediction(match, use_dixon_coles=False)
        self.assertIn("model_version", result)
        expected = "ml-blend-w{:.2f}".format(ML_CONFIG.get("blend_weight", 0.0))
        self.assertEqual(result["model_version"], expected)

    @patch("core.predictor.compute_onside_signals")
    def test_goal_line_derived_from_over_close_prefix_o(self, mock_onside):
        """goal_line 从 total_over_close='o2.5' 派生为 float 2.5。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        from core.predictor import calculate_prediction
        match = self._make_match(total_over_close="o2.5")
        result = calculate_prediction(match, use_dixon_coles=False)
        self.assertIn("goal_line", result)
        self.assertEqual(result["goal_line"], 2.5)
        self.assertIsInstance(result["goal_line"], float)

    @patch("core.predictor.compute_onside_signals")
    def test_goal_line_derived_from_over_close_plain(self, mock_onside):
        """goal_line 从 total_over_close='2.5'（无前缀）派生为 2.5。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        from core.predictor import calculate_prediction
        match = self._make_match(total_over_close="2.5")
        result = calculate_prediction(match, use_dixon_coles=False)
        self.assertIn("goal_line", result)
        self.assertEqual(result["goal_line"], 2.5)

    @patch("core.predictor.compute_onside_signals")
    def test_goal_line_absent_when_over_close_missing(self, mock_onside):
        """goal_line 在 total_over_close=None 时不写入，避免 None 污染下游。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        from core.predictor import calculate_prediction
        # 删除 total_over_close，让 _assemble_result 拿不到
        match = self._make_match()
        match.pop("total_over_close", None)
        result = calculate_prediction(match, use_dixon_coles=False)
        self.assertNotIn("goal_line", result,
            "total_over_close 缺失时 goal_line 不应存在")

    @patch("core.predictor.compute_onside_signals")
    def test_goal_line_skipped_when_parse_fails(self, mock_onside):
        """goal_line 在 total_over_close='garbage' 时静默跳过（不抛异常）。"""
        mock_onside.return_value = {
            "home": {"onside_score": 0.6},
            "away": {"onside_score": 0.6},
        }
        from core.predictor import calculate_prediction
        match = self._make_match(total_over_close="garbage")
        result = calculate_prediction(match, use_dixon_coles=False)
        # 解析失败不应写入 goal_line，但也不抛
        self.assertNotIn("goal_line", result)


if __name__ == "__main__":
    unittest.main()
