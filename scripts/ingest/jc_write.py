"""jc 八表写 SQL（P0-COLLECT2c；调用方持有连接与事务）：ON CONFLICT DO UPDATE 防旧批回退——
只有 EXCLUDED.<gate> >= 现值 gate 才更新；门控列（last_seen_at/snap_ts）必须进 INSERT 的 cols（占位符与值一一对应），
gates/extra_where 是纯文本条件、不占参数位。数值/时间列遇 ""/缺失/类型不对一律 NULL（硬规定3），原始值已由解析层保留。
"""
from __future__ import annotations

from typing import Any

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


def resolve_jc_teams(cur, home_sporttery_id: int | None, away_sporttery_id: int | None) -> tuple[int | None, int | None]:
    """官方 team id → ref.team(team_id)：aliases->>'sporttery' 文本比较（该键可能缺，缺=NULL，不许猜）。"""
    ids = [i for i in (home_sporttery_id, away_sporttery_id) if i]
    if not ids:
        return (None, None)
    rows = cur.execute(
            "SELECT (aliases->>'sporttery') AS jc, team_id FROM ref.team "
            "WHERE (aliases->>'sporttery') = ANY(%s)", ([str(i) for i in ids],)).fetchall()
    by_jc = {row[0]: row[1] for row in rows}
    return (by_jc.get(str(home_sporttery_id)), by_jc.get(str(away_sporttery_id)))


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
