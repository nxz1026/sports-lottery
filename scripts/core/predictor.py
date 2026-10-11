from __future__ import annotations

"""Core prediction engine: Onside 4+1 signal model combined with Dixon-Coles."""

from core.config import ONSIDE_WEIGHTS, MARKET_ODDS_WEIGHT, DC_RHO, THRESHOLDS, LEAGUE_DC_RHO, ML_CONFIG
from core.log import logger
# 统一从 core.rankings 导入（P1-3：打破循环依赖）
from core.rankings import fetch_fifa_rankings
from core.model.onside import compute_onside_signals
from core.model.poisson import dixon_coles_match_probs, poisson_pmf, poisson_confidence_interval
from core.elo import expected_score, DEFAULT_ELO
from core.model.features import extract_features
from core.model.ml_model import load_league_model

# ── ML 模型缓存（按联赛加载一次）────────────────────
_ML_MODEL_CACHE: dict[str, object] = {}


def get_ml_model(league_key: str):
    """按联赛加载 ML 模型（带缓存）；不存在或未启用时返回 None。"""
    if not ML_CONFIG.get("enabled", False):
        return None
    if league_key in _ML_MODEL_CACHE:
        return _ML_MODEL_CACHE[league_key]
    model = load_league_model(league_key)
    _ML_MODEL_CACHE[league_key] = model  # 可能为 None，缓存以避免重复 IO
    return model


def _blend_ml_probs(home_prob: float, draw_prob: float, away_prob: float,
                    match: dict, onside: dict, sm: float,
                    elo_ratings: dict | None, host_country: str | None,
                    league_key: str) -> tuple[float, float, float, list | None]:
    """ML 概率融合（P1/ML）：26 维特征分类器与规则模型概率加权混合。纯函数。"""
    ml_proba = None
    ml_model = get_ml_model(league_key)
    if ml_model is not None:
        feature_match = dict(match)
        feature_match["onside_signals"] = onside
        feature_match["spread_movement_score"] = sm
        _ml_context = {"elo_ratings": elo_ratings, "host_country": host_country}
        try:
            vec = extract_features(feature_match, _ml_context)
            ml_proba = ml_model.predict_proba(vec)
            w = float(ML_CONFIG.get("blend_weight", 0.0))
            if w > 0 and len(ml_proba) == 3:
                home_prob = (1 - w) * home_prob + w * ml_proba[0]
                draw_prob = (1 - w) * draw_prob + w * ml_proba[1]
                away_prob = (1 - w) * away_prob + w * ml_proba[2]
                _t = home_prob + draw_prob + away_prob
                if _t > 0:
                    home_prob /= _t
                    draw_prob /= _t
                    away_prob /= _t
                logger.debug(f"ML blend applied (league={league_key}, w={w})")
        except Exception as e:
            logger.warning(f"ML blend failed, falling back to rule model: {e}")
            ml_proba = None
    return home_prob, draw_prob, away_prob, ml_proba


def _decide_direction_and_stars(home_prob: float, draw_prob: float,
                                away_prob: float, match: dict) -> tuple[str, float, str | None, str]:
    """方向判断 + 置信度钳位 + 无盘口降级 + 星级映射。纯函数。"""
    if home_prob > THRESHOLDS["direction_min_prob"] and home_prob > away_prob * THRESHOLDS["direction_odds_ratio"]:
        direction = f"{match['home']} 胜"
        confidence_raw = (home_prob - 0.25) * 2
    elif away_prob > THRESHOLDS["direction_min_prob"] and away_prob > home_prob * THRESHOLDS["direction_odds_ratio"]:
        direction = f"{match['away']} 胜"
        confidence_raw = (away_prob - 0.25) * 2
    elif draw_prob > THRESHOLDS["draw_threshold"]:
        direction = "平局"
        confidence_raw = (draw_prob - 0.25) * 2
    else:
        if home_prob >= away_prob and home_prob >= draw_prob:
            direction = f"{match['home']} 胜 (接近)"
            confidence_raw = (home_prob - THRESHOLDS["near_mode_base"]) * 3
        elif away_prob >= home_prob and away_prob >= draw_prob:
            direction = f"{match['away']} 胜 (接近)"
            confidence_raw = (away_prob - THRESHOLDS["near_mode_base"]) * 3
        else:
            direction = "平局 (接近)"
            confidence_raw = (draw_prob - THRESHOLDS["near_mode_base"]) * 3

    confidence_raw = min(max(confidence_raw, 0.0), 1.0)

    # ── 盘口数据缺失降级 ──
    if not match.get("odds_data_available", False):
        confidence_raw = max(confidence_raw - 0.25, 0.0)
        confidence_note = "无盘口数据，仅基本面参考"
    else:
        confidence_note = None

    if confidence_raw >= THRESHOLDS["star_5"]:
        stars = "5-star"
    elif confidence_raw >= THRESHOLDS["star_4"]:
        stars = "4-star"
    elif confidence_raw >= THRESHOLDS["star_3"]:
        stars = "3-star"
    elif confidence_raw >= THRESHOLDS["star_2"]:
        stars = "2-star"
    else:
        stars = "1-star"
    return direction, confidence_raw, confidence_note, stars


