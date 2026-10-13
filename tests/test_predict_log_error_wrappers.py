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
