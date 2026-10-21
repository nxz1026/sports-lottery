"""竞彩官方 id → ref.team 的**批量建行 + 灌映射**生产 CLI（路线图 §6.12.3 A）。

背景（2026-10-21 生产实测）：``fact.jc_match`` 里出现过的官方 id 共 **397** 个，
其中 **153** 个已映射（``ref.team_sporttery`` + ``ref.team.aliases->>'sporttery'``），
剩下 **244** 个**没有对应的 ref.team 行** ⇒ 这不是 schema 问题、也不是「一队多 id」问题，
而是**那一行根本不存在**（§6.10.1 早已查实）。
实测这 244 个竞彩中文名**两两不同**、且与现有 ``ref.team.name_cn`` **零冲突**
⇒ 1:1 建行无歧义，覆盖 **23 个联赛**。

⚠️ 诚实结论：对**用户可见产品零收益** —— 产品层按队名 join（展示名取
``fact.jc_match.home_cn/away_cn``），``ref.team`` 全仓只被 ``scripts/store/jc_ops_view.py`` 读。
所以这个 CLI 的意义是**把数据补齐**，不是产品收益。

三条纪律：

1. **默认只读**（``--dry-run``）：真跑一遍**同一套逻辑**、最后 ``ROLLBACK``
   ⇒ 预演数字与真跑**逐字相同**，但一个字节都不落库。要写必须显式 ``--execute``
   （与 ``scripts/store/materialize.py`` 同款纪律）。
2. **只增不改**：既有 ``ref.team`` 行一个字节都不动（只 INSERT 新行 + ``ON CONFLICT DO NOTHING``）；
   ``fact.jc_match`` 的 ``home_team_id/away_team_id`` **只补 NULL**。
3. **幂等**：重跑同一命令，before/after 行数必须一致 ⇒ 输出里带 before/after 供对账。

用法：

    python scripts/ingest/jc_team_backfill.py             # 预演（默认，不写库）
    python scripts/ingest/jc_team_backfill.py --execute   # 真写库
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
from store import pg  # noqa: E402

from ingest import jc_write  # noqa: E402

COUNTS_SQL = (
    "SELECT (SELECT count(*) FROM ref.team),"
    " (SELECT count(*) FROM ref.team_sporttery),"
    " (SELECT count(DISTINCT ts.jc_id) FROM ref.team_sporttery ts WHERE ts.sport = %(sport)s),"
    " (SELECT count(*) FROM fact.jc_match "
    "  WHERE home_team_id IS NOT NULL AND away_team_id IS NOT NULL),"
    " (SELECT count(*) FROM fact.jc_match),"
    " (SELECT count(DISTINCT u.jc_id) FROM ("
    "    SELECT home_sporttery_id AS jc_id FROM fact.jc_match UNION ALL"
    "    SELECT away_sporttery_id FROM fact.jc_match) u "
    "  WHERE NOT EXISTS (SELECT 1 FROM ref.team_sporttery ts2 "
    "                    WHERE ts2.sport = %(sport)s AND ts2.jc_id = u.jc_id))")


def counts(conn, sport: str = "football") -> dict[str, int]:
    """关键行数 —— 幂等与否全靠它对账。"""
    row = conn.execute(COUNTS_SQL, {"sport": sport}).fetchone()
    return {"ref_team": row[0], "team_sporttery": row[1], "mapped_ids": row[2],
            "matches_fully_mapped": row[3], "jc_match": row[4], "unmapped_ids": row[5]}


def backfill(conn, sport: str = "football") -> dict[str, Any]:
    """跑补齐。**不 commit** —— 提交/回滚由调用方决定（预演与真跑走同一条路径）。"""
    before = counts(conn, sport)
    with conn.cursor() as cur:
        stats = jc_write.ensure_jc_team_mappings(cur, sport)
    return {"before": before, "in_tx": counts(conn, sport), "stats": stats}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。退出码：0=成功；1=失败；2=参数/环境错误（**不静默**）。"""
    ap = argparse.ArgumentParser(description="竞彩官方 id 批量建行 + 灌映射（默认预演，不写库）")
    ap.add_argument("--execute", action="store_true", help="真写库（缺省 = 预演并回滚）")
    ap.add_argument("--sport", default="football", help="运动种类（缺省 football）")
    args = ap.parse_args(argv)
    try:
        conn = pg.connect("ing")
    except Exception as exc:  # noqa: BLE001 — 连不上就明说，绝不降级到别的角色
        logger.error("连不上库（ing 角色）：%s: %s", type(exc).__name__, exc)
        return 2
    try:
        payload = backfill(conn, args.sport)
        if args.execute:
            conn.commit()
        else:
            conn.rollback()          # 预演：把刚才写的东西**全部撤销**
        payload["after"] = counts(conn, args.sport)
        payload["mode"] = "execute" if args.execute else "dry-run"
        payload["written"] = bool(args.execute)
    except Exception as exc:  # noqa: BLE001 — 失败必须出声（建议 3 的「静默失败」教训）
        conn.rollback()
        logger.error("队映射补齐失败：%s: %s", type(exc).__name__, exc)
        return 1
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, default=str))
    if not args.execute:
        logger.info("[预演] 未写库（已回滚）；真补齐请加 --execute")
    else:
        b, a = payload["before"], payload["after"]
        logger.info("[补齐] ref.team %s→%s / 映射表 %s→%s / 已映射官方 id %s→%s / 双队已映射场次 %s→%s",
                    b["ref_team"], a["ref_team"], b["team_sporttery"], a["team_sporttery"],
                    b["mapped_ids"], a["mapped_ids"], b["matches_fully_mapped"], a["matches_fully_mapped"])
    return 0


if __name__ == "__main__":
    # ⚠️ 这个入口不能少：少了它，`python scripts/ingest/jc_team_backfill.py` 会**静默什么都不做**
    # （模块被导入即结束 ⇒ 退出码 0、零输出、零写入），看起来像「跑了但没数据」。
    raise SystemExit(main())
