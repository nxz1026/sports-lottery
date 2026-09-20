"""web.routers.predictions — 只读数据 API（全部 require_auth）。

端点：
- GET /api/v1/predictions/today     BJT 比赛日、按联赛分组
- GET /api/v1/predictions/{date}    指定 BJT 日期（YYYY-MM-DD）
- GET /api/v1/championship          每联赛最新劳模模拟（monte_carlo）
- GET /api/v1/accuracy              每联赛最新命中率（accuracy_summary）
- GET /api/v1/history               每联赛历史预测文件清单

响应 key 冻结为前端契约（M4 消费）。只读、不触发引擎子进程。
"""
from __future__ import annotations

from datetime import date
import math
import time

from fastapi import APIRouter, Depends, Request

from web.auth import require_auth
from web.services import store
from web.services.datasource import LEAGUES

router = APIRouter(prefix="/api/v1", tags=["predictions"])


def _league_names() -> list[str]:
    return sorted(LEAGUES)


def _bball_extras(doc: dict) -> dict:
    return {
        "spread_pred": doc.get("spread_pred", ""),
        "total_pred": doc.get("total_pred", ""),
        "win_prob": doc.get("win_prob", ""),
        "kickoff_date": doc.get("kickoff_date", ""),
        "sport": "basketball",
    }


def _validated_model_probs(doc: dict) -> dict | None:
    """Return only an explicit, complete normalized 1X2 probability vector."""
    value = doc.get("model_probs", doc.get("ml_proba"))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        value = dict(zip(("home", "draw", "away"), value))
    if not isinstance(value, dict) or set(value) != {"home", "draw", "away"}:
        return None
    try:
        probs = {key: float(value[key]) for key in ("home", "draw", "away")}
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()):
        return None
    if abs(sum(probs.values()) - 1.0) > 1e-6:
        return None
    return probs


def _market_projection(doc: dict) -> dict:
    """Project only source-backed structured odds; never infer from confidence."""
    raw = doc.get("market")
    if not isinstance(raw, dict):
        return {"status": "missing"}
    source = raw.get("source")
    captured_at = raw.get("captured_at")
    market_type = raw.get("market_type")
    selections = raw.get("selections")
    if not source or not captured_at or not market_type or not isinstance(selections, dict):
        return {"status": "partial"}
    out = {"status": "partial", "source": source, "captured_at": captured_at,
           "market_type": market_type, "odds_format": raw.get("odds_format"),
           "selections": selections}
    if market_type != "1x2" or set(selections) != {"home", "draw", "away"}:
        return out
    decimals = {}
    for key, item in selections.items():
        if isinstance(item, dict):
            item = item.get("decimal_odds")
        try:
            decimals[key] = float(item)
        except (TypeError, ValueError):
            return out
    if any(not math.isfinite(v) or v <= 1 for v in decimals.values()):
        return out
    total = sum(1 / v for v in decimals.values())
    market_probs = {key: (1 / value) / total for key, value in decimals.items()}
    out.update({"status": "complete", "decimal_odds": decimals,
                "implied_probs": {k: 1 / v for k, v in decimals.items()},
                "devig_probs": market_probs, "margin": total - 1})
    model = _validated_model_probs(doc)
    if model is not None:
        out["model_probs"] = model
        out["edge_prob"] = {k: model[k] - market_probs[k] for k in model}
        out["ev_per_unit"] = {k: model[k] * decimals[k] - 1 for k in model}
    return out