def _init_inputs(match: dict, weights: dict | None, fifa_rankings: dict | None,
                 host_country: str | None, elo_ratings: dict | None,
                 league_key: str, dc_rho: float | None) -> tuple:
    """输入解包 + Onside 4 信号计算 + 联赛差异化 ρ。纯函数。"""
    if weights is None:
        weights = ONSIDE_WEIGHTS

    # ── P0-3: 联赛差异化 ρ ──
    if dc_rho is None:
        dc_rho = LEAGUE_DC_RHO.get(league_key, DC_RHO)
        if dc_rho != DC_RHO:
            logger.debug(f"Using league-specific rho for {league_key}: {dc_rho}")

    hp = match.get("home_true_prob") or 0.5
    dp = match.get("draw_true_prob") or 0.25
    ap = match.get("away_true_prob") or 0.25
    hfs = match.get("home_form_score", 0.5)
    afs = match.get("away_form_score", 0.5)
    hrs = match.get("home_record_score", 0.5)
    ars = match.get("away_record_score", 0.5)
    sm = match.get("spread_movement_score", 0)

    # ── Onside 4 信号 ──
    home_en = match.get("home_en", match.get("home", ""))
    away_en = match.get("away_en", match.get("away", ""))

    if fifa_rankings is None:
        fifa_rankings = fetch_fifa_rankings()

    onside = compute_onside_signals(home_en, away_en, fifa_rankings, host_country, elo_ratings=elo_ratings)
    home_onside = onside["home"]["onside_score"]
    away_onside = onside["away"]["onside_score"]
    return weights, dc_rho, hp, dp, ap, hfs, afs, hrs, ars, sm, home_en, away_en, onside, home_onside, away_onside


def _apply_market_calibration(hp: float, dp: float, ap: float,
                              calibration_offset: dict | None,
                              home_onside: float, away_onside: float
                              ) -> tuple[float, float, float, float, float, float, float, float, str | None]:
    """calibration offset 修正 market 隐含概率并重新归一化。纯函数。"""
    calibration_note = None
    dc_val = 1.0
    ohc = 1.0
    oac = 1.0
    if calibration_offset:
        hc = calibration_offset.get("home_correction", 1.0)
        dc_val = calibration_offset.get("draw_correction", 1.0)
        ac = calibration_offset.get("away_correction", 1.0)

        # 修正 market odds：乘以 offset 后重新归一化
        hp_corrected = hp * hc
        dp_corrected = dp * dc_val
        ap_corrected = ap * ac
        total_corrected = hp_corrected + dp_corrected + ap_corrected
        if total_corrected > 0:
            hp = hp_corrected / total_corrected
            dp = dp_corrected / total_corrected
            ap = ap_corrected / total_corrected

        # 修正 Onside 4 信号：使用专用的 onside 修正因子（已减半），避免双重修正
        ohc = calibration_offset.get("onside_home_correction", 1.0)
        oac = calibration_offset.get("onside_away_correction", 1.0)
        onside_h = home_onside * ohc
        onside_a = away_onside * oac
        total_on = onside_h + onside_a
        if total_on > 0:
            home_onside = onside_h / total_on
            away_onside = onside_a / total_on

        calibration_note = f"calibration applied (n={calibration_offset.get('sample_size','?')}, " \
                           f"home×{hc}/draw×{dc_val}/away×{ac})"
        logger.info(calibration_note)

    return hp, dp, ap, dc_val, ohc, oac, home_onside, away_onside, calibration_note


