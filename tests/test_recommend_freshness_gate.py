"""定价赔率新鲜度守门（recommend.py）的契约单测。

背景（2026-10-22 采集端改手动模式）：
采集端从「计划任务自动采」改成「手动跑」之后，「人忘了先采集就直接出荐单」
成了新的**静默失败**路径 —— 库里最新 SP 可能还是几天前的，照样算得出 EV，
页面看上去完全正常（不报错、不空态、ROI 照样有数字）。

这道闸分两层：
  ① **全局闸**：全库 had 最新快照超龄 ⇒ 拒绝出单（退出码 3）；
  ② **逐场闸**：某一场自己的 had 快照超龄 ⇒ 跳过该场（不用旧价算 EV）。

⚠️ 口径依赖：fact.jc_offer 主键含 snap_ts ⇒ **每次采集都新插一行**，
所以 snap_ts 能当「采集时刻」用（不存在「赔率没动就不更新」导致龄期虚高）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision import recommend as rec  # noqa: E402


def _hours_ago(h: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=h)


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    """按 SQL 关键字分发的假连接。

    - fresh_snap / arrival：新鲜度两个口径的 max() 返回值
    - sale_rows：_SALE_SQL 返回的行（tuple 形态，和生产 psycopg3 tuple_row 一致）
    - archive_rows：模型归档行
    """

    def __init__(self, *, fresh_snap=None, arrival=None, sale_rows=None, archive_rows=None):
        self._fresh_snap = fresh_snap
        self._arrival = arrival
        self._sale_rows = sale_rows or []
        self._archive_rows = archive_rows or []
        self.written = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if "max(snap_ts)" in text and "fact.jc_offer" in text:
            return _Cur([(self._fresh_snap,)])
        if "max(arrived_at)" in text and "ops.file_arrival" in text:
            return _Cur([(self._arrival,)])
        if "fact.jc_match m" in text and "business_date" in text:
            return _Cur(self._sale_rows)
        if "model.pred_archive_match am" in text:
            return _Cur(self._archive_rows)
        return _Cur([])


# ── A. _age_hours 的边界 ──────────────────────────────────────────────

def test_age_hours_none_is_none():
    """缺失时间戳 ⇒ None（调用方按最坏情况处理，不当 0 小时）。"""
    assert rec._age_hours(None) is None


def test_age_hours_unparsable_string_is_none():
    """不可解析的字符串 ⇒ None（不是 0，也不是抛异常）。"""
    assert rec._age_hours("not-a-time") is None
    assert rec._age_hours("") is None


def test_age_hours_naive_datetime_treated_as_utc():
    """naive datetime ⇒ 按 UTC 解释（不按本地时区，避免口径漂移）。"""
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    age = rec._age_hours(naive)
    assert age is not None
    assert 1.9 < age < 2.1


def test_age_hours_iso_string_parsed():
    """ISO 字符串 ⇒ 正常算龄。"""
    ts = (_hours_ago(3)).isoformat()
    age = rec._age_hours(ts)
    assert age is not None
    assert 2.9 < age < 3.1


def test_age_hours_rejects_non_datetime():
    assert rec._age_hours(12345) is None


# ── B. offer_freshness 的两种口径 ─────────────────────────────────────

def test_offer_freshness_no_rows_is_stale():
    """一条快照都没有 ⇒ stale=True（宁可拦，不可拿旧价出单）。"""
    conn = _Conn(fresh_snap=None, arrival=None)
    out = rec.offer_freshness(conn, 6.0)
    assert out["stale"] is True
    assert out["age_hours"] is None
    assert out["arrival_stale"] is True
    assert out["threshold_hours"] == 6.0


def test_offer_freshness_fresh_is_ok():
    """1 小时内的快照 ⇒ 不算 stale。"""
    conn = _Conn(fresh_snap=_hours_ago(1), arrival=_hours_ago(0.5))
    out = rec.offer_freshness(conn, 6.0)
    assert out["stale"] is False
    assert out["arrival_stale"] is False
    assert 0.9 < out["age_hours"] < 1.1
    assert out["latest_snap"]


def test_offer_freshness_old_is_stale():
    """30 小时前的快照 ⇒ stale=True。"""
    conn = _Conn(fresh_snap=_hours_ago(30), arrival=_hours_ago(29))
    out = rec.offer_freshness(conn, 6.0)
    assert out["stale"] is True
    assert out["arrival_stale"] is True
    assert 29.9 < out["age_hours"] < 30.1


def test_offer_freshness_threshold_is_respected():
    """阈值是参数：10 小时前的快照在 6h 阈值下 stale，在 24h 阈值下不 stale。"""
    conn = _Conn(fresh_snap=_hours_ago(10), arrival=_hours_ago(10))
    assert rec.offer_freshness(conn, 6.0)["stale"] is True
    assert rec.offer_freshness(conn, 24.0)["stale"] is False


# ── C. 逐场闸 ─────────────────────────────────────────────────────────

def _sale(match_id, snap, sp=None):
    return (match_id, "2026-10-22", "甲队", "乙队", "西甲",
            sp or {"h": "3.00", "d": "3.70", "a": "3.12"}, snap)


def _no_predfile(monkeypatch):
    monkeypatch.setattr(rec, "latest_pred_rows", lambda directory=None: {})


def test_stale_match_is_skipped(monkeypatch):
    """超龄场次被跳过（不用旧价算 EV），且计入 stale_skipped。"""
    _no_predfile(monkeypatch)
    conn = _Conn(
        sale_rows=[
            _sale(2042111, _hours_ago(30)),     # 旧价 ⇒ 跳过
            _sale(2042112, _hours_ago(0.2)),    # 新价 ⇒ 保留
        ],
        archive_rows=[
            (2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False, "2026-10-22", "laliga",
             "2026-10-22 01:30:02+00"),
            (2042112, 0.55, 0.25, 0.20, "3-star", 0.72, False, "2026-10-22", "laliga",
             "2026-10-22 01:30:02+00"),
        ],
    )
    cands, stats = rec.build_candidates(conn, backfill=False, max_offer_age_hours=6.0)
    assert stats["stale_skipped"] == 1
    assert stats["sale"] == 2
    assert [c.match_id for c in cands] == [2042112]


def test_allow_stale_keeps_stale_match(monkeypatch):
    """--allow-stale ⇒ 超龄场次也保留（放行是显式决定）。"""
    _no_predfile(monkeypatch)
    conn = _Conn(
        sale_rows=[_sale(2042111, _hours_ago(30))],
        archive_rows=[(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                       "2026-10-22", "laliga", "2026-10-22 01:30:02+00")],
    )
    cands, stats = rec.build_candidates(conn, backfill=False, max_offer_age_hours=6.0,
                                         allow_stale=True)
    assert stats["stale_skipped"] == 0
    assert [c.match_id for c in cands] == [2042111]


def test_no_limit_keeps_stale_match(monkeypatch):
    """max_offer_age_hours=None ⇒ 不限龄（回溯模式 / 兜底调用）。"""
    _no_predfile(monkeypatch)
    conn = _Conn(
        sale_rows=[_sale(2042111, _hours_ago(300))],
        archive_rows=[(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                       "2026-10-22", "laliga", "2026-10-22 01:30:02+00")],
    )
    cands, stats = rec.build_candidates(conn, backfill=False, max_offer_age_hours=None)
    assert stats["stale_skipped"] == 0
    assert len(cands) == 1


def test_stats_report_max_age(monkeypatch):
    """stats 报告保留下来的最老快照龄（页面/日志可读）。"""
    _no_predfile(monkeypatch)
    conn = _Conn(
        sale_rows=[_sale(2042111, _hours_ago(2)), _sale(2042112, _hours_ago(0.1))],
        archive_rows=[],
    )
    _c, stats = rec.build_candidates(conn, backfill=False, max_offer_age_hours=6.0)
    assert stats["offer_age_hours"] is not None
    assert 1.9 < stats["offer_age_hours"] < 2.1


# ── D. 全局闸：main() 必须拒绝出单 ────────────────────────────────────

class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def test_main_refuses_when_offer_stale(monkeypatch):
    """快照超龄 ⇒ 退出码 3，且**不写库**。"""
    conn = _Conn(fresh_snap=_hours_ago(30), arrival=_hours_ago(29))
    monkeypatch.setattr(rec.pg, "write_conn", lambda *a, **k: _Ctx(conn))

    def _boom(*a, **k):
        raise AssertionError("超龄时不得写库")

    monkeypatch.setattr(rec, "write_reports", _boom)
    rc = rec.main([])
    assert rc == rec.STALE_EXIT_CODE
    assert rec.STALE_EXIT_CODE == 3


def test_main_allow_stale_passes_gate(monkeypatch):
    """--allow-stale ⇒ 放行（但 note 里要留痕）。"""
    conn = _Conn(
        fresh_snap=_hours_ago(30), arrival=_hours_ago(29),
        sale_rows=[_sale(2042111, _hours_ago(30))],
        archive_rows=[(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                       "2026-10-22", "laliga", "2026-10-22 01:30:02+00")],
    )
    monkeypatch.setattr(rec.pg, "write_conn", lambda *a, **k: _Ctx(conn))
    _no_predfile(monkeypatch)

    seen: dict = {}

    def _capture(conn_, picks):
        seen["picks"] = picks
        return []

    monkeypatch.setattr(rec, "write_reports", _capture)
    rc = rec.main(["--allow-stale"])
    assert rc == 0
    assert seen["picks"], "放行后应产出推荐单"
    assert seen["picks"][0].get("stale_note"), "放行必须在 note 里留痕"
    assert "小时" in seen["picks"][0]["stale_note"]


def test_main_fresh_passes_gate(monkeypatch):
    """快照新鲜 ⇒ 正常出单。"""
    conn = _Conn(
        fresh_snap=_hours_ago(0.5), arrival=_hours_ago(0.3),
        sale_rows=[_sale(2042111, _hours_ago(0.5))],
        archive_rows=[(2042111, 0.55, 0.25, 0.20, "3-star", 0.72, False,
                       "2026-10-22", "laliga", "2026-10-22 01:30:02+00")],
    )
    monkeypatch.setattr(rec.pg, "write_conn", lambda *a, **k: _Ctx(conn))
    _no_predfile(monkeypatch)
    seen: dict = {}

    def _capture(conn_, picks):
        seen["picks"] = picks
        return []

    monkeypatch.setattr(rec, "write_reports", _capture)
    rc = rec.main([])
    assert rc == 0
    assert seen["picks"]
    assert not seen["picks"][0].get("stale_note")


def test_write_reports_note_includes_stale_note(monkeypatch):
    """stale_note 必须进 note 正文（留痕在库里，不只在日志里）。"""
    conn = _Conn()
    monkeypatch.setattr(rec.pg, "write_conn", lambda *a, **k: _Ctx(conn))
    captured: dict = {}

    class _Ret:
        def fetchone(self):
            return (1,)

    def _exec(sql, params=None):
        captured["params"] = params
        return _Ret()

    conn.execute = _exec  # type: ignore[assignment]
    picks = [{
        "match_id": 2042111, "option_code": "h", "sp": 3.0, "p": 0.55,
        "ev": 0.65, "kelly": 0.1, "stake": 1.0, "all_ev": {"h": 0.65},
        "home_cn": "甲队", "away_cn": "乙队", "match_date": "2026-10-22",
        "league_key": "laliga", "stars": "3-star", "confidence": 0.72,
        "low_confidence": False, "source": "archive",
        "sp_snapshot_ts": "2026-10-20 23:53:01+00", "decided_at": None,
        "stale_note": "⚠️ 定价赔率快照已 30.0 小时未更新（阈值 6.0 小时，--allow-stale 放行）",
    }]
    rec.write_reports(conn, picks)
    params = captured["params"]
    note = params[6]  # note 列在 INSERT 里的位置（made_at,legs,p_parlay,ev,kelly,corr_adj,note,…）
    assert "30.0 小时" in note
    assert "--allow-stale" in note


# ── E. 回溯模式不受今日守门影响 ───────────────────────────────────────

def test_backfill_mode_ignores_stale_gate(monkeypatch):
    """回溯模式用「决策时刻可见价」，与今日快照龄无关 ⇒ 不被逐场闸拦。"""
    _no_predfile(monkeypatch)
    conn = _Conn(
        sale_rows=[],
        archive_rows=[],
    )
    # 回溯走 _BACKFILL_SQL（model.pred_archive_match am）⇒ 这里让它返回空即可
    cands, stats = rec.build_candidates(conn, backfill=True, to_date="2026-10-22",
                                        max_offer_age_hours=6.0)
    assert cands == []
    assert stats["stale_skipped"] == 0
