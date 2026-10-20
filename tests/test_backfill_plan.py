"""run_backfill 计划/编排回归锁：计划集与端点形态（纯函数）+ 真库单事务 ROLLBACK（零留痕、零真网络），连不上库即 skip。
钉死 E/F（AF 15 + FD 15、五联赛 × 三赛季、无源 id 的联赛不进计划、端点与 params 形态逐字）、D（配额到点既不发请求也不当
错误）、--plan 零 HTTP、--limit 只发 N 条、两源限速 7s；断言只挂哨兵赛季 SENTINEL 与假账本，绝不折算 raw/ops 真库存量。"""

from __future__ import annotations

import datetime as dt
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from core.leagues import LEAGUE_CONFIG  # noqa: E402
from ingest import api_get, quota, run_backfill  # noqa: E402
from store import pg  # noqa: E402

AF_EP, SENTINEL = "https://v3.football.api-sports.io/fixtures", "2099"  # 哨兵赛季：真库绝不会有这个键


@pytest.fixture()
def conn():
    try:
        connection = pg.connect("ing")
    except Exception as exc:
        pytest.skip(f"no db: {type(exc).__name__}: {exc}")
    connection.autocommit = True    # api_get 守卫只认 autocommit 连接（非 autocommit 必抛 RuntimeError）
    connection.execute("BEGIN")     # 手开真事务：run() 的每条事务降级成 SAVEPOINT，收尾 rollback，绝不提交
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture()
def tap(monkeypatch) -> SimpleNamespace:
    """零真网络假世界：假 urlopen（恒 200，只记 URL）+ 假 sleep（只记退避）+ 假账本（余额 left、charge 只记账）。
    真库 ops.quota_ledger 是全库共享的当日存量，真跑会改，用例一律不读也不花它 ⇒ 结果与真库存量无关。"""
    state = SimpleNamespace(calls=[], sleeps=[], spent=[], left=100)
    def urlopen(req, timeout=None):
        state.calls.append(req.full_url)
        return nullcontext(SimpleNamespace(status=200, read=lambda: b'{"ok": 1}'))
    def charge(_c, _d, src, cap=100, n=1):
        state.left -= n
        state.spent.append((src, n))
        return cap - state.left
    monkeypatch.setattr(api_get.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(run_backfill.time, "sleep", state.sleeps.append)
    monkeypatch.setattr(quota, "charge", charge)
    monkeypatch.setattr(quota, "remaining", lambda _c, _d, _s, cap: min(state.left, cap))
    return state


def test_plan_rows_shape_endpoints_and_id_screen():
    """E+F：AF 18 + FD 15（六联赛 × 三赛季 − 欧冠无 FD 源）、端点与 params 形态逐字；
    无源 id 的联赛（nba）不进计划。"""
    rows = run_backfill.plan_rows()
    af, fd = [r for r in rows if r["table"] == "af_raw"], [r for r in rows if r["table"] == "fd_raw"]
    # 欧冠（ucl）只有 AF 源（football-data.org 没有欧冠 ⇒ 无 league_id）⇒ AF 6 联赛 × 3 季 = 18，
    # FD 仍 5 联赛 × 3 季 = 15。
    assert (len(rows), len(af), len(fd)) == (33, 18, 15)
    assert {r["node"].split()[1] for r in rows} == set(LEAGUE_CONFIG) - {"nba"}  # nba 在配置里但无源 id
    assert {r["node"].split()[2] for r in rows} == set(run_backfill.SEASONS)
    assert {r["endpoint"] for r in af} == {AF_EP}
    assert {r["params"]["league"] for r in af} == {39, 140, 78, 135, 61, 2}
    assert {r["endpoint"].rsplit("/", 2)[1] for r in fd} == {"PL", "PD", "BL1", "SA", "FL1"}
    assert all(set(r["params"]) == {"season"} for r in fd)


def test_plan_mode_is_read_only_then_marks_cached(conn, tap, caplog):
    """--plan 零 HTTP（计划全待取 + 两源剩余满额）；真取过一条即变「已有」，其余仍待取（重跑不发同一条）。"""
    rows = run_backfill.plan_rows(("af", "fd"), (SENTINEL,))  # 哨兵赛季：真库无此键 ⇒ 待取数由计划集本身给定
    assert run_backfill.main(["--plan", "--seasons", SENTINEL], conn=conn) == 0
    assert tap.calls == [] and caplog.text.count("待取") == len(rows)
    for src, cap in (("api_football", 100), ("football_data", 30)):
        assert f"{src} 今日剩余 {quota.remaining(conn, dt.date.today(), src, cap)}/{cap}" in caplog.text
    with conn.transaction():  # 真取一条：真连接真事务落块，收尾随外层 ROLLBACK 一起消失
        api_get.fetch(conn, **{k: rows[0][k] for k in ("source", "table", "endpoint", "params", "cap")})
    caplog.clear()
    assert run_backfill.main(["--source", "af", "--seasons", SENTINEL], conn=conn) == 0
    af = [r for r in rows if r["table"] == "af_raw"]
    assert (len(tap.calls), caplog.text.count("已有"), caplog.text.count("待取")) == (1, 1, len(af) - 1)


@pytest.mark.parametrize("src", ("af", "fd"))
def test_execute_respects_limit_and_fd_pacing(conn, tap, src):
    """--limit 3 只发 3 条且每条恰好花 1 次配额；两源条间都要 sleep 7s（免费档 10 req/min，第 11 条起 429）。"""
    assert run_backfill.main(["--execute", "--source", src, "--limit", "3", "--seasons", SENTINEL], conn=conn) == 0
    assert (len(tap.calls), tap.sleeps, len(tap.spent)) == (3, [7, 7], 3)


def test_quota_exhausted_is_clean_shutdown(conn, tap, caplog):
    """D：预检到点 → 原样抛 QuotaExceeded 且零 HTTP；编排层把「今日到此为止」当正常收尾 return 0。"""
    tap.left = 0  # 今日到点：假账本余额归零（不碰真账本，也不靠把真账本花光来造状态）
    with pytest.raises(quota.QuotaExceeded):
        api_get.fetch(conn, source="api_football", table="af_raw", endpoint=AF_EP, cap=100,
                      params={"league": 39, "season": SENTINEL})
    args = ["--execute", "--source", "af", "--limit", "3", "--seasons", SENTINEL]
    assert run_backfill.main(args, conn=conn) == 0 and tap.calls == [] and "今日到此为止" in caplog.text
