"""web.services.recommend 的新鲜度展示层契约（§6.13.5）。

要点：
- **唯一真源在 scripts 层**（`decision.recommend.offer_freshness`）⇒ web 只做延迟 import 委托，
  不复制一份 SQL —— 否则会出现「页面说新鲜、生成端说过期」的两套口径。
- 读失败 ⇒ `degraded=True`、字段为 None，**不编数、不 5xx**。
- `snapshot()` 必须带 `offer_freshness`（看板要显式显示「定价赔率快照 X 小时前」）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import decision.recommend as rec  # noqa: E402

import web.services.recommend as wsr  # noqa: E402


class _Ctx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


def test_offer_freshness_delegates_to_scripts_single_source(monkeypatch):
    """web 层必须调 scripts 层的同一个函数（阈值也用同一个默认值）。"""
    seen: dict = {}

    def _fake(conn, th):
        seen["th"] = th
        return {"latest_snap": "2026-10-22 14:48:07+00", "age_hours": 0.54,
                "stale": False, "threshold_hours": th,
                "arrival_ts": "2026-10-22 14:50:05+00", "arrival_age_hours": 0.47,
                "arrival_stale": False}

    monkeypatch.setattr(rec, "offer_freshness", _fake)
    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx())

    out = wsr.offer_freshness()
    assert out["age_hours"] == 0.54
    assert out["stale"] is False
    assert seen["th"] == rec.DEFAULT_MAX_OFFER_AGE_HOURS


def test_offer_freshness_degrades_on_db_error(monkeypatch):
    """DB 读失败 ⇒ degraded=True + 字段为 None（不编数、不抛）。"""

    def _boom(*a, **k):
        raise RuntimeError("db connection lost")

    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsr.offer_freshness()
    assert out["degraded"] is True
    assert out["age_hours"] is None
    assert out["latest_snap"] is None
    assert out["stale"] is None


def test_offer_freshness_accepts_custom_threshold(monkeypatch):
    """显式传阈值 ⇒ 传给 scripts 层（页面与守门可用不同阈值但同一真源）。"""
    seen: dict = {}

    def _fake(conn, th):
        seen["th"] = th
        return {"threshold_hours": th, "stale": False, "age_hours": 1.0}

    monkeypatch.setattr(rec, "offer_freshness", _fake)
    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx())
    out = wsr.offer_freshness(24.0)
    assert seen["th"] == 24.0
    assert out["threshold_hours"] == 24.0


def test_snapshot_includes_offer_freshness(monkeypatch):
    """snapshot() 必须带 offer_freshness（看板要显示新鲜度）。"""
    # P1（2026-10-23）：snapshot() 改用 today_picks_status()（带 degraded 标志）⇒
    # 这里也要跟着换替身，否则会真的去连库。
    monkeypatch.setattr(wsr, "today_picks_status",
                        lambda day=None: {"picks": [], "degraded": False})
    monkeypatch.setattr(wsr, "record", lambda min_n=5: {"n_settled": 0, "degraded": False})
    monkeypatch.setattr(wsr, "offer_freshness", lambda *a, **k: {"stale": False,
                                                               "age_hours": 0.5})
    out = wsr.snapshot()
    assert "offer_freshness" in out
    assert out["n_picks"] == 0
    assert out["picks_degraded"] is False
    assert out["offer_freshness"]["age_hours"] == 0.5


def test_snapshot_calendar_note_says_manual_not_timer(monkeypatch):
    """calendar_note 不得再说「09:30 timer 生成」（荐单根本不在那个管道里）。"""
    monkeypatch.setattr(wsr, "today_picks_status",
                        lambda day=None: {"picks": [], "degraded": False})
    monkeypatch.setattr(wsr, "record", lambda min_n=5: {"n_settled": 0, "degraded": False})
    monkeypatch.setattr(wsr, "offer_freshness", lambda *a, **k: {})
    note = wsr.snapshot()["calendar_note"]
    assert "09:30" not in note
    assert "手动" in note
