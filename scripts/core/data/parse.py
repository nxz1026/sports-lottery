from __future__ import annotations

"""Event parsing and odds/statistics extraction."""

from datetime import datetime, timezone
from core.log import logger
# 兼容 re-export：tests/test_parse.py 仍 `from core.data.parse import to_cn`
from core.i18n import to_cn


def parse_american_odds(odds_str: str | None) -> float | None:
    """解析美式赔率 → 隐含概率 (含 vig)"""
    try:
        raw = str(odds_str).strip().lstrip('+')
        odds = int(raw)
        abs_odds = abs(odds)
        if odds < 0:
            return abs_odds / (abs_odds + 100)
        else:
            return 100 / (abs_odds + 100)
    except (ValueError, TypeError):
        return None


def parse_details(details_str: str | None) -> tuple[str | None, str | None, float | None]:
    """解析 details 字段如 'CZE -125' → (team, odds_str, implied)"""
    if not details_str:
        return None, None, None
    parts = details_str.strip().split()
    if len(parts) >= 2:
        team = parts[0]
        odds_str = parts[-1]
        impl = parse_american_odds(odds_str)
        return team, odds_str, impl
    return None, None, None


def form_to_score(form_str: str | None) -> float:
    """'DWDDW' → 0-1, W=3, D=1, L=0，带指数时间衰减（最近比赛权重更高）

    P1-A 修复: 衰减系数从线性 0.3 升级为指数 2^i，
    最新一场权重是最老的 16 倍，更符合足球状态敏感性。
    """
    if not form_str:
        return 0.5
    total_weight = 0
    weighted_sum = 0
    # 指数时间衰减：从左到右（最老→最新），最新权重最高
    for i, c in enumerate(form_str):
        w = 2.0 ** i  # 最后一场权重 = 2^4 = 16x（5场时）
        score = 3 if c == 'W' else 1 if c == 'D' else 0
        weighted_sum += score * w
        total_weight += w * 3
    return weighted_sum / total_weight if total_weight > 0 else 0.5


def record_to_score(records: list) -> float:
    """records[0].summary '1-0-0' (W-D-L) → 0-1"""
    if not records:
        return 0.5
    summary = records[0].get("summary", "")
    parts = summary.split("-")
    if len(parts) >= 3:
        w, d, l = int(parts[0]), int(parts[1]), int(parts[2])
        total = w + d + l
        return (w * 3 + d) / (total * 3) if total > 0 else 0.5
    return 0.5


def spread_movement_factor(away_open: dict | None, away_close: dict | None,
                          home_open: dict | None = None, home_close: dict | None = None) -> float:
    """综合主客场盘口 line + odds 变化判断 market 方向。

    P1-B 升级: 原版仅用 away spread line 差值，
    现在加入 home spread + 双方 odds 变化作为辅助信号。

    正值 = 盘口向主队移动（利好主队）
    负值 = 盘口向客队移动（利好客队）
    """
    signal = 0.0

    # Away spread line 变化
    if away_open and away_close:
        ol = away_open.get("line")
        cl = away_close.get("line")
        if ol is not None and cl is not None:
            try:
                signal += (float(cl) - float(ol)) / 3.0
            except (ValueError, TypeError):
                pass

    # Home spread line 变化（方向相反：home line 升高 = 客队被看好）
    if home_open and home_close:
        ol = home_open.get("line")
        cl = home_close.get("line")
        if ol is not None and cl is not None:
            try:
                signal += (float(ol) - float(cl)) / 3.0
            except (ValueError, TypeError):
                pass

    # Away odds 变化辅助信号（赔率下降 = 被看好）
    if away_open and away_close:
        oo = away_open.get("odds")
        co = away_close.get("odds")
        if oo and co:
            try:
                odds_shift = (float(oo) - float(co)) / 100.0
                signal += odds_shift * 0.3
            except (ValueError, TypeError):
                pass

    return max(-1.0, min(1.0, signal))


def decimal_to_american(decimal_odds: str | float | None) -> str | None:
    """Convert decimal odds to American format (API-Football uses decimal)."""
    if decimal_odds is None:
        return None
    try:
        dec = float(decimal_odds)
        if dec <= 1.0:
            return None
        if dec >= 2.0:
            return f"+{round((dec - 1) * 100)}"
        else:
            return str(round(-100 / (dec - 1)))
    except (ValueError, TypeError):
        return None


