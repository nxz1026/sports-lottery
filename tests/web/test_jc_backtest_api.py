"""Authenticated JC backtest API contract and safe v1 alias tests."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
    monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
    import web.config as config
    import web.session_store as session_store
    import web.auth as auth
    importlib.reload(config)
    importlib.reload(session_store)
    importlib.reload(auth)
    from web.api import create_app
    return TestClient(create_app())


def _login(client):
    response = client.post("/api/v1/login", json={
        "username": "unit-test-user", "password": "unit-test-pass",
    })
    assert response.status_code == 200


def test_backtest_endpoints_require_auth(client):
    # 2026-09-18：后端信任 Nginx Basic Auth，require_auth 放行 → 不再 401。
    for path in ("/api/jc/backtest", "/api/v1/backtest"):
        response = client.get(path)
        assert response.status_code != 401, f"{path} 仍被应用层 401 拒绝（应放行）"


def test_v1_alias_exposes_existing_metrics_without_fabricating_bins(client, monkeypatch):
    from store import jc_view
    rows = [{
        "play_type": "had", "n_fp": 2, "brier": 0.627,
        "brier_uniform": 0.667, "log_loss": 1.0444,
        "acc": 0.4772, "hit_rate": 0.4772, "clv_filled": 0,
    }]
    monkeypatch.setattr(jc_view, "backtest_summary", lambda: rows)
    _login(client)
    for path in ("/api/jc/backtest", "/api/v1/backtest"):
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == rows
        assert body["degraded"] is False
        assert "bins" not in body and "calibration" not in body
