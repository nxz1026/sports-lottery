"""``predict.py`` 高风险入口 log_error 包裹 — Round 9 H6+H7 守门。

覆盖：
- ``_fetch_and_parse.save_results`` 失败 → ``log_error(context="_fetch_and_parse.save_results")``
- ``_setup_league_run`` 中 ESPN odds enrichment 失败 → ``log_error(context="espn_odds_enrich")``
- ``_setup_league_run`` 中 append_historical_past_matches 失败 → ``log_error(context="append_historical_past_matches")``
- 失败不抛，不影响主流程
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402

# 用于 now_utc 参数的固定 UTC 时间（predict.py 内部 _drop_ghost_future + 提前 days 都要它）
_NOW_UTC = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def reset_obs(tmp_path):
    """隔离 obs 到 tmp_path，避免污染正式 events.jsonl。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


def _read_events():
    p = obs.events_jsonl_path()
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ────────────────────── H6: _fetch_and_parse.save_results ──────────────────────────


class TestFetchAndParseSaveResults:
    """H6: save_results 失败时落 events.jsonl，context=_fetch_and_parse.save_results"""

    def test_save_results_failure_writes_event(self):
        """save_results 抛 OSError → events.jsonl 出现 kind=error / context=_fetch_and_parse.save_results"""
        # patch predict.save_results（predict 模块内部用绑定别名，core.output.save_results
        # patch 无法穿透 from-import 后的 alias）
        with patch("predict.save_results") as mock_save:
            mock_save.side_effect = OSError("disk full simulated")

            from predict import _fetch_and_parse
            _fetch_and_parse(
                league_key="epl", data_source="football-data",
                dates_str="2026-10-13", now_utc=_NOW_UTC,
                skip_fetch=True, whole_season=False,
            )

        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "_fetch_and_parse.save_results"]
        assert len(ctx) >= 1
        e = ctx[0]
        assert e["kind"] == "error"
        assert e["league"] == "epl"
        assert "disk full" in e.get("exc_repr", "") or "OSError" in e.get("exc_type", "")

    def test_save_results_success_no_error_event(self):
        """save_results 成功 → events.jsonl 不出现 save_results error"""
        with patch("predict.save_results") as _mock_save:  # 不抛
            from predict import _fetch_and_parse
            _fetch_and_parse(
                league_key="epl", data_source="football-data",
                dates_str="2026-10-13", now_utc=_NOW_UTC,
                skip_fetch=True, whole_season=False,
            )

        evs = _read_events()
        ctx_err = [e for e in evs if e.get("context") == "_fetch_and_parse.save_results"]
        assert ctx_err == [], f"成功路径不应有 save_results 错误事件: {ctx_err}"


# ────────────────────── H7: _setup_league_run 异常分支 ──────────────────────────


