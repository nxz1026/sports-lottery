"""jc 八表写 SQL（P0-COLLECT2c；调用方持有连接与事务）：ON CONFLICT DO UPDATE 防旧批回退——
只有 EXCLUDED.<gate> >= 现值 gate 才更新；门控列（last_seen_at/snap_ts）必须进 INSERT 的 cols（占位符与值一一对应），
gates/extra_where 是纯文本条件、不占参数位。数值/时间列遇 ""/缺失/类型不对一律 NULL（硬规定3），原始值已由解析层保留。
"""
from __future__ import annotations

from typing import Any

from core.log import logger
from psycopg import sql
from psycopg.types.json import Json
from store.parse_values import dec


def _num(value: Any) -> str | None:
    """官方数字串（带千分位逗号）→ 列值：'' / 缺 / 非数字串 → NULL（硬规定3，不许当 0）。"""
    if isinstance(value, str) and value.strip():
        return dec(value.strip().replace(",", ""))
    return None


def _stext(value: Any) -> str | None:
    """str 列：空串 → NULL（DDL 原值列语义），非 str → NULL。"""
    value = str(value) if isinstance(value, (int, float)) else value
    value = value if isinstance(value, str) else None
    return value or None


def _sint(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# ── 竞彩官方 team id → ref.team(team_id) 的解析（路线图 §6.10.1）──────────────
# ① 映射表 ref.team_sporttery 为准：**一家俱乐部在竞彩可能有多个官方 id**
#    （例：毕尔巴鄂竞技 506 / 贝西克塔斯 279 是同一支队）⇒ 旧口径一行只能持一个 id，
#    塞第二个就会覆盖第一个、比不映射更糟。
# ② ref.team.aliases->>'sporttery' 兜底（仍是「主 id」）⇒ 兼容存量；
#    且这里**同时认字符串与数组两种形态** ⇒ 将来真把 aliases 迁成数组，读者已就绪、不必再改。
# 两个来源都查不到 ⇒ None（**不许猜**）。
TEAM_MAP_SQL = ("SELECT team_id, jc_id::text AS jc FROM ref.team_sporttery "
                "WHERE sport = %s AND jc_id = ANY(%s::bigint[])")
TEAM_ALIAS_SQL = (
    "SELECT t.team_id, a.jc FROM ref.team t, LATERAL ("
    "  SELECT jsonb_array_elements_text("
    "    CASE WHEN jsonb_typeof(t.aliases->'sporttery') = 'array'"
    "         THEN t.aliases->'sporttery'"
    "         ELSE jsonb_build_array(t.aliases->>'sporttery') END) AS jc"
    ") a WHERE a.jc = ANY(%s::text[])")
TEAM_MAP_INSERT_SQL = ("INSERT INTO ref.team_sporttery (sport, jc_id, team_id) VALUES (%s, %s, %s) "
                       "ON CONFLICT (sport, jc_id) DO NOTHING RETURNING team_id")
TEAM_MAP_GET_SQL = "SELECT team_id FROM ref.team_sporttery WHERE sport = %s AND jc_id = %s"

_MAP_TABLE_OK: bool | None = None      # None = 还没查过；False = 缺表（退回单 id 语义，但要出声）
_MAP_TABLE_WARNED = False


def _map_table_ready(cur) -> bool:
    """映射表是否已就位。

    缺表 ⇒ 退回旧的「单 id」语义（aliases->>'sporttery'）⇒ **功能降级但管线不断**
    （竞彩采集每 10 分钟跑一次，不能因为一张新表没建就整条断掉）。
    ⚠️ 但**必须出声**：静默降级 = 映射率悄悄变差而没人知道。
    查法用 to_regclass（不抛错的目录查询）⇒ 不会把事务带进 aborted 状态。
    """
    global _MAP_TABLE_OK, _MAP_TABLE_WARNED
    if _MAP_TABLE_OK:
        return True
    ok = bool(cur.execute("SELECT to_regclass('ref.team_sporttery') IS NOT NULL").fetchone()[0])
    _MAP_TABLE_OK = ok
    if not ok and not _MAP_TABLE_WARNED:
        _MAP_TABLE_WARNED = True
        logger.warning("[jc] ref.team_sporttery 不存在 ⇒ 官方 id 解析退回 aliases->>'sporttery' 单 id 语义"
                       "（请先执行 docs/db/suggestion10_team_sporttery_map.sql）")
    return ok


def resolve_jc_teams(cur, home_sporttery_id: int | None, away_sporttery_id: int | None,
                     sport: str = "football") -> tuple[int | None, int | None]:
    """官方 team id → ref.team(team_id)：映射表为准 + aliases 兜底（缺键=NULL，不许猜）。"""
    ids = [i for i in (home_sporttery_id, away_sporttery_id) if i]
    if not ids:
        return (None, None)
    by_jc: dict[str, int] = {}
    if _map_table_ready(cur):
        for team_id, jc in cur.execute(TEAM_MAP_SQL, (sport, ids)).fetchall():
            by_jc[str(jc)] = team_id
    missing = [i for i in ids if str(i) not in by_jc]
    if missing:
        for team_id, jc in cur.execute(TEAM_ALIAS_SQL, (missing,)).fetchall():
            by_jc.setdefault(str(jc), team_id)
    return (by_jc.get(str(home_sporttery_id)), by_jc.get(str(away_sporttery_id)))


def set_team_sporttery(cur, team_id: int, jc_id: int, sport: str = "football") -> bool:
    """登记「官方 id → 队行」到映射表（幂等）。

    返回 True = 已登记（本次新增，或本来就指向同一支）；False = **该 id 已属别的队** ⇒ 不写。
    ⚠️ 绝不覆盖既有映射：改指向等于把历史场次的 join 键改写 ⇒ 那是人工决策，不是代码该做的事。
    """
    if cur.execute(TEAM_MAP_INSERT_SQL, (sport, jc_id, team_id)).fetchone():
        return True
    row = cur.execute(TEAM_MAP_GET_SQL, (sport, jc_id)).fetchone()
    return bool(row and row[0] == team_id)


# ── 未映射官方 id 的批量建行 + 灌映射（路线图 §6.12.3 A）──────────────────────
# 实测（2026-10-21 生产库）：397 个官方 id 中 153 已映射 ⇒ **244 个未映射**；
# 这 244 个竞彩中文名**两两不同**、且与现有 ref.team.name_cn **零冲突** ⇒ 1:1 建行无歧义，
# 覆盖 23 个联赛（欧洲国家联赛 42 / 美国职业大联盟 20 / 巴西甲 19 / 国际赛 16 / 日本职业联赛 14 …）。
#
# 三条硬纪律（缺一条就可能把历史 join 键改写）：
#   ① **只增不改**：既有 ref.team 行一个字节都不动（只 INSERT 新行 + ON CONFLICT DO NOTHING）；
#   ② **不猜**：队名只取 fact.jc_match 的**竞彩官方中文名**（唯一真源），不做任何名称推断；
#   ③ **只补 NULL**：fact.jc_match 的 team_id 只填 NULL，已有映射绝不被改写。
#
# ⚠️ 诚实结论：对**用户可见产品零收益**（产品层按队名 join，ref.team 全仓只被
#    scripts/store/jc_ops_view.py 读）⇒ 做的理由是**把数据补齐**，不是产品收益。
_JC_ID_NAMES_SQL = (
    "SELECT DISTINCT jc_id, cn FROM ("
    "  SELECT home_sporttery_id AS jc_id, home_cn AS cn FROM fact.jc_match"
    "  UNION ALL"
    "  SELECT away_sporttery_id AS jc_id, away_cn AS cn FROM fact.jc_match"
    ") u WHERE u.jc_id IS NOT NULL AND u.cn IS NOT NULL AND u.cn <> ''")

TEAM_ENSURE_ROWS_SQL = (
    "INSERT INTO ref.team (sport, name_cn, aliases) "
    "SELECT DISTINCT ON (u.cn) %(sport)s, u.cn, jsonb_build_object('sporttery', u.jc_id::text) "
    "FROM (" + _JC_ID_NAMES_SQL + ") u "
    "WHERE NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts "
    "                  WHERE ts.sport = %(sport)s AND ts.jc_id = u.jc_id) "
    "  AND NOT EXISTS (SELECT 1 FROM ref.team t2 "
    "                  WHERE t2.aliases->>'sporttery' = u.jc_id::text) "
    "  AND NOT EXISTS (SELECT 1 FROM ref.team t3 "
    "                  WHERE t3.sport = %(sport)s AND t3.name_cn = u.cn) "
    "ORDER BY u.cn, u.jc_id "
    "ON CONFLICT (sport, name_cn) DO NOTHING")

TEAM_FILL_MAP_SQL = (
    "INSERT INTO ref.team_sporttery (sport, jc_id, team_id) "
    "SELECT DISTINCT ON (u.jc_id) %(sport)s, u.jc_id, t.team_id "
    "FROM (" + _JC_ID_NAMES_SQL + ") u "
    "JOIN ref.team t ON t.sport = %(sport)s AND t.name_cn = u.cn "
    "WHERE NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts "
    "                  WHERE ts.sport = %(sport)s AND ts.jc_id = u.jc_id) "
    "ORDER BY u.jc_id, t.team_id "
    "ON CONFLICT (sport, jc_id) DO NOTHING")

TEAM_BACKFILL_HOME_SQL = (
    "UPDATE fact.jc_match m SET home_team_id = ts.team_id "
    "FROM ref.team_sporttery ts "
    "WHERE ts.sport = %(sport)s AND ts.jc_id = m.home_sporttery_id "
    "AND m.home_team_id IS NULL")
TEAM_BACKFILL_AWAY_SQL = (
    "UPDATE fact.jc_match m SET away_team_id = ts.team_id "
    "FROM ref.team_sporttery ts "
    "WHERE ts.sport = %(sport)s AND ts.jc_id = m.away_sporttery_id "
    "AND m.away_team_id IS NULL")

# 同一个官方 id 在不同场次里出现不同中文名 ⇒ 建行会二选一 ⇒ 这种必须**报出来**（不静默）。
TEAM_MULTI_NAME_SQL = (
    "SELECT count(*) FROM (SELECT jc_id FROM (" + _JC_ID_NAMES_SQL + ") x "
    "GROUP BY jc_id HAVING count(DISTINCT cn) > 1) y")


def ensure_jc_team_mappings(cur, sport: str = "football") -> dict[str, int]:
    """为「竞彩出现过、但 ref.team 里还没有行」的官方 id 建行 + 灌映射，并回填存量场次。

    幂等：已建行/已映射的行不再动；``fact.jc_match`` 的 team_id **只补 NULL**。
    每轮采集跑一次的成本是 4 条查询（建完就是 0 行改动）⇒ 新队不再继续攒映射缺口。

    返回 ``{"teams_created", "mappings_added", "home_backfilled", "away_backfilled",
    "multi_name_ids"}``；``multi_name_ids`` > 0 ⇒ 有官方 id 带多个中文名，**需要人工看一眼**。
    """
    params = {"sport": sport}
    cur.execute(TEAM_ENSURE_ROWS_SQL, params)
    created = cur.rowcount or 0
    cur.execute(TEAM_FILL_MAP_SQL, params)
    mapped = cur.rowcount or 0
    cur.execute(TEAM_BACKFILL_HOME_SQL, params)
    home = cur.rowcount or 0
    cur.execute(TEAM_BACKFILL_AWAY_SQL, params)
    away = cur.rowcount or 0
    multi = cur.execute(TEAM_MULTI_NAME_SQL, params).fetchone()[0]
    if created or mapped or home or away:
        logger.info("[jc] 队映射补齐：新建 %d 行 / 灌映射 %d 条 / 回填场次 %d+%d", created, mapped, home, away)
    if multi:
        logger.warning("[jc] 有 %d 个竞彩官方 id 出现多个中文名 ⇒ 建行只取其一，需人工核对", multi)
    return {"teams_created": created, "mappings_added": mapped,
            "home_backfilled": home, "away_backfilled": away, "multi_name_ids": int(multi or 0)}



def _upsert(cur, table: str, pks: list[str], cols: list[str], row: dict,
            gates: list[str] | None = None, extra_where: str = "") -> int:
    """通用 upsert：ON CONFLICT (pks) DO UPDATE SET 可更新列 = EXCLUDED 值（WHERE 防旧批回退）。
    cols 必须含全部 PK 列与门控列（防旧批 WHERE 引用的列才有值可对比）；gates/extra_where 纯文本不占参数位。"""
    updates = [f"{c} = EXCLUDED.{c}" for c in cols if c not in pks]
    where = [f"EXCLUDED.{g} >= {table}.{g}" for g in gates or []]
    if extra_where:
        where.append(extra_where)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    st = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}{}").format(
        sql.Identifier(*table.split(".")), sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols), sql.SQL(", ").join(map(sql.Identifier, pks)),
        sql.SQL(", ").join(sql.SQL(u) for u in updates) if updates else sql.SQL("src_hash = EXCLUDED.src_hash"),
        sql.SQL(clause))
    cur.execute(st, [row.get(c) for c in cols])
    return cur.rowcount or 0


