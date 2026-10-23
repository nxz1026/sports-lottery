"""web.routers.predictions — 只读数据 API（全部 require_auth）。

端点：
- GET /api/v1/predictions/today     BJT 比赛日、按联赛分组
- GET /api/v1/predictions/{date}    指定 BJT 日期（YYYY-MM-DD）
- GET /api/v1/championship          每联赛最新劳模模拟（monte_carlo）
- GET /api/v1/accuracy              每联赛最新命中率（accuracy_summary）
- GET /api/v1/history               每联赛历史预测文件清单

响应 key 冻结为前端契约（M4 消费）。只读、不触发引擎子进程。

⚠️ **P2（2026-10-23）**：``/accuracy`` 与 ``/accuracy/breakdown`` **新增** ``overall`` 键
（全部联赛的聚合命中率：范围 / 窗口 / 样本量全部显式，见 ``_aggregate_accuracy``）。
既有键一个没删、没改语义。
"""
from __future__ import annotations

import logging
import math
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from web.auth import require_auth
from web.services import store
from web.services.datasource import LEAGUES

# scripts/（core 纯函数，如 core.model_probs —— 「模型概率唯一真源」）供本模块复用：
# 追加到 sys.path **末尾**（不前置），避免 scripts/ 下同名包遮蔽 web/、store/
# （与 web/api.py 的做法一致）。自举 ⇒ 本模块与导入顺序无关（R47 同款加固）。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

logger = logging.getLogger("web.predictions")
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
    """返回**唯一真源**的 1X2 模型概率向量，或 None（含义是「不知道」，不是 0）。

    P0（2026-10-23 实测事故）：旧实现读 ``doc["ml_proba"]``，而 ``ml_proba`` 存的是
    **ML 分类器原始输出**（未经与规则模型融合），在强弱悬殊的场次会塌成
    ``[0.978, 0.008, 0.014]``。被当成「模型概率」后造成三处硬伤：

    ① 卡片概率条与卡片**自己推荐的 direction** 自相矛盾（direction 来自 ``probs``）；
    ② ``edge_prob`` / ``ev_per_unit`` 全是**假账** —— 卡片说主胜「每投 1 元长期多赚
       0.33 元」，而决策层用**同一个模型**算出主胜 EV = **−0.159**（要亏）；
    ③ 卡片让买主胜、推荐单让买客胜 —— 同一页面上两个面板互相打脸。

    ⇒ 改走 ``core.model_probs``（模型概率唯一真源，口径与事故复盘见该模块 docstring）：
    ``model_probs`` → ``probs.{h,d,a}`` → ``reasoning_factors.*_prob_weighted``。
    **不用** ``ml_proba``（分类器原始输出），**不用** ``*_true_prob``（ESPN 市场先验）。

    容差从 1e-6 放宽到 1e-3：融合后概率写入预测文件时按 4 位小数四舍五入，
    三项求和的舍入误差上界 ≈ 1.5e-4 ⇒ 旧容差把 **99/162（61%）** 的真实场次
    误判为「非法向量」，``edge_prob`` / ``ev_per_unit`` 整体缺失 ⇒ 卡片静默不显示
    「比庄家高/低 X 个点」，用户看不出缺了什么。
    """
    from core.model_probs import resolve_model_probs

    return resolve_model_probs(doc)


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
                "lambda_away", "lambda_home_ci95", "lambda_away_ci95",
                # P0（2026-10-23）：把**模型概率真源**（probs，融合后最终值）也透出，
                # 便于前端/排障直接看到「模型到底认为多大赢面」，不必反推 edge_prob。
                "probs"):
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
_ACC_TTL_OK = 600  # 秒：成功结果缓存 10 分钟，避免每次请求重扫预测文件
_ACC_TTL_FAIL = 30  # 秒：失败结果仅短缓存 30 秒，store 恢复后尽快追上

# 命中率窗口：**最近 N 个自然日**（不是「最近 N 场」）。
# 口径纪律（P2，2026-10-23）：窗口名必须能被调用方读到 ⇒ 聚合结果里带 window / window_days。
_ACC_WINDOWS: tuple[tuple[str, int], ...] = (("7d", 7), ("30d", 30))
_ACC_METRICS = ("direction_accuracy", "score_accuracy", "over_under_accuracy")


