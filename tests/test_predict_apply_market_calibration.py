"""R33-2: scripts/core/predictor.py::_apply_market_calibration 5 测直测。

现状：Calibration 是预测准确率最大单杠杆；每次 predict 都会跑。0 直测。
9-tuple 返回结构（hp/dp/ap/dc_val/ohc/oac/home_onside/away_onside/note）。

本轮补 5 测：
- calibration_offset=None → 透传 hp/dp/ap，note=None，所有校正因子=1.0
- market 校正（home_correction/draw_correction/away_correction）→ 重归一化 sum=1
- onside 校正（onside_home_correction/onside_away_correction）→ 重归一化 sum=1
- total=0 边界（hp*hc + dp*dc + ap*ac = 0）→ 跳过 market 归一化（防 div-by-zero）
- note 字符串含 n={sample_size} + home×/draw×/away× 倍数（debug-friendly 锁定）
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
def predictor_mod():
    spec = importlib.util.spec_from_file_location(
        "apply_market_calibration_test",
        REPO_ROOT / "scripts" / "core" / "predictor.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _apply_market_calibration ────────────────────────


def test_apply_market_calibration_none_passthrough(predictor_mod):
    """R33-2：calibration_offset=None → hp/dp/ap 透传，note=None，所有校正因子=1.0。"""
    hp, dp, ap, dc_val, ohc, oac, h_on, a_on, note = predictor_mod._apply_market_calibration(
        hp=0.5, dp=0.25, ap=0.25,
        calibration_offset=None,
        home_onside=0.6, away_onside=0.4,
    )
    assert hp == 0.5 and dp == 0.25 and ap == 0.25
    assert dc_val == 1.0 and ohc == 1.0 and oac == 1.0
    assert h_on == 0.6 and a_on == 0.4
    assert note is None


def test_apply_market_calibration_dict_corrects_and_renormalizes(predictor_mod):
    """R33-2：dict 含 home_correction/draw_correction/away_correction → 重归一化 sum=1。

    hp=0.5, dp=0.25, ap=0.25 + hc=1.10, dc=0.95, ac=1.05：
    corrected: 0.55, 0.2375, 0.2625 → total=1.05 → 重归一化 0.5238, 0.2262, 0.2500
    """
    hp, dp, ap, dc_val, ohc, oac, h_on, a_on, note = predictor_mod._apply_market_calibration(
        hp=0.5, dp=0.25, ap=0.25,
        calibration_offset={"home_correction": 1.10, "draw_correction": 0.95,
                            "away_correction": 1.05, "sample_size": 30},
        home_onside=0.6, away_onside=0.4,
    )
    # 重归一化后 sum = 1
    assert abs((hp + dp + ap) - 1.0) < 1e-9
    # 主胜概率上调（hc=1.10 > 1）
    assert hp > 0.5
    # 校正因子透传
    assert dc_val == 0.95
    assert ohc == 1.0 and oac == 1.0  # 未设 onside 校正
    # onside 透传（未校正）
    assert h_on == 0.6 and a_on == 0.4
    # note 含 sample_size
    assert note is not None and "n=30" in note and "1.1" in note


def test_apply_market_calibration_dict_onside_corrections_renormalizes(predictor_mod):
    """R33-2：dict 含 onside_home_correction/onside_away_correction → onside 各自归一化。

    home_onside=0.7, away_onside=0.3 + ohc=0.8, oac=1.2：
    corrected: 0.56, 0.36 → total=0.92 → home=0.6087, away=0.3913（sum=1.0）
    """
    hp, dp, ap, dc_val, ohc, oac, h_on, a_on, note = predictor_mod._apply_market_calibration(
        hp=0.5, dp=0.25, ap=0.25,
        calibration_offset={"home_correction": 1.10,
                            "onside_home_correction": 0.8,
                            "onside_away_correction": 1.2,
                            "sample_size": 30},
        home_onside=0.7, away_onside=0.3,
    )
    # onside 重归一化 sum = 1
    assert abs((h_on + a_on) - 1.0) < 1e-9
    assert abs(h_on - 0.6086957) < 1e-5
    assert abs(a_on - 0.3913043) < 1e-5
    # ohc/oac 透传
    assert ohc == 0.8 and oac == 1.2


def test_apply_market_calibration_total_zero_skips_renormalization(predictor_mod):
    """R33-2：total_corrected=0 边界 → 跳过 market 归一化（防 div-by-zero）。

    hp=dp=ap=0 配 hc=dc=ac=0 → total=0 → 不归一化 → hp/dp/ap 不变。
    """
    hp, dp, ap, dc_val, ohc, oac, h_on, a_on, note = predictor_mod._apply_market_calibration(
        hp=0.0, dp=0.0, ap=0.0,
        calibration_offset={"home_correction": 0, "draw_correction": 0,
                            "away_correction": 0, "sample_size": 5},
        home_onside=0.5, away_onside=0.5,
    )
    # 跳过归一化：hp/dp/ap 保持 0
    assert hp == 0.0 and dp == 0.0 and ap == 0.0
    # 校正因子仍透传（即便 total=0）
    assert dc_val == 0.0
    # onside 50/50 透传（total_on=1 > 0 走归一化，结果仍是 0.5/0.5）
    assert h_on == 0.5 and a_on == 0.5
    # note 仍生成
    assert note is not None and "n=5" in note


def test_apply_market_calibration_note_format_locked(predictor_mod):
    """R33-2：note 字符串格式锁定 — 含 n={n} + home×{hc}/draw×{dc}/away×{ac}。

    debug-friendly 字段，下游日志 grep 依赖此格式。
    """
    _, _, _, _, _, _, _, _, note = predictor_mod._apply_market_calibration(
        hp=0.5, dp=0.25, ap=0.25,
        calibration_offset={"home_correction": 1.20, "draw_correction": 0.90,
                            "away_correction": 1.05, "sample_size": 50},
        home_onside=0.5, away_onside=0.5,
    )
    # note 格式：calibration applied (n=50, home×1.2/draw×0.9/away×1.05)
    assert note is not None
    assert "n=50" in note
    assert "home" in note and "1.2" in note
    assert "draw" in note and "0.9" in note
    assert "away" in note and "1.05" in note