def upsert_jc_match(cur, row: dict, snap: str, source: dict, home: int | None, away: int | None) -> int:
    """fact.jc_match：match 扩展键 21 列 + team 映射 + 元数据列；last_seen_at=snap_ts 门控防旧批回退。"""
    cols = ["match_id", "match_num", "match_num_str", "match_num_date", "business_date", "kickoff_bj", "league_id",
            "league_cn", "league_abbr", "home_sporttery_id", "away_sporttery_id", "home_cn", "away_cn",
            "home_abbr", "away_abbr", "home_rank", "away_rank", "home_team_id", "away_team_id", "match_status",
            "sell_status", "betting_single", "betting_all_up", "is_hide", "is_hot", "src_hash", "src_file",
            "last_seen_at"]
    data = {k: row.get(k) for k in cols if k in row}
    data.update({"home_team_id": home, "away_team_id": away,
                 "business_date": row.get("business_date"),
                 "home_sporttery_id": row.get("home_sporttery_id"),
                 "away_sporttery_id": row.get("away_sporttery_id"), "last_seen_at": snap,
                 "src_hash": source["src_hash"], "src_file": source["src_file"]})
    return _upsert(cur, "fact.jc_match", ["match_id"], list(data), data,
                   gates=["last_seen_at"], extra_where="EXCLUDED.last_seen_at IS NOT NULL")


