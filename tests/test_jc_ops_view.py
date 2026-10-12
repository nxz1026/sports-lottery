"""scripts.store.jc_ops_view 单测：竞彩看板「入库与对照」只读视图。

覆盖点（scripts/store/jc_ops_view.py 0% → 全覆盖）：
  · 四段完整性：teams / topics / quota / rejects 四个 key 齐全
  · ok_<key> 标记：每段独立记录成功/失败
  · 降级独立性：单段失败不影响其他段；异常被吞、不向上传
  · 返回类型：rows 是 list[dict[str, Any]]
  · 异常日志：异常时 logger.error 记录了异常类名（便于排障）

PG mock 策略：用 fake 上下文管理器替 ``pg.read_conn``，返回受控 conn +
cursor，避免依赖真 DSN。queue 按 cursor.execute 调用顺序逐次消费
（顺序与 _SQLS.items() 一致：teams → topics → quota → rejects）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from store import jc_ops_view  # noqa: E402,F401


@pytest.fixture
def fake_pg():
    """返回一个工厂 fake_read_conn(rows)，可按段配置异常/数据。

    rows 是 list，按 cursor.execute 调用顺序消费（顺序与 _SQLS.items()
    一致：teams → topics → quota → rejects）；元素 = list[dict] 或 Exception。

    实现要点：``queue`` 是 ``_make`` 闭包变量，所有 fake_read_conn 调用
    共享同一队列（ops_snapshot 会调 4 次 read_conn，每次走同一 queue）。
    """

    def _make(rows: list):
        queue = list(rows)

        def fake_read_conn(role: str = "ro"):
            class _Cursor:
                row_factory = None

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql):
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


# _SQLS.items() 顺序：teams → topics → quota → rejects
_TEAMS_OK = [{"league": "意甲", "n_matches": 10}]
_TOPICS_OK = [{"topic": "lottery_draw", "n_files": 5}]
_QUOTA_OK = [{"source": "api_football", "used": 10, "remaining": 90}]
_REJECTS_OK: list = []


def test_ops_snapshot_returns_four_sections_with_ok_marks(fake_pg, monkeypatch):
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([_TEAMS_OK, _TOPICS_OK, _QUOTA_OK, _REJECTS_OK]),
    )
    snap = jc_ops_view.ops_snapshot()
    # 四段全部 ok 且是 list
    for k in ("teams", "topics", "quota", "rejects"):
        assert isinstance(snap[k], list)
        assert snap["ok_" + k] is True
    # 数据正确
    assert snap["teams"] == _TEAMS_OK
    assert snap["topics"] == _TOPICS_OK
    assert snap["quota"][0]["remaining"] == 90
    assert snap["rejects"] == []


def test_ops_snapshot_degrades_only_failed_section(fake_pg, monkeypatch):
    """topics 段异常 → 仅 ok_topics=False，其他三段仍 ok=True。"""
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([
            [{"league": "西甲"}],
            RuntimeError("topics store 临时不可用"),
            [{"source": "api_football", "remaining": 100}],
            [],
        ]),
    )
    snap = jc_ops_view.ops_snapshot()
    assert snap["ok_teams"] is True
    assert snap["ok_topics"] is False
    assert snap["topics"] == []  # 失败段为空表
    assert snap["ok_quota"] is True
    assert snap["ok_rejects"] is True


def test_ops_snapshot_all_sections_fail_still_returns_shape(fake_pg, monkeypatch):
    """四段全失败 → 仍返回 4 段 + 4 个 ok=False 的字典结构，绝不 raise。"""
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([
            RuntimeError("a"), RuntimeError("b"),
            RuntimeError("c"), RuntimeError("d"),
        ]),
    )
    snap = jc_ops_view.ops_snapshot()
    for k in ("teams", "topics", "quota", "rejects"):
        assert snap[k] == []
        assert snap["ok_" + k] is False


def test_ops_snapshot_does_not_raise_on_exception(fake_pg, monkeypatch):
    """异常被吞，ops_snapshot() 自身永不 raise。"""
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([
            RuntimeError("x"), ValueError("y"),
            TypeError("z"), KeyError("w"),
        ]),
    )
    snap = jc_ops_view.ops_snapshot()
    assert isinstance(snap, dict)
    assert all(snap["ok_" + k] is False for k in ("teams", "topics", "quota", "rejects"))


def test_ops_snapshot_logs_exception_class(fake_pg, monkeypatch, caplog):
    """异常路径应 logger.error 记录「异常类名」便于排障。"""
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([
            [{"league": "x"}],
            ConnectionError("topics db down"),
            [{"source": "s", "remaining": 1}],
            [],
        ]),
    )
    with caplog.at_level("ERROR"):
        jc_ops_view.ops_snapshot()
    msgs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("ConnectionError" in m for m in msgs), \
        f"期望日志记到 ConnectionError，实得：{msgs!r}"


def test_ops_snapshot_sql_keys_complete():
    """_SQLS 必须含 teams/topics/quota/rejects 四段；缺失会破坏看板契约。"""
    assert set(jc_ops_view._SQLS.keys()) == {"teams", "topics", "quota", "rejects"}
    for k, sql in jc_ops_view._SQLS.items():
        assert sql.strip(), f"{k} SQL 为空"
        assert "select" in sql.lower(), f"{k} SQL 缺 select"


def test_fetch_returns_rows_as_dict_list(fake_pg, monkeypatch):
    """_fetch 正常路径：rows 是 list[dict]，ok=True。"""
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([[{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]]),
    )
    rows, ok = jc_ops_view._fetch("select 1")
    assert ok is True
    assert isinstance(rows, list)
    assert all(isinstance(r, dict) for r in rows)
    assert rows == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


def test_fetch_swallows_exception_and_returns_empty_false(fake_pg, monkeypatch):
    monkeypatch.setattr(
        jc_ops_view.pg, "read_conn",
        fake_pg([RuntimeError("boom")]),
    )
    rows, ok = jc_ops_view._fetch("select 1")
    assert rows == []
    assert ok is False


def test_fetch_returns_false_on_invalid_role(monkeypatch):
    """pg.read_conn 自己抛 ValueError（unknown role）→ _fetch 兜底返回 ([], False)。"""

    def bad(role="ro"):
        raise ValueError(f"unknown role {role!r}")

    monkeypatch.setattr(jc_ops_view.pg, "read_conn", bad)
    rows, ok = jc_ops_view._fetch("select 1")
    assert rows == []
    assert ok is False
