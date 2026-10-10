"""采集契约 §5 结构断言（P0-COLLECT1c：共享 fixture/helper 抽到 tests/collector_helpers.py 全仓唯一一份；
"批到达"去向类用例移 tests/test_collector_arrival.py，v1.1 新用例在 tests/test_collector_v11.py）：
stg 七表列名/类型/可空性 == 期望常量（契约来源 docs/db/infra_p0_2_schemas.sql）。"""

from __future__ import annotations

# ruff: noqa: F401, F811  # conn 必须显式 import 才能被 pytest fixture 发现（无 conftest.py）
from tests.collector_helpers import _q, conn

EXPECTED_COLUMNS = [("line", "jsonb", "NO"), ("src_hash", "text", "NO"),
                    ("loaded_at", "timestamp with time zone", "NO"), ("src_file", "text", "NO")]
STG_TABLES = ["jc_issue", "jc_issue_result", "jclq_offer", "jclq_result", "jczq_offer", "jczq_result", "lottery_draw"]


def test_stg_schema_matches_contract(conn):
    rows = _q(conn, "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
                    " WHERE table_schema = 'stg' ORDER BY table_name, ordinal_position")
    assert sorted({table for table, *_ in rows}) == STG_TABLES  # 表集合
    assert all([(col, kind, nullable) for table, col, kind, nullable in rows if table == name] == EXPECTED_COLUMNS
               for name in STG_TABLES)
