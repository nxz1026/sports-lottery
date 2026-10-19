"""P0-backfill 编排：五联赛 × 2022-2024 赛季，AF/FD 各 15 条；默认只读 --plan（零 HTTP），--execute 才发请求；每条一个
真事务（进块前钉死 autocommit，否则 SELECT 的隐式事务会让它退化成 SAVEPOINT，§9-19）；两源免费档都限 10 req/min ⇒ 条间
sleep 7s；配额到点或 429 即正常收工；收尾自审「发出 N/落块 M/命中 K」，M != N 即点名；策略表/--allow-paid 见 config。"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from contextlib import closing, nullcontext

from config import allow_paid_once, describe
from core.leagues import LEAGUE_CONFIG
from core.log import logger
from psycopg import Connection, sql
from store import pg

from ingest import api_get, quota

SEASONS = ("2022", "2023", "2024")
SOURCES: dict[str, tuple[str, str, int, str, str]] = {  # cli 名 → (source, raw 表, cap, id 字段, 端点模板)
    "af": ("api_football", "af_raw", 100, "api_football_id", "https://v3.football.api-sports.io/fixtures"),
    "fd": ("football_data", "fd_raw", 30, "league_id", "https://api.football-data.org/v4/competitions/{code}/matches")}
PAUSE_S, DEFAULT_LIMIT, MAX_LIMIT = 7, 10, 20  # 两源免费档均 10 req/min（实测第 11 条起 429；AF 另限 100/天）
PROBE = {t: sql.SQL("SELECT 1 FROM {} WHERE params_hash = %s").format(i) for t, i in api_get.RAW.items()}

def plan_rows(sources: tuple[str, ...] = ("af", "fd"), seasons: tuple[str, ...] = SEASONS) -> list[dict]:
    """计划集 = 源 × 可回填联赛 × 赛季；联赛缺该源 id（如 nba）一律排除，绝不把 None 拼进 URL。"""
    rows = []
    for key in sources:
        src, table, cap, ref_key, url = SOURCES[key]
        for league, ref in [(lg, c.get(ref_key)) for lg, c in LEAGUE_CONFIG.items() if c.get(ref_key)]:
            endpoint = url if key == "af" else url.format(code=ref)
            for season in seasons:
                params = {**({"league": ref} if key == "af" else {}), "season": season}
                rows.append({"node": f"{src} {league} {season}", "hash": api_get.params_hash(src, endpoint, params),
                             "source": src, "table": table, "cap": cap, "endpoint": endpoint, "params": params})
    return rows

def run(conn: Connection, rows: list[dict], *, dry: bool, limit: int) -> int:
    """dry 只打印「待取/已有」零 HTTP；否则每条一个真事务取数并落 raw，429/配额到点即收工（0）/真异常（1）。"""
    if not conn.autocommit: conn.autocommit = True  # §9-19 根因：非 autocommit 时上面的 SELECT 已开隐式事务
    if dry:  # 只读：逐条标「待取/已有」
        for row in rows:
            logger.info(f"{row['node']} {'已有' if api_get.cached(conn, row['table'], row['hash']) else '待取'}")
        logger.info(f"计划 {len(rows)} 条全部列毕")
        return 0
    todo = [r for r in rows if api_get.cached(conn, r["table"], r["hash"]) is None]  # 待取（失败块不在其列）
    hits = len(rows) - len(todo)
    sent = landed = rc = 0
    for row in todo[:limit]:
        if sent:  # 两源都限 10 req/min（实测第 11 条起 429）：第一条之前不睡
            time.sleep(PAUSE_S)
        try:
            with conn.transaction():
                status, _ = api_get.fetch(conn, source=row["source"], table=row["table"],
                                          endpoint=row["endpoint"], params=row["params"], cap=row["cap"])
        except quota.QuotaExceeded as exc:
            logger.info(f"今日到此为止（{exc}）；本轮已发 {sent} 条")
            break
        except Exception as exc:
            logger.error(f"{row['node']} 失败：{type(exc).__name__}: {exc}；本轮已发 {sent} 条")
            rc = 1
            break
        sent += 1
        landed += conn.execute(PROBE[row["table"]], (row["hash"],)).fetchone() is not None  # 真落库才算落块
        if status == 429:  # 429 之后剩下的条一条都不再发（再打就是拿配额自杀）
            logger.warning("限流，本轮到此为止（10 req/min）")
            break
    logger.info(f"本轮发出 {sent} 条 / 新落块 {landed} 块 / 命中缓存 {hits} 块")
    if landed != sent:  # 配额花了没落块 = 本单事故
        logger.error(f"自审异常：发出 {sent} 条只落 {landed} 块（差额 {sent - landed} = 配额花了没留痕）")
    return rc

def main(argv: list[str] | None = None, conn: Connection | None = None) -> int:
    """CLI 入口；conn 仅供测试注入（自开时用裸连接，autocommit 后 conn.transaction() 才真发 BEGIN/COMMIT）。"""
    ap = argparse.ArgumentParser(description="P0-backfill：AF/FD 五联赛 2022-2024 回填（默认只读）")
    ap.add_argument("--plan", action="store_true", help="只读打印计划与配额剩余（默认行为）")
    ap.add_argument("--execute", action="store_true", help=f"真发请求取数（每条独立事务，单轮上限 {MAX_LIMIT}）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"--execute 单轮上限（默认 {DEFAULT_LIMIT}）")
    ap.add_argument("--source", choices=("af", "fd", "all"), default="all", help="只跑一源，默认两源")
    ap.add_argument("--seasons", default=",".join(SEASONS), help="逗号分隔赛季，默认 2022,2023,2024")
    ap.add_argument("--allow-paid", action="store_true", help="单次放行付费源：写进程内 env LEAGUE_ALLOW_PAID=on，不落 .env")
    args = ap.parse_args(argv)
    if args.allow_paid: allow_paid_once()
    with (closing(pg.connect("ing")) if conn is None else nullcontext(conn)) as conn:
        # 拿到连接立刻钉死提交权（本单根因 §9-19）：非 autocommit 时 cached() 的 SELECT 先开隐式事务 ⇒ conn.transaction()
        # 退化成 SAVEPOINT、RELEASE 不是提交 ⇒ 关连接整体回滚（15 次请求无痕蒸发）；run() 里同一处置，两处都不可少。
        if not conn.autocommit: conn.autocommit = True
        rows = plan_rows(("af", "fd") if args.source == "all" else (args.source,),
                         tuple(s.strip() for s in args.seasons.split(",") if s.strip()))
        if args.execute:
            return run(conn, rows, dry=False, limit=min(max(args.limit, 0), MAX_LIMIT))
        for row in describe():  # 策略表（README §2-D9）：关闭的源在计划顶部就看得见，why 点名控制的 env 变量
            logger.info(f"[策略] {row['source']} {'可用' if row['spend'] else '关闭'} cap={row['daily_cap']} {row['why']}")
        for src, _, cap, _, _ in SOURCES.values():
            logger.info(f"{src} 今日剩余 {quota.remaining(conn, dt.date.today(), src, cap)}/{cap}")
        return run(conn, rows, dry=True, limit=0)
if __name__ == "__main__":
    raise SystemExit(main())
