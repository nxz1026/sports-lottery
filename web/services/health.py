"""web.services.health — /health 依赖探活（轻量、可观测、不阻塞）。

P1-4 修复：/health 必须探真依赖，避免 DB 宕掉 / 预测卡 24h 时仍返回 200。
探活项：
- sessions.db SQLite 可读可写（SELECT 1）
- 最新 prediction 文件 mtime < 24h（首次启动无文件不阻塞）

探活是「只 connect + 一行 SELECT + 一次 stat」级别，单次 /health 请求
< 50ms；任何探活失败 → 503，body 携带失败明细便于监控告警。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("web.health")

# 预测"新鲜"窗口：超过此窗口认为引擎卡住（默认 24h）。
PREDICTION_FRESH_HOURS = 24


def _probe_sessions_db(sessions_db_path: str | Path) -> dict:
    """轻量 SQLite 探活：connect + SELECT 1；任何失败返回 dict 含 error。"""
    try:
        path = Path(sessions_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=2)
        try:
            row = conn.execute("SELECT 1 AS ok").fetchone()
            # 默认 sqlite3.Row 不开启；用 fetchone()[0] 取首列（已 AS ok）
            ok = row is not None and row[0] == 1
            return {"ok": ok, "path": str(path)}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 探活容错
        logger.warning("health: sessions.db probe failed: %s", exc)
        return {"ok": False, "path": str(sessions_db_path), "error": str(exc)}


def _probe_predictions_fresh(
    predict_dir: str | Path, max_age_hours: int = PREDICTION_FRESH_HOURS
) -> dict:
    """最新 prediction 文件 mtime < max_age_hours；无文件返回 ok=True（首次启动免阻塞）。"""
    try:
        d = Path(predict_dir)
        if not d.is_dir():
            return {
                "ok": True,
                "note": "predictions/ not yet provisioned",
                "freshest_age_hours": None,
            }
        files = [p for p in d.iterdir() if p.is_file()]
        if not files:
            return {
                "ok": True,
                "note": "no prediction files yet",
                "freshest_age_hours": None,
            }
        freshest = max(files, key=lambda p: p.stat().st_mtime)
        age_hours = (time.time() - freshest.stat().st_mtime) / 3600.0
        return {
            "ok": age_hours < max_age_hours,
            "freshest_file": freshest.name,
            "freshest_age_hours": round(age_hours, 2),
            "max_age_hours": max_age_hours,
        }
    except Exception as exc:  # noqa: BLE001 - 探活容错
        logger.warning("health: predictions probe failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def check() -> dict:
    """汇总所有探活项；任何一项 ok=False → 顶层 ok=False（路由层据此 503）。"""
    from web import config
    from web.services import store as _store

    sessions_check = _probe_sessions_db(config.SESSION_DB_PATH)
    predictions_check = _probe_predictions_fresh(_store.OUTPUT_DIR / "predictions")

    overall_ok = sessions_check["ok"] and predictions_check["ok"]
    return {
        "ok": overall_ok,
        "time_utc_epoch": time.time(),
        "checks": {
            "sessions_db": sessions_check,
            "predictions_fresh": predictions_check,
        },
    }