"""``core.retrain_baseline`` 守门测试 — P-6 M-2 retrain 评估闭环。

覆盖：
- ``evaluate_baseline`` 完美预测 → brier=0 / accuracy=1.0
- ``evaluate_baseline`` 完美错误 → brier 接近 2 / accuracy=0
- ``evaluate_baseline`` 空样本 → n=0 / 所有指标 None
- 均匀分布 vs stored probs Brier 的差值落在合理区间
- per_outcome accuracy 拆分正确
- model_versions 计数正确
- ``load_joined_samples`` 走 ``ml_feedback.join_predictions_outcomes``
- 端到端：obs 写 events.jsonl → record_prediction+outcome → evaluate_baseline
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.ml_feedback as mlf  # noqa: E402
import core.observability as obs  # noqa: E402
from core.retrain_baseline import (  # noqa: E402
    BaselineMetrics,
    _brier,
    _naive_top_label,
    _top_label,
    evaluate_baseline,
    format_report,
    load_joined_samples,
    run,
)


@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    """隔离 obs 到 tmp_path，避免污染正式 events.jsonl。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


# ─────────────────────── 纯函数行为 ────────────────────────────

class TestBrierMath:
    """_brier 多类 Brier 计算：∈ [0, 2]，0=完美，2=最差。"""

    def test_perfect_prediction_zero(self):
        assert _brier({"h": 1.0, "d": 0.0, "a": 0.0}, "H") == 0.0
        assert _brier({"h": 0.0, "d": 1.0, "a": 0.0}, "D") == 0.0
        assert _brier({"h": 0.0, "d": 0.0, "a": 1.0}, "A") == 0.0

    def test_worst_prediction_two(self):
        # 实际 H，预测 A 完全错位：(0-1)^2+(0-0)^2+(1-0)^2 = 2
        assert _brier({"h": 0.0, "d": 0.0, "a": 1.0}, "H") == 2.0
        # 实际 D，预测 D：(1-0)^2+(0-1)^2+(0-0)^2 = 2
        assert _brier({"h": 1.0, "d": 0.0, "a": 0.0}, "D") == 2.0

    def test_uniform_half(self):
        # 实际 H，预测完全均匀：Brier sum = (1-1/3)^2 + 2×(0-1/3)^2 = 4/9+1/9+1/9 = 6/9 = 2/3
        for outcome in ("H", "D", "A"):
            assert math.isclose(
                _brier({"h": 1/3, "d": 1/3, "a": 1/3}, outcome), 2 / 3, abs_tol=1e-9
            )


class TestTopLabel:
    """argmax label：h -> H, d -> D, a -> A。"""

    def test_home_wins(self):
        assert _top_label({"h": 0.6, "d": 0.25, "a": 0.15}) == "H"

    def test_draw(self):
        assert _top_label({"h": 0.3, "d": 0.5, "a": 0.2}) == "D"

    def test_away_wins(self):
        assert _top_label({"h": 0.2, "d": 0.3, "a": 0.5}) == "A"


class TestEvaluateBaselineEmpty:
    """空样本 → 所有指标 None / 0，不抛。"""

    def test_empty_returns_zero_n(self):
        m = evaluate_baseline([])
        assert isinstance(m, BaselineMetrics)
        assert m.n == 0
        assert m.brier is None
        assert m.naive_brier is None
        assert m.accuracy is None
        assert m.improvement_pct is None


