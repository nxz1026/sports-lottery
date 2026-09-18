#!/usr/bin/env python3
"""
League Predict v4.0 — Onside 4+1 Signal Model + Dixon-Coles + Monte Carlo
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
    LEAGUE_CONFIG, PREDICTIONS_DIR, DC_RHO, DEFAULT_N_SIMULATIONS
)
from core.log import logger
from core.data.fetch import fetch_events
from core.rankings import fetch_fifa_rankings
from core.data.parse import parse_events
from core.model.poisson import fit_dc_rho
from core.model.monte_carlo import monte_carlo_champion
from core.calibration import build_calibration, compute_calibration_offset, load_historical_past_matches
from core.backtest import reconcile_predictions, backtest_with_live_results
from core.output import cleanup_old_files, save_results
from core.predictor import calculate_prediction
from core.elo import get_or_init_elo_ratings, process_match_result, save_elo_ratings

# AI feedback loop — load previous enrichment scores (按联赛在 run_league 内隔离加载)
try:
    from ai.feedback_loop import load_ai_adjustments, adjust_prediction
except Exception:
    load_ai_adjustments = lambda league_key="": {}
    adjust_prediction = lambda pred, adj: pred


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="League Predict v4.0 — Onside 4+1 Signal Model + Dixon-Coles + Monte Carlo"
    )
    parser.add_argument("--league", default="epl",
                        choices=list(LEAGUE_CONFIG.keys()),
                        help="League to predict")
    parser.add_argument("--all", action="store_true",
                        help="Run prediction for all supported leagues")
    parser.add_argument("--data-source", default="",
                        choices=["football-data", "espn", "api-football"],
                        help="Data source (default: per-league config)")
    parser.add_argument("--monte-carlo", action="store_true", help="Run Monte Carlo simulation")
    parser.add_argument("--n-simulations", type=int, default=DEFAULT_N_SIMULATIONS,
                        help="Monte Carlo iterations")
    parser.add_argument("--backtest", action="store_true", help="Run backtest after prediction")
    parser.add_argument("--cleanup", action="store_true", help="Clean old prediction/result files")
    parser.add_argument("--dates", help="Date range YYYYMMDD-YYYYMMDD")
    parser.add_argument("--no-fetch", action="store_true", help="Use local cached data")
    parser.add_argument("--no-dc", action="store_true", help="Disable Dixon-Coles model")
    parser.add_argument("--update-rankings", action="store_true", help="Force refresh FIFA rankings from API")
    parser.add_argument("--train-ml", action="store_true",
                        help="Train per-league ML models from historical data, then exit")
    parser.add_argument("--no-ml", action="store_true", help="Disable ML probability blending for this run")
    parser.add_argument("--dashboard", action="store_true",
                        help="Generate static HTML dashboard (predictions/dashboard_{league}.html)")
    return parser


def _fetch_and_parse(league_key: str, data_source: str, dates_str: str, now_utc, skip_fetch: bool) -> tuple[list, list, list, list]:
    """获取并解析赛事数据，返回 (events, past, future, in_prog)。"""
    if skip_fetch:
        logger.warning("--no-fetch is deprecated, use --data-source football-data for offline mode")
        events = []
    else:
        events = fetch_events(dates_str, league_key, data_source)

    logger.info(f"Got {len(events)} events")
    past, future, in_prog = parse_events(events, now_utc)
    logger.info(f"Past: {len(past)}, Future: {len(future)}, In progress: {len(in_prog)}")
    save_results(past)
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
    ai_adjustments: dict | None = None,
) -> list[dict]:
    """对每场未来比赛生成预测。"""
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
            # Apply AI feedback adjustment (按联赛隔离，P4)
            if ai_adjustments:
                pred = adjust_prediction(pred, ai_adjustments)
            predictions.append(pred)
        except Exception as e:
            logger.error(f"Prediction failed for {match.get('name', '?')}: {e}")
    logger.info(f"Predicted {len(predictions)} matches")
    return predictions


def _save_output(output: dict, calibration_offset: dict | None, now_utc) -> Path:
    """保存预测结果到文件，返回文件路径。

    文件名必须带联赛后缀：时间戳只有小时精度（%Y-%m-%d_%H），而 --all 模式会在同
    一次运行里依次跑完全部联赛 —— 不带联赛名时 6 个联赛写同一个文件、互相覆盖，
    最终只剩最后一个联赛的数据（实测 --all 跑完 5 个联赛均成功，落盘文件却只有
    ligue1 的 6 场）。
    归并侧 store.latest_by_league() 读的是 JSON 里的 league 字段、不解析文件名，
    故加后缀不影响归并。
    """
    ts = now_utc.strftime("%Y-%m-%d_%H")
    league = output.get("league") if isinstance(output.get("league"), str) else ""
    suffix = f"_{league}" if league else ""
    pred_file = PREDICTIONS_DIR / f"prediction_{ts}{suffix}.json"
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    with open(pred_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved: {pred_file}")

    if calibration_offset:
        cal_file = PREDICTIONS_DIR / "pred_calibration.json"
        with open(cal_file, "w", encoding="utf-8") as f:
            json.dump(calibration_offset, f, indent=2)

    return pred_file


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


def _annotate_monte_carlo(predictions: list, run_monte_carlo: bool, n_simulations: int, fitted_rho: float, tournament_type: str) -> dict | None:
    """蒙特卡洛冠军概率并写回 predictions。返回冠军榜 dict（未启用/无数据时 None）。
    """
    # 6. Monte Carlo（可选）
    monte_carlo_result = None
    if run_monte_carlo and predictions:
        team_strengths = {}
        for p in predictions:
            home = p.get("home", "")
            away = p.get("away", "")
            if home:
                team_strengths[home] = {"lambda_home": p.get("lambda_home", 1.5), "lambda_away": p.get("lambda_away", 1.2)}
            if away:
                team_strengths[away] = {"lambda_home": p.get("lambda_home", 1.5), "lambda_away": p.get("lambda_away", 1.2)}
        fixtures = [{"home": p["home"], "away": p["away"]} for p in predictions if p.get("home") and p.get("away")]
        monte_carlo_result = monte_carlo_champion(
            fixtures, team_strengths, n_simulations=n_simulations,
            rho=fitted_rho, tournament_type=tournament_type,
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

    # 1. 获取并解析赛事数据
    events, past, future, in_prog = _fetch_and_parse(league_key, data_source, dates_str, now_utc, skip_fetch)

    # 1.5 累计历史完赛记录（供线上 ML 训练 / calibration 跨运行累计，P5）
    try:
        from core.calibration import append_historical_past_matches
        _added = append_historical_past_matches(league_key, past)
        if _added:
            logger.info(f"Accumulated {_added} historical past matches (league={league_key})")
    except Exception as e:
        logger.warning(f"Failed to accumulate historical past matches: {e}")

    # 2. ELO 评分初始化与更新
    fifa_rankings = fetch_fifa_rankings()
    logger.info(f"FIFA rankings loaded: {len(fifa_rankings)} teams")
    elo_ratings = _update_elo(past, fifa_rankings, force_refresh=args.update_rankings)
    return (data_source, run_monte_carlo, n_simulations, run_backtest, use_dc,
            _t_start, host_country, tournament_type, past, future, fifa_rankings, elo_ratings)


def _compute_and_predict(past, future, league_key, fifa_rankings, host_country, use_dc, elo_ratings):
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
            logger.info(f"DC rho fit failed: {e}, using default")

    # 5. 生成预测（AI 反馈分数按联赛隔离加载，P4）
    ai_adjustments = load_ai_adjustments(league_key)
    predictions = _generate_predictions(
        future, calibration_offset, fifa_rankings, host_country,
        use_dc, fitted_rho, elo_ratings, league_key, ai_adjustments,
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
    calibration, calibration_offset, fitted_rho, predictions = _compute_and_predict(past, future, league_key, fifa_rankings, host_country, use_dc, elo_ratings)

    # 6. Monte Carlo（可选）
    monte_carlo_result = _annotate_monte_carlo(predictions, run_monte_carlo, n_simulations, fitted_rho, tournament_type)

    # 7. 构建输出
    output, accuracy_summary = _build_league_output(now_utc, dates_str, league_key, tournament_type, data_source, use_dc, fitted_rho, calibration, calibration_offset, past, predictions, _t_start, monte_carlo_result, args)

    # 8. 回测（可选）
    if run_backtest:
        pred_file = _save_output(output, calibration_offset, now_utc)
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


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
    parser = build_parser()
    args = parser.parse_args()

    if _apply_ml_args(args):
        return

    # 使用北京时间（BJT）计算日期范围，而非UTC
    now_bjt = datetime.now(timezone(timedelta(hours=8)))

    if args.dates:
        dates_str = args.dates
    else:
        d1 = now_bjt.strftime("%Y%m%d")
        d2 = (now_bjt + timedelta(days=1)).strftime("%Y%m%d")
        dates_str = f"{d1}-{d2}"

    if args.all:
        all_outputs = []
        for league_key in LEAGUE_CONFIG:
            print(f"\n{'#'*60}", file=sys.stderr)
            print(f"# LEAGUE: {league_key}", file=sys.stderr)
            print(f"{'#'*60}", file=sys.stderr)
            try:
                result = run_league(league_key, args, now_bjt, dates_str, silent=True)
                if result:
                    all_outputs.append(result)
            except Exception as e:
                logger.error(f"Prediction failed for {league_key}: {e}")
        # Print combined JSON array for --all mode
        print(json.dumps(all_outputs, indent=2, ensure_ascii=False))
    else:
        run_league(args.league, args, now_bjt, dates_str)


if __name__ == "__main__":
    main()
