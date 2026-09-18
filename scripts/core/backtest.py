from __future__ import annotations

"""Backtesting: compare predictions against actual results."""

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from core.config import PREDICTIONS_DIR, LEAGUE_CONFIG, ESPN_TIMEOUT_SECONDS
from core.calibration import _try_load_json


def _bk_parse_window(data_window: str) -> tuple[str, str]:
    """解析 data_window，返回 (start_date, end_date)，兼容多种日期格式。"""
    if "-" in data_window:
        start_raw, end_raw = data_window.split("-", 1)
    else:
        start_raw = end_raw = data_window

    def normalize_date(s: str) -> str:
        s = s.strip()
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if len(s) == 10 and s[4] == '-' and s[7] == '-':
            return s
        return s

    return normalize_date(start_raw), normalize_date(end_raw)


def _bk_load_recent_predictions(cutoff: float) -> list[dict[str, Any]]:
    """加载 cutoff 之后生成的预测文件，返回全部 predictions 条目。"""
    preds: list[dict[str, Any]] = []
    for f in sorted(PREDICTIONS_DIR.glob("prediction_*.json")):
        if f.stat().st_mtime < cutoff:
            continue
        try:
            with open(f, encoding="utf-8") as _fh:
                data = json.load(_fh)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            try:
                with open(f, encoding="gbk") as _fh:
                    data = json.load(_fh)
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                continue
        preds.extend(data.get("predictions", []))
    return preds


def _bk_reconcile_predictions(
    preds: list[dict[str, Any]], actuals: dict[str, tuple[int, int]]
) -> tuple[int, int, int, int, list[dict[str, Any]]]:
    """将预测条目与实际赛果比对，返回 (total, correct_dir, correct_score, correct_ou, details)。"""
    correct_dir = correct_score = correct_ou = total = 0
    details: list[dict[str, Any]] = []
    for p in preds:
        r = actuals.get(_bk_stable_key(p))
        if not r:
            continue
        h_act, a_act = r
        total += 1
        d = p.get("direction", "")
        # 三向方向判定：先判平局（最精确），再按主/客队名前缀区分主胜/客胜
        is_home_win = False
        is_away_win = False
        is_draw = "平" in d or "平局" in d
        if not is_draw:
            home_team = p.get("home", "")
            away_team = p.get("away", "")
            if d.startswith(home_team):
                is_home_win = True
            elif d.startswith(away_team):
                is_away_win = True

        if (is_home_win and h_act > a_act) or \
           (is_draw and h_act == a_act) or \
           (is_away_win and h_act < a_act):
            correct_dir += 1
            dir_ok = True
        else:
            dir_ok = False
        if p.get("predicted_score", "") == f"{h_act}-{a_act}":
            correct_score += 1
        ou = p.get("over_under", "")
        if "Over" in ou and h_act + a_act > 2.5:
            correct_ou += 1
        elif "Under" in ou and h_act + a_act < 2.5:
            correct_ou += 1
        details.append({
            "match": p.get("match",""),
            "predicted": p.get("predicted_score",""),
            "actual": f"{h_act}-{a_act}",
            "direction_correct": dir_ok,
        })
    return total, correct_dir, correct_score, correct_ou, details


def reconcile_predictions(past_matches: list[dict[str, Any]], days: int = 7) -> dict[str, Any] | None:
    """将历史预测文件中的预测与当前实际赛果比对，返回回测统计。"""
    actuals: dict[str, tuple[int, int]] = {}
    for m in past_matches:
        key = _bk_stable_key(m)
        score = m.get("score", "")
        if key and score and "-" in score:
            try:
                h, a = score.split("-")
                actuals[key] = (int(h), int(a))
            except (ValueError, IndexError):
                pass
    if not actuals:
        return None

    cutoff = time.time() - days * 86400
    preds = _bk_load_recent_predictions(cutoff)
    total, correct_dir, correct_score, correct_ou, details = _bk_reconcile_predictions(preds, actuals)
    if total == 0:
        return None
    return {
        "reconciled": total,
        "correct_direction": correct_dir,
        "correct_score": correct_score,
        "correct_over_under": correct_ou,
        "direction_accuracy": round(correct_dir / total, 3),
        "score_accuracy": round(correct_score / total, 3),
        "over_under_accuracy": round(correct_ou / total, 3),
        "details": details,
    }


def _bk_stable_key(rec: dict[str, Any]) -> str:
    """英文原名稳定主键（与 AI 打分链路同一约定）。

    ``name`` / ``match`` 字段是 ``to_cn()`` 的翻译结果，而 i18n 表未覆盖的队名
    会原样返回英文，于是同一场比赛在不同运行里可能一个中文、一个英文，
    连接直接失败（实测 41 个 actuals 与 6 个 preds 键交集为 0）。
    ``home_en`` / ``away_en`` 来自数据源 displayName，跨运行稳定。
    """
    h = (rec.get("home_en") or "").strip()
    a = (rec.get("away_en") or "").strip()
    if h and a:
        return f"{h}|{a}"
    return (rec.get("name") or rec.get("match") or "").strip()


