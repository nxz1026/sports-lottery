"""建议 5（滚动重算 + 赔率异动告警）的契约单测。

分六组：
  A. 相邻快照跳变检测（口径 = 跳变，**不是**首末差）；
  B. 落表与按场次聚合（推送不刷屏）；
  C. 飞书推送（**未配 webhook 必须如实说没推**，且绝不回显 URL）；
  D. 错位三分量（决策时错位 / 当前错位 / 盘口漂移）；
  E. movement 的「开盘价」bug 修复（第一版**完整** had）；
  F. web 层纪律（老字段不动 / 顶层不 import scripts / 凭证不回显）；
  G. 看板 UI（异动事件表 + 三分量列 + **内联 JS 语法合法**）与运维端点。
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market.gap_calc import compute_gaps, devig, tv2  # noqa: E402
from market.notify import feishu_configured, push_feishu  # noqa: E402
from market.odds_move import (  # noqa: E402
    DEFAULT_THRESHOLD_PCT,
    detect_moves,
    format_alert,
    group_by_match,
    jump_pct,
    write_moves,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """假连接：返回固定行，并记录执行过的 SQL。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor(self.rows)


def _ts(minute: int) -> datetime:
    return datetime(2026, 10, 21, 5, minute, tzinfo=timezone.utc)


# ── A. 相邻快照跳变检测 ───────────────────────────────────────────────

def test_jump_pct_known_values():
    """跳变幅度 = |curr/prev − 1| × 100。"""
    assert jump_pct(2.0, 2.2) == pytest.approx(10.0)
    assert jump_pct(3.3, 3.0) == pytest.approx(9.090909, rel=1e-4)
    assert jump_pct(2.0, 2.0) == pytest.approx(0.0)


def test_jump_pct_missing_or_illegal_returns_none():
    """缺值/非法 ⇒ None（不编数）。"""
    assert jump_pct(None, 2.0) is None
    assert jump_pct(2.0, None) is None
    assert jump_pct(0.0, 2.0) is None
    assert jump_pct(2.0, 0.0) is None


def test_detect_moves_flags_sides_over_threshold():
    """超阈值的侧被检出；幅度按侧算。"""
    rows = [
        (2042111, "西甲", datetime(2026, 10, 22, 2, 0), _ts(20), _ts(30),
         {"h": "3.30", "d": "3.40", "a": "2.20"}, {"h": "3.10", "d": "3.40", "a": "2.50"}),
    ]
    moves, n_pairs = detect_moves(_FakeConn(rows), threshold_pct=5.0)
    assert n_pairs == 1
    assert len(moves) == 2                       # 主胜 −6.06%、客胜 +13.64%
    by_side = {m["option_code"]: m for m in moves}
    assert by_side["h"]["move_pct"] == pytest.approx(6.0606, rel=1e-3)
    assert by_side["a"]["move_pct"] == pytest.approx(13.6363, rel=1e-3)
    assert by_side["a"]["sp_prev"] == pytest.approx(2.20)
    assert by_side["a"]["sp_curr"] == pytest.approx(2.50)
    assert by_side["a"]["match_date"] == "2026-10-22"


def test_detect_moves_respects_threshold_boundary():
    """恰好等于阈值 ⇒ 检出；略低于阈值 ⇒ 不检出。"""
    rows = [
        (1, "西甲", None, _ts(20), _ts(30),
         {"h": "2.00", "d": "3.00", "a": "4.00"}, {"h": "2.10", "d": "3.00", "a": "4.00"}),
    ]
    moves, _ = detect_moves(_FakeConn(rows), threshold_pct=5.0)
    assert len(moves) == 1 and moves[0]["move_pct"] == pytest.approx(5.0)
    moves2, _ = detect_moves(_FakeConn(rows), threshold_pct=5.01)
    assert moves2 == []


