"""scripts/ingest/jc_load 单测：真包 JSONL → fact.jc_* 装载器 CLI 入口。

0% → 全覆盖目标：
  · TOPICS / TABLES 常量完整性与 fact schema 对齐
  · _TABLE_SQL 派生字典键集 = TABLES 元组
  · load_batch() 三态：commit / rollback + errors / 空输入
  · main() 端到端走 mock pg.connect + iter_markers + load_topic

mock 策略：纯 unit 测试，不依赖真 DSN / 真包目录；依赖关系（pg /
jc_read / jc_topic）按 jc_load.py 实际用法 patch 即可。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest import jc_load


def test_topics_constants_complete():
    """TOPICS 必须含 7 主题；OPTIONAL（jc_odds_history）由 jc_read 单独定义，
    不进 jc_load.TOPICS（避免循环 import 风险，参见 P0-COLLECT3b3 备注）。"""
    assert set(jc_load.TOPICS) >= {
        "jczq_offer", "jczq_result", "jc_issue", "jc_issue_result",
        "lottery_draw", "jclq_offer", "jclq_result",
    }
    from ingest.jc_read import OPTIONAL
    assert all(t not in jc_load.TOPICS for t in OPTIONAL)


def test_tables_constants_match_fact_schema():
    """8 表常量是 jc_load.main() 末段对账依据；缺表 = 对账不完整。"""
    assert len(jc_load.TABLES) == 8
    expected = {"jc_match", "jc_offer", "jc_result", "jc_issue",
                "jc_issue_draw", "jc_issue_match", "jc_issue_prize",
                "lottery_draw"}
    assert set(jc_load.TABLES) == expected


def test_table_sql_covers_every_table():
    """_TABLE_SQL 键集 = TABLES；每条 SQL 含 fact schema + count(*)。"""
    assert set(jc_load._TABLE_SQL.keys()) == set(jc_load.TABLES)
    for tbl, sql in jc_load._TABLE_SQL.items():
        assert f"fact.{tbl}" in sql, f"{tbl} SQL 缺 fact schema: {sql}"
        assert "count" in sql.lower(), f"{tbl} SQL 缺 count"


def test_load_batch_returns_marker_on_empty_input():
    """空 root：files_for_batch={} → 返回 marker 名 + 空 topics + 空 errors。"""
    fake_marker = Path("/tmp/.done_empty")
    with patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic", return_value={"ups": 0, "topic": "x"}):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        r = jc_load.load_batch(conn, Path("/tmp"), fake_marker)
    assert r["marker"] == ".done_empty"
    assert r["topics"] == []
    assert r["errors"] == []


def test_load_batch_commits_on_success():
    """无异常 → conn.commit() 被调一次；rollback 不被调。"""
    with patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic", return_value={"ups": 1, "topic": "x"}):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        jc_load.load_batch(conn, Path("/tmp"), Path("/tmp/.done_ok"))
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_load_batch_rolls_back_on_exception_and_records_error():
    """load_topic 抛 RuntimeError → rollback 一次；errors 列表记到「类名:消息」。"""
    fake_marker = Path("/tmp/.done_boom")
    with patch.object(jc_load, "files_for_batch",
                      return_value={"jczq_offer": [(Path("x.jsonl"), "ok")]}), \
         patch.object(jc_load, "load_topic",
                      side_effect=RuntimeError("db timeout")):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        r = jc_load.load_batch(conn, Path("/tmp"), fake_marker)
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    assert len(r["errors"]) == 1
    assert "RuntimeError" in r["errors"][0]
    assert "db timeout" in r["errors"][0]


def test_load_batch_collects_topic_results():
    """多个 topic × 多个 file → topics 列表按调用顺序累积。"""
    topic_results = iter([
        {"ups": 5, "topic": "jczq_offer"},
        {"ups": 3, "topic": "jczq_result"},
    ])
    with patch.object(jc_load, "files_for_batch",
                      return_value={"jczq_offer": [(Path("a.jsonl"), "ok")],
                                    "jczq_result": [(Path("b.jsonl"), "ok")]}):
        with patch.object(jc_load, "load_topic", side_effect=lambda *a, **kw: next(topic_results)):
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value = MagicMock()
            r = jc_load.load_batch(conn, Path("/tmp"), Path("/tmp/.done"))
    assert len(r["topics"]) == 2
    assert r["topics"][0]["topic"] == "jczq_offer"
    assert r["topics"][1]["topic"] == "jczq_result"
    assert r["errors"] == []


def test_main_smoke_returns_zero_with_mocks():
    """main() 端到端：mock pg.connect + iter_markers + files_for_batch + load_topic。
    不依赖真 DSN / 真包目录；走空 marker 路径 → 末段对账也走 mock cursor。"""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    # 末段 [cur.execute(_TABLE_SQL[t]).fetchone()[0] for t in TABLES] 需要 [N]
    fake_cursor.execute.return_value.fetchone.return_value = [0]
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(jc_load.pg, "connect", return_value=fake_conn), \
         patch.object(jc_load, "iter_markers",
                      return_value=[Path("/tmp/.done_a")]), \
         patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic",
                      return_value={"ups": 0, "topic": "x"}):
        rc = jc_load.main(["--dir", "/tmp/incoming"])
    assert rc == 0
    assert fake_conn.commit.called
    fake_conn.close.assert_called_once()


def test_main_requires_dir_argument():
    """--dir 缺失 → argparse 抛 SystemExit(2)；main 永不静默吞错。"""
    with patch.object(jc_load.pg, "connect"):
        try:
            jc_load.main([])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("--dir 缺失应当 SystemExit，实得 rc=0")


def test_main_logs_marker_count_and_errors():
    """logger.info 被调：含 marker 名 + ups 数 + errors 数。"""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.execute.return_value.fetchone.return_value = [0]
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(jc_load.pg, "connect", return_value=fake_conn), \
         patch.object(jc_load, "iter_markers",
                      return_value=[Path("/tmp/.done_z")]), \
         patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic",
                      return_value={"ups": 0, "topic": "x"}):
        with patch.object(jc_load, "logger", MagicMock()) as fake_logger:
            jc_load.main(["--dir", "/tmp/incoming"])
    # 至少调了 3 次 info：batch / orphan 装载 / count
    assert fake_logger.info.call_count >= 3
    # 至少一条含 .done_z（marker 名）
    msgs = [str(call) for call in fake_logger.info.call_args_list]
    assert any(".done_z" in m for m in msgs)


def test_main_orphan_scan_skipped_logs_warning():
    """files_for_batch 在认领扫描阶段抛错 → logger.warning，main 不崩。"""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.execute.return_value.fetchone.return_value = [0]
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(jc_load.pg, "connect", return_value=fake_conn), \
         patch.object(jc_load, "iter_markers",
                      return_value=[Path("/tmp/.done_w")]), \
         patch.object(jc_load, "files_for_batch", side_effect=OSError("scan dir vanished")), \
         patch.object(jc_load, "load_topic",
                      return_value={"ups": 0, "topic": "x"}):
        with patch.object(jc_load, "logger", MagicMock()) as fake_logger:
            rc = jc_load.main(["--dir", "/tmp/incoming"])
    assert rc == 0
    # 至少一条 warning 含 orphan-scan
    warns = [str(call) for call in fake_logger.warning.call_args_list]
    assert any("orphan-scan" in m for m in warns), f"未记 orphan-scan warning: {warns!r}"


def test_main_orphan_glob_skipped_logs_warning():
    """orphan-glob 阶段（root/topic 不存在）抛错 → logger.warning，main 不崩。"""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.execute.return_value.fetchone.return_value = [0]
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(jc_load.pg, "connect", return_value=fake_conn), \
         patch.object(jc_load, "iter_markers", return_value=[]), \
         patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic",
                      return_value={"ups": 0, "topic": "x"}):
        # 让 (root / topic).glob 抛错（topic 目录不存在）
        def fake_glob(self, pattern):
            raise FileNotFoundError("topic dir missing")

        with patch.object(Path, "glob", new=fake_glob), \
             patch.object(jc_load, "logger", MagicMock()) as fake_logger:
            rc = jc_load.main(["--dir", "/tmp/incoming"])
    assert rc == 0
    warns = [str(call) for call in fake_logger.warning.call_args_list]
    assert any("orphan-glob" in m for m in warns), f"未记 orphan-glob warning: {warns!r}"


# ---------- R48：增量水位 + 已装载跳过 ----------

def test_pending_markers_skips_done_ones():
    """水位：已在 ops.ingest_marker 的 marker 不再处理（每轮只跑新到港的批次）。"""
    ms = [Path("/tmp/a.done"), Path("/tmp/b.done"), Path("/tmp/c.done")]
    with patch.object(jc_load, "iter_markers", return_value=ms):
        pending = jc_load._pending_markers(Path("/tmp"), None, {"a.done", "c.done"})
    assert [m.name for m in pending] == ["b.done"]


def test_orphan_files_skips_already_loaded():
    """已装载文件（ops.file_arrival 有记录）不再当孤儿重装 —— 98% 冗余 IO 的根。"""
    root = Path("/tmp/incoming")
    p = root / "jczq_offer" / "old.jsonl"

    def fake_glob(self, pattern):
        # 只在 jczq_offer 目录下有这个文件
        return [p] if self.name == "jczq_offer" else []

    with patch.object(Path, "glob", new=fake_glob):
        orph = jc_load._orphan_files(root, set(), {("jczq_offer", "jczq_offer/old.jsonl")})
    assert orph == []
    # 未装载的仍在孤儿集里
    with patch.object(Path, "glob", new=fake_glob):
        orph2 = jc_load._orphan_files(root, set(), set())
    assert orph2 == [p]


def test_orphan_files_skips_claimed_files():
    """被待处理 marker 认领的文件不算孤儿（由 marker 批装载）。"""
    root = Path("/tmp/incoming")
    p = root / "jczq_offer" / "x.jsonl"

    def fake_glob(self, pattern):
        return [p] if self.name == "jczq_offer" else []

    with patch.object(Path, "glob", new=fake_glob):
        orph = jc_load._orphan_files(root, {p}, set())
    assert orph == []


def test_main_records_marker_watermark():
    """处理完的 marker 写进 ops.ingest_marker 台账（失败批次 ok=false ⇒ 下轮重试）。"""
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.execute.return_value.fetchone.return_value = [0]
    fake_conn.cursor.return_value.__enter__.return_value = fake_cursor

    with patch.object(jc_load.pg, "connect", return_value=fake_conn), \
         patch.object(jc_load, "iter_markers", return_value=[Path("/tmp/m.done")]), \
         patch.object(jc_load, "files_for_batch", return_value={}), \
         patch.object(jc_load, "load_topic", return_value={"ups": 7, "topic": "x"}):
        jc_load.main(["--dir", "/tmp/incoming"])

    upserts = [c for c in fake_cursor.execute.call_args_list
               if "insert into ops.ingest_marker" in str(c[0])]
    assert upserts, "未写 ingest_marker 台账"
    assert upserts[0][0][1] == ("m.done", 0, True)   # files_for_batch 返空 ⇒ ups=0，ok=True