def remove_vig(home_p: float | None, draw_p: float | None, away_p: float | None = None,
              method: str = "logit") -> tuple[float | None, float | None, float | None]:
    """三向去水。

    P1-D 升级: 支持两种去水方法：
    - "logit"（默认）: 对数比例法 (Shen & Steinberg, 1994)，更准确处理热门方高抽水
    - "proportional": 简单比例法（向后兼容）
    """
    from core.config import BOOKMAKER_MARGIN, MIN_IMPLIED_PROB
    import math

    if draw_p is None:
        return None, None, None
    if home_p is None and away_p is None:
        return None, None, None
    _margin = BOOKMAKER_MARGIN
    if away_p is None:
        away_p = max(MIN_IMPLIED_PROB, 1.0 - home_p - draw_p)
    if home_p is None:
        home_p = max(MIN_IMPLIED_PROB, 1.0 - draw_p - away_p)

    if method == "logit":
        # 对数比例法：对隐含概率取对数 → 减均值 → 指数化 → 归一化
        probs = [home_p, draw_p, away_p]
        try:
            log_probs = [math.log(max(p, 1e-10)) for p in probs]
            mean_log = sum(log_probs) / len(log_probs)
            shifted = [math.exp(lp - mean_log) for lp in log_probs]
            total = sum(shifted)
            if total > 0:
                return (shifted[0] / total, shifted[1] / total, shifted[2] / total)
        except (ValueError, OverflowError, ZeroDivisionError):
            pass  # fallback to proportional

    # 比例法兜底
    total = home_p + draw_p + away_p
    if total <= 0:
        return home_p / _margin, draw_p / _margin, away_p / _margin
    return home_p / total, draw_p / total, away_p / total


def _collect_completed(past: list[dict]) -> list[tuple[str, str, str, int, int]]:
    """收集已完成比赛 (kickoff, home_en, away_en, home_goals, away_goals)。"""
    items: list[tuple[str, str, str, int, int]] = []
    for m in past:
        sc = m.get("score", "")
        if "-" not in sc:
            continue
        try:
            h, a = sc.split("-")
            h, a = int(h), int(a)
        except (ValueError, TypeError):
            continue
        items.append((m.get("kickoff_utc", ""), m.get("home_en", ""), m.get("away_en", ""), h, a))
    return items


def _team_form_before(completed: list[tuple[str, str, str, int, int]], team_en: str,
                      kickoff: str, n: int = 5) -> list[str]:
    """取某队在 kickoff 之前最近 n 场的结果序列（'W'/'D'/'L'，最近在前）。"""
    results: list[str] = []
    for ko, he, ae, h, a in completed:
        if ko >= kickoff:
            continue
        if he == team_en:
            results.append("W" if h > a else "L" if h < a else "D")
        elif ae == team_en:
            results.append("L" if h > a else "W" if h < a else "D")
    return results[-n:] if results else []


def _scores_from_results(results: list[str]) -> tuple[float, str, str, float]:
    """从 W/D/L 序列推导 form_score / form 串 / record 串 / record_score。"""
    if not results:
        return 0.5, "", "", 0.5
    score = sum(1.0 if r == "W" else 0.5 if r == "D" else 0.0 for r in results) / len(results)
    form_str = "".join(results)
    w = results.count("W")
    d = results.count("D")
    l = results.count("L")
    record_summary = f"{w}-{d}-{l}"
    rec_score = w / (w + d + l) if (w + d + l) else 0.5
    return round(score, 3), form_str, record_summary, round(rec_score, 3)


def _warm_team_names(events: list) -> None:
    """预热 LLM 队名翻译缓存（P6：队名统一中文，避免硬对照）。"""
    # 预热 LLM 队名翻译缓存（P6：队名统一中文，避免硬对照）
    try:
        from core.i18n import warm_translations
        _names: list[str] = []
        for ev in events:
            en = ev.get("name", "")
            if " at " in en:
                _a, _h = en.split(" at ", 1)
                _names.append(_h.strip())
                _names.append(_a.strip())
            else:
                _names.append(en)
            for _comp in (ev.get("competitions") or []):
                for _c in (_comp.get("competitors") or []):
                    _dn = (_c.get("team") or {}).get("displayName")
                    if _dn:
                        _names.append(_dn)
        warm_translations(_names)
    except Exception as e:
        logger.warning(f"Team-name translation warm-up failed: {e}")