def _acc_sample_n(window: dict[str, Any]) -> int:
    """窗口的样本量（已结算场次）：``reconciled`` 优先，其次 ``sample_count``。"""
    for key in ("reconciled", "sample_count"):
        v = window.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        return int(v)
    return 0


def _aggregate_accuracy(leagues: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """**全部联赛**的聚合命中率（唯一头条口径）。

    ⚠️ **P2（2026-10-23）**：页面 KPI「近 7 日命中率」原先是
    「取**第一个联赛**的 7d 值，7d 缺失时**回落 30d**」⇒ 联赛范围（一个 vs 全部）、
    窗口（7 日 vs 7 场）、数值来源（7d vs 30d）三方全不一致，页面在自说自话。
    ⇒ 现在后端给出**唯一**口径，调用方直接读它：
      · ``scope``  = ``all_leagues``（全部联赛，不是某一个联赛）
      · ``window`` = **实际使用的窗口**（"7d" / "30d"）
      · ``n``      = 参与聚合的已结算样本数（场次）
      · 命中率      = 按各联赛样本数 n **加权平均**（不是简单平均）
    7d 全无样本时改用 30d，但**必须**显式标出：``fallback=True`` + ``window="30d"``
    （不许静默回落）。
    """
    per_window: dict[str, dict[str, Any]] = {}
    for wname, wdays in _ACC_WINDOWS:
        n_total = 0
        names: list[str] = []
        acc_sum = {m: 0.0 for m in _ACC_METRICS}
        acc_cnt = {m: 0 for m in _ACC_METRICS}
        for league, payload in (leagues or {}).items():
            win = payload.get(wname) if isinstance(payload, dict) else None
            if not isinstance(win, dict):
                continue
            n = _acc_sample_n(win)
            if n <= 0:
                continue
            names.append(league)
            n_total += n
            for metric in _ACC_METRICS:
                v = win.get(metric)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    continue
                acc_sum[metric] += float(v) * n
                acc_cnt[metric] += 1
        per_window[wname] = {
            "window": wname,
            "window_days": wdays,
            "n": n_total,
            "n_leagues": len(names),
            "leagues": sorted(names),
            **{m: (round(acc_sum[m] / n_total, 4) if acc_cnt[m] and n_total else None)
               for m in _ACC_METRICS},
        }

    headline = None
    for wname, _wdays in _ACC_WINDOWS:
        if per_window[wname]["n"] > 0:
            headline = per_window[wname]
            break
    fallback = bool(headline) and headline["window"] != _ACC_WINDOWS[0][0]
    out: dict[str, Any] = {
        "scope": "all_leagues",
        "window": headline["window"] if headline else None,
        "window_days": headline["window_days"] if headline else None,
        "fallback": fallback,
        "n": headline["n"] if headline else 0,
        "n_leagues": headline["n_leagues"] if headline else 0,
        "leagues": headline["leagues"] if headline else [],
        "windows": per_window,
        "note": ("口径：全部联赛按已结算样本数 n 加权平均；窗口 = 最近 N 个自然日"
                 "（「不是」最近 N 场）。7d 无样本时改用 30d 并标 fallback=true（不静默回落）。"),
    }
    for metric in _ACC_METRICS:
        out[metric] = headline[metric] if headline else None
    return out


def _live_accuracy(league: str) -> dict | None:
    """存量 accuracy_summary 缺失时，实时对账已结算赛果得真实命中率。

    复用 ``web.services.accuracy.league_accuracy_web``（契约 §5 不允许反向
    import scripts/，故 web 端走独立实现）；无已结算样本返回 None。
    - 成功结果缓存 10 分钟（避免每次请求重扫预测文件）
    - 失败 / 空结果仅短缓存 30 秒（store 恢复后尽快追上，规避负缓存污染）
    - 异常降级为 None（绝不编数）
    """
    now = time.time()
    cached = _ACC_CACHE.get(league)
    if cached:
        ttl = _ACC_TTL_OK if cached[1] is not None else _ACC_TTL_FAIL
        if now - cached[0] < ttl:
            return cached[1]
    result: dict | None = None
    try:
        from web.services.accuracy import league_accuracy_web
        windows: dict = {}
        for days in (7, 30):
            acc = league_accuracy_web(league, days=days)
            if acc:
                windows[f"{days}d"] = acc
        result = windows or None
    except Exception as exc:  # noqa: BLE001 - 命中率计算容错
        logger.warning("predictions._live_accuracy(%s) failed: %s", league, exc)
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

    ⚠️ **P2（2026-10-23）新增键 ``overall``**：全部联赛的聚合命中率（唯一头条口径：
    范围=全部联赛、窗口=``window``、样本量=``n``；7d 无样本改用 30d 时 ``fallback=true``）。
    页面 KPI「近 7 日命中率」应读它，**不要**再取「第一个联赛的值 + 缺失就回落 30d」。
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
    return {"leagues": out, "pending": store.pending_predictions_by_league(),
            "overall": _aggregate_accuracy(out)}


@router.get("/accuracy/breakdown")
def accuracy_breakdown(request: Request,
                       _: None = Depends(require_auth)) -> dict:
    """Source-backed accuracy metrics, without deriving confidence intervals.

    This additive view deliberately projects only the metric fields currently
    emitted in ``accuracy_summary``.  It does not calculate estimates or add a
    sample count unless the producer explicitly supplied ``sample_count``.

    ⚠️ **P2（2026-10-23）新增键 ``overall``**：与 ``/accuracy`` 同一份聚合口径
    （范围/窗口/样本量全部显式，见 :func:`_aggregate_accuracy`）。
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
    return {"leagues": out, "overall": _aggregate_accuracy(out)}


@router.get("/accuracy/stratified")
def accuracy_stratified(request: Request,
                        _: None = Depends(require_auth)) -> dict:
    """分层命中率 + 样本量 + 置信区间（建议 3 子项 ①）。

    为什么单独开一个端点而不是改 ``/accuracy``：**老字段是前端契约**，
    改它会让现有页面与第三方消费方一起漂移 ⇒ 新端点为**增量**视图。

    每层（按 stars / 按 confidence 分档）都给 ``n`` / ``hits`` / ``rate`` / ``ci95``；
    ``n < 5`` 的层带 ``insufficient: true``。窗口内无已结算样本 → 该联赛不出现在结果里
    （**不编数**）。
    """
    from web.services.accuracy import league_accuracy_stratified

    out: dict = {}
    for league in _league_names():
        try:
            strat = league_accuracy_stratified(league, days=30)
        except Exception as exc:  # noqa: BLE001 - 单联赛失败不影响其它联赛
            logger.warning("accuracy_stratified(%s) failed: %s", league, exc)
            continue
        if strat:
            out[league] = strat
    return {"leagues": out, "method": "wilson-95", "min_n": 5}


@router.get("/clv")
def clv_daily(request: Request,
              _: None = Depends(require_auth)) -> dict:
    """CLV 日汇总（建议 3 子项 ②，主指标）。

    ``mean_clv`` 是 ``ln(p_model / p_close)`` 的均值：>0 = 模型比收盘市场更看好该选项。
    **口径纪律**：``n_matches`` 不足时行上带 ``insufficient: true``，
    页面据此**不显示均值**（宁可空白不误导）—— 与命中率分层同一套纪律。
    """
    from web.services import clv as clv_service

    st = clv_service.daily_rows_status(days=60)
    rows = st["rows"]
    return {
        "rows": rows,
        "summary": clv_service.summary(rows, degraded=st["degraded"]),
        "degraded": st["degraded"],
        "sql_note": ("CLV = ln(p_model / p_close)；p_close = 竞彩官方 had 赔率在开球前最后一版"
                     "（fact.jc_odds_history 的 max(seq_no)）的比例去水；"
                     "p_model = 模型自己的 probs.h/d/a（「不是」顶层 *_true_prob —— "
                     "那是市场先验，拿它算 CLV 是循环论证）"),
    }


@router.get("/recommend")
def recommend(request: Request,
              _: None = Depends(require_auth)) -> dict:
    """今日推荐单 + 战绩（建议 4 子项 ①②③，主指标）。

    「今日」按 **BJT** 判定（`made_at AT TIME ZONE 'Asia/Shanghai'`）——
    直接用 `made_at::date` 会按会话时区算、差一天（见路线图 §7 第 12 条的时区坑）。

    口径纪律：战绩 n 不足（< 5）时 `roi` 为 null，页面**不显示 ROI**（宁可空白不误导）。
    """
    from web.services import recommend as recommend_service

    return recommend_service.snapshot()


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
