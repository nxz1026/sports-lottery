"""``core.observability.events_summary`` + ``events_jsonl_size`` 守门测试 —— R15b。

覆盖：
- ``events_jsonl_size`` 单文件 + rotation 备份汇总
- ``events_summary`` 聚合 by_kind / by_league / 时间范围 / 空文件 / 缺字段
- admin 端点 ``/api/v1/admin/events/summary`` require_auth 守门 + 参数生效
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core.observability as obs  # noqa: E402


@pytest.fixture(autouse=True)
def reset_obs(tmp_path):
    """隔离 obs 到 tmp_path，避免污染正式 events.jsonl。"""
    obs.reset_for_test()
    obs.setup(dir=tmp_path / "logs")
    yield
    obs.reset_for_test()


# ── events_jsonl_size ──────────────────────────────────────

class TestEventsJsonlSize:
    def test_no_path_returns_zero(self):
        obs.reset_for_test()  # 卸载 events.jsonl
        assert obs.events_jsonl_size() == 0

    def test_empty_file_returns_zero(self, tmp_path):
        events_path = obs.events_jsonl_path()
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.touch()
        assert obs.events_jsonl_size() == 0

    def test_single_file_size(self):
        obs.log_event("prediction", league="epl", match_id="m1")
        obs.log_event("prediction", league="laliga", match_id="m2")
        size = obs.events_jsonl_size()
        assert size > 0
        assert size < 1000

    def test_includes_rotation_backups(self, tmp_path):
        """rotation 备份 ``events.jsonl.{N}.bak`` 也算进 size（运维总占用）。"""
        obs.log_event("prediction", league="epl", match_id="m1")
        main_size = obs.events_jsonl_size()
        events_path = obs.events_jsonl_path()
        bak = events_path.with_name(f"{events_path.name}.1.bak")
        bak.write_bytes(b'{"kind":"backup_test","x":"' + b"z" * 1000 + b'"}\n')
        try:
            new_size = obs.events_jsonl_size()
            assert new_size > main_size
            assert new_size >= main_size + 1000
        finally:
            bak.unlink()


# ── events_summary ──────────────────────────────────────────

class TestEventsSummary:
    def test_empty_path(self):
        """events.jsonl 不存在 → 全零 + None。"""
        obs.reset_for_test()
        s = obs.events_summary()
        assert s == {
            "total": 0,
            "size_bytes": 0,
            "kinds": {},
            "leagues": {},
            "first_ts": None,
            "last_ts": None,
        }

    def test_no_events_returns_empty(self):
        events_path = obs.events_jsonl_path()
        events_path.touch()
        s = obs.events_summary()
        assert s["total"] == 0
        assert s["kinds"] == {}
        assert s["first_ts"] is None
        assert s["last_ts"] is None

    def test_basic_aggregation(self):
        obs.log_event("batch_start", mode="all", n_leagues=5)
        obs.log_event("prediction", league="epl", match_id="m1")
        obs.log_event("prediction", league="epl", match_id="m2")
        obs.log_event("prediction", league="laliga", match_id="m3")
        obs.log_event("outcome", league="epl", match_id="m1", result="home_win")
        obs.log_event("error", league="", error_kind="boom")

        s = obs.events_summary()
        assert s["total"] == 6
        assert s["kinds"]["batch_start"] == 1
        assert s["kinds"]["prediction"] == 3
        assert s["kinds"]["outcome"] == 1
        assert s["kinds"]["error"] == 1
        assert s["leagues"]["epl"] == 3
        assert s["leagues"]["laliga"] == 1
        assert s["leagues"]["<empty>"] == 2
        assert s["first_ts"] is not None
        assert s["last_ts"] is not None
        assert s["first_ts"] <= s["last_ts"]

    def test_disabled_by_kind(self):
        obs.log_event("prediction", league="epl", match_id="m1")
        s = obs.events_summary(by_kind=False)
        assert s["kinds"] is None
        assert s["leagues"] is not None

    def test_disabled_by_league(self):
        obs.log_event("prediction", league="epl", match_id="m1")
        s = obs.events_summary(by_league=False)
        assert s["leagues"] is None
        assert s["kinds"] is not None

    def test_size_bytes_includes_main_and_rotation(self):
        obs.log_event("prediction", league="epl", match_id="m1")
        s = obs.events_summary()
        assert s["size_bytes"] == obs.events_jsonl_size()

    def test_league_missing_or_non_string(self):
        obs.log_event("prediction", league=None, match_id="m1")
        obs.log_event("prediction", match_id="m2")
        obs.log_event("prediction", league=42, match_id="m3")
        s = obs.events_summary()
        assert s["leagues"]["<empty>"] == 3

    def test_custom_path(self, tmp_path):
        custom = tmp_path / "custom_events.jsonl"
        with open(custom, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-14T00:00:00+00:00",
                                "kind": "test_kind", "league": "test_lg"}) + "\n")
            f.write(json.dumps({"ts": "2026-10-14T00:00:01+00:00",
                                "kind": "test_kind", "league": "test_lg"}) + "\n")
        s = obs.events_summary(path=custom)
        assert s["total"] == 2
        assert s["kinds"]["test_kind"] == 2
        assert s["leagues"]["test_lg"] == 2

    def test_invalid_json_lines_skipped(self, tmp_path):
        custom = tmp_path / "invalid_events.jsonl"
        with open(custom, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-10-14T00:00:00+00:00",
                                "kind": "valid"}) + "\n")
            f.write("NOT JSON\n")
            f.write(json.dumps({"ts": "2026-10-14T00:00:01+00:00",
                                "kind": "valid"}) + "\n")
        s = obs.events_summary(path=custom)
        assert s["total"] == 2


# ── admin 端点 ─────────────────────────────────────────────

class TestAdminEventsSummaryEndpoint:
    """``/api/v1/admin/events/summary`` 端点 require_auth 守门 + 参数生效。

    用 env var 设 AUTH_USERNAME/PASSWORD + 真实 ``/api/v1/login`` 拿 session
    cookie —— 与 ``tests/web/test_job_trigger_source.py`` 同一模式（避免
    monkeypatch require_auth 时 FastAPI 缓存依赖导致的 401 漏改）。
    """

    @pytest.fixture()
    def client(self, monkeypatch):
        import importlib
        from fastapi.testclient import TestClient
        # 隔离 env，让 tests 不依赖生产 session.db
        import tempfile
        tmp = tempfile.mkdtemp()
        monkeypatch.setenv("AUTH_USERNAME", "unit-test-user")
        monkeypatch.setenv("AUTH_PASSWORD", "unit-test-pass")
        monkeypatch.setenv("SESSION_DB_PATH", str(Path(tmp) / "sessions.db"))
        # reload config 让新 env 生效
        import web.config as config
        importlib.reload(config)
        import web.session_store as session_store
        importlib.reload(session_store)
        import web.auth as auth_mod
        importlib.reload(auth_mod)
        # reload admin router（让它重新 import 上述 reload 后的模块）
        from web.routers import admin as admin_router_mod
        importlib.reload(admin_router_mod)
        # 把 reload 后的 router 替换到 web.api 的引用
        import web.api as api_mod
        api_mod.admin_router = admin_router_mod.router
        from web.api import create_app
        app = create_app()
        return TestClient(app)

    def _login(self, client) -> None:
        res = client.post("/api/v1/login", json={
            "username": "unit-test-user", "password": "unit-test-pass"})
        assert res.status_code == 200, res.text

    def test_endpoint_requires_auth(self, client):
        """未鉴权 token → 401（require_auth 守门）。"""
        r = client.get("/api/v1/admin/events/summary")
        assert r.status_code == 401

    def test_endpoint_returns_summary(self, client):
        """鉴权通过时返回 events_summary dict。"""
        obs.log_event("prediction", league="epl", match_id="m1")
        self._login(client)
        r = client.get("/api/v1/admin/events/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert "size_bytes" in body
        assert "kinds" in body
        assert "leagues" in body
        assert body["kinds"]["prediction"] >= 1
        assert body["leagues"]["epl"] >= 1

    def test_endpoint_by_kind_false(self, client):
        """?by_kind=false 时 kinds 字段为 None。"""
        self._login(client)
        r = client.get("/api/v1/admin/events/summary?by_kind=false")
        assert r.status_code == 200
        assert r.json()["kinds"] is None

    def test_endpoint_by_league_false(self, client):
        """?by_league=false 时 leagues 字段为 None。"""
        self._login(client)
        r = client.get("/api/v1/admin/events/summary?by_league=false")
        assert r.status_code == 200
        assert r.json()["leagues"] is None