def _unified_strength_probs(market_home: float, market_draw: float, market_away: float,
                            home_onside: float, away_onside: float, sm_capped: float,
                            onside_weight: float, elo_ratings: bool,
                            elo_home_expected: float | None, elo_away_expected: float | None,
                            ELO_WEIGHT: float, dc_val: float, calibration_offset: dict | None
                            ) -> tuple[float, float, float]:
    """P0-2：方向概率与 λ 使用同一套加权信号 → home/draw/away 概率。纯函数。"""

    # ── 方向概率计算 ──
    home_strength = (
        market_home * MARKET_ODDS_WEIGHT
        + home_onside * onside_weight
        + sm_capped * 0.5
    )
    away_strength = (
        market_away * MARKET_ODDS_WEIGHT
        + away_onside * onside_weight
        + (-sm_capped) * 0.5
    )

    # ELO 加成
    if elo_ratings and elo_home_expected is not None:
        home_strength += elo_home_expected * ELO_WEIGHT
        away_strength += elo_away_expected * ELO_WEIGHT

    # P0-C 修复: Calibration 的 draw_correction 现在应用到 draw_strength
    _dc_draw = dc_val if calibration_offset else 1.0
    draw_strength = max(0, (market_draw * _dc_draw) * MARKET_ODDS_WEIGHT + THRESHOLDS["draw_base_score"])

    total = max(home_strength + draw_strength + away_strength, 0.05)
    home_prob = home_strength / total
    draw_prob_calc = draw_strength / total
    away_prob = away_strength / total

    return home_prob, draw_prob_calc, away_prob


def _weighted_direction_probs(hp: float, dp: float, ap: float,
                              home_onside: float, away_onside: float,
                              sm: float, elo_ratings: dict | None,
                              elo_home_expected: float | None, elo_away_expected: float | None,
                              ELO_WEIGHT: float, dc_val: float,
                              calibration_offset: dict | None
                              ) -> tuple[float, float, float, float, float]:
    """P0-2 统一权重体系：sm 截断 + 方向概率（与 λ 同一套加权信号）。纯函数。"""
    # ══════════════════════════════════════════════════════
    # P0-2 统一权重体系：方向概率和 λ 使用同一套加权信号
    # ══════════════════════════════════════════════════════
    sm_capped = max(-THRESHOLDS["spread_movement_cap"], min(THRESHOLDS["spread_movement_cap"], sm))
    onside_weight = (1 - MARKET_ODDS_WEIGHT) * (1 - ELO_WEIGHT if elo_ratings else 1.0)
    home_prob, draw_prob_calc, away_prob = _unified_strength_probs(
        hp, dp, ap, home_onside, away_onside, sm_capped, onside_weight,
        elo_ratings, elo_home_expected, elo_away_expected,
        ELO_WEIGHT, dc_val, calibration_offset)
    return sm_capped, onside_weight, home_prob, draw_prob_calc, away_prob


def _derive_lambdas_and_grid(
    league_key: str,
    hp: float, ap: float,
    home_onside: float, away_onside: float,
    sm_capped: float, onside_weight: float,
    elo_ratings: dict | None,
    elo_home_expected: float | None, elo_away_expected: float | None,
    ELO_WEIGHT: float,
    hfs: float, hrs: float, afs: float, ars: float,
    use_dixon_coles: bool, dc_rho: float,
) -> tuple[float, float, float, float, str, list, float, float]:
    """P0-2：统一权重推导 DC 强度 λ，展开 9×9 泊松网格 → 比分/top3/BTTS/O2.5。纯函数。"""
    _lambda_mult = THRESHOLDS["lambda_multiplier"]
    # P1-C: 联赛差异化 λ 映射系数（覆盖全局默认值）
    from core.config import LEAGUE_LAMBDA_MULTIPLIER
    if league_key in LEAGUE_LAMBDA_MULTIPLIER:
        _lambda_mult = LEAGUE_LAMBDA_MULTIPLIER[league_key]

    # 统一的 raw strength（与方向概率同一套信号）
    raw_home_unified = (
        hp * MARKET_ODDS_WEIGHT
        + home_onside * onside_weight
        + sm_capped * 0.5
    )
    raw_away_unified = (
        ap * MARKET_ODDS_WEIGHT
        + away_onside * onside_weight
        + (-sm_capped) * 0.5
    )

    if elo_ratings and elo_home_expected is not None:
        raw_home_unified += elo_home_expected * ELO_WEIGHT
        raw_away_unified += elo_away_expected * ELO_WEIGHT

    # 加入 form/record 作为补充微调（保留但降权）
    raw_home = raw_home_unified * 0.75 + (hfs * 0.15 + hrs * 0.10)
    raw_away = raw_away_unified * 0.75 + (afs * 0.15 + ars * 0.10)

    lambda_home = max(raw_home * _lambda_mult, THRESHOLDS["lambda_lower_bound"])
    lambda_away = max(raw_away * _lambda_mult, THRESHOLDS["lambda_lower_bound"])

    return lambda_home, lambda_away, raw_home, raw_away, *_score_grid(
        lambda_home, lambda_away, use_dixon_coles, dc_rho)


