"""web.services.odds_move —— 赔率异动事件的只读视图（建议 5 子项 ②）。

与 `web/services/recommend.py` 同一套访问约定：**顶层不 import psycopg / scripts**，
只在函数内延迟 import `store.pg`；模块级自举一次 `scripts/`。

⚠️ **口径纪律**：
- 异动是**相邻 ~10 分钟快照的跳变**（不是「开盘 vs 临场」的首末差 —— 后者 ≥5% 命中 77%，是噪音）；
- 读不出来一律**降级为空**（页面显示「暂无异动」，不 5xx、不编数）；
- `pushed=false` 就是**没推**（未配 webhook）—— **页面必须如实显示，不得美化**。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("web.odds_move")

# T3 启动链不含 scripts/：自举一次。
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)

_SIDE_CN = {"h": "主胜", "d": "平", "a": "客胜"}

_RECENT_SQL = """
SELECT m.match_id, o.league_cn, o.match_date, o.option_code,
       o.prev_ts, o.curr_ts, o.sp_prev, o.sp_curr, o.move_pct, o.threshold_pct,
       o.pushed, o.pushed_at, o.detected_at,
       m.home_cn, m.away_cn, m.match_num, m.match_num_str
  FROM analysis.odds_move o
  LEFT JOIN fact.jc_match m ON m.match_id = o.match_id
 WHERE o.curr_ts >= now() - make_interval(hours => %s)
 ORDER BY o.curr_ts DESC, o.move_pct DESC
 LIMIT %s
"""

_OPS_SQL = """
SELECT count(*) AS n_events,
       count(*) FILTER (WHERE pushed) AS n_pushed,
       count(DISTINCT match_id) AS n_matches,
       round(max(move_pct), 2) AS max_move_pct,
       round(avg(move_pct), 2) AS avg_move_pct,
       max(curr_ts) AS last_curr_ts,
       min(threshold_pct) AS threshold_pct
  FROM analysis.odds_move
 WHERE curr_ts >= now() - make_interval(hours => %s)
"""


def _row_to_dict(r: tuple) -> dict[str, Any]:
    return {
        "match_id": int(r[0]),
        "league_cn": r[1],
        "match_date": str(r[2]) if r[2] else None,
        "option_code": r[3],
        "option_cn": _SIDE_CN.get(r[3], r[3]),
        "prev_ts": str(r[4]),
        "curr_ts": str(r[5]),
        "sp_prev": float(r[6]) if r[6] is not None else None,
        "sp_curr": float(r[7]) if r[7] is not None else None,
        "move_pct": float(r[8]) if r[8] is not None else None,
        "threshold_pct": float(r[9]) if r[9] is not None else None,
        "pushed": bool(r[10]),
        "pushed_at": str(r[11]) if r[11] else None,
        "detected_at": str(r[12]),
        "home_cn": r[13],
        "away_cn": r[14],
        "match_num": int(r[15]) if r[15] is not None else None,
        "match_num_str": r[16],
    }


def _read_recent(hours: int = 24, limit: int = 50) -> tuple[list[dict[str, Any]], bool]:
    """读最近 N 小时的异动事件。返回 ``(rows, degraded)``；degraded=True ⇒ 读失败。"""
    try:
        from store import pg

        h = max(1, min(int(hours), 24 * 30))
        lim = max(1, min(int(limit), 500))
        with pg.read_conn("ro") as conn:
            rows = conn.execute(_RECENT_SQL, (h, lim)).fetchall()
        return [_row_to_dict(r) for r in rows], False
    except Exception as exc:  # noqa: BLE001 - 只读视图，任何异常降级为空
        logger.warning("web.odds_move 读异动事件失败（降级为空，degraded=True）：%s", exc)
        return [], True


def recent(hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
    """最近 N 小时的异动事件（**只读**，异常降级为空）。

    ⚠️ 只看这个返回值**分不出**「读失败」与「这段时间没有异动」——
    需要区分的调用方请用 `recent_status()`。
    """
    rows, _degraded = _read_recent(hours, limit)
    return rows


def recent_status(hours: int = 24, limit: int = 50) -> dict[str, Any]:
    """异动事件 + **degraded 标志**（True = 读失败，不是「没有异动」）。"""
    rows, degraded = _read_recent(hours, limit)
    return {"rows": rows, "degraded": degraded}


def ops(hours: int = 24) -> dict[str, Any]:
    """运维视图：命中量 / 推送状态 / 阈值。异常降级为空（带 degraded=True）。"""
    try:
        from store import pg

        h = max(1, min(int(hours), 24 * 30))
        with pg.read_conn("ro") as conn:
            r = conn.execute(_OPS_SQL, (h,)).fetchone()
        n = int(r[0] or 0)
        return {
            "window_hours": h,
            "n_events": n,
            "n_pushed": int(r[1] or 0),
            "n_matches": int(r[2] or 0),
            "max_move_pct": float(r[3]) if r[3] is not None else None,
            "avg_move_pct": float(r[4]) if r[4] is not None else None,
            "last_curr_ts": str(r[5]) if r[5] else None,
            "threshold_pct": float(r[6]) if r[6] is not None else None,
            "push_configured": push_configured(),
            "degraded": False,
            "note": ("异动口径 = 相邻 ~10 分钟快照的跳变（首末差 ≥5% 会命中 77%，是噪音 ⇒ 不用）。"
                     "pushed=false 就是「没推」（未配 webhook）—— 如实显示，不美化。"
                     "⚠️ 盘口异动是「信息」，不是下注理由。"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("web.odds_move 读运维视图失败（降级为空，degraded=True）：%s", exc)
        return {"window_hours": hours, "n_events": 0, "n_pushed": 0, "n_matches": 0,
                "max_move_pct": None, "avg_move_pct": None, "last_curr_ts": None,
                "threshold_pct": None, "push_configured": push_configured(),
                "degraded": True, "note": "读失败"}


def push_configured() -> bool:
    """是否配置了推送通道（**只回布尔值，不回 URL**）。"""
    try:
        from market.notify import feishu_configured

        return feishu_configured()
    except Exception:  # noqa: BLE001 - 拿不到就当没配
        return False
