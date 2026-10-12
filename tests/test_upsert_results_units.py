"""scripts.store.upsert_results 单测：fact.fixture_result 写手纯函数 + mock 编排。

背景：tests/test_upsert_from_raw.py 跑端到端（依赖 DSN），但
  纯函数（_params / _reject）和编排（_af_rows / _fd_rows / run）
  无单测 — DB 列名映射 / 对账明细 / FD 对齐分支都靠单测守住。

覆盖点：
  · RESULT_SQL / LOG_SQL 模板正确
  · _params(row, source, fixture_id)：字段映射完整
  · _reject(row, reason)：明细字段映射
  · _af_rows / _fd_rows：mock psycopg cur + align + 执行次数
  · run(conn)：mock league_maps + af_anchors + read_blocks 全链路
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from store import upsert_results  # noqa: E402


# ────────────────────────── 常量模板 ──────────────────────────


def test_result_sql_template_shape():
    """RESULT_SQL 含 INSERT … ON CONFLICT (fixture_id, source) DO UPDATE。"""
    sql = upsert_results.RESULT_SQL
    assert "INSERT INTO fact.fixture_result" in sql
    assert "ON CONFLICT (fixture_id, source)" in sql
    assert "DO UPDATE" in sql
    # WHERE EXISTS 子查询：fixture 缺失不落行（防 FK 破裂）
    assert "WHERE EXISTS (SELECT 1 FROM fact.fixture" in sql


def test_log_sql_template_shape():
    """LOG_SQL 写 ops.ingest_log（fd_align 留痕）。"""
    sql = upsert_results.LOG_SQL
    assert "INSERT INTO ops.ingest_log" in sql
    assert "topic" in sql
    assert "ok" in sql.lower()
    assert "rejected" in sql.lower()
    # ok=false（未对齐 = 失败留痕）
    assert "false" in sql.lower()


# ────────────────────────── _params ──────────────────────────


def test_params_field_mapping():
    """_params 必须把所有列填上（缺列 = INSERT 失败）。"""
    row = {
        "fixture_id": 1001,
        "ft_h": 2, "ft_a": 1,
        "ht_h": 1, "ht_a": 0,
        "elapsed_ht": "45",
        "status": "FT",
        "raw_node": {"home": "A", "away": "B"},
    }
    params = upsert_results._params(row, source="api_football", fixture_id=1001)
    assert params["fid"] == 1001
    assert params["src"] == "api_football"
    assert params["ft_h"] == 2
    assert params["ft_a"] == 1
    assert params["ht_h"] == 1
    assert params["ht_a"] == 0
    assert params["elapsed"] == "45"
    assert params["status"] == "FT"
    # raw 是 Jsonb（psycopg 类型）
    from psycopg.types.json import Jsonb
    assert isinstance(params["raw"], Jsonb)


def test_params_handles_fd_source():
    """FD 行 fixture_id 是 AF id（已对齐），但 source 必须是 'football_data'。"""
    row = {"ft_h": 0, "ft_a": 0, "ht_h": 0, "ht_a": 0,
           "elapsed_ht": None, "status": "FT", "raw_node": {}}
    params = upsert_results._params(row, source="football_data", fixture_id=9999)
    assert params["src"] == "football_data"
    assert params["fid"] == 9999


# ────────────────────────── _reject ──────────────────────────


def test_reject_detail_mapping():
    """_reject 明细必须含 fd_id / home / away / utc_date / reason。"""
    row = {"fixture_id": 1234, "home_name": "A", "away_name": "B",
           "kickoff_at": "2026-01-15T20:00:00Z"}
    detail = upsert_results._reject(row, reason="no_match")
    assert detail["fd_id"] == 1234
    assert detail["home"] == "A"
    assert detail["away"] == "B"
    assert detail["utc_date"] == "2026-01-15T20:00:00Z"
    assert detail["reason"] == "no_match"


# ────────────────────────── _af_rows ──────────────────────────


def _fake_row(fid: int):
    """构造一个 fixture 行（_af_rows / _fd_rows 入参）。"""
    return {
        "fixture_id": fid,
        "ft_h": 2, "ft_a": 1, "ht_h": 1, "ht_a": 0,
        "elapsed_ht": "45", "status": "FT",
        "raw_node": {"x": 1},
        "home_name": "A", "away_name": "B",
        "kickoff_at": "2026-01-15T20:00:00Z",
    }


def test_af_rows_inserts_each_fixture(monkeypatch):
    """_af_rows 对每个 fixture 调 cur.execute(RESULT_SQL) 一次。"""
    cur = MagicMock()
    cur.rowcount = 1  # 全部命中
    bodies = [[_fake_row(1001), _fake_row(1002), _fake_row(1003)]]
    leagues = {"AF_LEAGUE_ID": "league_key"}

    with patch("store.upsert_results.af_fixtures", side_effect=lambda b, l: b):
        stats = upsert_results._af_rows(cur, bodies, leagues)
    assert stats["rows"] == 3
    assert stats["results"] == 3
    assert stats["orphans"] == 0
    assert cur.execute.call_count == 3


def test_af_rows_orphans_when_rowcount_zero(monkeypatch):
    """cur.rowcount=0 → fixture 缺失，计 orphan。"""
    cur = MagicMock()
    cur.rowcount = 0  # fixture 缺失
    bodies = [[_fake_row(1001)]]
    with patch("store.upsert_results.af_fixtures", side_effect=lambda b, l: b):
        stats = upsert_results._af_rows(cur, bodies, {})
    assert stats["rows"] == 1
    assert stats["orphans"] == 1
    assert stats["results"] == 0


# ────────────────────────── _fd_rows ──────────────────────────


def test_fd_rows_unmatched_goes_to_rejected(monkeypatch):
    """align.match_fd 返回非 OK_REASONS → rejected，cur.execute 不被调。"""
    cur = MagicMock()
    bodies = [[_fake_row(2001)]]
    index, names = {}, {}
    leagues = {"FD": "league_key"}

    with patch("store.upsert_results.fd_fixtures", side_effect=lambda b, l: b), \
         patch("store.upsert_results.align.match_fd",
               return_value=(None, "no_match_in_index")):
        stats = upsert_results._fd_rows(cur, bodies, leagues, index, names)
    assert stats["rows"] == 1
    assert stats["unmatched"] == 1
    assert stats["results"] == 0
    assert len(stats["rejected"]) == 1
    assert stats["rejected"][0]["reason"] == "no_match_in_index"
    cur.execute.assert_not_called()


def test_fd_rows_matched_executes_and_returns_results(monkeypatch):
    """align.match_fd 返回 OK → cur.execute 调一次，stats.results +1。"""
    cur = MagicMock()
    cur.rowcount = 1
    bodies = [[_fake_row(2001)]]
    with patch("store.upsert_results.fd_fixtures", side_effect=lambda b, l: b), \
         patch("store.upsert_results.align.match_fd",
               return_value=(1001, "ok")):
        stats = upsert_results._fd_rows(cur, bodies, {}, {}, {})
    assert stats["results"] == 1
    assert stats["unmatched"] == 0
    cur.execute.assert_called_once()
    # 第二个参数是 _params dict，src 必须是 football_data
    args = cur.execute.call_args[0][1]
    assert args["src"] == "football_data"


def test_fd_rows_orphan_when_execute_returns_zero(monkeypatch):
    """命中但 cur.rowcount=0（fixture 缺失）→ orphan。"""
    cur = MagicMock()
    cur.rowcount = 0
    bodies = [[_fake_row(2001)]]
    with patch("store.upsert_results.fd_fixtures", side_effect=lambda b, l: b), \
         patch("store.upsert_results.align.match_fd", return_value=(1001, "ok")):
        stats = upsert_results._fd_rows(cur, bodies, {}, {}, {})
    assert stats["orphans"] == 1
    assert stats["results"] == 0


# ────────────────────────── run ──────────────────────────


def test_run_writes_log_when_fd_has_rejected(monkeypatch):
    """run()：FD 有 rejected → 写 ops.ingest_log（LOG_SQL）。"""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    with patch.object(upsert_results, "league_maps", return_value=({}, {})), \
         patch.object(upsert_results, "af_anchors",
                      return_value=({}, [], [], {}, {})), \
         patch.object(upsert_results, "read_blocks",
                      side_effect=[[{"x": 1}], [{"x": 2}]]), \
         patch.object(upsert_results, "_af_rows",
                      return_value={"rows": 1, "results": 1, "orphans": 0}), \
         patch.object(upsert_results, "_fd_rows",
                      return_value={"rows": 1, "results": 0, "orphans": 0,
                                    "unmatched": 1, "rejected": [{"fd_id": 1}]}):
        conn.execute.return_value.fetchone.return_value = [0]  # conflicts
        stats = upsert_results.run(conn)
    assert stats["results"] == 1
    assert stats["unmatched"] == 1
    # LOG_SQL 被调（写 ops.ingest_log）
    log_calls = [c for c in cur.execute.call_args_list
                 if "ops.ingest_log" in str(c)]
    assert len(log_calls) == 1, "FD rejected 时必须写 ops.ingest_log"


def test_run_no_log_when_fd_no_rejected(monkeypatch):
    """FD 无 rejected → 不写 LOG_SQL（避免空日志）。"""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    with patch.object(upsert_results, "league_maps", return_value=({}, {})), \
         patch.object(upsert_results, "af_anchors",
                      return_value=({}, [], [], {}, {})), \
         patch.object(upsert_results, "read_blocks",
                      side_effect=[[], []]), \
         patch.object(upsert_results, "_af_rows",
                      return_value={"rows": 0, "results": 0, "orphans": 0}), \
         patch.object(upsert_results, "_fd_rows",
                      return_value={"rows": 0, "results": 0, "orphans": 0,
                                    "unmatched": 0, "rejected": []}):
        conn.execute.return_value.fetchone.return_value = [0]
        upsert_results.run(conn)
    log_calls = [c for c in cur.execute.call_args_list
                 if "ops.ingest_log" in str(c)]
    assert len(log_calls) == 0, "无 FD rejected 时不应写 LOG_SQL"


def test_run_aggregates_af_and_fd_stats(monkeypatch):
    """run() 返回 dict 聚合 AF + FD：results / orphans / unmatched。"""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    with patch.object(upsert_results, "league_maps", return_value=({}, {})), \
         patch.object(upsert_results, "af_anchors",
                      return_value=({}, [], [], {}, {})), \
         patch.object(upsert_results, "read_blocks", side_effect=[[], []]), \
         patch.object(upsert_results, "_af_rows",
                      return_value={"rows": 5, "results": 4, "orphans": 1}), \
         patch.object(upsert_results, "_fd_rows",
                      return_value={"rows": 3, "results": 2, "orphans": 0,
                                    "unmatched": 1, "rejected": []}):
        conn.execute.return_value.fetchone.return_value = [0]
        stats = upsert_results.run(conn)
    assert stats["rows"] == 8  # 5 + 3
    assert stats["results"] == 6  # 4 + 2
    assert stats["af_results"] == 4
    assert stats["fd_results"] == 2
    assert stats["orphans"] == 1
    assert stats["unmatched"] == 1
    assert stats["conflicts"] == 0
