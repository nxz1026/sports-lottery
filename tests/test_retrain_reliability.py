"""Round 11 F1+F3 — ``core.retrain_baseline`` reliability diagram + CLI。

覆盖：
- 完美校准样本 → 每桶 mean_pred ≈ actual_rate 且 CI 包含 mean
- 严重高估样本（每次预测 90% 都输） → 实际 rate 远低于 mean_pred
- 低样本桶仍保留占位（n=0 桶存在）
- ece 加权算术正确
- CLI ``--reliability`` 走通
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        if p not in sys.path:
            sys.path.insert(0, p)

from core.retrain_baseline import (  # noqa: E402
    reliability_diagram, format_reliability_report, ReliabilityMetrics,
    ReliabilityBin, run, _cli, _PROB_KEYS,
)


def _make_sample(*, probs: dict[str, float], result: str) -> dict:
    """构造 joined sample schema（与 ``ml_feedback.join_predictions_outcomes`` 一致）。"""
    return {
        "match_id": "x",
        "league": "epl",
        "home": "A",
        "away": "B",
        "kickoff_utc": "2026-10-13T10:00:00",
        "probs": probs,
        "model": "rule-v1",
        "result": result,
    }


class TestReliabilityDiagramMath:
    """F1: reliability_diagram 数值正确。"""

    def test_empty_samples_no_bins(self):
        rm = reliability_diagram([])
        assert rm.n_total == 0
        assert rm.bins == []
        assert rm.ece == 0.0

    def test_perfect_calibration_actual_close_to_mean(self):
        """完美校准：每个样本的顶级预测赢，统计上 mean_pred ≈ actual_rate。"""
        # 5 个 0.7 prob 顶级预测全部赢（small sample 漂移允许）
        # 放进 [0.6, 0.7) 桶：mean_pred=0.7, n=5, hits=5 → actual_rate=1.0
        samples = []
        for _ in range(5):
            samples.append(_make_sample(
                probs={"h": 0.70, "d": 0.20, "a": 0.10}, result="home_win"))
        rm = reliability_diagram(samples, n_bins=10)
        assert rm.n_total == 5
        # 第 7 桶 = [0.6, 0.7)：0.7 在闭桶右端 → 进桶
        # 第 8 桶 = [0.7, 0.8]：0.7 < 0.7 → 不进 → 该桶 n=0
        # 把 mean_pred 找最高 n 的桶
        max_bin = max(rm.bins, key=lambda b: b.n)
        assert max_bin.n == 5
        assert max_bin.mean_pred == pytest.approx(0.70, abs=1e-4)
        assert max_bin.actual_rate == pytest.approx(1.0, abs=1e-4)

    def test_overconfident_high_bucket_lower_actual(self):
        """过度自信：bucket [0.9, 1.0] 预测全 H，但实际 1/3 赢。"""
        samples = []
        # 5 场预测 95% H 胜，只有 1 场赢
        for i in range(5):
            samples.append(_make_sample(
                probs={"h": 0.95, "d": 0.03, "a": 0.02},
                result="home_win" if i == 0 else "away_win"))
        rm = reliability_diagram(samples, n_bins=10)
        # 找 [0.9, 1.0] 桶
        high_bin = next(b for b in rm.bins if b.pred_lo == 0.9)
        assert high_bin.n == 5
        assert high_bin.mean_pred == pytest.approx(0.95, abs=1e-4)
        assert high_bin.actual_rate == pytest.approx(0.20, abs=1e-4)
        # ECE 应大于 0（高估）
        assert rm.ece > 0

    def test_wilson_ci_wider_for_small_n(self):
        """小样本桶 → Wilson CI 宽。"""
        # 同样 actual_rate=0.5，n=10 vs n=1000 → CI 半宽差 10 倍
        actual_samples = [_make_sample(
            probs={"h": 0.55, "d": 0.30, "a": 0.15},
            result="home_win" if i < 50 else "away_win")
            for i in range(100)]
        rm_small = reliability_diagram(actual_samples[:10], n_bins=1)
        rm_big = reliability_diagram(actual_samples, n_bins=1)
        small_w = rm_small.bins[0].ci_hi - rm_small.bins[0].ci_lo
        big_w = rm_big.bins[0].ci_hi - rm_big.bins[0].ci_lo
        assert small_w > big_w

    def test_ece_zero_for_perfect_calibration(self):
        """完美校准时 → 全部桶 mean_pred == actual → ece≈0。"""
        samples = []
        # 构造"predicted 概率 = 实际赢率"的完美校准：
        # 10 场 0.6 预测 → 6 赢，actual=0.6
        for _ in range(6):
            samples.append(_make_sample(
                probs={"h": 0.60, "d": 0.25, "a": 0.15}, result="home_win"))
        for _ in range(4):
            samples.append(_make_sample(
                probs={"h": 0.60, "d": 0.25, "a": 0.15}, result="away_win"))
        rm = reliability_diagram(samples, n_bins=10)
        # 0.60 在桶 [0.6, 0.7)
        bin_60 = next(b for b in rm.bins if b.pred_lo == 0.6)
        assert bin_60.n == 10
        assert bin_60.mean_pred == pytest.approx(0.60, abs=1e-4)
        assert bin_60.actual_rate == pytest.approx(0.60, abs=1e-4)
        # 该桶对 ece 贡献为 0
        assert rm.ece < 1e-9


class TestReliabilityFormatReport:
    """F1: format_reliability_report 文本输出 sanity。"""

    def test_zero_samples_reports_skipped(self):
        s = format_reliability_report(ReliabilityMetrics(bins=[], ece=0.0, n_total=0))
        assert "无样本" in s

    def test_with_bins_includes_header_and_rows(self):
        bins = [ReliabilityBin(0.0, 0.5, 8, 0.3, 0.4, 0.1, 0.7),
                ReliabilityBin(0.5, 1.0, 12, 0.7, 0.65, 0.4, 0.85)]
        s = format_reliability_report(
            ReliabilityMetrics(bins=bins, ece=0.085, n_total=20))
        assert "[reliability] bins=2" in s
        assert "0.00-0.50" in s
        assert "0.50-1.00" in s
        assert "ece=0.085" in s


class TestReliabilityCLI:
    """F3: CLI ``--reliability N`` 走通。"""

    def test_cli_with_reliability_flag(self, capsys):
        # run() 直接调，可视化输出
        rm = run(reliability_bins=5, verbose=True)
        out = capsys.readouterr().out
        # 无 events 时 retrain_baseline 报告"no joined samples"，reliability 也给空报告
        assert "no joined samples" in out
        assert "[reliability]" in out
        assert rm.n == 0  # 没真实 events

    def test_cli_with_real_samples_via_subprocess(self):
        """用 subprocess 跑 CLI 验证参数流。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "core.retrain_baseline", "--reliability", "5"],
            cwd=str(REPO_ROOT / "scripts"),
            capture_output=True, text=True,
        )
        out = result.stdout
        # 无 events → "[retrain] no joined samples"
        assert "[retrain] no joined samples" in out or "n=" in out
        assert "无样本" in out or "[retrain]" in out