def test_detect_moves_skips_incomplete_options():
    """残缺选项 ⇒ **该侧**不参与（逐侧计算：缺 a 则 a 侧不算，h 侧照算）。

    注：SQL 层已经过滤掉「三项不齐」的相邻对（生产实测 405/406 场的第 1 版 options 是 `{}`），
    这里补的是**代码层**的第二道防线 —— 即便上游给了残缺行，也拿半个选项算不出跳变。
    """
    rows = [
        (1, "西甲", None, _ts(20), _ts(30),
         {"h": "2.00", "d": "3.00"}, {"h": "2.10", "d": "3.00", "a": "4.00"}),
    ]
    moves, _ = detect_moves(_FakeConn(rows), threshold_pct=5.0)
    assert [m["option_code"] for m in moves] == ["h"]      # a 侧缺 prev ⇒ 跳过


def test_detect_moves_default_threshold_is_five_percent():
    """默认阈值 5%（实测相邻跳变命中率 0.405% ⇒ ≈3.2 组/天）。"""
    assert DEFAULT_THRESHOLD_PCT == 5.0


def test_detect_moves_window_is_four_to_twenty_minutes():
    """相邻对窗口 = **4~20 分钟**（240~1200 秒）。

    ⚠️ 原设计是 540~660 秒（「恰好 10 分钟」），**生产实测推翻了它**：
    ① 采集间隔**并不恒定**（实测 1/3/4/5/7/8/9/10/20/23/60/97/120 分钟都有）
       ⇒ 卡死 10 分钟会漏掉 324 场里的 148 场（**45%**）；
    ② **> 60 分钟间隔的 ≥5% 命中高达 9.868%**（隔夜/重开重定价 ⇒ 假异动）
       ⇒ 必须设上限，否则告警全是噪音。
    """
    from market import odds_move as om

    assert om.MIN_GAP_SEC == 240
    assert om.MAX_GAP_SEC == 1200
    # SQL 必须把窗口作为参数传下去（不是写死在 SQL 里）
    assert "%s" in om._PAIRS_SQL and "BETWEEN %s AND %s" in om._PAIRS_SQL


# ── B. 落表与聚合 ─────────────────────────────────────────────────────

def test_write_moves_uses_idempotent_upsert():
    """幂等：同场同侧同前后版本只一行 ⇒ SQL 必须带 ON CONFLICT。"""
    conn = _FakeConn()
    n = write_moves(conn, [{
        "match_id": 1, "option_code": "h", "prev_ts": _ts(20), "curr_ts": _ts(30),
        "sp_prev": 2.0, "sp_curr": 2.2, "move_pct": 10.0, "threshold_pct": 5.0,
        "league_cn": "西甲", "match_date": "2026-10-22",
    }])
    assert n == 1
    sql = conn.executed[0][0]
    assert "ON CONFLICT" in sql and "analysis.odds_move" in sql


def test_group_by_match_aggregates_sides_per_match():
    """同一场的多个侧聚合成一条（推送不刷屏）。"""
    moves = [
        {"match_id": 1, "option_code": "h", "prev_ts": _ts(20), "curr_ts": _ts(30),
         "sp_prev": 2.0, "sp_curr": 2.2, "move_pct": 10.0, "league_cn": "西甲"},
        {"match_id": 1, "option_code": "a", "prev_ts": _ts(20), "curr_ts": _ts(30),
         "sp_prev": 4.0, "sp_curr": 4.4, "move_pct": 10.0, "league_cn": "西甲"},
        {"match_id": 2, "option_code": "d", "prev_ts": _ts(20), "curr_ts": _ts(30),
         "sp_prev": 3.0, "sp_curr": 3.3, "move_pct": 10.0, "league_cn": "西甲"},
    ]
    groups = group_by_match(moves)
    assert len(groups) == 2
    g1 = next(g for g in groups if g["match_id"] == 1)
    assert len(g1["sides"]) == 2


