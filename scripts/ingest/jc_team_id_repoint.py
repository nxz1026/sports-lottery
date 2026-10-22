"""纠正「竞彩官方 id 279 挂错队」的生产 CLI（路线图 §6.13）。

背景（2026-10-22 生产实测）：
    ``docs/db/suggestion10_team_sporttery_map.sql`` 第 3 节手写了
    ``INSERT ... SELECT 'football', 279, 2351``（2351 = 毕尔巴鄂竞技），
    依据是「279 的对手是马赛、霍芬海姆 ⇒ 欧联 ⇒ 西甲劲旅毕尔巴鄂竞技」
    —— **从对手反推球队**的猜测；而竞彩 payload 里的字面就是「**贝西克塔斯**」。
    真相：**279 = 贝西克塔斯**（Real Betis 的西甲场次名），
    **505 = 皇家贝蒂斯**（同一支俱乐部的欧战场次名），**506 = 毕尔巴鄂竞技**。
    ⇒ 279 属于 Betis（ref.team #2355，af_id=543 = API-Football 官方名 "Real Betis"），
    不属于 Athletic Bilbao（#2351，af_id=531 = "Athletic Club"）。
    ⇒ 结果：**3 场竞彩「贝西克塔斯」的比赛在 fact.jc_match 里挂到了毕尔巴鄂竞技名下**。

本 CLI 的纪律：
    1. **默认预演**：跑**同一份 SQL**（``docs/db/suggestion13_betis_id_repoint.sql``）
       → 最后 ``ROLLBACK`` ⇒ 预演与真跑是同一条路径，一个字节都不落库。
    2. **唯一真源**：SQL 逻辑只写在 .sql 文件里，本文件**不复制**任何一条语句
       ⇒ 不存在「两处改一处」的分叉风险。
    3. **备份**：写入前把受影响的行导出成 JSON（默认 ``/tmp/jc_team_id_repoint_backup.json``）
       —— ``league_ing`` 角色**没有 CREATE 权限**（备份表历来由 DBA 用 postgres 建），
       所以这里用文件备份，不依赖 DB DDL。
    4. **失败必须出声**：连不上库 / SQL 报错 / 校验不过 ⇒ 退出码非 0 且不静默。

用法：
    python scripts/ingest/jc_team_id_repoint.py                # 预演（默认，不写库）
    python scripts/ingest/jc_team_id_repoint.py --execute      # 真写库
    python scripts/ingest/jc_team_id_repoint.py --verify-only   # 只看当前状态
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parents[1]          # scripts/
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from core.log import logger  # noqa: E402

REPO_ROOT = _SCRIPT_DIR.parent
SQL_PATH = REPO_ROOT / "docs" / "db" / "suggestion13_betis_id_repoint.sql"
DEFAULT_BACKUP = Path("/tmp/jc_team_id_repoint_backup.json")

# 备份块标记：这段需要 CREATE 权限（DBA 用 psql 跑 .sql 时才执行），
# league_ing 角色的 CLI 路径会把它剥掉，改用 JSON 文件备份。
_BACKUP_START = "-- @backup-block:start"
_BACKUP_END = "-- @backup-block:end"

# 目标队 = af_id 543（API-Football 官方名 "Real Betis"；ref.team.af_id 是身份真主键）
TARGET_AF_ID = 543
JC_ID = 279

_SNAPSHOT_SQL = """
SELECT s.sport, s.jc_id, s.team_id, t.name_cn, t.af_id
FROM ref.team_sporttery s JOIN ref.team t ON t.team_id = s.team_id
WHERE s.sport = 'football' AND s.jc_id = %s
"""

_MATCHES_SQL = """
SELECT match_id, business_date, home_cn, away_cn,
       home_sporttery_id, away_sporttery_id, home_team_id, away_team_id