def _bk_collect_league_data(
    league_key: str, cutoff: float
) -> tuple[dict[str, tuple[int, int]], list[dict[str, Any]]]:
    """收集指定联赛 historical past_matches 的 actuals 与去重后的 predictions。

    两侧一律用 ``_bk_stable_key`` 建键，保证中英混杂的显示名不会打断连接。
    """
    actuals: dict[str, tuple[int, int]] = {}
    preds: list[dict[str, Any]] = []
    seen_pred: set[str] = set()

    for f in sorted(PREDICTIONS_DIR.glob("prediction_*.json")):
        if f.stat().st_mtime < cutoff:
            continue
        data = _try_load_json(f)
        if not data or not isinstance(data, dict):
            continue
        if data.get("league") != league_key:
            continue
        for m in data.get("past_matches", []):
            key = _bk_stable_key(m)
            score = m.get("score", "")
            if key and score and "-" in score:
                try:
                    h, a = score.split("-")
                    actuals[key] = (int(h), int(a))
                except (ValueError, IndexError):
                    pass
        for p in data.get("predictions", []):
            key = _bk_stable_key(p)
            if not key or key in seen_pred:
                continue
            seen_pred.add(key)
            preds.append(p)
    return actuals, preds


def league_accuracy(league_key: str, days: int = 7) -> dict[str, Any] | None:
    """按联赛统计最近 days 天预测的方向/比分/大小球命中率。

    聚合该联赛历史预测文件中已结束比赛的 actuals（past_matches 含比分），
    再与历史 predictions 按比赛名匹配，得出真实命中率（P5 产品建议）。
    """
    cutoff = time.time() - days * 86400
    actuals, preds = _bk_collect_league_data(league_key, cutoff)

    if not preds:
        return None

    correct_dir = correct_score = correct_ou = total = 0
    for p in preds:
        r = actuals.get(_bk_stable_key(p))
        if not r:
            continue
        h_act, a_act = r
        total += 1
        d = p.get("direction", "")
        home_team = p.get("home", "")
        away_team = p.get("away", "")
        is_draw = "平" in d or "平局" in d
        is_home_win = is_away_win = False
        if not is_draw:
            if d.startswith(home_team):
                is_home_win = True
            elif d.startswith(away_team):
                is_away_win = True
        if (is_home_win and h_act > a_act) or (is_draw and h_act == a_act) or (is_away_win and h_act < a_act):
            correct_dir += 1
        if p.get("predicted_score", "") == f"{h_act}-{a_act}":
            correct_score += 1
        ou = p.get("over_under", "")
        if "Over" in ou and h_act + a_act > 2.5:
            correct_ou += 1
        elif "Under" in ou and h_act + a_act < 2.5:
            correct_ou += 1

    if total == 0:
        return None
    return {
        "window_days": days,
        "reconciled": total,
        "direction_accuracy": round(correct_dir / total, 3),
        "score_accuracy": round(correct_score / total, 3),
        "over_under_accuracy": round(correct_ou / total, 3),
    }


def _bk_fetch_api_actuals(
    api_key: str, fixture_league_id: Any, start_date: str, end_date: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """从 API-Football 拉取窗口内已结束赛果，建 (主队, 客队) → 赛果 索引。"""
    headers = {
        "User-Agent": "LeaguePredict/4.1",
        "x-apisports-key": api_key,
    }
    url = f"https://v3.football.api-sports.io/fixtures?dateFrom={start_date}&dateTo={end_date}"
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=ESPN_TIMEOUT_SECONDS)
    data = json.loads(resp.read())

    actual_index: dict[tuple[str, str], dict[str, Any]] = {}
    for fixture in data.get("response", []):
        league_info = fixture.get("league", {})
        if league_info.get("id") != fixture_league_id:
            continue
        teams = fixture.get("teams", {})
        home_name = teams.get("home", {}).get("name", "")
        away_name = teams.get("away", {}).get("name", "")
        goals = fixture.get("goals", {})
        home_goals = goals.get("home")
        away_goals = goals.get("away")
        status = fixture.get("fixture", {}).get("status", {}).get("short", "")
        if status != "FT" or home_goals is None or away_goals is None:
            continue
        actual_index[(home_name, away_name)] = {
            "winner": "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw",
            "score": f"{home_goals}-{away_goals}",
        }
    return actual_index


