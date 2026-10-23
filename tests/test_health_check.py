"""R46-5: web/services/health.py::check 4 cases direct + D-72 lock.

check (L74-L90, 17 行) 是 /health 顶层 orchestrator, 汇总 sessions.db +
predictions_fresh 两个探活项, 任一失败 -> overall ok=False (路由层据此 503)。

R44 P1-4 修复时 test_health.py 加了 4 个端到端测 (FastAPI TestClient 走 200/503),
但 check() 自身核心契约 (overall_ok 聚合 + checks 结构 + time_utc_epoch) 0 直测。

D-72 锁定 4 契约:
- D-72-1: happy - 2 probe 都 ok -> overall ok=True + checks 含两项明细
- D-72-2: sessions 失败 -> overall ok=False (任一失败即降级)
- D-72-3: predictions 失败 -> overall ok=False
- D-72-4: time_utc_epoch 是 float + 接近当前时间

依赖 stub:
- web.services.health._probe_sessions_db (monkeypatch 返受控 dict)
- web.services.health._probe_predictions_fresh (monkeypatch 返受控 dict)
- web.config.SESSION_DB_PATH + web.services.store.OUTPUT_DIR (用任意值, 不会被真读)
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def health_mod():
    """Spec-load web/services/health.py (复用 R46-3 套路)。"""
    spec = importlib.util.spec_from_file_location(
        "health_check_test",
        REPO_ROOT / "web" / "services" / "health.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_probes(health_mod, monkeypatch, sessions: dict, predictions: dict):
    """Mock _probe_sessions_db + _probe_predictions_fresh 返受控 dict。"""
    monkeypatch.setattr(health_mod, "_probe_sessions_db",
                        lambda path: sessions)
    monkeypatch.setattr(health_mod, "_probe_predictions_fresh",
                        lambda path, max_age_hours=24: predictions)


# ──────────────────────── D-72 四契约 ────────────────────────


def test_check_d72_1_happy_both_probes_ok(health_mod, monkeypatch):
    """R46-5 D-72-1: happy - 2 probe 都 ok -> overall ok=True。

    sessions {ok: True, path: ...} + predictions {ok: True, ...} -> overall ok=True。
    """
    _stub_probes(health_mod, monkeypatch,
        sessions={"ok": True, "path": "/fake/sessions.db"},
        predictions={"ok": True, "freshest_age_hours": 1.0, "max_age_hours": 24},
    )
    out = health_mod.check()
    assert out["ok"] is True
    assert out["checks"]["sessions_db"] == {"ok": True, "path": "/fake/sessions.db"}
    assert out["checks"]["predictions_fresh"]["ok"] is True
    assert out["checks"]["predictions_fresh"]["freshest_age_hours"] == 1.0
    assert "time_utc_epoch" in out


def test_check_d72_2_sessions_failure_returns_false(health_mod, monkeypatch):
    """R46-5 D-72-2: sessions 失败 -> overall ok=False (任一失败即降级)。

    L82 `overall_ok = sessions_check["ok"] and predictions_check["ok"]` 短路求值:
    sessions 失败 -> overall False, predictions 即使 ok 也无济于事。
    """
    _stub_probes(health_mod, monkeypatch,
        sessions={"ok": False, "error": "disk full"},
        predictions={"ok": True, "freshest_age_hours": 1.0},
    )
    out = health_mod.check()
    assert out["ok"] is False
    assert out["checks"]["sessions_db"]["ok"] is False
    assert out["checks"]["sessions_db"]["error"] == "disk full"
    # predictions 即使 ok 也不影响 overall
    assert out["checks"]["predictions_fresh"]["ok"] is True


def test_check_d72_3_predictions_failure_returns_false(health_mod, monkeypatch):
    """R46-5 D-72-3: predictions 失败 -> overall ok=False。

    sessions ok=True, predictions ok=False (引擎卡 24h) -> overall False。
    """
    _stub_probes(health_mod, monkeypatch,
        sessions={"ok": True, "path": "/fake/sessions.db"},
        predictions={"ok": False, "freshest_age_hours": 25.0, "max_age_hours": 24},
    )
    out = health_mod.check()
    assert out["ok"] is False
    assert out["checks"]["sessions_db"]["ok"] is True
    assert out["checks"]["predictions_fresh"]["ok"] is False


def test_check_d72_4_time_utc_epoch_is_recent(health_mod, monkeypatch):
    """R46-5 D-72-4: time_utc_epoch 是 float + 接近当前时间。

    L85 `time_utc_epoch: time.time()` 应是 float, 误差 < 5s。
    """
    _stub_probes(health_mod, monkeypatch,
        sessions={"ok": True, "path": "/fake"},
        predictions={"ok": True, "freshest_age_hours": 1.0},
    )
    before = time.time()
    out = health_mod.check()
    after = time.time()
    assert isinstance(out["time_utc_epoch"], float)
    assert before <= out["time_utc_epoch"] <= after
    # 整 response 顶层结构: ok + time_utc_epoch + checks
    # ⚠️ 2026-10-23：**加了一个** ``code_sha``（运行中代码的 git 短 sha）。
    #    为什么放**顶层**而不塞进 ``checks``：``checks`` 里每一项都是探活结果
    #    （都有 "ok" 键），监控常写 ``for name, c in checks.items(): c["ok"]``
    #    ⇒ 塞进去会让那种遍历炸掉 ⇒ 顶层新增字段是**向后兼容的加法**
    #    （JSON 消费者忽略未知字段），这才安全。
    assert set(out.keys()) == {"ok", "time_utc_epoch", "checks", "code_sha"}
    assert set(out["checks"].keys()) == {"sessions_db", "predictions_fresh"}
    # code_sha：字符串（短 sha）或 None（读不到 git 时不许编数）
    assert out["code_sha"] is None or isinstance(out["code_sha"], str)
    if out["code_sha"] is not None:
        assert len(out["code_sha"]) >= 7, "应是 git 短 sha（至少 7 位）"