def _prediction_summary(doc: dict, day) -> dict:
    """单场预测精简视图（契约 §2.2 字段对齐）。"""
    summary = {
        "match": doc.get("match", ""),
        "home": doc.get("home", ""),
        "away": doc.get("away", ""),
        "direction": doc.get("direction", ""),
        "stars": doc.get("stars", ""),
        "confidence_score": doc.get("confidence_score"),
        "predicted_score": doc.get("predicted_score", ""),
        "over_under": doc.get("over_under", ""),
        "btts": doc.get("btts", ""),
        "kickoff_utc": doc.get("kickoff_utc", ""),
        "data_window": doc.get("data_window", ""),
        "market": _market_projection(doc),
    }
    for key in ("odds_data_available", "confidence_note", "reasoning_factors",
                "ml_model_used", "ml_proba", "poisson_top3", "lambda_home",
                "lambda_away", "lambda_home_ci95", "lambda_away_ci95"):
        if key in doc:
            summary[key] = doc[key]
    if "spread_pred" in doc or "total_pred" in doc or "win_prob" in doc:
        summary.update(_bball_extras(doc))
    return summary


def _run_metadata(league: str, doc: dict) -> dict:
    """Safe, file-backed provenance metadata; never expose local paths."""
    data = doc.get("data", {})
    return {
        "league": league,
        "file": doc.get("name"),
        "generated_at": data.get("generated_at"),
        "data_window": data.get("data_window"),
        "status": data.get("status"),
        "data_source": data.get("data_source"),
        "tournament_type": data.get("tournament_type"),
        "dixon_coles_enabled": data.get("dixon_coles_enabled"),
        "dixon_coles_rho": data.get("dixon_coles_rho"),
        "n_predictions": len(data.get("predictions", [])) if isinstance(data.get("predictions"), list) else 0,
    }


def _group_for_day(day) -> dict:
    """按联赛分组某 BJT 日的预测；无数据联赛给空列表（前端契约）。"""
    out = {league: [] for league in _league_names()}
    for league, doc in store.latest_by_league().items():
        if league not in out:
            continue
        data = doc.get("data", {})
        if not store.covers_date(data, day):
            continue
        for p in data.get("predictions", []):
            out[league].append(_prediction_summary(p, day))
    return out


@router.get("/predictions/today")
def predictions_today(request: Request,
                      _: None = Depends(require_auth)) -> dict:
    """今日（BJT）各联赛预测，按联赛分组。"""
    day = store.bjt_today()
    return {"date": day.isoformat(), "leagues": _group_for_day(day)}


@router.get("/predictions/{date_str}")
def predictions_by_date(date_str: str, request: Request,
                        _: None = Depends(require_auth)) -> dict:
    """指定 BJT 日期（YYYY-MM-DD）各联赛预测，按联赛分组。"""
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        from web.errors import ApiError
        raise ApiError(http_status=400, code="invalid_date",
                       message=f"日期格式须为 YYYY-MM-DD: {date_str}")
    return {"date": day.isoformat(), "leagues": _group_for_day(day)}


@router.get("/championship")
def championship(request: Request,
                 _: None = Depends(require_auth)) -> dict:
    """每联赛最新蒙特卡洛夺冠概率（无数据 → 空 dict，200）。"""
    out: dict = {}
    for league, doc in store.latest_by_league().items():
        data = doc.get("data", {})
        mc = data.get("monte_carlo")
        if not isinstance(mc, dict) or not mc.get("champion_probs"):
            continue
        out[league] = {
            "generated_at": data.get("generated_at"),
            "data_window": data.get("data_window"),
            "champion_probs": mc.get("champion_probs", {}),
            "round_reach_probs": mc.get("round_reach_probs", {}),
            "simulation_count": mc.get("simulation_count"),
        }
    return {"leagues": out}


@router.get("/prediction-metadata")
def prediction_metadata(request: Request,
                        _: None = Depends(require_auth)) -> dict:
    """Latest run provenance per league (file-backed, read-only)."""
    return {"leagues": {
        league: _run_metadata(league, doc)
        for league, doc in store.latest_by_league().items()
    }}


@router.get("/calibration")
def calibration(request: Request,
                _: None = Depends(require_auth)) -> dict:
    """Persisted calibration state plus latest run context, when available."""
    states = store.calibration_states()
    latest = store.latest_by_league()
    leagues = {}
    for league in sorted(set(states) | set(latest)):
        row = {"state": states.get(league)}
        if league in latest:
            data = latest[league].get("data", {})
            row.update({"generated_at": data.get("generated_at"),
                        "data_window": data.get("data_window"),
                        "calibration": data.get("calibration"),
                        "calibration_offset": data.get("calibration_offset")})
        leagues[league] = row
    return {"leagues": leagues}


