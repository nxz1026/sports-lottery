"""scripts.store.upsert_fixtures 单测：raw → fact.fixture 写手补测。

背景：tests/test_upsert_from_raw.py 跑端到端（依赖 DSN），但
  纯函数（read_blocks / league_maps）+ 编排（_store / _merge_fd / run）
  无单测 — 表名校验、FD 对齐分支、AF 落库分支都靠单测守住。

覆盖点：
  · BLOCK_TABLES / FIXTURE_SQL / FD_ID_SQL 常量完整性
  · league_maps(conn)：返回 (af_map, fd_map) 字典
  · read_blocks(conn, table)：表名校验 + SQL 构造
  · _store(cur, row)：单行 upsert（mock resolve_team）
  · _merge_fd(cur, row, index, names)：FD 对齐 + UPDATE + merge 队 id
  · run(conn)：mock 全链路 → stats dict 字段齐全
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from store import upsert_fixtures  # noqa: E402


# ────────────────────────── 常量完整性 ──────────────────────────


def test_block_tables_contains_af_and_fd():
    """BLOCK_TABLES 必须含 af_raw / fd_raw（raw 两块来源）。"""
    assert "af_raw" in upsert_fixtures.BLOCK_TABLES
    assert "fd_raw" in upsert_fixtures.BLOCK_TABLES
    assert len(upsert_fixtures.BLOCK_TABLES) == 2


def test_fixture_sql_template_shape():
    """FIXTURE_SQL 含 INSERT … ON CONFLICT (fixture_id) DO UPDATE。"""
    sql = upsert_fixtures.FIXTURE_SQL
    assert "INSERT INTO fact.fixture" in sql
    assert "ON CONFLICT (fixture_id) DO UPDATE" in sql
    # 10 个 %s 列：fixture_id, league_key, season, round, kickoff_at,
    # home_team_id, away_team_id, status, venue, source_ids
    assert sql.count("%s") == 10


def test_fd_id_sql_template_shape():
    """FD_ID_SQL 把 football_data id 并入 source_ids jsonb。"""
    sql = upsert_fixtures.FD_ID_SQL
    assert "UPDATE fact.fixture" in sql
    assert "source_ids" in sql
    assert "jsonb_build_object" in sql
    assert "football_data" in sql


# ────────────────────────── league_maps ──────────────────────────


def test_league_maps_returns_af_and_fd_dicts():
    """league_maps: {AF league id: key}, {FD code: key}。"""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        ("epl", "39", None),
        ("laliga", "140", "PD"),
        ("seriea", "135", "SA"),
    ]
    af_map, fd_map = upsert_fixtures.league_maps(conn)
    assert af_map == {39: "epl", 140: "laliga", 135: "seriea"}
    assert fd_map == {"PD": "laliga", "SA": "seriea"}


def test_league_maps_skips_empty_ids():
    """ref.league.ids 含 NULL 字段 → 跳过（不污染映射）。"""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        ("epl", "39", None),
        ("unknown", None, None),   # 两个 id 都空
        ("laliga", "", "PD"),      # 空字符串视为缺
    ]
    af_map, fd_map = upsert_fixtures.league_maps(conn)
    assert "unknown" not in af_map
    assert "unknown" not in fd_map
    assert "laliga" not in af_map  # 空字符串不入 AF map
    assert fd_map == {"PD": "laliga"}


# ────────────────────────── read_blocks ──────────────────────────


def test_read_blocks_validates_table():
    """read_blocks 拒绝不在 BLOCK_TABLES 的表名（防 SQL 注入）。"""
    conn = MagicMock()
    with pytest.raises(ValueError, match="unknown raw block table"):
        upsert_fixtures.read_blocks(conn, table="other_raw")
    # 没执行 SQL
    conn.execute.assert_not_called()


def test_read_blocks_executes_sql_and_returns_bodies():
    """合法表名 → 调 conn.execute + 返回 list of body。"""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        ({"foo": "bar"},),
        ({"baz": "qux"},),
    ]
    bodies = upsert_fixtures.read_blocks(conn, table="af_raw")
    assert bodies == [{"foo": "bar"}, {"baz": "qux"}]
    assert conn.execute.call_count == 1


def test_read_blocks_with_params_hashes_filters():
    """params_hashes 非空 → SQL 含 params_hash = ANY(%s)。"""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    upsert_fixtures.read_blocks(conn, table="fd_raw", params_hashes=["h1", "h2"])
    sql_obj = conn.execute.call_args[0][0]
    # Composed.as_string 要 conn 有 .encoding + .connection 属性
    class _FakeConn:
        encoding = "utf-8"
        connection = None
    sql_str = sql_obj.as_string(_FakeConn())
    assert "params_hash" in sql_str
    assert "ANY" in sql_str


def test_read_blocks_without_params_hashes():
    """params_hashes=None → SQL 不含 params_hash 子句。"""
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = []
    upsert_fixtures.read_blocks(conn, table="af_raw")

    class _FakeConn:
        encoding = "utf-8"
        connection = None
    sql_str = conn.execute.call_args[0][0].as_string(_FakeConn())
    assert "params_hash" not in sql_str


# ────────────────────────── _store ──────────────────────────


def test_store_executes_fixture_sql_with_resolve_team():
    """_store 调 cur.execute(FIXTURE_SQL) + resolve_team 认队。"""
    captured = {}

    def fake_execute(sql, params):
        captured["sql"] = sql
        captured["params"] = params

    cur = MagicMock()
    cur.execute.side_effect = fake_execute

    row = {
        "fixture_id": 1001,
        "league_key": "epl",
        "season": 2025,
        "round": "Regular Season - 1",
        "kickoff_at": "2025-09-01",
        "status": "NS",
        "venue": "Anfield",
        "af_team_id": (10, 20),
        "home_name": "Liverpool",
        "away_name": "Bournemouth",
    }
    with patch.object(upsert_fixtures, "resolve_team",
                      side_effect=lambda c, n, t: (t or 100) + 1):
        upsert_fixtures._store(cur, row)
    # SQL = FIXTURE_SQL
    assert captured["sql"] == upsert_fixtures.FIXTURE_SQL
    # params 含 source_ids（Jsonb）—— psycopg3 Jsonb 不实现 __eq__，仅 isinstance
    from psycopg.types.json import Jsonb
    assert isinstance(captured["params"][-1], Jsonb)
    # 前 9 个位置参数映射正确
    assert captured["params"][0] == 1001     # fixture_id
    assert captured["params"][1] == "epl"    # league_key
    assert captured["params"][2] == 2025     # season
    assert captured["params"][3] == "Regular Season - 1"
    assert captured["params"][4] == "2025-09-01"
    assert captured["params"][5] == 11       # home_team_id（resolve_team(10)+1）
    assert captured["params"][6] == 21       # away_team_id（resolve_team(20)+1）
    assert captured["params"][7] == "NS"
    assert captured["params"][8] == "Anfield"


def test_store_handles_none_af_team_id():
    """af_team_id 为 None → resolve_team 用 None（待人工补队）。"""
    cur = MagicMock()
    row = {
        "fixture_id": 1002,
        "league_key": "epl",
        "season": 2025,
        "round": "R1",
        "kickoff_at": "2025-09-01",
        "status": "unknown",   # 触发 CASE WHEN 保留原 status
        "venue": None,
        "af_team_id": None,
        "home_name": "X",
        "away_name": "Y",
    }
    with patch.object(upsert_fixtures, "resolve_team", return_value=None):
        upsert_fixtures._store(cur, row)
    params = cur.execute.call_args[0][1]
    assert params[5] is None  # home_team_id
    assert params[6] is None  # away_team_id


# ────────────────────────── _merge_fd ──────────────────────────


def test_merge_fd_matched_executes_update():
    """align.match_fd 返回 OK → cur.execute(FD_ID_SQL) 调一次。
    注：merge_fd_teams 也会调 cur.execute，所以 cur.execute 实际 ≥ 2 次。
    本测试只断言「FD_ID_SQL 在 call_args_list 里」。"""
    cur = MagicMock()
    cur.rowcount = 1
    row = {"fixture_id": 2001, "fd_team_id": (30, 40)}
    index, names = {}, {}

    with patch.object(upsert_fixtures.align, "match_fd",
                      return_value=(1001, "ok")), \
         patch.object(upsert_fixtures, "merge_fd_teams", return_value=0):
        merged, taken = upsert_fixtures._merge_fd(cur, row, index, names)
    assert merged is True
    # FD_ID_SQL 必在所有 execute 调用中
    sqls = [c[0][0] for c in cur.execute.call_args_list]
    assert upsert_fixtures.FD_ID_SQL in sqls


def test_merge_fd_unmatched_returns_false_no_execute():
    """align.match_fd 返回非 OK → 不调 execute，merged=False。"""
    cur = MagicMock()
    row = {"fixture_id": 2001, "fd_team_id": (30, 40)}

    with patch.object(upsert_fixtures.align, "match_fd",
                      return_value=(None, "no_match_in_index")):
        merged, taken = upsert_fixtures._merge_fd(cur, row, {}, {})
    assert merged is False
    cur.execute.assert_not_called()


def test_merge_fd_ok_synonym_treated_as_hit():
    """align.OK_REASONS 含 'ok_synonym' → 也算命中（参名同义表）。"""
    cur = MagicMock()
    cur.rowcount = 1
    row = {"fixture_id": 2001, "fd_team_id": (30, 40)}

    with patch.object(upsert_fixtures.align, "match_fd",
                      return_value=(1001, "ok_synonym")):
        merged, _ = upsert_fixtures._merge_fd(cur, row, {}, {})
    assert merged is True


def test_merge_fd_zero_rowcount_after_update():
    """UPDATE 命中 0 行（fixture 不存在）→ merged=False。"""
    cur = MagicMock()
    cur.rowcount = 0
    row = {"fixture_id": 2001, "fd_team_id": (30, 40)}

    with patch.object(upsert_fixtures.align, "match_fd",
                      return_value=(1001, "ok")):
        merged, _ = upsert_fixtures._merge_fd(cur, row, {}, {})
    assert merged is False


def test_merge_fd_calls_merge_fd_teams_for_taken(monkeypatch):
    """merged=True → merge_fd_teams(cur, fid, fd_team_id) 返回 taken。"""
    cur = MagicMock()
    cur.rowcount = 1
    row = {"fixture_id": 2001, "fd_team_id": (30, 40)}

    with patch.object(upsert_fixtures.align, "match_fd",
                      return_value=(1001, "ok")), \
         patch.object(upsert_fixtures, "merge_fd_teams", return_value=2):
        merged, taken = upsert_fixtures._merge_fd(cur, row, {}, {})
    assert merged is True
    assert taken == 2


# ────────────────────────── run ──────────────────────────


def test_run_returns_stats_with_required_keys(monkeypatch):
    """run() 返回 dict 必含：rows/fd_merged/fd_unmatched/fd_id_taken
    /fixtures/skipped/unknown_leagues。"""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    with patch.object(upsert_fixtures, "league_maps", return_value=({}, {})), \
         patch.object(upsert_fixtures, "af_anchors",
                      return_value=({}, [], [], {}, {})), \
         patch.object(upsert_fixtures, "read_blocks",
                      side_effect=[[], []]):
        stats = upsert_fixtures.run(conn)
    assert set(stats.keys()) >= {
        "rows", "fd_merged", "fd_unmatched", "fd_id_taken",
        "fixtures", "skipped", "unknown_leagues",
    }


def test_run_logs_dropped_leagues(monkeypatch):
    """dropped 非空 → logger.warning 含「跳过 N 行」+ league 名。

    core.log 用 loguru，caplog 抓不到 → 直接 mock upsert_fixtures.logger。
    """
    fake_logger = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    dropped = [{"league_key": "epl"}, {"league_key": "unknown_lg"}]
    with patch.object(upsert_fixtures, "logger", fake_logger), \
         patch.object(upsert_fixtures, "league_maps", return_value=({}, {})), \
         patch.object(upsert_fixtures, "af_anchors",
                      return_value=({}, [], dropped, {}, {})), \
         patch.object(upsert_fixtures, "read_blocks", side_effect=[[], []]):
        upsert_fixtures.run(conn)
    warn_msgs = [str(c) for c in fake_logger.warning.call_args_list]
    assert any("跳过" in m and "2" in m for m in warn_msgs), \
        f"日志实得：{warn_msgs!r}"


def test_run_logs_fd_id_taken(monkeypatch):
    """fd_id_taken > 0 → logger.warning 含「FD 队 id」。

    必须让 read_blocks("fd_raw") 返回非空 body，run() 才会在 for 循环
    里调 fd_fixtures → _merge_fd，否则 taken 永远 = 0。
    core.log 用 loguru，caplog 抓不到 → 直接 mock logger。
    注：read_blocks 必须用函数 side_effect，MagicMock 的 list side_effect
    不能 mock 接受 keyword args 的函数（会被当作 return_value 序列消耗）。
    """
    fake_logger = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    fake_fd_row = {"fixture_id": 2001, "fd_team_id": (30, 40)}

    def fake_read(conn, table, params_hashes=None):
        if table == "fd_raw":
            return [{"x": 1}]
        return []

    with patch.object(upsert_fixtures, "logger", fake_logger), \
         patch.object(upsert_fixtures, "league_maps", return_value=({}, {})), \
         patch.object(upsert_fixtures, "af_anchors",
                      return_value=({}, [], [], {}, {})), \
         patch.object(upsert_fixtures, "read_blocks", side_effect=fake_read), \
         patch.object(upsert_fixtures, "fd_fixtures", return_value=[fake_fd_row]), \
         patch.object(upsert_fixtures, "_merge_fd", return_value=(True, 3)):
        upsert_fixtures.run(conn)
    assert fake_logger.warning.call_count >= 1
    warn_msgs = [str(c) for c in fake_logger.warning.call_args_list]
    assert any("FD 队 id" in m for m in warn_msgs), f"日志实得：{warn_msgs!r}"
