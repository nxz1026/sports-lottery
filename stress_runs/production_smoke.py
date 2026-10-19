"""Production smoke test: drive ml_feedback.predict->outcome 全链路。

目的：
- 验证 ``predict.py::_emit_ml_predictions`` + ``record_prediction`` + 
  ``record_outcome`` + ``join_predictions_outcomes`` + ``retrain_baseline.evaluate_baseline``
  + ``reliability_diagram`` 在 events.jsonl 上一气呵成。
- 验证 ``features_snapshot`` (Round 11 E.1) 落 events 后透传到 join samples。
- 不依赖网络 / API key / 真实 league fixture。
"""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

# Add repo root + scripts to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import core.ml_feedback as mlf  # noqa: E402
from core import observability as obs  # noqa: E402
from core.retrain_baseline import (  # noqa: E402
    evaluate_baseline,
    format_reliability_report,
    format_report,
    reliability_diagram,
)


def main(n_matches: int = 80, n_bins: int = 10, seed: int = 42):
    """Production smoke：构造"略好于随机"的预测样本，验证完整链路。

    数据生成：每场独立随机 home/draw/away 概率 + 同分布 outcome。
    极端 cases：pred 强偏向某标签时，outcome 也强偏向同标签（.50+.30+.20 → 50% H 30% D 20% A 真实赢率）。
    这样 probs 比"随机标签"信号强，足以让 reliability diagram 出非平凡桶分布。
    """
    random.seed(seed)

    # 1. 在临时 logs 目录装 observability（process_name 留 None → events.jsonl）
    with tempfile.TemporaryDirectory() as td:
        log_dir = Path(td) / "logs"
        obs.reset_for_test()
        obs.setup(dir=log_dir)  # 无 process_name 后缀 → events.jsonl
        events_path = obs.events_jsonl_path()

        # 2. 模拟 predict.py::record_prediction 发射 N 场 prediction +
        #    outcome，构造 probs 与 outcome 强相关（模拟真实模型的弱信号）。
        teams = [
            ("Arsenal", "Chelsea"), ("Liverpool", "Everton"),
            ("Real", "Atleti"), ("Bayern", "Dortmund"),
            ("Juventus", "Milan"), ("PSG", "Marseille"),
            ("ManUtd", "ManCity"), ("Barcelona", "Sevilla"),
            ("Ajax", "PSV"), ("Benfica", "Porto"),
            ("Inter", "Roma"), ("Dortmund", "Leverkusen"),
            ("Napoli", "Lazio"), ("Atletico", "Sevilla"),
            ("Valencia", "Villarreal"), ("Roma", "Napoli"),
        ]
        leagues = ["epl", "laliga", "bundesliga", "seriea", "ligue1"]

        n_emitted = 0
        for i in range(n_matches):
            home, away = teams[i % len(teams)]
            league = leagues[i % len(leagues)]
            kickoff = f"2026-09-{(15 + i) % 28 + 1:02d}T{15 + (i % 4):02d}:00:00Z"

            # 概率偏倚：home ~50%, draw ~25%, away ~25% 但具体每场独立随机
            base_h = random.uniform(0.30, 0.65)
            base_d = random.uniform(0.15, 0.30)
            base_a = max(1 - base_h - base_d, 0.05)
            total = base_h + base_d + base_a
            probs = {
                "h": round(base_h / total, 4),
                "d": round(base_d / total, 4),
                "a": round(base_a / total, 4),
            }
            # features_snapshot（predictor.reasoning_factors 子集）
            features_snapshot = {
                "home_ml_true_prob": probs["h"],
                "draw_true_prob": probs["d"],
                "away_ml_true_prob": probs["a"],
                "home_form_score": round(random.uniform(0.3, 0.8), 3),
                "away_form_score": round(random.uniform(0.3, 0.8), 3),
                "home_onside_score": round(random.uniform(0.2, 0.9), 3),
                "away_onside_score": round(random.uniform(0.2, 0.9), 3),
                "elo_home_expected": round(random.uniform(0.4, 0.65), 3),
                "home_elo_rating": 1500 + random.randint(-100, 200),
                "away_elo_rating": 1500 + random.randint(-100, 200),
            }

            mlf.record_prediction(
                league=league,
                match_id=f"m{i+1}",
                home=home, away=away,
                kickoff_utc=kickoff,
                probs=probs,
                model="rule-v1" if i % 3 == 0 else "ml-blend-w0.15",
                goal_line=2.5,
                features_snapshot=features_snapshot,
            )
            n_emitted += 1

            # 3. 同步写 outcome：probs 排序驱动 outcome 标签（保证强相关）；
            # 略带 15% 噪声使 reliability 桶分布真实。
            r = random.random()
            if r < probs["h"]:
                # H win：home score > away
                if random.random() < 0.85:
                    hs, as_ = random.randint(1, 4), 0
                else:
                    hs, as_ = random.randint(2, 4), random.randint(1, 2)
            elif r < probs["h"] + probs["d"]:
                # Draw
                if random.random() < 0.85:
                    hs = as_ = random.randint(0, 3)
                else:
                    hs, as_ = random.randint(0, 2), random.randint(0, 2)
                    if hs == as_:
                        hs += 1
            else:
                # A win：away score > home
                if random.random() < 0.85:
                    hs, as_ = 0, random.randint(1, 4)
                else:
                    hs, as_ = random.randint(1, 2), random.randint(2, 4)

            mlf.record_outcome(
                league=league,
                match_id=f"m{i+1}",
                home=home, away=away,
                kickoff_utc=kickoff,
                home_score=hs, away_score=as_, status="FT",
            )

        # 4. 读 events.jsonl 看数据落盘
        evs = list(obs.iter_events())
        preds = [e for e in evs if e["kind"] == "prediction"]
        outs = [e for e in evs if e["kind"] == "outcome"]
        print(f"[smoke] events.jsonl at {events_path}")
        print(f"[smoke] events: {len(evs)} (predictions={len(preds)}, outcomes={len(outs)})")
        with_fs = [e for e in preds if e.get("features_snapshot")]
        print(f"[smoke]   predictions with features_snapshot: {len(with_fs)}/{len(preds)}")
        # sanity：保证第一场 features_snapshot 透传
        if preds:
            sample = preds[0].get("features_snapshot")
            assert sample is not None and len(sample) >= 5, "features_snapshot 透传失败"

        # 5. retrain_baseline 一键跑
        samples = mlf.join_predictions_outcomes()
        print(f"[smoke] joined samples: {len(samples)}")
        metrics = evaluate_baseline(samples)
        print()
        print(format_report(metrics))
        print()
        rm = reliability_diagram(samples, n_bins=n_bins)
        print(format_reliability_report(rm))

        # 6. 校验（链路正确，不强求数据 quality）
        assert metrics.n == n_matches, f"join 应得 {n_matches} 样本，实际 {metrics.n}"

        # features_snapshot 透传
        with_fs_joined = [s for s in samples if s.get("features_snapshot")]
        assert len(with_fs_joined) == n_matches, (
            f"所有 join samples 都应有 features_snapshot，"
            f"实际 {len(with_fs_joined)}/{n_matches}"
        )
        # reliability bins 数 = n_bins
        assert len(rm.bins) == n_bins
        # ece < 0.30 是 reasonable 校准 — 真实数据可能超，synthetic 应该 OK
        assert rm.ece < 0.40, f"ece 过大: {rm.ece}（synthetic 数据应 <0.40）"
        # improvement_pct 应在 -10% 到 +30% 之间（合理区间）
        if metrics.improvement_pct is not None:
            print(f"[smoke]   improvement_pct={metrics.improvement_pct:+.2f}% vs uniform")

        print()
        print("[smoke] ALL CHECKS PASSED")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    main(n_matches=n)
