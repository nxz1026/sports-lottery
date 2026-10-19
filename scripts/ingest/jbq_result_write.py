"""P0-COLLECT2lqP 写手：fact.jbq_result（竞彩篮球竞猜结果）。
解析器已将结果封成写指令 {"table","pk","row","notes"} ⇒ 调 upsert_jbq_result_instruction。
COLUMNS 字面 tuple，26 个业务列白名单；审计列 src_hash/src_file 由写手追加。
调用方持有连接与事务（§9-83）；未知表/未登记列/主键缺列/pk 键集不符 ⇒ ValueError，不静默跳过。

P2-18 迁移：``_validate`` / ``_build_sql`` / ``bind_values`` 已抽到
``scripts/ingest/_upsert_helper``。本模块加一道 pk 键集校验（第 4 道闸，
jc_issue 端不需此闸 —— 解析器保证 ins["pk"] 与 _PK_BY_TABLE 对齐）。
"""
from __future__ import annotations

from ingest._upsert_helper import (
    bind_values,
)
from ingest._upsert_helper import (
    build_upsert_sql as _build_sql,
)
from ingest._upsert_helper import (
    validate as _validate_impl,
)

COLUMNS = {
    "fact.jbq_result": (
        "match_id", "final_score", "ft_h", "ft_a", "status", "pool_status",
        "betting_single",
        "mnl_combination", "mnl_desc", "mnl_result_status", "mnl_odds",
        "hdc_line", "hdc_combination", "hdc_desc", "hdc_result_status", "hdc_odds",
        "hilo_line", "hilo_combination", "hilo_desc", "hilo_result_status", "hilo_odds",
        "wnm_combination", "wnm_desc", "wnm_result_status", "wnm_odds",
        "raw_blocks",
    ),
}
_PK = ("match_id",)
_JSONB = frozenset({"raw_blocks"})


def _validate(table: str, pk: dict, row: dict) -> dict:
    """四道闸（全 ValueError）：未知表 → 未登记列 → 主键缺列 → pk 键集 ≠ _PK。

    前三闸委托给 ``ingest._upsert_helper.validate``，第 4 闸（pk 键集对齐）
    本模块独有。
    """
    row = _validate_impl(table, COLUMNS, row, _PK)
    if set(pk) != set(_PK):
        raise ValueError(f"pk 键集与登记主键不符 {sorted(pk)} ≠ {sorted(_PK)} → {table}")
    return row


def upsert_jbq_result_instruction(cur, ins: dict, src_hash: str, src_file: str) -> int:
    """一条写指令 → 一行 UPSERT；表名/列名不在 COLUMNS 登记、pk 键集与 _PK 不符、主键缺列
    ⇒ ValueError（响亮，不静默）；返回受影响行数(0/1)。
    不开事务、不 commit、不 catch 一切异常（连接归调用方，§9-83）。
    jsonb 列须 Json(...) 包裹；其他类型原样绑定。"""
    table = ins["table"]
    row = _validate(table, ins["pk"], ins["row"])
    cols, vals = bind_values(row, _JSONB, src_hash, src_file)
    cur.execute(_build_sql(table, cols, ins["pk"]), vals)
    return cur.rowcount or 0