def _derive_odds_fields(odds: dict, home_form: str, away_form: str,
                        home_records: list, away_records: list) -> dict:
    """从 odds dict 推导盘口/概率/形态评分字段。纯函数。"""
    details = odds.get("details", "")
    draw_ml = (odds.get("drawOdds") or {}).get("moneyLine", None)

    ps = odds.get("pointSpread") or {}
    spread_h = ps.get("home") or {}
    spread_a = ps.get("away") or {}
    spread_h.get("open")
    spread_h_close = spread_h.get("close") or {}
    spread_a_open = spread_a.get("open") or {}
    spread_a_close = spread_a.get("close") or {}

    tot = odds.get("total") or {}
    tot_o = tot.get("over") or {}
    tot_u = tot.get("under") or {}
    tot_o_close = tot_o.get("close") or {}
    tot_u_close = tot_u.get("close") or {}

    spread_h_line = spread_h_close.get("line", "")
    spread_h_odds = spread_h_close.get("odds", "")

    ml_team, ml_odds_str, home_ml_implied = parse_details(details)
    draw_implied = parse_american_odds(draw_ml)

    home_true, draw_true, away_true = remove_vig(home_ml_implied, draw_implied)

    spread_move = spread_movement_factor(spread_a_open, spread_a_close)

    h_fs = form_to_score(home_form)
    a_fs = form_to_score(away_form)
    h_rs = record_to_score(home_records)
    a_rs = record_to_score(away_records)

    return {"ml_odds_str": ml_odds_str, "draw_ml": draw_ml,
            "home_ml_implied": home_ml_implied, "draw_implied": draw_implied,
            "home_true": home_true, "draw_true": draw_true,
            "away_true": away_true,
            "spread_h_line": spread_h_line, "spread_h_odds": spread_h_odds,
            "spread_move": spread_move, "tot_o_close": tot_o_close,
            "tot_u_close": tot_u_close, "odds": odds,
            "h_fs": h_fs, "a_fs": a_fs, "h_rs": h_rs, "a_rs": a_rs}


def _extract_event_basics(ev: dict, now_utc: datetime) -> dict | None:
    """事件基础字段解析；缺 competitions 返回 None。纯函数。"""
    en_name = ev.get("name", "")
    if " at " in en_name:
        # ESPN: "Away at Home" → display "主队 vs 客队"
        away_en, home_en = en_name.split(" at ", 1)
        name = f"{to_cn(home_en)} vs {to_cn(away_en)}"
    else:
        name = to_cn(en_name)
    comps = ev.get("competitions", [{}])
    if not comps or not isinstance(comps, list) or len(comps) == 0:
        logger.warning(f"Event missing competitions data: {en_name}")
        return None
    comp = comps[0]
    status = comp.get("status", {}).get("type", {}).get("name", "")
    completed = comp.get("status", {}).get("type", {}).get("completed", False)
    date_str = ev.get("date", "")
    try:
        kickoff = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        kickoff = now_utc
    time_to = (kickoff - now_utc).total_seconds() / 3600

    competitors = comp.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

    home_name = to_cn(home["team"]["displayName"]) if home else "?"
    away_name = to_cn(away["team"]["displayName"]) if away else "?"
    home_abbr = home["team"]["abbreviation"] if home else ""
    away_abbr = away["team"]["abbreviation"] if away else ""
    home_score = home.get("score", "0") if home else "0"
    away_score = away.get("score", "0") if away else "0"

    home_form = home.get("form", "") if home else ""
    away_form = away.get("form", "") if away else ""
    home_records = home.get("records", []) if home else []
    away_records = away.get("records", []) if away else []

    odds_raw = comp.get("odds") or []
    odds = next((o for o in odds_raw if o), {}) if odds_raw else {}

    return {"name": name, "status": status, "completed": completed,
            "date_str": date_str, "time_to": time_to, "home": home,
            "away": away, "home_name": home_name, "away_name": away_name,
            "home_abbr": home_abbr, "away_abbr": away_abbr,
            "home_score": home_score, "away_score": away_score,
            "home_form": home_form, "away_form": away_form,
            "home_records": home_records, "away_records": away_records,
            "odds": odds}


