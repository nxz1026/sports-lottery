"""P1-4 验收测试：/health 真依赖探活 + 503 on failure。

修复点：旧 /health 只返回 {"status":"ok", ...}，DB 宕掉 / 预测卡 24h
仍返回 200；新版调用 web.services.health.check()，任一探活失败 → 503。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1].parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_app():
    """轻量构造 app 只用于 /health 探活；不导入 router。"""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from web import config
    from web.services import health

    app = FastAPI()

    @app.get("/health")
    def h():
        result = health.check()
        result["config"] = config.env_summary()
        return JSONResponse(
            status_code=200 if result["ok"] else 503, content=result
        )

    return app


def test_health_200_when_both_probes_ok(tmp_path):
    """sessions.db OK + predictions/ 24h 内有文件 → 200。"""
    import json
    import os

    from fastapi.testclient import TestClient

    fake_db = tmp_path / "sessions.db"
    predict_dir = tmp_path / "predictions"
    predict_dir.mkdir()
    fresh = predict_dir / "prediction_today.json"
    fresh.write_text(json.dumps({"predictions": []}), encoding="utf-8")
    # 改 mtime 到 1h 前
    os.utime(fresh, (time.time() - 3600, time.time() - 3600))

    with patch("web.config.SESSION_DB_PATH", str(fake_db)):
        with patch("web.services.store.OUTPUT_DIR", tmp_path):
                client = TestClient(_make_app())
                r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["checks"]["sessions_db"]["ok"] is True
    assert body["checks"]["predictions_fresh"]["ok"] is True
    assert body["checks"]["predictions_fresh"]["freshest_age_hours"] < 24


def test_health_503_when_predictions_stale(tmp_path):
    """最新 prediction 文件 mtime > 24h → 503（监控告警）。"""
    import json
    import os

    from fastapi.testclient import TestClient

    fake_db = tmp_path / "sessions.db"
    predict_dir = tmp_path / "predictions"
    predict_dir.mkdir()
    stale = predict_dir / "prediction_yesterday.json"
    stale.write_text(json.dumps({"predictions": []}), encoding="utf-8")
    os.utime(stale, (time.time() - 25 * 3600, time.time() - 25 * 3600))

    with patch("web.config.SESSION_DB_PATH", str(fake_db)):
        with patch("web.services.store.OUTPUT_DIR", tmp_path):
                client = TestClient(_make_app())
                r = client.get("/health")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["sessions_db"]["ok"] is True
    assert body["checks"]["predictions_fresh"]["ok"] is False
    assert body["checks"]["predictions_fresh"]["freshest_age_hours"] >= 24


def test_health_200_when_no_predictions_yet(tmp_path):
    """首次启动 predictions/ 为空 → 200（不阻塞启动）。"""
    from fastapi.testclient import TestClient

    fake_db = tmp_path / "sessions.db"
    (tmp_path / "predictions").mkdir()  # 留空

    with patch("web.config.SESSION_DB_PATH", str(fake_db)):
        with patch("web.services.store.OUTPUT_DIR", tmp_path):
            client = TestClient(_make_app())
            r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["checks"]["predictions_fresh"]["note"]


def test_health_503_when_sessions_db_unreachable(tmp_path):
    """sessions.db 路径不可写（read-only 父目录） → 503。"""
    from fastapi.testclient import TestClient

    # Linux/macOS 可 chmod；Windows 用占位不存在的盘符路径
    if hasattr(os := __import__("os"), "name") and os.name != "nt":
        read_only = tmp_path / "ro"
        read_only.mkdir()
        os.chmod(read_only, 0o555)
        bad_db = read_only / "sub" / "sessions.db"  # mkdir 会抛 PermissionError
    else:
        bad_db = "Z:\\nonexistent\\sessions.db"

    with patch("web.config.SESSION_DB_PATH", str(bad_db)):
        with patch("web.services.store.OUTPUT_DIR", tmp_path / "no-db-predictions"):
            r = TestClient(_make_app()).get("/health")
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["checks"]["sessions_db"]["ok"] is False


def test_unit_probe_sessions_db_unhappy_path(tmp_path):
    """_probe_sessions_db 在 sqlite 抛错时返回 ok=False + error 字段。"""
    import sqlite3

    from web.services import health

    with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk full")):
        r = health._probe_sessions_db(tmp_path / "x.db")
    assert r["ok"] is False
    assert "disk full" in r["error"]


def test_unit_probe_predictions_handles_missing_dir(tmp_path):
    """_probe_predictions_fresh 在目录不存在时返回 ok=True + note（首次启动）。"""
    from web.services import health

    r = health._probe_predictions_fresh(tmp_path / "nope")
    assert r["ok"] is True
    assert r["note"]
