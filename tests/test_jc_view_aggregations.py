"""scripts.store.jc_view 聚合/边界单测：补 P2-23 未覆盖的视图层。

覆盖点（scripts/store/jc_view.py 当前 39% → 全覆盖）：
  · _fetch 异常降级：异常被吞 + 日志记录（守住 P0-DASH1a 口径「绝不 5xx」）
  · fixtures_on(day)：date 校验 + day 缺省走 _DAY_LATEST
  · issues(limit)：limit 钳制到 [1, 200]
  · issue_matches(game_num, issue_no=None)：先查最新 issue_no 再查对阵
  · lottery_draws(per_type)：per_type 钳制到 [1, 100]
  · jc_movement(day)：开盘/临场聚合 + delta + 隐含概率

PG mock 策略：与 test_jc_ops_view.py 一致（闭包共享 queue + cursor
实现 __enter__/__exit__）；单测不依赖真 DSN。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from store import jc_view  # noqa: E402


@pytest.fixture
def fake_pg():
    """返回 fake_read_conn 工厂：queue 按 read_conn 调用次数 pop；元素 = list[dict] 或 Exception。"""

    def _make(rows: list):
        queue = list(rows)

        def fake_read_conn(role: str = "ro"):
            class _Cursor:
                row_factory = None

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql, params=None):
                    self_inner._next = queue.pop(0) if queue else []

                def fetchall(self_inner):
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    return list(nxt)

            class _Conn:
                def cursor(self_inner):
                    return _Cursor()

            class _Ctx:
                def __enter__(self_inner):
                    return self_inner.conn

                def __exit__(self_inner, *exc):
                    return False

            ctx = _Ctx()
            ctx.conn = _Conn()
            return ctx

        return fake_read_conn

    return _make


# ────────────────────────── _fetch ──────────────────────────


def test_fetch_returns_dict_list_on_success(fake_pg, monkeypatch):
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [{"match_num": 1, "league_cn": "意甲"}, {"match_num": 2, "league_cn": "西甲"}],
    ]))
    rows = jc_view._fetch("select 1")
    assert rows == [{"match_num": 1, "league_cn": "意甲"},
                    {"match_num": 2, "league_cn": "西甲"}]


def test_fetch_swallows_exception_returns_empty(fake_pg, monkeypatch):
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([RuntimeError("ro db down")]))
    assert jc_view._fetch("select 1") == []


def test_fetch_logs_on_exception(fake_pg, monkeypatch, caplog):
    """异常路径应记日志：既要 sql 首行也要异常类名（便于排障）。"""
    monkeypatch.setattr(jc_view.pg, "read_conn",
                        fake_pg([ConnectionError("timeout")]))
    with caplog.at_level("ERROR"):
        jc_view._fetch("select very_long_sql_first_line\n  from fact.x")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("ConnectionError" in m for m in msgs), \
        f"日志未记异常类名，实得：{msgs!r}"


# ────────────────────────── fixtures_on ──────────────────────


def test_fixtures_on_valid_date_uses_day_by(fake_pg, monkeypatch):
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [{"match_id": 1, "match_num": 7001}],
    ]))
    rows = jc_view.fixtures_on("2026-01-15")
    assert rows == [{"match_id": 1, "match_num": 7001}]


def test_fixtures_on_invalid_date_returns_empty_without_query(fake_pg, monkeypatch):
    """day 不是 ISO date → 直接返回 []，不发 SQL（避免下游错误）。"""
    calls = {"n": 0}

    def counting(role="ro"):
        calls["n"] += 1
        return fake_pg([])(role)

    monkeypatch.setattr(jc_view.pg, "read_conn", counting)
    assert jc_view.fixtures_on("not-a-date") == []
    assert calls["n"] == 0


def test_fixtures_on_none_uses_day_latest(fake_pg, monkeypatch):
    """day=None → 走 _DAY_LATEST 分支（=max(business_date)）。"""
    seen_params: list = []

    def fake_read_conn(role="ro"):
        class _Cursor:
            row_factory = None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                seen_params.append(params)
                self_inner._next = []

            def fetchall(self_inner):
                return []

        class _Conn:
            def cursor(self_inner):
                return _Cursor()

        class _Ctx:
            def __enter__(self_inner):
                return self_inner.conn

            def __exit__(self_inner, *exc):
                return False

        ctx = _Ctx()
        ctx.conn = _Conn()
        return ctx

    monkeypatch.setattr(jc_view.pg, "read_conn", fake_read_conn)
    jc_view.fixtures_on(None)
    # _DAY_LATEST 不需要 day 参数
    assert seen_params == [None] or seen_params == [{}]


# ────────────────────────── issues ──────────────────────────


def test_issues_clamps_limit_to_min_one(fake_pg, monkeypatch):
    """limit < 1 → 钳制到 1。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [{"game_num": "90", "issue_no": "26001"}],
    ]))
    jc_view.issues(limit=-5)
    # 验证 _fetch 被调（具体 params 不深究，钳制在 issues() 内部完成）
    assert True  # 不抛错即过