class TestSetupLeagueRunEspnEnrichment:
    """H7: ESPN odds enrichment 失败 → events.jsonl 出现 espn_odds_enrich 错误"""

    def test_espn_enrichment_failure_writes_event(self):
        """ESPN enrichment 内部抛错 → log_error 落 events.jsonl。"""
        from predict import _setup_league_run

        # 模拟一个 args 对象
        class _Args:
            data_source = "football-data"
            monte_carlo = False
            n_simulations = 1000
            backtest = False
            no_dc = True
            no_fetch = False
            update_rankings = False
            ahead_days = 7

        # _setup_league_run 内调 fetch_events(mock) + parse_events(mock)。
        # 模拟一个有未来比赛的场景让 enrich_soccer_odds 真进入失败路径。
        with patch("predict.fetch_events") as mock_fetch_ev, \
             patch("core.data.parse.parse_events") as mock_parse_ev, \
             patch("predict.fetch_fifa_rankings") as mock_fifa, \
             patch("predict._update_elo") as mock_elo, \
             patch("predict._drop_ghost_future") as mock_ghost, \
             patch("core.data.odds_enrich.enrich_soccer_odds") as mock_enrich:
            mock_fetch_ev.return_value = [{"id": "m1", "kickoff_utc": "2026-10-14T15:00:00Z",
                                            "name": "Arsenal vs Chelsea",
                                            "home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}}]
            # parse_events(events, now_utc) -> (past, future, in_prog)
            mock_parse_ev.return_value = ([], [{"id": "m1", "kickoff_utc": "2026-10-14T15:00:00Z",
                                                  "name": "Arsenal vs Chelsea"}], [])
            mock_ghost.return_value = ([{"id": "m1", "kickoff_utc": "2026-10-14T15:00:00Z",
                                          "name": "Arsenal vs Chelsea"}], 0)
            mock_fifa.return_value = {}
            mock_elo.return_value = {}
            mock_enrich.side_effect = RuntimeError("ESPN 503 simulated")

            _setup_league_run(
                league_key="epl",
                args=_Args(), now_utc=_NOW_UTC, dates_str="2026-10-13",
            )

        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "espn_odds_enrich"]
        assert len(ctx) >= 1
        e = ctx[0]
        assert e["kind"] == "error"
        assert e["league"] == "epl"
        assert "ESPN 503" in e.get("exc_repr", "")


class TestSetupLeagueRunAppendHistorical:
    """H7: append_historical_past_matches 失败 → events.jsonl 落 append_historical_past_matches"""

    def test_append_historical_failure_writes_event(self):
        from predict import _setup_league_run

        class _Args:
            data_source = "football-data"
            monte_carlo = False
            n_simulations = 1000
            backtest = False
            no_dc = True
            no_fetch = True  # 跳过 ESPN enrichment 测试直入 append_historical 路径
            update_rankings = False
            ahead_days = 7

        with patch("predict.fetch_fifa_rankings") as mock_fifa, \
             patch("predict._update_elo") as mock_elo, \
             patch("core.calibration.append_historical_past_matches") as mock_app:
            mock_fifa.return_value = {}
            mock_elo.return_value = {}
            mock_app.side_effect = PermissionError("DB locked simulated")

            _setup_league_run(
                league_key="laliga",
                args=_Args(), now_utc=_NOW_UTC, dates_str="2026-10-13",
            )

        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "append_historical_past_matches"]
        assert len(ctx) >= 1
        e = ctx[0]
        assert e["kind"] == "error"
        assert e["league"] == "laliga"
        assert "DB locked" in e.get("exc_repr", "")


# ────────────────────── log_error 调用方契约 ──────────────────────────


class TestLogErrorSmoke:
    """log_error 基本契约（用 observability 直接验）"""

    def test_log_error_writes_event_and_logger(self, caplog):
        obs.log_error(ValueError("x"), context="unit-test", league="epl")
        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "unit-test"]
        assert len(ctx) == 1
        assert ctx[0]["kind"] == "error"
        assert ctx[0]["league"] == "epl"


# ────────────────────── Round 11 B.1+B.3: inner try 静态守门 ──────────────────────────


