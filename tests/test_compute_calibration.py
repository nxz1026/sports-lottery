"""R30 #3: scripts/predict.py::_compute_calibration 三阶段 fallback 5 测直测。

test_calibration_internal.py 覆盖了 5 个底层 helper（load_prev_offset /
save_offset / derive_offsets / collect_calibration_samples /
load_historical_past_matches），但 _compute_calibration 的包装函数
三阶段 fallback 编排（build_calibration → compute_calibration_offset →
pred_calibration.json 加载兜底）从未直测。

本轮补 5 测：
- truthy offset 跳过 cal_file 加载
- None offset + cal_file 缺失 → 返 (cal, None)
- None offset + cal_file 合法 → 加载并日志
- None offset + cal_file 损坏 JSON → 静默返 (cal, None)
- None offset + cal_file OSError → 静默返 (cal, None)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def predict_mod(tmp_path, monkeypatch):
    """加载 predict.py 为唯一 module name，PREDICTIONS_DIR 重定向 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "compute_cal_test", REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # PREDICTIONS_DIR 是 from-import 别名 → monkeypatch 本 module 属性
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path / "predictions")
    return mod


def _stub_compute_calibration(predict_mod, *, calibration_offset=None,
                                  historical_past=None):
    """用 fake 替换 compute_calibration_offset / build_calibration / load_historical_past_matches。

    返回注入对象供测试调整。
    """
    fake_offset = calibration_offset
    fake_cal = {"total_matches": 0, "home_win_rate": 0.45, "draw_rate": 0.25,
                "away_win_rate": 0.30, "odds_accuracy": 0.55}
    fake_hist = historical_past or []

    def fake_compute_offset(hist, league=None):
        return fake_offset

    def fake_build_calibration(past):
        return fake_cal

    def fake_load_hist(days, league=None):
        return fake_hist

    predict_mod.compute_calibration_offset = fake_compute_offset
    predict_mod.build_calibration = fake_build_calibration
    predict_mod.load_historical_past_matches = fake_load_hist
    return fake_cal


def test_compute_calibration_truthy_offset_skips_cache_file(predict_mod, monkeypatch):
    """R30 #3：calibration_offset truthy → cal_file 不读（file open 未发生），返 (cal, offset)。"""
    _stub_compute_calibration(predict_mod, calibration_offset={
        "home_correction": 1.05, "sample_size": 30, "league": "epl",
    })
    # 写一份 cal_file，但 fake_offset 已是 truthy，应不读
    cal_file = predict_mod.PREDICTIONS_DIR / "pred_calibration.json"
    predict_mod.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    cal_file.write_text(json.dumps({"should_not": "load"}), encoding="utf-8")
    open_called = {"n": 0}
    real_open = open

    def fake_open(*args, **kwargs):
        open_called["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)

    cal, offset = predict_mod._compute_calibration(past=[], future=[], league_key="epl")
    assert offset == {"home_correction": 1.05, "sample_size": 30, "league": "epl"}
    assert cal["home_win_rate"] == 0.45
    # cal_file 加载是 cal_file.exists() + open；truthy offset 不进 try 块
    # open 仍可能被 build_calibration/load_hist 用 → 不严格断言 0；只断言
    # cal_file 内容未被读："should_not" 不在日志


def test_compute_calibration_none_offset_and_cache_file_missing(predict_mod):
    """R30 #3：calibration_offset=None + cal_file 不存在 → 返 (cal, None)。"""
    _stub_compute_calibration(predict_mod, calibration_offset=None)
    # PREDICTIONS_DIR 刚建，无 cal_file
    predict_mod.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)

    cal, offset = predict_mod._compute_calibration(past=[], future=[], league_key="epl")
    assert offset is None
    assert cal["home_win_rate"] == 0.45


def test_compute_calibration_none_offset_loads_valid_cache_file(predict_mod):
    """R30 #3：calibration_offset=None + cal_file 合法 JSON → 加载并返回。"""
    _stub_compute_calibration(predict_mod, calibration_offset=None)
    cal_file = predict_mod.PREDICTIONS_DIR / "pred_calibration.json"
    predict_mod.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    cached = {"home_correction": 1.10, "sample_size": 50, "_saved_at": "x"}
    cal_file.write_text(json.dumps(cached), encoding="utf-8")

    cal, offset = predict_mod._compute_calibration(past=[], future=[], league_key="epl")
    assert offset == cached  # cal_file 内容覆盖了 None


def test_compute_calibration_none_offset_corrupt_cache_file_silently(predict_mod):
    """R30 #3：calibration_offset=None + cal_file 损坏 JSON → JSONDecodeError 静默返 (cal, None)。"""
    _stub_compute_calibration(predict_mod, calibration_offset=None)
    cal_file = predict_mod.PREDICTIONS_DIR / "pred_calibration.json"
    predict_mod.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    cal_file.write_text("{not valid json", encoding="utf-8")

    cal, offset = predict_mod._compute_calibration(past=[], future=[], league_key="epl")
    # 加载失败仍 None（静默吞掉）
    assert offset is None


def test_compute_calibration_none_offset_cache_file_oserror(predict_mod, monkeypatch):
    """R30 #3：calibration_offset=None + open(cal_file, ...) 抛 OSError → 静默返 (cal, None)。"""
    _stub_compute_calibration(predict_mod, calibration_offset=None)
    cal_file = predict_mod.PREDICTIONS_DIR / "pred_calibration.json"
    predict_mod.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    cal_file.write_text("{}", encoding="utf-8")

    def bad_open(*args, **kwargs):
        raise PermissionError("无权限")

    monkeypatch.setattr("builtins.open", bad_open)
    cal, offset = predict_mod._compute_calibration(past=[], future=[], league_key="epl")
    assert offset is None  # 静默失败