class TestEvaluateBaselinePerfect:
    """全对预测 → brier=0 / accuracy=1.0 / 改进 100%（brier 占均匀基线的 0%）。"""

    def _match(self, *, label: str, league: str = "epl", model: str = "rule-v1") -> dict:
        if label == "H":
            probs = {"h": 1.0, "d": 0.0, "a": 0.0}
            result = "H"
        elif label == "D":
            probs = {"h": 0.0, "d": 1.0, "a": 0.0}
            result = "D"
        else:
            probs = {"h": 0.0, "d": 0.0, "a": 1.0}
            result = "A"
        return {
            "match_id": f"{label}{league}",
            "league": league,
            "probs": probs,
            "result": result,
            "model": model,
            "home": "X",
            "away": "Y",
            "kickoff_utc": "2026-10-13T10:00:00",
        }

    def test_perfect_three_outcomes(self):
        samples = [self._match(label="H"), self._match(label="D"), self._match(label="A")]
        m = evaluate_baseline(samples)
        assert m.n == 3
        assert math.isclose(m.brier, 0.0, abs_tol=1e-9)
        assert math.isclose(m.accuracy, 1.0, abs_tol=1e-9)
        # 均匀基线 brier sum = 2/3（每场），3 场平均仍 2/3
        assert math.isclose(m.naive_brier, 2 / 3, abs_tol=1e-9)
        # 100% 改进（brier 降到 0）
        assert math.isclose(m.improvement_pct, 100.0, abs_tol=1e-9)

    def test_worst_prediction_max_brier(self):
        # 实际 H，预测 A（最大错误）
        samples = [{
            "probs": {"h": 0.0, "d": 0.0, "a": 1.0},
            "result": "H",
            "match_id": "x", "league": "epl", "model": "rule-v1",
            "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00",
        }]
        m = evaluate_baseline(samples)
        assert math.isclose(m.brier, 2.0, abs_tol=1e-9)
        assert math.isclose(m.accuracy, 0.0, abs_tol=1e-9)

    def test_ml_feedback_schema_normalization(self):
        """归一化：home_win/away_win/draw 必须映射到 H/A/D.

        ml_feedback._result_label 用这三个长标签；本模块必须兼容。
        """
        samples = [
            {"probs": {"h": 1, "d": 0, "a": 0}, "result": "home_win",
             "match_id": "1", "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 0, "d": 1, "a": 0}, "result": "draw",
             "match_id": "2", "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 0, "d": 0, "a": 1}, "result": "away_win",
             "match_id": "3", "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
        ]
        m = evaluate_baseline(samples)
        assert m.n == 3
        assert math.isclose(m.brier, 0.0, abs_tol=1e-9)
        assert math.isclose(m.accuracy, 1.0, abs_tol=1e-9)


class TestEvaluateBaselineImprovement:
    """实际 probs 比均匀 (1/3,1/3,1/3) 应该给出正向 improvement_pct。"""

    def test_skewed_better_than_uniform(self):
        # 真实分布：~70% H, 20% D, 10% A（典型主场优势场景）
        samples = []
        truths = (["home_win"] * 7) + (["draw"] * 2) + (["away_win"] * 1)
        for i, t in enumerate(truths):
            if t == "home_win":
                p = {"h": 0.7, "d": 0.2, "a": 0.1}
            elif t == "draw":
                p = {"h": 0.3, "d": 0.5, "a": 0.2}
            else:
                p = {"h": 0.2, "d": 0.3, "a": 0.5}
            samples.append({
                "probs": p, "result": t,
                "match_id": f"m{i}", "league": "epl", "model": "rule-v1",
                "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00",
            })
        m = evaluate_baseline(samples)
        assert m.n == 10
        # 应为正改进（规则模型占比 0.7 > 0.33）
        assert m.improvement_pct > 0
        # accuracy 至少 0.5（弱信号）
        assert m.accuracy >= 0.5


class TestEvaluateBaselinePerOutcome:
    """per_outcome 拆分：H 准确率 / D 准确率 / A 准确率，按实际 outcome 分类。"""

    def test_per_outcome_split(self):
        samples = [
            {"probs": {"h": 1, "d": 0, "a": 0}, "result": "home_win", "match_id": "1",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 1, "d": 0, "a": 0}, "result": "draw", "match_id": "2",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            # 第二场预测 H 实际 D → H 标签精度 1/2
            {"probs": {"h": 0, "d": 0, "a": 1}, "result": "away_win", "match_id": "3",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 0, "d": 1, "a": 0}, "result": "draw", "match_id": "4",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
        ]
        m = evaluate_baseline(samples)
        assert m.per_outcome["H"]["n"] == 1
        assert math.isclose(m.per_outcome["H"]["accuracy"], 1.0, abs_tol=1e-9)
        assert m.per_outcome["D"]["n"] == 2
        assert math.isclose(m.per_outcome["D"]["accuracy"], 0.5, abs_tol=1e-9)
        assert m.per_outcome["A"]["n"] == 1
        assert math.isclose(m.per_outcome["A"]["accuracy"], 1.0, abs_tol=1e-9)


class TestEvaluateBaselineModelVersions:
    """model_versions 字段按 s['model'] 计数。"""

    def test_model_version_count(self):
        samples = [
            {"probs": {"h": 1, "d": 0, "a": 0}, "result": "H", "match_id": "1",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 1, "d": 0, "a": 0}, "result": "H", "match_id": "2",
             "league": "epl", "model": "rule-v1",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
            {"probs": {"h": 0, "d": 0, "a": 1}, "result": "A", "match_id": "3",
             "league": "epl", "model": "ml-blend-w0.15",
             "home": "X", "away": "Y", "kickoff_utc": "2026-10-13T10:00:00"},
        ]
        m = evaluate_baseline(samples)
        assert m.model_versions == {"rule-v1": 2, "ml-blend-w0.15": 1}


# ─────────────────────── 端到端（合成 events.jsonl）─────────────────────

class TestEndToEnd:
    """events.jsonl 写入 → ml_feedback join → evaluate_baseline 端到端。"""

    def test_real_event_write_then_evaluate(self):
        # 写 2 场 prediction + 2 场 outcome 进 obs.events_jsonl_path
        mlf.record_prediction(
            league="epl", match_id="m1", home="Arsenal", away="Chelsea",
            kickoff_utc="2026-10-13T15:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
            model="ml-blend-w0.15", goal_line=2.5, market_signal=0.05,
        )
        mlf.record_prediction(
            league="epl", match_id="m2", home="Liverpool", away="Everton",
            kickoff_utc="2026-10-13T17:30:00",
            probs={"h": 0.4, "d": 0.3, "a": 0.3},
            model="rule-v1", goal_line=2.5, market_signal=0.0,
        )
        mlf.record_outcome(
            league="epl", match_id="m1", home="Arsenal", away="Chelsea",
            kickoff_utc="2026-10-13T15:00:00",
            home_score=2, away_score=1, status="closed",
        )
        mlf.record_outcome(
            league="epl", match_id="m2", home="Liverpool", away="Everton",
            kickoff_utc="2026-10-13T17:30:00",
            home_score=1, away_score=1, status="closed",
        )

        samples = load_joined_samples(league="epl")
        assert len(samples) == 2
        m = evaluate_baseline(samples)
        assert m.n == 2
        # m1: H 胜预测 H → 对；m2: 平预测 H → 错
        assert math.isclose(m.accuracy, 0.5, abs_tol=1e-9)
        # 改进 vs 均匀基线：brier 应 < 均匀 0.75
        assert m.brier < m.naive_brier
        assert m.improvement_pct > 0

    def test_run_prints_report(self, capsys):
        mlf.record_prediction(
            league="epl", match_id="m1", home="Arsenal", away="Chelsea",
            kickoff_utc="2026-10-13T15:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
            model="ml-blend-w0.15", goal_line=2.5,
        )
        mlf.record_outcome(
            league="epl", match_id="m1", home="Arsenal", away="Chelsea",
            kickoff_utc="2026-10-13T15:00:00",
            home_score=2, away_score=1, status="closed",
        )
        m = run(verbose=True)
        captured = capsys.readouterr()
        assert m.n == 1
        assert "[retrain] n=1 pairs" in captured.out
        assert "brier:" in captured.out
        assert "改进:" in captured.out

    def test_empty_events_jsonl_reports_zero(self, capsys):
        # events.jsonl 已 setup 但无记录
        m = run(verbose=True)
        captured = capsys.readouterr()
        assert m.n == 0
        assert "no joined samples" in captured.out


class TestFormatReport:
    """format_report 在指标 0 / n>0 时均不抛。"""

    def test_zero_n(self):
        s = format_report(BaselineMetrics(n=0))
        assert "no joined samples" in s

    def test_with_metrics(self):
        s = format_report(BaselineMetrics(
            n=10, brier=0.5, naive_brier=0.7, accuracy=0.6,
            naive_accuracy=0.4, improvement_pct=28.57,
            per_outcome={"H": {"n": 5, "accuracy": 0.8}},
            model_versions={"rule-v1": 10},
        ))
        assert "n=10" in s
        assert "brier:" in s
        assert "改进:" in s
        assert "H:" in s
        assert "rule-v1=10" in s


# ─────────────────────── _naive_top_label 行为 ─────────────────────

class TestNaiveTopLabel:
    """naive 选最多出现的实际结果作为"众数基线"。"""

    def test_majority_h(self):
        samples = [{"result": r} for r in ["H", "H", "H", "D", "A"]]
        assert _naive_top_label(samples) == "H"

    def test_majority_d(self):
        samples = [{"result": r} for r in ["D", "D", "H", "A"]]
        assert _naive_top_label(samples) == "D"
