"""web.routers.jc_ops — 竞彩看板「入库与对照」只读 API（P0-DASH2c，全部 require_auth）。

端点：
- GET /api/jc/ops  四段一次取齐：球队对照 / 入库话题 / 当日配额 / 近 24h 拒行
  返回 {"rows": {"teams": [...], "topics": [...], "quota": [...], "rejects": [...]},
        "ok":   {"teams": true, ...},   ← 分键降级标记（store 异常该键 false）
        "sql_note": "...", "degraded": 是否任一段降级}
- GET /api/jc/ops/odds-move  赔率异动事件的运维视图（建议 5）
  返回 {"n_events", "n_pushed", "n_matches", "max_move_pct", "threshold_pct",
        "push_configured", ...} —— **push_configured 只回布尔值，不回 webhook URL**。

口径判定归页面/DASH 层：本层不发明健康判定，只做只读查询与降级标记。
（n_orphan = ops.file_arrival 里 done_marker=false 的行，即无 marker 认领、只由 orphan 阶段
装载的「无主通道」文件；R48 起 loader 有增量水位+已装载跳过，它们不再每轮被重扫。）
本文件不得出现 psycopg；端点内延迟 import store（顶层不 import，T5 免 psycopg 环境）。
web 启动带 PYTHONPATH=.:scripts（§9-38），故模块级自举一次。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Depends

from web.auth import require_auth

logger = logging.getLogger("web.jc_ops")
router = APIRouter(prefix="/api/jc", tags=["jc-ops"])

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_NOTE = ("teams=最新 business_date 各联赛对照进度（n_af_ready 才能直接预测）；"
         "topics=ops.file_arrival 按话题聚合（n_orphan 是无主通道：无 marker 认领、只由 orphan 阶段装载；"
         "R48 起 loader 有增量水位+已装载跳过，这批文件不再每轮重扫）；"
         "quota=当日各源配额余量（空=未发请求）；rejects=近 24h 进库差与失败。")


@router.get("/ops")
def ops(_: None = Depends(require_auth)) -> dict:
    """入库与对照四段快照；任一段 store 异常仅该键降级，不 5xx。"""
    from store import jc_ops_view
    snap = jc_ops_view.ops_snapshot()
    keys = ("teams", "topics", "quota", "rejects")
    rows = {k: snap.get(k, []) for k in keys}
    ok = {k: bool(snap.get("ok_" + k)) for k in keys}
    return {
        "rows": rows,
        "ok": ok,
        "sql_note": _NOTE,
        "degraded": not all(ok.values()),
    }


@router.get("/ops/odds-move")
def ops_odds_move(hours: int = 24, _: None = Depends(require_auth)) -> dict:
    """赔率异动事件的运维视图（建议 5）：阈值 / 命中量 / 推送状态。

    ⚠️ **口径**：异动 = **相邻 ~10 分钟快照的跳变**，不是「开盘 vs 临场」的首末差
    （后者 ≥5% 命中 77%，是纯噪音）。默认阈值 5%（可配 3%），实测命中 0.40% ⇒ ≈0.6 次/天。

    ⚠️ **诚实性**：`push_configured` 只回**布尔值**（webhook URL 是凭证，绝不回显）；
    `n_pushed < n_events` 就是**还有没推出去的**（未配 webhook 时必然如此）——
    页面必须如实显示，不得美化。

    降级：store 异常 ⇒ 计数全 0 + `note` 标注「读失败」，**不 5xx**。
    """
    from web.services import odds_move as odds_move_service

    try:
        return odds_move_service.ops(hours=hours)
    except Exception as exc:  # noqa: BLE001 - 运维视图降级，不 5xx
        logger.warning("jc_ops: odds_move ops failed: %s", exc)
        return {"window_hours": hours, "n_events": 0, "n_pushed": 0, "n_matches": 0,
                "max_move_pct": None, "avg_move_pct": None, "last_curr_ts": None,
                "threshold_pct": None, "push_configured": False,
                "note": "读失败（已降级为 0，不编数）"}


@router.get("/freshness")
def freshness(threshold_hours: float = 24.0, _: None = Depends(require_auth)) -> dict:
    """topic 新鲜度：复用 ops.topics 的 latest_arrival/min_since_latest，超阈值标记 stale。

    入参 threshold_hours 默认 24（按 plan item P0-infra「topic 超 24h 报警」）。
    返回 {ok, threshold_hours, stale, all}。store 异常 → 全部为空 + ok=False。
    """
    from store import jc_ops_view

    from web.services.freshness import evaluate_topics, format_alert_lines
    try:
        th = max(0.5, min(float(threshold_hours), 168.0))
    except (TypeError, ValueError):
        th = 24.0
    try:
        snap = jc_ops_view.ops_snapshot()
        topics = snap.get("topics") or []
    except Exception as exc:  # noqa: BLE001 - freshness 视图降级为空
        logger.warning("jc_ops: ops_snapshot failed: %s", exc)
        topics = []
    result = evaluate_topics(topics, threshold_hours=th)
    result["alerts"] = format_alert_lines(result)
    return result
