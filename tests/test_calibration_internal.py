"""R27 #1: scripts/core/calibration.py 5 内部 helper 直测。

现状：test_calibration.py 5 测仅覆盖 _parse_score / compute_calibration_offset /
build_calibration / append_historical_past_matches 4 个公开/计算层函数；本文件的
5 个底层 helper 全部 0 直测：
- _calibration_cache_file（league 路由 vs 全局）
- _load_prev_offset（按联赛隔离 P1-2）
- _save_offset（_saved_at / _league 戳 + 异常吞掉）
- _collect_calibration_samples（非法比分边界）
- _derive_offsets（指数平滑 0.7/0.3 + clamp 0.5-2.0）

这些是 R13 #7 / R23 #1 反复 revert 改 P0-1 / P1-2 的核心载体；本轮补 11 测锁定
行为防 drift。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def cal_mod(tmp_path, monkeypatch):
    """加载 calibration.py 为唯一 module name，FOOTBALL_DIR/PREDICTIONS_DIR 重定向 tmp。"""
    spec = importlib.util.spec_from_file_location(
        "cal_test", REPO_ROOT / "scripts" / "core" / "calibration.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # FOOTBALL_DIR + PREDICTIONS_DIR 是 from-import 别名 → monkeypatch 本 module 属性
    fake_root = tmp_path
    monkeypatch.setattr(mod, "FOOTBALL_DIR", fake_root)
    (fake_root / "references").mkdir()
    (fake_root / "predictions").mkdir()
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", fake_root / "predictions")
    return mod, fake_root


# ──────────────────────── _calibration_cache_file ────────────────────────


def test_calibration_cache_file_league_routing(cal_mod):
    """R27 #1：league="epl" → 文件名含 _epl 后缀；None → 全局无后缀。"""
    mod, _ = cal_mod
    epl_path = mod._calibration_cache_file("epl")
    global_path = mod._calibration_cache_file(None)
    assert epl_path.name == ".calibration_state_epl.json"
    assert global_path.name == ".calibration_state.json"
    # league 互不串
    assert epl_path != global_path


# ──────────────────────── _load_prev_offset ────────────────────────


def test_load_prev_offset_returns_none_when_file_missing(cal_mod):
    """R27 #1：cache_file 不存在 → 返 None 不抛穿。"""
    mod, _ = cal_mod
    assert mod._load_prev_offset("epl") is None


def test_load_prev_offset_returns_dict_when_valid(cal_mod):
    """R27 #1：写一个合法 dict → 读回相同内容。"""
    mod, root = cal_mod
    target = root / "references" / ".calibration_state_epl.json"
    target.write_text(json.dumps({
        "home_correction": 1.05, "sample_size": 25,
    }), encoding="utf-8")
    out = mod._load_prev_offset("epl")
    assert out == {"home_correction": 1.05, "sample_size": 25}


def test_load_prev_offset_returns_none_on_invalid_json(cal_mod, caplog):
    """R27 #1：cache_file 是坏 JSON → _try_load_json 返 None → 函数返 None + 走 except 不抛。"""
    mod, root = cal_mod
    target = root / "references" / ".calibration_state_epl.json"
    target.write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        out = mod._load_prev_offset("epl")
    # 顶层坏 JSON 即 None
    assert out is None


def test_load_prev_offset_returns_none_for_top_level_list(cal_mod):
    """R27 #1：顶层是 list（不是 dict）→ 返 None（contract 校验拒绝）。"""
    mod, root = cal_mod
    target = root / "references" / ".calibration_state_epl.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert mod._load_prev_offset("epl") is None


def test_load_prev_offset_league_isolation(cal_mod):
    """R27 #1：P1-2 修复 — epl / laliga 各加载各的 file，互不串。"""
    mod, root = cal_mod
    (root / "references" / ".calibration_state_epl.json").write_text(
        json.dumps({"home_correction": 1.1, "sample_size": 30}), encoding="utf-8")
    (root / "references" / ".calibration_state_laliga.json").write_text(
        json.dumps({"home_correction": 0.9, "sample_size": 20}), encoding="utf-8")
    epl = mod._load_prev_offset("epl")
    laliga = mod._load_prev_offset("laliga")
    assert epl["sample_size"] == 30
    assert laliga["sample_size"] == 20
    assert epl["home_correction"] != laliga["home_correction"]


# ──────────────────────── _save_offset ────────────────────────


def test_save_offset_writes_with_saved_at_and_league(cal_mod):
    """R27 #1：调用后 file 写入 dict 含 _saved_at + _league 戳，且原始 offset 字段在。"""
    mod, root = cal_mod
    mod._save_offset({"home_correction": 1.05, "sample_size": 30}, league="epl")
    target = root / "references" / ".calibration_state_epl.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["home_correction"] == 1.05
    assert payload["sample_size"] == 30
    assert payload["_league"] == "epl"
    assert "_saved_at" in payload
    # _saved_at 形如 2026-10-15T12:34:56Z
    assert len(payload["_saved_at"]) == 20


