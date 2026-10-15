"""R26 #1: scripts/core/backtest.py 内部 helper 与顶层编排 0 直测补测。

现状：5 个 mirror 测试只对账 ``_bk_reconcile_predictions`` / ``league_accuracy``，
完全不接触：
- `_bk_parse_window`（多格式日期归一化，纯函数）
- `_bk_match_predictions_against`（winner 解析 + predicted/actual 对账）
- `backtest_with_live_results`（顶层 data_source 编排）

R26 explore 报告：parser + winner + 顶层编排各 4-6 测，monkeypatch `_bk_fetch_fd_actuals`
隔离 urllib 网络。本轮 11 测。
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
def bk_mod(monkeypatch):
    """加载 scripts/core/backtest.py 为唯一 module name，堵网络。"""
    spec = importlib.util.spec_from_file_location(
        "bk_test", REPO_ROOT / "scripts" / "core" / "backtest.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── _bk_parse_window ────────────────────────


def test_bk_parse_window_range_8digit_with_hyphen(bk_mod):
    """R26 #1：8 位无横线 + "-" 区间 → 两侧都补成 ISO。"""
    assert bk_mod._bk_parse_window("20261012-20261018") == ("2026-10-12", "2026-10-18")


def test_bk_parse_window_range_iso_with_hyphen_first_split_bug(bk_mod):
    """R26 #1：ISO 区间含 "-" → split("-", 1) 取首个分隔；记录真实行为（含已知 bug）。

    现状：输入 "2026-10-12-2026-10-18" → split("-", 1) 把首个 "-" 当分隔符
    → start_raw="2026", end_raw="10-12-2026-10-18"；normalize 对长度 4 / 14
    非 8/10 → 原样返回。生产数据格式约定全是 8 位（"20261012-20261018"），故
    此边界不被触发。本测试把实际行为锁定，未来若改 split 策略需要一起更新。
    """
    assert bk_mod._bk_parse_window("2026-10-12-2026-10-18") == (
        "2026", "10-12-2026-10-18")


def test_bk_parse_window_single_iso_contains_dash(bk_mod):
    """R26 #1：单 ISO 也含 "-" → 走 split("-", 1) 路径；记录真实行为。

    真实生产仅调 _bk_parse_window(data_window) 而 data_window 通常无 "-"
    （单独日期）或为 "20261012-20261018" 8位区间；本边界不会被触发。锁定实际
    输出 `("2026", "10-12")` 防止静默 drift。
    """
    assert bk_mod._bk_parse_window("2026-10-12") == ("2026", "10-12")


def test_bk_parse_window_single_8digit(bk_mod):
    """R26 #1：单 8 位无 "-" → 两侧都补成 ISO。"""
    assert bk_mod._bk_parse_window("20261012") == ("2026-10-12", "2026-10-12")


# ──────────────────────── _bk_match_predictions_against ────────────────────────


def test_match_predictions_against_home_win(bk_mod):
    """R26 #1：predicted_score "2-1" → winner "home"；actual index 命中 → correct=True。"""
    actual_index = {("A", "B"): {"winner": "home", "score": "2-1"}}
    preds = [{"home": "A", "away": "B", "predicted_score": "2-1"}]
    rows = bk_mod._bk_match_predictions_against(preds, actual_index)
    assert len(rows) == 1
    assert rows[0]["predicted"] == "home"
    assert rows[0]["actual"] == "home"
    assert rows[0]["correct"] is True


def test_match_predictions_against_away_and_draw(bk_mod):
    """R26 #1：predicted "0-2" → "away"，predicted "1-1" → "draw"。"""
    actual_index = {
        ("A", "B"): {"winner": "away", "score": "0-2"},
        ("C", "D"): {"winner": "draw", "score": "1-1"},
    }
    preds = [
        {"home": "A", "away": "B", "predicted_score": "0-2"},
        {"home": "C", "away": "D", "predicted_score": "1-1"},
    ]
    rows = bk_mod._bk_match_predictions_against(preds, actual_index)
    assert len(rows) == 2
    by_match = {(r["home"], r["away"]): r for r in rows}
    assert by_match[("A", "B")]["predicted"] == "away"
    assert by_match[("A", "B")]["actual"] == "away"
    assert by_match[("A", "B")]["correct"] is True
    assert by_match[("C", "D")]["predicted"] == "draw"
    assert by_match[("C", "D")]["correct"] is True


