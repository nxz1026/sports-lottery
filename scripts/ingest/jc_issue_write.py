"""P0-COLLECT2eA 写手：三个"写指令" topic（jc_issue / jc_issue_result / lottery_draw）
解析器已返回同形写指令 {"table","pk","row","notes"} ⇒ 一套通用 upsert_issue_instruction。
P0-COLLECT2 续：jc_issue.matchList → fact.jc_issue_match；jc_issue_result.prizeLevelList → fact.jc_issue_prize。
COLUMNS 为 dict 形态双职责（§9-74）：列白名单 + jc-cols-check 静态列核对。
调用方持有连接与事务（§9-83）；未知表/未登记列/主键缺列 ⇒ 响亮 ValueError，不静默跳过。

P2-18 迁移：``_validate`` / ``_build_sql`` 已抽到 ``scripts/ingest/_upsert_helper``，
本模块保留 COLUMNS / _PK_BY_TABLE / _JSONB 配置 + 业务级 entry（``_insert_prize``
append-only 路径仍在本模块，因为是 jc 特有语义）。
"""
from __future__ import annotations

from psycopg import sql

from ingest._upsert_helper import (
    bind_values,
)
from ingest._upsert_helper import (
    build_upsert_sql as _build_sql,
)
from ingest._upsert_helper import (
    validate as _validate_impl,
)

# 表 → 允许列（字面 dict，列 ⊆ DDL；审计尾 first_seen_at 由 DDL 默认值管，last_seen_at 由 now() 管）
COLUMNS = {
    "fact.jc_issue": ("game_num", "issue_no", "game_name", "sale_begin", "sale_end", "draw_at",
                      "n_matches", "draw_num_list", "raw_head"),
    "fact.jc_issue_draw": ("game_num", "issue_no", "game_key", "game_name", "draw_result", "pool_after",
                           "sales", "pool_after_rj", "sales_rj", "paid_begin", "paid_end",
                           "is_delay", "delay_remark"),
    "fact.lottery_draw": ("game_num", "issue_no", "game_name", "draw_date", "status", "numbers_raw",
                          "numbers", "pool", "prizes", "equipment_count"),
    "fact.jc_issue_match": ("game_num", "issue_no", "seq", "gm_match_id", "league_cn",
                            "home_cn", "away_cn", "home_short", "away_short",
                            "start_date", "raw", "is_drawn", "cz_score", "cz_half_score", "official_result"),
    "fact.jc_issue_prize": ("game_num", "issue_no", "tier_no", "prize_level", "stake_count",
                            "stake_amount", "stake_amount_format", "total_prizeamount",
                            "award_type", "grp", "sort_no", "is_rj", "raw", "is_current"),
}
# jc_issue_match PK 是 (game_num, issue_no, seq)，jc_issue_prize PK 是 (game_num, issue_no, tier_no)；
# 其它三表 PK 仍为 (game_num, issue_no)。统一在 _validate 用 ins["pk"] 拿到当前行的真实主键。
_PK_BY_TABLE = {
    "fact.jc_issue": ("game_num", "issue_no"),
    "fact.jc_issue_draw": ("game_num", "issue_no"),
    "fact.lottery_draw": ("game_num", "issue_no"),
    "fact.jc_issue_match": ("game_num", "issue_no", "seq"),
    "fact.jc_issue_prize": ("game_num", "issue_no", "tier_no"),
}
_JSONB = frozenset({"draw_num_list", "raw_head", "numbers", "prizes", "raw"})


def _validate(table: str, row: dict) -> dict:
    """三道闸（全 ValueError）：未知表 → 未登记列 → 主键缺列；通过 ⇒ 空串归一后的 row。

    委托给 ``ingest._upsert_helper.validate``，PK 列由 ``_PK_BY_TABLE`` 拿。
    """
    return _validate_impl(table, COLUMNS, row, _PK_BY_TABLE[table])


def upsert_issue_instruction(cur, ins: dict, src_hash: str, src_file: str) -> int:
    """一条写指令 → 一行 UPSERT；表名/列名不在 COLUMNS 登记内 ⇒ ValueError（响亮，别静默跳过）；
    返回受影响行数(0/1)。不开事务、不 commit、不 catch 一切异常（连接归调用方，§9-83）。
    jsonb 列 dict/list 须 Json(...) 包裹（psycopg3 不自动适配 dict，实测 ProgrammingError）；
    timestamp/date/text 直接绑参数，不 str() 强转。
    P0-COLLECT2 续：fact.jc_issue_prize 是 append-only（无 first/last_seen_at，is_current 翻版）⇒ 走
    纯 INSERT；PK 撞同 tier ⇒ IntegrityError 上抛（不静默吞、不 upsert、不静默改 is_current）。"""
    table = ins["table"]
    row = _validate(table, ins["row"])
    if table == "fact.jc_issue_prize":
        return _insert_prize(cur, row, src_hash, src_file)
    cols, vals = bind_values(row, _JSONB, src_hash, src_file)
    cur.execute(_build_sql(table, cols, ins["pk"]), vals)
    return cur.rowcount or 0


def _insert_prize(cur, row: dict, src_hash: str, src_file: str) -> int:
    """fact.jc_issue_prize 纯 INSERT（无 first/last_seen_at，无 on conflict）。

    走纯 INSERT 而非 UPSERT：append-only 表，PK 撞同 tier ⇒ IntegrityError
    上抛（不静默吞、不 upsert 改写历史）。
    """
    schema, name = "fact", "jc_issue_prize"
    cols, vals = bind_values(row, _JSONB, src_hash, src_file)
    sql_obj = sql.SQL("insert into {t} ({c}) values ({p})").format(
        t=sql.Identifier(schema, name),
        c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        p=sql.SQL(", ").join([sql.Placeholder()] * len(cols)),
    )
    cur.execute(sql_obj, vals)
    return cur.rowcount or 0
