"""R43-5: scripts/predict.py::_compute_and_predict 4 tests direct + D-54 lock.

校准 + DC ρ 拟合 + 预测生成 helper 0 直测; run_league 调, 但 helper 自身契约
(4 返回元组 + use_dc=False 直走默认 ρ + fit_dc_rho 异常退回 DC_RHO) 未锁。

helper 含四条契约 (D-54):
- 返回 4-tuple: (calibration, calibration_offset, fitted_rho, predictions)
- use_dc=False -> fitted_rho = DC_RHO (不调 fit_dc_rho)
- use_dc=True + fit_dc_rho OK -> fitted_rho = fit_dc_rho(past) 返回值
- use_dc=True + fit_dc_rho 异常 -> 退回 DC_RHO + warn (silent fallback,
  不抛穿, 不让 run_league 5xx)
- predictions = _generate_predictions(future, calibration_offset, ...8 个 kwargs...)

4 tests (HIGH):
- happy: fit_dc_rho mock -> 4-tuple 各字段透传
- use_dc=False -> fitted_rho == DC_RHO (默认)
- fit_dc_rho 抛 RuntimeError -> 退回 DC_RHO, 不抛穿
- predictions 透传 _generate_predictions mock 输出 (键值锁定)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def predict_mod():
    """加载 scripts/predict.py (复用 R43-3 模式)。"""
    spec = importlib.util.spec_from_file_location(
        "predict_compute_test",
        REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-54 四契约 ────────────────────────


def test_compute_and_predict_d54_happy_4tuple(predict_mod, monkeypatch):
    """R43-5 D-54: happy path -> 4-tuple 各字段透传 (calibration / offset /
    fitted_rho / predictions)。"""
    fake_calibration = {"total_matches": 5, "home_win_rate": 0.5}
    fake_calibration_offset = {"sample_size": 5, "home_correction": 1.05}
    fake_fitted_rho = -0.12
    fake_predictions = [{"match": "Arsenal vs Chelsea",
                          "direction": "主胜", "stars": 4}]

    # 这些函数在源中被 `from core... import X` 形式 import, 然后模块本地引用;
    # 我们 patch predict_mod 上的引用 (后续 _compute_calibration 等的 import 路径
    # 走的是模块本地)
    monkeypatch.setattr(predict_mod, "_compute_calibration",
                        lambda past, future, league: (fake_calibration,
                                                      fake_calibration_offset))
    monkeypatch.setattr(predict_mod, "fit_dc_rho",
                        lambda past: fake_fitted_rho)
    monkeypatch.setattr(predict_mod, "_generate_predictions",
                        lambda *args, **kwargs: fake_predictions)
    monkeypatch.setattr(predict_mod, "load_ai_adjustments",
                        lambda league: {})

    # use_dc=True 让 fit_dc_rho 走实际路径
    out = predict_mod._compute_and_predict(
        past=[], future=[], league_key="epl",
        fifa_rankings={}, host_country="England",
        use_dc=True, elo_ratings={})
    assert len(out) == 4
    calibration, calibration_offset, fitted_rho, predictions = out
    assert calibration is fake_calibration
    assert calibration_offset is fake_calibration_offset
    assert fitted_rho == fake_fitted_rho
    assert predictions is fake_predictions


def test_compute_and_predict_d54_use_dc_false_uses_default_rho(predict_mod, monkeypatch):
    """R43-5 D-54: use_dc=False -> fitted_rho = DC_RHO (不调 fit_dc_rho)。"""
    call_count = {"n": 0}

    def fake_fit_dc_rho(past):
        call_count["n"] += 1
        return -999.0

    monkeypatch.setattr(predict_mod, "_compute_calibration",
                        lambda p, f, l: ({}, None))
    monkeypatch.setattr(predict_mod, "fit_dc_rho", fake_fit_dc_rho)
    monkeypatch.setattr(predict_mod, "_generate_predictions",
                        lambda *a, **kw: [])
    monkeypatch.setattr(predict_mod, "load_ai_adjustments", lambda league: {})

    _, _, fitted_rho, _ = predict_mod._compute_and_predict(
        past=[], future=[], league_key="epl",
        fifa_rankings={}, host_country="England",
        use_dc=False,  # 关键
        elo_ratings={})
    assert fitted_rho == predict_mod.DC_RHO  # 默认
    assert call_count["n"] == 0  # 不调 fit_dc_rho


def test_compute_and_predict_d54_fit_dc_rho_exception_falls_back(predict_mod, monkeypatch):
    """R43-5 D-54: fit_dc_rho 抛异常 -> 退回 DC_RHO (silent fallback, 不抛穿)。

    锁定: use_dc=True 时 fit_dc_rho 异常应被 try/except 吞, 不让 run_league 5xx。
    """
    def fake_fit_dc_rho(past):
        raise RuntimeError("simulated DC fit failure")

    monkeypatch.setattr(predict_mod, "_compute_calibration",
                        lambda p, f, l: ({}, None))
    monkeypatch.setattr(predict_mod, "fit_dc_rho", fake_fit_dc_rho)
    monkeypatch.setattr(predict_mod, "_generate_predictions",
                        lambda *a, **kw: [])
    monkeypatch.setattr(predict_mod, "load_ai_adjustments", lambda league: {})

    # 不应抛穿
    out = predict_mod._compute_and_predict(
        past=[], future=[], league_key="epl",
        fifa_rankings={}, host_country="England",
        use_dc=True,
        elo_ratings={})
    _, _, fitted_rho, _ = out
    # 退回默认
    assert fitted_rho == predict_mod.DC_RHO


def test_compute_and_predict_d54_pass_load_ai_adjustments_per_league(predict_mod, monkeypatch):
    """R43-5 D-54: load_ai_adjustments 按 league_key 拉 (locks AI 反馈分数联赛隔离)。

    P4: AI feedback 按联赛隔离加载; 不同 league 应分别调用, 防止跨联赛污染。
    """
    captured = {}

    def fake_load_ai_adjustments(league):
        captured["league"] = league
        return {"adjustment": 0.1}

    monkeypatch.setattr(predict_mod, "_compute_calibration",
                        lambda p, f, l: ({}, None))
    monkeypatch.setattr(predict_mod, "fit_dc_rho", lambda p: 0.0)
    monkeypatch.setattr(predict_mod, "_generate_predictions",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(predict_mod, "load_ai_adjustments", fake_load_ai_adjustments)

    predict_mod._compute_and_predict(
        past=[], future=[], league_key="la_liga",
        fifa_rankings={}, host_country="Spain",
        use_dc=True, elo_ratings={})
    assert captured["league"] == "la_liga"