def _read_source(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


class TestPredictInnerTryLogErrorWiring:
    """Round 11: predict.py 内部 try 块升级 log_error 静态守门。"""

    def test_emit_ml_predictions_import_log_error_wired(self):
        src = _read_source("scripts/predict.py")
        assert "_emit_ml_predictions.import" in src, (
            "B.1 设计：ml_feedback import 失败应 log_error context=_emit_ml_predictions.import"
        )

    def test_emit_ml_predictions_per_match_log_error_wired(self):
        src = _read_source("scripts/predict.py")
        assert "_emit_ml_predictions.per_match" in src
        idx = src.index("_emit_ml_predictions.per_match")
        snippet = src[max(0, idx - 300):idx + 200]
        assert "log_error" in snippet
        assert "match" in snippet and "match_id" in snippet

    def test_season_inputs_log_error_wired(self):
        """B.3 站点 1: _annotate_monte_carlo.season_inputs。"""
        src = _read_source("scripts/predict.py")
        assert "_annotate_monte_carlo.season_inputs" in src
        idx = src.index("_annotate_monte_carlo.season_inputs")
        snippet = src[max(0, idx - 200):idx + 200]
        assert "log_error" in snippet
        assert "league" in snippet
        assert "data_source" in snippet

    def test_accuracy_summary_log_error_wired(self):
        """B.3 站点 2: _build_league_output.accuracy_summary。"""
        src = _read_source("scripts/predict.py")
        assert "_build_league_output.accuracy_summary" in src

    def test_dashboard_log_error_wired(self):
        """B.3 站点 3: _build_league_output.dashboard。"""
        src = _read_source("scripts/predict.py")
        assert "_build_league_output.dashboard" in src

    def test_dc_rho_fit_log_error_wired(self):
        """B.3 站点 4: _compute_and_predict.dc_rho_fit（最危险：默认 ρ 兜底）。"""
        src = _read_source("scripts/predict.py")
        assert "_compute_and_predict.dc_rho_fit" in src
        idx = src.index("_compute_and_predict.dc_rho_fit")
        # 跨整个 try/except 块：log_error 应升级，logger 应升级到 warning
        block = src[max(0, idx - 300):idx + 400]
        assert "log_error" in block
        assert "league" in block
        # 不允许 logger.info 出现（之前"静默默认 ρ"的元凶）
        assert "logger.info" not in block.split("log_error")[0], (
            "log_error 之前不应再有 logger.info（应升级到 warning 或 error）"
        )

    def test_refresh_ml_models_train_log_error_wired(self):
        """B.3 站点 5: _refresh_ml_models.train_league_model。"""
        src = _read_source("scripts/predict.py")
        assert "_refresh_ml_models.train_league_model" in src

    def test_main_loop_refresh_ml_models_log_error_wired(self):
        """B.3 站点 6: --all 模式内层 _main_loop.refresh_ml_models。"""
        src = _read_source("scripts/predict.py")
        assert "_main_loop.refresh_ml_models" in src


# ────────────────────── Round 10 B.1+B.2: 主循环 log_error ──────────────────────────


class TestPredictMainLoopLogError:
    """B.1+B.2 (Round 10): 单场 + 整联赛 catch-all 上 events.jsonl。"""

    def test_per_match_failure_writes_event(self):
        """B.1: ``_generate_predictions`` 单场抛错 → log_error 落 events。

        patch ``predict.calculate_prediction`` 让 b1 强制抛错、g1 正常返回。
        验证：events.jsonl 出现 ``context=predict.generate_predictions.per_match``
        + good_match 仍出 prediction（仅 b1 被 except 接住）。
        """
        import predict

        good_match = {"id": "g1", "name": "A vs B", "kickoff_utc": "2026-10-14T15:00:00Z",
                      "home": "A", "away": "B"}
        bad_match = {"id": "b1", "name": "C vs D", "kickoff_utc": "2026-10-14T17:00:00Z",
                     "home": "C", "away": "D"}

        # predict 调用 ``calculate_prediction(match, ...)``，patch 让 b1 抛错
        def _calc_sometimes_fails(match, **_kw):
            if match["name"] == "C vs D":
                raise RuntimeError("simulated b1 calc failure")
            # g1 返回一个简单 dict（predict 后会赋 match/home/away/kickoff_utc）
            return {"direction": "A 胜", "stars": "3", "confidence_score": 0.7,
                    "predicted_score": "2-1", "poisson_top3": [],
                    "lambda_home": 1.5, "lambda_away": 1.0,
                    "lambda_home_ci95": [0, 3], "lambda_away_ci95": [0, 3],
                    "over_under": "Over 2.5", "btts": "Yes",
                    "dixon_coles_used": False, "dixon_coles_rho": None,
                    "ml_model_used": False, "ml_proba": None,
                    "dixon_coles_league_rho": 0.0, "onside_signals": {},
                    "confidence_note": None, "odds_data_available": False,
                    "probs": {"h": 0.6, "d": 0.25, "a": 0.15},
                    "model_version": "rule-v1"}

        with patch.object(predict, "calculate_prediction", side_effect=_calc_sometimes_fails):
            # predict.adjust_prediction 是把 pred 加 _source_id 标记后透传（默认 stub）
            with patch.object(predict, "adjust_prediction", side_effect=lambda p, _a: p):
                preds = predict._generate_predictions(
                    future=[good_match, bad_match],
                    calibration_offset=None,
                    fifa_rankings={},
                    host_country=None,
                    use_dc=False,
                    fitted_rho=0.0,
                    elo_ratings={},
                    league_key="epl",
                    ai_adjustments=None,
                    data_window="today",
                )
        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "predict.generate_predictions.per_match"]
        assert len(ctx) >= 1, f"应该有 per_match 错误事件，实际 events: {evs}"
        e = ctx[0]
        assert e["kind"] == "error"
        assert e["league"] == "epl"
        assert "simulated b1 calc failure" in e.get("exc_repr", "")
        # good_match 仍生成 prediction（仅 b1 被 catch）
        assert any(p.get("match") == "A vs B" for p in preds), \
            f"good_match 应仍生成 prediction，实际: {preds}"

    def test_per_league_main_failure_writes_event(self, tmp_path, monkeypatch):
        """B.2: main() --all 模式跑 league 报错 → log_error 落 events。

        用 ``sys.argv`` 触发 main()，patch run_league 模拟整联赛失败。
        """
        # patch 单 league kickoff 避免真实跑（Round 10 B.2 主断言是 events）
        import predict

        # setup LEAGUE_CONFIG 模拟只跑一个 league 避免全 6 联赛数据耗时
        with patch.object(predict, "LEAGUE_CONFIG", {"epl": predict.LEAGUE_CONFIG["epl"]}):
            # patch run_league 抛错
            with patch.object(predict, "run_league") as mock_run:
                mock_run.side_effect = RuntimeError("simulated league failure")
                # 调 main --all 路径
                with patch.object(predict, "_refresh_ml_models") as _mock_refresh:
                    test_argv = ["predict.py", "--all", "--data-source", "football-data"]
                    monkeypatch.setattr("sys.argv", test_argv)
                    with patch("sys.stderr", new=open(tmp_path / "stderr.log", "w")):
                        predict.main()

        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "predict.main.per_league"]
        assert len(ctx) >= 1, f"应该有 per_league 错误事件，实际 events: {evs}"
        e = ctx[0]
        assert e["kind"] == "error"
        assert e["league"] == "epl"
        assert "simulated league failure" in e.get("exc_repr", "")