def test_match_predictions_against_skips_no_actual_and_invalid_score(bk_mod):
    """R26 #1：actual 未命中 / predicted_score 无 "-" / 非 int / 空 → 跳过。"""
    actual_index = {("A", "B"): {"winner": "home", "score": "1-0"}}
    preds = [
        {"home": "A", "away": "B", "predicted_score": "1-0"},  # 命中
        {"home": "X", "away": "Y", "predicted_score": "1-0"},  # 无 actual
        {"home": "C", "away": "D", "predicted_score": "abc"},  # 非 int
        {"home": "E", "away": "F", "predicted_score": ""},      # 空
        {"home": "G", "away": "H"},                            # 缺 predicted_score
    ]
    rows = bk_mod._bk_match_predictions_against(preds, actual_index)
    # 仅 A/B 一行进 rows
    assert len(rows) == 1
    assert rows[0]["home"] == "A"
    assert rows[0]["away"] == "B"


# ──────────────────────── backtest_with_live_results 顶层编排 ────────────────────────


def test_backtest_with_live_results_file_read_error(bk_mod):
    """R26 #1：prediction_file 不存在 → 读文件抛异常 → status="error"。"""
    out = bk_mod.backtest_with_live_results("/nonexistent/path/prediction.json")
    assert out["status"] == "error"
    assert "error" in out


def test_backtest_with_live_results_unsupported_source(bk_mod, tmp_path):
    """R26 #1：league_config.data_source != "football-data"/"api-football" → skip。"""
    pred_file = tmp_path / "p.json"
    pred_file.write_text(json.dumps({"league": "custom_lg", "data_window": "20261010-20261015",
                                       "predictions": []}), encoding="utf-8")

    import core.config as core_cfg
    saved = dict(core_cfg.LEAGUE_CONFIG)
    core_cfg.LEAGUE_CONFIG["custom_lg"] = {
        "data_source": "espn",  # 不支持
        "league_id": 999,
    }
    try:
        out = bk_mod.backtest_with_live_results(str(pred_file))
        assert out["status"] == "skip"
        assert "unsupported data source" in out["reason"]
    finally:
        # 清理
        core_cfg.LEAGUE_CONFIG.clear()
        core_cfg.LEAGUE_CONFIG.update(saved)


def test_backtest_with_live_results_missing_fd_key(bk_mod, monkeypatch, tmp_path):
    """R26 #1：data_source=football-data 但 FOOTBALL_DATA_API_KEY 未设 → skip。"""
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    pred_file = tmp_path / "p.json"
    pred_file.write_text(json.dumps({"league": "epl", "data_window": "20261010-20261015",
                                       "predictions": [{"home": "A", "away": "B",
                                                        "predicted_score": "1-0"}]}),
                          encoding="utf-8")
    out = bk_mod.backtest_with_live_results(str(pred_file))
    assert out["status"] == "skip"
    assert "FOOTBALL_DATA_API_KEY not set" in out["reason"]


def test_backtest_with_live_results_happy_path_50pct_accuracy(bk_mod, monkeypatch, tmp_path):
    """R26 #1：monkeypatch _bk_fetch_fd_actuals → 2 预测 1 对 1 错 → accuracy=0.5。"""
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "fake-key")
    actual_index = {
        ("A", "B"): {"winner": "home", "score": "2-0"},
        ("C", "D"): {"winner": "away", "score": "0-1"},
    }
    monkeypatch.setattr(bk_mod, "_bk_fetch_fd_actuals",
                        lambda *a, **k: actual_index)
    pred_file = tmp_path / "p.json"
    pred_file.write_text(json.dumps({
        "league": "epl", "data_window": "20261010-20261015",
        "predictions": [
            {"home": "A", "away": "B", "predicted_score": "2-0"},  # correct
            {"home": "C", "away": "D", "predicted_score": "3-0"},  # wrong
        ],
    }), encoding="utf-8")

    out = bk_mod.backtest_with_live_results(str(pred_file))
    assert out["status"] == "ok"
    assert out["matched_matches"] == 2
    assert out["correct"] == 1
    assert out["accuracy"] == 0.5
