"""R40-6: scripts/predict.py::_print_summary 6 cases direct + D-37 lock.

stderr 摘要打印 helper 0 direct tests; _finalize_league_output 调用, 但
helper 自身契约未锁。helper 失败会让 stderr 漏掉关键摘要字段 (calibration
偏移 / Monte Carlo champion / accuracy 段)。

helper 含契约 (D-37):
- 所有打印走 sys.stderr (不污染 stdout, 让调度器能区分);
  `print(..., file=sys.stderr)` 在每个 print
- 6 段结构: 顶 `=`*60 + Window calibration + Calibration offset (3 行)
  + To predict + per-prediction + (可选) Monte Carlo + (可选) Accuracy
  + 底 `=`*60
- Calibration offset 必含 "n=" + "home x" + "draw x" + "away x" (字段锁)
- calibration_offset=None 时打 "insufficient data" 字符串 (不爆 AttributeError)
- 缺 mc / 缺 accuracy_summary 时整个段不打印

6 tests (HIGH):
- 全 stderr (capsys.readouterr().out == "")
- calibration_offset 块含 home x / draw x / away x / n=
- calibration_offset=None 时打 "insufficient data"
- 缺 mc -> "Monte Carlo champion" 字符串不出现
- 缺 accuracy_summary -> "Accuracy summary:" 不出现
- D-37 `=`*60 出现正好两次 (顶 + 底)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def predict_mod():
    """加载 scripts/predict.py 为唯一 module name (复用 test_predict_output_filename 模式)。"""
    spec = importlib.util.spec_from_file_location(
        "predict_print_summary_test",
        REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cal():
    """最小可用 calibration dict (默认占位值即可)。"""
    return {
        "total_matches": 10, "home_win_rate": 0.5, "draw_rate": 0.2,
        "away_win_rate": 0.3, "odds_accuracy": 0.6,
        "favored_won": 6, "favored_by_odds": 10,
    }


def _cal_off(n=20):
    """calibration_offset 完整字段锁。"""
    return {
        "sample_size": n, "home_correction": 1.05,
        "draw_correction": 0.95, "away_correction": 1.00,
        "actual_home_rate": 0.55, "actual_draw_rate": 0.20,
        "actual_away_rate": 0.25,
    }


def _pred(match="Arsenal vs Chelsea"):
    """最小可用 prediction dict."""
    return {
        "match": match, "direction": "主胜", "stars": 3,
        "predicted_score": "2-1", "poisson_top3": [{"score": "2-1", "prob": 0.4}],
        "lambda_home": 1.5, "lambda_away": 1.0,
        "lambda_home_ci95": (1.0, 2.0), "lambda_away_ci95": (0.6, 1.4),
        "dixon_coles_used": True,
    }


# ──────────────────────── D-37 输出目标 ────────────────────────


def test_print_summary_d37_all_output_to_stderr(predict_mod, capsys):
    """R40-6 D-37: 所有打印走 stderr, stdout 应为空 (调度器能干净分离)。"""
    predict_mod._print_summary([], _cal(), None, None, 0, None)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "=" * 60 in captured.err


def test_print_summary_d37_calibration_offset_fields_locked(predict_mod, capsys):
    """R40-6 D-37: calibration_offset 行必含 home x / draw x / away x / n= (字段锁)。"""
    predict_mod._print_summary([], _cal(), _cal_off(n=42), None, 0, None)
    err = capsys.readouterr().err
    # 字段锁: home/draw/away 三个校正系数 + sample size
    assert "home x" in err
    assert "draw x" in err
    assert "away x" in err
    assert "n=42" in err


# ──────────────────────── None-value 边界 ────────────────────────


def test_print_summary_calibration_offset_none_says_insufficient(predict_mod, capsys):
    """R40-6: calibration_offset=None -> "insufficient data (<5 matches)" 不爆 AttributeError。

    锁定 兜底字符串 (避免后续有人改 None 提早返或抛错, 导致 stderr 完全无信号)。
    """
    predict_mod._print_summary([], _cal(), None, None, 0, None)
    err = capsys.readouterr().err
    assert "insufficient data" in err


def test_print_summary_no_monte_carlo_when_none(predict_mod, capsys):
    """R40-6: monte_carlo_result=None -> "Monte Carlo champion" 行不出现。"""
    predict_mod._print_summary([], _cal(), _cal_off(), None, 0, None)
    err = capsys.readouterr().err
    assert "Monte Carlo champion" not in err


def test_print_summary_no_accuracy_summary_when_none(predict_mod, capsys):
    """R40-6: accuracy_summary=None 默认 -> "Accuracy summary:" 不出现。"""
    predict_mod._print_summary([], _cal(), _cal_off(), None, 0, None)
    err = capsys.readouterr().err
    assert "Accuracy summary:" not in err


# ──────────────────────── D-37 分隔符 ────────────────────────


def test_print_summary_d37_top_bottom_separators(predict_mod, capsys):
    """R40-6 D-37: `=`*60 出现正好两次 (顶 + 底分隔符锁)。

    防止后人加 / 删分隔符让调度器 grep 摘要区块出错。
    """
    predict_mod._print_summary([_pred()], _cal(), _cal_off(),
                                {"champion_probs": {"Arsenal": 0.32}}, 1000,
                                {"recent_30d": {"direction_accuracy": 0.6,
                                                "score_accuracy": 0.4,
                                                "over_under_accuracy": 0.55,
                                                "reconciled": 28}})
    err = capsys.readouterr().err
    # 精确 2 次 (不允许分隔符增减)
    assert err.count("=" * 60) == 2
    # 首尾顺序: 顶在头部, 底在尾部
    first_idx = err.index("=" * 60)
    last_idx = err.rindex("=" * 60)
    assert first_idx < last_idx
    # 顶分隔符最早 < 5 字符位置 (即出现在文件开头附近)
    assert first_idx < 100