# ──────────────────────────── P5-Q5: --all 跳过 inactive 联赛 ─────────────────────────


class TestPredictAllSkipsInactiveLeagues:
    """P5-Q5：main() --all 模式跳过 active=False 联赛（不撞 ESPN 400）。

    Oracle 实证：10/17+10/18 cron 都因 LEAGUE_CONFIG['nba']['active']=False 但
    data_source='the-odds'（predict.py 不支持）→ 落到 else: fetch_espn(epl) →
    URL 含空 dates= 参数 → ESPN HTTP 400 → 整轮 cron 退出码 1。
    """

    def test_all_mode_skips_nba_in_default_config(self, monkeypatch, caplog):
        """默认 LEAGUE_CONFIG 含 nba(active=False)；--all 跑通且无 run_league(nba) 调用。

        用 caplog 截 logger.info：predict.py 第 32 行 import 时 logger.handler.stream
        已经绑定旧 sys.stderr，后续 patch("sys.stderr", ...) 改的是模块属性但
        StreamHandler 不会刷新引用 —— stderr.log 文件拿不到 logger 输出。
        caplog 直接挂到 logging root/propagation，不依赖 StreamHandler。
        """
        import logging

        import predict

        called_leagues: list[str] = []

        def fake_run_league(league_key, args, now_utc, dates_str, silent=False):
            called_leagues.append(league_key)
            return None  # 不抛错，让 main 跑完 5 个 active league

        caplog.set_level(logging.INFO, logger="predict")
        with patch.object(predict, "run_league", side_effect=fake_run_league), \
             patch.object(predict, "_refresh_ml_models", return_value={}), \
             patch.object(predict, "_cleanup_intermediate", return_value={}):
            test_argv = ["predict.py", "--all", "--data-source", "football-data"]
            monkeypatch.setattr("sys.argv", test_argv)
            predict.main()

        # 关键断言：run_league 不应被 nba 触发
        assert "nba" not in called_leagues, \
            f"--all 不应调 run_league('nba')，实际: {called_leagues}"
        # 至少 5 个 active league 跑了（epl/laliga/bundesliga/seriea/ligue1）
        assert len(called_leagues) >= 5

        # logger.info 应有 skip 提示（纯 ASCII，避免 Windows 控制台编码乱码）
        skip_records = [r for r in caplog.records
                        if r.name == "predict" and "skipping inactive" in r.getMessage()]
        assert len(skip_records) >= 1, \
            f"logger 应提示跳过 inactive 联赛，实际 records: " \
            f"{[(r.name, r.getMessage()) for r in caplog.records]}"
        msg = skip_records[0].getMessage()
        assert "nba" in msg, f"skip 提示应含 nba，实际: {msg}"

    def test_explicit_league_nba_raises_systemexit(self, monkeypatch):
        """显式 --league nba → SystemExit 清晰提示（不是 ESPN 400 崩溃）。

        run_league 内的 active 检查必须先于 _setup_league_run（后者才会撞 ESPN 400）。
        因此不能 mock run_league —— 让真函数跑到 SystemExit 那行。
        但 _setup_league_run 走 ESPN / football-data 网络 IO，需要把
        _setup_league_run 旁路掉，让 run_league 直接到 active 守门。
        """
        import predict

        # 旁路 _setup_league_run 让 run_league 入口的 active 守门先命中
        with patch.object(predict, "_setup_league_run",
                          side_effect=AssertionError("active 守门未先于 _setup 命中")):
            test_argv = ["predict.py", "--league", "nba", "--data-source", "football-data"]
            monkeypatch.setattr("sys.argv", test_argv)
            with pytest.raises(SystemExit) as exc_info:
                predict.main()
            # SystemExit 信息要明确告知用 bball/run.py（纯 ASCII）
            msg = str(exc_info.value)
            assert "nba" in msg and "bball" in msg, \
                f"SystemExit 应提示用 bball/run.py，实际: {msg}"

    def test_active_false_in_custom_config_skipped(self, tmp_path, monkeypatch):
        """临时把 epl 标 active=False → --all 跳过 epl。"""
        import predict

        called: list[str] = []

        def fake_run_league(league_key, args, now_utc, dates_str, silent=False):
            called.append(league_key)
            return None

        patched_cfg = {k: dict(v, active=False) if k == "epl" else v
                       for k, v in predict.LEAGUE_CONFIG.items()}
        with patch.object(predict, "LEAGUE_CONFIG", patched_cfg), \
             patch.object(predict, "run_league", side_effect=fake_run_league), \
             patch.object(predict, "_refresh_ml_models", return_value={}), \
             patch.object(predict, "_cleanup_intermediate", return_value={}):
            test_argv = ["predict.py", "--all", "--data-source", "football-data"]
            monkeypatch.setattr("sys.argv", test_argv)
            with patch("sys.stderr", new=open(tmp_path / "stderr.log", "w")):
                predict.main()

        assert "epl" not in called, \
            f"active=False 的 epl 应被跳过，实际: {called}"