def _score_grid(
    lambda_home: float,
    lambda_away: float,
    use_dixon_coles: bool,
    dc_rho: float,
) -> tuple[str, list, float, float, list]:
    """9×9 泊松比分网格 → 最可能比分/top3/BTTS/O2.5。纯函数。"""
    # ── Dixon-Coles 或独立泊松比分预测 ──
    if use_dixon_coles:
        dc_result = dixon_coles_match_probs(lambda_home, lambda_away, rho=dc_rho)
        all_scores = dc_result["score_probs"]
        top3 = [(s[0], s[1], round(s[2], 4)) for s in all_scores[:3]]
        predicted_score = f"{top3[0][0]}-{top3[0][1]}"

        # 从 DC 模型计算 BTTS 和 Over/2.5
        btts_prob = sum(s[2] for s in all_scores if s[0] > 0 and s[1] > 0)
        over_25_prob = sum(s[2] for s in all_scores if s[0] + s[1] > 2)
    else:
        all_scores = []
        for h in range(9):
            for a in range(9):
                p = poisson_pmf(h, lambda_home) * poisson_pmf(a, lambda_away)
                if p >= 0.001:
                    all_scores.append((h, a, p))
        all_scores.sort(key=lambda x: -x[2])
        top3 = [(s[0], s[1], round(s[2], 4)) for s in all_scores[:3]]
        predicted_score = f"{top3[0][0]}-{top3[0][1]}"
        btts_prob = sum(s[2] for s in all_scores if s[0] > 0 and s[1] > 0)
        over_25_prob = sum(s[2] for s in all_scores if s[0] + s[1] > 2)

    return predicted_score, top3, btts_prob, over_25_prob, all_scores


def _resolve_direction_winner(dir_str: str, match_ctx: dict) -> str | None:
    """解析方向字符串 → 'home' | 'draw' | 'away' | None"""
    if "胜" not in dir_str:
        return None
    home_name = match_ctx.get("home", "")
    away_name = match_ctx.get("away", "")
    if dir_str.startswith(home_name):
        return "home"
    elif dir_str.startswith(away_name):
        return "away"
    # 前缀均不匹配（如队名互为前缀等边界情况），记录以便排查
    logger.warning(
        f"_resolve_direction_winner: 无法解析方向 '{dir_str}' "
        f"(home='{home_name}', away='{away_name}')"
    )
    return None


def _reconcile_score_with_direction(predicted_score: str, direction: str,
                                    all_scores: list, match: dict) -> str:
    """P1-4：预测方向与最可能比分矛盾时，从网格里取方向一致的最高概率比分。纯函数。"""
    # ── 方向一致性检查（P1-4：结构化枚举 + 主客胜全覆盖） ──
    if "胜" in direction:
        winner = _resolve_direction_winner(direction, match)
        predicted_h = int(predicted_score.split("-")[0])
        predicted_a = int(predicted_score.split("-")[1])
        if predicted_h == predicted_a and winner:
            for h, a, p in all_scores:
                if (winner == "home" and h > a) or (winner == "away" and a > h):
                    predicted_score = f"{h}-{a}"
                    break

    return predicted_score


def _elo_expectations(elo_ratings: dict | None, home_en: str, away_en: str) -> tuple[float | None, float | None, float]:
    """ELO 期望胜率信号（可选，无 elo_ratings 时返回 None）。纯函数。"""
    # ── ELO 信号（可选） ──
    elo_home_expected = None
    elo_away_expected = None
    if elo_ratings:
        home_elo = elo_ratings.get(home_en, DEFAULT_ELO)
        away_elo = elo_ratings.get(away_en, DEFAULT_ELO)
        elo_home_expected = expected_score(home_elo, away_elo, home_adv=True)
        elo_away_expected = 1.0 - elo_home_expected
    ELO_WEIGHT = THRESHOLDS.get("elo_weight", 0.18)
    return elo_home_expected, elo_away_expected, ELO_WEIGHT