def _ddl_columns(ddl_text: str, table: str) -> set[str]:
    """从 DDL 里抽出某张表的列名（忽略 CONSTRAINT / PRIMARY KEY / CHECK 行）。"""
    m = re.search(r"CREATE TABLE IF NOT EXISTS " + re.escape(table) + r"\s*\((.*?)\n\);",
                  ddl_text, re.S)
    assert m, f"DDL 里找不到 {table} 的建表语句"
    cols: set[str] = set()
    for line in m.group(1).split("\n"):
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(
                ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")):
            continue
        cols.add(line.split()[0])
    return cols


def _s5_ddl_text() -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / "docs" / "db" / "suggestion5_schema.sql").read_text(encoding="utf-8")


def test_insert_sql_columns_match_ddl():
    """INSERT 用到的列必须都在 DDL 里（**单测的假连接验不到这一层**）。

    ⚠️ 生产首跑实测踩到：INSERT 写了 ``RETURNING move_id``，而表里**根本没有
    `move_id` 列** ⇒ 每次落表都抛 ``UndefinedColumn`` ⇒ 异动事件一条都进不去。
    单测全绿（假连接不执行真 SQL）⇒ 只有「DDL 与 SQL 对账」这一层能挡住它。
    """
    from market.odds_move import _INSERT_SQL

    cols = _ddl_columns(_s5_ddl_text(), "analysis.odds_move")
    m = re.search(r"INSERT INTO analysis\.odds_move\s*\(([^)]*)\)", _INSERT_SQL, re.S)
    assert m, "解析不出 INSERT 的列清单"
    used = {c.strip() for c in m.group(1).split(",") if c.strip()}
    assert used, "INSERT 列清单为空"
    missing = used - cols
    assert not missing, f"INSERT 用了 DDL 里没有的列：{sorted(missing)}"
    n_ph = _INSERT_SQL.split("VALUES", 1)[1].split("ON CONFLICT", 1)[0].count("%s")
    assert n_ph == len(used), f"占位符 {n_ph} 个 != 列 {len(used)} 个"
    m_ret = re.search(r"RETURNING\s+(\w+)", _INSERT_SQL)
    if m_ret:
        assert m_ret.group(1) in cols, f"RETURNING 引用了不存在的列 {m_ret.group(1)}"


def test_web_read_sql_columns_match_ddl():
    """只读视图的 SQL 引用列也必须都在 DDL 里（同样验不到的那一层）。"""
    from web.services import odds_move as svc

    cols = _ddl_columns(_s5_ddl_text(), "analysis.odds_move")
    for sql in (svc._RECENT_SQL, svc._OPS_SQL):
        for col in re.findall(r"\bo\.(\w+)", sql):
            assert col in cols, f"只读 SQL 引用了不存在的列 o.{col}"


def test_already_pushed_detects_previously_pushed_events():
    """推送必须**去重**：检测窗口 24h ⇒ 同一异动每轮都会重新检出。

    ⚠️ 不去重 ⇒ 每 10 分钟重推同一条 ⇒ 刷屏。已推过的事件要从推送集合里剔除。
    """
    from market.odds_move import already_pushed

    m1 = {"match_id": 1, "option_code": "h", "prev_ts": _ts(20), "curr_ts": _ts(30)}
    m2 = {"match_id": 2, "option_code": "a", "prev_ts": _ts(20), "curr_ts": _ts(30)}
    conn = _FakeConn([(1, "h", _ts(20), _ts(30))])   # m1 已推过
    done = already_pushed(conn, [m1, m2])
    assert done == {(1, "h", _ts(20), _ts(30))}
    assert m2["match_id"] not in {d[0] for d in done}
    # 查询必须带 pushed 条件 + 四元组精确匹配
    sql = conn.executed[0][0]
    assert "pushed" in sql and "prev_ts" in sql and "curr_ts" in sql


def test_already_pushed_empty_moves_short_circuits():
    """没有异动 ⇒ 不查库。"""
    from market.odds_move import already_pushed

    conn = _FakeConn()
    assert already_pushed(conn, []) == set()
    assert conn.executed == []


