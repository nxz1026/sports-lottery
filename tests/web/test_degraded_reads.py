"""P1（2026-10-23）：「读失败」与「本来就没有」必须**分开表达**。

事故：数据库读失败时，页面把「连不上库」印成
- 今日推荐单 →「你还没跑脚本」（**会让人白跑一趟**的错误原因）
- 推荐单战绩 →「已结算 0 注」
- CLV        →「暂无 CLV 数据…成对样本不存在」（**误导性的原因**）
- 赔率异动    →「最近 24 小时无异动事件」

旧实现四种情况都返回空列表 / 全 0 ⇒ 页面在说谎。
修法（照抄仓库现成范式 `recommend.offer_freshness()` 的 `degraded` 标志）：
后端在读失败时返回 `degraded=True`，前端据此印「⚠️ 读取失败」。

守门纪律：**「没给数」必须说清楚为什么**，不许静默变成「没有」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.services.clv as wsc  # noqa: E402
import web.services.odds_move as wso  # noqa: E402
import web.services.recommend as wsr  # noqa: E402


def _boom(*a, **k):
    raise RuntimeError("db connection lost")


# ── recommend：今日推荐单 ──────────────────────────────────────────────────


def test_today_picks_status_degraded_on_read_failure(monkeypatch):
    """读失败 ⇒ degraded=True（不是「今天没有荐单」）。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsr.today_picks_status()
    assert out["degraded"] is True
    assert out["picks"] == []


def test_today_picks_status_not_degraded_when_empty(monkeypatch):
    """真的没有荐单 ⇒ degraded=False（**不**要说成读失败）。"""

    class _Ctx2:
        def __enter__(self):
            class _C:
                def execute(self, sql, params=None):
                    return self

                def fetchall(self):
                    return []
            return _C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx2())
    out = wsr.today_picks_status()
    assert out["degraded"] is False
    assert out["picks"] == []


def test_record_degraded_on_read_failure(monkeypatch):
    """战绩读失败 ⇒ degraded=True（不是「已结算 0 注」）。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsr.record()
    assert out["degraded"] is True
    assert out["n_settled"] == 0


def test_record_not_degraded_on_empty_but_readable(monkeypatch):
    """读得到但一注都没结算 ⇒ degraded=False。"""
    class _Ctx2:
        def __enter__(self):
            class _C:
                def __init__(self):
                    self._rows = [0, 0, 0.0, 0.0]

                def execute(self, sql, params=None):
                    return self

                def fetchone(self):
                    r = self._rows
                    self._rows = None
                    return r

                def fetchall(self):
                    return []
            return _C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx2())
    out = wsr.record()
    assert out["degraded"] is False
    assert out["n_settled"] == 0


def test_snapshot_carries_picks_degraded(monkeypatch):
    """snapshot() 必须把 picks_degraded / record.degraded 透出（页面据此区分）。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsr.snapshot()
    assert out["picks_degraded"] is True
    assert out["record"]["degraded"] is True


# ── clv ────────────────────────────────────────────────────────────────────


def test_clv_daily_rows_status_degraded_on_read_failure(monkeypatch):
    """CLV 读失败 ⇒ degraded=True（不是「没有成对样本」）。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsc.daily_rows_status()
    assert out["degraded"] is True
    assert out["rows"] == []


def test_clv_summary_carries_degraded_flag():
    """summary() 必须把 degraded 标志带出去（否则路由层一改就丢）。"""
    out = wsc.summary([], degraded=True)
    assert out["degraded"] is True
    out2 = wsc.summary([], degraded=False)
    assert out2["degraded"] is False


def test_clv_summary_reads_rows_itself_and_propagates_degraded(monkeypatch):
    """summary() 不传 rows 时自己读 ⇒ 也必须带出 degraded。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wsc.summary()
    assert out["degraded"] is True


# ── odds_move ──────────────────────────────────────────────────────────────


def test_odds_move_recent_status_degraded_on_read_failure(monkeypatch):
    """异动事件读失败 ⇒ degraded=True（不是「24 小时无异动」）。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wso.recent_status()
    assert out["degraded"] is True
    assert out["rows"] == []


def test_odds_move_ops_degraded_on_read_failure(monkeypatch):
    """运维视图读失败 ⇒ degraded=True。"""
    monkeypatch.setattr("store.pg.read_conn", _boom)
    out = wso.ops()
    assert out["degraded"] is True


def test_odds_move_ops_not_degraded_on_success(monkeypatch):
    """读得到 ⇒ degraded=False。"""
    class _Ctx2:
        def __enter__(self):
            class _C:
                def execute(self, sql, params=None):
                    return self

                def fetchone(self):
                    return [0, 0, 0, None, None, None, 3.0]
            return _C()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("store.pg.read_conn", lambda *a, **k: _Ctx2())
    out = wso.ops()
    assert out["degraded"] is False
