"""R33-5: scripts/core/predictor.py::_blend_ml_probs 4 测直测。

现状：ML 概率融合（P1/ML）函数本体 0 直测；仅在
test_assemble_result_ml_feedback_fields.py 中以 @patch mock 替换，
未直测本体。

D-1 契约（predictor.py:64-66）：predict_proba 抛异常时
`try/except Exception` 静默吞掉 + ml_proba=None + 走规则基线，**不能 raise**。

本轮补 4 测：
- ml_model=None → 透传 hp/dp/ap，ml_proba=None
- D-1 锁定：predict_proba 抛 RuntimeError → 吞异常 + ml_proba=None + hp/dp/ap 不变
- len(ml_proba) != 3 → 跳过 blend（hp/dp/ap 不变，ml_proba 非 None 透传）
- blend 后 _t ≤ 0 → 跳过归一化；w > 0 + 正常 3 元 → 加权 + 重归一化
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def predictor_mod(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "blend_ml_probs_test", REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _blend_ml_probs ────────────────────────


def test_blend_ml_probs_no_model_returns_none_and_passthrough(predictor_mod, monkeypatch):
    """R33-5：ml_model=None → 透传 hp/dp/ap，ml_proba=None（未加载 ML 模型）。"""
    monkeypatch.setattr(predictor_mod, "get_ml_model", lambda lk: None)
    h, d, a, ml = predictor_mod._blend_ml_probs(
        home_prob=0.5, draw_prob=0.25, away_prob=0.25,
        match={}, onside={}, sm=0.0,
        elo_ratings=None, host_country=None, league_key="epl")
    assert (h, d, a) == (0.5, 0.25, 0.25)
    assert ml is None


def test_blend_ml_probs_d1_swallow_runtime_error(predictor_mod, monkeypatch, caplog):
    """R33-5 D-1 锁定：predict_proba 抛 RuntimeError → 吞异常 + ml_proba=None + 透传。

    锁定 try/except Exception 行为：不能 raise；hp/dp/ap 保持调用者传入原值。
    """
    class BadModel:
        def predict_proba(self, vec):
            raise RuntimeError("model not loaded")
    monkeypatch.setattr(predictor_mod, "get_ml_model", lambda lk: BadModel())
    monkeypatch.setattr(predictor_mod, "extract_features", lambda m, c: [0.0] * 26)

    with caplog.at_level("WARNING"):
        h, d, a, ml = predictor_mod._blend_ml_probs(
            home_prob=0.6, draw_prob=0.2, away_prob=0.2,
            match={}, onside={}, sm=0.0,
            elo_ratings=None, host_country=None, league_key="epl")
    # 不抛异常 + 透传
    assert (h, d, a) == (0.6, 0.2, 0.2)
    assert ml is None
    # warning 记录
    assert any("ML blend failed" in r.message for r in caplog.records)


def test_blend_ml_probs_len_not_three_skips_blend(predictor_mod, monkeypatch):
    """R33-5：ml_proba 长度 != 3 → 跳过 blend（hp/dp/ap 不变，ml_proba 非 None 透传）。"""
    class TwoModel:
        def predict_proba(self, vec):
            return [0.5, 0.5]  # 仅 2 元素
    monkeypatch.setattr(predictor_mod, "get_ml_model", lambda lk: TwoModel())
    monkeypatch.setattr(predictor_mod, "extract_features", lambda m, c: [0.0] * 26)

    h, d, a, ml = predictor_mod._blend_ml_probs(
        home_prob=0.6, draw_prob=0.2, away_prob=0.2,
        match={}, onside={}, sm=0.0,
        elo_ratings=None, host_country=None, league_key="epl")
    # 跳过 blend：hp/dp/ap 不变 + ml_proba 透传（即使长度不对也不抛）
    assert (h, d, a) == (0.6, 0.2, 0.2)
    assert ml == [0.5, 0.5]


def test_blend_ml_probs_blend_with_weight_renormalizes(predictor_mod, monkeypatch):
    """R33-5：w > 0 + 正常 3 元 → 加权 + 重归一化（_t > 0）；blend 后 sum=1。

    hp=0.6, dp=0.2, ap=0.2, ml=[0.5, 0.25, 0.25], w=0.3：
    blended = (1-0.3)*0.6 + 0.3*0.5 = 0.42+0.15 = 0.57
              (1-0.3)*0.2 + 0.3*0.25 = 0.14+0.075 = 0.215
              (1-0.3)*0.2 + 0.3*0.25 = 0.14+0.075 = 0.215
    _t = 1.0 → 重归一化（同值）
    """
    class FakeModel:
        def predict_proba(self, vec):
            return [0.5, 0.25, 0.25]
    monkeypatch.setattr(predictor_mod, "get_ml_model", lambda lk: FakeModel())
    monkeypatch.setattr(predictor_mod, "extract_features", lambda m, c: [0.0] * 26)
    monkeypatch.setitem(predictor_mod.ML_CONFIG, "blend_weight", 0.3)

    h, d, a, ml = predictor_mod._blend_ml_probs(
        home_prob=0.6, draw_prob=0.2, away_prob=0.2,
        match={}, onside={}, sm=0.0,
        elo_ratings=None, host_country=None, league_key="epl")
    # 加权 + sum=1
    assert abs((h + d + a) - 1.0) < 1e-9
    assert abs(h - 0.57) < 1e-9
    assert abs(d - 0.215) < 1e-9
    assert abs(a - 0.215) < 1e-9
    assert ml == [0.5, 0.25, 0.25]