def test_mark_pushed_updates_exact_rows_only():
    """推送标记必须**逐条精确**（场次+侧+前后两版），不许用 ANY 的笛卡尔积。

    ⚠️ 原实现是 ``match_id = ANY(...) AND curr_ts = ANY(...)``
    ⇒ 会把「同场但不同侧 / 不同相邻对」里**没被推送**的行也标成 pushed=true
    ⇒ 正是「绝不谎报已推」这条纪律要防的事。
    """
    from market.odds_move import mark_pushed

    conn = _FakeConn()
    groups = group_by_match([
        {"match_id": 1, "option_code": "h", "prev_ts": _ts(20), "curr_ts": _ts(30),
         "sp_prev": 2.0, "sp_curr": 2.2, "move_pct": 10.0, "league_cn": "西甲"},
        {"match_id": 1, "option_code": "a", "prev_ts": _ts(40), "curr_ts": _ts(50),
         "sp_prev": 4.0, "sp_curr": 4.4, "move_pct": 10.0, "league_cn": "西甲"},
    ])
    n = mark_pushed(conn, groups)
    assert n == 2
    for sql, params in conn.executed:
        assert "ANY(" not in sql, "不许用 ANY 的笛卡尔积"
        assert params == (1, params[1], params[2], params[3])
    # 两条标记各自精确到（场次, 侧, 前一版, 后一版）
    assert [p[1] for p in (c[1] for c in conn.executed)] == ["h", "a"]
    assert conn.executed[0][1][2] == _ts(20) and conn.executed[0][1][3] == _ts(30)
    assert conn.executed[1][1][2] == _ts(40) and conn.executed[1][1][3] == _ts(50)


def test_format_alert_warns_moves_are_info_not_signal():
    """告警文案必须带「信息，不是下注理由」（路线图 §6.7.3 第 ① 条）。"""
    groups = group_by_match([{
        "match_id": 1, "option_code": "h", "prev_ts": _ts(20), "curr_ts": _ts(30),
        "sp_prev": 2.0, "sp_curr": 2.2, "move_pct": 10.0, "league_cn": "西甲",
    }])
    text = format_alert(groups, 5.0)
    assert "不是下注理由" in text
    assert "主胜" in text and "2.00" in text and "2.20" in text


# ── C. 飞书推送 ───────────────────────────────────────────────────────

def test_push_feishu_without_config_returns_not_configured(monkeypatch):
    """未配 webhook ⇒ ok=False 且 **不抛**（不许因此阻断检测/落库）。"""
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    assert feishu_configured() is False
    res = push_feishu("hello")
    assert res["ok"] is False
    assert res["reason"] == "not_configured"