_ACC_CACHE: dict = {}
_ACC_TTL = 600  # 秒：实时对账带缓存，避免每次请求重扫预测文件


def _live_accuracy(league: str) -> dict | None:
    """存量 accuracy_summary 缺失时，实时对账已结算赛果得真实命中率。

    复用既有 core.backtest.league_accuracy（纯函数）；无已结算样本返回 None。
    带 10 分钟缓存；异常降级为 None（绝不编数）。
    """
    now = time.time()
    cached = _ACC_CACHE.get(league)
    if cached and now - cached[0] < _ACC_TTL:
        return cached[1]
    result: dict | None = None
    try:
        from core.backtest import league_accuracy
        windows: dict = {}
        for days in (7, 30):
            acc = league_accuracy(league, days=days)
            if acc:
                windows[f"{days}d"] = acc
        result = windows or None
    except Exception:
        result = None
    _ACC_CACHE[league] = (now, result)
    return result


@router.get("/accuracy")
def accuracy(request: Request,
             _: None = Depends(require_auth)) -> dict:
    """每联赛最新命中率（accuracy_summary 7d/30d；无数据 → 空 dict）。

    存量 ``accuracy_summary`` 缺失（如生成当刻尚无结算样本）时，回退为实时对账
    （core.backtest.league_accuracy，结算赛果→方向命中率），保证"如实展示真实数据"。
    ``pending``：每联赛「已预测但尚未完赛」的场次（稳定键去重）。
    """
    out: dict = {}
    for league, doc in store.latest_by_league().items():
        data = doc.get("data", {})
        acc = data.get("accuracy_summary")
        if not isinstance(acc, dict) or not acc:
            acc = _live_accuracy(league) or {}
        if not acc:
            continue
        out[league] = {
            "generated_at": data.get("generated_at"),
            "data_window": data.get("data_window"),
            **{k: v for k, v in acc.items() if isinstance(v, dict)},
        }
    return {"leagues": out, "pending": store.pending_predictions_by_league()}


@router.get("/accuracy/breakdown")
def accuracy_breakdown(request: Request,
                       _: None = Depends(require_auth)) -> dict:
    """Source-backed accuracy metrics, without deriving confidence intervals.

    This additive view deliberately projects only the metric fields currently
    emitted in ``accuracy_summary``.  It does not calculate estimates or add a
    sample count unless the producer explicitly supplied ``sample_count``.
    """
    metric_keys = (
        "direction_accuracy", "score_accuracy", "over_under_accuracy",
        "reconciled", "sample_count",
    )
    out: dict = {}
    for league, doc in store.latest_by_league().items():
        data = doc.get("data", {})
        summary = data.get("accuracy_summary")
        if not isinstance(summary, dict):
            continue
        windows: dict = {}
        for window, values in summary.items():
            if not isinstance(values, dict):
                continue
            projected = {key: values[key] for key in metric_keys if key in values}
            if projected:
                windows[window] = projected
        if windows:
            out[league] = {
                "generated_at": data.get("generated_at"),
                "data_window": data.get("data_window"),
                **windows,
            }
    return {"leagues": out}


@router.get("/history")
def history(request: Request,
            _: None = Depends(require_auth)) -> dict:
    """每联赛历史预测文件清单（按 generated_at 降序）。"""
    out: dict = {}
    for league, docs in store.history_by_league().items():
        out[league] = [
            {
                "name": doc["name"],
                "generated_at": doc.get("generated_at_iso"),
                "data_window": doc.get("data", {}).get("data_window"),
                "status": doc.get("data", {}).get("status"),
                "n_predictions": len(doc.get("data", {}).get("predictions", [])),
            }
            for doc in docs
        ]
    return {"leagues": out}