# ──────────────────── R22 #2: _emit_ml_predictions / _emit_ml_outcomes 直接单测 ────────────────────


class TestEmitMlPredictions:
    """R22 #2：直接 unit test _emit_ml_predictions（per_match log_error + 跨 league）。"""

    def test_emit_writes_event_per_match(self, tmp_path, monkeypatch):
        """多场 + 跨 league → 每场 record_prediction 调用一次。"""
        from predict import _emit_ml_predictions
        output = {
            "predictions": [
                {"id": "m1", "home": "A", "away": "B", "league": "epl",
                 "kickoff_utc": "2026-10-15T00:00:00Z",
                 "probs": {"h": 0.5, "d": 0.3, "a": 0.2},
                 "model_version": "v3", "reasoning_factors": {"form_home": 0.6}},
                {"id": "m2", "home": "C", "away": "D", "league": "laliga",
                 "kickoff_utc": "2026-10-15T01:00:00Z",
                 "probs": {"h": 0.4, "d": 0.4, "a": 0.2}},
            ]
        }
        calls = []
        import core.ml_feedback
        monkeypatch.setattr(core.ml_feedback, "record_prediction",
                            lambda **kw: calls.append(kw))
        _emit_ml_predictions(output, league="epl")
        assert len(calls) == 2
        assert calls[0]["home"] == "A" and calls[0]["away"] == "B"
        # 顶层 league 参数优先（predict.py:346 ``league or m.get("league")``），
        # 即便是 m2 自带 league="laliga"，也被顶层 league="epl" 覆盖（保持 run_league 单联赛调用一致）。
        assert calls[0]["league"] == "epl"
        assert calls[1]["league"] == "epl"
        # 但 m1/m2 home/away 还是按各自 match 走
        assert calls[1]["home"] == "C" and calls[1]["away"] == "D"
        # features_snapshot 透传 reasoning_factors（R13 E1 迁移）
        assert calls[0]["features_snapshot"] == {"form_home": 0.6}
        assert calls[1]["features_snapshot"] is None

    def test_emit_skips_non_dict_predictions(self, tmp_path, monkeypatch):
        """predictions 含非 dict / None 条目 → 跳过不抛。"""
        from predict import _emit_ml_predictions
        output = {"predictions": [
            "not a dict",
            None,
            {"id": "m1", "home": "A", "away": "B", "league": "epl",
             "kickoff_utc": "x", "probs": {"h": 0.5, "d": 0.3, "a": 0.2}},
        ]}
        calls = []
        import core.ml_feedback
        monkeypatch.setattr(core.ml_feedback, "record_prediction",
                            lambda **kw: calls.append(kw))
        _emit_ml_predictions(output, league="epl")
        assert len(calls) == 1
        assert calls[0]["home"] == "A"

    def test_emit_per_match_exception_logs_error_and_continues(self, tmp_path, monkeypatch):
        """R13 #5（迁移 #B.1）：某场 record_prediction 抛错 → log_error 落 events，循环继续。"""
        from predict import _emit_ml_predictions
        output = {"predictions": [
            {"id": "bad", "home": "A", "away": "B", "league": "epl",
             "kickoff_utc": "x", "probs": {"h": None, "d": None, "a": None}},  # 第一场 fail
            {"id": "good", "home": "C", "away": "D", "league": "epl",
             "kickoff_utc": "y", "probs": {"h": 0.5, "d": 0.3, "a": 0.2}},  # 第二场 ok
        ]}
        seen = []

        def record(**kw):
            if kw["match_id"] == "bad":
                raise ValueError("simulated per_match failure")
            seen.append(kw["match_id"])
        import core.ml_feedback
        monkeypatch.setattr(core.ml_feedback, "record_prediction", record)

        _emit_ml_predictions(output, league="epl")
        assert seen == ["good"]
        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "_emit_ml_predictions.per_match"]
        assert len(ctx) == 1
        assert ctx[0]["league"] == "epl"
        assert "ValueError" in ctx[0].get("exc_type", "")


