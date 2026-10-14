"""``core.retrain_baseline.train_league_model_from_events`` 守门测试 — R12 M-3。

覆盖：
- 纯函数：``_common_feature_keys`` / ``_build_xy_from_samples`` / ``_train_metrics``
- 集成：events.jsonl 端到端 → reason=ok + 落盘 + model 可加载 + predict_proba 自洽
- 集成：features_snapshot 公共 key 不够 → reason=insufficient features
- 集成：有效样本不够 → reason=insufficient samples
- 集成：league 无样本 → reason=no joined samples
- 集成：predictor 主模型 ml_model_<league>.json 不被 from_events 训练污染
- CLI：``--train-league`` 退出码与 failure reason 映射
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402
import core.ml_feedback as mlf  # noqa: E402
from core.retrain_baseline import (  # noqa: E402
    TrainResult, _common_feature_keys, _build_xy_from_samples, _train_metrics,
    _events_model_path, _kfold_metrics,
    train_league_model_from_events, format_train_report,
)


# ───────────────────────── fixtures ──────────────────────────

@pytest.fixture(autouse=True)
def reset_obs(tmp_path, monkeypatch):
    """隔离 obs 到 tmp_path，并 monkeypatch base_dir 给 from_events 落盘路径。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


# ───────────────────────── 纯函数 ─────────────────────────────

class TestCommonFeatureKeys:
    """_common_feature_keys：所有样本 features_snapshot 公共覆盖 key。"""

    def test_all_samples_share_keys(self):
        samples = [
            {"features_snapshot": {"a": 1.0, "b": 2.0, "c": 3.0}},
            {"features_snapshot": {"a": 0.5, "b": 1.5, "c": 2.5}},
            {"features_snapshot": {"a": 0.7, "b": 1.7, "c": 2.7}},
        ]
        assert _common_feature_keys(samples) == ["a", "b", "c"]

    def test_intersection_only(self):
        """只返回所有样本都有的 key（交集），保首次出现顺序。"""
        samples = [
            {"features_snapshot": {"a": 1.0, "b": 2.0, "extra1": 0.1}},
            {"features_snapshot": {"a": 0.5, "c": 1.5, "extra2": 0.2}},
            {"features_snapshot": {"a": 0.7, "d": 1.7}},
        ]
        # "a" 是唯一三个都有的；首次出现顺序 = ["a", "b", "extra1", ...] → 仅 "a"
        assert _common_feature_keys(samples) == ["a"]

    def test_skips_non_dict_snapshot(self):
        samples = [
            {"features_snapshot": {"a": 1.0, "b": 2.0}},
            {"features_snapshot": None},
            {"features_snapshot": {"a": 0.5, "b": 1.5}},
        ]
        assert _common_feature_keys(samples) == ["a", "b"]

    def test_skips_non_numeric_values(self):
        """value 不是数字的 key 不能进训练矩阵 → 跳过。"""
        samples = [
            {"features_snapshot": {"a": 1.0, "label": "home_win"}},
            {"features_snapshot": {"a": 0.5, "label": "draw"}},
        ]
        # "label" 不是数值 → 不在公共 key 里
        assert _common_feature_keys(samples) == ["a"]

    def test_skips_empty_samples(self):
        """全部样本 features_snapshot 都空 → 返回空。"""
        samples = [
            {"features_snapshot": None},
            {"features_snapshot": {}},
            {},
        ]
        assert _common_feature_keys(samples) == []

    def test_skips_bool_values(self):
        """bool 不是数值（避免被误识成 0/1）。"""
        samples = [
            {"features_snapshot": {"a": True}},
            {"features_snapshot": {"a": False}},
        ]
        assert _common_feature_keys(samples) == []


