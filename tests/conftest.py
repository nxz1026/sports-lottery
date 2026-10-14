"""pytest 根级 conftest —— 收敛跨测试复用的 fixture（不起 autouse）。

R17 #5: 之前 tests/test_jc_view_aggregations.py:29 + tests/test_jc_ops_view.py:27
两处重复实现 ``fake_pg`` fixture（jc_view / jc_ops_view 用的 fake read_conn
工厂，行为基本相同）。提取到本 conftest.py 让两测试共享，其它测试按需引用。

注意：本 conftest **不起 autouse**。``reset_obs`` / 临时目录隔离等 autouse fixture
仍由各测试文件本地持有（避免引入跨文件隐式依赖）。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fake_pg():
    """返回 fake_read_conn 工厂：queue 按 cursor.execute 调用次数 pop。

    用法::

        def test_x(fake_pg):
            fake_read_conn = fake_pg([_TEAMS_OK, _TOPICS_OK, ...])
            with patch.object(jc_view, "read_conn", fake_read_conn):
                result = jc_view.ops_snapshot(...)
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

                def execute(self_inner, sql, params=None):
                    self_inner._next = queue.pop(0) if queue else []

                def fetchone(self_inner):
                    """R18 #3: 单列查询走 fetchone() 而非 fetchall()。

                    queue 元素 = list[dict]（fetchall）/ list[tuple]（fetchone）
                    / Exception（raise）。list[tuple] 时取 [0]；空 → None。
                    """
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    if isinstance(nxt, list) and nxt:
                        return nxt[0]
                    return None

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