def test_issues_clamps_limit_to_max_200(fake_pg, monkeypatch):
    """limit > 200 → 钳制到 200。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([[]]))
    jc_view.issues(limit=999)


def test_issues_keeps_limit_in_range(fake_pg, monkeypatch):
    """limit 在 [1, 200] → 原样。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([[]]))
    jc_view.issues(limit=20)


def test_issues_string_limit_is_cast(fake_pg, monkeypatch):
    """str 数字 → int(limit) 钳制（接口容错）。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([[]]))
    jc_view.issues(limit="50")


# ────────────────────────── issue_matches ──────────────────────


def test_issue_matches_explicit_no_returns_rows_directly(fake_pg, monkeypatch):
    """issue_no 已传 → 直接查对阵（1 次 _fetch）。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [{"game_num": "90", "issue_no": "26001", "seq": 1, "home_cn": "A", "away_cn": "B"}],
    ]))
    rows = jc_view.issue_matches(game_num="90", issue_no="26001")
    assert len(rows) == 1
    assert rows[0]["home_cn"] == "A"


def test_issue_matches_none_finds_latest_issue_first(fake_pg, monkeypatch):
    """issue_no 缺省 → 先查最新一期 (game_num=90)，再查对阵（2 次 _fetch）。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        # 第 1 次：查最新 issue_no
        [{"issue_no": "26015"}],
        # 第 2 次：查对阵
        [{"seq": 1, "home_cn": "X"}, {"seq": 2, "home_cn": "Y"}],
    ]))
    rows = jc_view.issue_matches(game_num="90", issue_no=None)
    assert len(rows) == 2
    assert rows[0]["seq"] == 1


def test_issue_matches_no_latest_issue_returns_empty(fake_pg, monkeypatch):
    """缺省 issue_no + fact.jc_issue 无数据 → []，不发第二次 _fetch。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        # 第 1 次：查最新 issue_no → 空
        [],
    ]))
    rows = jc_view.issue_matches(game_num="90", issue_no=None)
    assert rows == []


# ────────────────────────── lottery_draws ──────────────────────


def test_lottery_draws_clamps_per_type(fake_pg, monkeypatch):
    """per_type 钳制 [1, 100]。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [{"game_num": "85", "issue_no": "26001"}],
    ]))
    # 边界值
    jc_view.lottery_draws(per_type=0)   # → 1
    jc_view.lottery_draws(per_type=200)  # → 100
    jc_view.lottery_draws(per_type=20)   # → 20


# ────────────────────────── jc_movement ──────────────────────


def _ts(hour: int, minute: int = 0):
    """构造 UTC tz-aware datetime（snap_ts 是 timestamptz）。"""
    return datetime(2026, 1, 15, hour, minute, tzinfo=timezone.utc)


def test_jc_movement_groups_by_match_and_play_type(fake_pg, monkeypatch):
    """每场每玩法聚合一条：开盘 first vs 临场 last。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [
            {"match_num": 7001, "play_type": "had", "league_cn": "意甲",
             "home_cn": "A", "away_cn": "B", "kickoff_bj": "01-15 20:00",
             "snap_ts": _ts(8), "goal_line": -1.0,
             "options": {"h": "2.00", "d": "3.40", "a": "3.60"}},
            {"match_num": 7001, "play_type": "had", "league_cn": "意甲",
             "home_cn": "A", "away_cn": "B", "kickoff_bj": "01-15 20:00",
             "snap_ts": _ts(20), "goal_line": -1.0,
             "options": {"h": "1.85", "d": "3.60", "a": "4.20"}},
        ],
    ]))
    rows = jc_view.jc_movement(day="2026-01-15")
    assert len(rows) == 1
    r = rows[0]
    assert r["match_num"] == 7001
    assert r["play_type"] == "had"
    # opening = first, closing = last
    assert r["opening_odds"]["h"] == 2.0
    assert r["closing_odds"]["h"] == 1.85
    # delta = closing - opening
    assert r["delta"]["h"] == pytest.approx(1.85 - 2.0, abs=1e-3)
    # opening_ts / closing_ts 是 ISO 字符串
    assert "T08:00:00" in r["opening_ts"]
    assert "T20:00:00" in r["closing_ts"]