def test_push_feishu_never_echoes_url(monkeypatch):
    """返回值里绝不含 URL（凭证不回显）。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/secret-token-xyz")
    assert feishu_configured() is True
    res = push_feishu("")            # 空文本 ⇒ 直接失败，不发网络请求
    assert res["ok"] is False
    assert "secret-token-xyz" not in str(res)


def test_notify_module_source_has_no_hardcoded_url():
    """源码里不得出现硬编码 webhook（只准从 env 读）。"""
    src = Path(__file__).resolve().parent.parent / "scripts" / "market" / "notify.py"
    text = src.read_text(encoding="utf-8")
    assert "open.feishu.cn" not in text
    assert "open-apis/bot" not in text


# ── D. 错位三分量 ─────────────────────────────────────────────────────

def test_tv2_known_value():
    """TV/2 = sum(|官方 − 模型|) / 2。"""
    assert tv2({"h": 0.5, "d": 0.3, "a": 0.2}, {"h": 0.4, "d": 0.3, "a": 0.3}) == pytest.approx(0.1)
    assert tv2({"h": 0.5, "d": 0.3, "a": 0.2}, {"h": 0.5, "d": 0.3, "a": 0.2}) == pytest.approx(0.0)


def test_devig_requires_all_three_sides():
    """三项不齐 ⇒ None（不用半个选项）。"""
    assert devig({"h": "1.86", "d": "3.70", "a": "3.12"}) is not None
    assert devig({"h": "1.86", "d": "3.70"}) is None
    assert devig({}) is None
    assert devig({"h": "1.86", "d": "3.70", "a": "abc"}) is None


def test_compute_gaps_splits_decide_now_and_drift():
    """三分量：决策时错位（固定）/ 当前错位（滚动）/ 漂移（pp）。"""
    rows = [
        (2042111, 7001, 0.55, 0.25, 0.20, "2026-10-22", "laliga",
         datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc), 1,
         {"h": "3.00", "d": "3.50", "a": "4.00"}, _ts(20),
         {"h": "3.30", "d": "3.50", "a": "3.60"}, _ts(30)),
    ]
    gaps = compute_gaps(_FakeConn(rows))
    assert len(gaps) == 1
    g = gaps[0]
    assert g["match_id"] == 2042111 and g["match_num"] == 7001
    assert g["gap_decide"] is not None and g["gap_now"] is not None
    assert g["gap_decide"] < g["gap_now"]          # 盘口动了 ⇒ 当前错位变大
    assert g["drift_pp"] is not None
    assert set(g["drift_pp"]) == {"h", "d", "a"}
    assert g["p_decide"] is not None and g["p_now"] is not None


def test_compute_gaps_without_decide_price_keeps_null():
    """缺「决策时盘口」⇒ gap_decide / drift 为 None（**不编数**）。"""
    rows = [
        (2042111, 7001, 0.55, 0.25, 0.20, "2026-10-22", "laliga",
         datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc), 1,
         None, None,
         {"h": "3.30", "d": "3.50", "a": "3.60"}, _ts(30)),
    ]
    gaps = compute_gaps(_FakeConn(rows))
    assert len(gaps) == 1
    assert gaps[0]["gap_decide"] is None
    assert gaps[0]["drift_pp"] is None
    assert gaps[0]["gap_now"] is not None


def test_compute_gaps_skips_match_without_current_price():
    """连当前盘口都没有 ⇒ 该场不返回（给不出任何错位）。"""
    rows = [
        (2042111, 7001, 0.55, 0.25, 0.20, "2026-10-22", "laliga",
         datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc), 1,
         {"h": "3.00", "d": "3.50", "a": "4.00"}, _ts(20), None, None),
    ]
    assert compute_gaps(_FakeConn(rows)) == []


def test_compute_gaps_filters_by_match_nums():
    """web 层只查榜上那几场 ⇒ 参数化（不扫全表）。"""
    conn = _FakeConn([])
    compute_gaps(conn, match_nums=[7001, 7002])
    assert conn.executed[0][1] == ([7001, 7002], [7001, 7002])


# ── E. movement 的「开盘价」bug 修复 ──────────────────────────────────

def test_has_full_had_rejects_empty_options():
    """空 options（竞彩刚开盘）⇒ 不算「齐全」。"""
    from store.jc_view import _has_full_had

    assert _has_full_had({}) is False
    assert _has_full_had(None) is False
    assert _has_full_had({"h": "1.86", "d": "3.70"}) is False
    assert _has_full_had({"h": "1.86", "d": "3.70", "a": "3.12"}) is True


def test_jc_movement_uses_first_complete_had_as_opening():
    """「开盘价」取第一版**齐全**的 had（不是 snaps[0]）。"""
    from store import jc_view

    snaps = [
        {"match_num": 1, "play_type": "had", "snap_ts": _ts(1), "options": {},
         "league_cn": "西甲", "home_cn": "甲", "away_cn": "乙", "kickoff_bj": None,
         "goal_line": ""},
        {"match_num": 1, "play_type": "had", "snap_ts": _ts(20),
         "options": {"h": "3.00", "d": "3.50", "a": "4.00"},
         "league_cn": "西甲", "home_cn": "甲", "away_cn": "乙", "kickoff_bj": None,
         "goal_line": ""},
        {"match_num": 1, "play_type": "had", "snap_ts": _ts(30),
         "options": {"h": "3.30", "d": "3.50", "a": "3.60"},
         "league_cn": "西甲", "home_cn": "甲", "away_cn": "乙", "kickoff_bj": None,
         "goal_line": ""},
    ]
    groups = {(1, "had"): {"base": snaps[0], "snaps": snaps}}
    orig_fetch = jc_view._fetch
    jc_view._fetch = lambda sql, params=None: snaps
    try:
        out = jc_view.jc_movement(day="2026-10-21")
    finally:
        jc_view._fetch = orig_fetch
    assert len(out) == 1
    assert out[0]["opening_odds"]["h"] == pytest.approx(3.0)     # 不是空的第一版
    assert out[0]["delta"]["h"] == pytest.approx(0.3)          # 3.30 − 3.00
    assert out[0]["opening_ts"].startswith("2026-10-21T05:20")


def test_jc_movement_falls_back_to_partial_had_when_no_complete_one():
    """全场**没有**一版齐全的 had ⇒ 退而取第一版「有有效值」的（老口径，不丢行）。

    老契约（tests/test_jc_view_aggregations.py）要求：单边有效赔率仍出行，
    隐含概率退化成单边 100% ⇒ 「取不齐就不出行」会破坏历史 ABI。
    """
    from store import jc_view

    snaps = [
        {"match_num": 1, "play_type": "had", "snap_ts": _ts(1),
         "options": {"h": "bad", "d": None, "a": "4.0"},
         "league_cn": "西甲", "home_cn": "甲", "away_cn": "乙", "kickoff_bj": None,
         "goal_line": ""},
    ]
    orig_fetch = jc_view._fetch
    jc_view._fetch = lambda sql, params=None: snaps
    try:
        out = jc_view.jc_movement(day="2026-10-21")
    finally:
        jc_view._fetch = orig_fetch
    assert len(out) == 1
    assert out[0]["opening_implied"] == {"a": 1.0}      # 单边 100%（与历史 ABI 兼容）
    assert out[0]["delta"]["h"] is None                # 缺的侧不参与、不编数


def test_jc_movement_window_days_uses_window_sql():
    """``window_days`` 给定 ⇒ 走窗口 SQL（不是只看最新一期）。"""
    from store import jc_view

    calls: list[tuple[str, dict]] = []
    orig_fetch = jc_view._fetch

    def _spy(sql, params=None):
        calls.append((sql, params))
        return []

    jc_view._fetch = _spy
    try:
        jc_view.jc_movement(window_days=3)
    finally:
        jc_view._fetch = orig_fetch
    assert len(calls) == 1
    sql, params = calls[0]
    assert sql == jc_view._MOV_SQL_WINDOW
    assert params == {"window_days": 3}


# ── F. web 层纪律 ─────────────────────────────────────────────────────

def test_web_gap_attach_triples_keeps_legacy_fields():
    """老字段（gap / official / model）**一字未动**，只增量附加新字段。"""
    from web.services.gap import attach_triples

    items = [{"match_num": 7001, "gap": 0.12, "official": {"主胜": 0.4}, "model": {"主胜": 0.5}}]
    orig = os.environ.get("FEISHU_WEBHOOK_URL")
    out = attach_triples([dict(i) for i in items])
    assert out[0]["gap"] == 0.12
    assert out[0]["official"] == {"主胜": 0.4}
    assert out[0]["model"] == {"主胜": 0.5}
    assert "gap_decide" in out[0] and "gap_now" in out[0] and "drift_pp" in out[0]
    if orig is not None:
        os.environ["FEISHU_WEBHOOK_URL"] = orig


def test_web_services_do_not_import_scripts_at_top_level():
    """web 层顶层不得 import scripts.*（守门同款纪律，单测再锁一遍）。"""
    import ast

    root = Path(__file__).resolve().parent.parent
    for rel in ("web/services/gap.py", "web/services/odds_move.py", "web/services/notify.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in tree.body:            # 只看模块顶层
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] != "scripts", f"{rel}: import {a.name}"
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "scripts", f"{rel}: from {node.module}"


# ── G. 看板 UI + 运维端点 ─────────────────────────────────────────────

_DASH = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


def test_dashboard_has_odds_move_event_table():
    """看板「盘口变动」区必须有一张**异动事件表**（含跳变 % 与已推/未推标记）。"""
    html = _DASH.read_text(encoding="utf-8")
    assert 'id="jcMoveBody"' in html          # 事件表容器
    assert "JC_MOVE_COLS" in html            # 列定义
    assert "move_pct" in html and "pushed" in html   # 跳变 % + 推送标记
    assert "loadJcOddsMove" in html          # 加载函数
    # 面板切到「盘口变动」时两块都加载（首末差 + 异动事件）
    assert "loadJcMovementPanel" in html


def test_dashboard_gap_table_has_three_components():
    """错位榜必须给出决策时错位 / 当前错位 / 漂移三列（老列保留）。"""
    html = _DASH.read_text(encoding="utf-8")
    for k in ("gap_decide", "gap_now", "drift_pp"):
        assert f'k:"{k}"' in html
    # 老字段一字未动
    for k in ("official", "model", "gap", "worst"):
        assert f'k:"{k}"' in html


def test_dashboard_inline_js_is_syntactically_valid():
    """看板内联 JS 必须语法合法。

    ⚠️ 本轮实测踩到：往 dashboard.html 插函数时少写一个大括号 ⇒ 整个内联脚本
    解析失败 ⇒ 看板所有交互全挂（页面还"能打开"，只是什么都不响应 ⇒ 静默失败）。
    浏览器测试在无 playwright 环境里是 skip ⇒ 这里再兜一层。
    """
    import shutil
    import subprocess
    import tempfile

    if shutil.which("node") is None:
        pytest.skip("当前环境未安装 node ⇒ 本项未检查（非失败）")
    html = _DASH.read_text(encoding="utf-8")
    m = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", html)
    assert m, "看板应有内联 script"
    tmp = Path(tempfile.gettempdir()) / "sports_lottery_dash_inline_test.js"
    tmp.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    assert proc.returncode == 0, f"看板内联 JS 语法错误：\n{proc.stderr[:800]}"


def test_ops_odds_move_endpoint_exists_and_degrades_gracefully(monkeypatch):
    """运维端点存在；store 异常时**降级为 0**（不 5xx、不编数）。"""
    pytest.importorskip("fastapi")
    from web.routers import jc_ops

    assert hasattr(jc_ops, "ops_odds_move")

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("web.services.odds_move.ops", _boom)
    out = jc_ops.ops_odds_move(hours=24)
    assert out["n_events"] == 0 and out["n_pushed"] == 0
    assert out["push_configured"] is False
    assert "http://" not in str(out) and "https://" not in str(out)   # 绝不回显 URL


def test_odds_move_endpoint_exists():
    """/api/jc/odds-move 端点存在（异动事件只读视图）。"""
    pytest.importorskip("fastapi")
    from web.routers import jc

    assert hasattr(jc, "odds_move")
    src = Path(jc.__file__).read_text(encoding="utf-8")
    assert '@router.get("/odds-move")' in src


def test_web_odds_move_service_degrades_to_empty_on_error(monkeypatch):
    """只读视图异常 ⇒ 降级为空列表（页面显示「暂无异动」，不 5xx）。"""
    from web.services import odds_move as svc

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("store.pg.read_conn", _boom, raising=False)
    assert svc.recent(hours=24, limit=10) == []
    out = svc.ops(hours=24)
    assert out["n_events"] == 0