def test_save_offset_creates_parent_dir(cal_mod):
    """R27 #1：references/ 不存在 → 自动 mkdir parents=True。"""
    mod, root = cal_mod
    (root / "references").rmdir()
    assert not (root / "references").exists()
    mod._save_offset({"sample_size": 5}, league="test")
    assert (root / "references" / ".calibration_state_test.json").exists()


def test_save_offset_swallows_oserror(cal_mod, monkeypatch, caplog):
    """R27 #1：open 抛 OSError → 吞掉 + logger.warning，不抛穿。"""
    mod, _ = cal_mod

    def bad_open(*args, **kwargs):
        raise OSError("写不动")

    monkeypatch.setattr("builtins.open", bad_open)
    with caplog.at_level("WARNING"):
        mod._save_offset({"sample_size": 5}, league="epl")
    # 应有 warning 但函数不抛
    assert any("Failed to save calibration offset" in r.message for r in caplog.records)


# ──────────────────────── _derive_offsets ────────────────────────


def test_derive_offsets_basic_no_prev_smoothing(cal_mod, monkeypatch, caplog):
    """R27 #1：prev=None（无 cache file）→ 不打 smoothing 日志；home/draw/away 直接计算 + clamp。"""
    mod, _ = cal_mod
    # _load_prev_offset 默认 None（无 cache file）→ 直接计算
    with caplog.at_level("INFO"):
        out = mod._derive_offsets(0.50, 0.25, 0.25, total=30, league="epl")
    # actual 0.50 / baseline 0.45 = 1.111 → clamp 在 [0.5, 2.0]
    assert 0.5 <= out["home_correction"] <= 2.0
    assert "Exponential smoothing applied" not in caplog.text
    assert out["sample_size"] == 30


def test_derive_offsets_exponential_smoothing_applied(cal_mod, monkeypatch, caplog):
    """R27 #1：prev_offset 存在 → 指数平滑 0.7·prev + 0.3·current 应用。"""
    mod, _ = cal_mod
    # monkeypatch _load_prev_offset 返一份 prev
    prev = {
        "home_correction": 2.0,
        "draw_correction": 1.0,
        "away_correction": 1.0,
        "sample_size": 50,
    }
    monkeypatch.setattr(mod, "_load_prev_offset", lambda *a, **k: prev)

    # current actual_home_rate = 0.45 (== baseline) → current home_correction=1.0
    # smoothed = 0.7 * 2.0 + 0.3 * 1.0 = 1.7
    with caplog.at_level("INFO"):
        out = mod._derive_offsets(0.45, 0.25, 0.30, total=50, league="epl")
    assert abs(out["home_correction"] - 1.7) < 0.01
    assert "Exponential smoothing applied" in caplog.text


def test_derive_offsets_clamp_upper_bound(cal_mod, monkeypatch):
    """R27 #1：actual_home_rate → ∞ → home_correction → clamp 到 2.0（不上溢）。"""
    mod, _ = cal_mod
    # actual 0.99 / baseline 0.45 = 2.2 → clamp 到 2.0
    monkeypatch.setattr(mod, "_load_prev_offset", lambda *a, **k: None)
    out = mod._derive_offsets(0.99, 0.25, 0.30, total=200, league="epl")
    assert out["home_correction"] == 2.0


# ──────────────────────── _collect_calibration_samples ────────────────────────


def test_collect_calibration_samples_basic_counts(cal_mod):
    """R27 #1：主胜 / 平 / 客胜 三类计数；空 → 全 0。"""
    mod, _ = cal_mod
    assert mod._collect_calibration_samples([]) == (0, 0, 0)
    samples = [
        {"score": "2-1"},  # home
        {"score": "0-0"},  # draw
        {"score": "1-2"},  # away
        {"score": "3-2"},  # home
        {"score": "1-1"},  # draw
    ]
    assert mod._collect_calibration_samples(samples) == (2, 2, 1)


def test_collect_calibration_samples_skips_invalid_scores(cal_mod):
    """R27 #1：缺 score / 空 / 无 "-" / 多 dash "3-2-1" / 非整数 "abc" / "2.5" → 全部 skip。"""
    mod, _ = cal_mod
    samples = [
        {"score": ""},                    # 空
        {},                                # 缺 score
        {"score": "no-dash"},             # 无 "-"
        {"score": "3-2-1"},                # 多 dash
        {"score": "abc-def"},              # 非整数
        {"score": "2.5-1.5"},              # 小数
        {"score": "1-0"},                  # 仅一条有效 (home)
    ]
    h, d, a = mod._collect_calibration_samples(samples)
    # 仅 1-0 进 home 胜
    assert (h, d, a) == (1, 0, 0)