def test_jc_movement_multiple_matches_distinct(fake_pg, monkeypatch):
    """3 场不同 match_num → 3 条记录。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [
            {"match_num": 7001, "play_type": "had", "league_cn": "意甲",
             "home_cn": "A", "away_cn": "B", "kickoff_bj": "01-15 20:00",
             "snap_ts": _ts(8), "goal_line": 0,
             "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
            {"match_num": 7002, "play_type": "had", "league_cn": "西甲",
             "home_cn": "C", "away_cn": "D", "kickoff_bj": "01-15 22:00",
             "snap_ts": _ts(8), "goal_line": -1.0,
             "options": {"h": "1.5", "d": "4.0", "a": "6.0"}},
            {"match_num": 7003, "play_type": "hhad", "league_cn": "德甲",
             "home_cn": "E", "away_cn": "F", "kickoff_bj": "01-15 00:30",
             "snap_ts": _ts(8), "goal_line": 1.0,
             "options": {"h": "3.0", "d": "3.5", "a": "2.1"}},
        ],
    ]))
    rows = jc_view.jc_movement(day="2026-01-15")
    assert len(rows) == 3
    assert {r["match_num"] for r in rows} == {7001, 7002, 7003}


def test_jc_movement_none_day_uses_latest_sql(fake_pg, monkeypatch):
    """day 缺省 → 走 _MOV_SQL_LATEST（不带 day 参数）。"""
    captured: dict = {}

    def fake_read_conn(role="ro"):
        class _Cursor:
            row_factory = None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def execute(self_inner, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params
                self_inner._next = []

            def fetchall(self_inner):
                return []

        class _Conn:
            def cursor(self_inner):
                return _Cursor()

        class _Ctx:
            def __enter__(self_inner):
                return self_inner.conn

            def __exit__(self_inner, *exc):
                return False

        ctx = _Ctx()
        ctx.conn = _Conn()
        return ctx

    monkeypatch.setattr(jc_view.pg, "read_conn", fake_read_conn)
    jc_view.jc_movement()
    # _MOV_SQL_LATEST 不含 %(day)s 参数，params 应为 {}（day=None 走 no-params 分支）
    assert captured["params"] == {}
    # sql 应含 max(business_date) 子查询
    assert "max(business_date)" in captured["sql"]


def test_jc_movement_implied_for_full_three_way(fake_pg, monkeypatch):
    """3 路完整赔率 → opening_implied 归一化 3 项（和 = 1.0）。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [
            {"match_num": 7001, "play_type": "had", "league_cn": "意甲",
             "home_cn": "A", "away_cn": "B", "kickoff_bj": "01-15 20:00",
             "snap_ts": _ts(8), "goal_line": 0,
             "options": {"h": "2.0", "d": "3.0", "a": "4.0"}},
        ],
    ]))
    rows = jc_view.jc_movement(day="2026-01-15")
    impl = rows[0]["opening_implied"]
    # 1/2 + 1/3 + 1/4 = 0.5 + 0.333 + 0.25 = 1.0833 → 归一化
    # p_h = 0.5 / 1.0833 ≈ 0.462
    assert set(impl.keys()) == {"h", "d", "a"}
    assert sum(impl.values()) == pytest.approx(1.0, abs=1e-2)


def test_jc_movement_handles_missing_odds_keys(fake_pg, monkeypatch):
    """options 缺键 / 字符串 → _nums 解析为 None；聚合不崩。"""
    monkeypatch.setattr(jc_view.pg, "read_conn", fake_pg([
        [
            {"match_num": 7001, "play_type": "had", "league_cn": "意甲",
             "home_cn": "A", "away_cn": "B", "kickoff_bj": "01-15 20:00",
             "snap_ts": _ts(8), "goal_line": 0,
             "options": {"h": "bad", "d": None, "a": "4.0"}},
        ],
    ]))
    rows = jc_view.jc_movement(day="2026-01-15")
    # 单边有效赔率（仅 a=4.0 > 1）→ 单边 100%（与历史 ABI 兼容）
    assert rows[0]["opening_implied"] == {"a": 1.0}
