from __future__ import annotations

"""Calibration: historical match analysis and prediction offset computation."""

import json
import time
from pathlib import Path
from typing import Any

from core.config import PREDICTIONS_DIR, FOOTBALL_DIR
from core.log import logger


# ── P0-1: Calibration 持久化路径 ────────────────────────
# 每个联赛独立持久化，避免跨联赛指数平滑污染（P1-2 修复）
def _calibration_cache_file(league: str | None = None) -> Path:
    if league:
        return FOOTBALL_DIR / "references" / f".calibration_state_{league}.json"
    return FOOTBALL_DIR / "references" / ".calibration_state.json"


def _try_load_json(path) -> dict | None:
    """尝试用多种编码加载 JSON 文件（兼容 Windows gbk 历史文件）"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            continue
    return None


def _load_prev_offset(league: str | None = None) -> dict | None:
    """从持久化文件加载上一次校准偏移（P0-1 修复，按联赛隔离 P1-2）"""
    cache_file = _calibration_cache_file(league)
    try:
        if cache_file.exists():
            data = _try_load_json(cache_file)
            if data and isinstance(data, dict):
                logger.info(f"Loaded previous calibration offset (league={league or 'global'}, "
                            f"sample_size={data.get('sample_size', '?')})")
                return data
    except Exception as e:
        logger.warning(f"Failed to load previous calibration offset: {e}")
    return None


def _save_offset(offset: dict, league: str | None = None) -> None:
    """持久化校准偏移到文件（P0-1 修复，按联赛隔离 P1-2）"""
    cache_file = _calibration_cache_file(league)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # 保存时附带时间戳，便于调试
        offset["_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        offset["_league"] = league or "global"
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(offset, f, ensure_ascii=False, indent=2)
        logger.debug(f"Saved calibration offset to {cache_file}")
    except Exception as e:
        logger.warning(f"Failed to save calibration offset: {e}")


def load_historical_past_matches(days: int = 30, league: str | None = None) -> list[dict[str, Any]]:
    """读取历史 predictions/ 文件中的 past_matches + references/historical_past_matches.json，去重后返回。

    P1-2 修复: 当传入 league 时，只纳入该联赛的历史文件（按文件顶层 'league' 字段过滤），
    避免五联赛一起跑时主/平/客胜率修正因子相互污染叠加。
    """
    all_past: list[dict[str, Any]] = []
    cutoff = time.time() - days * 86400

    for f in PREDICTIONS_DIR.glob("prediction_*.json"):
        if f.stat().st_mtime < cutoff:
            continue
        try:
            d = _try_load_json(f)
            if d is None:
                continue
            # 按联赛过滤：文件顶层 league 字段
            if league is not None and d.get("league") != league:
                continue
            all_past.extend(d.get("past_matches", []))
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    hist_file = FOOTBALL_DIR / "references" / "historical_past_matches.json"
    if hist_file.exists():
        try:
            hist_data = _try_load_json(hist_file) or []
            # 同样按联赛过滤（仅有 league 字段的历史条目才会被纳入）
            if league is not None:
                hist_data = [m for m in hist_data if m.get("league") == league]
            all_past.extend(hist_data)
        except (json.JSONDecodeError, OSError):
            pass

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for m in all_past:
        key = f"{m.get('kickoff_utc','')}_{m.get('home','')}_{m.get('away','')}"
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def append_historical_past_matches(league: str, past_matches: list[dict[str, Any]],
                                   base_dir: Path | None = None) -> int:
    """将本次运行的完赛记录合并进 references/historical_past_matches.json。

    仅纳入带有效比分的已结束比赛；按 联赛+kickoff+主客队 去重。
    供 ML 训练 / calibration 跨 GHA 运行累计样本（P5：线上 ML 激活的前提）。
    """
    ref_dir = (base_dir or FOOTBALL_DIR) / "references"
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return 0
    hist_file = ref_dir / "historical_past_matches.json"
    try:
        existing = _try_load_json(hist_file) or []
    except Exception:
        existing = []
    if not isinstance(existing, list):
        existing = []

    seen: set[str] = set()
    for m in existing:
        seen.add(f"{m.get('league','')}_{m.get('kickoff_utc','')}_{m.get('home','')}_{m.get('away','')}")

    merged = list(existing)
    added = 0
    for m in past_matches:
        score = m.get("score", "")
        if not score or "-" not in score:
            continue
        parts = score.split("-")
        if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
            continue
        entry = dict(m)
        entry["league"] = league
        key = f"{league}_{entry.get('kickoff_utc','')}_{entry.get('home','')}_{entry.get('away','')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
        added += 1

    if added:
        try:
            with open(hist_file, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save historical past matches: {e}")
            return 0
    return added


def _collect_calibration_samples(past_matches: list[dict[str, Any]]) -> tuple[int, int, int]:
    """收集/过滤窗口期比赛样本：解析比分并统计主胜/平局/客胜场次。

    非法比分（缺 '-'、非两段数字）自动跳过。返回 (home_wins, draws, away_wins)。
    """
    home_wins = draws = away_wins = 0

    for m in past_matches:
        score = m.get("score", "")
        if not score or "-" not in score:
            continue
        parts = score.split("-")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        hs, aws = int(parts[0]), int(parts[1])
        if hs > aws:
            home_wins += 1
        elif hs == aws:
            draws += 1
        else:
            away_wins += 1

    return home_wins, draws, away_wins


def _derive_offsets(actual_home_rate: float, actual_draw_rate: float, actual_away_rate: float,
                    total: int, league: str | None = None) -> dict[str, Any]:
    """从实际赛果分布推导校准修正系数（含样本量加权与指数平滑 P0-1/P1-2）。

    产出字段收进返回 dict：home/draw/away 及 onside 修正、样本统计与真实分布。
    """
    # 使用足球联赛实际分布作为基线（主胜~45%, 平局~25%, 客胜~30%）
    _baseline_home = 0.45
    _baseline_draw = 0.25
    _baseline_away = 0.30

    # 样本量加权：小样本时降低修正幅度，避免噪声放大（P1-1）
    sample_weight = min(1.0, total / 50.0)
    _clamp_lo, _clamp_hi = 0.5, 2.0

    home_correction = max(_clamp_lo, min(_clamp_hi, actual_home_rate / _baseline_home))
    draw_correction = max(_clamp_lo, min(_clamp_hi, actual_draw_rate / _baseline_draw))
    away_correction = max(_clamp_lo, min(_clamp_hi, actual_away_rate / _baseline_away))

    # ── P0-1 修复: 指数平滑现在真正生效 ──
    _smooth = 0.7
    prev_offset = _load_prev_offset(league)  # 按联赛加载上一次值（P1-2）
    if prev_offset:
        home_correction = _smooth * prev_offset.get("home_correction", home_correction) + (1 - _smooth) * home_correction
        draw_correction = _smooth * prev_offset.get("draw_correction", draw_correction) + (1 - _smooth) * draw_correction
        away_correction = _smooth * prev_offset.get("away_correction", away_correction) + (1 - _smooth) * away_correction
        logger.info(f"Exponential smoothing applied (α={_smooth})")

    # 对 Onside signal 的修正幅度减半（避免双修正误差放大）
    onside_home_correction = 1.0 + (home_correction - 1.0) * sample_weight * 0.5
    onside_away_correction = 1.0 + (away_correction - 1.0) * sample_weight * 0.5

    return {
        "home_correction": round(home_correction, 3),
        "draw_correction": round(draw_correction, 3),
        "away_correction": round(away_correction, 3),
        "onside_home_correction": round(onside_home_correction, 3),
        "onside_away_correction": round(onside_away_correction, 3),
        "sample_size": total,
        "sample_weight": round(sample_weight, 3),
        "actual_home_rate": round(actual_home_rate, 3),
        "actual_draw_rate": round(actual_draw_rate, 3),
        "actual_away_rate": round(actual_away_rate, 3),
    }


def compute_calibration_offset(past_matches: list[dict[str, Any]], league: str | None = None) -> dict[str, Any] | None:
    """
    从累积 past_matches 计算 calibration 修正因子。
    用实际赛果分布 vs 均匀分布(1/3)的比率做软修正。

    P0-1 修复: 指数平滑现在会真正执行，通过 _load_prev_offset / _save_offset 持久化。
    P1-2 修复: league 参数使指数平滑按联赛独立进行，不再跨联赛污染。
    """
    if len(past_matches) < 5:
        return None

    home_wins, draws, away_wins = _collect_calibration_samples(past_matches)

    total = home_wins + draws + away_wins
    if total < 5:
        logger.debug(f"Calibration: only {total} finished matches (<5), skipping offset")
        return None

    actual_home_rate = home_wins / total
    actual_draw_rate = draws / total
    actual_away_rate = away_wins / total

    offsets = _derive_offsets(actual_home_rate, actual_draw_rate, actual_away_rate, total, league)

    # ── P0-1 修复: 持久化当前校准值 ──
    _save_offset(offsets, league)

    logger.info(f"Calibration offset(n={total}, weight={offsets['sample_weight']:.2f}): "
                f"home×{offsets['home_correction']:.3f} draw×{offsets['draw_correction']:.3f} away×{offsets['away_correction']:.3f} | "
                f"onside_home×{offsets['onside_home_correction']:.3f} onside_away×{offsets['onside_away_correction']:.3f}")

    return offsets


def _parse_score(score_str: str | None) -> tuple[int, int] | None:
    """Parse 'H-A' score string → (home, away) ints, or None on failure."""
    if not score_str or "-" not in str(score_str):
        return None
    parts = str(score_str).split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def build_calibration(past_matches: list[dict[str, Any]], future_matches: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """从结束比赛计算校准参数"""
    if not past_matches:
        return {"note": "no past matches to calibrate from"}

    home_wins = sum(1 for m in past_matches if (s := _parse_score(m.get("score"))) and s[0] > s[1])
    draws = sum(1 for m in past_matches if (s := _parse_score(m.get("score"))) and s[0] == s[1])
    away_wins = sum(1 for m in past_matches if (s := _parse_score(m.get("score"))) and s[0] < s[1])
    total = home_wins + draws + away_wins

    favorite_wins = 0
    total_odds_based = 0
    for m in past_matches:
        hp = m.get("home_true_prob")
        if hp and hp > 0.5:
            total_odds_based += 1
            if m["score"]:
                try:
                    s = _parse_score(m.get("score"))
                    if s and s[0] > s[1]:
                        favorite_wins += 1
                except (ValueError, AttributeError):
                    pass

    return {
        "total_matches": total,
        "home_wins": home_wins,
        "draws": draws,
        "away_wins": away_wins,
        "home_win_rate": round(home_wins/total, 3) if total else 0,
        "draw_rate": round(draws/total, 3) if total else 0,
        "away_win_rate": round(away_wins/total, 3) if total else 0,
        "favored_by_odds": total_odds_based,
        "favored_won": favorite_wins,
        "odds_accuracy": round(favorite_wins/total_odds_based, 3) if total_odds_based else 0,
    }