def _bk_match_predictions_against(
    preds: list[dict[str, Any]], actual_index: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    """将预测与赛果索引匹配，返回可评估行。"""
    rows: list[dict[str, Any]] = []
    for pred in preds:
        home = pred.get("home", "")
        away = pred.get("away", "")
        actual = actual_index.get((home, away))
        if not actual:
            continue
        predicted: str | None = None
        score = pred.get("predicted_score") or ""
        if "-" in score:
            try:
                h, a = [int(x.strip()) for x in score.split("-", 1)]
            except Exception:
                h = a = None
            if h is not None:
                predicted = "home" if h > a else "away" if a > h else "draw"
        if predicted is None:
            continue
        rows.append({
            "home": home,
            "away": away,
            "predicted": predicted,
            "actual": actual["winner"],
            "correct": predicted == actual["winner"],
            "predicted_score": score,
            "actual_score": actual.get("score", ""),
        })
    return rows


def _backtest_api_football(pred_data: dict[str, Any], league_config: dict) -> dict[str, Any]:
    """从 API-Football 获取实际赛果进行回测。"""
    api_key = os.environ.get("API_FOOTBALL_KEY", "")
    if not api_key:
        return {"status": "skip", "reason": "API_FOOTBALL_KEY not set"}

    fixture_league_id = league_config.get("api_football_id")
    if not fixture_league_id:
        return {"status": "skip", "reason": "no api_football_id configured"}

    start_date, end_date = _bk_parse_window(pred_data.get("data_window", ""))

    try:
        actual_index = _bk_fetch_api_actuals(api_key, fixture_league_id, start_date, end_date)
    except Exception as e:
        return {"status": "error", "error": f"API-Football fetch failed: {e}"}

    rows = _bk_match_predictions_against(pred_data.get("predictions", []), actual_index)
    if not rows:
        return {"status": "no_evaluable_matches", "matched_matches": 0}
    correct = sum(1 for r in rows if r["correct"])
    return {
        "status": "ok",
        "matched_matches": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows),
        "rows": rows,
    }


def _bk_fetch_fd_actuals(
    api_key: str, league_id: Any, start_date: str, end_date: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """从 football-data.org 拉取窗口内已结束赛果，建 (主队, 客队) → 赛果 索引。"""
    url = f"https://api.football-data.org/v4/competitions/{league_id}/matches?dateFrom={start_date}&dateTo={end_date}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "LeaguePredict/4.1",
        "X-Auth-Token": api_key,
    })
    resp = urllib.request.urlopen(req, timeout=ESPN_TIMEOUT_SECONDS)
    data = json.loads(resp.read())

    actual_index: dict[tuple[str, str], dict[str, Any]] = {}
    for m in data.get("matches", []):
        home_name = m.get("homeTeam", {}).get("name", "")
        away_name = m.get("awayTeam", {}).get("name", "")
        score = m.get("score", {})
        home_goals = score.get("fullTime", {}).get("home")
        away_goals = score.get("fullTime", {}).get("away")
        status = m.get("status", "")
        if status != "FINISHED" or home_goals is None or away_goals is None:
            continue
        try:
            h, a = int(home_goals), int(away_goals)
        except Exception:
            continue
        actual_index[(home_name, away_name)] = {
            "winner": "home" if h > a else "away" if a > h else "draw",
            "score": f"{h}-{a}",
        }
    return actual_index


def backtest_with_live_results(prediction_file: str) -> dict[str, Any]:
    """Auto-fetch actual results from football-data.org for the prediction window."""
    try:
        pred_data = json.loads(Path(prediction_file).read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "error": str(e)}

    league = pred_data.get("league", "epl")
    league_config = LEAGUE_CONFIG.get(league, LEAGUE_CONFIG["epl"])
    data_source = league_config.get("data_source", "football-data")

    if data_source == "api-football":
        return _backtest_api_football(pred_data, league_config)
    elif data_source != "football-data":
        return {"status": "skip", "reason": f"unsupported data source: {data_source}"}

    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not api_key:
        return {"status": "skip", "reason": "FOOTBALL_DATA_API_KEY not set"}

    start_date, end_date = _bk_parse_window(pred_data.get("data_window", ""))
    league_id = league_config["league_id"]
    try:
        actual_index = _bk_fetch_fd_actuals(api_key, league_id, start_date, end_date)
    except Exception as e:
        return {"status": "error", "error": f"fetch failed: {e}"}

    rows = _bk_match_predictions_against(pred_data.get("predictions", []), actual_index)
    if not rows:
        return {"status": "no_evaluable_matches", "matched_matches": 0}
    correct = sum(1 for r in rows if r["correct"])
    return {
        "status": "ok",
        "matched_matches": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows),
        "rows": rows,
    }