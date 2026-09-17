"""P0-COLLECT2lqP 写手：fact.jbq_result（竞彩篮球竞猜结果）。
解析器已将结果封成写指令 {"table","pk","row","notes"} ⇒ 调 upsert_jbq_result_instruction。
COLUMNS 字面 tuple，26 个业务列白名单；审计列 src_hash/src_file 由写手追加。
调用方持有连接与事务（§9-83）；未知表/未登记列/主键缺列/pk 键集不符 ⇒ ValueError，不静默跳过。"""
from __future__ import annotations

from psycopg import sql
from psycopg.types.json import Json

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
    """四道闸（全 ValueError）：未知表 → 未登记列 → 主键缺列 → pk 键集 ≠ _PK；
    通过 ⇒ 空串归一（""→None）后的 row。"""
    cols = COLUMNS.get(table)
    if cols is None:
        raise ValueError(f"未知写指令目标表 {table!r}")
    extra = set(row) - set(cols)
    if extra:
        raise ValueError(f"指令含未登记列 {sorted(extra)} → {table}")
    missing = [c for c in _PK if c not in row]
    if missing:
        raise ValueError(f"主键缺列 {missing} → {table}")
    if set(pk) != set(_PK):
        raise ValueError(f"pk 键集与登记主键不符 {sorted(pk)} ≠ {sorted(_PK)} → {table}")
    return {k: (None if v == "" else v) for k, v in row.items()}


def _build_sql(table: str, cols: list[str], pk: dict) -> sql.Composed:
    """整条 UPSERT：冲突键=sorted(pk)；SET=所有非主键列 + last_seen_at=now()；
    first_seen_at 绝不进 SET（DDL 默认值管首见时间）。标识符一律 sql.Identifier 组合，不拼字符串。"""
    schema, name = table.split(".")
    set_parts = [sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                 for c in cols if c not in pk]
    set_parts.append(sql.SQL("last_seen_at = now()"))
    return sql.SQL(
        "insert into {t} ({c}) values ({p}) on conflict ({k}) do update set {s}"
    ).format(
        t=sql.Identifier(schema, name),
        c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        p=sql.SQL(", ").join([sql.Placeholder()] * len(cols)),
        k=sql.SQL(", ").join(map(sql.Identifier, sorted(pk))),
        s=sql.SQL(", ").join(set_parts),
    )


def upsert_jbq_result_instruction(cur, ins: dict, src_hash: str, src_file: str) -> int:
    """一条写指令 → 一行 UPSERT；表名/列名不在 COLUMNS 登记、pk 键集与 _PK 不符、主键缺列
    ⇒ ValueError（响亮，不静默）；返回受影响行数(0/1)。
    不开事务、不 commit、不 catch 一切异常（连接归调用方，§9-83）。
    jsonb 列须 Json(...) 包裹；其他类型原样绑定。"""
    table = ins["table"]
    row = _validate(table, ins["pk"], ins["row"])
    cols = list(row) + ["src_hash", "src_file"]
    vals = [Json(v) if c in _JSONB else v for c, v in row.items()] + [src_hash, src_file]
    cur.execute(_build_sql(table, cols, ins["pk"]), vals)
    return cur.rowcount or 0