class TestEmitMlOutcomes:
    """R22 #2：直接 unit test _emit_ml_outcomes（per-match log_error）。"""

    def test_emit_outcomes_writes_event_per_match(self, tmp_path, monkeypatch):
        """结果列表每条都写 outcome 事件，参数正确（含 home_score/away_score/status）。"""
        from core.output import _emit_ml_outcomes
        matches = [
            {"id": "m1", "home": "A", "away": "B", "league": "epl",
             "kickoff_utc": "2026-10-15T00:00:00Z",
             "home_score": 2, "away_score": 1, "status": "FT"},
        ]
        calls = []
        import core.ml_feedback
        monkeypatch.setattr(core.ml_feedback, "record_outcome",
                            lambda **kw: calls.append(kw))

        _emit_ml_outcomes(matches)
        assert len(calls) == 1
        assert calls[0]["home"] == "A" and calls[0]["away"] == "B"
        assert calls[0]["home_score"] == 2 and calls[0]["away_score"] == 1
        assert calls[0]["status"] == "FT"

    def test_emit_outcomes_per_match_exception_logs_error(self, tmp_path, monkeypatch):
        """R13 #5：单场 outcome 失败 = 该场进不了训练集，比 prediction 失败更关键 → log_error。"""
        from core.output import _emit_ml_outcomes
        # home_score 非数字 → int() 抛 ValueError
        matches = [
            {"id": "bad", "home": "A", "away": "B", "league": "epl",
             "kickoff_utc": "x", "home_score": "x", "away_score": 1},
        ]
        _emit_ml_outcomes(matches)
        evs = _read_events()
        ctx = [e for e in evs if e.get("context") == "_emit_ml_outcomes.per_match"]
        assert len(ctx) == 1, f"应落 per_match 失败事件；实际 {evs}"
        assert "ValueError" in ctx[0].get("exc_type", "")


