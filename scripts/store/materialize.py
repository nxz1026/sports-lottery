"""raw → fact 物化入口的**生产 CLI**（路线图 §6.10.2）。

为什么之前没有这个入口：物化函数**早就在**
（`store.upsert_fixtures.run` → `ref.team` + `fact.fixture`；
`store.upsert_results.run` → `fact.fixture_result` + `ops.ingest_log`），
但**全仓只有 `tests/` 在调** ⇒ 每轮回填后只能手工物化
（建议 1 的 UCL 回填就是手工跑的，路线图 §6.4 已记）。

三条纪律：

1. **默认只读**（`--dry-run`）：真跑一遍**同一套逻辑**、最后 `ROLLBACK`
   ⇒ 预演数字与真跑**逐字相同**，但一个字节都不落库。要写必须显式 `--execute`
   （与 `ingest/run_backfill.py` 的「默认只读」纪律同族）。
2. **一个事务包住两个 run**：要么都成、要么都不成 ⇒ 不留「fixture 写了、赛果没写」的半截状态。
   （`fact`/`ref` 没有 DELETE，插进去就洗不掉 ⇒ 半截状态比不跑更糟。）
3. **幂等**：重跑同一命令，`fact` 行数必须完全一致 —— 所以输出里**必须**带 before/after 行数，
   操作者一眼能看出「有没有长出重复行」。

用法：

    python scripts/store/materialize.py                          # 预演（默认，不写库）
    python scripts/store/materialize.py --execute                # 真物化
    python scripts/store/materialize.py --params-hashes a,b --execute
    python scripts/store/materialize.py --fixtures-only          # 只物化场次
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
from psycopg import sql  # noqa: E402

from store import pg  # noqa: E402
from store.upsert_fixtures import run as run_fixtures  # noqa: E402
from store.upsert_results import run as run_results  # noqa: E402

COUNTS_SQL = ("SELECT (SELECT count(*) FROM fact.fixture), (SELECT count(*) FROM fact.fixture_result),"
              " (SELECT count(*) FROM ref.team),"
              " (SELECT count(*) FROM ops.ingest_log WHERE topic = 'fd_align')")


def counts(conn) -> dict[str, int]:
    """四张表的关键行数 —— 幂等与否全靠它对账。"""
    row = conn.execute(COUNTS_SQL).fetchone()
    return {"fact_fixture": row[0], "fact_fixture_result": row[1],
            "ref_team": row[2], "fd_align_log": row[3]}


def block_counts(conn, hashes: list[str] | None) -> dict[str, int]:
    """输入规模：raw 块数（**只算 http_status=200** —— 403/失败块绝不物化）。"""
    out: dict[str, int] = {}
    for table in ("af_raw", "fd_raw"):
        query = sql.SQL("SELECT count(*) FROM raw.{} WHERE http_status = 200").format(sql.Identifier(table))
        if hashes:
            query = sql.SQL("SELECT count(*) FROM raw.{} WHERE http_status = 200 "
                            "AND params_hash = ANY(%s)").format(sql.Identifier(table))
            out[table] = conn.execute(query, (hashes,)).fetchone()[0]
        else:
            out[table] = conn.execute(query).fetchone()[0]
    return out


def materialize(conn, hashes: list[str] | None, do_fixtures: bool = True,
                do_results: bool = True) -> dict[str, Any]:
    """跑物化。**不 commit** —— 提交/回滚由调用方决定（预演与真跑走同一条路径）。"""
    before = counts(conn)
    stats: dict[str, Any] = {}
    if do_fixtures:
        stats["fixtures"] = run_fixtures(conn, hashes)
    if do_results:
        stats["results"] = run_results(conn, hashes)
    return {"before": before, "in_tx": counts(conn), "stats": stats}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。退出码：0=成功；1=物化失败；2=参数/环境错误（**不静默**）。"""
    ap = argparse.ArgumentParser(description="raw→fact 物化（默认预演，不写库）")
    ap.add_argument("--execute", action="store_true", help="真写库（缺省 = 预演并回滚）")
    ap.add_argument("--params-hashes", default="",
                    help="逗号分隔的 raw params_hash；缺省 = 全部 200 块")
    ap.add_argument("--fixtures-only", action="store_true", help="只物化场次（ref.team + fact.fixture）")
    ap.add_argument("--results-only", action="store_true", help="只物化赛果（fact.fixture_result）")
    args = ap.parse_args(argv)
    if args.fixtures_only and args.results_only:
        logger.error("--fixtures-only 与 --results-only 不能同时给")
        return 2
    hashes = [h.strip() for h in args.params_hashes.split(",") if h.strip()]
    try:
        conn = pg.connect("ing")
    except Exception as exc:  # noqa: BLE001 — 连不上就明说，绝不降级到别的角色
        logger.error("连不上库（ing 角色）：%s: %s", type(exc).__name__, exc)
        return 2
    try:
        payload = materialize(conn, hashes or None, not args.results_only, not args.fixtures_only)
        if args.execute:
            conn.commit()
        else:
            conn.rollback()          # 预演：把刚才写的东西**全部撤销**
        payload["after"] = counts(conn)
        payload["mode"] = "execute" if args.execute else "dry-run"
        payload["written"] = bool(args.execute)
        payload["scope"] = {"params_hashes": hashes or None, "blocks": block_counts(conn, hashes or None)}
    except Exception as exc:  # noqa: BLE001 — 失败必须出声（建议 3 的「静默失败」教训）
        conn.rollback()
        logger.error("物化失败：%s: %s", type(exc).__name__, exc)
        return 1
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, default=str))
    if not args.execute:
        logger.info("[预演] 未写库（已回滚）；真物化请加 --execute")
    else:
        b, a = payload["before"], payload["after"]
        logger.info("[物化] fact.fixture %s→%s / fact.fixture_result %s→%s / ref.team %s→%s",
                    b["fact_fixture"], a["fact_fixture"], b["fact_fixture_result"], a["fact_fixture_result"],
                    b["ref_team"], a["ref_team"])
    return 0


if __name__ == "__main__":
    # ⚠️ 这个入口不能少：少了它，`python scripts/store/materialize.py` 会**静默什么都不做**
    # （模块被导入即结束 ⇒ 退出码 0、零输出、零写入），看起来像「跑了但没数据」。
    raise SystemExit(main())