FROM fact.jc_match
WHERE home_sporttery_id = %s OR away_sporttery_id = %s
ORDER BY match_id
"""


def strip_backup_block(sql: str) -> str:
    """把 ``-- @backup-block:start/end`` 之间的备份语句剥掉（CLI 角色无 CREATE 权限）。"""
    if _BACKUP_START not in sql or _BACKUP_END not in sql:
        return sql
    head, rest = sql.split(_BACKUP_START, 1)
    _, tail = rest.split(_BACKUP_END, 1)
    return head + tail


def to_rollback(sql: str) -> str:
    """预演：把**最后一个** COMMIT 换成 ROLLBACK（其余语句一字不动）。"""
    idx = sql.rfind("COMMIT;")
    if idx < 0:
        return sql
    return sql[:idx] + "ROLLBACK;" + sql[idx + len("COMMIT;"):]


def snapshot(conn) -> dict[str, Any]:
    """改前快照：受影响的映射行 + 场次行（用于回滚与对账）。"""
    mp = conn.execute(_SNAPSHOT_SQL, (JC_ID,)).fetchall()
    ms = conn.execute(_MATCHES_SQL, (JC_ID, JC_ID)).fetchall()
    return {
        "jc_id": JC_ID,
        "mapping": [{"sport": r[0], "jc_id": r[1], "team_id": r[2],
                     "name_cn": r[3], "af_id": r[4]} for r in mp],
        "matches": [{"match_id": r[0], "business_date": str(r[1]), "home_cn": r[2],
                     "away_cn": r[3], "home_sporttery_id": r[4], "away_sporttery_id": r[5],
                     "home_team_id": r[6], "away_team_id": r[7]} for r in ms],
    }


def verify(conn) -> dict[str, Any]:
    """校验：279 是否已指向 Real Betis 行、3 场是否已改、506 是否未被动过。"""
    target = conn.execute(
        "SELECT team_id, name_cn FROM ref.team WHERE sport = 'football' AND af_id = %s",
        (TARGET_AF_ID,)).fetchone()
    mapped = conn.execute(
        "SELECT team_id FROM ref.team_sporttery WHERE sport = 'football' AND jc_id = %s",
        (JC_ID,)).fetchone()
    wrong = conn.execute(
        "SELECT count(*) FROM fact.jc_match "
        " WHERE (home_sporttery_id = %s AND home_team_id <> %s) "
        "    OR (away_sporttery_id = %s AND away_team_id <> %s)",
        (JC_ID, target[0], JC_ID, target[0])).fetchone()[0] if target else -1
    jc506 = conn.execute(
        "SELECT team_id FROM ref.team_sporttery WHERE sport = 'football' AND jc_id = 506").fetchone()
    return {
        "target_team_id": target[0] if target else None,
        "target_name_cn": target[1] if target else None,
        "jc279_team_id": mapped[0] if mapped else None,
        "jc279_ok": bool(mapped and target and mapped[0] == target[0]),
        "wrong_side_rows": int(wrong),
        "jc506_team_id": jc506[0] if jc506 else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="纠正竞彩 id 279 的球队映射（默认预演，不写库）")
    ap.add_argument("--execute", action="store_true", help="真写库（缺省 = 预演并回滚）")
    ap.add_argument("--verify-only", action="store_true", help="只看当前状态，不动任何东西")
    ap.add_argument("--backup-file", default=str(DEFAULT_BACKUP), help="改前快照的落盘位置")
    args = ap.parse_args(argv)

    if not SQL_PATH.exists():
        logger.error("找不到 SQL 文件：%s", SQL_PATH)
        return 2
    sql_raw = SQL_PATH.read_text(encoding="utf-8")

    from store import pg
    try:
        conn = pg.connect("ing")
    except Exception as exc:  # noqa: BLE001 — 连不上就明说，绝不降级到别的角色
        logger.error("连不上库（ing 角色）：%s: %s", type(exc).__name__, exc)
        return 2

    try:
        before = snapshot(conn)
        if args.verify_only:
            print(json.dumps({"before": before, "verify": verify(conn)},
                             ensure_ascii=False, default=str, indent=2))
            return 0

        sql = to_rollback(strip_backup_block(sql_raw)) if not args.execute else strip_backup_block(sql_raw)
        conn.execute(sql)
        after_in_tx = verify(conn)
        if args.execute:
            conn.commit()
        else:
            conn.rollback()
        after = verify(conn)
    except Exception as exc:  # noqa: BLE001 — 失败必须出声
        conn.rollback()
        logger.error("279 映射纠正失败：%s: %s", type(exc).__name__, exc)
        return 1
    finally:
        conn.close()

    payload = {"before": before, "in_tx": after_in_tx, "after": after,
               "mode": "execute" if args.execute else "dry-run",
               "written": bool(args.execute)}
    if args.execute:
        backup_path = Path(args.backup_file)
        backup_path.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["backup_file"] = str(backup_path)
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    v = after
    if not v["jc279_ok"]:
        logger.error("校验不通过：jc_id 279 未指向 Real Betis 行（当前 %s）", v["jc279_team_id"])
        return 1
    if v["wrong_side_rows"]:
        logger.error("校验不通过：仍有 %s 场以 279 为主/客的场次指向别的队", v["wrong_side_rows"])
        return 1
    logger.info("[%s] 279 → team_id=%s（%s）；受影响的场次 %s 条已改指",
                "已写库" if args.execute else "预演", v["target_team_id"], v["target_name_cn"],
                len(before["matches"]))
    if not args.execute:
        logger.info("[预演] 未写库（已回滚）；真改请加 --execute")
    return 0


if __name__ == "__main__":
    # ⚠️ 这个入口不能少：少了它，`python scripts/ingest/jc_team_id_repoint.py` 会**静默什么都不做**
    raise SystemExit(main())