class TestBuildXy:
    """_build_xy_from_samples：按 feature_cols 投影成 (X, y)。"""

    def test_basic_projection(self):
        samples = [
            {
                "features_snapshot": {"a": 1.0, "b": 2.0},
                "result": "home_win",
            },
            {
                "features_snapshot": {"a": 3.0, "b": 4.0},
                "result": "draw",
            },
            {
                "features_snapshot": {"a": 5.0, "b": 6.0},
                "result": "away_win",
            },
        ]
        X, y = _build_xy_from_samples(samples, ["a", "b"])
        assert X == [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        assert y == [0, 1, 2]  # H/D/A → 0/1/2

    def test_skip_unrecognized_result(self):
        samples = [
            {"features_snapshot": {"a": 1.0}, "result": "home_win"},
            {"features_snapshot": {"a": 2.0}, "result": "invalid_label"},
            {"features_snapshot": {"a": 3.0}, "result": "draw"},
        ]
        X, y = _build_xy_from_samples(samples, ["a"])
        assert X == [[1.0], [3.0]]
        assert y == [0, 1]

    def test_skip_missing_feature_key(self):
        """feature_cols 里有但 features_snapshot 里没的 key → 该样本被跳过。"""
        samples = [
            {"features_snapshot": {"a": 1.0, "b": 2.0}, "result": "home_win"},
            {"features_snapshot": {"a": 3.0}, "result": "draw"},  # 缺 b
            {"features_snapshot": {"a": 5.0, "b": 6.0}, "result": "away_win"},
        ]
        X, y = _build_xy_from_samples(samples, ["a", "b"])
        assert X == [[1.0, 2.0], [5.0, 6.0]]
        assert y == [0, 2]

    def test_legacy_label_passthrough(self):
        """旧 schema 直接传 'H'/'D'/'A' 也要能识别。"""
        samples = [
            {"features_snapshot": {"a": 1.0}, "result": "H"},
            {"features_snapshot": {"a": 2.0}, "result": "D"},
            {"features_snapshot": {"a": 3.0}, "result": "A"},
        ]
        X, y = _build_xy_from_samples(samples, ["a"])
        assert y == [0, 1, 2]


class TestTrainMetrics:
    """_train_metrics：训练集 in-sample accuracy + Brier。"""

    def test_perfect_model_zero_brier(self):
        """模型预测完美 → accuracy=1.0, brier≈0。

        300 epochs 后残差 ~1e-3 量级（softmax 概率略有偏离 0/1），
        用 abs_tol=1e-2 容许（强调"接近零"而不是"严格零"）。
        """
        from core.model.ml_model import MatchMLModel
        # 用全 1 信号 + H 全胜：y=0 一对一
        X = [[1.0, 0.0]] * 4
        y = [0, 0, 0, 0]
        model = MatchMLModel(n_features=2).fit(X, y)
        acc, brier = _train_metrics(X, y, model)
        assert math.isclose(acc, 1.0, abs_tol=1e-6)
        assert math.isclose(brier, 0.0, abs_tol=1e-2)

    def test_returns_two_floats(self):
        """返回值类型稳定。"""
        from core.model.ml_model import MatchMLModel
        model = MatchMLModel(n_features=2).fit([[1.0, 0.0]], [0])
        acc, brier = _train_metrics([[1.0, 0.0]], [0], model)
        assert isinstance(acc, float)
        assert isinstance(brier, float)


class TestKfoldMetricsGuards:
    """R16 #1: _kfold_metrics 守门 k=0/1 防 ZeroDivisionError。"""

    def test_k_zero_returns_none(self):
        """k=0（typo 原 ``k < k`` 守门无效 → ZeroDivisionError）→ 应返回 (None, None)。"""
        X = [[0.0] * 3 for _ in range(60)]
        y = [0] * 60
        acc, brier = _kfold_metrics(X, y, n_features=3, k=0)
        assert acc is None
        assert brier is None

    def test_k_one_returns_none(self):
        """k=1（无 fold 划分）→ 应返回 (None, None)。"""
        X = [[0.0] * 3 for _ in range(60)]
        y = [0] * 60
        acc, brier = _kfold_metrics(X, y, n_features=3, k=1)
        assert acc is None
        assert brier is None

    def test_k_greater_than_n_returns_none(self):
        """k>n（折数比样本还多）→ 应返回 (None, None)。"""
        X = [[0.0] * 3 for _ in range(10)]
        y = [0] * 10
        acc, brier = _kfold_metrics(X, y, n_features=3, k=20)
        assert acc is None
        assert brier is None

    def test_k_below_min_samples_returns_none(self):
        """n < _CV_MIN_SAMPLES (50) → 应返回 (None, None)。"""
        X = [[0.0] * 3 for _ in range(30)]
        y = [0] * 30
        acc, brier = _kfold_metrics(X, y, n_features=3, k=5)
        assert acc is None
        assert brier is None


# ───────────────────── events.jsonl 合成 helper ─────────────────────

def _emit_match_events(league: str, n: int, *, with_features: bool = True,
                       feature_keys: list[str] | None = None) -> None:
    """通过 obs 真实落盘 n 条 prediction+outcome。

    probs 与 outcome 强相关（80% 按顶级预测走，20% 噪声）—— 让训练
    得到的模型 accuracy 至少高于均匀基线。
    """
    rng_seed = 1234
    import random
    rng = random.Random(rng_seed)

    feature_keys = feature_keys or [
        # R13 #4 后 reasoning_factors 26 维全集（FEATURE_COLUMNS）—— helper 默认
        # 模拟真实 predictor 行为；测试显式传短 list 时用于 insufficient 路径。
        "home_true_prob", "draw_true_prob", "away_true_prob", "odds_available",
        "home_form_score", "away_form_score",
        "home_record_score", "away_record_score",
        "spread_movement", "home_ml_implied", "draw_implied",
        "home_onside_score", "away_onside_score",
        "home_fifa_score", "away_fifa_score",
        "elo_home_expected", "home_elo_rating", "away_elo_rating", "elo_diff",
        "home_form_vs_away_form", "home_record_vs_away_record",
        "form_x_record_home", "form_x_record_away",
        "odds_vs_elo_diff", "onside_vs_elo_diff",
        "is_host_country",
    ]

    for i in range(n):
        # probs 三类均匀 + 一点扰动；outcomes 80% 按顶级标签
        h = rng.uniform(0.3, 0.6)
        d = rng.uniform(0.15, 0.3)
        a = 1.0 - h - d
        probs = {"h": round(h, 4), "d": round(d, 4), "a": round(a, 4)}
        top = max(probs, key=probs.get)
        if rng.random() < 0.8:
            outcome_label = {"h": "home_win", "d": "draw", "a": "away_win"}[top]
        else:
            outcome_label = rng.choice(["home_win", "draw", "away_win"])
        hs, as_ = (2, 1) if outcome_label == "home_win" else (
            (1, 1) if outcome_label == "draw" else (0, 2))

        snap = None
        if with_features:
            snap = {k: round(rng.uniform(0.0, 1.0), 4) for k in feature_keys}
            # 让 feature 与 outcome 强信号：home_win 时 home_form_score 拉高
            if outcome_label == "home_win":
                snap["home_form_score"] = round(rng.uniform(0.6, 0.9), 4)
                snap["away_form_score"] = round(rng.uniform(0.2, 0.5), 4)
            elif outcome_label == "away_win":
                snap["away_form_score"] = round(rng.uniform(0.6, 0.9), 4)
                snap["home_form_score"] = round(rng.uniform(0.2, 0.5), 4)

        kickoff = f"2026-09-{15 + (i % 14):02d}T{12 + (i % 6):02d}:00:00Z"
        mlf.record_prediction(
            league=league, match_id=f"smoke-{i}",
            home=f"Home-{i}", away=f"Away-{i}", kickoff_utc=kickoff,
            probs=probs, model="ml-blend-w0.15",
            features_snapshot=snap,
        )
        mlf.record_outcome(
            league=league, match_id=f"smoke-{i}",
            home=f"Home-{i}", away=f"Away-{i}", kickoff_utc=kickoff,
            home_score=hs, away_score=as_, status="FT",
        )


# ───────────────────── train_league_model_from_events ─────────────────────

class TestTrainLeagueHappyPath:
    """events.jsonl 含足够多 join 样本 + 公共 features_snapshot → reason=ok + 落盘。"""

    def test_reason_ok_with_sufficient_data(self, tmp_path):
        _emit_match_events("epl", n=80)
        result = train_league_model_from_events("epl", base_dir=tmp_path / "refs")
        assert result.reason == "ok"
        assert result.n_samples == 80
        assert result.n_features >= 8
        assert result.model is not None
        assert result.path is not None
        assert result.path.exists()
        assert result.train_accuracy is not None
        assert result.train_brier is not None
        # 训练集 accuracy 应高于 1/3（outcome-feature 强相关）
        assert result.train_accuracy > 1 / 3

    def test_saved_model_roundtrips(self, tmp_path):
        """落盘的 ml_model_<league>_from_events.json 可被 MatchMLModel.from_dict 还原。"""
        from core.model.ml_model import MatchMLModel
        _emit_match_events("epl", n=50)
        result = train_league_model_from_events("epl", base_dir=tmp_path / "refs")
        assert result.reason == "ok"
        assert result.path.exists()
        with open(result.path, encoding="utf-8") as f:
            d = json.load(f)
        loaded = MatchMLModel.from_dict(d)
        # predict_proba 自接受 vec。
        proba = loaded.predict_proba([0.5] * result.n_features)
        assert len(proba) == 3
        assert math.isclose(sum(proba), 1.0, abs_tol=1e-6)

    def test_default_base_dir_writes_to_football_dir_references(self):
        """不传 base_dir → 落到 FOOTBALL_DIR/references/。"""
        from core.config import FOOTBALL_DIR
        expected_root = FOOTBALL_DIR / "references"
        try:
            expected_root.mkdir(parents=True, exist_ok=True)
            _emit_match_events("epl", n=40)
            result = train_league_model_from_events("epl")
            if result.reason == "ok" and result.path is not None:
                # 当落盘成功时，path 应在 expected_root 下
                assert str(result.path).startswith(str(expected_root))
        finally:
            # 清理落盘文件，避免污染 FOOTBALL_DIR
            for p in expected_root.glob("ml_model_epl_from_events*.json"):
                try:
                    p.unlink()
                except OSError:
                    pass

    def test_does_not_overwrite_main_model_file(self, tmp_path):
        """from_events 训练不应影响主模型 ml_model_<league>.json（生产上是 26 维）。"""
        from core.config import FOOTBALL_DIR
        main_path = FOOTBALL_DIR / "references" / "ml_model_epl.json"
        main_existed = main_path.exists()
        if main_existed:
            original_bytes = main_path.read_bytes()
        else:
            # 用空 dict 占位（让"主模型存在"这件事成立）
            try:
                main_path.parent.mkdir(parents=True, exist_ok=True)
                main_path.write_text("{}", encoding="utf-8")
                original_bytes = b"{}"
            except OSError:
                pytest.skip("cannot create main model placeholder")
        try:
            _emit_match_events("epl", n=40)
            result = train_league_model_from_events("epl", base_dir=tmp_path / "refs")
            assert result.reason == "ok"
            # 主模型文件应仍是占位内容（未被覆盖）
            assert main_path.exists()
            assert main_path.read_bytes() == original_bytes
        finally:
            if not main_existed:
                if main_path.exists():
                    main_path.unlink()
            elif main_existed:
                # 写回原始字节（防御：万一测试异常已改写）
                main_path.write_bytes(original_bytes)


class TestTrainLeagueInsufficientFeatures:
    """公共 features_snapshot key < min_feature_keys → reason=insufficient features。"""

    def test_reason_insufficient_features(self):
        _emit_match_events("epl", n=50, feature_keys=["a", "b", "c"])  # 3 keys
        result = train_league_model_from_events("epl", min_feature_keys=8)
        assert result.reason == "insufficient features"
        assert result.n_samples == 50  # 50 samples 被读到
        assert result.feature_columns == ["a", "b", "c"]
        assert result.n_features == 0  # 训练未触发
        assert result.model is None
        assert result.path is None

    def test_no_features_snapshot_at_all(self):
        _emit_match_events("epl", n=50, with_features=False)
        result = train_league_model_from_events("epl", min_feature_keys=8)
        assert result.reason == "insufficient features"
        assert result.feature_columns == []


class TestTrainLeagueInsufficientSamples:
    """有效样本数 < min_train_samples → reason=insufficient samples。"""

    def test_reason_insufficient_samples(self, tmp_path):
        _emit_match_events("epl", n=20, feature_keys=[
            "a", "b", "c", "d", "e", "f", "g", "h", "i",  # 9 keys（注释对齐列表）
        ])
        result = train_league_model_from_events(
            "epl", min_train_samples=30, min_feature_keys=8, base_dir=tmp_path / "refs",
        )
        assert result.reason == "insufficient samples"
        assert result.n_samples == 20
        assert result.n_features == 9
        assert result.model is None
        assert result.path is None


class TestTrainLeagueNoJoinedSamples:
    """league 无样本 / events.jsonl 缺失 → reason=no joined samples。"""

    def test_no_events_jsonl(self, tmp_path):
        """obs 没装挂 → events.jsonl 不存在 → ml_feedback 抛 FileNotFoundError。"""
        obs.reset_for_test()  # 卸载 events.jsonl
        result = train_league_model_from_events("epl", base_dir=tmp_path / "refs")
        assert result.reason == "no joined samples"

    def test_empty_league_filter(self):
        """events 写入了别的 league 的样本，但 train 时 league 过滤无命中。"""
        _emit_match_events("laliga", n=20)
        result = train_league_model_from_events("epl", min_train_samples=10)
        assert result.reason == "no joined samples"


class TestFormatTrainReport:
    """format_train_report：人类可读摘要。"""

    def test_format_ok_report(self):
        result = TrainResult(
            reason="ok", n_samples=80, n_features=10,
            feature_columns=[f"f{i}" for i in range(10)],
            model=None, path=Path("/tmp/ml_model_epl_from_events.json"),
            train_accuracy=0.62, train_brier=0.55,
            detail="trained",
        )
        s = format_train_report(result)
        assert "reason=ok" in s
        assert "n_samples=80" in s
        assert "n_features=10" in s
        assert "train accuracy:   0.6200" in s
        assert "train brier:      0.5500" in s
        assert "ml_model_epl_from_events.json" in s

    def test_format_failure_report(self):
        result = TrainResult(
            reason="insufficient features", n_samples=50,
            feature_columns=["a", "b"],
            detail="公共 key 仅 2 < min_feature_keys=8",
        )
        s = format_train_report(result)
        assert "reason=insufficient features" in s
        assert "公共 key 仅 2" in s


# ───────────────────────── CLI ─────────────────────────

class TestTrainLeagueCLI:
    """CLI ``--train-league <league>`` 退出码 + 输出。"""

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        """运行 ``python -m core.retrain_baseline <args>`` 并返回结果。

        Windows GBK 控制台：subprocess 进程可能输出含中文 logger 警告，强制
        ``errors="replace"`` 让 decode 不会因 cp936 字节失败而让测试崩溃。
        """
        return subprocess.run(
            [sys.executable, "-m", "core.retrain_baseline", *args],
            cwd=str(SCRIPTS_DIR.parent),
            env={"PYTHONPATH": "scripts", "PATH": __import__("os").environ["PATH"]},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_help_lists_train_league_flag(self):
        result = self._run_cli("--help")
        assert result.returncode == 0
        # help 文本可能在 stderr（py3 argparse 默认 stdout），两处都查
        assert "--train-league" in (result.stdout + result.stderr)

    def test_no_joined_samples_returns_code_3(self):
        """events.jsonl 不存在 → reason=no joined samples → exit 3。"""
        from core.retrain_baseline import _cli
        rc = _cli(["--train-league", "epl"])
        assert rc == 3

    def test_happy_path_returns_code_0(self, tmp_path):
        """足够样本 + 公共 key → exit 0。"""
        _emit_match_events("epl", n=80)
        from core import retrain_baseline as rb
        original = rb._events_model_path
        rb._events_model_path = lambda league, base_dir: tmp_path / f"ml_model_{league}_from_events.json"
        try:
            rc = rb._cli(["--train-league", "epl", "--base-dir", str(tmp_path)])
        finally:
            rb._events_model_path = original
        assert rc == 0
        assert (tmp_path / "ml_model_epl_from_events.json").exists()