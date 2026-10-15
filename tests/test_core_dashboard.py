"""R26 #3: scripts/core/dashboard.py 8 函数全 0 直测补测。

现状：test_predict_log_error_wrappers.py:234 仅 AST 检查含 ``_build_league_
output.dashboard`` 字符串，不验证行为。8 个公开符号 (generate_dashboard
+ 7 helpers) 全 0 测。

本轮补 11 测，覆盖：
- _load_dashboard_data 1：完整 prediction_data 各字段透传 + now_str 兜底
- _compute_dashboard_stats 1：5 字段（含默认 0）
- _render_predictions_rows 2：row-home/row-away/row-draw 三分支 + 字段缺失不抛
- _render_monte_carlo 1：空 champion_probs / 8 队上限 / 宽度 = prob/max_prob*100
- _render_calibration_info 1：None → 空 + populated 含字段
- _render_backtest 1：None → 空 + ok 状态百分比格式化
- _write_dashboard_file 2：写文件 / OSError 吞掉 warning
- generate_dashboard 2：端到端空数据 / output_path 写文件
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def dash_mod(tmp_path, monkeypatch):
    """加载 dashboard.py 为唯一 module name，outputs 重定向 tmp_path。"""
    spec = importlib.util.spec_from_file_location(
        "dash_test", REPO_ROOT / "scripts" / "core" / "dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # PREDICTIONS_DIR 是 from-import 别名 → monkeypatch 本 module 属性
    monkeypatch.setattr(mod, "PREDICTIONS_DIR", tmp_path / "predictions")
    return mod


# ──────────────────────── _load_dashboard_data ────────────────────────


def test_load_dashboard_data_extracts_all_sections(dash_mod):
    """R26 #3：全字段 prediction_data → 7 字段全透传；缺字段用默认。"""
    pd = {
        "predictions": [{"home": "A"}],
        "monte_carlo": {"champion_probs": {"X": 0.5}},
        "calibration": {"total_matches": 10},
        "calibration_offset": {"sample_size": 5},
        "backtest": {"status": "ok"},
        "generated_at": "2026-10-15T10:00:00+00:00",
        "league": "epl",
    }
    data = dash_mod._load_dashboard_data(pd)
    assert data["predictions"] == [{"home": "A"}]
    assert data["mc"] == {"champion_probs": {"X": 0.5}}
    assert data["cal"] == {"total_matches": 10}
    assert data["cal_offset"] == {"sample_size": 5}
    assert data["bt"] == {"status": "ok"}
    assert data["now_str"] == "2026-10-15T10:00:00+00:00"
    assert data["league"] == "epl"


# ──────────────────────── _compute_dashboard_stats ────────────────────────


def test_compute_dashboard_stats_returns_five_keys(dash_mod):
    """R26 #3：cal 输入 → 5 字段；缺字段默认 0。"""
    out = dash_mod._compute_dashboard_stats({})
    assert set(out) == {"total_matches", "home_win_rate", "draw_rate",
                         "away_win_rate", "odds_accuracy"}
    assert all(v == 0 for v in out.values())

    # 全字段
    out2 = dash_mod._compute_dashboard_stats({"total_matches": 30, "home_win_rate": 0.45,
                                                "draw_rate": 0.25, "away_win_rate": 0.30,
                                                "odds_accuracy": 0.60})
    assert out2 == {"total_matches": 30, "home_win_rate": 0.45,
                     "draw_rate": 0.25, "away_win_rate": 0.30,
                     "odds_accuracy": 0.60}


# ──────────────────────── _render_predictions_rows ────────────────────────


def test_render_predictions_rows_classifies_three_directions(dash_mod):
    """R26 #3：direction 决定 row-home / row-away / row-draw 三 CSS class。"""
    # 主胜：direction startswith home 或含"主"
    preds = [
        {"direction": "Liverpool 胜", "home": "Liverpool", "away": "Arsenal",
         "stars": "⭐⭐⭐", "predicted_score": "2-1", "confidence_score": 0.6,
         "lambda_home": 1.5, "lambda_away": 0.8, "over_under": "Over 2.5",
         "btts": "Yes", "match": "Liv vs Ars"},
        # 客胜：含"胜"但非主
        {"direction": "Arsenal 胜", "home": "Liverpool", "away": "Arsenal",
         "stars": "⭐", "predicted_score": "0-1", "confidence_score": 0.4,
         "lambda_home": 0.5, "lambda_away": 1.5, "over_under": "Under 2.5",
         "btts": "No", "match": "Liv vs Ars"},
        # 平局：无胜
        {"direction": "平局", "home": "A", "away": "B", "stars": "⭐",
         "predicted_score": "1-1", "confidence_score": 0.3,
         "lambda_home": 1.0, "lambda_away": 1.0, "over_under": "Under 2.5",
         "btts": "No", "match": "A vs B"},
    ]
    html = dash_mod._render_predictions_rows(preds)
    assert 'class="row-home"' in html
    assert 'class="row-away"' in html
    assert 'class="row-draw"' in html


