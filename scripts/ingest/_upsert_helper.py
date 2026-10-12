"""scripts/ingest/_upsert_helper.py — UPSERT 写手共享的"列校验 + SQL 构建"。

之前散在两处（同一模式，两份独立实现）：
  - ``scripts/ingest/jc_issue_write._validate`` + ``_build_sql``
  - ``scripts/ingest/jbq_result_write._validate`` + ``_build_sql``

子任务审计遗留 #1。两侧共同算法：

1. 三道闸校验：未知表 → 未登记列 → 主键缺列
2. UPSERT SQL：insert ... on conflict (k) do update set c=EXCLUDED.c, last_seen_at=now()
3. 标识符统一 ``sql.Identifier(schema, name)`` 两参拆开（单参会把点吃进引号 ⇒ UndefinedTable）
4. first_seen_at 绝不进 SET（首见时间只能由 DDL 默认值在 INSERT 那一次落定）
5. 空串归一（""→None）

差异由 caller 在 wrapper 里处理：
- jc_issue_prize 是 append-only（无 last_seen_at，走纯 INSERT，PK 撞同 tier ⇒ IntegrityError 上抛）
- jbq_result 加 pk 键集必须等于 _PK 的第 4 道闸（jc_issue 端靠解析器保证 pk 与 _PK_BY_TABLE 对齐，故无此闸）

测试：本模块抽走后，jc_issue_write / jbq_result_write 的既有测试（FakeCur
记录 execute 入参 + SQL 文本断言）应继续通过；若不通过即回归。
"""
from __future__ import annotations

from psycopg import sql


def validate(table: str, columns: dict[str, tuple[str, ...]], row: dict,
             pk_cols: tuple[str, ...]) -> dict:
    """三道闸校验（全 ValueError）：未知表 → 未登记列 → 主键缺列。

    Args:
        table: 目标表名（"schema.name"）
        columns: ``{table: (允许列 tuple)}`` 白名单字典（与 caller 模块的
                 COLUMNS 一致；本函数不拥有 schema 定义，只用其校验）
        row: 业务行 dict
        pk_cols: 该表 PK 列 tuple

    Returns:
        校验通过 + 空串归一后的 row dict（""→None）。

    Raises:
        ValueError: 未知表 / 含未登记列 / 主键缺列
    """
    cols = columns.get(table)
    if cols is None:
        raise ValueError(f"未知写指令目标表 {table!r}")
    extra = set(row) - set(cols)
    if extra:
        raise ValueError(f"指令含未登记列 {sorted(extra)} → {table}")
    missing = [c for c in pk_cols if c not in row]
    if missing:
        raise ValueError(f"主键缺列 {missing} → {table}")
    return {k: (None if v == "" else v) for k, v in row.items()}


def build_upsert_sql(table: str, cols: list[str], pk: dict) -> sql.Composed:
    """整条 UPSERT：冲突键 = sorted(pk)；SET = 所有非主键列 c=EXCLUDED.c + last_seen_at=now()。

    ``first_seen_at`` 绝不进 SET —— 首见时间只能由 DDL 默认值在 INSERT 那一次落定，
    后续 UPSERT 不能再写。schema.name 两参拆开（单参会把点吃进引号）。
    """
    schema, name = table.split(".")
    set_parts = [
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
        for c in cols if c not in pk
    ]
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


def bind_values(row: dict, jsonb_cols: frozenset[str],
                src_hash: str, src_file: str) -> tuple[list[str], list]:
    """构造 (cols, vals)：业务列 + src_hash + src_file，jsonb 列走 psycopg.Json 包裹。

    Returns:
        (cols, vals) tuple —— cols 是列名 list（不含 first_seen_at /
        last_seen_at，这两个由 DDL / SET 子句管），vals 是对应绑定值 list。
    """
    cols = list(row) + ["src_hash", "src_file"]
    vals = [Json(v) if c in jsonb_cols else v for c, v in row.items()] + [src_hash, src_file]
    return cols, vals


# psycopg.types.json.Json 在模块顶层 import（避免 bind_values 每次重 import）
from psycopg.types.json import Json  # noqa: E402
