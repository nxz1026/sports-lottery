"""ML-7: 验证 ML 模型训练/推理正确，以及 predictor 中 ML 概率融合真正生效。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import ML_CONFIG
from core.model.features import NUM_FEATURES
from core.model.ml_model import MatchMLModel, load_league_model, save_league_model
from core.predictor import _ML_MODEL_CACHE, calculate_prediction


def _make_match(**overrides):
    base = {
        "home": "A", "away": "B",
        "home_true_prob": 0.5, "draw_true_prob": 0.25, "away_true_prob": 0.25,
        "odds_data_available": True,
    }
    base.update(overrides)
    return base


class TestMLModel(unittest.TestCase):
    """ML-1: 多分类器基本正确性。"""

    def test_predict_proba_sums_to_one(self):
        model = MatchMLModel(n_features=NUM_FEATURES)
        # 用随机但固定的特征训练
        X = [[i * 0.01 * (c + 1) for c in range(NUM_FEATURES)] for i in range(60)]
        y = [(i % 3) for i in range(60)]
        model.fit(X, y)
        proba = model.predict_proba(X[0])
        self.assertEqual(len(proba), 3)
        self.assertAlmostEqual(sum(proba), 1.0, places=5)

    def test_separable_data_is_learnable(self):
        # 构造强可分特征：第 0 维决定类别
        X, y = [], []
        for i in range(90):
            cls = i % 3
            row = [0.0] * NUM_FEATURES
            row[0] = 1.0 if cls == 0 else (-1.0 if cls == 1 else 0.0)
            X.append(row)
            y.append(cls)
        model = MatchMLModel(n_features=NUM_FEATURES, epochs=200)
        model.fit(X, y)
        # 类别 0 样本应给出最高 home 概率
        self.assertEqual(max(range(3), key=lambda c: model.predict_proba(X[0])[c]), 0)

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            model = MatchMLModel(n_features=NUM_FEATURES)
            model.fit([[0.1] * NUM_FEATURES, [0.2] * NUM_FEATURES], [0, 1])
            save_league_model(model, "unittest", base_dir=base)
            loaded = load_league_model("unittest", base_dir=base)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.n_features, NUM_FEATURES)
            self.assertAlmostEqual(
                loaded.predict_proba([0.1] * NUM_FEATURES)[0],
                model.predict_proba([0.1] * NUM_FEATURES)[0], places=5,
            )


class TestPredictorMLBlend(unittest.TestCase):
    """ML-3: predictor 在存在联赛模型时输出 ML 概率并融合。"""

    def setUp(self):
        ML_CONFIG["enabled"] = True
        _ML_MODEL_CACHE.clear()
        # 构造并保存一个测试联赛模型
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        X = [[(i % 3) * 0.5 - 0.5] + [0.0] * (NUM_FEATURES - 1) for i in range(90)]
        y = [i % 3 for i in range(90)]
        model = MatchMLModel(n_features=NUM_FEATURES)
        model.fit(X, y)
        save_league_model(model, "mltest", base_dir=base)
        self._base = base

    def tearDown(self):
        _ML_MODEL_CACHE.clear()
        self._tmp.cleanup()
        ML_CONFIG["enabled"] = True

    @patch("core.predictor.load_league_model")
    def test_ml_proba_present_when_model_exists(self, mock_load):
        # 强制 get_ml_model 返回我们保存的模型
        saved = load_league_model("mltest", base_dir=self._base)
        mock_load.return_value = saved
        _ML_MODEL_CACHE["mltest"] = saved

        with patch("core.predictor.compute_onside_signals") as mock_onside:
            mock_onside.return_value = {"home": {"onside_score": 0.6}, "away": {"onside_score": 0.6}}
            result = calculate_prediction(_make_match(), use_dixon_coles=False, league_key="mltest")
        self.assertTrue(result["ml_model_used"])
        self.assertIsNotNone(result["ml_proba"])
        self.assertEqual(len(result["ml_proba"]), 3)

    def test_ml_disabled_returns_none(self):
        ML_CONFIG["enabled"] = False
        with patch("core.predictor.compute_onside_signals") as mock_onside:
            mock_onside.return_value = {"home": {"onside_score": 0.6}, "away": {"onside_score": 0.6}}
            result = calculate_prediction(_make_match(), use_dixon_coles=False, league_key="mltest")
        self.assertFalse(result["ml_model_used"])
        self.assertIsNone(result["ml_proba"])


if __name__ == "__main__":
    unittest.main()