# ESPN 赔率富化字段：predict.py 顺序保证 odds_enrich 已对 match 写完这些字段后再调用本函数。
# 必须从 match 透传到 result（写入 prediction_*.json），否则 web 层 _market_projection 永远拿不到
# ``market``，今日推荐 / 串关工作台全部展示「赔率/价值不可用」。仅在值非空时透传，避免污染 None。
_ODDS_PROPAGATION_FIELDS = (
    "market", "home_ml_implied", "draw_implied", "away_ml_implied",
    "home_true_prob", "draw_true_prob", "away_true_prob",
    "spread_home_line", "spread_home_close_odds",
    "total_over_close", "total_under_close", "draw_ml",
)


def _assemble_result(
    match: dict, direction: str, stars: str, confidence_raw: float,
    predicted_score: str, top3: list[tuple[int, int, float]],
    lambda_home: float, lambda_away: float, raw_home: float, raw_away: float,
    btts_prob: float, over_25_prob: float, ml_proba: list[float] | None,
    home_prob: float, draw_prob_calc: float, away_prob: float,
    hfs: float, hrs: float, afs: float, ars: float, sm: float,
    home_onside: float, away_onside: float, elo_home_expected: float | None,
    elo_ratings: dict[str, float] | None, use_dixon_coles: bool,
    dc_rho: float | None, league_key: str, onside: dict,
    confidence_note: str | None, hp: float, dp: float, ap: float,
) -> dict:
    """组装最终预测结果字典（含 95% CI 与 Over/Under）。纯函数。

    ESPN 赔率字段（``market`` / ``home_ml_implied`` 等）由 ``odds_enrich._apply_enrichment``
    就地写入 ``match``，本函数负责透传到 ``result`` 让 prediction_*.json 落盘带上盘口；
    见 ``_ODDS_PROPAGATION_FIELDS``。空值（None / "" / 空 dict）一律不写，避免污染下游默认降级。
    """
    # 95% 置信区间
    ci_home = poisson_confidence_interval(lambda_home)
    ci_away = poisson_confidence_interval(lambda_away)

    ou_total = match.get("total_over_close", "2.5")
    ou_total = ou_total.lstrip("ou")
    if over_25_prob > 0.5:
        ou = f"Over {ou_total}"
    else:
        ou = f"Under {ou_total}"

    result = {
        "direction": direction, "stars": stars,
        "confidence_score": round(confidence_raw, 3),
        "predicted_score": predicted_score,
        "poisson_top3": [{"score": f"{h}-{a}", "prob": round(p, 4)} for h, a, p in top3],
        "lambda_home": round(lambda_home, 2), "lambda_away": round(lambda_away, 2),
        "lambda_home_ci95": ci_home, "lambda_away_ci95": ci_away,
        "over_under": f"{ou}", "btts": "Yes" if btts_prob > 0.5 else "No",
        "dixon_coles_used": use_dixon_coles,
        "dixon_coles_rho": dc_rho if use_dixon_coles else None,
        "ml_model_used": ml_proba is not None,
        "ml_proba": [round(p, 4) for p in ml_proba] if ml_proba else None,
        "dixon_coles_league_rho": LEAGUE_DC_RHO.get(league_key, DC_RHO),  # P0-3: 报告使用的 ρ 来源
        "onside_signals": onside, "confidence_note": confidence_note,
        "odds_data_available": match.get("odds_data_available", False),
    }
    # ESPN 赔率字段透传（仅非空，避免污染 None）。见 _ODDS_PROPAGATION_FIELDS 注释。
    for _k in _ODDS_PROPAGATION_FIELDS:
        _v = match.get(_k)
        if _v is None:
            continue
        if isinstance(_v, (str, list, dict)) and len(_v) == 0:
            continue
        result[_k] = _v
    result.update(_debug_fields(hfs, hrs, afs, ars, sm, home_onside,
        away_onside, home_prob, draw_prob_calc, away_prob,
        elo_home_expected, elo_ratings, raw_home, raw_away,
        hp, dp, ap))
    return result


