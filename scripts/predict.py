#!/usr/bin/env python3
"""
League Predict v4.1 — Onside 4+1 Signal Model + Dixon-Coles + Monte Carlo
CLI entry point.

Usage: python3 predict.py [--league epl] [--data-source football-data] [--monte-carlo]
       [--n-simulations 10000] [--backtest] [--cleanup] [--dates YYYYMMDD-YYYYMMDD]
       [--no-fetch] [--no-dc]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ── 确保脚本目录与仓库根在 sys.path 中（仓库根含 ai/ 包，LLM 翻译等需要）──
_SCRIPT_DIR = str(Path(__file__).parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
_REPO_ROOT = str(Path(__file__).parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.config import (
    LEAGUE_CONFIG, PREDICTIONS_DIR, DC_RHO, DEFAULT_N_SIMULATIONS, DEFAULT_PAST_DAYS,
)
from core.constants import DEFAULT_AHEAD_DAYS
from core.log import logger
from core.data.fetch import fetch_events
from core.rankings import fetch_fifa_rankings
from core.data.parse import parse_events
from core.model.poisson import fit_dc_rho
from core.model.monte_carlo import (
    monte_carlo_champion, build_league_standings, derive_team_strengths
)
from core.calibration import build_calibration, compute_calibration_offset, load_historical_past_matches
from core.backtest import reconcile_predictions, backtest_with_live_results
from core.output import cleanup_old_files, save_results
from core.predictor import calculate_prediction
from core.elo import get_or_init_elo_ratings, process_match_result, save_elo_ratings

# AI feedback loop — load previous enrichment scores (按联赛在 run_league 内隔离加载)
try:
    from ai.feedback_loop import load_ai_adjustments, adjust_prediction
except Exception:
    def load_ai_adjustments(league_key: str = "") -> dict:  # noqa: ARG001
        return {}

    def adjust_prediction(pred, adj):  # noqa: ARG001
        return pred


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="League Predict v4.1 — Onside 4+1 Signal Model + Dixon-Coles + Monte Carlo"
    )
    parser.add_argument("--league", default="epl",
                        choices=list(LEAGUE_CONFIG.keys()),
                        help="League to predict")
    parser.add_argument("--all", action="store_true",
                        help="Run prediction for all supported leagues")
    parser.add_argument("--data-source", default="",
                        choices=["football-data", "espn", "api-football"],
                        help="Data source (default: per-league config)")
    parser.add_argument("--monte-carlo", dest="monte_carlo", action="store_true",
                        default=True,
                        help="Run Monte Carlo simulation (default: on)")
    parser.add_argument("--no-monte-carlo", dest="monte_carlo", action="store_false",
                        help="Skip Monte Carlo simulation (冠军页将显示为空)")
    parser.add_argument("--n-simulations", type=int, default=DEFAULT_N_SIMULATIONS,
                        help="Monte Carlo iterations")
    parser.add_argument("--backtest", action="store_true", help="Run backtest after prediction")
    parser.add_argument("--cleanup", action="store_true", help="Clean old prediction/result files")
    parser.add_argument("--dates", help="Date range YYYYMMDD-YYYYMMDD")
    parser.add_argument("--past-days", type=int, default=DEFAULT_PAST_DAYS,
                        help="Days to look back for finished matches (feeds calibration/"
                             "accuracy/reconciliation/form; default %d)" % DEFAULT_PAST_DAYS)
    parser.add_argument("--ahead-days", type=int, default=DEFAULT_AHEAD_DAYS,
                        help="Days to look ahead for upcoming matches (default %d)" % DEFAULT_AHEAD_DAYS)
    parser.add_argument("--no-fetch", action="store_true", help="Use local cached data")
    parser.add_argument("--no-dc", action="store_true", help="Disable Dixon-Coles model")
    parser.add_argument("--update-rankings", action="store_true", help="Force refresh FIFA rankings from API")
    parser.add_argument("--train-ml", action="store_true",
                        help="Train per-league ML models from historical data, then exit")
    parser.add_argument("--no-ml", action="store_true", help="Disable ML probability blending for this run")
    parser.add_argument("--dashboard", action="store_true",
                        help="Generate static HTML dashboard (predictions/dashboard_{league}.html)")
    return parser


GHOST_MATCH_GRACE_HOURS = 3.0


def _match_kickoff_utc(match: dict) -> float | None:
    """解析 match.kickoff_utc（ISO 带 Z）为 Unix 时间戳；无/不可解析返回 None。"""
    raw = str(match.get("kickoff_utc") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _drop_ghost_future(future: list, now_utc) -> tuple[list, list]:
    """剔除「开球时刻已早于 now-宽限，却被标为未开赛」的幽灵场次。

    数据源（football-data 等）状态更新滞后时，完赛场次会以 SCHEDULED/TIMED
    混进 future 列表，进而进入推荐与蒙特卡洛剩余赛程。开球时刻距今超过
    GHOST_MATCH_GRACE_HOURS 的场次判为幽灵并剔除；开球时间缺失的场次保留
    （无时刻可证伪，留给页面「时间待定」，不误删）。

    返回 (保留的 future, 被剔除的幽灵场次)。
    """
    now_ts = now_utc.timestamp()
    kept, dropped = [], []
    for m in future:
        kt = _match_kickoff_utc(m)
        if kt is not None and (now_ts - kt) > GHOST_MATCH_GRACE_HOURS * 3600:
            dropped.append(m)
        else:
            kept.append(m)
    return kept, dropped


def _fetch_and_parse(league_key: str, data_source: str, dates_str: str, now_utc, skip_fetch: bool, whole_season: bool = False) -> tuple[list, list, list, list]:
    """获取并解析赛事数据，返回 (events, past, future, in_prog)。

    whole_season=True 时取整季赛程（含未来比赛），用于预测场景。
    """
    if skip_fetch:
        logger.warning("--no-fetch is deprecated, use --data-source football-data for offline mode")
        events = []
    else:
        events = fetch_events("" if whole_season else dates_str, league_key, data_source, whole_season=whole_season)

    logger.info(f"Got {len(events)} events")
    past, future, in_prog = parse_events(events, now_utc)
    # 幽灵场次守卫：数据源状态滞后会把已完赛的比赛仍标为 SCHEDULED/TIMED
    # （实测西甲「莱万特 vs 毕尔巴鄂竞技」开球 09-16T00:00Z，两天后才进入
    # 今日推荐）。这类场次既不该出现在推荐里（比赛已结束），也不该进入
    # 蒙特卡洛剩余赛程。按开球时刻距今超过宽限小时数剔除。
    future, ghost_dropped = _drop_ghost_future(future, now_utc)
    if ghost_dropped:
        logger.warning(f"Dropped {ghost_dropped} ghost matches (kickoff in past but marked scheduled): "
                       + ", ".join(m.get("name") or "?" for m in ghost_dropped))
    logger.info(f"Past: {len(past)}, Future: {len(future)}, In progress: {len(in_prog)}")
    # H6: save_results 偶发 OSError / json 序列化失败（Round 8 扫到），
    # 失败静默让"已完赛落库"对运营不可见，包一层 log_error 上 events.jsonl。
    try:
        save_results(past)
    except Exception as _save_exc:
        from core.observability import log_error
        log_error(_save_exc, context="_fetch_and_parse.save_results",
                  league=league_key, past_count=len(past))
        logger.warning(f"save_results 在 league={league_key} 失败（已落 events.jsonl）: {_save_exc}")
    return events, past, future, in_prog


def _update_elo(past: list, fifa_rankings: dict, force_refresh: bool = False) -> dict[str, float]:
    """从 FIFA 排名初始化 ELO，并用已结束比赛更新，返回评分表。"""
    elo_ratings = get_or_init_elo_ratings(fifa_rankings, force_refresh=force_refresh)
    for m in past:
        try:
            home_goals = int(m.get("score", "0-0").split("-")[0])
            away_goals = int(m.get("score", "0-0").split("-")[1])
            process_match_result(m.get("home_en", ""), m.get("away_en", ""), home_goals, away_goals, elo_ratings)
        except (ValueError, IndexError):
            pass
    save_elo_ratings(elo_ratings)
    logger.info(f"ELO ratings: {len(elo_ratings)} teams")
    return elo_ratings


def _compute_calibration(past: list, future: list, league_key: str | None = None) -> tuple[dict, dict | None]:
    """计算校准参数，返回 (calibration, calibration_offset)。"""
    calibration = build_calibration(past)
    logger.info(f"Calibration: {json.dumps(calibration)}")

    # P1-2 修复: 按联赛过滤历史文件，避免跨联赛校准污染
    historical_past = load_historical_past_matches(days=30, league=league_key)
    calibration_offset = compute_calibration_offset(historical_past, league=league_key)
    if calibration_offset:
        logger.info(f"Calibration offset: {json.dumps(calibration_offset)}")
    else:
        logger.info("Calibration offset: insufficient historical data (<5 matches)")

    cal_file = PREDICTIONS_DIR / "pred_calibration.json"
    if not calibration_offset and cal_file.exists():
        try:
            with open(cal_file, encoding="utf-8") as f:
                calibration_offset = json.load(f)
            logger.info(f"Loaded calibration offset from {cal_file}")
        except Exception as e:
            logger.info(f"Failed to load calibration offset: {e}")

    return calibration, calibration_offset


def _generate_predictions(
    future: list, calibration_offset: dict | None, fifa_rankings: dict,
    host_country: str | None, use_dc: bool, fitted_rho: float,
    elo_ratings: dict[str, float], league_key: str,
    ai_adjustments: dict | None = None, data_window: str = "",
) -> list[dict]:
    """对每场未来比赛生成预测。

    ``data_window`` 只是把本次取数区间写进每条预测：API 层
    (``web/routers/predictions.py``) 逐条暴露 ``data_window``，而它此前只在
    输出顶层存在，导致今日页每条都显示「数据窗口 —」。
    """
    ai_adjustments = ai_adjustments or {}
    predictions = []
    for match in future:
        try:
            pred = calculate_prediction(
                match,
                calibration_offset=calibration_offset,
                fifa_rankings=fifa_rankings,
                host_country=host_country,
                use_dixon_coles=use_dc,
                dc_rho=fitted_rho,
                elo_ratings=elo_ratings,
                league_key=league_key,
            )
            pred["match"] = match["name"]
            pred["home"] = match.get("home", "")
            pred["away"] = match.get("away", "")
            # 英文原名是数据源的原始标识符，必须一路带到预测产物里：
            # 中文名是 to_cn() 的派生显示值，LLM 富化时会被模型"纠正"成别的队
            # （实测 '埃尔切'→'阿根廷'、'勒芒'→'洛森'、'弗赖堡'→'德累斯顿'），
            # 拿它当跨运行/跨源的匹配主键必然丢数据。中文仍用于显示。
            pred["home_en"] = match.get("home_en", "")
            pred["away_en"] = match.get("away_en", "")
            # 开球时间必须带进预测行：API 逐条暴露 kickoff_utc，而它此前只存在于
            # past_matches/future 记录里，导致今日页足球 28/28 行「时间待定」，
            # 前端「今天/明天」时间筛选恒为 0 场。
            pred["kickoff_utc"] = match.get("kickoff_utc", "")
            if data_window:
                pred["data_window"] = data_window
            # Apply AI feedback adjustment (按联赛隔离，P4)
            if ai_adjustments:
                pred = adjust_prediction(pred, ai_adjustments)
            predictions.append(pred)
        except Exception as e:
            # B.1 (Round 10): 单场预测失败落 events.jsonl —— 否则只能 stderr 看，
            # 排错时与 events 流断链。context 字段供 ml_feedback / dashboard filter。
            from core.observability import log_error
            log_error(e, context="predict.generate_predictions.per_match",
                      league=league_key, match=match.get("name") or "?")
            logger.error(f"Prediction failed for {match.get('name', '?')}: {e}")
    logger.info(f"Predicted {len(predictions)} matches")
    return predictions


def _save_output(output: dict, calibration_offset: dict | None, now_utc,
                 *, emit_ml_events: bool = True) -> Path:
    """保存预测结果到文件，返回文件路径。

    文件名必须带联赛后缀：时间戳只有小时精度（%Y-%m-%d_%H），而 --all 模式会在同
    一次运行里依次跑完全部联赛 —— 不带联赛名时 6 个联赛写同一个文件、互相覆盖，
    最终只剩最后一个联赛的数据（实测 --all 跑完 5 个联赛均成功，落盘文件却只有
    ligue1 的 6 场）。
    归并侧 store.latest_by_league() 读的是 JSON 里的 league 字段、不解析文件名，
    故加后缀不影响归并。

    R13 #2 ``emit_ml_events=False``：跳过 ``_emit_ml_predictions`` —— 给 backtest
    路径用：第一次调拿到 ``pred_file`` 给 ``backtest_with_live_results`` 读，emit
    留给 line 670 ``_finalize_league_output`` 统一处理，避免同 prediction 在
    events.jsonl 双写（join 后按 ts 选 latest 但旧行不清 → ML 训练样本翻倍）。

    R13 #3 文件 I/O 兜底：磁盘满 / 权限变更 / RO fs 不让 OSError 抛出去断主流程；
    对齐 H6 (line 152-160) ``save_results`` 模板：log_error 上 events.jsonl +
    logger.warning 回到调用栈上层走完收尾。
    """
    from core.observability import log_error  # R13 #3 兜底（与 H6 同模板）

    ts = now_utc.strftime("%Y-%m-%d_%H")
    league = output.get("league") if isinstance(output.get("league"), str) else ""
    suffix = f"_{league}" if league else ""
    pred_file = PREDICTIONS_DIR / f"prediction_{ts}{suffix}.json"
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(pred_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved: {pred_file}")
    except OSError as e:
        # R13 #3：磁盘满 / 权限变更 / RO fs 时不让 _save_output 抛出去断主流程；
        # 对齐 H6 (line 152-160) save_results 的兜底模板 —— log_error 上 events.jsonl
        # + logger.warning 回到调用栈上层走完收尾。
        log_error(e, context="_save_output.write_predictions",
                  league=league, pred_file=str(pred_file))
        logger.warning(f"_save_output 写预测文件失败（已落 events.jsonl）: {e}")

    # P-6 ML 反馈：每场预测写 events.jsonl 留作训练数据。
    # R13 #2：emit_ml_events=False 时跳过 —— backtest 路径要 _save_output 只写文件
    # 给 backtest_with_live_results 用；emit 让 line 670 的 _finalize_league_output
    # 那次统一处理，避免同一 prediction 在 events.jsonl 双写。
    if emit_ml_events:
        _emit_ml_predictions(output, league=league)

    if calibration_offset:
        cal_file = PREDICTIONS_DIR / "pred_calibration.json"
        try:
            with open(cal_file, "w", encoding="utf-8") as f:
                json.dump(calibration_offset, f, indent=2)
        except OSError as e:
            log_error(e, context="_save_output.write_calibration",
                      league=league, cal_file=str(cal_file))
            logger.warning(f"_save_output 写校准文件失败（已落 events.jsonl）: {e}")

    return pred_file


def _emit_ml_predictions(output: dict, *, league: str) -> None:
    """P-6 ML 反馈：每场预测写 events.jsonl 一行（与 outcomes 通过 match_key 关联）。

    失败安全：写 events 失败不能影响主流程（吞 OSError）。
    """
    try:
        from core.ml_feedback import record_prediction
    except Exception as exc:
        # B.1 (Round 11): ml_feedback import 失败升级到 log_error，
        # 让 ml 反馈链路断点可观测（之前仅 logger.debug，排错断链）。
        try:
            from core.observability import log_error
            log_error(exc, context="_emit_ml_predictions.import",
                      league=league)
        except Exception:
            pass
        logger.debug(f"ml_feedback import skipped: {exc}")
        return
    predictions = output.get("predictions") if isinstance(output, dict) else None
    if not isinstance(predictions, list):
        return
    for m in predictions:
        if not isinstance(m, dict):
            continue
        try:
            probs = m.get("probs") or {}
            record_prediction(
                league=league or m.get("league") or "",
                match_id=str(m.get("id") or m.get("name") or ""),
                home=str(m.get("home") or m.get("home_en") or ""),
                away=str(m.get("away") or m.get("away_en") or ""),
                kickoff_utc=str(m.get("kickoff_utc") or ""),
                probs={
                    "h": float(probs.get("h", 0.0) or 0.0),
                    "d": float(probs.get("d", 0.0) or 0.0),
                    "a": float(probs.get("a", 0.0) or 0.0),
                },
                model=str(m.get("model_version") or m.get("model") or "default"),
                goal_line=m.get("goal_line"),
                market_signal=m.get("market"),
                # Round 11 E1: 把 predictor._debug_fields 的 reasoning_factors
                # 当 features_snapshot —— 内含 ELO/form/onside/fifa 等 ~16 维
                # 数值。缺关键 onside_signals 时仍允许下游 train_league_model
                # 走 fallback（参 retrain_baseline）。
                features_snapshot=m.get("reasoning_factors"),
                extra=None,
            )
        except Exception as exc:
            # B.1 (Round 11): 单场失败升级到 log_error —— 排错"全场 prediction
            # 数 vs events 行数"对不上时能定位是哪场。
            try:
                from core.observability import log_error
                log_error(exc, context="_emit_ml_predictions.per_match",
                          league=league,
                          match=str(m.get("name") or "?"),
                          match_id=str(m.get("id") or ""))
            except Exception:
                pass
            logger.debug(f"record_prediction skipped: {exc}")


def _print_summary(predictions: list, calibration: dict, calibration_offset: dict | None,
                    monte_carlo_result: dict | None, n_simulations: int,
                    accuracy_summary: dict | None = None) -> None:
    """打印 stderr 摘要。"""
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Window calibration: {calibration.get('total_matches',0)} finished | "
          f"home win {calibration.get('home_win_rate',0)*100:.0f}% "
          f"draw {calibration.get('draw_rate',0)*100:.0f}% "
          f"away win {calibration.get('away_win_rate',0)*100:.0f}%", file=sys.stderr)
    print(f"    Odds favorite accuracy: {calibration.get('odds_accuracy',0)*100:.0f}% "
          f"({calibration.get('favored_won',0)}/{calibration.get('favored_by_odds',0)})", file=sys.stderr)
    if calibration_offset:
        print(f"Calibration offset(n={calibration_offset['sample_size']}): "
              f"home x{calibration_offset['home_correction']} "
              f"draw x{calibration_offset['draw_correction']} "
              f"away x{calibration_offset['away_correction']}", file=sys.stderr)
        print(f"   Actual distribution: home {calibration_offset['actual_home_rate']} | "
              f"draw {calibration_offset['actual_draw_rate']} | "
              f"away {calibration_offset['actual_away_rate']}", file=sys.stderr)
    else:
        print("Calibration offset: insufficient data (<5 matches), skipping", file=sys.stderr)
    print(f"To predict: {len(predictions)} matches", file=sys.stderr)
    for p in predictions:
        poisson_str = " / ".join(f"{t['score']}({t['prob']:.0%})" for t in p.get('poisson_top3', [])[:3])
        ci_home = p.get('lambda_home_ci95', (0,0))
        ci_away = p.get('lambda_away_ci95', (0,0))
        cal = ' [cal]' if calibration_offset else ''
        dc = ' [DC]' if p.get('dixon_coles_used') else ''
        print(f"  {p['match']} | {p['direction']} {p['stars']}{cal}{dc} | "
              f"{p['predicted_score']} | l={p.get('lambda_home',0)}[{ci_home[0]}-{ci_home[1]}]/"
              f"{p.get('lambda_away',0)}[{ci_away[0]}-{ci_away[1]}] | {poisson_str}", file=sys.stderr)

    if monte_carlo_result:
        print(f"\nMonte Carlo champion prediction (n={n_simulations}):", file=sys.stderr)
        for team, prob in list(monte_carlo_result["champion_probs"].items())[:5]:
            print(f"  {team}: {prob:.1%}", file=sys.stderr)

    if accuracy_summary:
        print("\nAccuracy summary:", file=sys.stderr)
        for window, a in accuracy_summary.items():
            print(f"  {window}: dir {a['direction_accuracy']*100:.0f}% | "
                  f"score {a['score_accuracy']*100:.0f}% | "
                  f"O/U {a['over_under_accuracy']*100:.0f}% (n={a['reconciled']})", file=sys.stderr)

    print(f"{'='*60}", file=sys.stderr)


def _season_mc_inputs(league_key: str, data_source: str, now_utc) -> tuple[list, dict, dict]:
    """取整季赛程，返回 (remaining_fixtures, initial_standings, team_strengths)。

    一次请求覆盖整季（football-data 实测：不传日期参数即返回本赛季全部
    380 场 = 40 已结束 + 340 未开赛，20 支球队齐全）：已结束比赛用于播种
    当前积分榜并推导每队攻防强度，剩余赛程用于模拟。

    旧实现只用「窗口内的 6 场」当 fixtures，等于拿 6 场球去推整个联赛的
    夺冠概率，只能覆盖 12 支球队，语义不成立。
    """
    events = fetch_events("", league_key, data_source, whole_season=True)
    past, future, _in_prog = parse_events(events, now_utc)
    # 幽灵场次守卫（同 _fetch_and_parse）：完赛仍被标未开赛的比赛不能混进剩余赛程
    future, mc_ghosts = _drop_ghost_future(future, now_utc)
    if mc_ghosts:
        logger.warning(f"Season MC dropped {len(mc_ghosts)} ghost matches (kickoff in past but marked scheduled)")

    finished: list = []
    for m in past:
        score = m.get("score") or ""
        if "-" not in score:
            continue
        try:
            hg_s, ag_s = score.split("-")[:2]
            hg, ag = int(hg_s), int(ag_s)
        except (ValueError, IndexError):
            continue
        if not m.get("home") or not m.get("away"):
            continue
        finished.append({"home": m["home"], "away": m["away"],
                         "home_goals": hg, "away_goals": ag})

    remaining = [{"home": m.get("home", ""), "away": m.get("away", "")}
                 for m in future if m.get("home") and m.get("away")]

    logger.info(f"Season MC inputs: finished={len(finished)}, remaining={len(remaining)}")
    return remaining, build_league_standings(finished), derive_team_strengths(finished)


def _annotate_monte_carlo(predictions: list, run_monte_carlo: bool, n_simulations: int,
                          fitted_rho: float, tournament_type: str,
                          league_key: str = "", data_source: str = "", now_utc=None) -> dict | None:
    """蒙特卡洛冠军概率。返回冠军榜 dict（未启用/无可用赛程时 None）。

    优先用整季剩余赛程 + 当前积分榜播种；取数失败时退回窗口内赛程
    （语义弱但不会让冠军页整个空掉）。
    """
    if not (run_monte_carlo and predictions):
        return None

    fixtures: list = []
    initial_standings: dict = {}
    team_strengths: dict = {}
    try:
        fixtures, initial_standings, team_strengths = _season_mc_inputs(
            league_key, data_source, now_utc)
    except Exception as exc:
        # B.3 (Round 11): 取数异常升级 events —— 取整季赛程本就会跨多月，
        # 偶发拉取失败应当可观测以便排查数据源稳定性。
        try:
            from core.observability import log_error
            log_error(exc, context="_annotate_monte_carlo.season_inputs",
                      league=league_key, data_source=data_source)
        except Exception:
            pass
        logger.warning(f"整季赛程取数失败，回退窗口内赛程: {exc}")

    if not fixtures:
        # 回退：窗口内赛程（覆盖球队少，仅供降级展示）
        logger.warning("整季剩余赛程为空，回退为窗口内赛程（冠军概率语义较弱）")
        for p in predictions:
            home, away = p.get("home", ""), p.get("away", "")
            if not (home and away):
                continue
            fixtures.append({"home": home, "away": away})
            for t in (home, away):
                team_strengths.setdefault(t, {
                    "lambda_home": p.get("lambda_home", 1.5),
                    "lambda_away": p.get("lambda_away", 1.2),
                })
        initial_standings = {}

    if not fixtures:
        logger.warning("蒙特卡洛无可用赛程，跳过")
        return None

    monte_carlo_result = monte_carlo_champion(
        fixtures, team_strengths, n_simulations=n_simulations,
        rho=fitted_rho, tournament_type=tournament_type,
        initial_standings=initial_standings,
    )
    logger.info(f"Monte Carlo complete. Top champion: {list(monte_carlo_result['champion_probs'].items())[:3]}")
    return monte_carlo_result


def _build_league_output(now_utc, dates_str, league_key, tournament_type, data_source, use_dc, fitted_rho, calibration, calibration_offset, past, predictions, _t_start, monte_carlo_result, args) -> tuple[dict, dict]:
    """组装联赛输出 dict：reconciliation、命中率小结、静态 Dashboard 触发。
    """
    # 7. 构建输出
    output = {
        "generated_at": now_utc.isoformat(), "data_window": dates_str,
        "status": "ok", "league": league_key,
        "tournament_type": tournament_type, "data_source": data_source,
        "dixon_coles_enabled": use_dc,
        "dixon_coles_rho": fitted_rho if use_dc else None,
        "calibration": calibration, "calibration_offset": calibration_offset,
        "past_matches": past, "predictions": predictions,
        "timing_ms": {"total": round((time.time() - _t_start) * 1000)},
    }

    reconciliation = reconcile_predictions(past)
    if reconciliation:
        output["reconciliation"] = reconciliation

    if monte_carlo_result:
        output["monte_carlo"] = monte_carlo_result

    # 7.4 命中率小结（P5 产品建议：近 7/30 天各联赛方向/比分/大小球命中率）
    accuracy_summary: dict[str, Any] = {}
    try:
        from core.backtest import league_accuracy
        for _d in (7, 30):
            _acc = league_accuracy(league_key, days=_d)
            if _acc:
                accuracy_summary[f"{_d}d"] = _acc
    except Exception as e:
        # B.3 (Round 11): accuracy_summary 失败升级 events（DB 拉取失败 / db 文件损坏等）
        try:
            from core.observability import log_error
            log_error(e, context="_build_league_output.accuracy_summary",
                      league=league_key)
        except Exception:
            pass
        logger.warning(f"Accuracy summary failed: {e}")
    if accuracy_summary:
        output["accuracy_summary"] = accuracy_summary

    # 7.5 生成静态 Dashboard（P5-1：接入此前未使用的高完成度孤岛功能）
    if args.dashboard:
        from core.dashboard import generate_dashboard
        dash_path = PREDICTIONS_DIR / f"dashboard_{league_key}.html"
        try:
            generate_dashboard(output, dash_path)
            logger.info(f"Dashboard generated: {dash_path}")
        except Exception as e:
            # B.3 (Round 11): dashboard 失败升级 events（template 错 / asset 缺）
            try:
                from core.observability import log_error
                log_error(e, context="_build_league_output.dashboard",
                          league=league_key)
            except Exception:
                pass
            logger.warning(f"Dashboard generation failed: {e}")
    return output, accuracy_summary


def _setup_league_run(league_key: str, args, now_utc, dates_str):
    """第一阶段：参数解包、获取并解析赛事数据、ELO 初始化。返回后续阶段所需全部数据。
    """
    data_source = args.data_source
    run_monte_carlo = args.monte_carlo
    n_simulations = args.n_simulations
    run_backtest = args.backtest
    use_dc = not args.no_dc
    skip_fetch = args.no_fetch

    _t_start = time.time()

    league_config = LEAGUE_CONFIG.get(league_key, LEAGUE_CONFIG["epl"])
    host_country = league_config.get("host_country")
    tournament_type = league_config.get("tournament_type", "league")

    logger.info(f"League: {league_key} ({league_config['name']}), source: {data_source}, type: {tournament_type}")

    # 1. 获取并解析赛事数据（始终取整季，确保含未来比赛）
    events, past, future, in_prog = _fetch_and_parse(league_key, data_source, dates_str, now_utc, skip_fetch, whole_season=True)

    # 1.1 按 ahead_days 过滤未来比赛（避免推荐整季几百场）
    _ahead_days = max(1, int(getattr(args, "ahead_days", DEFAULT_AHEAD_DAYS)))
    _ahead_cutoff_ts = (now_utc + timedelta(days=_ahead_days)).timestamp()
    future = [m for m in future if _match_kickoff_utc(m) and _match_kickoff_utc(m) <= _ahead_cutoff_ts]

    # 1.2 ESPN 赔率富化（P0-2 修复）：默认 football-data 源不带赔率，
    # 全部预测行 market.status=missing，今日推荐 KPI 与串关组合赔率恒「—」。
    # 只有当预测行确实缺赔率、且联赛配置了 espn_slug、且数据源不是 ESPN 本身时才富化。
    # ESPN 是公开 scoreboard（无 key 无配额），抓取失败非致命。
    # ESPN scoreboard 的 dates 参数只接受单日 YYYYMMDD（区间会返回 0 场），
    # 故按 future 实际开球日逐日抓取，避免为 30 天回看窗口付 30 次请求。
    _odds_enriched = 0
    if future and not skip_fetch and data_source != "espn":
        espn_slug = league_config.get("espn_slug")
        if espn_slug:
            try:
                from core.data.odds_enrich import enrich_soccer_odds
                from core.data.fetch import fetch_espn
                days = sorted({str(m.get("kickoff_utc"))[:10].replace("-", "")
                               for m in future if m.get("kickoff_utc")})
                espn_events: list = []
                for day in days:
                    espn_events.extend(fetch_espn(day, espn_slug))
                _odds_enriched = enrich_soccer_odds(future, espn_events, now_utc)
            except Exception as e:
                # H7: ESPN 富化失败（scoreboard 429 / json 解析错等）落 events.jsonl
                from core.observability import log_error
                log_error(e, context="espn_odds_enrich",
                          league=league_key, future_count=len(future),
                          espn_slug=espn_slug)
                logger.warning(f"ESPN odds enrichment failed (non-fatal, 已落 events.jsonl): {e}")
    if _odds_enriched:
        logger.info(f"League {league_key}: ESPN odds enriched {_odds_enriched}/{len(future)} future matches")

    # 1.5 累计历史完赛记录（供线上 ML 训练 / calibration 跨运行累计，P5）
    try:
        from core.calibration import append_historical_past_matches
        _added = append_historical_past_matches(league_key, past)
        if _added:
            logger.info(f"Accumulated {_added} historical past matches (league={league_key})")
    except Exception as e:
        # H7: append_historical 失败（DB locked / fs quota 等）落 events.jsonl
        from core.observability import log_error
        log_error(e, context="append_historical_past_matches",
                  league=league_key, past_count=len(past))
        logger.warning(f"Failed to accumulate historical past matches (已落 events.jsonl): {e}")

    # 2. ELO 评分初始化与更新
    fifa_rankings = fetch_fifa_rankings()
    logger.info(f"FIFA rankings loaded: {len(fifa_rankings)} teams")
    elo_ratings = _update_elo(past, fifa_rankings, force_refresh=args.update_rankings)
    return (data_source, run_monte_carlo, n_simulations, run_backtest, use_dc,
            _t_start, host_country, tournament_type, past, future, fifa_rankings, elo_ratings)


def _compute_and_predict(past, future, league_key, fifa_rankings, host_country, use_dc, elo_ratings,
                         data_window: str = ""):
    """校准、DC ρ 拟合与预测生成。返回 (calibration, calibration_offset, fitted_rho, predictions)。
    """
    # 3. 校准
    calibration, calibration_offset = _compute_calibration(past, future, league_key)

    # 4. Dixon-Coles ρ 拟合
    fitted_rho = DC_RHO
    if use_dc:
        try:
            fitted_rho = fit_dc_rho(past)
        except Exception as e:
            # B.3 (Round 11): DC ρ 拟合失败最危险 —— 现默认 ρ 兜底会让 league 间
            # DC 相关结构失效且运营看不到；升级 log_error + warning。
            try:
                from core.observability import log_error
                log_error(e, context="_compute_and_predict.dc_rho_fit",
                          league=league_key)
            except Exception:
                pass
            logger.warning(f"DC rho fit failed: {e}, 退回默认 {DC_RHO}")

    # 5. 生成预测（AI 反馈分数按联赛隔离加载，P4）
    ai_adjustments = load_ai_adjustments(league_key)
    predictions = _generate_predictions(
        future, calibration_offset, fifa_rankings, host_country,
        use_dc, fitted_rho, elo_ratings, league_key, ai_adjustments,
        data_window=data_window,
    )
    return calibration, calibration_offset, fitted_rho, predictions


def _finalize_league_output(silent, output, calibration_offset, now_utc, predictions, calibration, monte_carlo_result, n_simulations, accuracy_summary, _t_start):
    """收尾：打印 JSON、落盘、摘要与运行时统计（stderr）。
    """
    # 9. 输出
    if not silent:
        print(json.dumps(output, indent=2, ensure_ascii=False))

    _save_output(output, calibration_offset, now_utc)
    _print_summary(predictions, calibration, calibration_offset, monte_carlo_result, n_simulations, accuracy_summary)

    _elapsed = (time.time() - _t_start) * 1000
    print(f"Total runtime: {_elapsed:.0f}ms", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)


def run_league(league_key: str, args, now_utc, dates_str, silent: bool = False) -> dict | None:
    data_source, run_monte_carlo, n_simulations, run_backtest, use_dc, _t_start, host_country, tournament_type, past, future, fifa_rankings, elo_ratings = _setup_league_run(league_key, args, now_utc, dates_str)

    if not future and not past:
        logger.info("No matches found in window")
        return {
            "generated_at": now_utc.isoformat(), "data_window": dates_str,
            "status": "no_matches", "league": league_key,
            "tournament_type": tournament_type,
            "message": f"No matches in window ({dates_str})",
            "calibration": {"note": "no data"}, "past_matches": [], "predictions": [],
        }

    if not future and not run_backtest:
        logger.info("No future matches to predict")
        calibration = build_calibration(past)
        output = {
            "generated_at": now_utc.isoformat(), "data_window": dates_str,
            "status": "no_future_matches", "league": league_key,
            "tournament_type": tournament_type,
            "message": f"No matches to predict in window ({dates_str})",
            "calibration": calibration, "past_matches": past, "predictions": [],
        }
        reconciliation = reconcile_predictions(past)
        if reconciliation:
            output["reconciliation"] = reconciliation
        return output

    # 3. 校准
    calibration, calibration_offset, fitted_rho, predictions = _compute_and_predict(past, future, league_key, fifa_rankings, host_country, use_dc, elo_ratings, data_window=dates_str)

    # 6. Monte Carlo（默认开启；用整季剩余赛程 + 当前积分榜播种）
    monte_carlo_result = _annotate_monte_carlo(
        predictions, run_monte_carlo, n_simulations, fitted_rho, tournament_type,
        league_key=league_key, data_source=data_source, now_utc=now_utc,
    )

    # 7. 构建输出
    output, accuracy_summary = _build_league_output(now_utc, dates_str, league_key, tournament_type, data_source, use_dc, fitted_rho, calibration, calibration_offset, past, predictions, _t_start, monte_carlo_result, args)

    # 8. 回测（可选）
    if run_backtest:
        # R13 #2：backtest 路径只写文件不 emit events —— 让 line 670 _finalize_league_output
        # 那次统一处理。否则同 prediction 在 events.jsonl 双写 → ml_feedback.join
        # 按 ts 选 latest 但旧行不清 → ML 训练样本翻倍（train_accuracy/brier 偏乐观）。
        pred_file = _save_output(output, calibration_offset, now_utc, emit_ml_events=False)
        bt = backtest_with_live_results(str(pred_file))
        output["backtest"] = bt
        logger.info(f"Backtest: {bt.get('status')} matched={bt.get('matched_matches')} acc={bt.get('accuracy')}")

    _finalize_league_output(silent, output, calibration_offset, now_utc, predictions, calibration, monte_carlo_result, n_simulations, accuracy_summary, _t_start)

    return output


def _apply_ml_args(args) -> bool:
    """ML-5 开关与训练/清理快捷路径。

    Returns:
        True 表示已执行训练或清理并应提前结束，False 表示继续正常预测流程。
    """
    from core.config import ML_CONFIG
    if args.no_ml:
        ML_CONFIG["enabled"] = False
        logger.info("ML blending disabled via --no-ml")

    if args.train_ml:
        from core.model.ml_model import train_all_league_models
        from core.rankings import fetch_fifa_rankings
        from core.elo import get_or_init_elo_ratings
        fifa = fetch_fifa_rankings()
        elo = get_or_init_elo_ratings(fifa)
        logger.info("Training per-league ML models from historical data...")
        results = train_all_league_models(elo_ratings=elo)
        for league_key, model in results.items():
            status = "trained" if model is not None else "skipped (insufficient data)"
            print(f"  {league_key}: {status}", file=sys.stderr)
        return True

    if args.cleanup:
        cleanup_old_files(days=7)
        return True

    return False


def _refresh_ml_models(league_key=None, max_age_days: float = 1.0, base_dir=None) -> dict:
    """过期/缺失的 ML 模型按需重训（不修改 core，仅复用公开训练函数）。

    每次预测跑完后调用：刚结算的比赛进入历史样本，模型随新比赛自更新。
    返回 {league_key: True|False|None}（True=已重训，False=样本不足回退规则，
    None=训练异常跳过，不影响本次预测）。
    """
    from core.config import LEAGUE_CONFIG, FOOTBALL_DIR
    from core.model.ml_model import train_league_model
    from pathlib import Path
    import os
    import time as _t
    keys = [league_key] if league_key else list(LEAGUE_CONFIG)
    base = Path(base_dir) if base_dir else Path(FOOTBALL_DIR) / "references"
    out: dict = {}
    for k in keys:
        p = base / f"ml_model_{k}.json"
        try:
            stale = (not p.exists()) or ((_t.time() - os.path.getmtime(p)) / 86400.0) >= max_age_days
        except OSError:
            stale = True
        if not stale:
            continue
        try:
            m = train_league_model(k, base_dir=base_dir)
            out[k] = m is not None
        except Exception as e:
            # B.3 (Round 11): ML 模型重训失败升级 events（特征不一致 / sample 不足）
            try:
                from core.observability import log_error
                log_error(e, context="_refresh_ml_models.train_league_model",
                          league=k, base_dir=str(base_dir))
            except Exception:
                pass
            logger.warning(f"ML refresh failed for {k}: {e}")
            out[k] = None
    return out


def _cleanup_intermediate(days: int = 7) -> dict:
    """方案甲：删除运行期中间件（>days 天），保留训练所需数据。

    中间件＝运行期 json：scripts/predictions/*.json、scripts/results/*.json、
    scripts/references/ 下除训练必需外的 json（含 .calibration_state*）。
    始终保留（训练/参照必需，>days 天也不删）：references/historical_past_matches.json、
    references/ml_model_*.json、references/team_translations.json。
    返回 {predictions,results,references} 各自删除数。
    """
    import time as _t
    from core.config import FOOTBALL_DIR
    cutoff = _t.time() - days * 86400
    base = Path(FOOTBALL_DIR)
    removed = {"predictions": 0, "results": 0, "references": 0}

    def _prot_references(name: str) -> bool:
        return (name == "historical_past_matches.json"
                or name.startswith("ml_model_")
                or name == "team_translations.json")

    for sub in ("predictions", "results"):
        d = base / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            try:
                if f.is_file() and f.suffix == ".json" and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed[sub] += 1
                    logger.info(f"清理中间件 {sub}/{f.name}")
            except OSError:
                continue
    rd = base / "references"
    if rd.is_dir():
        for f in rd.iterdir():
            try:
                if (f.is_file() and f.suffix == ".json"
                        and not _prot_references(f.name)
                        and f.stat().st_mtime < cutoff):
                    f.unlink()
                    removed["references"] += 1
                    logger.info(f"清理中间件 references/{f.name}")
            except OSError:
                continue
    total = sum(removed.values())
    if total:
        logger.info(f"中间件清理完成：删除 {total} 个 >{days} 天文件 {removed}")
    return removed


def main() -> int:
    """主入口。返回退出码（0=全部成功，1=有联赛失败 / 致命错）。

    R18 #1: 生产 cron / systemd timer / k8s CronJob 调度 ``predict.py`` 时——
    之前 main() 全程无 ``sys.exit``，5 联赛全失败（如 Postgres 挂 / 网络断）也以 0 退出，
    cron 视为"成功"，运维侧无可见报错（events.jsonl 落 logs 但需专门监控拉取）。
    现在改成：
      - 单联赛分支：run_league 失败 → return 1
      - --all 分支：失败联赛数 > 0 → return 1（fail-fast 语义）
      - 清理失败 / ML refresh 失败 不影响退出码（次要步骤）
    """
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    parser = build_parser()
    args = parser.parse_args()

    if _apply_ml_args(args):
        return 0  # ML 训练/清理快捷路径

    # 使用北京时间（BJT）计算日期范围，而非UTC
    now_bjt = datetime.now(timezone(timedelta(hours=8)))

    if args.dates:
        dates_str = args.dates
    else:
        # 回看 past_days 天：窗口若只取「今天-明天」，past_matches 恒为空，
        # 会连带打死校准、命中率、对账与模型 form/record 特征（见 constants.py）。
        _past_days = max(0, int(getattr(args, "past_days", DEFAULT_PAST_DAYS)))
        _ahead_days = max(1, int(getattr(args, "ahead_days", DEFAULT_AHEAD_DAYS)))
        d0 = (now_bjt - timedelta(days=_past_days)).strftime("%Y%m%d")
        d2 = (now_bjt + timedelta(days=_ahead_days)).strftime("%Y%m%d")
        dates_str = f"{d0}-{d2}"

    if args.all:
        # R15a #8: --all 模式批量边界事件锚点 —— 5 联赛 × N matches 跑测后
        # grep 时间窗口定位 "这次跑覆盖了哪些 league / 几个 succeeded" 更直观。
        from core.observability import log_event as _log_event_batch
        _log_event_batch("batch_start", mode="all", n_leagues=len(LEAGUE_CONFIG))
        all_outputs = []
        failed_leagues = 0
        for league_key in LEAGUE_CONFIG:
            print(f"\n{'#'*60}", file=sys.stderr)
            print(f"# LEAGUE: {league_key}", file=sys.stderr)
            print(f"{'#'*60}", file=sys.stderr)
            try:
                result = run_league(league_key, args, now_bjt, dates_str, silent=True)
                if result:
                    all_outputs.append(result)
                try:
                    _refresh_ml_models(league_key)
                except Exception as _e:
                    # B.3 (Round 11): 单联赛 ML refresh 失败落 events
                    # （--all 模式内层 catch-all，区别于上层 B.2 per_league）
                    try:
                        from core.observability import log_error
                        log_error(_e, context="_main_loop.refresh_ml_models",
                                  league=league_key)
                    except Exception:
                        pass
                    logger.warning(f"ML refresh failed for {league_key}: {_e}")
            except Exception as e:
                # B.2 (Round 10): --all 模式整联赛预测失败落 events.jsonl
                # （如 data_source 网络故障 / DC ρ 拟合严重错等）。
                failed_leagues += 1
                try:
                    from core.observability import log_error
                    log_error(e, context="predict.main.per_league",
                              league=league_key, args=str(args)[:200])
                except Exception:
                    pass
                logger.error(f"Prediction failed for {league_key}: {e}")
        # R15a #8: 批量边界事件锚点 —— n_succeeded 标本次成功 run 一场数（不论 0/1/N）。
        n_succeeded = sum(1 for o in all_outputs if o is not None)
        _log_event_batch("batch_done", mode="all", n_succeeded=n_succeeded)
        # Print combined JSON array for --all mode
        print(json.dumps(all_outputs, indent=2, ensure_ascii=False))
        # R18 #1: 任一联赛失败 → 退出码 1（fail-fast：cron 可见失败）
        return 1 if failed_leagues > 0 else 0

    # 单联赛分支
    failed = False
    try:
        run_league(args.league, args, now_bjt, dates_str)
    except Exception as e:
        # R15a #1: 单联赛 run_league 失败落 log_error（已对齐 --all B.2 模板）
        from core.observability import log_error
        log_error(e, context="predict.main.run_league",
                  league=args.league, args=str(args)[:200])
        logger.error(f"Prediction failed for {args.league}: {e}")
        failed = True
    try:
        _refresh_ml_models(args.league)
    except Exception as _e:
        # R15a #1: ML refresh 失败落 log_error，但**不影响退出码**（次要步骤）
        from core.observability import log_error
        log_error(_e, context="predict.main.refresh_ml_models",
                  league=args.league)
        logger.warning(f"ML refresh failed for {args.league}: {_e}")

    # 运行期中间件清理（方案甲）：保留训练所需数据；--backtest 跳过（不删回测基线输入）。
    if not getattr(args, "backtest", False):
        try:
            _cleanup_intermediate(days=7)
        except Exception as _e:
            logger.warning(f"中间件清理失败: {_e}")
            # 清理失败不影响退出码（次要步骤）

    # R18 #1: 单联赛 run_league 失败 → 退出码 1；ML refresh 失败不影响
    return 1 if failed else 0


if __name__ == "__main__":
    # R18 #1: 把 main() 返回的 int 透传给进程退出码 —— 之前直接 main() 让所有失败都 0 退。
    raise SystemExit(main())
