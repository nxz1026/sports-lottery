"""R28 #1: scripts/predict.py::build_parser 17 flag 边界测。

现状：仅 test_p0_window_mc_stablekey.py 两条平凡冒烟测（--past-days 7 +
默认 30），其他 15 个 flag 完全无直测。本轮覆盖 cron / systemd timer / k8s
CronJob 入口参数解析边界。
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
def predict_mod():
    """加载 predict.py 为唯一 module name（避免污染 sys.modules + 顶层 __main__ 防 GIL）。"""
    spec = importlib.util.spec_from_file_location(
        "predict_bp_test", REPO_ROOT / "scripts" / "predict.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── --league 边界 ────────────────────────


def test_league_accepts_known(predict_mod):
    """R28 #1：--league epl/laliga 接受；返回 args.league 字符串。"""
    parser = predict_mod.build_parser()
    for lg in ("epl", "laliga", "seriea", "bundesliga", "ligue1"):
        args = parser.parse_args(["--league", lg])
        assert args.league == lg


def test_league_rejects_unknown(predict_mod):
    """R28 #1：--league xyz 不在 choices → argparse exit 2。"""
    parser = predict_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--league", "xyz"])


# ──────────────────────── --all 单 toggle ────────────────────────


def test_all_flag_default_false(predict_mod):
    """R28 #1：不传 --all → args.all=False；传 → True。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args([])
    assert args.all is False
    args2 = parser.parse_args(["--all"])
    assert args2.all is True


# ──────────────────────── --monte-carlo / --no-monte-carlo 互斥 ────────────────────────


def test_monte_carlo_default_true(predict_mod):
    """R28 #1：默认 args.monte_carlo=True（action store_true + default=True）。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args([])
    assert args.monte_carlo is True


def test_monte_carlo_negation_overrides(predict_mod):
    """R28 #1：--no-monte-carlo 与 --monte-carlo 共享 dest=monte_carlo，后传入胜出。
    --no-monte-carlo 单用 → False。
    """
    parser = predict_mod.build_parser()
    args = parser.parse_args(["--no-monte-carlo"])
    assert args.monte_carlo is False
    # 顺序传：--monte-carlo 后覆盖 --no-monte-carlo
    args2 = parser.parse_args(["--no-monte-carlo", "--monte-carlo"])
    assert args2.monte_carlo is True
    args3 = parser.parse_args(["--monte-carlo", "--no-monte-carlo"])
    assert args3.monte_carlo is False


# ──────────────────────── --n-simulations type 校验 ────────────────────────


def test_n_simulations_accepts_int(predict_mod):
    """R28 #1：--n-simulations 1000 接受；不接受字符串（type=int 校验）。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args(["--n-simulations", "1500"])
    assert args.n_simulations == 1500


def test_n_simulations_rejects_non_int(predict_mod):
    """R28 #1：--n-simulations abc → argparse type error exit 2。"""
    parser = predict_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--n-simulations", "abc"])


# ──────────────────────── --data-source choices ────────────────────────


def test_data_source_choices(predict_mod):
    """R28 #1：--data-source 接受 football-data/espn/api-football 三个；拒绝其他。"""
    parser = predict_mod.build_parser()
    for src in ("football-data", "espn", "api-football"):
        args = parser.parse_args(["--data-source", src])
        assert args.data_source == src
    # 非法 → exit 2
    with pytest.raises(SystemExit):
        parser.parse_args(["--data-source", "random"])


# ──────────────────────── --dates 字符串透传 ────────────────────────