class TestEmitMlImportFailure:
    """R22 #5：``from core.ml_feedback import record_{prediction,outcome}`` 失败
    → log_error 落 events.jsonl context=_emit_ml_*.import，函数体静默 return。
    """

    def test_emit_predictions_import_failure_logs_and_returns(
            self, tmp_path, monkeypatch):
        """``core.ml_feedback.record_prediction`` 不存在 → import 失败兜底。"""
        # 把 record_prediction 从 module 中删 → ``from core.ml_feedback import
        # record_prediction`` 失败
        import core.ml_feedback
        from predict import _emit_ml_predictions
        monkeypatch.delattr(core.ml_feedback, "record_prediction", raising=False)

        # 不能走到 for 循环 — 函数应 import 失败直接 return
        _emit_ml_predictions(
            {"predictions": [
                {"home": "X", "away": "Y", "league": "epl", "kickoff_utc": "k",
                 "probs": {"h": 0.5, "d": 0.3, "a": 0.2}},
            ]},
            league="epl",
        )
        evs = _read_events()
        import_err = [
            e for e in evs
            if e.get("context") == "_emit_ml_predictions.import"
        ]
        assert len(import_err) == 1, f"应落 _emit_ml_predictions.import；实际 {evs}"
        assert import_err[0]["league"] == "epl"
        # ImportError 仍走 log_error
        assert "ImportError" in import_err[0].get("exc_type", "")

    def test_emit_outcomes_import_failure_logs_and_returns(
            self, tmp_path, monkeypatch):
        """``core.ml_feedback.record_outcome`` 不存在 → import 失败兜底。"""
        import core.ml_feedback
        from core.output import _emit_ml_outcomes
        monkeypatch.delattr(core.ml_feedback, "record_outcome", raising=False)

        _emit_ml_outcomes([
            {"home": "X", "away": "Y", "league": "epl", "kickoff_utc": "k",
             "home_score": 1, "away_score": 0},
        ])
        evs = _read_events()
        import_err = [
            e for e in evs
            if e.get("context") == "_emit_ml_outcomes.import"
        ]
        assert len(import_err) == 1, f"应落 _emit_ml_outcomes.import；实际 {evs}"
        assert "ImportError" in import_err[0].get("exc_type", "")
