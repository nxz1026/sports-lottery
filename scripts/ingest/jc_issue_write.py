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
    P0-COLLECT2 续：fact.jc_issue_prize 是 append-only（无 first/last_seen_at，is_current 翻版）
    ⇒ 走 _insert_prize：同 hash 幂等跳过、异 hash 旧行置 false + 新行插入（R48 实测修，见其 docstring）。"""
    table = ins["table"]
    row = _validate(table, ins["row"])
    if table == "fact.jc_issue_prize":
        return _insert_prize(cur, row, src_hash, src_file)
    cols, vals = bind_values(row, _JSONB, src_hash, src_file)
    cur.execute(_build_sql(table, cols, ins["pk"]), vals)
    return cur.rowcount or 0


def _insert_prize(cur, row: dict, src_hash: str, src_file: str) -> int:
    """fact.jc_issue_prize：同 hash 幂等跳过 + 异 hash 翻版（is_current 语义）。

    R48（生产实测根因）：原实现是**纯 INSERT**，PK 撞同 tier ⇒ IntegrityError 上抛
    → ``jc_load.load_batch`` 捕获后 ``conn.rollback()`` ⇒ **整批 7 个 topic 全部回滚**。
    实测 2026-10-01T14:30Z 起每批必撞（采集机把当期在售期次在**每一批**里反复推送），
    2580 批里 1805 批（70%）回滚 ⇒ 队尾新批次永远落不了库（fact.jc_offer 新鲜度落后 17h）。

    撞 PK 的两批内容实测**逐字相同**：``src_hash`` 是 payload 内容哈希（不含 snap_ts /
    fetched_at，实测同内容跨批同 hash）⇒ 这不是"数据变更"，是**同一期被反复推送**的幂等问题。

    修法（对齐 DDL v1.2 的 is_current 设计：官方开奖前后 prizeLevelList 会整体换形，
    重刷同一期时旧下标行只能置 false、不许假删）：

    · 同 ``(tier, src_hash)`` 已在表里 ⇒ **幂等跳过**（不新增行、不动 is_current）；
    · 同 tier 但 src_hash 不同（真换形）⇒ 旧行 ``is_current=false``，新行 ``is_current=true``
      插入（append-only 保留历史行）。

    PK 已放宽为 ``(game_num, issue_no, tier_no, src_hash)``（src_hash 即版本标识）
    —— 见 ``docs/db/infra_p0_8_jc_tables.sql`` 与同目录的 R48 迁移脚本。
    """
    schema, name = "fact", "jc_issue_prize"
    cols, vals = bind_values(row, _JSONB, src_hash, src_file)
    insert_sql = sql.SQL(
        "insert into {t} ({c}) values ({p}) on conflict ({k}) do nothing"
    ).format(
        t=sql.Identifier(schema, name),
        c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        p=sql.SQL(", ").join([sql.Placeholder()] * len(cols)),
        k=sql.SQL(", ").join(map(sql.Identifier, _PK_BY_TABLE["fact.jc_issue_prize"] + ("src_hash",))),
    )
    cur.execute(insert_sql, vals)
    if not cur.rowcount:
        return 0        # 同内容已在表里 ⇒ 幂等跳过（不新增版本、不改 is_current）
    # 新版本入库 ⇒ 同 tier 的旧当前版本置 false（历史行保留，符合 append-only 语义）
    cur.execute(
        sql.SQL("update {t} set is_current=false where {c1}={v1} and {c2}={v2} "
                "and {c3}={v3} and is_current and src_hash <> {v4}").format(
            t=sql.Identifier(schema, name),
            c1=sql.Identifier("game_num"), c2=sql.Identifier("issue_no"),
            c3=sql.Identifier("tier_no"),
            v1=sql.Placeholder(), v2=sql.Placeholder(),
            v3=sql.Placeholder(), v4=sql.Placeholder()),
        (row["game_num"], row["issue_no"], row["tier_no"], src_hash),
    )
    return 1