def _build_match_record(ev: dict, now_utc: datetime) -> tuple[str | None, dict | None]:
    """ESPN 单事件 → (桶名 'past'|'future'|'in_progress'|None, 记录 dict|None)。纯函数。"""
    _b = _extract_event_basics(ev, now_utc)
    if _b is None:
        return None, None
    _od = _derive_odds_fields(_b["odds"], _b["home_form"], _b["away_form"],
                              _b["home_records"], _b["away_records"])
    rec = {
        "name": _b["name"], "status": _b["status"], "completed": _b["completed"],
        "kickoff_utc": _b["date_str"], "time_to_kickoff_h": round(_b["time_to"], 1),
        "home": _b["home_name"], "away": _b["away_name"],
        "home_en": _b["home"]["team"]["displayName"] if _b["home"] else "",
        "away_en": _b["away"]["team"]["displayName"] if _b["away"] else "",
        "home_abbr": _b["home_abbr"], "away_abbr": _b["away_abbr"],
        "score": f"{_b['home_score']}-{_b['away_score']}" if _b["status"] in ("STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_FINAL_ET") else "",
        "home_form": _b["home_form"], "away_form": _b["away_form"],
        "home_form_score": round(_od["h_fs"], 3), "away_form_score": round(_od["a_fs"], 3),
        "home_record": _b["home_records"][0].get("summary","") if _b["home_records"] else "",
        "away_record": _b["away_records"][0].get("summary","") if _b["away_records"] else "",
        "home_record_score": round(_od["h_rs"], 3), "away_record_score": round(_od["a_rs"], 3),
        "ml_home_close": _od["ml_odds_str"], "draw_ml": _od["draw_ml"],
        "home_ml_implied": round(_od["home_ml_implied"], 4) if _od["home_ml_implied"] else None,
        "draw_implied": round(_od["draw_implied"], 4) if _od["draw_implied"] else None,
        "home_true_prob": round(_od["home_true"], 4) if _od["home_true"] else None,
        "draw_true_prob": round(_od["draw_true"], 4) if _od["draw_true"] else None,
        "away_true_prob": round(_od["away_true"], 4) if _od["away_true"] else None,
        "spread_home_line": _od["spread_h_line"], "spread_home_close_odds": _od["spread_h_odds"],
        "spread_movement_score": round(_od["spread_move"], 3),
        "total_over_close": _od["tot_o_close"].get("line",""), "total_under_close": _od["tot_u_close"].get("line",""),
        "odds_data_available": bool(_od["odds"].get("details") or _od["odds"].get("drawOdds") or _od["odds"].get("pointSpread") or _od["odds"].get("total")),
    }
    if _b["status"] in ("STATUS_FULL_TIME", "STATUS_FINAL_PEN", "STATUS_FINAL_ET"):
        return "past", rec
    elif _b["status"] == "STATUS_SCHEDULED":
        return "future", rec
    else:
        return "in_progress", rec


def parse_events(events: list, now_utc: datetime | None = None) -> tuple[list, list, list]:
    """解析 ESPN events → 结束比赛列表 + 待预测比赛列表"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    past = []
    future = []
    in_progress = []

    _warm_team_names(events)

    for ev in events:
        bucket, rec = _build_match_record(ev, now_utc)
        if rec is None:
            continue
        {"past": past, "future": future, "in_progress": in_progress}[bucket].append(rec)

    # ── 状态/战绩信号回填（P2 修复）──────────────────
    # 默认数据源 football-data 的每场不提供 form/records，导致该信号恒为中性 0.5。
    # 这里从已完成比赛的真实比分推导各队近期状态，回填到所有比赛；
    # 若数据源本身提供了 form（如 ESPN）则保留原值。
    completed = _collect_completed(past)
    _warned_no_form = False
    for m in list(past) + list(future) + list(in_progress):
        ko = m.get("kickoff_utc", "")
        for en_key, form_key, fs_key, rec_key, rs_key in (
            ("home_en", "home_form", "home_form_score", "home_record", "home_record_score"),
            ("away_en", "away_form", "away_form_score", "away_record", "away_record_score"),
        ):
            if m.get(form_key):
                continue  # 数据源已提供，保留
            results = _team_form_before(completed, m.get(en_key, ""), ko)
            if results:
                fs, form_str, rec_sum, rs = _scores_from_results(results)
                m[form_key] = form_str
                m[fs_key] = fs
                m[rec_key] = rec_sum
                m[rs_key] = rs
            elif not _warned_no_form:
                logger.warning("form/records 数据源未提供且无历史比赛可推导，状态/战绩信号回退为中性值")
                _warned_no_form = True

    return past, future, in_progress