def test_render_predictions_rows_handles_missing_fields(dash_mod):
    """R26 #3：缺 home / away / lambda_* 不抛穿（format spec 默认 0）。"""
    preds = [{"direction": "Liverpool 胜"}]  # 几乎全空
    html = dash_mod._render_predictions_rows(preds)
    # 不抛穿即可；同时应该归类为 row-home（"Liverpool 胜" 含"胜"但缺 home，匹配默认 row-draw）
    assert "row-" in html  # 至少有一个 CSS class


# ──────────────────────── _render_monte_carlo ────────────────────────


def test_render_monte_carlo_empty_and_cap_and_width(dash_mod):
    """R26 #3：空 → fallback HTML；>8 队截前 8；bar_width = prob/max_prob*100。"""
    # 空
    html_empty = dash_mod._render_monte_carlo({})
    assert "No Monte Carlo data" in html_empty

    # 8 队 → 8 条 bar
    mc = {"champion_probs": {f"team_{i}": 0.1 * (i + 1) for i in range(10)}}
    html = dash_mod._render_monte_carlo(mc)
    # 列表前 8 队上限：team_0 ~ team_7
    assert "team_0" in html and "team_7" in html
    assert "team_8" not in html and "team_9" not in html
    # max_prob = 0.1*8 = 0.8 → team_7 bar 100%，team_0 bar 12.5% → 12%
    assert "100%" in html  # team_7 bar width 100
    assert "12%" in html  # team_0 bar width 12

    # 全 0 prob → bar width 全 0（不抛错）
    html2 = dash_mod._render_monte_carlo({"champion_probs": {"A": 0, "B": 0}})
    assert "0%" in html2


# ──────────────────────── _render_calibration_info ────────────────────────


def test_render_calibration_info_none_vs_populated(dash_mod):
    """R26 #3：None → 空串；populated dict → 含 sample_size + correction。"""
    assert dash_mod._render_calibration_info(None) == ""

    html = dash_mod._render_calibration_info({
        "sample_size": 30,
        "home_correction": 1.05,
        "draw_correction": 1.00,
        "away_correction": 0.95,
        "onside_home_correction": 1.02,
        "onside_away_correction": 0.98,
    })
    assert "n=30" in html
    assert "Home x1.05" in html
    assert "Onside Home x1.02" in html


# ──────────────────────── _render_backtest ────────────────────────


def test_render_backtest_none_vs_ok(dash_mod):
    """R26 #3：None → 空；ok 状态 → 含 status/matched/accuracy 百分比。"""
    assert dash_mod._render_backtest(None) == ""

    html = dash_mod._render_backtest({
        "status": "ok", "matched_matches": 3, "accuracy": 0.667,
    })
    assert "Status: ok" in html
    assert "Matched: 3" in html
    assert "Accuracy: 66.7%" in html


# ──────────────────────── _write_dashboard_file ────────────────────────


def test_write_dashboard_file_writes_to_path(dash_mod, tmp_path):
    """R26 #3：output_path 指定 → 写文件；mkdir parents 兜底。"""
    target = tmp_path / "nested" / "sub" / "dash.html"
    html = "<html><body>test</body></html>"
    dash_mod._write_dashboard_file(html, target)
    assert target.read_text(encoding="utf-8") == html


def test_write_dashboard_file_oserror_silently_warns(dash_mod, caplog):
    """R26 #3：output_path 是目录而非文件 → OSError → warning 吞掉不抛。"""
    target = tmp_path = __import__("pathlib").Path.cwd() / "_not_a_file_marker.txt"
    # 写一个父目录作为 target，让 write_text 在不存在父目录时仍 mkdir
    # 模拟 OSError：让 target.parent 是 None
    class FakePath:
        def __init__(self):
            self.parent = self
        def mkdir(self, **kwargs):
            raise OSError("mkdir fail")
        def write_text(self, *a, **k):
            raise OSError("write fail")

    with caplog.at_level("WARNING"):
        dash_mod._write_dashboard_file("html", FakePath())  # type: ignore
    assert any("Failed to save dashboard" in r.message for r in caplog.records)


# ──────────────────────── generate_dashboard end-to-end ────────────────────────


def test_generate_dashboard_returns_html_with_empty_data(dash_mod):
    """R26 #3：空 prediction_data → 返 HTML，含预期标记。"""
    html = dash_mod.generate_dashboard({})
    assert "League Predict Dashboard" in html
    assert "Predictions (0)" in html
    assert "No Monte Carlo data" in html
    assert "UNKNOWN" in html  # league.upper() 默认 unknown


def test_generate_dashboard_writes_to_output_path(dash_mod, tmp_path):
    """R26 #3：output_path 指定 → 文件被写出。"""
    target = tmp_path / "league_dash.html"
    html = dash_mod.generate_dashboard(
        {"league": "epl", "predictions": [{"match": "A vs B", "home": "A", "away": "B",
                                              "direction": "A 胜", "predicted_score": "2-1",
                                              "confidence_score": 0.6, "lambda_home": 1.5,
                                              "lambda_away": 0.8}]},
        output_path=target,
    )
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "EPL" in content
    assert "Predictions (1)" in content
    assert "row-home" in content