def upsert_jc_offer(cur, row: dict, snap: str, source: dict) -> int:
    """fact.jc_offer：官方 HAFU → DDL 'haf' 归一值已由解析层给出（硬规定4，本层不再动大小写）；snap_ts 是 PK 成员必须进 cols。"""
    cols = ["match_id", "play_type", "snap_ts", "pool_code", "options", "goal_line", "goal_line_value", "odds_update",
            "src_hash", "src_file"]
    data = {"match_id": row["match_id"], "play_type": row["play_type"], "pool_code": row["pool_code"],
            "options": Json(row["options"]), "goal_line": row.get("goal_line"),
            "goal_line_value": row.get("goal_line_value"),
            "odds_update": row.get("odds_update"), "snap_ts": snap,
            "src_hash": source["src_hash"], "src_file": source["src_file"]}
    return _upsert(cur, "fact.jc_offer", ["match_id", "play_type", "snap_ts"], cols, data,
                   gates=["snap_ts"], extra_where="EXCLUDED.snap_ts IS NOT NULL")
def upsert_jc_result(cur, row: dict, snap: str, source: dict) -> int:
    """fact.jc_result：解析层 25 键行 + team 映射 + 元数据；空串 SP/比分解析列已是 None；last_seen_at=snap_ts 门控防旧批回退。"""
    cols = ["match_num_str", "sections_no_1", "sections_no_999", "ht_h", "ht_a", "ft_h", "ft_a", "sp_home", "sp_draw",
            "sp_away", "goal_line", "win_flag", "match_result_status", "result_status", "pool_status", "league_id",
            "league_cn", "home_cn", "away_cn", "home_sporttery_id", "away_sporttery_id", "betting_single"]
    data = {k: row.get(k) for k in cols}
    data.update({"match_id": row["match_id"],
                 "src_hash": source["src_hash"], "src_file": source["src_file"], "last_seen_at": snap})
    return _upsert(cur, "fact.jc_result", ["match_id"], [c for c in data], data,
                   gates=["last_seen_at"], extra_where="EXCLUDED.last_seen_at IS NOT NULL")