def _debug_fields(
    hfs: float, hrs: float, afs: float, ars: float, sm: float,
    home_onside: float, away_onside: float, home_prob: float,
    draw_prob_calc: float, away_prob: float, elo_home_expected: float | None,
    elo_ratings: dict[str, float] | None, raw_home: float, raw_away: float,
    hp: float, dp: float, ap: float,
) -> dict:
    """构造调试/审计字段（暴露输入便于调试）。纯函数。"""
    return {
        "reasoning_factors": {
            "home_ml_true_prob": round(hp, 3),
            "draw_true_prob": round(dp, 3),
            "away_ml_true_prob": round(ap, 3),
            "home_form_score": round(hfs, 3),
            "away_form_score": round(afs, 3),
            "home_record_score": round(hrs, 3),
            "away_record_score": round(ars, 3),
            "spread_movement": round(sm, 3),
            "home_onside_score": round(home_onside, 3),
            "away_onside_score": round(away_onside, 3),
            "home_prob_weighted": round(home_prob, 3),
            "draw_prob_weighted": round(draw_prob_calc, 3),
            "away_prob_weighted": round(away_prob, 3),
            "elo_home_expected": round(elo_home_expected, 3) if elo_ratings and elo_home_expected is not None else None,
            "raw_lambda_home": round(raw_home, 4),   # P0-2: 暴露原始 λ 输入便于调试
            "raw_lambda_away": round(raw_away, 4),   # P0-2: 暴露原始 λ 输入便于调试
        },
    }


# Args:
#     match: 比赛数据字典
#     weights: 权重字典（可选，默认 ONSIDE_WEIGHTS）
#     calibration_offset: 校准偏移字典
#     fifa_rankings: FIFA 排名字典
#     host_country: 东道主国家
#     use_dixon_coles: 是否使用 Dixon-Coles 模型
#     dc_rho: Dixon-Coles ρ 参数，None 时按联赛自动选择 (P0-3)
#     elo_ratings: ELO 评分表 {team: elo}，提供则加入信号融合
#     league_key: 联赛键名，用于查找联赛特定参数 (P0-3)
def calculate_prediction(
    match: dict,
    weights: dict | None = None,
    calibration_offset: dict | None = None,
    fifa_rankings: dict | None = None,
    host_country: str | None = None,
    use_dixon_coles: bool = True,
    dc_rho: float | None = None,  # P0-3: 允许 None 以使用联赛差异化 ρ
    elo_ratings: dict[str, float] | None = None,
    league_key: str = "epl",  # P0-3: 传入联赛 key 用于差异化 ρ
) -> dict:
    """Onside 4 信号 + ELO + Dixon-Coles 预测 → 方向 + 信心 + 比分预测 + 95% CI"""
    weights, dc_rho, hp, dp, ap, hfs, afs, hrs, ars, sm, home_en, away_en, onside, home_onside, away_onside = _init_inputs(
        match, weights, fifa_rankings, host_country, elo_ratings, league_key, dc_rho)

    # ── 应用 calibration offset 修正隐含概率 ──
    hp, dp, ap, dc_val, ohc, oac, home_onside, away_onside, calibration_note = _apply_market_calibration(
        hp, dp, ap, calibration_offset, home_onside, away_onside)

    elo_home_expected, elo_away_expected, ELO_WEIGHT = _elo_expectations(
        elo_ratings, home_en, away_en)

    sm_capped, onside_weight, home_prob, draw_prob_calc, away_prob = _weighted_direction_probs(
        hp, dp, ap, home_onside, away_onside, sm, elo_ratings,
        elo_home_expected, elo_away_expected, ELO_WEIGHT, dc_val, calibration_offset)

    # ── ML 概率融合（P1/ML: 26 维特征分类器与主模型概率做加权融合）──
    home_prob, draw_prob_calc, away_prob, ml_proba = _blend_ml_probs(
        home_prob, draw_prob_calc, away_prob, match, onside, sm,
        elo_ratings, host_country, league_key)

    # ── 方向判断 ──
    direction, confidence_raw, confidence_note, stars = _decide_direction_and_stars(
        home_prob, draw_prob_calc, away_prob, match)

    lambda_home, lambda_away, raw_home, raw_away, predicted_score, top3, btts_prob, over_25_prob, all_scores = _derive_lambdas_and_grid(
        league_key, hp, ap, home_onside, away_onside, sm_capped, onside_weight,
        elo_ratings, elo_home_expected, elo_away_expected, ELO_WEIGHT,
        hfs, hrs, afs, ars, use_dixon_coles, dc_rho)

    predicted_score = _reconcile_score_with_direction(
        predicted_score, direction, all_scores, match)

    return _assemble_result(
        match, direction, stars, confidence_raw, predicted_score, top3,
        lambda_home, lambda_away, raw_home, raw_away, btts_prob, over_25_prob,
        ml_proba, home_prob, draw_prob_calc, away_prob, hfs, hrs, afs, ars, sm,
        home_onside, away_onside, elo_home_expected, elo_ratings,
        use_dixon_coles, dc_rho, league_key, onside, confidence_note, hp, dp, ap)
