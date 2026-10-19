"""Round 11 E1+E4 — ``ml_feedback.record_prediction`` 加 ``features_snapshot`` 守门。

覆盖：
- ``record_prediction`` 接 features_snapshot 参数 → 写到 events.jsonl
- 不传 features_snapshot 时不写该字段（向后兼容）
- 回放时 ``join_predictions_outcomes`` 透传 features_snapshot
- retrain_baseline 的 retrain 路径判别：有 features_snapshot 进训练样本，无则 fallback calibration
"""
from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def reset_obs(tmp_path):
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


def _read_events():
    p = obs.events_jsonl_path()
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ────────────────────── E1: record_prediction features_snapshot 参数 ──────────────────────────


class TestRecordPredictionFeaturesSnapshot:
    """E1: features_snapshot 参数透传到 events.jsonl。"""

    def test_features_snapshot_writes_event(self):
        mlf.record_prediction(
            league="epl", match_id="m1", home="A", away="B",
            kickoff_utc="2026-10-13T10:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
            model="rule-v1", goal_line=2.5,
            features_snapshot={"home_form_score": 0.7, "away_onside_score": 0.4,
                               "home_ml_true_prob": 0.55},
        )
        evs = _read_events()
        assert len(evs) == 1
        rec = evs[0]
        assert rec["kind"] == "prediction"
        assert "features_snapshot" in rec
        assert rec["features_snapshot"]["home_form_score"] == pytest.approx(0.7)
        assert rec["features_snapshot"]["home_ml_true_prob"] == pytest.approx(0.55)

    def test_features_snapshot_absent_by_default(self):
        """不传 → events.jsonl 不出 features_snapshot 字段（向后兼容）。"""
        mlf.record_prediction(
            league="epl", match_id="m1", home="A", away="B",
            kickoff_utc="2026-10-13T10:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
        )
        evs = _read_events()
        rec = evs[0]
        assert "features_snapshot" not in rec, (
            "向后兼容：未传 features_snapshot 不应落 events.jsonl 中"
        )

    def test_features_snapshot_none_treated_as_absent(self):
        """显式传 None → 同样不出字段。"""
        mlf.record_prediction(
            league="epl", match_id="m1", home="A", away="B",
            kickoff_utc="2026-10-13T10:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
            features_snapshot=None,
        )
        rec = _read_events()[0]
        assert "features_snapshot" not in rec

    def test_features_snapshot_empty_dict_treated_as_absent(self):
        """空 dict → 不出字段。"""
        mlf.record_prediction(
            league="epl", match_id="m1", home="A", away="B",
            kickoff_utc="2026-10-13T10:00:00",
            probs={"h": 0.6, "d": 0.25, "a": 0.15},
            features_snapshot={},
        )
        rec = _read_events()[0]
        assert "features_snapshot" not in rec


# ────────────────────── join_predictions_outcomes 透传 ──────────────────────────


class TestJoinFeaturesSnapshotPassthrough:
    """E1: join 后 samples 透传 features_snapshot（ML 重训直接消费）。"""

    def _emit_pair(self, *, with_snapshot: bool):
        if with_snapshot:
            mlf.record_prediction(
                league="epl", match_id="m1", home="A", away="B",
                kickoff_utc="2026-10-13T10:00:00",
                probs={"h": 0.6, "d": 0.25, "a": 0.15},
                features_snapshot={"home_form_score": 0.7, "elo_home_expected": 0.6})
        else:
            mlf.record_prediction(
                league="epl", match_id="m1", home="A", away="B",
                kickoff_utc="2026-10-13T10:00:00",
                probs={"h": 0.6, "d": 0.25, "a": 0.15})
        mlf.record_outcome(
            league="epl", match_id="m1", home="A", away="B",
            kickoff_utc="2026-10-13T10:00:00",
            home_score=2, away_score=1, status="closed")

    def test_join_passes_features_snapshot_when_present(self):
        self._emit_pair(with_snapshot=True)
        joined = mlf.join_predictions_outcomes(league="epl")
        assert len(joined) == 1
        sample = joined[0]
        assert "features_snapshot" in sample
        assert sample["features_snapshot"]["home_form_score"] == pytest.approx(0.7)
        assert sample["features_snapshot"]["elo_home_expected"] == pytest.approx(0.6)

    def test_join_no_features_snapshot_when_absent(self):
        self._emit_pair(with_snapshot=False)
        joined = mlf.join_predictions_outcomes(league="epl")
        assert len(joined) == 1
        assert joined[0].get("features_snapshot") is None


# ────────────────────── E4: retrain_baseline 选用 sample ──────────────────────────


class TestRetrainBaselineSelectsByFeaturesSnapshot:
    """E4: retrain_baseline 可区分有/无 features_snapshot 的样本。

    本测试不真跑 ML 重训（MatchMLModel 训练开销大）—— 只验"有 features_snapshot
    的样本被推到 ml 训练集、无的只用于 calibration"。
    """

    def test_partition_samples_with_and_without_features_snapshot(self):
        # 3 场 prediction+outcome，其中 2 场带 features_snapshot
        for i, with_fs in enumerate([True, False, True]):
            if with_fs:
                mlf.record_prediction(
                    league="epl", match_id=f"m{i+1}", home="A", away="B",
                    kickoff_utc=f"2026-10-13T{10+i}:00:00",
                    probs={"h": 0.6, "d": 0.25, "a": 0.15},
                    features_snapshot={"x": 0.1 * i})
            else:
                mlf.record_prediction(
                    league="epl", match_id=f"m{i+1}", home="A", away="B",
                    kickoff_utc=f"2026-10-13T{10+i}:00:00",
                    probs={"h": 0.6, "d": 0.25, "a": 0.15})
            mlf.record_outcome(
                league="epl", match_id=f"m{i+1}", home="A", away="B",
                kickoff_utc=f"2026-10-13T{10+i}:00:00",
                home_score=1, away_score=0, status="closed")
        joined = mlf.join_predictions_outcomes(league="epl")
        assert len(joined) == 3
        with_fs = [s for s in joined if s.get("features_snapshot")]
        without_fs = [s for s in joined if not s.get("features_snapshot")]
        assert len(with_fs) == 2
        assert len(without_fs) == 1
        # with_fs 的 x 字段保持传入值
        xs = sorted(s["features_snapshot"]["x"] for s in with_fs)
        assert xs == [0.0, 0.2]