def test_dates_string_passthrough(predict_mod):
    """R28 #1：--dates 20260101-20260201 round-trip；不传 → None。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args(["--dates", "20260101-20260201"])
    assert args.dates == "20260101-20260201"
    args2 = parser.parse_args([])
    assert args2.dates is None


# ──────────────────────── --train-ml 早退路径 ────────────────────────


def test_train_ml_triggers_early_return(predict_mod, monkeypatch):
    """R28 #1：--train-ml → _apply_ml_args 返 True → main() return 0 不进 run_league 分支。

    mock train_all_league_models / fetch_fifa_rankings / get_or_init_elo_ratings 验证
    提前结束。
    """
    train_called = {"n": 0}

    def fake_train(**kwargs):
        train_called["n"] += 1
        return {}  # 空 dict → 跳过 print 输出

    # fake FIFA / ELO
    fake_fifa = {"Brazil": 1800, "England": 1900}
    fake_elo = {"Brazil": 1800.0, "England": 1900.0}

    # simulate import path: 注入 ml_model.train_all_league_models
    fake_ml_model = type("M", (), {"train_all_league_models": staticmethod(fake_train)})
    fake_rankings = type("R", (), {"fetch_fifa_rankings": staticmethod(lambda: fake_fifa)})
    fake_elo_mod = type("E", (), {"get_or_init_elo_ratings": staticmethod(lambda f: fake_elo)})

    # 拦截 imports 走 fake namespace（sys.modules 已含这些包，无需 monkeypatch）
    import core.config as core_cfg

    monkeypatch.setitem(sys.modules, "core.model.ml_model", fake_ml_model)
    monkeypatch.setitem(sys.modules, "core.rankings", fake_rankings)
    monkeypatch.setitem(sys.modules, "core.elo", fake_elo_mod)

    args = predict_mod.build_parser().parse_args(["--train-ml", "--league", "epl"])
    assert args.train_ml is True

    # 触发 _apply_ml_args → train 全跑 + 返 True
    rc = predict_mod._apply_ml_args(args)
    assert rc is True
    assert train_called["n"] == 1


def test_no_ml_disables_ml_blending(predict_mod, monkeypatch):
    """R28 #1：--no-ml → ML_CONFIG["enabled"]=False；不触发 _apply_ml_args 早退（除非 --train-ml/--cleanup）。"""
    import core.config as core_cfg
    # 先记下原始值用于还原
    saved = dict(core_cfg.ML_CONFIG)
    core_cfg.ML_CONFIG["enabled"] = True
    try:
        args = predict_mod.build_parser().parse_args(["--no-ml"])
        rc = predict_mod._apply_ml_args(args)
        # 没 train/cleanup 时返 False（继续预测流程）
        assert rc is False
        assert core_cfg.ML_CONFIG["enabled"] is False
    finally:
        core_cfg.ML_CONFIG.clear()
        core_cfg.ML_CONFIG.update(saved)


def test_cleanup_triggers_early_return(predict_mod, monkeypatch):
    """R28 #1：--cleanup → cleanup_old_files(days=7) + 返 True。"""
    removed = {"n": 0}

    def fake_cleanup(days):
        removed["n"] += 1
        return 5

    monkeypatch.setattr(predict_mod, "cleanup_old_files", fake_cleanup)
    args = predict_mod.build_parser().parse_args(["--cleanup"])
    rc = predict_mod._apply_ml_args(args)
    assert rc is True
    assert removed["n"] == 1


# ──────────────────────── 边界 multi-flag 共存 ────────────────────────


def test_multiple_flags_coexist(predict_mod):
    """R28 #1：多个 flag 共存：--all + --no-monte-carlo + --past-days 14 + --dashboard。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args(["--all", "--no-monte-carlo", "--past-days", "14", "--dashboard"])
    assert args.all is True
    assert args.monte_carlo is False
    assert args.past_days == 14
    assert args.dashboard is True


def test_default_flags(predict_mod):
    """R28 #1：不传任何 flag 时默认值集合（防止回归改默认）。"""
    parser = predict_mod.build_parser()
    args = parser.parse_args([])
    assert args.league == "epl"
    assert args.all is False
    assert args.data_source == ""
    assert args.monte_carlo is True
    assert args.n_simulations == 10000  # DEFAULT_N_SIMULATIONS
    assert args.backtest is False
    assert args.cleanup is False
    assert args.dates is None
    assert args.past_days == 30  # DEFAULT_PAST_DAYS
    assert args.ahead_days == 14  # DEFAULT_AHEAD_DAYS
    assert args.no_fetch is False
    assert args.no_dc is False
    assert args.update_rankings is False
    assert args.train_ml is False
    assert args.no_ml is False
    assert args.dashboard is False
